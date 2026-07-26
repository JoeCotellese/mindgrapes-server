# ABOUTME: Unit tests for claim parsing/validation (no network).
# ABOUTME: Pins the validation contract: coercion, enums, the predicate=='other' rule.

import pytest

from openbrain.brain.extraction.claims import (
    CLAIM_SYSTEM_PROMPT,
    ClaimValidationError,
    parse_claims,
)


def _claim(**overrides):
    base = {
        "subject": "Grace",
        "subject_kind": "person",
        "predicate": "works_at",
        "predicate_detail": "",
        "object": "Initech",
        "object_kind": "org",
        "support_kind": "verbatim",
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


def test_parse_claims_happy_path():
    claims = parse_claims({"claims": [_claim()]})
    assert len(claims) == 1
    assert claims[0]["subject"] == "Grace"
    assert claims[0]["predicate"] == "works_at"


def test_parse_claims_coerces_empty_predicate_detail_to_none():
    claims = parse_claims({"claims": [_claim(predicate_detail="")]})
    assert claims[0]["predicate_detail"] is None


def test_parse_claims_other_requires_detail():
    with pytest.raises(ClaimValidationError, match="predicate='other'"):
        parse_claims({"claims": [_claim(predicate="other", predicate_detail="")]})


def test_parse_claims_other_with_detail_ok():
    claims = parse_claims(
        {"claims": [_claim(predicate="other", predicate_detail="is_godparent_to")]}
    )
    assert claims[0]["predicate"] == "other"
    assert claims[0]["predicate_detail"] == "is_godparent_to"


def test_parse_claims_rejects_unknown_predicate():
    with pytest.raises(ClaimValidationError):
        parse_claims({"claims": [_claim(predicate="nonsense")]})


def test_parse_claims_rejects_unknown_kind():
    with pytest.raises(ClaimValidationError):
        parse_claims({"claims": [_claim(subject_kind="alien")]})


def test_parse_claims_accepts_animal_kind():
    # #57: pets/animals are a first-class entity kind, not forced into 'concept'.
    claims = parse_claims(
        {"claims": [_claim(subject="Roger", subject_kind="animal")]}
    )
    assert claims[0]["subject_kind"] == "animal"


def test_parse_claims_accepts_owns_predicate():
    # #58: ownership/pet relations get a canonical predicate instead of 'other'.
    claims = parse_claims(
        {
            "claims": [
                _claim(
                    subject="Joe",
                    predicate="owns",
                    object="Roger",
                    object_kind="animal",
                )
            ]
        }
    )
    assert claims[0]["predicate"] == "owns"
    assert claims[0]["predicate_detail"] is None


def test_parse_claims_rejects_out_of_range_confidence():
    with pytest.raises(ClaimValidationError):
        parse_claims({"claims": [_claim(confidence=1.5)]})


def test_parse_claims_rejects_empty_subject():
    with pytest.raises(ClaimValidationError):
        parse_claims({"claims": [_claim(subject="")]})


def test_parse_claims_empty_array_is_valid():
    assert parse_claims({"claims": []}) == []


def test_parse_claims_requires_claims_array():
    with pytest.raises(ClaimValidationError):
        parse_claims({"not_claims": []})


# #11: the "Predicates explicitly excluded" section of docs/predicates.md never
# reached the prompt, so banned relations routed through 'other' instead of
# being dropped. These pin each family into the shipped prompt text. They cannot
# prove the model obeys — that needs the live eval reported on the PR.
@pytest.mark.parametrize(
    "banned",
    ["is_a", "instance_of", "mentioned_in", "references", "_count", "will_"],
)
def test_prompt_names_every_excluded_predicate_family(banned):
    assert banned in CLAIM_SYSTEM_PROMPT


def test_prompt_tells_the_model_to_drop_excluded_claims():
    # The escape hatch is unconditional ("if no canonical predicate fits, use
    # 'other'"), so the exclusions are only load-bearing if they say "drop".
    assert "Drop the claim" in CLAIM_SYSTEM_PROMPT


def test_prompt_steers_ownership_onto_owns():
    assert '"owns"' in CLAIM_SYSTEM_PROMPT
