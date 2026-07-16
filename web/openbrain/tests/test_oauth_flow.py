"""Browser OAuth flow — models, grants, consent, token, DCR (Slice 3.2, #74).

These run in the default suite (Django test client + sqlite), like
``test_oauth_views.py``. The HTTP-layer flow tests stand in for the issue's
"pytest integration" set; the real-client tunnel ceremony is a manual e2e gate.
"""

import base64
import json
import time
import uuid
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from django.contrib.auth import get_user_model
from django.test import Client
from joserfc.jwk import OKPKey

from openbrain.oauth import scopes
from openbrain.oauth.models import (
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthToken,
)

User = get_user_model()

pytestmark = pytest.mark.django_db

_KEY = OKPKey.generate_key("Ed25519", private=True)
_PEM = _KEY.as_pem(private=True).decode()
ISSUER = "https://brain.test"
REDIRECT_URI = "https://app.example/cb"


@pytest.fixture(autouse=True)
def _oauth_settings(settings):
    settings.OAUTH_JWT_PRIVATE_KEY = _PEM
    settings.OAUTH_ISSUER = ISSUER
    settings.OAUTH_AUDIENCE = "brain"


def _jwt_claims(token: str) -> dict:
    """Decode (without verifying) a JWT's claim set for assertions."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _authorize_query(challenge, *, method="S256", scope="brain:read brain:write"):
    return {
        "response_type": "code",
        "client_id": "cid123",
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "state": "xyz",
        "code_challenge": challenge,
        "code_challenge_method": method,
    }


def _logged_in_client():
    user = User.objects.create_user(email="member@home.co")
    http = Client()
    http.force_login(user)
    return http, user


def _mint_code(http, challenge, **overrides):
    """Run GET+POST authorize as a logged-in user; return the issued code.

    The consent form posts back to the same URL, so the authorize parameters
    stay in the query string and only ``action`` rides in the body.
    """
    query = _authorize_query(challenge)
    query.update(overrides)
    url = "/oauth/authorize?" + urlencode(query)
    assert http.get(url).status_code == 200
    res = http.post(url, {"action": "allow"})
    assert res.status_code == 302, res.content
    return parse_qs(urlparse(res["Location"]).query)["code"][0]


def make_client(client_id="cid123", **meta):
    md = {
        "redirect_uris": ["https://app.example/cb"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "brain:read brain:write",
        "client_name": "Test Client",
    }
    md.update(meta)
    client = OAuthClient(client_id=client_id)
    client.set_client_metadata(md)
    client.save()
    return client


# ---------------------------------------------------------------------------
# scopes
# ---------------------------------------------------------------------------


def test_describe_maps_known_scopes_in_canonical_order():
    # Order follows SCOPES, not the requested string.
    assert scopes.describe("brain:write brain:read") == [
        scopes.SCOPES["brain:read"],
        scopes.SCOPES["brain:write"],
    ]


def test_describe_ignores_unknown_scopes():
    assert scopes.describe("brain:read evil:everything") == [
        scopes.SCOPES["brain:read"]
    ]


def test_describe_handles_empty():
    assert scopes.describe("") == []
    assert scopes.describe(None) == []


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def test_client_mixin_methods():
    client = make_client()
    assert client.get_client_id() == "cid123"
    assert client.get_default_redirect_uri() == "https://app.example/cb"
    assert client.check_redirect_uri("https://app.example/cb")
    assert not client.check_redirect_uri("https://evil.example/cb")
    assert client.check_response_type("code")
    assert client.check_grant_type("authorization_code")
    assert client.check_grant_type("refresh_token")
    assert not client.check_grant_type("password")
    assert client.check_endpoint_auth_method("none", "token")
    # Unsupported requested scopes are filtered to the client's allowed set.
    assert client.get_allowed_scope("brain:read mystery") == "brain:read"
    assert client.client_name == "Test Client"


def test_authorization_code_expiry_and_accessors():
    user = User.objects.create_user(email="a@b.co")
    fresh = OAuthAuthorizationCode.objects.create(
        code="fresh",
        client_id="cid123",
        redirect_uri="https://app.example/cb",
        scope="brain:read",
        user=user,
        auth_time=int(time.time()),
    )
    assert not fresh.is_expired()
    assert fresh.get_redirect_uri() == "https://app.example/cb"
    assert fresh.get_scope() == "brain:read"

    stale = OAuthAuthorizationCode.objects.create(
        code="stale",
        client_id="cid123",
        user=user,
        auth_time=int(time.time()) - 400,
    )
    assert stale.is_expired()


def test_token_mixin_methods():
    user = User.objects.create_user(email="t@b.co")
    client = make_client()
    family = uuid.uuid4()
    token = OAuthToken.objects.create(
        client_id="cid123",
        access_token="header.payload.sig",
        refresh_token="r1",
        scope="brain:read",
        issued_at=int(time.time()),
        expires_in=600,
        family_id=family,
        sub=str(user.pk),
        user=user,
    )
    assert token.check_client(client)
    assert token.get_scope() == "brain:read"
    assert token.get_expires_in() == 600
    assert not token.is_revoked()
    assert token.get_user() == user
    assert token.get_client().client_id == "cid123"

    token.refresh_token_revoked_at = int(time.time())
    assert token.is_revoked()


# ---------------------------------------------------------------------------
# authorize (consent)
# ---------------------------------------------------------------------------


def test_anonymous_authorize_redirects_to_login():
    make_client()
    challenge = create_s256_code_challenge("v" * 64)
    res = Client().get("/oauth/authorize?" + urlencode(_authorize_query(challenge)))
    assert res.status_code == 302
    assert "/accounts/login/" in res["Location"]


def test_consent_screen_shows_client_and_scopes():
    make_client()
    http, _ = _logged_in_client()
    challenge = create_s256_code_challenge("v" * 64)
    res = http.get("/oauth/authorize?" + urlencode(_authorize_query(challenge)))
    assert res.status_code == 200
    body = res.content.decode()
    assert "Test Client" in body
    assert "app.example" in body  # redirect host shown (anti-phishing)
    assert scopes.SCOPES["brain:read"] in body
    assert scopes.SCOPES["brain:write"] in body
    # Cancel is the focused default.
    assert 'value="deny" autofocus' in body


def test_authorize_deny_redirects_with_access_denied():
    make_client()
    http, _ = _logged_in_client()
    challenge = create_s256_code_challenge("v" * 64)
    url = "/oauth/authorize?" + urlencode(_authorize_query(challenge))
    res = http.post(url, {"action": "deny"})
    assert res.status_code == 302
    qs = parse_qs(urlparse(res["Location"]).query)
    assert qs["error"] == ["access_denied"]
    assert qs["state"] == ["xyz"]


def test_authorize_rejects_plain_pkce():
    make_client()
    http, _ = _logged_in_client()
    # A `plain` challenge is an unsupported method — rejected before any code
    # is minted, so `plain` can never reach the token endpoint.
    query = _authorize_query(
        "an-unhashed-plain-challenge-value-1234567890", method="plain"
    )
    res = http.get("/oauth/authorize?" + urlencode(query))
    assert res.status_code == 400
    assert OAuthAuthorizationCode.objects.count() == 0


# ---------------------------------------------------------------------------
# token — authorization_code grant
# ---------------------------------------------------------------------------


def _token(http, **form):
    return http.post("/oauth/token", form)


def test_authorization_code_exchange_mints_jwt():
    make_client()
    http, user = _logged_in_client()
    verifier = "verifier" + "0" * 50
    code = _mint_code(http, create_s256_code_challenge(verifier))

    res = _token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT_URI,
        client_id="cid123",
        code_verifier=verifier,
    )
    assert res.status_code == 200, res.content
    payload = res.json()
    assert payload["token_type"] == "Bearer"
    assert payload["expires_in"] == 600
    claims = _jwt_claims(payload["access_token"])
    assert claims["sub"] == str(user.pk)
    assert claims["aud"] == "brain"
    assert claims["iss"] == ISSUER
    assert "brain:read" in claims["scope"]
    # The issuing client rides in the token so the resource server can key
    # revocation on it.
    assert claims["client_id"] == "cid123"
    # The code is single-use: it's gone after exchange.
    assert OAuthAuthorizationCode.objects.count() == 0


def test_replayed_code_is_rejected():
    make_client()
    http, _ = _logged_in_client()
    verifier = "verifier" + "0" * 50
    code = _mint_code(http, create_s256_code_challenge(verifier))
    common = dict(
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT_URI,
        client_id="cid123",
        code_verifier=verifier,
    )
    assert _token(http, **common).status_code == 200
    replay = _token(http, **common)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_expired_code_is_rejected():
    make_client()
    http, _ = _logged_in_client()
    verifier = "verifier" + "0" * 50
    code = _mint_code(http, create_s256_code_challenge(verifier))
    # Age the code past its 5-minute window.
    OAuthAuthorizationCode.objects.filter(code=code).update(
        auth_time=int(time.time()) - 400
    )
    res = _token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT_URI,
        client_id="cid123",
        code_verifier=verifier,
    )
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_grant"


def test_wrong_code_verifier_is_rejected():
    make_client()
    http, _ = _logged_in_client()
    code = _mint_code(http, create_s256_code_challenge("verifier" + "0" * 50))
    res = _token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT_URI,
        client_id="cid123",
        code_verifier="a-different-verifier-" + "0" * 40,
    )
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# token — refresh rotation + family revocation
# ---------------------------------------------------------------------------


def _full_flow_tokens(http):
    verifier = "verifier" + "0" * 50
    code = _mint_code(http, create_s256_code_challenge(verifier))
    return _token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT_URI,
        client_id="cid123",
        code_verifier=verifier,
    ).json()


def test_refresh_rotation_and_family_revocation():
    make_client()
    http, _ = _logged_in_client()
    first = _full_flow_tokens(http)
    r1 = first["refresh_token"]

    # Rotation: a new refresh token in the same family; the old one is revoked.
    rotated = _token(
        http, grant_type="refresh_token", refresh_token=r1, client_id="cid123"
    )
    assert rotated.status_code == 200, rotated.content
    r2 = rotated.json()["refresh_token"]
    assert r2 != r1
    fams = set(OAuthToken.objects.values_list("family_id", flat=True))
    assert len(fams) == 1  # rotation stays in one lineage

    # Replaying the revoked r1 is reuse-detection: it kills the whole family.
    replay = _token(
        http, grant_type="refresh_token", refresh_token=r1, client_id="cid123"
    )
    assert replay.status_code == 400

    # ...so the freshly rotated r2 is now dead too.
    after = _token(
        http, grant_type="refresh_token", refresh_token=r2, client_id="cid123"
    )
    assert after.status_code == 400


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------


def test_dcr_creates_public_client():
    res = Client().post(
        "/oauth/register",
        data=json.dumps(
            {"redirect_uris": ["https://claude.ai/cb"], "client_name": "Claude"}
        ),
        content_type="application/json",
    )
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["token_endpoint_auth_method"] == "none"
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert body["client_name"] == "Claude"
    client = OAuthClient.objects.get(client_id=body["client_id"])
    assert client.redirect_uris == ["https://claude.ai/cb"]


def test_dcr_rejects_non_loopback_http_redirect():
    res = Client().post(
        "/oauth/register",
        data=json.dumps({"redirect_uris": ["http://evil.example/cb"]}),
        content_type="application/json",
    )
    assert res.status_code == 400
    assert res.json()["error"] == "invalid_redirect_uri"
    assert OAuthClient.objects.count() == 0


def test_dcr_requires_redirect_uris():
    res = Client().post(
        "/oauth/register",
        data=json.dumps({"client_name": "No Redirects"}),
        content_type="application/json",
    )
    assert res.status_code == 400


def test_dcr_allows_loopback_http_redirect():
    res = Client().post(
        "/oauth/register",
        data=json.dumps({"redirect_uris": ["http://127.0.0.1:8765/cb"]}),
        content_type="application/json",
    )
    assert res.status_code == 201


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_discovery_advertises_endpoints_and_scopes():
    body = Client().get("/.well-known/oauth-authorization-server").json()
    assert body["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert body["token_endpoint"] == f"{ISSUER}/oauth/token"
    assert body["registration_endpoint"] == f"{ISSUER}/oauth/register"
    assert body["scopes_supported"] == ["brain:read", "brain:write"]
    assert body["code_challenge_methods_supported"] == ["S256"]


# ---------------------------------------------------------------------------
# CORS — browser-based OAuth clients (e.g. the MCP Inspector)
# ---------------------------------------------------------------------------


def test_discovery_sends_cors_header():
    res = Client().get("/.well-known/oauth-authorization-server")
    assert res["Access-Control-Allow-Origin"] == "*"


def test_register_answers_cors_preflight():
    res = Client().options("/oauth/register")
    assert res.status_code == 204
    assert res["Access-Control-Allow-Origin"] == "*"
    assert "POST" in res["Access-Control-Allow-Methods"]


def test_token_answers_cors_preflight():
    res = Client().options("/oauth/token")
    assert res.status_code == 204
    assert res["Access-Control-Allow-Origin"] == "*"
