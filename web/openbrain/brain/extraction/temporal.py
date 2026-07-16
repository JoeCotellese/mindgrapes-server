# ABOUTME: Temporal-anchor extractor.
# ABOUTME: parse_temporal_anchor validates the model JSON; extract_temporal_anchor wraps the call.

from datetime import datetime

from openbrain.brain.extraction.openrouter_json import call_openrouter_json, iso_z

DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_TOKENS = 200


class TemporalValidationError(ValueError):
    """The model's temporal-anchor JSON failed validation."""


# The shipped extraction contract — don't reword without re-validating output.
SYSTEM_PROMPT = """You extract temporal anchors from short notes that a user captured into their second brain.
Return a JSON object with these fields:
- "occurred_at": ISO 8601 timestamp if the note describes a single point in time, otherwise null
- "occurred_window": object {"lower": ISO 8601, "upper": ISO 8601} for a fuzzy range (e.g. "last week", "around May 2025"), otherwise null
- "confidence": number 0..1 expressing how anchored the note is in time

Rules:
- Set at most ONE of occurred_at / occurred_window. The other must be null.
- If the note has no temporal anchor at all, set both to null and confidence to 0.
- Resolve relative phrases ("yesterday", "last Tuesday") against the capture timestamp the user provides.
- Do not invent dates; if you are not sure, prefer null + low confidence."""

TEMPORAL_ANCHOR_JSON_SCHEMA = {
    "name": "temporal_anchor",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["occurred_at", "occurred_window", "confidence"],
        "properties": {
            "occurred_at": {"type": ["string", "null"]},
            "occurred_window": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": ["lower", "upper"],
                "properties": {
                    "lower": {"type": "string"},
                    "upper": {"type": "string"},
                },
            },
            # No bounds: Bedrock rejects them; parse_temporal_anchor enforces 0..1.
            "confidence": {"type": "number"},
        },
    },
}


def parse_temporal_anchor(raw: dict) -> dict:
    """Validate the model's temporal anchor and return parsed datetimes.

    At most one of occurred_at / occurred_window
    may be set, every timestamp must be ISO 8601, and confidence must be 0..1.
    """
    if not isinstance(raw, dict):
        raise TemporalValidationError("expected a temporal-anchor object")

    occurred_at_raw = raw.get("occurred_at")
    window_raw = raw.get("occurred_window")
    confidence = raw.get("confidence")

    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise TemporalValidationError("confidence must be a number in 0..1")

    if occurred_at_raw is not None and window_raw is not None:
        raise TemporalValidationError(
            "model returned both occurred_at and occurred_window"
        )

    occurred_at = _iso(occurred_at_raw, "occurred_at") if occurred_at_raw else None

    window = None
    if window_raw is not None:
        if not isinstance(window_raw, dict):
            raise TemporalValidationError("occurred_window must be an object")
        window = {
            "lower": _iso(window_raw.get("lower"), "occurred_window.lower"),
            "upper": _iso(window_raw.get("upper"), "occurred_window.upper"),
        }

    return {
        "occurred_at": occurred_at,
        "occurred_window": window,
        "confidence": float(confidence),
    }


def _iso(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise TemporalValidationError(f"{field} must be a non-empty ISO 8601 string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise TemporalValidationError(f"{field} is not a valid ISO 8601 date") from exc


def extract_temporal_anchor(
    content: str,
    captured_at: datetime,
    *,
    model: str | None = None,
    timeout: float | None = None,
    api_key: str | None = None,
    client=None,
) -> dict:
    """Extract a temporal anchor from one note via OpenRouter."""
    raw = call_openrouter_json(
        model=model or DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"captured_at: {iso_z(captured_at)}\n\ncontent:\n{content}",
            },
        ],
        json_schema=TEMPORAL_ANCHOR_JSON_SCHEMA,
        max_tokens=DEFAULT_MAX_TOKENS,
        timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
        error_prefix="temporal extraction",
        api_key=api_key,
        client=client,
    )
    return parse_temporal_anchor(raw)
