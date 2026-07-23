# ABOUTME: Unit tests for EXIF GPS decoding and lat/lng promotion (#43).
# ABOUTME: Covers hemisphere signs, equator/prime-meridian zeros, and params-beat-EXIF.
from openbrain.brain.extraction.geo import exif_gps_to_latlng, promote_latlng


def test_exif_north_east_is_positive():
    gps = {
        "GPSLatitudeRef": "N",
        "GPSLatitude": (41.0, 53.0, 24.0),
        "GPSLongitudeRef": "E",
        "GPSLongitude": (12.0, 29.0, 0.0),
    }
    lat, lng = exif_gps_to_latlng(gps)
    assert round(lat, 4) == 41.8900
    assert round(lng, 4) == 12.4833


def test_exif_south_west_is_negated_from_ref():
    # Unsigned rationals + a S/W ref must yield the southern/western hemisphere.
    gps = {
        "GPSLatitudeRef": "S",
        "GPSLatitude": (33.0, 51.0, 54.0),
        "GPSLongitudeRef": "W",
        "GPSLongitude": (58.0, 22.0, 12.0),
    }
    lat, lng = exif_gps_to_latlng(gps)
    assert lat < 0
    assert lng < 0
    assert round(lat, 4) == -33.8650


def test_exif_numeric_tag_keys_supported():
    # Pillow's raw GPSInfo dict is keyed by numeric EXIF tag ids.
    gps = {1: "N", 2: (0.0, 0.0, 0.0), 3: "E", 4: (0.0, 0.0, 0.0)}
    lat, lng = exif_gps_to_latlng(gps)
    # Zero at the equator / prime meridian is a real coordinate, not "missing".
    assert lat == 0.0
    assert lng == 0.0


def test_exif_missing_returns_none_none():
    assert exif_gps_to_latlng(None) == (None, None)
    assert exif_gps_to_latlng({}) == (None, None)
    assert exif_gps_to_latlng({"GPSLatitudeRef": "N"}) == (None, None)


def test_promote_params_beat_exif():
    lat, lng = promote_latlng(1.5, 2.5, 9.9, 9.9)
    assert (lat, lng) == (1.5, 2.5)


def test_promote_exif_fallback_when_params_absent():
    lat, lng = promote_latlng(None, None, 9.9, 8.8)
    assert (lat, lng) == (9.9, 8.8)


def test_promote_zero_param_is_kept_not_treated_as_missing():
    # 0.0 at the equator must not fall through to the EXIF fallback.
    lat, lng = promote_latlng(0.0, 0.0, 9.9, 8.8)
    assert (lat, lng) == (0.0, 0.0)


def test_promote_all_none_is_none():
    assert promote_latlng(None, None, None, None) == (None, None)
