# ABOUTME: Unit tests for the pure helpers in services/mcp_reads.py (no DB needed).
# ABOUTME: Covers id-set intersection, provenance grouping, and stats aggregation.
from datetime import UTC, datetime
from decimal import Decimal

from openbrain.brain.services.mcp_reads import (
    aggregate_stats,
    group_provenance,
    intersect_id_sets,
)


def test_intersect_single_set_passthrough():
    assert intersect_id_sets([["a", "b", "c"]]) == ["a", "b", "c"]


def test_intersect_preserves_first_set_order():
    result = intersect_id_sets([["a", "b", "c", "d"], ["d", "b", "z"]])
    assert result == ["b", "d"]


def test_intersect_empty_when_disjoint():
    assert intersect_id_sets([["a"], ["b"]]) == []


def test_intersect_no_sets_is_empty():
    assert intersect_id_sets([]) == []


def _prov_row(experience_id, claim_id, **overrides):
    base = {
        "experience_id": experience_id,
        "claim_id": claim_id,
        "predicate": "works_at",
        "predicate_detail": None,
        "object_literal": None,
        "polarity": "asserted",
        "confidence": Decimal("0.90"),
        "support_kind": "inferred",
        "source_confidence": Decimal("0.50"),
        "extracted_by": "consolidation",
        "superseded_by": None,
    }
    base.update(overrides)
    return base


def test_group_provenance_buckets_by_experience_and_drops_key():
    rows = [
        _prov_row("e1", "c1"),
        _prov_row("e1", "c2", predicate="founded"),
        _prov_row("e2", "c3"),
    ]
    grouped = group_provenance(rows)
    assert set(grouped) == {"e1", "e2"}
    assert len(grouped["e1"]) == 2
    assert "experience_id" not in grouped["e1"][0]


def test_group_provenance_coerces_numeric_to_float():
    grouped = group_provenance([_prov_row("e1", "c1")])
    claim = grouped["e1"][0]
    assert claim["confidence"] == 0.9
    assert isinstance(claim["confidence"], float)
    assert isinstance(claim["source_confidence"], float)


def test_group_provenance_allows_null_confidence():
    grouped = group_provenance(
        [_prov_row("e1", "c1", confidence=None, source_confidence=None)]
    )
    assert grouped["e1"][0]["confidence"] is None


def _row(meta, when):
    return {"metadata": meta, "created_at": when}


def test_aggregate_stats_empty():
    stats = aggregate_stats([])
    assert stats["total"] == 0
    assert stats["date_range"] is None
    assert stats["types"] == {}
    assert stats["top_topics"] == [] and stats["top_people"] == []


def test_aggregate_stats_counts_and_date_range():
    newest = datetime(2026, 6, 20, tzinfo=UTC)
    oldest = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [  # ordered captured_at desc, as the SQL returns them
        _row({"type": "observation", "topics": ["x", "y"], "people": ["Grace"]}, newest),
        _row(
            {"type": "observation", "topics": ["x"], "people": ["Grace", "Bea"]},
            oldest,
        ),
    ]
    stats = aggregate_stats(rows)
    assert stats["total"] == 2
    assert stats["types"] == {"observation": 2}
    assert stats["top_topics"][0] == {"name": "x", "count": 2}
    assert {"name": "Grace", "count": 2} in stats["top_people"]
    # date_range: first = oldest (last row), last = newest (first row).
    assert stats["date_range"]["first"] == oldest
    assert stats["date_range"]["last"] == newest


def test_aggregate_stats_top_is_capped_at_ten():
    meta = {"type": "note", "topics": [f"t{i}" for i in range(15)]}
    stats = aggregate_stats([_row(meta, datetime(2026, 6, 20, tzinfo=UTC))])
    assert len(stats["top_topics"]) == 10
