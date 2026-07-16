# ABOUTME: Entity resolve/link policy.
# ABOUTME: resolve_or_create_entity applies the trgm thresholds; link_mention writes brain.mentions.

import json

from openbrain.brain.db import dictfetchall

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
    action ('matched' | 'created' | 'borderline'), the three resolver scores,
    and — for 'borderline' only — borderline_entity_id (the existing entity the
    new one might merge into).
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
        evidence = {
            "surface_form": surface,
            "experience_id": experience_id,
            "trgm_score": top["trgm_score"],
            "vec_score": top["vec_score"],
            "phon_match": top["phon_match"],
            "fused_score": top["fused_score"],
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
        outcome = _outcome(surface, field, kind, new_id, "borderline", top)
        outcome["borderline_entity_id"] = top["entity_id"]
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
