# ABOUTME: Bare-capture metadata extractor.
# ABOUTME: gpt-4o-mini json_object call with a silent fallback so capture never blocks on a bad parse.

import json

import httpx
from django.conf import settings

from openbrain.brain.extraction.openrouter_json import OPENROUTER_BASE

METADATA_MODEL = "openai/gpt-4o-mini"
_TIMEOUT_SECONDS = 15.0

# The shipped extraction contract — don't reword without re-validating output.
_SYSTEM_PROMPT = """Extract metadata from the user's captured thought. Return JSON with:
- "people": array of people mentioned (empty if none)
- "action_items": array of implied to-dos (empty if none)
- "dates_mentioned": array of dates YYYY-MM-DD (empty if none)
- "topics": array of 1-3 short topic tags (always at least one)
- "type": one of "observation", "task", "idea", "reference", "person_note"
Only extract what's explicitly there."""

_FALLBACK = {"topics": ["uncategorized"], "type": "observation"}


def extract_metadata(text: str, *, client: httpx.Client | None = None) -> dict:
    """Extract bare-capture metadata; fall back to a neutral tag on parse failure.

    The HTTP call and response body
    decode propagate (a transport error aborts the capture before any write),
    while a missing/non-JSON content field silently yields the fallback.
    """
    if client is not None:
        return _request(client, text)
    with httpx.Client(timeout=_TIMEOUT_SECONDS) as own_client:
        return _request(own_client, text)


def _request(client, text):
    response = client.post(
        f"{OPENROUTER_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": METADATA_MODEL,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        },
    )
    body = response.json()
    try:
        return json.loads(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError):
        return dict(_FALLBACK)
