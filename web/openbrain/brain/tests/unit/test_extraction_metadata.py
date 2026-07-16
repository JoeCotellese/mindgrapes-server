# ABOUTME: Unit tests for the bare-capture metadata extractor (no network).
# ABOUTME: Verifies happy-path passthrough and the silent fallback on bad content.

import httpx

from openbrain.brain.extraction.metadata import extract_metadata


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_extract_metadata_passes_through_valid_json():
    payload = {
        "people": ["Grace"],
        "action_items": [],
        "dates_mentioned": [],
        "topics": ["fernworks"],
        "type": "observation",
    }

    def handler(_request):
        import json

        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    with _client(handler) as client:
        out = extract_metadata("Grace mentioned Fernworks", client=client)
    assert out == payload


def test_extract_metadata_falls_back_when_content_not_json():
    def handler(_request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "not json at all"}}]}
        )

    with _client(handler) as client:
        out = extract_metadata("whatever", client=client)
    assert out == {"topics": ["uncategorized"], "type": "observation"}


def test_extract_metadata_falls_back_when_choices_missing():
    # A non-2xx error body with no choices array must fall back — the content
    # access is guarded so a malformed response never blocks the capture.
    def handler(_request):
        return httpx.Response(500, json={"error": "rate limited"})

    with _client(handler) as client:
        out = extract_metadata("whatever", client=client)
    assert out == {"topics": ["uncategorized"], "type": "observation"}
