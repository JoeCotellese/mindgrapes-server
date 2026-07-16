# ABOUTME: Unit tests for the structured-output OpenRouter client (no network).
# ABOUTME: Covers extract_first_json_object and call_openrouter_json error prefixing.

import httpx
import pytest
from django.test import override_settings

from openbrain.brain.extraction.openrouter_json import (
    OpenRouterJSONError,
    call_openrouter_json,
    extract_first_json_object,
)

_SCHEMA = {"name": "t", "strict": True, "schema": {"type": "object"}}


def test_extract_first_json_object_clean():
    assert extract_first_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_first_json_object_markdown_fenced():
    assert extract_first_json_object('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_extract_first_json_object_prose_after():
    s = 'Sure: {"a": {"b": 2}} hope that helps'
    assert extract_first_json_object(s) == '{"a": {"b": 2}}'


def test_extract_first_json_object_braces_inside_strings_ignored():
    # A brace inside a string literal must not close the object early.
    assert extract_first_json_object('{"a": "}{"}') == '{"a": "}{"}'


def test_extract_first_json_object_none_when_absent():
    assert extract_first_json_object("no object here") is None


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_call_openrouter_json_returns_parsed_object():
    def handler(_request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"x": 1}'}}]}
        )

    with _client(handler) as client:
        out = call_openrouter_json(
            model="m",
            messages=[],
            json_schema=_SCHEMA,
            max_tokens=10,
            error_prefix="t",
            api_key="k",
            client=client,
        )
    assert out == {"x": 1}


def test_call_openrouter_json_unwraps_fenced_content():
    def handler(_request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '```json\n{"y": 2}\n```'}}]},
        )

    with _client(handler) as client:
        out = call_openrouter_json(
            model="m",
            messages=[],
            json_schema=_SCHEMA,
            max_tokens=10,
            api_key="k",
            client=client,
        )
    assert out == {"y": 2}


def test_call_openrouter_json_http_error_is_prefixed():
    def handler(_request):
        return httpx.Response(500, text="boom")

    with _client(handler) as client:
        with pytest.raises(OpenRouterJSONError) as exc:
            call_openrouter_json(
                model="m",
                messages=[],
                json_schema=_SCHEMA,
                max_tokens=10,
                error_prefix="claim extraction",
                api_key="k",
                client=client,
            )
    assert str(exc.value).startswith("claim extraction: HTTP 500")


def test_call_openrouter_json_no_json_object_raises():
    def handler(_request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "sorry, no json"}}]}
        )

    with _client(handler) as client:
        with pytest.raises(OpenRouterJSONError, match="returned no JSON object"):
            call_openrouter_json(
                model="m",
                messages=[],
                json_schema=_SCHEMA,
                max_tokens=10,
                error_prefix="claim extraction",
                api_key="k",
                client=client,
            )


@override_settings(OPENROUTER_API_KEY="")
def test_call_openrouter_json_missing_api_key_raises():
    with pytest.raises(OpenRouterJSONError, match="OPENROUTER_API_KEY not set"):
        call_openrouter_json(
            model="m",
            messages=[],
            json_schema=_SCHEMA,
            max_tokens=10,
            error_prefix="claim extraction",
        )
