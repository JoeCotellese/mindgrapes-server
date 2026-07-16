# ABOUTME: Unit tests for DjangoJWTVerifier — EdDSA validation + revocation watermark.
# ABOUTME: Signs tokens the way openbrain.oauth.jwt does, so this proves real interop.
import asyncio
import time
import warnings

import pytest
from joserfc import jwt
from joserfc.errors import SecurityWarning
from joserfc.jwk import KeySet, OKPKey

from openbrain.mcp.auth import DjangoJWTVerifier

ISSUER = "http://localhost:8080"
AUDIENCE = "brain"


def _mint(key: OKPKey, alg: str = "EdDSA", **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": "42",
        "aud": AUDIENCE,
        "iss": ISSUER,
        "iat": now,
        "exp": now + 600,
        "scope": "brain:read brain:write",
    }
    claims.update(overrides)
    header = {"alg": alg, "kid": key.thumbprint()}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SecurityWarning)
        return jwt.encode(header, claims, key, algorithms=[alg])


async def _never_revoked(sub, client_id, iat):
    return False


async def _always_revoked(sub, client_id, iat):
    return True


def _verify(key: OKPKey, token: str, is_revoked=_never_revoked, verify_key=None):
    verifier = DjangoJWTVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        key_set=KeySet([verify_key or key]),
        is_revoked=is_revoked,
    )
    return asyncio.run(verifier.verify_token(token))


@pytest.fixture
def key() -> OKPKey:
    return OKPKey.generate_key("Ed25519")


def test_valid_token_returns_access_token(key):
    token = _verify(key, _mint(key))
    assert token is not None
    assert token.subject == "42"
    assert token.scopes == ["brain:read", "brain:write"]
    assert token.claims["iss"] == ISSUER


def test_wrong_audience_rejected(key):
    assert _verify(key, _mint(key, aud="other")) is None


def test_wrong_issuer_rejected(key):
    assert _verify(key, _mint(key, iss="http://evil")) is None


def test_expired_token_rejected(key):
    now = int(time.time())
    assert _verify(key, _mint(key, iat=now - 1200, exp=now - 600)) is None


def test_bad_signature_rejected(key):
    other = OKPKey.generate_key("Ed25519")
    # Signed by `key`, verified against `other`'s public key -> reject.
    assert _verify(key, _mint(key), verify_key=other) is None


def test_missing_sub_rejected(key):
    assert _verify(key, _mint(key, sub="")) is None


def test_revoked_token_rejected_when_client_id_present(key):
    token = _mint(key, client_id="cli-1")
    assert _verify(key, token, is_revoked=_always_revoked) is None


def test_revocation_skipped_without_client_id(key):
    # No client_id claim -> revocation never consulted (operator-minted tokens).
    token = _mint(key)  # no client_id
    assert _verify(key, token, is_revoked=_always_revoked) is not None
