# ABOUTME: Unit tests for the claim writer's pure decisions (no DB).
# ABOUTME: Pins the object literal-vs-entity policy and the accumulator.

import pytest

from openbrain.brain.services.claim_writer import (
    MATCH_THRESHOLD,
    _object_should_be_literal,
    build_binding_index,
    is_owner_self_reference,
    lookup_binding,
    new_accumulator,
)
from openbrain.brain.services.name_matching import REUSE_THRESHOLD


def _binding(entity_id, canonical_name, surface_form=None, kind="person", aliases=None):
    """One row as _EXPERIENCE_BINDINGS_SQL returns it."""
    return {
        "surface_form": surface_form or canonical_name,
        "entity_id": entity_id,
        "kind": kind,
        "canonical_name": canonical_name,
        "aliases": aliases if aliases is not None else [canonical_name],
    }


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


# #73: the claim pass binds to what the capture already resolved.
def test_binding_matches_the_canonical_name():
    index = build_binding_index([_binding("e1", "Bonnie Ravina")])
    assert lookup_binding(index, "Bonnie Ravina", "person") == "e1"
    # Normalization folds case and punctuation, as name_matching does elsewhere.
    assert lookup_binding(index, "bonnie  ravina", "person") == "e1"


def test_binding_matches_an_alias_and_the_mention_surface():
    index = build_binding_index(
        [
            _binding(
                "e1",
                "Bonnie Ravina",
                surface_form="the Bonnie from Full Circle",
                aliases=["Bonnie Ravina", "B. Ravina"],
            )
        ]
    )
    assert lookup_binding(index, "B. Ravina", "person") == "e1"
    assert lookup_binding(index, "the Bonnie from Full Circle", "person") == "e1"


def test_bare_given_name_binds_to_its_only_expansion():
    # The extractor routinely shortens a full name it saw in the content.
    index = build_binding_index([_binding("e1", "Bonnie Ravina")])
    assert lookup_binding(index, "Bonnie", "person") == "e1"


def test_ambiguous_given_name_does_not_bind():
    # Two bound Bonnies: no basis to pick, so fall through to resolution.
    index = build_binding_index(
        [_binding("e1", "Bonnie Ravina"), _binding("e2", "Bonnie Chen")]
    )
    assert lookup_binding(index, "Bonnie", "person") is None


def test_binding_is_kind_scoped():
    index = build_binding_index([_binding("e1", "Naftiko", kind="org")])
    assert lookup_binding(index, "Naftiko", "org") == "e1"
    assert lookup_binding(index, "Naftiko", "person") is None


def test_name_claimed_by_two_entities_is_dropped():
    # Same alias on two bound entities of one kind — ambiguous, not a coin flip.
    index = build_binding_index(
        [
            _binding("e1", "Rick Nucci", aliases=["Rick Nucci", "Rick"]),
            _binding("e2", "Rick Ellis", aliases=["Rick Ellis", "Rick"]),
        ]
    )
    assert lookup_binding(index, "Rick", "person") is None
    assert lookup_binding(index, "Rick Nucci", "person") == "e1"


def test_lookup_with_no_bindings_or_blank_name():
    index = build_binding_index([_binding("e1", "Bonnie Ravina")])
    assert lookup_binding({}, "Bonnie Ravina", "person") is None
    assert lookup_binding(index, "   ", "person") is None


def test_threshold_is_the_shared_reuse_constant():
    # #73: the claim path used to carry its own copy of 0.85, free to drift from
    # the capture path during retuning. One constant, one place to retune.
    assert MATCH_THRESHOLD is REUSE_THRESHOLD


def test_new_accumulator_shape():
    acc = new_accumulator()
    assert acc == {
        "claims_inserted": 0,
        "claim_sources_inserted": 0,
        "entities_created_for_objects": 0,
        "literal_objects_fell_back": 0,
        "claims_skipped_self": 0,
    }


# #56: first-person subjects/objects must NOT mint a junk entity named "I".
@pytest.mark.parametrize(
    "name",
    [
        "I",
        "i",
        "me",
        "My",
        "mine",
        "myself",
        "the user",
        "  Me  ",
        "I.",
        '"me"',
        "I,",
        "we",
        "We",
        "us",
        "our",
        "ours",
        "ourselves",
    ],
)
def test_is_owner_self_reference_true(name):
    assert is_owner_self_reference(name) is True


@pytest.mark.parametrize(
    # No false positives: real names, and multi-word phrases that merely start
    # with a pronoun ("my team" is a real thing to remember, not the owner).
    "name",
    [
        "Ian",
        "Mimi",
        "my team",
        "my dog Roger",
        "our team",
        "Ines",
        "user",
        "Miles",
        "",
        "  ",
    ],
)
def test_is_owner_self_reference_false(name):
    assert is_owner_self_reference(name) is False
