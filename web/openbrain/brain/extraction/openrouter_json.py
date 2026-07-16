# ABOUTME: Structured-output OpenRouter client.
# ABOUTME: call_openrouter_json POSTs a strict json_schema chat request and returns the parsed object.

import json
from datetime import UTC, datetime

import httpx
from django.conf import settings

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_SECONDS = 15.0


def iso_z(value: datetime) -> str:
    """Format a datetime as UTC ISO 8601 for the captured_at the extractors send.

    Naive datetimes are assumed UTC. Shared by claims/temporal so the user
    message format stays consistent across extractors.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class OpenRouterJSONError(Exception):
    """A structured-output OpenRouter call failed (HTTP, timeout, or parse)."""


def extract_first_json_object(s: str) -> str | None:
    """Return the first balanced ``{...}`` block in s, or None.

    OpenRouter does not enforce response_format on every backend, so models
    sometimes wrap JSON in markdown fences and append explanatory prose. This
    scans for the first balanced object — fenced output, prose-after-JSON, and
    clean JSON are handled identically.
    """
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            in_str, escape = _scan_in_string(ch, escape)
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _scan_in_string(ch: str, escape: bool) -> tuple[bool, bool]:
    """Advance the string-literal state machine one char; return (in_str, escape)."""
    if escape:
        return True, False
    if ch == "\\":
        return True, True
    if ch == '"':
        return False, False
    return True, False


def call_openrouter_json(
    *,
    model: str,
    messages: list[dict],
    json_schema: dict,
    max_tokens: int,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    error_prefix: str = "openrouter call",
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """Call OpenRouter chat/completions in strict json_schema mode; return the parsed object.

    Schema validation of the returned object is left to the caller (claims /
    temporal own their own parsers) — this layer guarantees only that the reply
    is a JSON object. Every failure raises OpenRouterJSONError prefixed with
    error_prefix so callers can tell extraction stages apart.
    """
    key = api_key if api_key is not None else settings.OPENROUTER_API_KEY
    if not key:
        raise OpenRouterJSONError(f"{error_prefix}: OPENROUTER_API_KEY not set")

    payload = {
        "model": model,
        "response_format": {"type": "json_schema", "json_schema": json_schema},
        "max_tokens": max_tokens,
        "messages": messages,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    if client is not None:
        return _request(client, payload, headers, timeout, error_prefix)
    with httpx.Client(timeout=timeout) as own_client:
        return _request(own_client, payload, headers, timeout, error_prefix)


def _request(client, payload, headers, timeout, error_prefix):
    try:
        response = client.post(
            f"{OPENROUTER_BASE}/chat/completions", headers=headers, json=payload
        )
    except httpx.TimeoutException as exc:
        raise OpenRouterJSONError(
            f"{error_prefix}: request aborted after {timeout}s"
        ) from exc
    except httpx.HTTPError as exc:
        raise OpenRouterJSONError(f"{error_prefix}: {exc}") from exc

    if response.status_code // 100 != 2:
        raise OpenRouterJSONError(
            f"{error_prefix}: HTTP {response.status_code} {response.text}".strip()
        )

    try:
        raw_content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise OpenRouterJSONError(
            f"{error_prefix}: malformed chat completion envelope ({exc})"
        ) from exc

    json_slice = extract_first_json_object(raw_content)
    if json_slice is None:
        raise OpenRouterJSONError(f"{error_prefix}: model returned no JSON object")
    try:
        return json.loads(json_slice)
    except json.JSONDecodeError as exc:
        raise OpenRouterJSONError(
            f"{error_prefix}: model returned non-JSON content ({exc})"
        ) from exc
