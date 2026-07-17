# ABOUTME: Review-queue, correction, and disambiguation write services —
# ABOUTME: the resolution/review block of the MCP tools (issue #120).
"""Review / correction / disambiguation services for the MCP server (Slice C).

review_queue surfaces five queues; propose_correction queues a non-destructive
fix; resolve_correction drains one by dispatching to the matching repair tool
(split/rename/retract) under an atomic conditional UPDATE; request/resolve
disambiguation move a token-keyed user choice through its lifecycle.
"""

import json

from django.db import transaction

from openbrain.brain.db import brain_cursor, dictfetchall, parse_json
from openbrain.brain.services import entities

# Special status string surfaced verbatim to the consuming LLM so callers know to
# surface the options to the user instead of guessing.
DISAMBIGUATION_STATUS = "awaiting_user_disambiguation"

# Context marker distinguishing a provisional-binding disambiguation — opened at
# capture time by the capture-then-reconcile path (#8) — from a plain caller-driven
# request_disambiguation. resolve_disambiguation keys its reconciliation on it.
PROVISIONAL_BINDING_KIND = "provisional_binding"

_INSERT_DISAMBIGUATION_ON_CURSOR_SQL = """
    insert into brain.disambiguations (question, options, context)
         values (%s, %s::jsonb, %s::jsonb)
      returning token::text as token
"""


def open_provisional_binding_on_cursor(
    cursor,
    *,
    experience_id: str,
    surface: str,
    field: str,
    entity_kind: str,
    candidate_entity_id: str,
    candidate_name: str,
    trgm_score: float,
    verification_score: float,
) -> dict:
    """Open a provisional-binding disambiguation on the caller's cursor (#8).

    Records — atomically with the capture, hence the on-cursor variant like
    merge_entities_on_cursor — a pending disambiguation whose context links the
    experience + mention to the best-guess entity it was bound to. Confirm keeps
    the bind; reject repoints the mention onto a fresh entity. Returns the
    needs_disambiguation block (token + question + options + candidate ids) the
    capture response carries so the caller can reconcile without re-resolving.
    """
    options = [
        {
            "label": f"Same as {candidate_name}",
            "value": {"action": "confirm", "entity_id": candidate_entity_id},
        },
        {
            "label": f'Different — "{surface}" is another {entity_kind}',
            "value": {"action": "reject"},
        },
    ]
    question = (
        f'Is "{surface}" the same {entity_kind} as the existing "{candidate_name}"?'
    )
    context = {
        "kind": PROVISIONAL_BINDING_KIND,
        "experience_id": experience_id,
        "surface": surface,
        "field": field,
        "entity_kind": entity_kind,
        "provisional_entity_id": candidate_entity_id,
        "candidate_entity_ids": [candidate_entity_id],
        "trgm_score": trgm_score,
        "verification_score": verification_score,
    }
    cursor.execute(
        _INSERT_DISAMBIGUATION_ON_CURSOR_SQL,
        [question, json.dumps(options), json.dumps(context)],
    )
    token = dictfetchall(cursor)[0]["token"]
    return {
        "surface": surface,
        "provisional_entity_id": candidate_entity_id,
        "candidate_entity_ids": [candidate_entity_id],
        "token": token,
        "question": question,
        "options": options,
    }


# Impact gate for pending merge candidates (mindgrapes-server#18): a pair of
# claim-free, one-mention-or-less concepts carries no retrievable stakes, so it
# is hidden — not resolved — and resurfaces on its own if either entity gains a
# mention or claim. Mentions roll up over merged_into like _mention_counts so
# the gate agrees with the counts the workbench displays. Fragment expects the
# mc/ea/eb aliases; shared with mcp_reads._PENDING_REVIEWS_SQL so the badge
# count matches the visible list.
LOW_IMPACT_MERGE_SQL = """
    ea.kind = 'concept' and eb.kind = 'concept'
    and not exists (
        select 1 from brain.claims c
         where c.polarity <> 'retracted'
           and (c.subject_id in (mc.entity_a, mc.entity_b)
                or c.object_entity_id in (mc.entity_a, mc.entity_b)))
    and (select count(distinct m.experience_id)
           from brain.mentions m
           join brain.entities me on me.id = m.entity_id
          where coalesce(me.merged_into, me.id) = mc.entity_a) <= 1
    and (select count(distinct m.experience_id)
           from brain.mentions m
           join brain.entities me on me.id = m.entity_id
          where coalesce(me.merged_into, me.id) = mc.entity_b) <= 1
"""


