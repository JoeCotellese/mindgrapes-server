"""OAuth access-token signing + JWKS (Slice 3.1, #73).

Django is the authorization server: it signs short-lived EdDSA access tokens and
publishes the public key as a JWKS. These tests prove the trust seam locally —
a token signed by `sign_access_token` validates *offline* against nothing but
the published JWKS, exactly as the MCP resource server does.
"""

import time
import warnings

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from joserfc import jwt as joserfc_jwt
from joserfc.errors import BadSignatureError, SecurityWarning
from joserfc.jwk import OKPKey

from openbrain.oauth import jwt as oauth_jwt

User = get_user_model()

# A throwaway Ed25519 key, generated once, stands in for the deployed signing key.
_KEY = OKPKey.generate_key("Ed25519", private=True)
_PEM = _KEY.as_pem(private=True).decode()

ISSUER = "https://brain.test"
AUDIENCE = "brain"

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _oauth_settings(settings):
    settings.OAUTH_JWT_PRIVATE_KEY = _PEM
    settings.OAUTH_ISSUER = ISSUER
    settings.OAUTH_AUDIENCE = AUDIENCE
    settings.OAUTH_ACCESS_TTL_SECONDS = 600


def _verify(token, key):
    # We deliberately sign with the JWS "EdDSA" identifier (the resource-server
    # verifier expects it); joserfc emits an RFC 9864 advisory preferring the
    # newer "Ed25519" name. Silence it locally so test output stays pristine.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SecurityWarning)
        return joserfc_jwt.decode(token, key, algorithms=["EdDSA"])


def test_kid_is_stable_rfc7638_thumbprint():
    assert oauth_jwt.kid() == _KEY.thumbprint()
    assert oauth_jwt.kid() == oauth_jwt.kid()  # deterministic across calls


def test_signed_token_header_kid_matches_jwks():
    user = User.objects.create_user(email="a@example.net")
    token = oauth_jwt.sign_access_token(user)
    public_key = OKPKey.import_key(oauth_jwt.jwks()["keys"][0])
    decoded = _verify(token, public_key)
    assert decoded.header["kid"] == oauth_jwt.jwks()["keys"][0]["kid"]
    assert decoded.header["alg"] == "EdDSA"


def test_claims_are_correct():
    user = User.objects.create_user(email="b@example.net")
    before = int(time.time())
    token = oauth_jwt.sign_access_token(user, scope="brain:read")
    public_key = OKPKey.import_key(oauth_jwt.jwks()["keys"][0])
    claims = _verify(token, public_key).claims
    assert claims["sub"] == str(user.pk)
    assert claims["aud"] == "brain"
    assert claims["iss"] == ISSUER
    assert claims["scope"] == "brain:read"
    assert claims["exp"] - claims["iat"] == 600
    assert claims["iat"] >= before


def test_client_id_claim_present_when_provided():
    # The MCP resource server keys its revocation lookup on (sub, client_id),
    # so a browser-flow token must carry the issuing client (Slice 3.3, #75).
    user = User.objects.create_user(email="cid@example.net")
    token = oauth_jwt.sign_access_token(user, client_id="cid123")
    public_key = OKPKey.import_key(oauth_jwt.jwks()["keys"][0])
    claims = _verify(token, public_key).claims
    assert claims["client_id"] == "cid123"


def test_client_id_claim_absent_when_not_provided():
    # The management-command / test mint path has no client; the claim is
    # omitted rather than emitted empty.
    user = User.objects.create_user(email="nocid@example.net")
    token = oauth_jwt.sign_access_token(user)
    public_key = OKPKey.import_key(oauth_jwt.jwks()["keys"][0])
    claims = _verify(token, public_key).claims
    assert "client_id" not in claims


def test_token_validates_offline_against_published_jwks():
    # The verifier is built from ONLY the public JWKS — no private material.
    user = User.objects.create_user(email="c@example.net")
    token = oauth_jwt.sign_access_token(user)
    public_key = OKPKey.import_key(oauth_jwt.jwks()["keys"][0])
    decoded = _verify(token, public_key)
    assert decoded.claims["sub"] == str(user.pk)


def test_tampered_token_is_rejected():
    user = User.objects.create_user(email="d@example.net")
    token = oauth_jwt.sign_access_token(user)
    tampered = token[:-2] + ("BB" if token.endswith("AA") else "AA")
    public_key = OKPKey.import_key(oauth_jwt.jwks()["keys"][0])
    with pytest.raises(BadSignatureError):
        _verify(tampered, public_key)


def test_jwks_shape_is_rfc7517():
    jwks = oauth_jwt.jwks()
    assert set(jwks) == {"keys"}
    key = jwks["keys"][0]
    assert key["kty"] == "OKP"
    assert key["crv"] == "Ed25519"
    assert "x" in key
    assert key["kid"] == oauth_jwt.kid()
    assert key["use"] == "sig"
    assert key["alg"] == "EdDSA"
    assert "d" not in key  # never leak the private scalar


def test_missing_key_raises_improperly_configured(settings):
    settings.OAUTH_JWT_PRIVATE_KEY = ""
    user = User.objects.create_user(email="e@example.net")
    with pytest.raises(ImproperlyConfigured):
        oauth_jwt.sign_access_token(user)
