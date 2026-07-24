# ABOUTME: Integration tests for experience geolocation against real Postgres (#43).
# ABOUTME: Covers the lat/lng columns, partial indexes, live-visible predicate, bbox read.
"""Geolocation integration tests against the real brain.* schema (#43).

Requires the dev stack up (make dev-up); run via make dev-test-integration. These
exercise the lat/lng columns + partial btree indexes, the shared
brain.live_visible_experiences predicate, and the bounding-box read helper — so
column / function drift surfaces here even against an empty database. Every write
runs inside brain_write_txn and is rolled back.
"""

import uuid

import pytest
from django.db import connection

from openbrain.brain.services import captures
from openbrain.brain.services.geo import experiences_in_bbox

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("brain_db", "brain_write_txn"),
]

VIEWER = "geo-itest-viewer"
OTHER = "geo-itest-other"

# All-positive unit vector satisfies the vector(1536) NOT NULL column on seeds.
_VEC_SEED_LIT = "[" + ",".join(["0.05"] * 1536) + "]"


def _seed(lat, lng, owner=VIEWER, visibility="private", content="geo seed"):
    eid = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.experiences "
            "(id, content, embedding, owner, visibility, lat, lng) "
            "values (%s::uuid, %s, %s::vector, %s, %s::brain.visibility, %s, %s)",
            [eid, content, _VEC_SEED_LIT, owner, visibility, lat, lng],
        )
    return eid


def _ids(rows):
    return {row["id"] for row in rows}


def test_bbox_returns_only_in_box_rows():
    inside = _seed(41.9, 12.5)  # Rome
    outside = _seed(48.85, 2.35)  # Paris
    no_loc = _seed(None, None)

    rows = experiences_in_bbox(
        viewer=VIEWER, min_lat=41.0, min_lng=12.0, max_lat=42.0, max_lng=13.0
    )
    got = _ids(rows)
    assert inside in got
    assert outside not in got
    assert no_loc not in got


def test_bbox_keeps_equator_prime_meridian_zero():
    null_island = _seed(0.0, 0.0)
    rows = experiences_in_bbox(
        viewer=VIEWER, min_lat=-1.0, min_lng=-1.0, max_lat=1.0, max_lng=1.0
    )
    assert null_island in _ids(rows)


def test_bbox_honors_viewer_visibility():
    mine = _seed(10.0, 10.0, owner=VIEWER, visibility="private")
    shared = _seed(10.1, 10.1, owner=OTHER, visibility="shared")
    others_private = _seed(10.2, 10.2, owner=OTHER, visibility="private")

    rows = experiences_in_bbox(
        viewer=VIEWER, min_lat=9.0, min_lng=9.0, max_lat=11.0, max_lng=11.0
    )
    got = _ids(rows)
    assert mine in got
    assert shared in got
    assert others_private not in got


def test_bbox_null_viewer_sees_everything():
    others_private = _seed(20.0, 20.0, owner=OTHER, visibility="private")
    rows = experiences_in_bbox(
        viewer=None, min_lat=19.0, min_lng=19.0, max_lat=21.0, max_lng=21.0
    )
    assert others_private in _ids(rows)


def test_bbox_excludes_superseded_and_deleted():
    live = _seed(30.0, 30.0)
    superseded = _seed(30.1, 30.1)
    deleted = _seed(30.2, 30.2)
    with connection.cursor() as cur:
        cur.execute(
            "update brain.experiences set superseded_by = %s::uuid where id = %s::uuid",
            [live, superseded],
        )
        cur.execute(
            "update brain.experiences set deleted_at = now() where id = %s::uuid",
            [deleted],
        )

    rows = experiences_in_bbox(
        viewer=VIEWER, min_lat=29.0, min_lng=29.0, max_lat=31.0, max_lng=31.0
    )
    got = _ids(rows)
    assert live in got
    assert superseded not in got
    assert deleted not in got


def test_bbox_antimeridian_split():
    # Box from +170 wrapping to -170 (20 degrees wide over the Pacific).
    east = _seed(0.0, 175.0)
    west = _seed(0.0, -175.0)
    middle = _seed(0.0, 0.0)  # not in the wrapped range

    rows = experiences_in_bbox(
        viewer=VIEWER, min_lat=-5.0, min_lng=170.0, max_lat=5.0, max_lng=-170.0
    )
    got = _ids(rows)
    assert east in got
    assert west in got
    assert middle not in got


def test_capture_persists_promoted_latlng():
    result = captures.capture(
        content="standing outside the notary, they said yes",
        owner=VIEWER,
        account_id="household",
        visibility="private",
        lat=41.9028,
        lng=12.4964,
    )
    eid = result["experience_id"]
    with connection.cursor() as cur:
        cur.execute(
            "select lat, lng from brain.experiences where id = %s::uuid", [eid]
        )
        lat, lng = cur.fetchone()
    assert round(lat, 4) == 41.9028
    assert round(lng, 4) == 12.4964

    rows = experiences_in_bbox(
        viewer=VIEWER, min_lat=41.0, min_lng=12.0, max_lat=42.0, max_lng=13.0
    )
    assert eid in _ids(rows)


def test_content_edit_carries_latlng_to_superseding_row():
    # Editing the content supersedes the row; only the live row is mappable, so
    # the geotag must ride forward or the memory silently falls off the map.
    from openbrain.brain.services import edits

    original = captures.capture(
        content="at the trattoria, they recommended the cacio e pepe",
        owner=VIEWER,
        account_id="household",
        visibility="private",
        lat=41.9028,
        lng=12.4964,
    )["experience_id"]

    result = edits.edit_experience(
        viewer=VIEWER,
        experience_id=original,
        content="at the trattoria near the Pantheon, cacio e pepe was the move",
    )
    new_id = result["new_id"]

    with connection.cursor() as cur:
        cur.execute(
            "select lat, lng from brain.experiences where id = %s::uuid", [new_id]
        )
        lat, lng = cur.fetchone()
    assert round(lat, 4) == 41.9028
    assert round(lng, 4) == 12.4964

    rows = experiences_in_bbox(
        viewer=VIEWER, min_lat=41.0, min_lng=12.0, max_lat=42.0, max_lng=13.0
    )
    got = _ids(rows)
    assert new_id in got
    assert original not in got  # superseded row is no longer live/mappable
