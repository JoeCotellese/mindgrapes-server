"""Health + legacy-redirect views, plus the browser-extension capture endpoint.

The walking-skeleton landing page is retired with the legacy /ui surface (#101
Slice D); the site root is now the login-gated Brain dashboard. `capture_api`
(#35) is the bearer-authed POST the Mind Grapes browser extension calls to
bookmark a page: summarize the text, store it as an imported experience.
"""

import json
import warnings

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from joserfc import jwt
from joserfc.errors import JoseError, SecurityWarning
from joserfc.jwk import KeySet

from openbrain.brain.embeddings import EmbeddingError
from openbrain.brain.extraction.openrouter_json import (
    OpenRouterJSONError,
    call_openrouter_json,
)
from openbrain.brain.services import captures
from openbrain.oauth.cors import cors
from openbrain.oauth.jwt import ALG, public_jwk

# Cheap/fast model for one-paragraph page summaries, matching the extraction
# layer's convention (openbrain.brain.extraction.claims.DEFAULT_MODEL).
_SUMMARY_MODEL = "anthropic/claude-haiku-4.5"
_SUMMARY_SCHEMA = {
    "name": "page_summary",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    },
}
# Cap the text sent to the summarizer to bound token cost; the lede carries the
# gist for a bookmark-grade summary.
_SUMMARY_INPUT_CHARS = 8000


def health(request):
    """Liveness probe used by the container healthcheck and integration tests."""
    return HttpResponse("ok", content_type="text/plain")


def _verify_bearer(request) -> str | None:
    """Validate an `Authorization: Bearer` OAuth token, returning its subject.

    Verifies the token offline against the authorization server's own public key
    (this process signs and validates with the same in-memory key), enforcing
    issuer, audience, and expiry via the joserfc claims registry — the same trust
    seam the MCP resource server applies (openbrain.mcp.auth), minus the async
    JWKS fetch. Returns None on any failure so the caller emits a 401.
    """
    scheme, _, token = request.META.get("HTTP_AUTHORIZATION", "").partition(" ")
    # RFC 6750: the Bearer auth scheme is case-insensitive.
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        key_set = KeySet.import_key_set({"keys": [public_jwk()]})
        with warnings.catch_warnings():
            # joserfc emits an RFC 9864 advisory preferring "Ed25519" over the
            # "EdDSA" JWS identifier we sign with; the signing side silences the
            # same warning. Keep verification quiet.
            warnings.simplefilter("ignore", SecurityWarning)
            decoded = jwt.decode(token, key_set, algorithms=[ALG])
        claims = decoded.claims
        registry = jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": settings.OAUTH_ISSUER},
            aud={"essential": True, "value": settings.OAUTH_AUDIENCE},
        )
        registry.validate(claims)
    except (JoseError, ValueError, KeyError):
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None


def ui_legacy_redirect(request):
    """The legacy /ui surface (#101 Slice D) is retired; bounce to the dashboard."""
    return redirect("brain-dashboard")


def _summarize(title: str, text: str) -> str:
    """Summarize a page into one paragraph via OpenRouter (strict JSON mode)."""
    excerpt = (text or "")[:_SUMMARY_INPUT_CHARS]
    messages = [
        {
            "role": "system",
            "content": (
                "Summarize the web page in a single concise paragraph so it can "
                'be recalled later. Respond as JSON: {"summary": ...}.'
            ),
        },
        {"role": "user", "content": f"Title: {title}\n\n{excerpt}"},
    ]
    result = call_openrouter_json(
        model=_SUMMARY_MODEL,
        messages=messages,
        json_schema=_SUMMARY_SCHEMA,
        max_tokens=220,
        error_prefix="capture summary",
    )
    return (result.get("summary") or "").strip()


@csrf_exempt
@cors
def capture_api(request):
    """Bookmark a page for the browser extension: summarize, then store it.

    Bearer-authed (no session cookie — hence csrf_exempt); the extension is a
    cross-origin client, so `cors` answers the preflight and stamps the headers.
    The page is stored through the shared capture() write service as an imported
    experience whose content is the summary and whose source_ref is the URL, so
    it behaves like every other experience in search/recall.
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    sub = _verify_bearer(request)
    if sub is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        # Valid JSON but not an object (a list/scalar body) — .get would crash.
        return JsonResponse({"error": "invalid json"}, status=400)
    url = (payload.get("url") or "").strip()
    if not url:
        return JsonResponse({"error": "url required"}, status=400)

    title = payload.get("title") or ""
    try:
        # Fall back to title/URL so an empty summary never blocks the bookmark.
        summary = _summarize(title, payload.get("text") or "") or title or url
        result = captures.capture(
            content=summary,
            owner=sub,
            account_id=settings.BRAIN_HOUSEHOLD_ACCOUNT_ID,
            source_kind="imported",
            source_ref=url,
            client="browser_extension",
        )
    except (OpenRouterJSONError, EmbeddingError):
        # OpenRouter (summary or embedding) is down — tell the extension so it
        # can surface a retry, rather than leaking a 500 HTML page.
        return JsonResponse({"error": "summary service unavailable"}, status=502)
    return JsonResponse({"experience_id": result["experience_id"], "summary": summary})