def _load_merge_candidates(cursor) -> tuple[list[dict], int]:
    # ponytail: correlated subqueries per pending pair — fine at review-queue
    # scale, same rationale as _mention_counts.
    cursor.execute(
        f"""
        select mc.id::text, mc.entity_a::text as entity_a,
               mc.entity_b::text as entity_b, mc.similarity, mc.created_at,
               ({LOW_IMPACT_MERGE_SQL}) as low_impact
          from brain.merge_candidates mc
          join brain.entities ea on ea.id = mc.entity_a
          join brain.entities eb on eb.id = mc.entity_b
         where mc.status = 'pending'
         order by mc.similarity desc, mc.created_at
        """
    )
    rows = dictfetchall(cursor)
    visible: list[dict] = []
    deferred = 0
    for row in rows:
        if row.pop("low_impact"):
            deferred += 1
            continue
        row["similarity"] = float(row["similarity"])
        visible.append(row)
    return visible, deferred


def _load_low_confidence_claims(cursor) -> list[dict]:
    cursor.execute(
        """
        select c.id::text          as claim_id,
               c.subject_id::text  as subject_id,
               c.predicate         as predicate,
               c.confidence        as confidence,
               cs.support_kind::text as support_kind
          from brain.claims c
          join brain.claim_sources cs on cs.claim_id = c.id
         where c.polarity <> 'retracted'
           and cs.support_kind = 'inferred'
           and c.confidence < 0.6
         order by c.confidence asc
         limit 200
        """
    )
    rows = dictfetchall(cursor)
    for row in rows:
        row["confidence"] = float(row["confidence"])
    return rows


def _load_contradictions(cursor) -> list[dict]:
    cursor.execute(
        """
        select c.id::text             as claim_id,
               c.superseded_by::text  as superseded_by,
               c.subject_id::text     as subject_id,
               c.predicate            as predicate
          from brain.claims c
         where c.superseded_by is not null
           and c.polarity <> 'retracted'
         order by c.created_at desc
         limit 200
        """
    )
    return dictfetchall(cursor)


def _load_disambiguations(cursor) -> list[dict]:
    cursor.execute(
        """
        select token::text as token, question, options, created_at
          from brain.disambiguations
         where status = 'pending'
         order by created_at
        """
    )
    rows = dictfetchall(cursor)
    for row in rows:
        row["options"] = parse_json(row["options"])
    return rows


def _load_proposed_corrections(cursor) -> list[dict]:
    cursor.execute(
        """
        select id::text, target_kind::text as target_kind, target_id::text as target_id,
               suggested_change, reason, created_at
          from brain.proposed_corrections
         where status = 'pending'
         order by created_at
        """
    )
    rows = dictfetchall(cursor)
    for row in rows:
        row["suggested_change"] = parse_json(row["suggested_change"])
    return rows


# Ordered so the result object's keys keep the shipped review_queue shape.
_REVIEW_LOADERS = (
    ("merge_candidates", _load_merge_candidates),
    ("low_confidence_claims", _load_low_confidence_claims),
    ("contradictions", _load_contradictions),
    ("disambiguations", _load_disambiguations),
    ("proposed_corrections", _load_proposed_corrections),
)


def review_queue(kind: str = "all") -> dict:
    """Return items awaiting human review across five surfaces.

    kind scopes to one surface; default 'all' returns every queue. Read-only.
    merge_candidates_deferred counts the pending pairs hidden by the low-impact
    gate (LOW_IMPACT_MERGE_SQL) so the truncation is visible, not silent.
    """
    result: dict = {key: [] for key, _ in _REVIEW_LOADERS}
    result["merge_candidates_deferred"] = 0
    with brain_cursor() as cursor:
        for key, loader in _REVIEW_LOADERS:
            if kind != "all" and kind != key:
                continue
            if key == "merge_candidates":
                result[key], result["merge_candidates_deferred"] = loader(cursor)
            else:
                result[key] = loader(cursor)
    return result


