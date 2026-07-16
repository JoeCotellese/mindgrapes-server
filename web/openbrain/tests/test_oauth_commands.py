"""OAuth management commands (Slice 3.1, #73)."""

import warnings
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from joserfc import jwt as joserfc_jwt
from joserfc.errors import SecurityWarning
from joserfc.jwk import OKPKey

from openbrain.oauth import jwt as oauth_jwt

User = get_user_model()

_KEY = OKPKey.generate_key("Ed25519", private=True)
_PEM = _KEY.as_pem(private=True).decode()

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _oauth_settings(settings):
    settings.OAUTH_JWT_PRIVATE_KEY = _PEM
    settings.OAUTH_ISSUER = "https://brain.test"
    settings.OAUTH_AUDIENCE = "brain"
    settings.OAUTH_ACCESS_TTL_SECONDS = 600


def test_gen_jwt_key_prints_importable_ed25519_pem():
    out = StringIO()
    call_command("gen_jwt_key", stdout=out)
    pem = out.getvalue().strip()
    assert "BEGIN PRIVATE KEY" in pem
    OKPKey.import_key(pem)  # parses without error


def test_mint_access_token_emits_a_valid_token():
    user = User.objects.create_user(email="dev@example.net")
    out = StringIO()
    call_command("mint_access_token", "dev@example.net", stdout=out)
    token = out.getvalue().strip()
    public_key = OKPKey.import_key(oauth_jwt.jwks()["keys"][0])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SecurityWarning)
        decoded = joserfc_jwt.decode(token, public_key, algorithms=["EdDSA"])
    assert decoded.claims["sub"] == str(user.pk)


def test_mint_access_token_unknown_user_errors():
    with pytest.raises(CommandError):
        call_command("mint_access_token", "ghost@example.net")
