"""Unit tests for the recent-feed read service (view-model transforms, no Postgres).

StubCursor feeds canned rows so we cover the Python side — projection, snippet
truncation, the limit+1 has_more probe, and that the viewer + pagination params
are bound. The real viewer filtering runs against brain.* in the integration
suite; here we only assert the WHERE clause is present and the param is passed.
"""

from datetime import UTC, date, datetime, timedelta

from openbrain.brain.services.feed import (
    SNIPPET_CHARS,
    bucket_by_day,
    get_recent_feed,
)

from ._support import StubCursor, patch_brain_cursor

FEED_COLUMNS = ["id", "content", "captured_at", "source", "visibility", "entities"]


def _row(
    id="exp-1",
    content="hello world",
    captured_at=None,
    source="mcp",
    visibility="private",
    entities=None,
):
    # entities is json (json_agg) → psycopg returns it already parsed as a list.
    return (id, content, captured_at, source, visibility, entities or [])


def _patch(monkeypatch, cursor):
    patch_brain_cursor(monkeypatch, cursor, module="openbrain.brain.services.feed")


def test_get_recent_feed_projects_row_fields(monkeypatch):
    entities = [{"id": "ent-1", "canonical_name": "Acme", "kind": "org"}]
    cursor = StubCursor(
        [
            (
                FEED_COLUMNS,
                [_row(source="claude", visibility="shared", entities=entities)],
            )
        ]
    )
    _patch(monkeypatch, cursor)

    page = get_recent_feed("7", 20, 0)

    row = page["experiences"][0]
    assert row["id"] == "exp-1"
    assert row["snippet"] == "hello world"
    assert row["source"] == "claude"
    assert row["visibility"] == "shared"
    assert row["entities"][0]["canonical_name"] == "Acme"


def test_get_recent_feed_truncates_long_content_to_snippet(monkeypatch):
    long = "x" * (SNIPPET_CHARS + 50)
    cursor = StubCursor([(FEED_COLUMNS, [_row(content=long)])])
    _patch(monkeypatch, cursor)

    snippet = get_recent_feed("7", 20, 0)["experiences"][0]["snippet"]

    assert snippet.endswith("…")
    assert len(snippet) <= SNIPPET_CHARS + 1  # body capped at SNIPPET_CHARS + ellipsis


def test_get_recent_feed_short_content_is_unchanged(monkeypatch):
    cursor = StubCursor([(FEED_COLUMNS, [_row(content="brief")])])
    _patch(monkeypatch, cursor)

    assert get_recent_feed("7", 20, 0)["experiences"][0]["snippet"] == "brief"


def test_get_recent_feed_has_more_when_limit_plus_one_returned(monkeypatch):
    # limit=2 → the service asks for 3; a full 3 rows means a further page exists.
    rows = [_row(id=f"exp-{i}") for i in range(3)]
    cursor = StubCursor([(FEED_COLUMNS, rows)])
    _patch(monkeypatch, cursor)

    page = get_recent_feed("7", 2, 0)

    assert len(page["experiences"]) == 2  # the probe row is trimmed off
    assert page["has_more"] is True
    assert page["next_offset"] == 2


def test_get_recent_feed_last_page_reports_no_more(monkeypatch):
    rows = [_row(id=f"exp-{i}") for i in range(2)]  # fewer than limit+1
    cursor = StubCursor([(FEED_COLUMNS, rows)])
    _patch(monkeypatch, cursor)

    page = get_recent_feed("7", 5, 10)

    assert len(page["experiences"]) == 2
    assert page["has_more"] is False
    assert page["next_offset"] == 12


def test_get_recent_feed_binds_viewer_and_pagination(monkeypatch):
    cursor = StubCursor([(FEED_COLUMNS, [])])
    _patch(monkeypatch, cursor)

    page = get_recent_feed("7", 20, 40)

    sql, params = cursor.calls[0]
    # Viewer scoping is enforced in SQL; assert the clause is present and bound.
    assert "owner = %(viewer)s" in sql
    assert "visibility = 'shared'" in sql
    assert params["viewer"] == "7"
    assert params["limit"] == 21  # limit + 1 probe
    assert params["offset"] == 40
    assert page["experiences"] == []
    assert page["has_more"] is False