def attach_entity_names(rows: list[dict], surface: str) -> list[dict]:
    """Attach display names for the entity UUIDs a review surface carries.

    review_queue returns bare UUIDs (the MCP ReviewQueueResult shape is fixed and
    must not change), but the web workbench needs names to show a merge's two
    entities and to deep-link a low-confidence / contradicted claim's subject. One
    batched lookup over brain.entities; a UUID with no live row (hard-deleted
    since) falls back to None rather than raising. Rows are mutated in place and
    returned for convenience.
    """
    if surface == "merge_candidates":
        id_fields = ("entity_a", "entity_b")
    elif surface in ("low_confidence_claims", "contradictions"):
        id_fields = ("subject_id",)
    else:
        return rows
    if not rows:
        return rows

    ids = {row[f] for row in rows for f in id_fields if row.get(f)}
    lookup: dict[str, dict] = {}
    if ids:
        with brain_cursor() as cursor:
            cursor.execute(
                """
                select id::text, canonical_name, kind::text as kind,
                       merged_into::text as merged_into
                  from brain.entities
                 where id = any(%s::uuid[])
                """,
                [list(ids)],
            )
            lookup = {row["id"]: row for row in dictfetchall(cursor)}

    counts = _mention_counts(rows, lookup) if surface == "merge_candidates" else {}

    def _live_id(side_id):
        ent = lookup.get(side_id)
        return ent["merged_into"] if ent and ent["merged_into"] else side_id

    for row in rows:
        if surface == "merge_candidates":
            for side in ("entity_a", "entity_b"):
                ent = lookup.get(row.get(side))
                row[f"{side}_name"] = ent["canonical_name"] if ent else None
                row[f"{side}_kind"] = ent["kind"] if ent else None
                row[f"{side}_count"] = counts.get(_live_id(row.get(side)), 0)
        else:
            ent = lookup.get(row.get("subject_id"))
            row["subject_name"] = ent["canonical_name"] if ent else None
    return rows


def _mention_counts(rows: list[dict], lookup: dict[str, dict]) -> dict[str, int]:
    """Distinct-experience mention count per LIVE entity for the merge pairs.

    A merge is a soft merged_into pointer (mentions aren't rewritten), so the
    join resolves each mention onto coalesce(merged_into, id) — the count reflects
    the live entity the user is actually deciding about, rolling up any already-
    merged children. Covered by mentions_entity_idx.

    # ponytail: scans mentions x entities for the pair's live ids — fine at the
    # single-user scale this queue serves; revisit if the queue ever fans out.
    """
    live_ids = set()
    for row in rows:
        for side in ("entity_a", "entity_b"):
            sid = row.get(side)
            if not sid:
                continue
            ent = lookup.get(sid)
            live_ids.add(ent["merged_into"] if ent and ent["merged_into"] else sid)
    if not live_ids:
        return {}
    with brain_cursor() as cursor:
        cursor.execute(
            """
            select coalesce(e.merged_into, e.id)::text as live_id,
                   count(distinct m.experience_id)::int as cnt
              from brain.mentions m
              join brain.entities e on e.id = m.entity_id
             where coalesce(e.merged_into, e.id) = any(%s::uuid[])
             group by 1
            """,
            [list(live_ids)],
        )
        return {r["live_id"]: r["cnt"] for r in dictfetchall(cursor)}


