"""Integration tests for the MCP read services against real brain.*.

Requires the dev stack up (make dev-up); run via make dev-test-integration. These
exercise the raw SQL — match_brain_hybrid's 7-arg form, experience_ids_mentioning_name,
the provenance join, the summary cache reshape, and the review-queue counts — so
function/enum/column drift surfaces here. The unit suite covers the pure transforms
(intersection, provenance grouping, stats aggregation); this is the drift contract.
"""

import uuid

import pytest
from django.db import connection
from django.test import override_settings

from openbrain.brain.services.mcp_reads import (
    SummaryCacheEmpty,
    hybrid_search,
    list_thoughts,
    pending_reviews,
    recent_entities,
    summary_for_resource,
    thought_stats,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_db")]

VIEWER = "1"
_VEC_LIT = "[" + ",".join(["0.05"] * 1536) + "]"


def _zero_embed(text):
    # Constant 1536-dim vector satisfies the vector(1536) cast; relevance is
    # beside the point — we only assert the hybrid SQL runs against the real
    # function with every filter wired in.
    return [0.001] * 1536


@override_settings(
    BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_mcp_reads._zero_embed"
)
def test_hybrid_search_runs_with_viewer_filter():
    hits = hybrid_search(VIEWER, "anything", limit=5)
    assert isinstance(hits, list)
    for hit in hits:
        assert {"id", "content", "metadata", "vec_score", "fused_score"} <= hit.keys()
        # Without with_provenance the key must be absent, not null.
        assert "provenance" not in hit


@override_settings(
    BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_mcp_reads._zero_embed"
)
def test_hybrid_search_with_provenance_attaches_list_to_every_hit():
    hits = hybrid_search(VIEWER, "anything", limit=5, with_provenance=True)
    for hit in hits:
        assert isinstance(hit["provenance"], list)


@override_settings(
    BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_mcp_reads._zero_embed"
)
@pytest.mark.usefixtures("brain_write_txn")
def test_provenance_block_ordered_by_confidence_desc():
    """High-trust claims surface first within an experience's provenance block.

    Seeds a low- then high-confidence claim (insertion order is the trap) sourced
    from the same experience and asserts the block comes back confidence-desc.
    """
    eid = str(uuid.uuid4())
    subject = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.experiences (id, content, embedding, owner, visibility) "
            "values (%s::uuid, %s, %s::vector, %s, 'private'::brain.visibility)",
            [eid, "zqprovenance ordering marker", _VEC_LIT, VIEWER],
        )
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name) "
            "values (%s::uuid, 'person'::brain.entity_kind, %s)",
            [subject, f"itest-prov-{subject[:8]}"],
        )
        for conf in (0.3, 0.9):  # low first so an unordered query would mis-rank
            cid = str(uuid.uuid4())
            cur.execute(
                "insert into brain.claims "
                "(id, subject_id, predicate, polarity, confidence) "
                "values (%s::uuid, %s::uuid, 'relates_to', 'asserted'::brain.polarity, %s)",
                [cid, subject, conf],
            )
            cur.execute(
                "insert into brain.claim_sources (claim_id, experience_id, support_kind) "
                "values (%s::uuid, %s::uuid, 'verbatim'::brain.support_kind)",
                [cid, eid],
            )

    hits = hybrid_search(
        VIEWER, "zqprovenance", limit=5, experience_ids=[eid], with_provenance=True
    )
    prov = next(h["provenance"] for h in hits if h["id"] == eid)
    confs = [c["confidence"] for c in prov]
    assert confs == sorted(confs, reverse=True)
    assert confs[0] == pytest.approx(0.9, abs=1e-3)


@override_settings(
    BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_mcp_reads._zero_embed"
)
def test_hybrid_search_person_filter_no_match_is_empty():
    # A name that resolves to no entity must short-circuit to [] without error.
    hits = hybrid_search(VIEWER, "anything", person="zzz-nonexistent-person-zzz")
    assert hits == []


def test_list_thoughts_runs_and_returns_view_models():
    rows = list_thoughts(VIEWER, limit=5)
    assert isinstance(rows, list)
    for row in rows:
        assert {"content", "metadata", "created_at"} <= row.keys()
        assert isinstance(row["metadata"], dict)


def test_list_thoughts_with_all_filters_runs():
    # Exercises every dynamic WHERE branch + the viewer filter against the schema.
    rows = list_thoughts(
        VIEWER, limit=5, type="observation", topic="x", person="Grace", days=30
    )
    assert isinstance(rows, list)


def test_thought_stats_shape():
    stats = thought_stats()
    assert isinstance(stats["total"], int)
    assert isinstance(stats["types"], dict)
    assert isinstance(stats["top_topics"], list)
    assert isinstance(stats["top_people"], list)
    assert stats["date_range"] is None or set(stats["date_range"]) == {"first", "last"}


def test_summary_for_resource_nested_time_range():
    try:
        summary = summary_for_resource()
    except SummaryCacheEmpty:
        pytest.skip("summary cache not populated in this dev volume")
    assert set(summary["time_range"]) == {"earliest", "latest"}
    assert "experience_count" in summary


def test_recent_entities_shape():
    result = recent_entities(window_days=30)
    assert result["window_days"] == 30
    assert isinstance(result["entities"], list)
    for entity in result["entities"]:
        assert {
            "id",
            "kind",
            "canonical_name",
            "aliases",
            "merged_into",
        } <= entity.keys()
        assert isinstance(entity["aliases"], list)


def test_pending_reviews_counts_sum_to_total():
    counts = pending_reviews()
    keys = {
        "merge_candidates",
        "low_confidence_claims",
        "contradictions",
        "disambiguations",
        "proposed_corrections",
    }
    assert keys <= counts.keys()
    assert counts["total"] == sum(counts[k] for k in keys)
