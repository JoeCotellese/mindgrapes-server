# ABOUTME: OpenRouter vision seam (#42) — describe an image when the caller gave no text.
# ABOUTME: Own (larger) timeout than the embed call; only reached for opt-in visibility.
"""Vision fallback: a one-line description so an otherwise-textless image is
retrievable. Indirected behind settings.BRAIN_VISION_FN (a dotted path resolved
at call time) exactly like BRAIN_EMBED_FN, so the unit suite stubs it and never
egresses image bytes.

This is a CROSS-BOUNDARY data flow: it POSTs the (derivative) image bytes to a
third party. The caller (image_captures.capture_image) is responsible for gating
it on visibility — private captures fail closed to a placeholder and never reach
here. Documented in the tool's side-effects and docs/deploy.md.
"""

import base64

import httpx
from django.conf import settings
from django.utils.module_loading import import_string

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
VISION_MODEL = "openai/gpt-4o-mini"
# Deliberately larger than the 10s embed timeout — vision inference is slower.
_TIMEOUT_SECONDS = 30.0

_PROMPT = (
    "Describe this image in one or two plain sentences for later retrieval. "
    "Note any legible text, people, place, and what is happening. No preamble."
)


class VisionError(Exception):
    """The vision request failed; the caller degrades to a placeholder + pending."""


def describe_image(image_bytes: bytes, mime: str = "image/webp") -> str:
    """Return a short description of the image via OpenRouter, or raise VisionError."""
    data_uri = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
                json={
                    "model": VISION_MODEL,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": _PROMPT},
                                {"type": "image_url", "image_url": {"url": data_uri}},
                            ],
                        }
                    ],
                },
            )
    except httpx.HTTPError as exc:
        raise VisionError(f"OpenRouter vision request failed: {exc}") from exc

    if response.status_code // 100 != 2:
        raise VisionError(
            f"OpenRouter vision failed: {response.status_code} {response.text}"
        )
    try:
        text = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise VisionError(f"OpenRouter vision malformed response: {exc}") from exc
    if not isinstance(text, str) or not text.strip():
        raise VisionError("OpenRouter vision returned empty text")
    return text.strip()


def describe(image_bytes: bytes, mime: str = "image/webp") -> str:
    """Describe through the configured vision function (stubbed in tests)."""
    return import_string(settings.BRAIN_VISION_FN)(image_bytes, mime)