# --- bucket_by_day (#135): pure day-grouping over feed rows, today injected ---


def _at(d: date, h=12, m=0, s=0):
    # TIME_ZONE is UTC, so a UTC-aware datetime's local date equals its date.
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=UTC)


def _exp(id, captured_at):
    return {"id": id, "captured_at": captured_at}


def _labels(groups):
    return [g["label"] for g in groups]


def _slugs(groups):
    return [g["slug"] for g in groups]


def test_bucket_by_day_empty_input_returns_empty_list():
    assert bucket_by_day([], date(2026, 6, 17)) == []


def test_bucket_by_day_groups_in_fixed_order_and_omits_empty():
    today = date(2026, 6, 17)  # Wednesday
    rows = [
        _exp("a", _at(today)),
        _exp("b", _at(today - timedelta(days=40))),  # earlier
    ]

    groups = bucket_by_day(rows, today)

    # Only the populated buckets, in canonical order — no empty Yesterday/This week.
    assert _labels(groups) == ["Today", "Earlier"]
    assert _slugs(groups) == ["today", "earlier"]


def test_bucket_by_day_splits_today_yesterday_thisweek_earlier():
    today = date(2026, 6, 17)  # Wednesday; Monday of week = 2026-06-15
    rows = [
        _exp("today", _at(today)),
        _exp("yest", _at(today - timedelta(days=1))),  # Tue 6/16
        _exp("week", _at(date(2026, 6, 15))),  # Mon 6/15 → this week
        _exp("old", _at(date(2026, 6, 14))),  # Sun 6/14 → earlier (last week)
    ]

    groups = bucket_by_day(rows, today)

    assert _labels(groups) == ["Today", "Yesterday", "This week", "Earlier"]
    assert [g["rows"][0]["id"] for g in groups] == ["today", "yest", "week", "old"]


def test_bucket_by_day_midnight_boundary():
    today = date(2026, 6, 17)
    rows = [
        _exp("midnight_today", _at(today, 0, 0, 0)),  # 00:00:00 today → Today
        _exp(
            "eod_yesterday", _at(today - timedelta(days=1), 23, 59, 59)
        ),  # → Yesterday
    ]

    groups = bucket_by_day(rows, today)

    by_slug = {g["slug"]: [r["id"] for r in g["rows"]] for g in groups}
    assert by_slug["today"] == ["midnight_today"]
    assert by_slug["yesterday"] == ["eod_yesterday"]


def test_bucket_by_day_monday_has_no_this_week_bucket():
    today = date(2026, 6, 15)  # Monday → Monday-of-week is today itself
    rows = [
        _exp("today", _at(today)),
        _exp("yest", _at(date(2026, 6, 14))),  # Sun → Yesterday
        _exp("old", _at(date(2026, 6, 13))),  # Sat → Earlier (last week)
    ]

    groups = bucket_by_day(rows, today)

    assert _slugs(groups) == ["today", "yesterday", "earlier"]
    assert "this-week" not in _slugs(groups)


def test_bucket_by_day_all_in_one_bucket_preserves_order():
    today = date(2026, 6, 17)
    rows = [_exp(f"r{i}", _at(today, 9 + i)) for i in range(3)]

    groups = bucket_by_day(rows, today)

    assert len(groups) == 1
    assert groups[0]["slug"] == "today"
    assert [r["id"] for r in groups[0]["rows"]] == ["r0", "r1", "r2"]


def test_bucket_by_day_future_date_folds_into_today():
    today = date(2026, 6, 17)
    groups = bucket_by_day([_exp("fut", _at(date(2999, 1, 1)))], today)

    assert _slugs(groups) == ["today"]


def test_bucket_by_day_none_captured_at_is_earlier():
    today = date(2026, 6, 17)
    groups = bucket_by_day([_exp("nil", None)], today)

    assert _slugs(groups) == ["earlier"]
