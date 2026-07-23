# ABOUTME: Unit tests for the pure image pipeline (#42) — decode/validate/re-encode.
# ABOUTME: Bounds, oversized-derivative invariant, dedup-key + encode determinism, HEIC.

import hashlib
import io
import os

import pytest
from PIL import Image
from PIL.ExifTags import GPS, Base

from openbrain.brain.extraction import images


def _png_bytes(width, height) -> bytes:
    # Incompressible random-noise pixels so the PNG original is heavy, like a real
    # photo — a solid-color image compresses to a few KB and wouldn't exercise the
    # shrink invariant.
    img = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_reencode_bounds_max_dim_1024():
    result = images.process_image(_png_bytes(2000, 1500))
    assert result.width <= images.MAX_DIM
    assert result.height <= images.MAX_DIM
    assert max(result.width, result.height) == images.MAX_DIM
    assert result.mime == "image/webp"


def test_oversized_derivative_smaller_than_original():
    raw = _png_bytes(2400, 1800)
    result = images.process_image(raw)
    assert len(result.derivative) < len(raw)


def test_dedup_key_is_sha256_of_original_bytes():
    raw = _png_bytes(800, 600)
    result = images.process_image(raw)
    assert result.original_sha256 == hashlib.sha256(raw).hexdigest()


def test_encode_is_deterministic_for_pinned_params():
    raw = _png_bytes(1600, 1200)
    a = images.process_image(raw)
    b = images.process_image(raw)
    # Same input + pinned quality/method => byte-identical derivative + same hash.
    assert a.derivative == b.derivative
    assert a.derivative_sha256 == b.derivative_sha256
    assert a.original_sha256 == b.original_sha256


def test_decode_fails_on_non_image_bytes():
    with pytest.raises(images.ImageDecodeError):
        images.process_image(b"this is not an image at all")


def test_decode_fails_on_empty_bytes():
    with pytest.raises(images.ImageDecodeError):
        images.process_image(b"")


def test_decode_fails_on_truncated_image():
    raw = _png_bytes(400, 300)
    with pytest.raises(images.ImageDecodeError):
        images.process_image(raw[: len(raw) // 2])


def test_animated_gif_takes_first_frame():
    frames = [Image.new("RGB", (300, 200), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
    result = images.process_image(buf.getvalue())
    assert result.is_animated is True
    assert result.mime == "image/webp"
    assert result.width <= images.MAX_DIM


def test_heic_round_trips_to_webp():
    pytest.importorskip("pillow_heif")
    src = Image.new("RGB", (1500, 1000), (30, 90, 160))
    buf = io.BytesIO()
    try:
        src.save(buf, format="HEIF")
    except Exception:
        pytest.skip("pillow-heif build cannot encode HEIF in this environment")
    result = images.process_image(buf.getvalue())
    assert result.mime == "image/webp"
    assert max(result.width, result.height) == images.MAX_DIM


def test_exif_gps_promoted_from_image():
    # A JPEG carrying GPS EXIF should surface lat/lng for the capture path.
    img = Image.new("RGB", (500, 400), (10, 20, 30))
    exif = img.getexif()
    gps_ifd = {
        GPS.GPSLatitudeRef: "N",
        GPS.GPSLatitude: (41.0, 53.0, 24.0),
        GPS.GPSLongitudeRef: "E",
        GPS.GPSLongitude: (12.0, 29.0, 32.0),
    }
    exif[Base.GPSInfo] = gps_ifd
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    result = images.process_image(buf.getvalue())
    assert result.exif_lat is not None and result.exif_lng is not None
    assert 41.0 < result.exif_lat < 42.0
    assert 12.0 < result.exif_lng < 13.0
