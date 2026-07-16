"""Integration tests for recently_active_entities against the real brain.* schema.

Seeds experiences (own / shared / private-not-mine, plus an old one and a merged
entity) each linked to a distinct entity via a mention, then asserts the service's
viewer-scoping, window filtering, recency ranking, and merged-exclusion. Most rows
use a far-future captured_at so they sort above whatever the shared dev database
already holds; brain_write_txn rolls the whole transaction back.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import uuid

import pytest
from django.db import connection

from openbrain.brain.services.reads import recently_active_entities

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

VIEWER = "itest-ra-viewer"
OTHER = "itest-ra-other"

# Far future so seeded rows sort above any pre-existing entities (whose mentions
# carry real, past captured_at); still inside any sane recency window.
FAR = "2999-01-01T00:00:00+00:00"


def _seed_mention(name, owner=VIEWER, visibility="private", captured_at=FAR):
    """One experience + entity + mention; returns the entity id."""
    eid = str(uuid.uuid4())
    ent_id = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.experiences (id, content, owner, visibility, captured_at) "
            "values (%s::uuid, %s, %s, %s::brain.visibility, %s::timestamptz)",
            [eid, "itest recently-active", owner, visibility, captured_at],
        )
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name) "
            "values (%s::uuid, 'concept'::brain.entity_kind, %s)",
            [ent_id, name],
        )
        cur.execute(
            "insert into brain.mentions (experience_id, entity_id, surface_form, field) "
            "values (%s::uuid, %s::uuid, %s, %s)",
            [eid, ent_id, name, "topics"],
        )
    return ent_id


def _ids(rows):
    return [row["id"] for row in rows]


def test_excludes_private_not_mine_keeps_own_and_shared():
    own_private = _seed_mention("ra-own-private", VIEWER, "private")
    own_shared = _seed_mention("ra-own-shared", VIEWER, "shared")
    other_shared = _seed_mention("ra-other-shared", OTHER, "shared")
    other_private = _seed_mention("ra-other-private", OTHER, "private")

    ids = _ids(recently_active_entities(VIEWER, limit=1000))

    assert own_private in ids
    assert own_shared in ids
    assert other_shared in ids
    assert other_private not in ids  # private-and-not-mine is absent (AC)


def test_excludes_mentions_outside_window():
    recent = _seed_mention("ra-recent", VIEWER, captured_at=FAR)
    old = _seed_mention("ra-old", VIEWER, captured_at="2000-01-01T00:00:00+00:00")

    ids = _ids(recently_active_entities(VIEWER, window_days=30, limit=1000))

    assert recent in ids
    assert old not in ids


def test_ranks_by_most_recent_mention():
    older = _seed_mention("ra-older", VIEWER, captured_at="2999-01-01T00:00:00+00:00")
    newer = _seed_mention("ra-newer", VIEWER, captured_at="2999-06-01T00:00:00+00:00")

    ranked = [
        row["id"]
        for row in recently_active_entities(VIEWER, limit=1000)
        if row["id"] in {older, newer}
    ]

    assert ranked == [newer, older]


def test_excludes_merged_entities():
    winner = _seed_mention("ra-winner", VIEWER)
    loser = _seed_mention("ra-loser", VIEWER)
    with connection.cursor() as cur:
        cur.execute(
            "update brain.entities set merged_into = %s::uuid where id = %s::uuid",
            [winner, loser],
        )

    ids = _ids(recently_active_entities(VIEWER, limit=1000))

    assert winner in ids
    assert loser not in ids


def test_projects_name_kind_and_recency():
    eid = _seed_mention("ra-projected", VIEWER, captured_at=FAR)

    row = next(
        r for r in recently_active_entities(VIEWER, limit=1000) if r["id"] == eid
    )

    assert row["canonical_name"] == "ra-projected"
    assert row["kind"] == "concept"
    assert row["last_mentioned_at"] is not None
    assert "recency" in row
