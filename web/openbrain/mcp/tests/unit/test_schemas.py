# ABOUTME: Unit tests for the pydantic output models (the shipped MCP output shapes).
# ABOUTME: Pins nullable-vs-optional semantics that clients depend on.
from openbrain.mcp.schemas import (
    HybridSearchHit,
    ListThoughtsResult,
    SearchThoughtsResult,
    ThoughtStatsResult,
)


def _hit(**overrides) -> dict:
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "content": "a thought",
        "metadata": {"type": "observation", "topics": ["x"]},
        "captured_at": "2026-06-20T12:00:00.000Z",
        "occurred_at": None,
        "vec_score": 0.5,
        "lex_score": 0.1,
        "fused_score": 0.016,
    }
    base.update(overrides)
    return base


def test_search_result_validates_node_shape_without_provenance():
    result = SearchThoughtsResult(count=1, hits=[_hit()])
    assert result.count == 1
    # provenance is the only .optional() field: omitted in -> omitted out.
    assert "provenance" not in result.hits[0].model_dump(exclude_none=True)


def test_hit_keeps_nullable_occurred_at_present_as_null():
    # .nullable() (not .optional()) => key present with value null.
    dumped = HybridSearchHit(**_hit(occurred_at=None)).model_dump()
    assert "occurred_at" in dumped
    assert dumped["occurred_at"] is None


def test_hit_accepts_provenance_block():
    prov = [
        {
            "claim_id": "c1",
            "predicate": "works_at",
            "predicate_detail": None,
            "object_literal": None,
            "polarity": "asserted",
            "confidence": 0.9,
            "support_kind": "inferred",
            "source_confidence": None,
            "extracted_by": "consolidation",
            "superseded_by": None,
        }
    ]
    hit = HybridSearchHit(**_hit(provenance=prov))
    assert hit.provenance[0].predicate == "works_at"


def test_list_thoughts_result_shape():
    result = ListThoughtsResult(
        count=1,
        thoughts=[
            {
                "content": "c",
                "metadata": {"type": "idea"},
                "created_at": "2026-06-20T12:00:00.000Z",
            }
        ],
    )
    assert result.thoughts[0].created_at.endswith("Z")


def test_thought_stats_nullable_date_range():
    result = ThoughtStatsResult(
        total=0, date_range=None, types={}, top_topics=[], top_people=[]
    )
    assert result.date_range is None
    populated = ThoughtStatsResult(
        total=2,
        date_range={
            "first": "2026-01-01T00:00:00.000Z",
            "last": "2026-02-01T00:00:00.000Z",
        },
        types={"observation": 2},
        top_topics=[{"name": "x", "count": 2}],
        top_people=[{"name": "Grace", "count": 1}],
    )
    assert populated.date_range.first.endswith("Z")
    assert populated.top_people[0].name == "Grace"


# --- Slice C models -----------------------------------------------------------


def test_split_entity_result_shape():
    from openbrain.mcp.schemas import SplitEntityResult

    r = SplitEntityResult(
        source_entity_id="s",
        target_entity_id="t",
        target_created=True,
        mentions_repointed=1,
        claims_repointed=2,
        correction_event_ids=["ce1"],
    )
    assert r.claims_repointed == 2


def test_recall_recent_result_reuses_hit_shape():
    from openbrain.mcp.schemas import RecallRecentResult

    r = RecallRecentResult(hits=[_hit(vec_score=0, lex_score=0, fused_score=0)])
    assert r.hits[0].fused_score == 0.0


def test_resolve_correction_drops_apply_only_fields_on_reject():
    from openbrain.mcp.schemas import ResolveCorrectionResult

    reject = ResolveCorrectionResult(id="p", decision="reject", status="rejected")
    dumped = reject.model_dump(exclude_none=True)
    assert "dispatched_tool" not in dumped
    assert "result" not in dumped
    apply = ResolveCorrectionResult(
        id="p",
        decision="apply",
        status="applied",
        dispatched_tool="rename_entity",
        result={"entity_id": "e"},
    )
    assert apply.dispatched_tool == "rename_entity"


