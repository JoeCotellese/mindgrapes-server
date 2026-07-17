# ABOUTME: Entity resolve/link policy.
# ABOUTME: resolve_or_create_entity applies the trgm thresholds; link_mention writes brain.mentions.

import json

from openbrain.brain.db import dictfetchall
from openbrain.brain.services.entities import merge_entities_on_cursor
from openbrain.brain.services.name_matching import (
    AUTO_MERGE_THRESHOLD,
)
from openbrain.brain.services.name_matching import (
    match_score as verify_match_score,
)

# Policy applied to brain.resolve_entity's trgm_score (name-similarity, 0-1):
#   trgm > 0.85          → existing entity, append surface_form to aliases
#   0.55 < trgm <= 0.85  → new entity, queue (existing, new) in merge_candidates
#   trgm <= 0.55         → new entity, no candidate row
# vec_score / phon_match are not thresholds: both call sites pass the experience
# embedding as context, so vec_score is high regardless of name similarity. They
# inform fused_score's tiebreaking only.
MATCH_THRESHOLD = 0.85
BORDERLINE_THRESHOLD = 0.55

_RESOLVE_ENTITY_SQL = """
    select entity_id::text, trgm_score, phon_match, vec_score, fused_score
      from brain.resolve_entity(%s, %s::vector, %s::brain.entity_kind, 1)
"""

_APPEND_ALIAS_SQL = """
    update brain.entities
       set aliases = case
         when %s = any(aliases) then aliases
         else array_append(aliases, %s)
       end
     where id = %s::uuid
"""

_INSERT_ENTITY_SQL = """
    insert into brain.entities (kind, canonical_name, aliases, embedding)
         values (%s::brain.entity_kind, %s, array[%s]::text[], %s::vector)
      returning id::text as id
"""

_INSERT_MENTION_SQL = """
    insert into brain.mentions (experience_id, entity_id, surface_form, field)
         values (%s::uuid, %s::uuid, %s, %s)
    on conflict do nothing
    returning experience_id
"""

_ENTITY_NAME_SQL = """
    select canonical_name, aliases
      from brain.entities where id = %s::uuid
"""

_INSERT_MERGE_CANDIDATE_SQL = """
    insert into brain.merge_candidates (entity_a, entity_b, similarity, evidence)
         values (
           least(%s::uuid, %s::uuid),
           greatest(%s::uuid, %s::uuid),
           %s,
           %s::jsonb
         )
    on conflict (entity_a, entity_b) do nothing
    returning id
"""


def resolve_or_create_entity(
    cursor,
    experience_id: str,
    embedding: str | None,
    *,
    surface: str,
    field: str,
    kind: str,
) -> dict:
    """Resolve surface to an existing entity or create a new one; return the outcome.

    The outcome dict carries surface/field/kind, the resolved entity_id, an
    action ('matched' | 'created' | 'borderline' | 'auto_merged'), the three
    resolver scores, and — for 'borderline' / 'auto_merged' — the second-stage
    verification_score. 'borderline' also carries borderline_entity_id (the
    existing entity the new one might merge into); 'auto_merged' carries
    merged_from_entity_id (the just-created entity that was soft-merged into the
    returned entity_id) and resolves to the surviving entity.
    """
    cursor.execute(_RESOLVE_ENTITY_SQL, [surface, embedding, kind])
    candidates = dictfetchall(cursor)
    top = candidates[0] if candidates else None
    match_score = top["trgm_score"] if top else 0

    if top and match_score > MATCH_THRESHOLD:
        cursor.execute(_APPEND_ALIAS_SQL, [surface, surface, top["entity_id"]])
        return _outcome(surface, field, kind, top["entity_id"], "matched", top)

    cursor.execute(_INSERT_ENTITY_SQL, [kind, surface, surface, embedding])
    new_id = dictfetchall(cursor)[0]["id"]

    if top and match_score > BORDERLINE_THRESHOLD:
        # Second-stage verification: before parking a human decision, verify the
        # new entity against the existing top candidate with the Jaro-Winkler /
        # containment seam. A confident match is soft-auto-merged (audited,
        # reversible via unmerge_entity); everything below queues exactly as
        # before. The candidate row is recorded either way so a reversed
        # auto-merge reopens into the queue rather than vanishing.
        cursor.execute(_ENTITY_NAME_SQL, [top["entity_id"]])
        existing = dictfetchall(cursor)[0]
        verification = verify_match_score(
            kind,
            existing["canonical_name"],
            existing["aliases"],
            kind,
            surface,
            [surface],
        )
        evidence = {
            "surface_form": surface,
            "experience_id": experience_id,
            "trgm_score": top["trgm_score"],
            "vec_score": top["vec_score"],
            "phon_match": top["phon_match"],
            "fused_score": top["fused_score"],
            "verification_score": verification,
        }
        cursor.execute(
            _INSERT_MERGE_CANDIDATE_SQL,
            [
                top["entity_id"],
                new_id,
                top["entity_id"],
                new_id,
                match_score,
                json.dumps(evidence),
            ],
        )

        if verification >= AUTO_MERGE_THRESHOLD:
            merge_entities_on_cursor(
                cursor,
                new_id,
                top["entity_id"],
                reason=f"second-stage auto-merge (verification={verification:.3f})",
                created_by="mcp:entity_resolver:auto_merge",
            )
            outcome = _outcome(
                surface, field, kind, top["entity_id"], "auto_merged", top
            )
            outcome["merged_from_entity_id"] = new_id
            outcome["verification_score"] = verification
            return outcome

        outcome = _outcome(surface, field, kind, new_id, "borderline", top)
        outcome["borderline_entity_id"] = top["entity_id"]
        outcome["verification_score"] = verification
        return outcome

    return _outcome(surface, field, kind, new_id, "created", top)


def _outcome(surface, field, kind, entity_id, action, top) -> dict:
    return {
        "surface": surface,
        "field": field,
        "kind": kind,
        "entity_id": entity_id,
        "action": action,
        "trgm_score": top["trgm_score"] if top else 0,
        "vec_score": top["vec_score"] if top else 0,
        "phon_match": top["phon_match"] if top else False,
    }


def link_mention(
    cursor, experience_id: str, entity_id: str, surface: str, field: str
) -> bool:
    """Link an experience to an entity in brain.mentions; True if newly inserted."""
    cursor.execute(_INSERT_MENTION_SQL, [experience_id, entity_id, surface, field])
    return len(dictfetchall(cursor)) > 0