def merge_candidate_evidence(viewer: str, candidate_id: str) -> dict | None:
    """Up to 2 example experiences per side of a pending merge candidate (#155).

    Reuses the viewer-scoped mention->experience read (reads.get_entity_mentions),
    so the evidence peek never surfaces another member's private experiences.
    Returns None when the candidate row is gone (bad id, or resolved + pruned) so
    the view can render nothing rather than 500.
    """
    from openbrain.brain.services import reads

    with brain_cursor() as cursor:
        cursor.execute(
            """
            select mc.entity_a::text as entity_a, mc.entity_b::text as entity_b,
                   ea.canonical_name as a_name, eb.canonical_name as b_name
              from brain.merge_candidates mc
              left join brain.entities ea on ea.id = mc.entity_a
              left join brain.entities eb on eb.id = mc.entity_b
             where mc.id = %s::uuid
            """,
            [candidate_id],
        )
        rows = dictfetchall(cursor)
    if not rows:
        return None
    cand = rows[0]

    def _side(entity_id, name):
        page = reads.get_entity_mentions(viewer, entity_id, 2, 0)
        return {"id": entity_id, "name": name, "experiences": page["mentions"]}

    return {
        "a": _side(cand["entity_a"], cand["a_name"]),
        "b": _side(cand["entity_b"], cand["b_name"]),
    }


def propose_correction(
    target_kind: str,
    target_id: str,
    suggested_change: dict,
    *,
    reason: str | None = None,
) -> dict:
    """Queue a non-destructive correction proposal in 'pending' status."""
    with brain_cursor() as cursor:
        cursor.execute(
            """
            insert into brain.proposed_corrections
                 (target_kind, target_id, suggested_change, reason)
                 values (%s::brain.target_kind, %s::uuid, %s::jsonb, %s)
              returning id::text
            """,
            [target_kind, target_id, json.dumps(suggested_change or {}), reason],
        )
        new_id = dictfetchall(cursor)[0]["id"]
    return {"id": new_id, "status": "pending"}


def _assert_proposal_was_pending(proposal_id: str):
    """Raise a precise error when a conditional claim updated zero rows."""
    with brain_cursor() as cursor:
        cursor.execute(
            "select status from brain.proposed_corrections where id = %s::uuid",
            [proposal_id],
        )
        rows = dictfetchall(cursor)
    if not rows:
        raise ValueError(f"resolve_correction: proposal {proposal_id} not found")
    raise ValueError(
        f"resolve_correction: proposal {proposal_id} is already {rows[0]['status']}"
    )


def resolve_correction(
    proposal_id: str,
    decision: str,
    *,
    reason: str | None = None,
    created_by: str = "mcp:resolve_correction",
) -> dict:
    """Transition a proposed_corrections row pending -> applied/rejected.

    The row is claimed with a single conditional UPDATE (... where status =
    'pending'), which is atomic, so a concurrent resolve can't double-apply and
    we never hold a transaction open across the dispatched repair. On apply we
    dispatch only after the claim commits; each repair tool runs in its OWN
    transaction. If the dispatch fails we roll the row back to 'pending' so it
    can be retried.
    """
    if decision not in ("apply", "reject"):
        raise ValueError("resolve_correction: decision must be 'apply' or 'reject'")
    resolved_by = created_by

    if decision == "reject":
        with brain_cursor() as cursor:
            cursor.execute(
                """
                update brain.proposed_corrections
                   set status = 'rejected', resolved_at = now(), resolved_by = %s
                 where id = %s::uuid and status = 'pending'
              returning id::text
                """,
                [resolved_by, proposal_id],
            )
            rows = dictfetchall(cursor)
        if not rows:
            _assert_proposal_was_pending(proposal_id)
        return {"id": proposal_id, "decision": "reject", "status": "rejected"}

    # Atomically claim the row (capturing its payload) before dispatching.
    with brain_cursor() as cursor:
        cursor.execute(
            """
            update brain.proposed_corrections
               set status = 'applied', resolved_at = now(), resolved_by = %s
             where id = %s::uuid and status = 'pending'
          returning target_id::text as target_id, suggested_change
            """,
            [resolved_by, proposal_id],
        )
        claimed = dictfetchall(cursor)
    if not claimed:
        _assert_proposal_was_pending(proposal_id)
    target_id = claimed[0]["target_id"]
    suggested_change = parse_json(claimed[0]["suggested_change"])

    try:
        plan = plan_correction_dispatch(suggested_change, target_id)
        tool = plan["tool"]
        params = plan["params"]
        if tool == "split_entity":
            outcome = entities.split_entity(
                params["source_entity_id"],
                params["experience_ids"],
                params["into"],
                reason=params.get("reason") or reason,
                created_by=resolved_by,
            )
        elif tool == "rename_entity":
            outcome = entities.rename_entity(
                params["entity_id"],
                params["new_canonical_name"],
                reason=params.get("reason") or reason,
                created_by=resolved_by,
            )
        else:  # retract_claim
            outcome = entities.retract_claim(
                params["claim_id"],
                params.get("reason") or reason or "applied via resolve_correction",
                created_by=resolved_by,
            )
        return {
            "id": proposal_id,
            "decision": "apply",
            "status": "applied",
            "dispatched_tool": tool,
            "result": outcome,
        }
    except Exception:
        # Dispatch failed after the claim committed: undo the stamp so the
        # proposal returns to the queue rather than lingering as phantom 'applied'.
        try:
            with brain_cursor() as cursor:
                cursor.execute(
                    """
                    update brain.proposed_corrections
                       set status = 'pending', resolved_at = null, resolved_by = null
                     where id = %s::uuid and status = 'applied'
                    """,
                    [proposal_id],
                )
        except Exception:
            pass
        raise


