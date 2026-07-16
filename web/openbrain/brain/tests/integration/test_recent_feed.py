"""Integration tests for the recent-feed read service against real brain.*.

Seeds rows (own / shared / private / superseded / deleted, plus an entity
mention) with far-future captured_at so they sort to the top of the viewer's
feed regardless of what the shared dev database already holds, asserts the
service's privacy + lifecycle filtering and projection, then brain_write_txn
rolls the whole transaction back. This is the drift contract for the new SQL
(viewer clause, live filter, metadata->>'source', the entity json_agg).

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import json
import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from openbrain.brain.services.feed import bucket_by_day, get_recent_feed

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

VIEWER = "itest-viewer"
OTHER = "itest-other"

# Far future so seeded rows sort above any pre-existing shared rows in the
# shared dev database; tests assert on membership/relative order of these ids.
FAR = "2999-01-01T00:00:00+00:00"


def _seed(
    owner=VIEWER,
    visibility="private",
    captured_at=FAR,
    content="itest seed",
    source="mcp",
    superseded_by=None,
    deleted_at=None,
):
    eid = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            """
            insert into brain.experiences
              (id, content, owner, visibility, metadata, captured_at,
               superseded_by, deleted_at)
            values (%s::uuid, %s, %s, %s::brain.visibility, %s::jsonb,
                    %s::timestamptz, %s::uuid, %s::timestamptz)
            """,
            [
                eid,
                content,
                owner,
                visibility,
                json.dumps({"source": source}),
                captured_at,
                superseded_by,
                deleted_at,
            ],
        )
    return eid


def _seed_entity_mention(experience_id, name):
    ent_id = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name) "
            "values (%s::uuid, 'concept'::brain.entity_kind, %s)",
            [ent_id, name],
        )
        cur.execute(
            "insert into brain.mentions (experience_id, entity_id, surface_form, field) "
            "values (%s::uuid, %s::uuid, %s, %s)",
            [experience_id, ent_id, name, "topics"],
        )
    return ent_id


def _ids(feed):
    return [row["id"] for row in feed["experiences"]]


def test_excludes_private_not_mine_keeps_own_and_shared():
    own_private = _seed(VIEWER, "private")
    own_shared = _seed(VIEWER, "shared")
    other_shared = _seed(OTHER, "shared")
    other_private = _seed(OTHER, "private")

    ids = _ids(get_recent_feed(VIEWER, 100, 0))

    assert own_private in ids
    assert own_shared in ids
    assert other_shared in ids
    assert other_private not in ids  # private-and-not-mine is absent (US-3)


def test_excludes_superseded_and_deleted():
    live = _seed(VIEWER, content="itest live")
    superseded = _seed(VIEWER, content="itest superseded", superseded_by=live)
    deleted = _seed(
        VIEWER, content="itest deleted", deleted_at="2026-01-01T00:00:00+00:00"
    )

    ids = _ids(get_recent_feed(VIEWER, 100, 0))

    assert live in ids
    assert superseded not in ids
    assert deleted not in ids


def test_orders_newest_first_by_captured_at():
    oldest = _seed(VIEWER, captured_at="2999-01-01T00:00:00+00:00")
    middle = _seed(VIEWER, captured_at="2999-01-02T00:00:00+00:00")
    newest = _seed(VIEWER, captured_at="2999-01-03T00:00:00+00:00")

    mine = [
        i
        for i in _ids(get_recent_feed(VIEWER, 100, 0))
        if i in {oldest, middle, newest}
    ]

    assert mine == [newest, middle, oldest]


def test_pagination_boundary():
    oldest = _seed(VIEWER, captured_at="2999-01-01T00:00:00+00:00")
    middle = _seed(VIEWER, captured_at="2999-01-02T00:00:00+00:00")
    newest = _seed(VIEWER, captured_at="2999-01-03T00:00:00+00:00")

    page1 = get_recent_feed(VIEWER, 2, 0)
    # The three far-future rows are the feed's top; the first page is the two
    # newest, in order, with a further page flagged.
    assert _ids(page1) == [newest, middle]
    assert page1["has_more"] is True
    assert page1["next_offset"] == 2

    page2 = get_recent_feed(VIEWER, 2, 2)
    assert oldest in _ids(page2)


def test_projects_entity_tags():
    eid = _seed(VIEWER, content="itest with entity")
    _seed_entity_mention(eid, "ItestTopic")

    feed = get_recent_feed(VIEWER, 100, 0)
    row = next(r for r in feed["experiences"] if r["id"] == eid)

    assert "ItestTopic" in [e["canonical_name"] for e in row["entities"]]


def test_projects_source_and_visibility():
    eid = _seed(VIEWER, visibility="shared", source="claude")

    feed = get_recent_feed(VIEWER, 100, 0)
    row = next(r for r in feed["experiences"] if r["id"] == eid)

    assert row["source"] == "claude"
    assert row["visibility"] == "shared"


def _bucket_of(groups, exp_id):
    for group in groups:
        if any(row["id"] == exp_id for row in group["rows"]):
            return group["slug"]
    return None


def test_bucket_by_day_buckets_real_feed_rows():
    # The drift contract for the real timestamptz → day-bucket path (#135): the
    # rows come back as psycopg tz-aware datetimes, which bucket_by_day must
    # localize correctly against today.
    today = timezone.localdate()

    def at(d):
        return f"{d.isoformat()}T12:00:00+00:00"

    today_id = _seed(VIEWER, captured_at=at(today), content="itest today")
    yest_id = _seed(
        VIEWER, captured_at=at(today - timedelta(days=1)), content="itest yesterday"
    )
    old_id = _seed(
        VIEWER, captured_at=at(today - timedelta(days=40)), content="itest earlier"
    )

    groups = bucket_by_day(get_recent_feed(VIEWER, 1000, 0)["experiences"], today)

    assert _bucket_of(groups, today_id) == "today"
    assert _bucket_of(groups, yest_id) == "yesterday"
    assert _bucket_of(groups, old_id) == "earlier"
