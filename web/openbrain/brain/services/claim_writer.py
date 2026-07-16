# ABOUTME: Per-experience claim writer for the consolidation pipeline.
# ABOUTME: resolve-or-create subject/object entities, then insert claims + claim_sources.

from openbrain.brain.db import dictfetchall

# trgm_score (0..1) is the channel for entity binding. This is a DISTINCT policy
# from entity_resolver.py: an inclusive >= threshold, and NO alias-append / NO
# merge-candidate / NO mention side effects. It mirrors the historical claims
# backfill exactly so backfilled and consolidation-written rows are bit-identical in shape.
MATCH_THRESHOLD = 0.85

_RESOLVE_ENTITY_SQL = """
    select entity_id::text, trgm_score, phon_match, vec_score, fused_score
      from brain.resolve_entity(%s, %s::vector, %s::brain.entity_kind, 1)
"""

_INSERT_ENTITY_SQL = """
    insert into brain.entities (kind, canonical_name, aliases, embedding)
         values (%s::brain.entity_kind, %s, array[%s]::text[], %s::vector)
      returning id::text as id
"""

_INSERT_CLAIM_SQL = """
    insert into brain.claims (
        subject_id, predicate, predicate_detail,
        object_entity_id, object_literal, confidence
    ) values (
        %s::uuid, %s, %s,
        %s::uuid, %s, %s
    ) returning id::text as id
"""

_INSERT_CLAIM_SOURCE_SQL = """
    insert into brain.claim_sources (
        claim_id, experience_id, support_kind, source_confidence, extracted_by
    ) values (
        %s::uuid, %s::uuid, %s::brain.support_kind, %s, %s
    )
"""


def new_accumulator() -> dict:
    """Per-batch write counters, returned to the worker for its log line."""
    return {
        "claims_inserted": 0,
        "claim_sources_inserted": 0,
        "entities_created_for_objects": 0,
        "literal_objects_fell_back": 0,
    }


def _object_should_be_literal(claim: dict, top: dict | None) -> bool:
    """'concept'-typed objects are usually free-form quotes/rationales — wrong to
    spawn an entity for. Try resolution first; fall back to a literal when there's
    no strong name match. Non-concept objects always bind to an entity.
    """
    if claim["object_kind"] != "concept":
        return False
    if not top or top["entity_id"] is None:
        return True
    return top["trgm_score"] < MATCH_THRESHOLD


def _resolve_top(cursor, name: str, kind: str, embedding: str | None) -> dict | None:
    cursor.execute(_RESOLVE_ENTITY_SQL, [name, embedding, kind])
    rows = dictfetchall(cursor)
    return rows[0] if rows else None


def _insert_entity(cursor, name: str, kind: str, embedding: str | None) -> str:
    cursor.execute(_INSERT_ENTITY_SQL, [kind, name, name, embedding])
    return dictfetchall(cursor)[0]["id"]


def _resolve_or_create_entity(
    cursor, name: str, kind: str, embedding: str | None, acc: dict
) -> str:
    top = _resolve_top(cursor, name, kind, embedding)
    if top and top["entity_id"] is not None and top["trgm_score"] >= MATCH_THRESHOLD:
        return top["entity_id"]
    acc["entities_created_for_objects"] += 1
    return _insert_entity(cursor, name, kind, embedding)


def write_claim_for_experience(
    cursor,
    experience_id: str,
    embedding: str | None,
    claim: dict,
    extracted_by: str,
    acc: dict,
) -> None:
    """Resolve subject/object entities and insert one claim + its claim_source.

    ``claim`` is the snake_case dict that extraction/claims.py:parse_claims emits.
    ``embedding`` is the experience embedding as a pgvector text literal (used as
    resolver context). The caller owns the surrounding transaction.
    """
    subject_id = _resolve_or_create_entity(
        cursor, claim["subject"], claim["subject_kind"], embedding, acc
    )

    top = _resolve_top(cursor, claim["object"], claim["object_kind"], embedding)
    object_entity_id: str | None = None
    object_literal: str | None = None
    if _object_should_be_literal(claim, top):
        object_literal = claim["object"]
        acc["literal_objects_fell_back"] += 1
    elif top and top["entity_id"] is not None and top["trgm_score"] >= MATCH_THRESHOLD:
        object_entity_id = top["entity_id"]
    else:
        object_entity_id = _insert_entity(
            cursor, claim["object"], claim["object_kind"], embedding
        )
        acc["entities_created_for_objects"] += 1

    cursor.execute(
        _INSERT_CLAIM_SQL,
        [
            subject_id,
            claim["predicate"],
            claim["predicate_detail"],
            object_entity_id,
            object_literal,
            claim["confidence"],
        ],
    )
    claim_id = dictfetchall(cursor)[0]["id"]
    acc["claims_inserted"] += 1

    cursor.execute(
        _INSERT_CLAIM_SOURCE_SQL,
        [
            claim_id,
            experience_id,
            claim["support_kind"],
            claim["confidence"],
            extracted_by,
        ],
    )
    acc["claim_sources_inserted"] += 1


def write_claims_for_experience(
    cursor,
    experience_id: str,
    embedding: str | None,
    claims: list[dict],
    extracted_by: str,
    acc: dict | None = None,
) -> dict:
    """Write every claim for one experience; return the accumulator."""
    a = acc if acc is not None else new_accumulator()
    for claim in claims:
        write_claim_for_experience(
            cursor, experience_id, embedding, claim, extracted_by, a
        )
    return a
