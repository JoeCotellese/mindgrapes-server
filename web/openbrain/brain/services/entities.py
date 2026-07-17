# ABOUTME: Entity & claim identity-repair write services — the identity
# ABOUTME: block of the MCP tools (merge/rename/retract/split/unmerge/resolve).
"""Entity-identity repair for the MCP server (issue #120, Slice C).

Every mutation runs in its own transaction and writes at least one
brain.correction_events row via record_correction, so each change is auditable
and reversible. split_entity is the
brain's only HARD reference rewrite (mentions + claims physically repointed);
the rest are soft (merge sets merged_into, reads chase the pointer).
"""

import json

from django.db import transaction

from openbrain.brain.db import (
    brain_cursor,
    dictfetchall,
    record_correction,
    to_vector_literal,
)
from openbrain.brain.embeddings import embed_query

EntityKind = ("person", "org", "event", "place", "concept")


def normalize_split_into(into: dict, source_kind: str) -> dict:
    """Decide whether split_entity mints a fresh entity or repoints an existing one.

    Exactly one of into.canonical_name (mint new) / into.entity_id (existing
    target) must be present; a blank canonical_name counts as absent so an empty
    string can't silently mint a nameless entity. kind defaults to the source
    entity's kind.
    """
    canonical = into.get("canonical_name")
    has_create = isinstance(canonical, str) and canonical.strip() != ""
    entity_id = into.get("entity_id")
    has_existing = isinstance(entity_id, str) and entity_id.strip() != ""

    if has_create == has_existing:
        raise ValueError(
            "normalize_split_into: provide exactly one of into.canonical_name "
            "(mint new) or into.entity_id (existing target)"
        )
    if has_existing:
        return {"mode": "existing", "entity_id": entity_id.strip()}
    return {
        "mode": "create",
        "canonical_name": canonical.strip(),
        "kind": into.get("kind") or source_kind,
        "aliases": into.get("aliases") or [],
        "metadata": into.get("metadata") or {},
    }


_ENTITY_FOR_UPDATE_SQL = """
    select id::text, kind::text as kind, canonical_name, aliases, merged_into::text
      from brain.entities where id = %s::uuid for update
"""


def merge_entities(
    loser_id: str,
    winner_id: str,
    *,
    reason: str | None = None,
    created_by: str = "mcp:merge_entities",
) -> dict:
    """Soft-merge loser into winner: append aliases, set merged_into, audit.

    Returns {loser_id, winner_id, correction_event_id, alias_appended}.
    """
    if loser_id == winner_id:
        raise ValueError("merge_entities: loser_id and winner_id must differ")

    with transaction.atomic(), brain_cursor() as cursor:
        cursor.execute(_ENTITY_FOR_UPDATE_SQL, [loser_id])
        loser_rows = dictfetchall(cursor)
        if not loser_rows:
            raise ValueError(f"merge_entities: loser_id {loser_id} not found")
        loser = loser_rows[0]
        if loser["merged_into"]:
            raise ValueError(
                f"merge_entities: loser_id {loser_id} is already merged into "
                f"{loser['merged_into']}"
            )

        cursor.execute(_ENTITY_FOR_UPDATE_SQL, [winner_id])
        winner_rows = dictfetchall(cursor)
        if not winner_rows:
            raise ValueError(f"merge_entities: winner_id {winner_id} not found")
        winner = winner_rows[0]
        if winner["merged_into"]:
            raise ValueError(
                f"merge_entities: winner_id {winner_id} is itself merged into "
                f"{winner['merged_into']}"
            )
        if loser["kind"] != winner["kind"]:
            raise ValueError(
                f"merge_entities: kind mismatch (loser={loser['kind']}, "
                f"winner={winner['kind']})"
            )

        # Append the loser's canonical_name + every alias onto the winner so a
        # future search by any historical surface form still resolves.
        incoming = [loser["canonical_name"], *loser["aliases"]]
        cursor.execute(
            """
            update brain.entities
               set aliases = coalesce((
                 select array_agg(distinct a)
                   from unnest(coalesce(aliases, '{}'::text[]) || %s::text[]) a
                  where a is not null
                    and a <> %s
               ), '{}'::text[])
             where id = %s::uuid
            returning aliases
            """,
            [incoming, winner["canonical_name"], winner["id"]],
        )
        new_aliases = dictfetchall(cursor)[0]["aliases"]
        alias_appended = len(new_aliases or []) > len(winner["aliases"])

        cursor.execute(
            "update brain.entities set merged_into = %s::uuid where id = %s::uuid",
            [winner["id"], loser["id"]],
        )

        # Mark any pending merge_candidates row covering this pair as resolved.
        cursor.execute(
            """
            update brain.merge_candidates
               set status = 'merged', resolved_at = now()
             where status = 'pending'
               and entity_a = least(%s::uuid, %s::uuid)
               and entity_b = greatest(%s::uuid, %s::uuid)
            """,
            [loser["id"], winner["id"], loser["id"], winner["id"]],
        )

        correction_id = record_correction(
            cursor,
            target_kind="entity",
            target_id=loser["id"],
            before={
                "canonical_name": loser["canonical_name"],
                "aliases": loser["aliases"],
                "merged_into": None,
            },
            after={"merged_into": winner["id"]},
            reason=reason,
            created_by=created_by,
        )

    return {
        "loser_id": loser["id"],
        "winner_id": winner["id"],
        "correction_event_id": correction_id,
        "alias_appended": alias_appended,
    }


