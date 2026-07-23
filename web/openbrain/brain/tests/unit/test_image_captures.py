# ABOUTME: Unit tests for capture_image orchestration (#42) — pre-transaction composition.
# ABOUTME: Stubs embed/vision/blobstore/capture so no OpenRouter, S3, or Postgres is hit.

import base64
import io

import pytest
from PIL import Image

from openbrain.brain.extraction.images import ImageDecodeError
from openbrain.brain.services import blobstore, image_captures

_MOD = "openbrain.brain.services.image_captures"


def _png_b64(width=800, height=600) -> str:
    img = Image.new("RGB", (width, height), (10, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.fixture
def wired(monkeypatch):
    """Stub every network/DB seam; return a dict capturing what capture() got."""
    blobstore._MEMORY_STORE.clear()
    captured: dict = {}

    def fake_capture(**kwargs):
        captured.update(kwargs)
        return {"experience_id": "exp-1", "is_structured": True, "metadata": {}}

    monkeypatch.setattr(f"{_MOD}.embed_query", lambda text: [0.01] * 1536)
    monkeypatch.setattr(f"{_MOD}.captures.capture", fake_capture)
    monkeypatch.setattr(
        f"{_MOD}.blobstore_mod.get_blobstore",
        lambda: blobstore.MemoryBlobstore(bucket="test-bucket"),
    )
    yield captured
    blobstore._MEMORY_STORE.clear()


def _call(**overrides):
    params = dict(
        owner="alice",
        account_id="household",
        visibility="private",
        image_base64=_png_b64(),
        description="A photo of the whiteboard",
    )
    params.update(overrides)
    return image_captures.capture_image(**params)


def test_description_is_the_embedded_content(wired):
    _call(description="A photo of the whiteboard")
    assert wired["content"] == "A photo of the whiteboard"
    assert wired["source_kind"] == "imported"
    assert wired["source_ref"].startswith("attachment:")


def test_ocr_folded_into_content(wired):
    _call(description="Receipt from the hardware store", ocr="TOTAL $42.10")
    assert "Receipt from the hardware store" in wired["content"]
    assert "Detected text: TOTAL $42.10" in wired["content"]
    assert wired["metadata_extra"]["ocr"] == "TOTAL $42.10"


def test_private_image_without_description_never_calls_vision(wired, monkeypatch):
    called = {"vision": False}

    def boom(*a, **k):
        called["vision"] = True
        raise AssertionError("private image must not egress to vision")

    monkeypatch.setattr(f"{_MOD}.vision_describe", boom)
    _call(description=None, visibility="private", occurred_at="2026-07-01T10:00:00Z")
    assert called["vision"] is False
    assert "description pending" in wired["content"]
    assert wired["metadata_extra"]["vision_status"] == "skipped_private"
    assert wired["metadata_extra"]["description_pending"] is True


def test_shared_image_without_description_uses_vision(wired, monkeypatch):
    monkeypatch.setattr(f"{_MOD}.vision_describe", lambda b, m: "A dog on a beach")
    _call(description=None, visibility="shared")
    assert wired["content"] == "A dog on a beach"
    assert wired["metadata_extra"]["vision_status"] == "generated"


def test_shared_image_vision_failure_degrades_to_placeholder(wired, monkeypatch):
    def boom(b, m):
        raise RuntimeError("openrouter down")

    monkeypatch.setattr(f"{_MOD}.vision_describe", boom)
    _call(description=None, visibility="shared", occurred_at="2026-07-01T10:00:00Z")
    assert "description pending" in wired["content"]
    assert wired["metadata_extra"]["vision_status"] == "failed"
    assert wired["metadata_extra"]["description_pending"] is True


def test_blob_is_put_before_capture(wired):
    result = _call()
    store = blobstore.MemoryBlobstore(bucket="test-bucket")
    assert store.head(result["object_key"]) is not None


def test_base64_ceiling_rejects_oversize(wired):
    huge = "A" * (image_captures.MAX_BASE64_CHARS + 1)
    with pytest.raises(image_captures.ImagePayloadError):
        _call(image_base64=huge)


def test_requires_exactly_one_intake(wired):
    with pytest.raises(image_captures.ImagePayloadError):
        _call(image_base64=None, object_key=None)
    with pytest.raises(image_captures.ImagePayloadError):
        _call(image_base64=_png_b64(), object_key="household/x.webp")


def test_non_image_bytes_raise_decode_error(wired):
    payload = base64.b64encode(b"definitely not an image").decode("ascii")
    with pytest.raises(ImageDecodeError):
        _call(image_base64=payload)


def test_metadata_size_bound_rejects_oversize(wired):
    big = {"blob": "x" * (image_captures.MAX_METADATA_BYTES + 10)}
    with pytest.raises(image_captures.ImagePayloadError):
        _call(metadata=big)


def test_metadata_depth_bound_rejects_deep(wired):
    node: dict = {}
    cur = node
    for _ in range(image_captures.MAX_METADATA_DEPTH + 3):
        cur["k"] = {}
        cur = cur["k"]
    with pytest.raises(image_captures.ImagePayloadError):
        _call(metadata=node)


def test_location_params_beat_exif_and_land_lat_lng(wired):
    _call(location={"lat": 45.5, "lng": -73.6, "label": "Montreal"})
    assert wired["lat"] == 45.5
    assert wired["lng"] == -73.6
    assert wired["metadata_extra"]["location"]["label"] == "Montreal"
