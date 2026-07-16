"""Unit tests for the brain read services (view-model transforms, no Postgres).

The real SQL runs against the brain.* schema in the integration suite; here
StubCursor feeds canned rows so we cover the Python transforms — dict mapping,
the empty-cache None, and (later) is_live / 404 / merge-redirect shaping.
"""

from datetime import UTC, datetime, timedelta

from openbrain.brain.services.reads import (
    _recency,
    get_entity_detail,
    get_entity_mentions,
    get_experience_detail,
    get_summary,
    recently_active_entities,
    search_experiences,
)

from ._support import StubCursor, patch_brain_cursor

SUMMARY_COLUMNS = [
    "experience_count",
    "entity_count",
    "claim_count",
    "time_range_earliest",
    "time_range_latest",
    "top_entities",
    "top_topics",
    "refreshed_at",
]


def test_get_summary_returns_row_as_dict(monkeypatch):
    # top_entities / top_topics are jsonb, which this stack returns as text.
    row = (
        3,
        2,
        5,
        None,
        None,
        '[{"id": "e1", "canonical_name": "Joe", "kind": "person", "mention_count": 4}]',
        '[{"topic": "ai", "count": 2}]',
        None,
    )
    cursor = StubCursor([(SUMMARY_COLUMNS, [row])])
    patch_brain_cursor(monkeypatch, cursor)

    summary = get_summary()

    assert summary["experience_count"] == 3
    assert summary["top_entities"][0]["canonical_name"] == "Joe"
    assert summary["top_topics"][0]["topic"] == "ai"


def test_get_summary_is_none_when_cache_empty(monkeypatch):
    cursor = StubCursor([(SUMMARY_COLUMNS, [])])
    patch_brain_cursor(monkeypatch, cursor)

    assert get_summary() is None


EXPERIENCE_COLUMNS = [
    "id",
    "content",
    "captured_at",
    "occurred_at",
    "occurred_window",
    "source_kind",
    "source_ref",
    "metadata",
    "consolidation_status",
    "superseded_by",
    "deleted_at",
    "owner",
    "visibility",
]
MENTION_COLUMNS = ["entity_id", "canonical_name", "kind", "surface_form", "merged_into"]
CLAIM_COLUMNS = [
    "claim_id",
    "predicate",
    "predicate_detail",
    "polarity",
    "confidence",
    "support_kind",
    "source_confidence",
    "extracted_by",
    "subject",
    "object",
]


def _exp_row(owner="7", visibility="private", superseded_by=None, deleted_at=None):
    # metadata is jsonb → text on this stack; the service parses it.
    return (
        "exp-1",
        "hello world",
        None,
        None,
        None,
        "chat",
        None,
        '{"k": "v"}',
        "raw",
        superseded_by,
        deleted_at,
        owner,
        visibility,
    )


def test_get_experience_detail_owner_sees_live_editable(monkeypatch):
    mention = ("ent-1", "Acme", "org", "Acme", None)
    claim = (
        "c1",
        "works_on",
        None,
        "positive",
        0.9,
        "stated",
        0.8,
        "llm",
        {"id": "ent-1", "canonical_name": "Acme", "kind": "org"},
        {"id": None, "canonical_name": None, "kind": None, "literal": "the app"},
    )
    cursor = StubCursor(
        [
            (EXPERIENCE_COLUMNS, [_exp_row(owner="7", visibility="private")]),
            (MENTION_COLUMNS, [mention]),
            (CLAIM_COLUMNS, [claim]),
        ]
    )
    patch_brain_cursor(monkeypatch, cursor)

    detail = get_experience_detail("7", "exp-1")

    assert detail["experience"]["is_live"] is True
    assert detail["experience"]["can_change_visibility"] is True
    assert detail["experience"]["metadata"] == {"k": "v"}  # jsonb text parsed
    assert detail["mentions"][0]["canonical_name"] == "Acme"
    assert detail["claims_sourced_here"][0]["predicate"] == "works_on"


def test_get_experience_detail_missing_returns_none(monkeypatch):
    cursor = StubCursor([(EXPERIENCE_COLUMNS, [])])
    patch_brain_cursor(monkeypatch, cursor)

    assert get_experience_detail("7", "nope") is None


def test_get_experience_detail_private_not_mine_is_identical_404(monkeypatch):
    # Only the experience query is queued: the privacy gate must short-circuit
    # before the mentions/claims queries, returning None like a missing row.
    cursor = StubCursor(
        [(EXPERIENCE_COLUMNS, [_exp_row(owner="9", visibility="private")])]
    )
    patch_brain_cursor(monkeypatch, cursor)

    assert get_experience_detail("7", "exp-1") is None


