"""Unit tests for the pure helpers in the entities service (no Postgres).

normalize_split_into decides whether split_entity mints a fresh entity or
repoints onto an existing one. The DB-effecting paths (merge/rename/retract/
split/unmerge/resolve) are covered in the integration suite; here we pin the
branch logic normalize_split_into encodes.
"""

import pytest

from openbrain.brain.services.entities import normalize_split_into


def test_existing_mode_trims_entity_id():
    out = normalize_split_into({"entity_id": "  abc  "}, "person")
    assert out == {"mode": "existing", "entity_id": "abc"}


def test_create_mode_defaults_kind_to_source():
    out = normalize_split_into({"canonical_name": "Karen B"}, "person")
    assert out == {
        "mode": "create",
        "canonical_name": "Karen B",
        "kind": "person",
        "aliases": [],
        "metadata": {},
    }


def test_create_mode_keeps_explicit_kind_aliases_metadata():
    out = normalize_split_into(
        {
            "canonical_name": "  Acme  ",
            "kind": "org",
            "aliases": ["Acme Inc"],
            "metadata": {"note": "x"},
        },
        "person",
    )
    assert out == {
        "mode": "create",
        "canonical_name": "Acme",
        "kind": "org",
        "aliases": ["Acme Inc"],
        "metadata": {"note": "x"},
    }


def test_blank_canonical_name_counts_as_absent():
    # A blank canonical_name must not silently mint a nameless entity, and with
    # no entity_id either there is nothing to do -> error.
    with pytest.raises(ValueError):
        normalize_split_into({"canonical_name": "   "}, "person")


def test_both_present_is_ambiguous():
    with pytest.raises(ValueError):
        normalize_split_into({"canonical_name": "X", "entity_id": "abc"}, "person")


def test_neither_present_is_error():
    with pytest.raises(ValueError):
        normalize_split_into({}, "person")
