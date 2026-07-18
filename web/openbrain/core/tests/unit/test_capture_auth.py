# ABOUTME: Unit tests for the bearer-token verifier behind POST /capture.
# ABOUTME: Proves _verify_bearer accepts our own signed tokens and rejects the rest.
"""Bearer verification for the browser-extension capture endpoint (#35).

The capture endpoint is bearer-authed, not session/cookie-authed: the extension
sends an OAuth access token exactly like an MCP client. `_verify_bearer` must
validate it offline against the in-process signing key and hand back the subject,
so these tests mint tokens with `sign_access_token` and drive requests through a
RequestFactory — no HTTP layer, no Postgres.
"""

from django.test import RequestFactory
from joserfc.jwk import OKPKey

import pytest

from openbrain.core.views import _verify_bearer
from openbrain.oauth import jwt as oauth_jwt

pytestmark = pytest.mark.django_db

from django.contrib.auth import get_user_model

User = get_user_model()

_KEY = OKPKey.generate_key("Ed25519", private=True)
_PEM = _KEY.as_pem(private=True).decode()
ISSUER = "https://brain.test"
AUDIENCE = "brain"


@pytest.fixture(autouse=True)
def _oauth_settings(settings):
    settings.OAUTH_JWT_PRIVATE_KEY = _PEM
    settings.OAUTH_ISSUER = ISSUER
    settings.OAUTH_AUDIENCE = AUDIENCE
    settings.OAUTH_ACCESS_TTL_SECONDS = 600


def _request(auth_header=None):
    extra = {"HTTP_AUTHORIZATION": auth_header} if auth_header else {}
    return RequestFactory().post("/capture", **extra)


def test_valid_token_returns_subject():
    user = User.objects.create_user(email="a@example.net")
    token = oauth_jwt.sign_access_token(user)
    assert _verify_bearer(_request(f"Bearer {token}")) == str(user.pk)


def test_missing_header_returns_none():
    assert _verify_bearer(_request()) is None


def test_non_bearer_scheme_returns_none():
    user = User.objects.create_user(email="b@example.net")
    token = oauth_jwt.sign_access_token(user)
    assert _verify_bearer(_request(f"Basic {token}")) is None


def test_tampered_token_returns_none():
    user = User.objects.create_user(email="c@example.net")
    token = oauth_jwt.sign_access_token(user)
    assert _verify_bearer(_request(f"Bearer {token}x")) is None


def test_expired_token_returns_none():
    user = User.objects.create_user(email="d@example.net")
    token = oauth_jwt.sign_access_token(user, ttl=-1)
    assert _verify_bearer(_request(f"Bearer {token}")) is None
