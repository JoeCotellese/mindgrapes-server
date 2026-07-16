# ABOUTME: Unit tests for the datetime->ISO serializer.
# ABOUTME: Pins JS Date.toISOString() parity (UTC, milliseconds, trailing Z).
from datetime import UTC, datetime, timedelta, timezone

from openbrain.mcp.serialization import serialize, to_iso_z


def test_to_iso_z_utc_millisecond_format():
    dt = datetime(2026, 6, 20, 12, 0, 0, 123456, tzinfo=UTC)
    assert to_iso_z(dt) == "2026-06-20T12:00:00.123Z"


def test_to_iso_z_truncates_micros_to_millis():
    dt = datetime(2026, 6, 20, 12, 0, 0, 999, tzinfo=UTC)
    assert to_iso_z(dt) == "2026-06-20T12:00:00.000Z"


def test_to_iso_z_converts_to_utc():
    est = timezone(timedelta(hours=-5))
    dt = datetime(2026, 6, 20, 7, 0, 0, 0, tzinfo=est)
    assert to_iso_z(dt) == "2026-06-20T12:00:00.000Z"


def test_to_iso_z_naive_treated_as_utc():
    dt = datetime(2026, 6, 20, 12, 0, 0)
    assert to_iso_z(dt) == "2026-06-20T12:00:00.000Z"


def test_serialize_recurses_dicts_and_lists():
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    value = {"a": dt, "b": [dt, {"c": dt}], "d": "x", "n": 3}
    assert serialize(value) == {
        "a": "2026-01-02T03:04:05.000Z",
        "b": ["2026-01-02T03:04:05.000Z", {"c": "2026-01-02T03:04:05.000Z"}],
        "d": "x",
        "n": 3,
    }


def test_serialize_passthrough_primitives():
    assert serialize(None) is None
    assert serialize(5) == 5
    assert serialize("s") == "s"
    assert serialize(True) is True
