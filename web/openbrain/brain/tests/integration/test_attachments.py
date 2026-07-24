# ABOUTME: Integration tests for brain.blobs / brain.attachments against real Postgres.
# ABOUTME: Covers refcounted dedup, supersede carry-forward, and orphan reconciliation.
"""Attachment integration tests against the real brain.* schema (#42).

Requires the dev stack up (make dev-up) with init/19 applied (brain_ledger
migrate); run via make dev-test-integration. These exercise the brain.blobs /
brain.attachments tables, the refcounted dedup, the get_experience attachment
block, supersede carry-forward, event/place linking, and the orphan-detection
reconciliation. Every write runs inside brain_write_txn and is rolled back.

This file validates the DATABASE substrate: it drives whichever blobstore backend
is configured (the in-memory fake by default, minio in the dev stack) but asserts
on rows, not on bytes over HTTP. The real minio round-trip — a presigned HTTP GET
returning the exact bytes, an expired URL 403ing, and a URL signed for the wrong
host 403ing — lives in test_blobstore_s3.py, against the dev stack's minio
service.
"""

import base64
import io
import uuid

import pytest
from django.db import connection
from django.test import override_settings
from PIL import Image

from openbrain.brain.services import blobstore, edits, image_captures, reads

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("brain_db", "brain_write_txn"),
]

VIEWER = "att-itest-viewer"

def _embed(text):
    # Content-addressed one-hot so distinct content is orthogonal (cosine 0 =>
    # supersede) while identical content collides (cosine 1 => in-place). A flat
    # constant vector would read every edit as in-place regardless of content.
    vec = [0.0] * 1536
    vec[hash(text) % 1536] = 1.0
    return vec


@pytest.fixture(autouse=True)
def _clear_memory_store():
    # Sweep the configured store too: brain_write_txn rolls back the blob ROWS
    # but nothing rolls back an object PUT, so against minio every object these
    # tests store would otherwise linger in the shared dev bucket.
    store = blobstore.get_blobstore()
    before = set(store.list_keys())
    blobstore._MEMORY_STORE.clear()
    yield
    for key in set(store.list_keys()) - before:
        try:
            store.delete(key)
        except Exception:
            pass
    blobstore._MEMORY_STORE.clear()


def _png_b64(width=64, height=48, color=(10, 120, 200)) -> str:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _capture(**overrides):
    params = dict(
        owner=VIEWER,
        account_id="household",
        visibility="private",
        image_base64=_png_b64(),
        description="a small blue test image",
    )
    params.update(overrides)
    return image_captures.capture_image(**params)


@override_settings(BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_attachments._embed")
def test_capture_writes_experience_blob_and_attachment():
    result = _capture()
    eid = result["experience_id"]
    with connection.cursor() as cur:
        cur.execute("select content from brain.experiences where id = %s::uuid", [eid])
        (content,) = cur.fetchone()
        cur.execute(
            "select b.object_key, b.mime from brain.attachments a "
            "join brain.blobs b on b.id = a.blob_id where a.experience_id = %s::uuid",
            [eid],
        )
        rows = cur.fetchall()
    assert content == "a small blue test image"
    assert len(rows) == 1
    assert rows[0][0] == result["object_key"]
    assert rows[0][1] == "image/webp"


@override_settings(BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_attachments._embed")
def test_get_experience_returns_attachment_block():
    eid = _capture()["experience_id"]
    detail = reads.get_experience_detail(VIEWER, eid)
    block = detail["attachment"]
    assert block is not None
    assert block["mime"] == "image/webp"
    assert block["presigned_url"]


@override_settings(BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_attachments._embed")
def test_denied_viewer_gets_no_attachment_and_no_presign():
    eid = _capture(visibility="private")["experience_id"]
    # A different viewer cannot read a private row — identical-404, no presign.
    assert reads.get_experience_detail("someone-else", eid) is None


@override_settings(BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_attachments._embed")
def test_same_bytes_dedup_one_blob_two_attachments_and_blob_survives_delete():
    same = _png_b64(color=(7, 7, 7))
    first = _capture(image_base64=same, description="first caption")["experience_id"]
    second = _capture(image_base64=same, description="second caption")["experience_id"]

    with connection.cursor() as cur:
        cur.execute(
            "select count(distinct a.blob_id), count(*) from brain.attachments a "
            "where a.experience_id in (%s::uuid, %s::uuid)",
            [first, second],
        )
        blob_count, attach_count = cur.fetchone()
    assert blob_count == 1  # one shared blob
    assert attach_count == 2  # two attachment rows

    # Delete one experience: its attachment cascades, the shared blob survives.
    with connection.cursor() as cur:
        cur.execute("delete from brain.experiences where id = %s::uuid", [first])
        cur.execute(
            "select b.object_key from brain.attachments a "
            "join brain.blobs b on b.id = a.blob_id where a.experience_id = %s::uuid",
            [second],
        )
        (object_key,) = cur.fetchone()
    # The surviving experience's blob still resolves in the store.
    store = blobstore.get_blobstore()
    assert store.head(object_key) is not None


@override_settings(BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_attachments._embed")
def test_supersede_caption_carries_attachment_forward():
    eid = _capture(description="whiteboard sketch of the schema")["experience_id"]
    result = edits.edit_experience(
        viewer=VIEWER,
        experience_id=eid,
        content="a completely different description that forces a supersede",
    )
    assert result["mode"] == "superseded"
    new_id = result["new_id"]
    detail = reads.get_experience_detail(VIEWER, new_id)
    assert detail["attachment"] is not None  # the live row still shows the image


@override_settings(BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_attachments._embed")
def test_event_and_place_linked_as_entities():
    eid = _capture(
        event="Rome anniversary trip",
        location={"lat": 41.9, "lng": 12.5, "label": "Rome"},
    )["experience_id"]
    with connection.cursor() as cur:
        cur.execute(
            "select e.kind::text, e.canonical_name from brain.mentions m "
            "join brain.entities e on e.id = m.entity_id "
            "where m.experience_id = %s::uuid order by e.kind",
            [eid],
        )
        rows = cur.fetchall()
    kinds = {r[0] for r in rows}
    assert "event" in kinds
    assert "place" in kinds


@override_settings(BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_attachments._embed")
def test_orphan_blob_detected_by_reconciliation():
    # Simulate a post-put/pre-commit crash: object in the store, no blob row.
    store = blobstore.get_blobstore()
    orphan_key = f"household/{uuid.uuid4().hex}.webp"
    store.put(orphan_key, b"orphaned-bytes", "image/webp")
    assert orphan_key in image_captures.orphan_blob_keys()