def resolve_merge_candidate(
    candidate_id: str,
    decision: str,
    *,
    winner_id: str | None = None,
    reason: str | None = None,
    created_by: str = "ui:resolve_merge_candidate",
) -> dict:
    """Apply or dismiss a pending brain.merge_candidates row from the review UI.

    confirm merges the non-winner of the pair into winner_id via merge_entities
    (which appends aliases, sets merged_into, stamps the candidate 'merged', and
    audits). reject stamps the candidate 'kept_separate' without touching either
    entity — a "no, keep them apart" decision, so like resolve_disambiguation it
    writes no correction_event. A candidate that is no longer pending raises a
    precise 'already <status>' error so the web layer can render an idempotent
    "already handled" partial instead of a 500.
    """
    if decision not in ("confirm", "reject"):
        raise ValueError(
            "resolve_merge_candidate: decision must be 'confirm' or 'reject'"
        )

    with brain_cursor() as cursor:
        cursor.execute(
            """
            select entity_a::text as entity_a, entity_b::text as entity_b, status
              from brain.merge_candidates where id = %s::uuid
            """,
            [candidate_id],
        )
        rows = dictfetchall(cursor)
    if not rows:
        raise ValueError(f"resolve_merge_candidate: candidate {candidate_id} not found")
    candidate = rows[0]
    if candidate["status"] != "pending":
        raise ValueError(
            f"resolve_merge_candidate: candidate {candidate_id} is already "
            f"{candidate['status']}"
        )

    if decision == "reject":
        with brain_cursor() as cursor:
            cursor.execute(
                """
                update brain.merge_candidates
                   set status = 'kept_separate', resolved_at = now()
                 where id = %s::uuid and status = 'pending'
              returning id::text
                """,
                [candidate_id],
            )
            updated = dictfetchall(cursor)
        if not updated:
            # Lost a race between the status read and this claim.
            raise ValueError(
                f"resolve_merge_candidate: candidate {candidate_id} is already resolved"
            )
        return {"id": candidate_id, "decision": "reject", "status": "kept_separate"}

    pair = {candidate["entity_a"], candidate["entity_b"]}
    if winner_id not in pair:
        raise ValueError(
            "resolve_merge_candidate: winner_id must be one of the candidate's two "
            "entities"
        )
    loser_id = (pair - {winner_id}).pop()
    # merge_entities is atomic and stamps the merge_candidates row 'merged' itself;
    # a concurrent merge surfaces as a ValueError ('already merged into ...').
    outcome = entities.merge_entities(
        loser_id, winner_id, reason=reason, created_by=created_by
    )
    return {
        "id": candidate_id,
        "decision": "confirm",
        "status": "merged",
        "winner_id": winner_id,
        "loser_id": loser_id,
        "result": outcome,
    }