def test_review_queue_result_advertises_deferred_count():
    from openbrain.mcp.schemas import ReviewQueueResult

    props = ReviewQueueResult.model_json_schema()["properties"]
    assert "merge_candidates_deferred" in props
    assert (
        ReviewQueueResult(
            merge_candidates=[],
            low_confidence_claims=[],
            contradictions=[],
            disambiguations=[],
            proposed_corrections=[],
        ).merge_candidates_deferred
        == 0
    )


def test_review_queue_result_carries_split_candidates_lane():
    from openbrain.mcp.schemas import ReviewQueueResult

    props = ReviewQueueResult.model_json_schema()["properties"]
    assert "split_candidates" in props
    # Additive: an old-shaped payload omitting the lane still validates, defaulting
    # to an empty list so pre-#15 clients are unaffected.
    r = ReviewQueueResult(
        merge_candidates=[],
        low_confidence_claims=[],
        contradictions=[],
        disambiguations=[],
        proposed_corrections=[],
    )
    assert r.split_candidates == []
    r2 = ReviewQueueResult(
        merge_candidates=[],
        low_confidence_claims=[],
        contradictions=[],
        disambiguations=[],
        proposed_corrections=[],
        split_candidates=[
            {
                "entity_id": "e",
                "canonical_name": "junk drawer",
                "kind": "concept",
                "degree": 42,
            }
        ],
    )
    assert r2.split_candidates[0].degree == 42


def test_review_queue_result_carries_jsonb_payloads():
    from openbrain.mcp.schemas import ReviewQueueResult

    r = ReviewQueueResult(
        merge_candidates=[],
        low_confidence_claims=[],
        contradictions=[],
        disambiguations=[
            {
                "token": "t",
                "question": "Which?",
                "options": [{"label": "A"}, {"label": "B"}],
                "created_at": "2026-06-20T12:00:00.000Z",
            }
        ],
        proposed_corrections=[
            {
                "id": "p",
                "target_kind": "entity",
                "target_id": "e",
                "suggested_change": {"action": "rename", "new_canonical_name": "X"},
                "reason": None,
                "created_at": "2026-06-20T12:00:00.000Z",
            }
        ],
    )
    assert r.proposed_corrections[0].suggested_change["action"] == "rename"
    assert r.disambiguations[0].options[1]["label"] == "B"


def test_disambiguation_option_value_is_omittable():
    from openbrain.mcp.schemas import DisambiguationOption

    bare = DisambiguationOption(label="A")
    assert "value" not in bare.model_dump(exclude_none=True)


def test_resolve_entity_result_requires_recommendation():
    from openbrain.mcp.schemas import ResolveEntityResult

    props = ResolveEntityResult.model_json_schema()["properties"]
    assert "recommendation" in props
    r = ResolveEntityResult(
        query_name="Ada", query_kind="person", candidates=[], recommendation="create"
    )
    assert r.recommendation == "create"


def test_capture_result_provisional_defaults_false_and_needs_disambiguation_optional():
    from openbrain.mcp.schemas import (
        CaptureThoughtResult,
        ExtractedEntity,
        NeedsDisambiguation,
    )

    # provisional defaults False so a non-borderline participant needn't set it.
    assert ExtractedEntity(surface="x", entity_id="e", action="created").provisional is (
        False
    )

    # needs_disambiguation is omittable and drops from output when absent.
    bare = CaptureThoughtResult(
        experience_id="e1", is_structured=False, metadata={}
    ).model_dump(exclude_none=True)
    assert "needs_disambiguation" not in bare

    block = NeedsDisambiguation(
        surface="Vorptangle",
        provisional_entity_id="ent-1",
        candidate_entity_ids=["ent-1"],
        token="tok-1",
        question="Same?",
        options=[{"label": "Same", "value": {"action": "confirm"}}],
    )
    assert block.candidate_entity_ids == ["ent-1"]


def test_resolve_disambiguation_reconciliation_dropped_when_absent():
    from openbrain.mcp.schemas import ResolveDisambiguationResult

    plain = ResolveDisambiguationResult(
        token="t", resolved_choice={"label": "A"}, question="Q?"
    ).model_dump(exclude_none=True)
    assert "reconciliation" not in plain

    reconciled = ResolveDisambiguationResult(
        token="t",
        resolved_choice={"label": "Same"},
        question="Q?",
        reconciliation={"action": "confirmed", "entity_id": "e"},
    )
    assert reconciled.reconciliation["action"] == "confirmed"
