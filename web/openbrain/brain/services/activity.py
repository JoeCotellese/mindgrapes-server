# ABOUTME: Activity-log read service — the brain's change-event audit trail.
# ABOUTME: Maps brain.correction_events to typed, linked, actor-tagged rows for /activity.
"""Read service backing /activity (issue #136).

One query over brain.correction_events, newest-first, joined out to the change
targets (experience / entity / claim) so each row links to a named record rather
than a bare UUID, and a merge resolves its surviving entity. Change type is derived
from the event shape — target_kind plus the keys present in before/after — and the
actor from created_by (human via UI/MCP vs. the autonomous consolidation worker).
Read-only.

Access is gated to the operator (superuser) at the view: correction_events has no
owner column, and entity/claim events have no per-row owner, so there is no viewer
filter here (revisit under multi-user, #52). The point is trust — explaining every
autonomous change without opening Postgres.
"""

import json

from openbrain.brain.db import brain_cursor, dictfetchall, parse_json

# Target snippets are short — this is a dense log, not the reading view.
SNIPPET_CHARS = 120

# Newest-first change events, each LEFT JOINed to its target so the link carries a
# name. `win` resolves a merge's surviving entity from after->>'merged_into' (the
# join is gated to entity targets; only merges set that key to a uuid). before/after
# are jsonb → text in this stack, parsed in Python; the merged_into extraction is
# done in SQL so it works regardless of that.
_ACTIVITY_SQL = """
    select ce.id::text          as id,
           ce.target_kind::text as target_kind,
           ce.target_id::text   as target_id,
           ce.before            as before,
           ce.after             as after,
           ce.reason            as reason,
           ce.created_at        as created_at,
           ce.created_by        as created_by,
           ex.content           as exp_content,
           ent.canonical_name   as entity_name,
           win.id::text         as winner_id,
           win.canonical_name   as winner_name,
           cl.predicate         as claim_predicate,
           sub.canonical_name   as claim_subject
      from brain.correction_events ce
      left join brain.experiences ex
             on ce.target_kind = 'experience' and ex.id = ce.target_id
      left join brain.entities ent
             on ce.target_kind = 'entity' and ent.id = ce.target_id
      left join brain.entities win
             on ce.target_kind = 'entity'
            and win.id = nullif(ce.after->>'merged_into', '')::uuid
      left join brain.claims cl
             on ce.target_kind = 'claim' and cl.id = ce.target_id
      left join brain.entities sub
             on sub.id = cl.subject_id
     order by ce.created_at desc
     limit %(limit)s offset %(offset)s
"""

# slug -> (chip label, Bulma chip class). The label is the real signal; the color
# is decorative — change type is never conveyed by color alone (a11y Tier 1).
_CHANGE_TYPES = {
    "edit": ("Edited", "is-info is-light"),
    "visibility": ("Visibility", "is-info is-light"),
    "supersede": ("Superseded", "is-warning is-light"),
    "delete": ("Deleted", "is-danger is-light"),
    "retract": ("Retracted", "is-danger is-light"),
    "merge": ("Merged", "is-link is-light"),
    "unmerge": ("Unmerged", "is-link is-light"),
    "rename": ("Renamed", "is-primary is-light"),
    "split": ("Split", "is-primary is-light"),
}


def _classify_actor(created_by: str | None) -> dict:
    """Who made the change, from created_by. Worker writes get the auto marker."""
    cb = (created_by or "").strip()
    if cb.startswith("consolidation"):
        return {
            "kind": "consolidation",
            "label": "Consolidation worker",
            "is_auto": True,
        }
    if cb.startswith("ui-session:"):
        return {"kind": "human", "label": "You (web)", "is_auto": False}
    if cb.startswith("mcp:"):
        return {"kind": "human", "label": "You (via AI)", "is_auto": False}
    # Legacy / null / system writes: surface verbatim, never claimed as automated.
    return {"kind": "system", "label": cb or "unknown", "is_auto": False}


