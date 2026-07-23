# ABOUTME: EXIF GPS decoding and lat/lng promotion for experience geolocation (#43).
# ABOUTME: Pure functions — no I/O — so the capture path can promote params over EXIF.


def _dms_to_decimal(dms) -> float | None:
    """Convert an EXIF (degrees, minutes, seconds) triple to signed-magnitude decimal.

    The magnitude is always non-negative here; the hemisphere sign is applied
    separately from GPSLatitudeRef / GPSLongitudeRef by the caller.
    """
    try:
        deg, minute, sec = (float(part) for part in dms)
    except (TypeError, ValueError):
        return None
    return deg + minute / 60.0 + sec / 3600.0


def exif_gps_to_latlng(gps) -> tuple[float | None, float | None]:
    """Decode a Pillow GPSInfo mapping into (lat, lng), or (None, None) if absent.

    Accepts either the human-readable keys (GPSLatitude, GPSLatitudeRef, ...) or
    the raw numeric EXIF tag ids Pillow yields (2/1/4/3). EXIF stores unsigned
    magnitudes plus a hemisphere ref, so S/W must negate — without it every
    southern/western coordinate lands in the wrong hemisphere. Zero at the
    equator / prime meridian is a valid coordinate and is preserved.
    """
    if not gps:
        return None, None

    lat_val = gps.get("GPSLatitude", gps.get(2))
    lng_val = gps.get("GPSLongitude", gps.get(4))
    if lat_val is None or lng_val is None:
        return None, None

    lat = _dms_to_decimal(lat_val)
    lng = _dms_to_decimal(lng_val)
    if lat is None or lng is None:
        return None, None

    lat_ref = gps.get("GPSLatitudeRef", gps.get(1))
    lng_ref = gps.get("GPSLongitudeRef", gps.get(3))
    if lat_ref and str(lat_ref).strip().upper().startswith("S"):
        lat = -lat
    if lng_ref and str(lng_ref).strip().upper().startswith("W"):
        lng = -lng
    return lat, lng


def promote_latlng(
    param_lat: float | None,
    param_lng: float | None,
    exif_lat: float | None,
    exif_lng: float | None,
) -> tuple[float | None, float | None]:
    """Caller-supplied params win; EXIF is the fallback (#43).

    `is not None` (never truthiness) so 0.0 at the equator / prime meridian is a
    real coordinate that is kept, not silently replaced by the EXIF value.
    """
    lat = param_lat if param_lat is not None else exif_lat
    lng = param_lng if param_lng is not None else exif_lng
    return lat, lng
