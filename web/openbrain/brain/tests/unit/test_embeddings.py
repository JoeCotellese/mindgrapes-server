"""Unit tests for the OpenRouter embedding client.

Uses httpx.MockTransport (built in — no extra dependency) so the request shape
and every failure mode are exercised without touching the network.
"""

import json

import httpx
import pytest
from django.conf import settings
from django.test import override_settings
from django.utils.module_loading import import_string

from openbrain.brain.embeddings import EmbeddingError, embed_query, get_embedding

_embed_calls: list[str] = []


def _fake_embed(text: str) -> list[float]:
    """Module-level stub resolvable by dotted path for the BRAIN_EMBED_FN seam."""
    _embed_calls.append(text)
    return [0.5, 0.25, 0.125]


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_success_returns_float_vector():
    vector = [0.01 * i for i in range(1536)]

    def handler(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/embeddings"
        assert request.method == "POST"
        assert request.headers["Authorization"].startswith("Bearer ")
        body = json.loads(request.content)
        assert body == {"model": "openai/text-embedding-3-small", "input": "hello"}
        return httpx.Response(200, json={"data": [{"embedding": vector}]})

    result = get_embedding("hello", client=_client(handler))
    assert result == vector
    assert len(result) == 1536


def test_non_2xx_raises_embedding_error():
    def handler(request):
        return httpx.Response(500, text="upstream boom")

    with pytest.raises(EmbeddingError):
        get_embedding("x", client=_client(handler))


def test_transport_error_raises_embedding_error():
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(EmbeddingError):
        get_embedding("x", client=_client(handler))


def test_malformed_body_raises_embedding_error():
    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(EmbeddingError):
        get_embedding("x", client=_client(handler))


def test_empty_vector_raises_embedding_error():
    def handler(request):
        return httpx.Response(200, json={"data": [{"embedding": []}]})

    with pytest.raises(EmbeddingError):
        get_embedding("x", client=_client(handler))


def test_embed_query_default_setting_resolves_to_get_embedding():
    # The shipped default wires the seam to the real OpenRouter client without
    # making a network call here.
    assert import_string(settings.BRAIN_EMBED_FN) is get_embedding


@override_settings(
    BRAIN_EMBED_FN="openbrain.brain.tests.unit.test_embeddings._fake_embed"
)
def test_embed_query_resolves_and_calls_configured_fn():
    _embed_calls.clear()
    result = embed_query("how do vectors work")
    assert result == [0.5, 0.25, 0.125]
    assert _embed_calls == ["how do vectors work"]
