# ABOUTME: Unit tests for the bare-vs-structured capture trigger predicate (no Postgres).
# ABOUTME: Pins that visibility is orthogonal and never triggers the structured path.

from openbrain.brain.services.captures import is_structured_capture

_HINT = {
    "subject": "a",
    "predicate": "knows",
    "object": "b",
    "support_kind": "verbatim",
}


def test_bare_when_all_fields_absent():
    assert is_structured_capture(None, None, None, None, None) is False


def test_structured_when_occurred_at_present():
    assert is_structured_capture("2025-05-01T00:00:00Z", None, None, None, None) is True


def test_structured_when_participants_non_empty():
    assert is_structured_capture(None, [{"name": "Grace"}], None, None, None) is True


def test_bare_when_participants_empty_list():
    assert is_structured_capture(None, [], None, None, None) is False


def test_structured_when_predicate_hints_non_empty():
    assert is_structured_capture(None, None, [_HINT], None, None) is True


def test_bare_when_predicate_hints_empty_list():
    assert is_structured_capture(None, None, [], None, None) is False


def test_structured_when_source_kind_present():
    assert is_structured_capture(None, None, None, "manual", None) is True


def test_structured_when_source_ref_present():
    assert is_structured_capture(None, None, None, None, "gdrive:abc") is True