def rename_entity(
    entity_id: str,
    new_canonical_name: str,
    *,
    reason: str | None = None,
    created_by: str = "mcp:rename_entity",
) -> dict:
    """Change an entity's canonical_name, preserving the old name as an alias."""
    new_name = new_canonical_name.strip()
    if not new_name:
        raise ValueError("rename_entity: new_canonical_name must be non-empty")

    with transaction.atomic(), brain_cursor() as cursor:
        cursor.execute(
            "select id::text, canonical_name, aliases, merged_into::text "
            "from brain.entities where id = %s::uuid for update",
            [entity_id],
        )
        rows = dictfetchall(cursor)
        if not rows:
            raise ValueError(f"rename_entity: entity {entity_id} not found")
        e = rows[0]
        if e["canonical_name"] == new_name:
            # No-op rename. Skip the audit row entirely; correction_events should
            # only fire on actual state transitions.
            return {
                "entity_id": e["id"],
                "old_canonical_name": e["canonical_name"],
                "new_canonical_name": new_name,
                "correction_event_id": "",
            }

        cursor.execute(
            """
            update brain.entities
               set canonical_name = %s,
                   aliases = (
                     select array_agg(distinct a)
                       from unnest(coalesce(aliases, '{}'::text[]) || array[%s]::text[]) a
                      where a is not null and a <> %s
                   )
             where id = %s::uuid
            """,
            [new_name, e["canonical_name"], new_name, e["id"]],
        )
        correction_id = record_correction(
            cursor,
            target_kind="entity",
            target_id=e["id"],
            before={"canonical_name": e["canonical_name"], "aliases": e["aliases"]},
            after={"canonical_name": new_name},
            reason=reason,
            created_by=created_by,
        )

    return {
        "entity_id": e["id"],
        "old_canonical_name": e["canonical_name"],
        "new_canonical_name": new_name,
        "correction_event_id": correction_id,
    }


def retract_claim(
    claim_id: str,
    reason: str,
    *,
    created_by: str = "mcp:retract_claim",
) -> dict:
    """Mark a claim retracted (polarity='retracted') so it stops surfacing."""
    if not reason or not reason.strip():
        raise ValueError("retract_claim: reason is required for audit trail")

    with transaction.atomic(), brain_cursor() as cursor:
        cursor.execute(
            "select id::text, polarity::text as polarity "
            "from brain.claims where id = %s::uuid for update",
            [claim_id],
        )
        rows = dictfetchall(cursor)
        if not rows:
            raise ValueError(f"retract_claim: claim {claim_id} not found")
        c = rows[0]
        if c["polarity"] == "retracted":
            raise ValueError(f"retract_claim: claim {claim_id} is already retracted")

        cursor.execute(
            "update brain.claims set polarity = 'retracted' where id = %s::uuid",
            [c["id"]],
        )
        correction_id = record_correction(
            cursor,
            target_kind="claim",
            target_id=c["id"],
            before={"polarity": c["polarity"]},
            after={"polarity": "retracted"},
            reason=reason,
            created_by=created_by,
        )

    return {
        "claim_id": c["id"],
        "prior_polarity": c["polarity"],
        "correction_event_id": correction_id,
    }