def request_disambiguation(
    question: str,
    options: list[dict],
    *,
    context: dict | None = None,
) -> dict:
    """Halt the task and ask the user to choose between options (>= 2)."""
    if not isinstance(options, list) or len(options) < 2:
        raise ValueError(
            "request_disambiguation: options must contain at least 2 entries"
        )
    if not question.strip():
        raise ValueError("request_disambiguation: question is required")

    with brain_cursor() as cursor:
        cursor.execute(
            """
            insert into brain.disambiguations (question, options, context)
                 values (%s, %s::jsonb, %s::jsonb)
              returning token::text as token
            """,
            [question, json.dumps(options), json.dumps(context) if context else None],
        )
        token = dictfetchall(cursor)[0]["token"]

    return {
        "status": DISAMBIGUATION_STATUS,
        "token": token,
        "question": question,
        "options": options,
    }


def resolve_disambiguation(token: str, choice) -> dict:
    """Apply the user's choice to a pending disambiguation token.

    For a plain disambiguation this writes NO correction_events: the
    disambiguations row itself (status, resolved_at, resolved_choice) is the audit
    trail. For a provisional-binding token (#8) it additionally reconciles the
    best-guess bind AFTER the token is stamped resolved and its transaction
    commits — mirroring resolve_correction, so the reconciling repair runs in its
    own transaction and never nests inside the token update. Reject repoints the
    mention via split_entity (which audits); confirm leaves the bind in place. If
    the reconciliation fails the token is reopened so the bind resurfaces in
    review_queue for another attempt.
    """
    with transaction.atomic(), brain_cursor() as cursor:
        cursor.execute(
            """
            select token::text as token, question, options, context, status
              from brain.disambiguations where token = %s::uuid for update
            """,
            [token],
        )
        rows = dictfetchall(cursor)
        if not rows:
            raise ValueError(f"resolve_disambiguation: token {token} not found")
        row = rows[0]
        if row["status"] != "pending":
            raise ValueError(
                f"resolve_disambiguation: token {token} is already {row['status']}"
            )

        options = parse_json(row["options"])
        resolved = match_choice(options, choice)
        if not resolved:
            raise ValueError(
                "resolve_disambiguation: choice did not match any of the "
                f"{len(options)} options"
            )

        cursor.execute(
            """
            update brain.disambiguations
               set status = 'resolved', resolved_choice = %s::jsonb, resolved_at = now()
             where token = %s::uuid
            """,
            [json.dumps(resolved), row["token"]],
        )

    context = parse_json(row["context"]) if row.get("context") else {}
    result = {
        "token": row["token"],
        "resolved_choice": resolved,
        "question": row["question"],
    }
    if context.get("kind") == PROVISIONAL_BINDING_KIND:
        try:
            result["reconciliation"] = _reconcile_provisional_binding(context, resolved)
        except Exception:
            try:
                with brain_cursor() as cursor:
                    cursor.execute(
                        """
                        update brain.disambiguations
                           set status = 'pending', resolved_choice = null,
                               resolved_at = null
                         where token = %s::uuid and status = 'resolved'
                        """,
                        [row["token"]],
                    )
            except Exception:
                pass
            raise
    return result


def _reconcile_provisional_binding(context: dict, resolved: dict) -> dict:
    """Apply a resolved provisional-binding choice (#8).

    confirm leaves the best-guess bind in place — the mention already points at
    the provisional entity, so there is nothing to move. reject repoints the
    mention onto a fresh entity for the surface via split_entity, undoing the
    guess with a correction_events audit trail.
    """
    action = (resolved.get("value") or {}).get("action")
    provisional_entity_id = context["provisional_entity_id"]
    if action == "confirm":
        return {"action": "confirmed", "entity_id": provisional_entity_id}
    if action == "reject":
        outcome = entities.split_entity(
            provisional_entity_id,
            [context["experience_id"]],
            {"canonical_name": context["surface"], "kind": context["entity_kind"]},
            reason="provisional binding rejected via resolve_disambiguation",
            created_by="mcp:resolve_disambiguation:reject",
        )
        return {"action": "repointed", **outcome}
    raise ValueError(
        "resolve_disambiguation: provisional-binding choice carries no "
        f"confirm/reject action: {resolved!r}"
    )


