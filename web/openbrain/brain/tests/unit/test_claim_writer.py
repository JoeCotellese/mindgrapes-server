# ABOUTME: Unit tests for the claim writer's pure decisions (no DB).
# ABOUTME: Pins the object literal-vs-entity policy and the accumulator.

from openbrain.brain.services.claim_writer import (
    MATCH_THRESHOLD,
    _object_should_be_literal,
    new_accumulator,
)


def _claim(object_kind="org"):
    return {
        "subject": "B",
        "subject_kind": "person",
        "predicate": "works_at",
        "predicate_detail": None,
        "object": "Initech",
        "object_kind": object_kind,
        "support_kind": "verbatim",
        "confidence": 0.9,
    }


def _top(entity_id="e1", trgm_score=0.9):
    return {
        "entity_id": entity_id,
        "trgm_score": trgm_score,
        "phon_match": False,
        "vec_score": 0.5,
        "fused_score": 0.5,
    }


def test_non_concept_object_is_never_literal():
    # Even with no resolver match, a typed (org/person/...) object binds to an entity.
    assert _object_should_be_literal(_claim("org"), None) is False
    assert _object_should_be_literal(_claim("person"), _top(trgm_score=0.1)) is False


def test_concept_object_with_no_match_is_literal():
    assert _object_should_be_literal(_claim("concept"), None) is True
    assert _object_should_be_literal(_claim("concept"), _top(entity_id=None)) is True


def test_concept_object_with_weak_match_is_literal():
    assert _object_should_be_literal(_claim("concept"), _top(trgm_score=0.5)) is True


def test_concept_object_with_strong_match_binds_to_entity():
    assert _object_should_be_literal(_claim("concept"), _top(trgm_score=0.9)) is False
    # The boundary is inclusive: trgm == threshold binds (mirrors the >= in the writer).
    assert (
        _object_should_be_literal(_claim("concept"), _top(trgm_score=MATCH_THRESHOLD))
        is False
    )


def test_new_accumulator_shape():
    acc = new_accumulator()
    assert acc == {
        "claims_inserted": 0,
        "claim_sources_inserted": 0,
        "entities_created_for_objects": 0,
        "literal_objects_fell_back": 0,
    }
