# ABOUTME: Pure image ingest helpers for capture_image (#42) — decode/validate/re-encode.
# ABOUTME: Bounded WebP derivative, sha256 dedup key over the ORIGINAL bytes, EXIF read.
"""Image pipeline: turn caller-supplied bytes into a bounded, stored-ready blob.

No I/O beyond in-memory codec work, so the capture service can run this before it
opens a transaction or touches S3. Identity (the dedup key) is sha256 of the
ORIGINAL input bytes, never the re-encoded WebP — libwebp output drifts across
upgrades, so hashing the original keeps dedup stable while encode params are
merely pinned for best-effort reproducibility.

HEIC (iOS camera default) is supported via pillow-heif; a genuinely undecodable
input raises ImageDecodeError naming the accepted formats so a photo is never
silently dropped.
"""

import hashlib
import io

from PIL import Image, UnidentifiedImageError

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - HEIC optional at runtime
    pass

from openbrain.brain.extraction.geo import exif_gps_to_latlng

# Pinned so the derivative is best-effort reproducible across runs on one
# libwebp. Identity does NOT depend on these (see module docstring).
MAX_DIM = 1024
WEBP_QUALITY = 80
WEBP_METHOD = 6
DERIVATIVE_MIME = "image/webp"

# The shrink invariant (derivative smaller than original) only makes sense for a
# genuinely heavy original — a dimensionally-oversized but byte-tiny image (a
# solid-color 4000px PNG) is valid and must not be rejected just because a bounded
# WebP of it is a few bytes larger. Real photos clear this floor comfortably.
SHRINK_FLOOR_BYTES = 64 * 1024

ACCEPTED_FORMATS = "JPEG, PNG, WebP, GIF, HEIC/HEIF, TIFF, BMP"


class ImageDecodeError(Exception):
    """The bytes are not a decodable image; no experience/attachment is written."""


class ProcessedImage:
    """The result of process_image: the derivative plus everything the row needs."""

    def __init__(
        self,
        *,
        derivative: bytes,
        derivative_sha256: str,
        original_sha256: str,
        width: int,
        height: int,
        mime: str,
        exif_lat: float | None,
        exif_lng: float | None,
        exif_occurred_at: str | None,
        is_animated: bool,
    ):
        self.derivative = derivative
        self.derivative_sha256 = derivative_sha256
        self.original_sha256 = original_sha256
        self.width = width
        self.height = height
        self.mime = mime
        self.exif_lat = exif_lat
        self.exif_lng = exif_lng
        self.exif_occurred_at = exif_occurred_at
        self.is_animated = is_animated


def _decode(raw: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # force decode so truncated/corrupt bytes fail here, not later
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ImageDecodeError(
            f"not a decodable image ({exc}); accepted formats: {ACCEPTED_FORMATS}"
        ) from exc
    return img


def _exif_occurred_at(img: Image.Image) -> str | None:
    """EXIF DateTimeOriginal ('YYYY:MM:DD HH:MM:SS') as naive ISO, or None."""
    try:
        exif = img.getexif()
    except Exception:
        return None
    # 36867 = DateTimeOriginal; 306 = DateTime (fallback).
    raw = exif.get(36867) or exif.get(306)
    if not raw or not isinstance(raw, str):
        return None
    try:
        date_part, time_part = raw.strip().split(" ", 1)
        return date_part.replace(":", "-") + "T" + time_part
    except ValueError:
        return None


def _exif_gps(img: Image.Image) -> tuple[float | None, float | None]:
    try:
        exif = img.getexif()
        gps = exif.get_ifd(0x8825)  # GPSInfo IFD
    except Exception:
        return None, None
    return exif_gps_to_latlng(gps)


def _reencode_webp(img: Image.Image) -> tuple[bytes, int, int]:
    """Downscale to fit MAX_DIM (only ever down) and encode a bounded WebP."""
    frame = img
    if getattr(img, "is_animated", False):
        img.seek(0)
        frame = img
    rgb = frame.convert("RGB")
    rgb.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
    buf = io.BytesIO()
    rgb.save(buf, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
    data = buf.getvalue()
    return data, rgb.width, rgb.height


def process_image(raw: bytes) -> ProcessedImage:
    """Decode, validate, and re-encode `raw` to a bounded WebP derivative.

    Raises ImageDecodeError on non-image/corrupt input. Asserts the re-encoded max
    dimension is <= MAX_DIM and, when the original was oversized (a dimension past
    MAX_DIM), that the derivative is smaller than the original — the space-
    optimization invariant. Returns everything the blobs/attachments rows need.
    """
    if not raw:
        raise ImageDecodeError(f"empty image bytes; accepted formats: {ACCEPTED_FORMATS}")

    img = _decode(raw)
    src_w, src_h = img.width, img.height
    is_animated = bool(getattr(img, "is_animated", False))
    exif_lat, exif_lng = _exif_gps(img)
    exif_occurred_at = _exif_occurred_at(img)

    derivative, out_w, out_h = _reencode_webp(img)

    assert out_w <= MAX_DIM and out_h <= MAX_DIM, "derivative exceeds MAX_DIM"
    if (
        max(src_w, src_h) > MAX_DIM
        and len(raw) > SHRINK_FLOOR_BYTES
        and len(derivative) >= len(raw)
    ):
        raise ImageDecodeError(
            "re-encoded derivative is not smaller than a heavy oversized original"
        )

    return ProcessedImage(
        derivative=derivative,
        derivative_sha256=hashlib.sha256(derivative).hexdigest(),
        original_sha256=hashlib.sha256(raw).hexdigest(),
        width=out_w,
        height=out_h,
        mime=DERIVATIVE_MIME,
        exif_lat=exif_lat,
        exif_lng=exif_lng,
        exif_occurred_at=exif_occurred_at,
        is_animated=is_animated,
    )