def _change_type(target_kind: str, before, after) -> str:
    """The change slug, derived from the event's target_kind + before/after keys.

    The keys each write leaves are stable (see every record_correction call site);
    the free-text reason is not, so it is deliberately not used here.
    """
    keys = set(before or {}) | set(after or {})
    if target_kind == "claim":
        # Every claim correction is a polarity -> retracted flip.
        return "retract"
    if target_kind == "entity":
        if "merged_into" in keys:
            return "unmerge" if (after or {}).get("merged_into") is None else "merge"
        if "experience_ids" in keys:
            return "split"
        if "canonical_name" in keys:
            return "rename"
        return "edit"
    # experience
    if "deleted_at" in keys:
        return "delete"
    if "superseded_by" in keys:
        return "supersede"
    if "visibility" in keys and "content" not in keys and "metadata" not in keys:
        return "visibility"
    return "edit"


def _snippet(content: str | None) -> str:
    text = (content or "").strip()
    if len(text) <= SNIPPET_CHARS:
        return text
    return text[:SNIPPET_CHARS].rstrip() + "…"


def _target_and_secondary(row: dict) -> tuple[dict, dict | None]:
    """The linked target and, for a merge, the surviving entity as a secondary link.

    Claims have no detail route, so they carry a descriptive label and no href.
    A target whose row was since hard-deleted falls back to a generic label.
    """
    kind = row["target_kind"]
    tid = row["target_id"]
    if kind == "experience":
        label = _snippet(row.get("exp_content")) or "an experience"
        return {
            "kind": kind,
            "id": tid,
            "href": f"/experience/{tid}",
            "label": label,
        }, None
    if kind == "entity":
        target = {
            "kind": kind,
            "id": tid,
            "href": f"/entity/{tid}",
            "label": row.get("entity_name") or "an entity",
        }
        secondary = None
        if row.get("winner_id"):
            secondary = {
                "href": f"/entity/{row['winner_id']}",
                "label": row.get("winner_name") or "an entity",
            }
        return target, secondary
    # claim (no detail page)
    predicate = row.get("claim_predicate")
    subject = row.get("claim_subject")
    if subject and predicate:
        label = f"{subject}: {predicate}"
    elif predicate:
        label = f"claim: {predicate}"
    else:
        label = "a claim"
    return {"kind": kind, "id": tid, "href": None, "label": label}, None


def _format_row(row: dict) -> dict:
    before = parse_json(row["before"]) or {}
    after = parse_json(row["after"]) or {}
    change_type = _change_type(row["target_kind"], before, after)
    label, chip_class = _CHANGE_TYPES.get(change_type, _CHANGE_TYPES["edit"])
    target, secondary = _target_and_secondary(row)
    return {
        "id": row["id"],
        "change_type": change_type,
        "change_label": label,
        "chip_class": chip_class,
        "target": target,
        "secondary": secondary,
        "actor": _classify_actor(row["created_by"]),
        "reason": row.get("reason"),
        "created_at": row["created_at"],
        "before": before,
        "after": after,
        "before_pretty": json.dumps(before, indent=2, sort_keys=True) if before else "",
        "after_pretty": json.dumps(after, indent=2, sort_keys=True) if after else "",
        "has_diff": bool(before) or bool(after),
    }


def get_activity(limit: int, offset: int) -> dict:
    """One page of change events, newest-first (#136).

    Asks for limit + 1 rows so a further page can be detected without a separate
    count query; the probe row is trimmed off and reported via has_more.
    """
    params = {"limit": limit + 1, "offset": offset}
    with brain_cursor() as cursor:
        cursor.execute(_ACTIVITY_SQL, params)
        rows = dictfetchall(cursor)
    has_more = len(rows) > limit
    events = [_format_row(row) for row in rows[:limit]]
    return {
        "events": events,
        "next_offset": offset + len(events),
        "has_more": has_more,
    }
