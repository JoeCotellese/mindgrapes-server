"""OpenRouter embedding client for the Brain UI.

POSTs a single string to OpenRouter's
/embeddings endpoint and returns the 1536-float vector. Raises EmbeddingError on
any failure (non-2xx, transport error/timeout, or a malformed/empty response
body) so the edit path can abort BEFORE opening a transaction — guaranteeing no
partial write when the service is unavailable.
"""

import httpx
from django.conf import settings
from django.utils.module_loading import import_string

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
EMBED_MODEL = "openai/text-embedding-3-small"
_TIMEOUT_SECONDS = 10.0


class EmbeddingError(Exception):
    """The embedding request failed; the caller must not proceed with a write."""


def get_embedding(text: str, *, client: httpx.Client | None = None) -> list[float]:
    if client is not None:
        return _request(client, text)
    with httpx.Client(timeout=_TIMEOUT_SECONDS) as own_client:
        return _request(own_client, text)


def embed_query(text: str) -> list[float]:
    """Embed a search query through the configured embedding function.

    Resolves settings.BRAIN_EMBED_FN (a dotted path) at call time so tests can
    stub embeddings without reaching OpenRouter; the shipped default points at
    get_embedding, the real client.
    """
    return import_string(settings.BRAIN_EMBED_FN)(text)


def _request(client: httpx.Client, text: str) -> list[float]:
    try:
        response = client.post(
            f"{OPENROUTER_BASE}/embeddings",
            headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
            json={"model": EMBED_MODEL, "input": text},
        )
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"OpenRouter embeddings request failed: {exc}") from exc

    if response.status_code // 100 != 2:
        raise EmbeddingError(
            f"OpenRouter embeddings failed: {response.status_code} {response.text}"
        )

    try:
        embedding = response.json()["data"][0]["embedding"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise EmbeddingError(
            f"OpenRouter embeddings malformed response: {exc}"
        ) from exc

    if not isinstance(embedding, list) or not embedding:
        raise EmbeddingError("OpenRouter embeddings returned an empty vector")
    return embedding
