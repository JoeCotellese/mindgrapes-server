# ABOUTME: Unit tests for claim parsing/validation (no network).
# ABOUTME: Pins the validation contract: coercion, enums, the predicate=='other' rule.

import pytest

from openbrain.brain.extraction.claims import ClaimValidationError, parse_claims


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
