"""OAuth public metadata endpoints (Slice 3.1, #73).

The JWKS lets the MCP resource server validate tokens offline; the RFC 8414
discovery document advertises the issuer + JWKS location (browser endpoints are
placeholders until #3.2).
"""

import pytest
from django.test import Client
from joserfc.jwk import OKPKey

from openbrain.oauth import jwt as oauth_jwt

_KEY = OKPKey.generate_key("Ed25519", private=True)
_PEM = _KEY.as_pem(private=True).decode()
ISSUER = "https://brain.test"


@pytest.fixture(autouse=True)
def _oauth_settings(settings):
    settings.OAUTH_JWT_PRIVATE_KEY = _PEM
    settings.OAUTH_ISSUER = ISSUER
    settings.OAUTH_AUDIENCE = "brain"


def test_jwks_endpoint_serves_valid_jwks():
    res = Client().get("/oauth/jwks.json")
    assert res.status_code == 200
    assert res["Content-Type"] == "application/json"
    body = res.json()
    assert len(body["keys"]) == 1
    key = body["keys"][0]
    assert key["kty"] == "OKP"
    assert key["crv"] == "Ed25519"
    assert key["kid"] == oauth_jwt.kid()
    assert "d" not in key  # public material only


def test_jwks_endpoint_only_allows_get():
    res = Client().post("/oauth/jwks.json")
    assert res.status_code == 405


def test_as_discovery_advertises_issuer_and_jwks_uri():
    res = Client().get("/.well-known/oauth-authorization-server")
    assert res.status_code == 200
    body = res.json()
    assert body["issuer"] == ISSUER
    assert body["jwks_uri"] == f"{ISSUER}/oauth/jwks.json"
    # Placeholders filled in #3.2 — present so clients can discover the shape.
    assert body["authorization_endpoint"].startswith(ISSUER)
    assert body["token_endpoint"].startswith(ISSUER)
    assert "S256" in body["code_challenge_methods_supported"]
