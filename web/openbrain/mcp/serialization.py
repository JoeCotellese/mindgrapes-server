# ABOUTME: Coerces datetimes to JS-style ISO 8601
# ABOUTME: strings recursively so structuredContent keeps the shipped timestamp format.
from datetime import UTC, datetime


def to_iso_z(value: datetime) -> str:
    """Format a datetime as UTC ISO 8601 with millisecond precision + 'Z'.

    Matches JavaScript's Date.prototype.toISOString() — the timestamp format
    MCP clients have always seen. psycopg returns tz-aware
    datetimes for timestamptz; a naive value is assumed UTC.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    millis = value.microsecond // 1000
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def serialize(value):
    """Recursively coerce datetime -> ISO string through dicts and lists.

    psycopg returns datetime objects for
    timestamptz columns, but the MCP output schemas declare ISO strings.
    """
    if isinstance(value, datetime):
        return to_iso_z(value)
    if isinstance(value, list):
        return [serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    return value
