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


def _pixel_bomb_png(width, height) -> bytes:
    # A byte-tiny PNG declaring an enormous canvas: the decompression-bomb shape.
    # A 1-bit blank image compresses to nothing, so the wire-byte ceilings never
    # see it coming — only a pixel-count check does.
    buf = io.BytesIO()
    Image.new("1", (width, height), 0).save(buf, format="PNG")
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


def test_pixel_bomb_past_pillows_hard_limit_is_a_decode_error():
    # 225 megapixels: Pillow raises DecompressionBombError, which derives straight
    # from Exception and would otherwise escape process_image as a 500.
    raw = _pixel_bomb_png(15000, 15000)
    assert len(raw) < 1024 * 1024
    with pytest.raises(images.ImageDecodeError):
        images.process_image(raw)


def test_pixel_bomb_in_pillows_warn_only_band_is_a_decode_error():
    # 144 megapixels: Pillow only WARNS between MAX_IMAGE_PIXELS and 2x it, and
    # decodes anyway (~430MB resident on the convert("RGB")).
    raw = _pixel_bomb_png(12000, 12000)
    assert len(raw) < 1024 * 1024
    with pytest.raises(images.ImageDecodeError):
        images.process_image(raw)


def test_pixel_ceiling_rejects_from_the_header_below_pillows_thresholds():
    # 56 megapixels: under every Pillow threshold, past ours. The byte ceilings
    # bound compressed bytes; this is the one that bounds the decode.
    raw = _pixel_bomb_png(8000, 7000)
    assert 8000 * 7000 > images.MAX_PIXELS
    with pytest.raises(images.ImageDecodeError, match="pixels"):
        images.process_image(raw)


def test_a_real_phone_photo_clears_the_pixel_ceiling():
    # 48MP (8064x6048) is today's iPhone full-res; it must still be accepted.
    assert 8064 * 6048 <= images.MAX_PIXELS


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


def _jpeg_with_exif(source: Image.Image, *, orientation=None, original=None,
                    offset=None, plain=None) -> bytes:
    # DateTimeOriginal/OffsetTimeOriginal live in the Exif sub-IFD (0x8769);
    # Orientation and DateTime live in IFD0. Write each where a camera writes it.
    exif = source.getexif()
    if orientation is not None:
        exif[Base.Orientation.value] = orientation
    if plain is not None:
        exif[Base.DateTime.value] = plain
    sub = exif.get_ifd(0x8769)
    if original is not None:
        sub[Base.DateTimeOriginal.value] = original
    if offset is not None:
        sub[Base.OffsetTimeOriginal.value] = offset
    buf = io.BytesIO()
    source.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def test_exif_orientation_is_baked_into_the_derivative():
    # Orientation=6 is the iOS portrait default: the raster is stored landscape
    # and only displays upright once rotated. The WebP derivative carries no EXIF
    # and the original bytes are discarded, so an unapplied rotation is lost.
    raw = _jpeg_with_exif(Image.new("RGB", (400, 200), (10, 20, 30)), orientation=6)
    result = images.process_image(raw)
    assert (result.width, result.height) == (200, 400)


def test_no_orientation_tag_leaves_the_frame_alone():
    raw = _jpeg_with_exif(Image.new("RGB", (400, 200), (10, 20, 30)))
    result = images.process_image(raw)
    assert (result.width, result.height) == (400, 200)


def test_exif_orientation_moves_pixels_not_just_the_frame():
    # Orientation=3 (180) keeps the dimensions, so only the pixels prove it ran.
    src = Image.new("RGB", (400, 200), (0, 0, 0))
    src.paste((255, 255, 255), (0, 0, 120, 60))  # white block in the top-left
    result = images.process_image(_jpeg_with_exif(src, orientation=3))
    out = Image.open(io.BytesIO(result.derivative)).convert("RGB")
    assert out.getpixel((out.width - 10, out.height - 10))[0] > 200
    assert out.getpixel((10, 10))[0] < 60


def test_exif_datetime_original_carries_the_camera_utc_offset():
    # Tag 36867 is local wall time; 36881 is the offset iOS writes beside it.
    # Without the offset the value is read as UTC and every capture is skewed.
    raw = _jpeg_with_exif(
        Image.new("RGB", (300, 200), (10, 20, 30)),
        original="2026:07:01 18:30:00",
        offset="+02:00",
    )
    assert images.process_image(raw).exif_occurred_at == "2026-07-01T18:30:00+02:00"


def test_exif_negative_utc_offset_is_preserved():
    raw = _jpeg_with_exif(
        Image.new("RGB", (300, 200), (10, 20, 30)),
        original="2026:07:01 18:30:00",
        offset="-05:00",
    )
    assert images.process_image(raw).exif_occurred_at == "2026-07-01T18:30:00-05:00"


def test_exif_datetime_original_is_read_from_the_exif_sub_ifd():
    raw = _jpeg_with_exif(
        Image.new("RGB", (300, 200), (10, 20, 30)), original="2026:07:01 18:30:00"
    )
    assert images.process_image(raw).exif_occurred_at == "2026-07-01T18:30:00"


def test_malformed_exif_offset_is_ignored():
    raw = _jpeg_with_exif(
        Image.new("RGB", (300, 200), (10, 20, 30)),
        original="2026:07:01 18:30:00",
        offset="not an offset",
    )
    assert images.process_image(raw).exif_occurred_at == "2026-07-01T18:30:00"


def test_exif_datetime_falls_back_to_ifd0_tag_306():
    raw = _jpeg_with_exif(
        Image.new("RGB", (300, 200), (10, 20, 30)), plain="2026:07:01 18:30:00"
    )
    assert images.process_image(raw).exif_occurred_at == "2026-07-01T18:30:00"


def test_no_exif_timestamp_yields_none():
    assert images.process_image(_png_bytes(200, 150)).exif_occurred_at is None