def test_get_experience_detail_superseded_marks_not_live(monkeypatch):
    cursor = StubCursor(
        [
            (EXPERIENCE_COLUMNS, [_exp_row(owner="7", superseded_by="exp-2")]),
            (MENTION_COLUMNS, []),
            (CLAIM_COLUMNS, []),
        ]
    )
    patch_brain_cursor(monkeypatch, cursor)

    detail = get_experience_detail("7", "exp-1")

    assert detail["experience"]["is_live"] is False
    assert detail["experience"]["superseded_by"] == "exp-2"


def test_get_experience_detail_shared_readable_but_not_editable(monkeypatch):
    cursor = StubCursor(
        [
            (EXPERIENCE_COLUMNS, [_exp_row(owner="9", visibility="shared")]),
            (MENTION_COLUMNS, []),
            (CLAIM_COLUMNS, []),
        ]
    )
    patch_brain_cursor(monkeypatch, cursor)

    detail = get_experience_detail("7", "exp-1")

    assert detail is not None
    assert detail["experience"]["can_change_visibility"] is False


ENTITY_COLUMNS = [
    "id",
    "kind",
    "canonical_name",
    "aliases",
    "confidence",
    "metadata",
    "merged_into",
    "created_at",
]
ENTITY_COUNT_COLUMNS = ["count"]
ENTITY_MENTION_ROW_COLUMNS = [
    "experience_id",
    "captured_at",
    "occurred_at",
    "content",
    "surface_form",
]
ENTITY_CLAIM_COLUMNS = [
    "claim_id",
    "predicate",
    "predicate_detail",
    "polarity",
    "confidence",
    "subject",
    "object",
    "source_count",
]
AUDIT_COLUMNS = [
    "correction_event_id",
    "before",
    "after",
    "reason",
    "created_at",
    "created_by",
]
WINNER_COLUMNS = ["id", "canonical_name", "kind"]


def _entity_row(merged_into=None):
    # metadata is jsonb → text; aliases is a text[] (a real Python list).
    return (
        "ent-1",
        "org",
        "Acme",
        ["Acme Inc"],
        0.9,
        '{"k": "v"}',
        merged_into,
        None,
    )


def test_get_entity_detail_canonical_with_mentions_and_claims(monkeypatch):
    mention = ("exp-1", None, None, "We met with Acme today", "Acme")
    subj_claim = (
        "c1",
        "builds",
        None,
        "positive",
        0.8,
        {"id": "ent-1", "canonical_name": "Acme", "kind": "org"},
        {"id": None, "canonical_name": None, "kind": None, "literal": "an app"},
        1,
    )
    obj_claim = (
        "c2",
        "invested_in",
        None,
        "positive",
        0.7,
        {"id": "ent-9", "canonical_name": "Joe", "kind": "person"},
        {"id": "ent-1", "canonical_name": "Acme", "kind": "org", "literal": None},
        2,
    )
    cursor = StubCursor(
        [
            (ENTITY_COLUMNS, [_entity_row()]),
            (ENTITY_COUNT_COLUMNS, [(1,)]),
            (ENTITY_MENTION_ROW_COLUMNS, [mention]),
            (ENTITY_CLAIM_COLUMNS, [subj_claim]),
            (ENTITY_CLAIM_COLUMNS, [obj_claim]),
        ]
    )
    patch_brain_cursor(monkeypatch, cursor)

    detail = get_entity_detail("7", "ent-1", 50, 0)

    assert detail["is_merged"] is False
    assert detail["entity"]["canonical_name"] == "Acme"
    assert detail["entity"]["metadata"] == {"k": "v"}  # jsonb text parsed
    assert detail["mention_count"] == 1
    assert detail["has_more"] is False
    assert "<mark>Acme</mark>" in detail["mentions"][0]["content_excerpt"]
    assert detail["claims_as_subject"][0]["predicate"] == "builds"
    assert detail["claims_as_object"][0]["predicate"] == "invested_in"


def test_get_entity_detail_merged_returns_redirect_shape(monkeypatch):
    # before / after are jsonb → text on this stack.
    audit = (
        "ce1",
        '{"canonical_name": "Wavly"}',
        '{"merged_into": "ent-2"}',
        "duplicate",
        None,
        "consolidation",
    )
    cursor = StubCursor(
        [
            (ENTITY_COLUMNS, [_entity_row(merged_into="ent-2")]),
            (AUDIT_COLUMNS, [audit]),
            (WINNER_COLUMNS, [("ent-2", "Acme", "org")]),
        ]
    )
    patch_brain_cursor(monkeypatch, cursor)

    detail = get_entity_detail("7", "ent-1", 50, 0)

    assert detail["is_merged"] is True
    assert detail["merged_into"] == "ent-2"
    assert detail["winner"]["canonical_name"] == "Acme"
    assert detail["merge_audit"][0]["reason"] == "duplicate"
    assert detail["merge_audit"][0]["before"] == {"canonical_name": "Wavly"}