def _repoint_entity_references(
    cursor,
    *,
    source_id: str,
    target_id: str,
    experience_ids: list[str],
    reason: str | None,
    created_by: str,
) -> dict:
    """HARD reference rewrite scoped to a set of experiences (caller's txn).

    Moves every binding of source_id onto target_id across mentions.entity_id,
    claims.subject_id, and claims.object_entity_id (claims in scope via any
    claim_sources row pointing at experience_ids). One correction_events row is
    written only when something actually moved, recording enough to reverse the
    repoint by running it back source<->target over the same experience set.
    """
    # Move mentions via insert-then-delete: the mentions PK is
    # (experience_id, entity_id, surface_form, field), so a bare `set entity_id`
    # collides when the target already carries the same surface form in the same
    # experience. `on conflict do nothing` keeps the existing target row; the
    # delete then clears the source binding either way.
    cursor.execute(
        """
        insert into brain.mentions (experience_id, entity_id, surface_form, field, created_at)
             select experience_id, %s::uuid, surface_form, field, created_at
               from brain.mentions
              where entity_id = %s::uuid
                and experience_id = any(%s::uuid[])
        on conflict do nothing
        """,
        [target_id, source_id, experience_ids],
    )
    cursor.execute(
        "delete from brain.mentions "
        "where entity_id = %s::uuid and experience_id = any(%s::uuid[])",
        [source_id, experience_ids],
    )
    mentions_repointed = cursor.rowcount or 0

    cursor.execute(
        """
        update brain.claims c
           set subject_id = %s::uuid
         where c.subject_id = %s::uuid
           and exists (
             select 1 from brain.claim_sources cs
              where cs.claim_id = c.id
                and cs.experience_id = any(%s::uuid[])
           )
        """,
        [target_id, source_id, experience_ids],
    )
    claims_subject_repointed = cursor.rowcount or 0

    cursor.execute(
        """
        update brain.claims c
           set object_entity_id = %s::uuid
         where c.object_entity_id = %s::uuid
           and exists (
             select 1 from brain.claim_sources cs
              where cs.claim_id = c.id
                and cs.experience_id = any(%s::uuid[])
           )
        """,
        [target_id, source_id, experience_ids],
    )
    claims_object_repointed = cursor.rowcount or 0

    correction_ids: list[str] = []
    total = mentions_repointed + claims_subject_repointed + claims_object_repointed
    if total > 0:
        correction_ids.append(
            record_correction(
                cursor,
                target_kind="entity",
                target_id=source_id,
                before={"entity_id": source_id, "experience_ids": experience_ids},
                after={
                    "entity_id": target_id,
                    "mentions_repointed": mentions_repointed,
                    "claims_subject_repointed": claims_subject_repointed,
                    "claims_object_repointed": claims_object_repointed,
                },
                reason=reason,
                created_by=created_by,
            )
        )

    return {
        "mentions_repointed": mentions_repointed,
        "claims_subject_repointed": claims_subject_repointed,
        "claims_object_repointed": claims_object_repointed,
        "correction_event_ids": correction_ids,
    }


def split_entity(
    source_entity_id: str,
    experience_ids: list[str],
    into: dict,
    *,
    reason: str | None = None,
    created_by: str = "mcp:split_entity",
) -> dict:
    """Split an over-collapsed entity by repointing a subset of its references."""
    if not isinstance(experience_ids, list) or len(experience_ids) == 0:
        raise ValueError("split_entity: experience_ids must be a non-empty array")

    with transaction.atomic(), brain_cursor() as cursor:
        cursor.execute(
            "select id::text, kind::text as kind, merged_into::text "
            "from brain.entities where id = %s::uuid for update",
            [source_entity_id],
        )
        src_rows = dictfetchall(cursor)
        if not src_rows:
            raise ValueError(
                f"split_entity: source_entity_id {source_entity_id} not found"
            )
        source = src_rows[0]
        if source["merged_into"]:
            # A merged-away entity has no live references — reads chase the
            # pointer to its winner — so splitting one would move rows the winner
            # is assumed to own. Unmerge it first, then split the winner.
            raise ValueError(
                f"split_entity: source_entity_id {source_entity_id} is merged into "
                f"{source['merged_into']}; unmerge it first"
            )

        normalized = normalize_split_into(into, source["kind"])

        target_created = False
        if normalized["mode"] == "existing":
            cursor.execute(
                "select id::text, merged_into::text "
                "from brain.entities where id = %s::uuid for update",
                [normalized["entity_id"]],
            )
            tgt_rows = dictfetchall(cursor)
            if not tgt_rows:
                raise ValueError(
                    f"split_entity: into.entity_id {normalized['entity_id']} not found"
                )
            if tgt_rows[0]["merged_into"]:
                raise ValueError(
                    f"split_entity: into.entity_id {normalized['entity_id']} is itself "
                    f"merged into {tgt_rows[0]['merged_into']}"
                )
            target_id = normalized["entity_id"]
        else:
            cursor.execute(
                """
                insert into brain.entities (kind, canonical_name, aliases, metadata)
                     values (%s::brain.entity_kind, %s, %s::text[], %s::jsonb)
                  returning id::text
                """,
                [
                    normalized["kind"],
                    normalized["canonical_name"],
                    normalized["aliases"],
                    json.dumps(normalized["metadata"]),
                ],
            )
            target_id = dictfetchall(cursor)[0]["id"]
            target_created = True

        if target_id == source_entity_id:
            raise ValueError("split_entity: target and source must differ")

        repoint = _repoint_entity_references(
            cursor,
            source_id=source_entity_id,
            target_id=target_id,
            experience_ids=experience_ids,
            reason=reason,
            created_by=created_by,
        )

    return {
        "source_entity_id": source_entity_id,
        "target_entity_id": target_id,
        "target_created": target_created,
        "mentions_repointed": repoint["mentions_repointed"],
        "claims_repointed": (
            repoint["claims_subject_repointed"] + repoint["claims_object_repointed"]
        ),
        "correction_event_ids": repoint["correction_event_ids"],
    }


