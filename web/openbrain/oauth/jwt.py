"""Ed25519 access-token signing + JWKS for the OAuth authorization server.

Slice 3.1 (#73). Django is the OAuth 2.1 authorization server: it signs short
(10-minute) EdDSA access tokens and publishes the public key as a JWKS so the
MCP *resource server* (openbrain.mcp.auth) can validate them offline. A token
can also be minted directly via `sign_access_token` (management command / tests).

joserfc supplies the JOSE primitives (the maintained successor to authlib.jose).
We sign with the JWS ``EdDSA`` algorithm identifier — required for interop with
the resource-server verifier and current MCP clients — and silence joserfc's
RFC 9864 advisory that prefers the newer ``Ed25519`` name.
"""

import time
import warnings

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from joserfc import jwt
from joserfc.errors import SecurityWarning
from joserfc.jwk import OKPKey

# JWS algorithm identifier. Must stay "EdDSA" to match the resource-server
# verifier (openbrain.mcp.auth.ALG).
ALG = "EdDSA"


def _load_private_key() -> OKPKey:
    """Load the Ed25519 signing key from settings.

    The PEM may arrive as a single line with literal ``\\n`` separators
    (django-environ does not decode them), so normalize before parsing.
    """
    pem = settings.OAUTH_JWT_PRIVATE_KEY
    if not pem:
        raise ImproperlyConfigured(
            "OAUTH_JWT_PRIVATE_KEY is not set; generate one with "
            "`manage.py gen_jwt_key`."
        )
    return OKPKey.import_key(pem.replace("\\n", "\n"))


def kid() -> str:
    """RFC 7638 JWK thumbprint of the signing key — stable across restarts."""
    return _load_private_key().thumbprint()


def public_jwk() -> dict:
    """The public half as a signed-use JWK, tagged with its thumbprint kid."""
    key = _load_private_key()
    return {
        **key.as_dict(private=False),
        "kid": key.thumbprint(),
        "use": "sig",
        "alg": ALG,
    }


def jwks() -> dict:
    """RFC 7517 JWK Set. List-shaped so rotation can publish multiple kids."""
    return {"keys": [public_jwk()]}


def sign_access_token(
    user,
    *,
    scope: str = "brain:read brain:write",
    ttl: int | None = None,
    client_id: str | None = None,
) -> str:
    """Mint a signed access token for ``user``.

    Claims: ``sub`` = the Django user id, ``aud`` = the brain audience, ``iss``,
    ``iat``, ``exp`` (default 10-minute TTL), and ``scope``. The header carries
    the signing key's ``kid`` so the resource server can select it from the JWKS.

    ``client_id``, when given, is added as a claim so the MCP resource server
    can key revocation on ``(sub, client_id)`` (Slice 3.3, #75). The mint path
    used by the management command / tests passes none, and the claim is omitted.
    """
    key = _load_private_key()
    now = int(time.time())
    ttl = settings.OAUTH_ACCESS_TTL_SECONDS if ttl is None else ttl
    claims = {
        "sub": str(user.pk),
        "aud": settings.OAUTH_AUDIENCE,
        "iss": settings.OAUTH_ISSUER,
        "iat": now,
        "exp": now + ttl,
        "scope": scope,
    }
    if client_id:
        claims["client_id"] = client_id
    header = {"alg": ALG, "kid": key.thumbprint()}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SecurityWarning)
        return jwt.encode(header, claims, key, algorithms=[ALG])
