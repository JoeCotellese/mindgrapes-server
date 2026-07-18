# ABOUTME: Integration tests for POST /capture against the real brain.* schema.
# ABOUTME: Bearer-authed bookmark → LLM summary → imported experience, then rolled back.
"""POST /capture endpoint contract against the dev Postgres (#35).

This is the server half of the browser-extension bookmarking loop. The endpoint
verifies a bearer token, summarizes the page (LLM stubbed here — the real call is
exercised by the extension e2e), and stores an imported experience via the same
capture() write service everything else uses. Each test runs inside
brain_write_txn and is rolled back, so the shared dev database is never mutated.

The happy-path test bookmarks the very article that motivated this feature and
asserts it lands in the brain — the acceptance / stop condition from the issue.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import json
import types
import uuid

import pytest
from django.db import connection
from joserfc.jwk import OKPKey

from openbrain.oauth import jwt as oauth_jwt

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

# A throwaway signing key stands in for the deployed one; the endpoint verifies
# against this same in-process key via public_jwk().
_KEY = OKPKey.generate_key("Ed25519", private=True)
_PEM = _KEY.as_pem(private=True).decode()
_VEC = [0.05] * 1536

ARTICLE = "https://claude.com/blog/getting-started-with-loops"
CANNED = "A one-paragraph summary of the page, produced by the LLM."


def _embed(_text):
    return _VEC


@pytest.fixture(autouse=True)
def _capture_settings(settings, monkeypatch):
    settings.OAUTH_JWT_PRIVATE_KEY = _PEM
    settings.OAUTH_ISSUER = "https://brain.test"
    settings.OAUTH_AUDIENCE = "brain"
    settings.OAUTH_ACCESS_TTL_SECONDS = 600
    # Stub the embedding seam (structured capture still embeds) so no OpenRouter.
    settings.BRAIN_EMBED_FN = f"{__name__}._embed"
    # Stub the summary seam — the real LLM call is covered by the extension e2e.
    monkeypatch.setattr("openbrain.core.views._summarize", lambda title, text: CANNED)


def _token(sub="itest-capture-sub"):
    return oauth_jwt.sign_access_token(types.SimpleNamespace(pk=sub))


def _bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _post(client, payload, headers):
    return client.post(
        "/capture",
        data=json.dumps(payload),
        content_type="application/json",
        **headers,
    )


def _row(source_ref):
    with connection.cursor() as cur:
        cur.execute(
            "select content, source_kind::text from brain.experiences "
            "where source_ref = %s",
            [source_ref],
        )
        return cur.fetchone()


def test_bookmarking_the_article_stores_an_imported_experience(client):
    """The stop condition: bookmark the loops article, assert it's in the brain."""
    resp = _post(
        client,
        {"url": ARTICLE, "title": "Getting started with loops", "text": "body text"},
        _bearer(_token()),
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["summary"] == CANNED
    assert body["experience_id"]

    row = _row(ARTICLE)
    assert row is not None
    content, source_kind = row
    assert source_kind == "imported"
    assert content  # non-empty summary stored


def test_missing_bearer_is_unauthorized_and_writes_nothing(client):
    url = f"https://example.test/{uuid.uuid4().hex}"
    resp = _post(client, {"url": url, "title": "t", "text": "y"}, {})
    assert resp.status_code == 401
    assert _row(url) is None


def test_options_preflight_answers_with_cors(client):
    resp = client.options("/capture")
    assert resp.status_code == 204
    assert resp["Access-Control-Allow-Origin"] == "*"


def test_empty_text_still_stores_best_effort(client):
    url = f"https://example.test/{uuid.uuid4().hex}"
    resp = _post(client, {"url": url, "title": "t", "text": ""}, _bearer(_token()))
    assert resp.status_code == 200, resp.content
    assert _row(url) is not None