def unmerge_entity(
    entity_id: str,
    *,
    reason: str | None = None,
    created_by: str = "mcp:unmerge_entity",
) -> dict:
    """Undo a soft merge: clear merged_into so the entity stands on its own again.

    Aliases that merge appended to the winner are intentionally left in place —
    they are indistinguishable from independently-added aliases, and dropping
    them risks clobbering legitimate search surface.
    """
    with transaction.atomic(), brain_cursor() as cursor:
        cursor.execute(
            "select id::text, merged_into::text "
            "from brain.entities where id = %s::uuid for update",
            [entity_id],
        )
        rows = dictfetchall(cursor)
        if not rows:
            raise ValueError(f"unmerge_entity: entity {entity_id} not found")
        e = rows[0]
        if not e["merged_into"]:
            raise ValueError(f"unmerge_entity: entity {entity_id} is not merged")

        cursor.execute(
            "update brain.entities set merged_into = null where id = %s::uuid",
            [e["id"]],
        )
        # Reopen the merge_candidates row merge_entities resolved so the pair
        # resurfaces in review_queue rather than carrying a stale 'merged' verdict.
        cursor.execute(
            """
            update brain.merge_candidates
               set status = 'pending', resolved_at = null
             where status = 'merged'
               and entity_a = least(%s::uuid, %s::uuid)
               and entity_b = greatest(%s::uuid, %s::uuid)
            """,
            [e["id"], e["merged_into"], e["id"], e["merged_into"]],
        )
        correction_id = record_correction(
            cursor,
            target_kind="entity",
            target_id=e["id"],
            before={"merged_into": e["merged_into"]},
            after={"merged_into": None},
            reason=reason,
            created_by=created_by,
        )

    return {
        "entity_id": e["id"],
        "prior_merged_into": e["merged_into"],
        "correction_event_id": correction_id,
    }


def resolve_entity(
    name: str,
    *,
    context_text: str | None = None,
    kind: str = "person",
    top_k: int = 5,
) -> dict:
    """Fuzzy + phonetic + semantic candidate lookup for a name.

    Embeds context_text (or the name itself) and ranks candidates via the
    brain.resolve_entity SQL function. Read-only.
    """
    text = (context_text or "").strip() or name
    vec = to_vector_literal(embed_query(text))

    with brain_cursor() as cursor:
        cursor.execute(
            """
            select r.entity_id::text   as entity_id,
                   e.canonical_name    as canonical_name,
                   e.kind::text        as kind,
                   r.trgm_score        as trgm_score,
                   r.phon_match        as phon_match,
                   r.vec_score         as vec_score,
                   r.fused_score       as fused_score
              from brain.resolve_entity(%s, %s::vector, %s::brain.entity_kind, %s) r
              join brain.entities e on e.id = r.entity_id
             order by r.fused_score desc, r.trgm_score desc
            """,
            [name, vec, kind, top_k],
        )
        candidates = dictfetchall(cursor)

    for row in candidates:
        row["trgm_score"] = float(row["trgm_score"])
        row["phon_match"] = bool(row["phon_match"])
        row["vec_score"] = float(row["vec_score"])
        row["fused_score"] = float(row["fused_score"])

    return {"query_name": name, "query_kind": kind, "candidates": candidates}
