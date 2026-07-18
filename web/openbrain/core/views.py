"""Health + legacy-redirect views, plus the browser-extension capture endpoint.

The walking-skeleton landing page is retired with the legacy /ui surface (#101
Slice D); the site root is now the login-gated Brain dashboard. `capture_api`
(#35) is the bearer-authed POST the Mind Grapes browser extension calls to
bookmark a page: summarize the text, store it as an imported experience.
"""

import warnings

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect
from joserfc import jwt
from joserfc.errors import JoseError, SecurityWarning
from joserfc.jwk import KeySet

from openbrain.oauth.jwt import ALG, public_jwk


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
    if scheme != "Bearer" or not token:
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