def test_get_entity_detail_missing_returns_none(monkeypatch):
    cursor = StubCursor([(ENTITY_COLUMNS, [])])
    patch_brain_cursor(monkeypatch, cursor)

    assert get_entity_detail("7", "nope", 50, 0) is None


def test_get_entity_mentions_page_reports_has_more(monkeypatch):
    cursor = StubCursor(
        [
            (ENTITY_COUNT_COLUMNS, [(3,)]),
            (
                ENTITY_MENTION_ROW_COLUMNS,
                [("exp-2", None, None, "more on Acme", "Acme")],
            ),
        ]
    )
    patch_brain_cursor(monkeypatch, cursor)

    page = get_entity_mentions("7", "ent-1", 1, 1)

    assert page["mention_count"] == 3
    assert page["next_offset"] == 2
    assert page["has_more"] is True
    assert "<mark>Acme</mark>" in page["mentions"][0]["content_excerpt"]


SEARCH_COLUMNS = [
    "id",
    "content",
    "metadata",
    "captured_at",
    "occurred_at",
    "vec_score",
    "lex_score",
    "fused_score",
    "mentioned_entities",
    "claim_count",
]


def test_search_experiences_passes_embedding_and_viewer(monkeypatch):
    monkeypatch.setattr(
        "openbrain.brain.services.reads.embed_query", lambda q: [0.1, 0.2]
    )
    # metadata is jsonb (text); mentioned_entities is json (already parsed).
    hit = (
        "exp-1",
        "Met with Acme about the deal",
        '{"source": "chat"}',
        None,
        None,
        0.83,
        0.5,
        0.9,
        [{"id": "ent-1", "canonical_name": "Acme", "kind": "org"}],
        3,
    )
    cursor = StubCursor([(SEARCH_COLUMNS, [hit])])
    patch_brain_cursor(monkeypatch, cursor)

    results = search_experiences("7", "acme deal", 10)

    assert results[0]["fused_score"] == 0.9
    assert results[0]["metadata"] == {"source": "chat"}  # jsonb text parsed
    assert results[0]["mentioned_entities"][0]["canonical_name"] == "Acme"
    assert results[0]["claim_count"] == 3
    # Params order is [query, vector-literal, limit, viewer]; the embedding is
    # passed as a pgvector literal and the viewer scopes the hybrid filter.
    _sql, params = cursor.calls[0]
    assert params == ["acme deal", "[0.1,0.2]", 10, "7"]


NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def test_recency_buckets_seconds_minutes_hours_days():
    assert _recency(NOW - timedelta(seconds=30), NOW) == "now"
    assert _recency(NOW - timedelta(minutes=5), NOW) == "5m"
    assert _recency(NOW - timedelta(hours=3), NOW) == "3h"
    assert _recency(NOW - timedelta(days=2), NOW) == "2d"


def test_recency_future_timestamp_reads_now():
    # Clock skew / far-future test seeds must not produce a negative label.
    assert _recency(NOW + timedelta(hours=1), NOW) == "now"


RECENTLY_ACTIVE_COLUMNS = ["id", "canonical_name", "kind", "last_mentioned_at"]


def test_recently_active_entities_maps_rows_and_attaches_recency(monkeypatch):
    monkeypatch.setattr("openbrain.brain.services.reads.timezone.now", lambda: NOW)
    last = NOW - timedelta(days=3)
    cursor = StubCursor([(RECENTLY_ACTIVE_COLUMNS, [("ent-1", "Acme", "org", last)])])
    patch_brain_cursor(monkeypatch, cursor)

    rows = recently_active_entities("7")

    assert rows[0]["canonical_name"] == "Acme"
    assert rows[0]["last_mentioned_at"] == last
    assert rows[0]["recency"] == "3d"


def test_recently_active_entities_passes_viewer_window_and_limit(monkeypatch):
    monkeypatch.setattr("openbrain.brain.services.reads.timezone.now", lambda: NOW)
    cursor = StubCursor([(RECENTLY_ACTIVE_COLUMNS, [])])
    patch_brain_cursor(monkeypatch, cursor)

    assert recently_active_entities("7", window_days=14, limit=5) == []

    _sql, params = cursor.calls[0]
    assert params == {"viewer": "7", "window_days": 14, "limit": 5}
