# ABOUTME: Resource-server bearer auth for the Python MCP — validates Django's
# ABOUTME: EdDSA JWTs (joserfc) + revocation watermark, wrapped as a RemoteAuthProvider.
import threading
import time
import warnings

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from fastmcp.server.auth import RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from joserfc import jwt
from joserfc.errors import JoseError, SecurityWarning
from joserfc.jwk import KeySet
from pydantic import AnyHttpUrl

# JWS algorithm identifier — must match openbrain.oauth.jwt.ALG ("EdDSA").
# fastmcp's own JWTVerifier rejects EdDSA (RSA/ECDSA/HMAC only), which is why
# this is a hand-rolled joserfc verifier rather than a JWTVerifier subclass.
ALG = "EdDSA"


def _is_oauth_revoked(sub: str, client_id: str, iat: int) -> bool:
    """True when (sub, client_id) was revoked after the token was issued.

    One OAuthRevocation row kills every token
    for that pair issued before `revoked_after`. Sync ORM; called via threadpool.
    """
    from datetime import UTC, datetime

    from openbrain.oauth.models import OAuthRevocation

    issued_at = datetime.fromtimestamp(iat, tz=UTC)
    return OAuthRevocation.objects.filter(
        user_id=sub, client_id=client_id, revoked_after__gt=issued_at
    ).exists()


class DjangoJWTVerifier(TokenVerifier):
    """Validate Django-issued EdDSA access tokens offline against the JWKS.

    Verifies signature (joserfc), issuer, audience, and expiry, then applies the
    revocation watermark for tokens that carry a client_id (operator-minted
    tokens without one skip the check). Returns None on any
    failure so fastmcp's auth layer emits the RFC 9728 401.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_uri: str | None = None,
        key_set: KeySet | None = None,
        is_revoked=None,
        jwks_ttl: float = 300.0,
        revocation_ttl: float = 15.0,
        http_client: httpx.AsyncClient | None = None,
    ):
        super().__init__()
        self._issuer = issuer
        self._audience = audience
        self._jwks_uri = jwks_uri
        self._static_key_set = key_set
        self._is_revoked = is_revoked or self._default_is_revoked
        self._jwks_ttl = jwks_ttl
        self._revocation_ttl = revocation_ttl
        self._http_client = http_client
        self._cached_key_set: KeySet | None = None
        self._cached_at = 0.0
        self._revocation_cache: dict[str, tuple[bool, float]] = {}
        self._lock = threading.Lock()

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key_set = await self._get_key_set()
            with warnings.catch_warnings():
                # joserfc warns that EdDSA is deprecated (RFC 9864); the signing
                # side silences the same advisory. Keep verification quiet.
                warnings.simplefilter("ignore", SecurityWarning)
                decoded = jwt.decode(token, key_set, algorithms=[ALG])
            claims = decoded.claims
            registry = jwt.JWTClaimsRegistry(
                iss={"essential": True, "value": self._issuer},
                aud={"essential": True, "value": self._audience},
            )
            registry.validate(claims)
        except (JoseError, ValueError, KeyError):
            return None

        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            return None

        client_id = claims.get("client_id")
        iat = claims.get("iat")
        if client_id and isinstance(iat, int):
            if await self._revoked(sub, client_id, iat):
                return None

        scope = claims.get("scope") or ""
        return AccessToken(
            token=token,
            # fastmcp's AccessToken requires a string client_id; operator-minted
            # tokens carry no client_id claim, so fall back to the subject.
            client_id=client_id or sub,
            scopes=scope.split() if scope else [],
            expires_at=claims.get("exp"),
            subject=sub,
            claims=claims,
        )

    async def _revoked(self, sub: str, client_id: str, iat: int) -> bool:
        key = f"{sub} {client_id} {iat}"
        now = time.monotonic()
        with self._lock:
            cached = self._revocation_cache.get(key)
            if cached is not None and cached[1] > now:
                return cached[0]
        revoked = await self._is_revoked(sub, client_id, iat)
        with self._lock:
            self._revocation_cache[key] = (revoked, now + self._revocation_ttl)
        return revoked

    async def _get_key_set(self) -> KeySet:
        if self._static_key_set is not None:
            return self._static_key_set
        now = time.monotonic()
        if (
            self._cached_key_set is not None
            and (now - self._cached_at) < self._jwks_ttl
        ):
            return self._cached_key_set
        data = await self._fetch_jwks()
        key_set = KeySet.import_key_set(data)
        self._cached_key_set = key_set
        self._cached_at = now
        return key_set

    async def _fetch_jwks(self) -> dict:
        if self._http_client is not None:
            resp = await self._http_client.get(self._jwks_uri)
            resp.raise_for_status()
            return resp.json()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(self._jwks_uri)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    async def _default_is_revoked(sub: str, client_id: str, iat: int) -> bool:
        return await sync_to_async(_is_oauth_revoked, thread_sensitive=True)(
            sub, client_id, iat
        )


def _base_url() -> str:
    """The MCP server's public base URL (BRAIN_MCP_URL minus the /mcp path)."""
    mcp_url = settings.BRAIN_MCP_URL.rstrip("/")
    if mcp_url.endswith("/mcp"):
        return mcp_url[: -len("/mcp")]
    return mcp_url


def build_auth() -> RemoteAuthProvider:
    """Wire the verifier into a RemoteAuthProvider (RFC 9728 metadata)."""
    from openbrain.oauth.scopes import SCOPES

    verifier = DjangoJWTVerifier(
        issuer=settings.OAUTH_ISSUER,
        audience=settings.OAUTH_AUDIENCE,
        jwks_uri=settings.OAUTH_JWKS_URL,
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(settings.OAUTH_ISSUER)],
        base_url=_base_url(),
        scopes_supported=list(SCOPES),
    )
