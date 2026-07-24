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
import re
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

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

# Every other ceiling in the stack (Caddy's request_body, MAX_IMAGE_UPLOAD_BYTES,
# MAX_IMAGE_BYTES) bounds COMPRESSED bytes, which says nothing about what a decode
# costs: a 17KB 1-bit PNG can declare 144 megapixels and then take ~430MB resident
# through convert("RGB"). Pillow's own guard is not enough — it only warns between
# MAX_IMAGE_PIXELS and 2x that, and decodes anyway. So bound pixels ourselves, from
# the header, before load(). 50MP sits above today's full-res phone cameras (48MP
# = 8064x6048) and well under Pillow's 89MP warn threshold.
MAX_PIXELS = 50_000_000

_EXIF_IFD = 0x8769
_UTC_OFFSET_RE = re.compile(r"^[+-]\d{2}:\d{2}$")


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


def _reject_pixel_bomb(img: Image.Image) -> None:
    """Bound the decode from the header, before load() allocates the pixels."""
    width, height = img.size
    if width * height > MAX_PIXELS:
        raise ImageDecodeError(
            f"image declares too many pixels to decode ({width}x{height} = "
            f"{width * height} pixels > {MAX_PIXELS})"
        )


def _decode(raw: bytes) -> Image.Image:
    try:
        with warnings.catch_warnings():
            # Pillow's bomb guard raises above 2x MAX_IMAGE_PIXELS but only WARNS
            # below it; promote the warning so both land in the ImageDecodeError
            # path instead of one escaping as a 500 and the other decoding anyway.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(raw))
            _reject_pixel_bomb(img)
            img.load()  # force decode so truncated/corrupt bytes fail here, not later
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ImageDecodeError(
            f"not a decodable image ({exc}); accepted formats: {ACCEPTED_FORMATS}"
        ) from exc
    return img


def _exif_occurred_at(img: Image.Image) -> str | None:
    """EXIF DateTimeOriginal as ISO 8601, offset-qualified when the camera said so.

    Tag 36867 carries local wall time and nothing else, and the value rides into
    `%s::timestamptz` with the session TimeZone at Etc/UTC — so an unqualified
    string is read as UTC and a photo shot at 18:30 in Rome anchors two hours
    late. Tag 36881 (OffsetTimeOriginal), which iOS writes precisely to make that
    recoverable, is appended when present; without it the zone is genuinely
    unknowable and the naive value stays as the best-effort anchor.

    36867/36880/36881 live in the Exif sub-IFD, not IFD0; only the 306 fallback
    is an IFD0 tag.
    """
    try:
        exif = img.getexif()
        sub = exif.get_ifd(_EXIF_IFD)
    except Exception:
        return None
    raw, offset = sub.get(36867), sub.get(36881)  # DateTimeOriginal, OffsetTimeOriginal
    if not raw:
        raw, offset = exif.get(306), sub.get(36880)  # DateTime, OffsetTime
    if not raw or not isinstance(raw, str):
        return None
    try:
        date_part, time_part = raw.strip().split(" ", 1)
    except ValueError:
        return None
    stamp = date_part.replace(":", "-") + "T" + time_part
    if isinstance(offset, str) and _UTC_OFFSET_RE.match(offset.strip()):
        stamp += offset.strip()
    return stamp


def _exif_gps(img: Image.Image) -> tuple[float | None, float | None]:
    try:
        exif = img.getexif()
        gps = exif.get_ifd(0x8825)  # GPSInfo IFD
    except Exception:
        return None, None
    return exif_gps_to_latlng(gps)


def _reencode_webp(img: Image.Image) -> tuple[bytes, int, int]:
    """Downscale to fit MAX_DIM (only ever down) and encode a bounded WebP.

    exif_transpose bakes EXIF Orientation into the pixels first. Only the
    derivative is stored (the original bytes are discarded) and the WebP carries
    no EXIF, so an unapplied rotation is lost rather than deferred — and
    Orientation=6 is the iOS portrait default, so this is the common case for a
    real phone photo, not an edge one. Transposing also makes the returned
    width/height the DISPLAY dimensions, which is what attachments records.
    """
    frame = img
    if getattr(img, "is_animated", False):
        img.seek(0)
        frame = img
    rgb = ImageOps.exif_transpose(frame).convert("RGB")
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