def fold_notes_into_metadata(into: dict) -> dict:
    """Fold a free-form `notes` string on a repoint target into its metadata.

    normalize_split_into only reads canonical_name/kind/aliases/metadata, so a
    `notes` field some propose_correction callers attach would be dropped; move
    it under metadata.notes instead.
    """
    notes = into.get("notes")
    if not isinstance(notes, str) or notes.strip() == "":
        return into
    rest = {k: v for k, v in into.items() if k != "notes"}
    existing = rest["metadata"] if isinstance(rest.get("metadata"), dict) else {}
    return {**rest, "metadata": {**existing, "notes": notes}}


def plan_correction_dispatch(suggested_change: dict, target_id: str) -> dict:
    """Map a proposed_corrections.suggested_change onto the repair tool to apply.

    The action vocabulary is convention (it mirrors what propose_correction
    callers write), not a SQL constraint, so an unknown action raises rather than
    silently no-opping. target_id is the proposal's target, used as the natural
    fallback for rename/retract ids and as the experience scope for a
    repoint_participant proposal filed against an experience.
    """
    action = suggested_change.get("action")
    if not isinstance(action, str) or not action:
        raise ValueError(
            "plan_correction_dispatch: suggested_change.action is required to "
            "apply a correction"
        )
    # suggested_change is free-form jsonb, so coerce a non-string reason to None
    # rather than letting it slip past the dispatched tool's reason validation.
    raw_reason = suggested_change.get("reason")
    reason = raw_reason if isinstance(raw_reason, str) else None

    if action == "repoint_participant":
        source = suggested_change.get("source_entity_id")
        if not isinstance(source, str):
            current = suggested_change.get("current_entity_id")
            source = current if isinstance(current, str) else None
        raw_ids = suggested_change.get("experience_ids")
        experience_ids = raw_ids if isinstance(raw_ids, list) else [target_id]
        raw_into = suggested_change.get("into")
        if raw_into is None:
            raw_into = suggested_change.get("new_entity")
        if (
            not isinstance(source, str)
            or len(experience_ids) == 0
            or not isinstance(raw_into, dict)
        ):
            raise ValueError(
                "plan_correction_dispatch: repoint_participant requires a source "
                "entity (source_entity_id or current_entity_id), an experience "
                "scope (experience_ids[] or the proposal target), and a target "
                "(into or new_entity)"
            )
        return {
            "tool": "split_entity",
            "params": {
                "source_entity_id": source,
                "experience_ids": experience_ids,
                "into": fold_notes_into_metadata(raw_into),
                "reason": reason,
            },
        }
    if action == "rename":
        new_name = suggested_change.get("new_canonical_name")
        if not isinstance(new_name, str) or not new_name.strip():
            raise ValueError(
                "plan_correction_dispatch: rename requires new_canonical_name"
            )
        entity_id = suggested_change.get("entity_id")
        return {
            "tool": "rename_entity",
            "params": {
                "entity_id": entity_id if isinstance(entity_id, str) else target_id,
                "new_canonical_name": new_name,
                "reason": reason,
            },
        }
    if action == "retract":
        claim_id = suggested_change.get("claim_id")
        return {
            "tool": "retract_claim",
            "params": {
                "claim_id": claim_id if isinstance(claim_id, str) else target_id,
                "reason": reason,
            },
        }
    raise ValueError(f"plan_correction_dispatch: unknown action '{action}'")


def match_choice(options: list[dict], choice) -> dict | None:
    """Resolve a disambiguation selection (index / label / object) to an option.

    A bool is rejected (it is an int subclass in Python but not a numeric
    choice); a numeric index is bounds-checked so a negative never
    wraps the way Python list indexing would.
    """
    if isinstance(choice, bool):
        return None
    if isinstance(choice, int):
        return options[choice] if 0 <= choice < len(options) else None
    if isinstance(choice, str):
        return next((o for o in options if o.get("label") == choice), None)
    if isinstance(choice, dict) and "label" in choice:
        return next((o for o in options if o.get("label") == choice["label"]), None)
    return None
