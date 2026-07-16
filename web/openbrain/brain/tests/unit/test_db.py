"""Unit tests for the brain.* data-access seam.

The Postgres path (real brain_cursor queries) is exercised by the integration
suite; here we cover the vendor guard and the row->dict mapping with no DB.
"""

from openbrain.brain.db import (
    brain_schema_present,
    dictfetchall,
    parse_json,
    to_vector_literal,
)


class FakeCursor:
    description = [("a",), ("b",)]

    def fetchall(self):
        return [(1, 2), (3, 4)]


def test_dictfetchall_maps_rows_to_dicts():
    assert dictfetchall(FakeCursor()) == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]


def test_brain_schema_present_is_false_on_sqlite():
    # Unit tests run on sqlite; the vendor guard must short-circuit to False
    # without issuing Postgres-only SQL (to_regclass).
    assert brain_schema_present() is False


def test_to_vector_literal_formats_pgvector_literal():
    # A bare '[v1,v2,...]' — the pgvector text-literal form for a ::vector cast.
    assert to_vector_literal([0.1, 0.2, 0.3]) == "[0.1,0.2,0.3]"


def test_to_vector_literal_handles_empty():
    assert to_vector_literal([]) == "[]"


def test_parse_json_decodes_jsonb_text():
    # jsonb columns arrive as text on this stack; parse_json restores objects.
    assert parse_json('[{"a": 1}]') == [{"a": 1}]
    assert parse_json('{"k": 2}') == {"k": 2}


def test_parse_json_passes_through_parsed_and_none():
    # json columns arrive already parsed; None stays None.
    assert parse_json([1, 2]) == [1, 2]
    assert parse_json(None) is None
