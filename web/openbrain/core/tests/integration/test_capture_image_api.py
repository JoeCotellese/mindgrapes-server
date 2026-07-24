# ABOUTME: Integration tests for POST /capture/image against the real brain.* schema.
# ABOUTME: Multipart photo in -> experience + attachment + blob rows, then rolled back.
"""The app image-intake endpoint against the dev Postgres (#42).

The HTTP half of the photo loop: a bearer-authed multipart POST lands an
experience, an attachment row, and a content-addressed blob — the same rows the
MCP capture_image tool writes, because both doors share image_captures. Each test
runs inside brain_write_txn and is rolled back, so the shared dev database is
never mutated.

The blobstore here follows BLOBSTORE_BACKEND (the in-memory fake by default), so
this file validates the DATABASE + HTTP substrate. The real minio round-trip —
a presigned HTTP GET returning the exact bytes — lives in
openbrain/brain/tests/integration/test_blobstore_s3.py.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import io
import os
import types

import pytest
from django.db import connection
from django.test import override_settings
from joserfc.jwk import OKPKey
from PIL import Image

from openbrain.brain.services import blobstore

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

_KEY = OKPKey.generate_key("Ed25519", private=True)
_PEM = _KEY.as_pem(private=True).decode()
_VEC = [0.05] * 1536

URL = "/capture/image"
EMBED = "openbrain.core.tests.integration.test_capture_image_api._embed"


def _embed(_text):
    return _VEC


@pytest.fixture(autouse=True)
def _capture_settings(settings):
    settings.OAUTH_JWT_PRIVATE_KEY = _PEM
    settings.OAUTH_ISSUER = "https://brain.test"
    settings.OAUTH_AUDIENCE = "brain"
    settings.OAUTH_ACCESS_TTL_SECONDS = 600
    settings.BRAIN_EMBED_FN = EMBED


@pytest.fixture(autouse=True)
def _sweep_stored_objects():
    """Delete objects these tests store — brain_write_txn cannot roll back an S3 put.

    The blob rows vanish with the transaction, so anything left behind is an
    orphan by construction (exactly what orphan_blob_keys reports). Sweep it so a
    shared dev bucket doesn't accumulate litter on every run.
    """
    store = blobstore.get_blobstore()
    before = set(store.list_keys())
    yield
    for key in set(store.list_keys()) - before:
        try:
            store.delete(key)
        except Exception:
            pass


def _png(width=64, height=48, color=(10, 120, 200)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _upload(data=None, name="photo.png", content_type="image/png"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, data if data is not None else _png(), content_type)


def _bearer(sub="itest-image-sub"):
    from openbrain.oauth import jwt as oauth_jwt

    token = oauth_jwt.sign_access_token(types.SimpleNamespace(pk=sub))
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _post(client, data=None, headers=None):
    payload = {"image": _upload()} if data is None else data
    return client.post(URL, data=payload, **(headers if headers is not None else _bearer()))


def _attachment_rows(experience_id):
    with connection.cursor() as cur:
        cur.execute(
            "select b.object_key, b.mime, b.byte_len, a.width, a.height "
            "from brain.attachments a join brain.blobs b on b.id = a.blob_id "
            "where a.experience_id = %s::uuid",
            [experience_id],
        )
        return cur.fetchall()


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_multipart_post_writes_experience_attachment_and_blob(client):
    """The stop condition: the app POSTs a photo, the brain holds it."""
    resp = _post(
        client,
        {"image": _upload(), "description": "the whiteboard after the design review"},
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    eid = body["experience_id"]
    assert body["attachment_id"]
    assert body["object_key"]
    assert body["byte_len"] > 0

    with connection.cursor() as cur:
        cur.execute(
            "select content, source_kind::text, metadata->>'source', visibility::text "
            "from brain.experiences where id = %s::uuid",
            [eid],
        )
        content, source_kind, source, visibility = cur.fetchone()
    assert content == "the whiteboard after the design review"
    assert source_kind == "imported"
    assert source == "app"  # the writing client, distinct from how it was acquired
    assert visibility == "private"  # the default, never widened by omission

    rows = _attachment_rows(eid)
    assert len(rows) == 1
    object_key, mime, byte_len, width, height = rows[0]
    assert object_key == body["object_key"]
    assert mime == "image/webp"  # re-encoded, whatever the client sent
    assert byte_len > 0
    assert width and height

    # The derivative is actually in the store under that key.
    assert blobstore.get_blobstore().head(object_key) is not None


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_stored_object_bytes_match_the_recorded_length(client):
    body = _post(client, {"image": _upload(), "description": "a small blue image"}).json()
    store = blobstore.get_blobstore()
    stored = store.get(body["object_key"])
    assert len(stored) == body["byte_len"]
    assert stored[:4] == b"RIFF"  # WebP container


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_geo_event_and_people_land_on_the_row(client):
    resp = _post(
        client,
        {
            "image": _upload(),
            "description": "gelato in the piazza",
            "lat": "41.9028",
            "lng": "12.4964",
            "event": "Rome anniversary trip",
            "people": "Sofia",
            "labels": "food, travel",
            "ocr_text": "GELATERIA",
        },
    )
    assert resp.status_code == 200, resp.content
    eid = resp.json()["experience_id"]
    with connection.cursor() as cur:
        cur.execute(
            "select lat, lng, metadata->'labels'->>0, metadata->>'ocr' "
            "from brain.experiences where id = %s::uuid",
            [eid],
        )
        lat, lng, first_label, ocr = cur.fetchone()
        cur.execute(
            "select e.kind::text from brain.mentions m "
            "join brain.entities e on e.id = m.entity_id "
            "where m.experience_id = %s::uuid",
            [eid],
        )
        kinds = {r[0] for r in cur.fetchall()}
    assert float(lat) == pytest.approx(41.9028, abs=1e-4)
    assert float(lng) == pytest.approx(12.4964, abs=1e-4)
    assert first_label == "food"
    assert ocr == "GELATERIA"
    assert "event" in kinds
    # OCR is folded into the embedded content so search sees it.
    with connection.cursor() as cur:
        cur.execute("select content from brain.experiences where id = %s::uuid", [eid])
        (content,) = cur.fetchone()
    assert "GELATERIA" in content


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_same_photo_twice_dedups_to_one_blob(client):
    raw = _png(color=(7, 7, 7))
    first = _post(client, {"image": _upload(raw), "description": "first caption"}).json()
    second = _post(client, {"image": _upload(raw), "description": "second caption"}).json()
    assert first["object_key"] == second["object_key"]  # content-addressed
    with connection.cursor() as cur:
        cur.execute(
            "select count(distinct a.blob_id), count(*) from brain.attachments a "
            "where a.experience_id in (%s::uuid, %s::uuid)",
            [first["experience_id"], second["experience_id"]],
        )
        blob_count, attach_count = cur.fetchone()
    assert blob_count == 1
    assert attach_count == 2


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_get_experience_detail_exposes_a_presigned_url(client):
    from openbrain.brain.services import reads

    sub = "itest-image-owner"
    body = _post(
        client, {"image": _upload(), "description": "a photo to read back"}, _bearer(sub)
    ).json()
    detail = reads.get_experience_detail(sub, body["experience_id"])
    block = detail["attachment"]
    assert block["mime"] == "image/webp"
    assert block["presigned_url"]
    assert block["byte_len"] == body["byte_len"]


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_unauthorized_post_writes_nothing(client):
    before = _experience_count()
    resp = _post(client, {"image": _upload(), "description": "should not land"}, {})
    assert resp.status_code == 401
    assert _experience_count() == before


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_oversize_upload_is_rejected_and_writes_nothing(client, settings):
    settings.MAX_IMAGE_UPLOAD_BYTES = 128
    before = _experience_count()
    img = Image.frombytes("RGB", (200, 200), os.urandom(200 * 200 * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    resp = _post(client, {"image": _upload(buf.getvalue()), "description": "too big"})
    assert resp.status_code == 413
    assert _experience_count() == before


@override_settings(BRAIN_EMBED_FN=EMBED)
def test_non_image_upload_is_rejected_and_writes_nothing(client):
    before = _experience_count()
    resp = _post(
        client, {"image": _upload(b"this is not an image at all"), "description": "nope"}
    )
    assert resp.status_code == 415
    assert _experience_count() == before


def _experience_count() -> int:
    with connection.cursor() as cur:
        cur.execute("select count(*) from brain.experiences")
        return cur.fetchone()[0]
