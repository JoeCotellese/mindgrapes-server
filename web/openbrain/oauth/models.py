"""OAuth 2.1 authorization-server tables (Slice 3.2, #74).

Django is the authorization server: it registers clients (DCR), issues
short-lived PKCE-protected authorization codes, and mints EdDSA JWT access
tokens (signed in ``jwt.py``) plus rotating opaque refresh tokens.

Authlib 1.7.2 ships its ORM model mixins only for SQLAlchemy, so these models
implement the framework-agnostic interfaces in
``authlib.oauth2.rfc6749.models`` (``ClientMixin`` / ``AuthorizationCodeMixin``
/ ``TokenMixin``) directly, mirroring the SQLAlchemy reference fields. These
tables supersede the legacy ``auth.*`` tables (deprecated-not-dropped).
"""

import secrets
import time
import uuid

from authlib.oauth2.rfc6749 import (
    AuthorizationCodeMixin as _AuthorizationCodeMixin,
)
from authlib.oauth2.rfc6749 import (
    ClientMixin,
    list_to_scope,
    scope_to_list,
)
from authlib.oauth2.rfc6749 import (
    TokenMixin as _TokenMixin,
)
from django.conf import settings
from django.db import models


def _now_epoch() -> int:
    """Current unix time as an int. Module-level so migrations can serialize it."""
    return int(time.time())


class OAuthClient(ClientMixin, models.Model):
    """A registered OAuth client. Public clients (Claude) have no secret.

    Metadata defined by RFC 7591 (redirect_uris, grant_types, scope, ...) is
    kept in a single JSON column, exposed via properties so the Authlib
    interface methods read it the same way the SQLAlchemy mixin does.
    """

    client_id = models.CharField(max_length=48, unique=True, db_index=True)
    client_secret = models.CharField(max_length=120, blank=True, default="")
    client_id_issued_at = models.IntegerField(default=0)
    client_secret_expires_at = models.IntegerField(default=0)
    client_metadata = models.JSONField(default=dict)
    # The household member who registered the client, if known. DCR is
    # anonymous, so this is optional.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="oauth_clients",
    )

    def __str__(self):
        return f"{self.client_name or '(unnamed)'} ({self.client_id})"

    def set_client_metadata(self, value):
        self.client_metadata = value

    @property
    def redirect_uris(self):
        return self.client_metadata.get("redirect_uris", [])

    @property
    def token_endpoint_auth_method(self):
        return self.client_metadata.get("token_endpoint_auth_method", "none")

    @property
    def grant_types(self):
        return self.client_metadata.get("grant_types", [])

    @property
    def response_types(self):
        return self.client_metadata.get("response_types", [])

    @property
    def client_name(self):
        return self.client_metadata.get("client_name")

    @property
    def scope(self):
        return self.client_metadata.get("scope", "")

    # ---- ClientMixin interface ----
    def get_client_id(self):
        return self.client_id

    def get_default_redirect_uri(self):
        if self.redirect_uris:
            return self.redirect_uris[0]
        return None

    def get_allowed_scope(self, scope):
        if not scope:
            return ""
        allowed = set(self.scope.split())
        return list_to_scope([s for s in scope_to_list(scope) if s in allowed])

    def check_redirect_uri(self, redirect_uri):
        return redirect_uri in self.redirect_uris

    def check_client_secret(self, client_secret):
        return bool(self.client_secret) and secrets.compare_digest(
            self.client_secret, client_secret
        )

    def check_endpoint_auth_method(self, method, endpoint):
        if endpoint == "token":
            return self.token_endpoint_auth_method == method
        return True

    def check_response_type(self, response_type):
        return response_type in self.response_types

    def check_grant_type(self, grant_type):
        return grant_type in self.grant_types


class OAuthAuthorizationCode(_AuthorizationCodeMixin, models.Model):
    """A single-use, 5-minute authorization code bound to a PKCE challenge."""

    code = models.CharField(max_length=120, unique=True)
    client_id = models.CharField(max_length=48)
    redirect_uri = models.TextField(blank=True, default="")
    response_type = models.TextField(blank=True, default="")
    scope = models.TextField(blank=True, default="")
    auth_time = models.IntegerField(default=_now_epoch)
    # NULL (not "") is deliberate: Authlib's PKCE extension treats a missing
    # method as "default to S256", but an empty-string method has no verifier
    # function and would raise. Mirrors the SQLAlchemy reference columns.
    code_challenge = models.TextField(null=True, blank=True)  # noqa: DJ001
    code_challenge_method = models.CharField(  # noqa: DJ001
        max_length=48, null=True, blank=True
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oauth_codes",
    )

    def __str__(self):
        return f"authorization code {self.code[:6]}… for client {self.client_id}"

    def is_expired(self):
        return self.auth_time + 300 < time.time()

    def get_redirect_uri(self):
        return self.redirect_uri

    def get_scope(self):
        return self.scope


class OAuthToken(_TokenMixin, models.Model):
    """An issued access/refresh token pair.

    The access token is a self-contained EdDSA JWT (validated offline by the
    MCP resource server); it is stored for audit only. The refresh token is an
    opaque, single-use string that rotates. ``family_id`` links a refresh-token
    lineage so that replay of a rotated token can revoke the whole family.
    """

    client_id = models.CharField(max_length=48)
    token_type = models.CharField(max_length=40, default="Bearer")
    # The JWT can exceed 255 chars; store it verbatim for audit, not lookup.
    access_token = models.TextField()
    refresh_token = models.CharField(
        max_length=255, blank=True, default="", db_index=True
    )
    scope = models.TextField(blank=True, default="")
    issued_at = models.IntegerField(default=_now_epoch)
    access_token_revoked_at = models.IntegerField(default=0)
    refresh_token_revoked_at = models.IntegerField(default=0)
    expires_in = models.IntegerField(default=0)
    # Refresh-token lineage for rotation + family revocation.
    family_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    # Subject claim baked into the JWT (the Django user pk as a string).
    sub = models.CharField(max_length=255, blank=True, default="")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oauth_tokens",
    )

    def __str__(self):
        return f"token for sub={self.sub} client={self.client_id}"

    # ---- TokenMixin interface ----
    def check_client(self, client):
        return self.client_id == client.get_client_id()

    def get_scope(self):
        return self.scope

    def get_expires_in(self):
        return self.expires_in

    def is_revoked(self):
        return bool(self.access_token_revoked_at or self.refresh_token_revoked_at)

    def is_expired(self):
        if not self.expires_in:
            return False
        return self.issued_at + self.expires_in < time.time()

    def get_user(self):
        return self.user

    def get_client(self):
        return OAuthClient.objects.filter(client_id=self.client_id).first()


class OAuthRevocation(models.Model):
    """A revocation watermark for one ``(user, client)`` pair (Slice 3.3, #75).

    Rather than enumerate individual token jtis, revoking a client writes a
    single ``revoked_after`` timestamp here. The MCP resource server treats an
    access token as dead when its ``iat`` predates this watermark for the same
    ``(sub, client_id)``, so one row kills every current token for the client at
    once. A later re-authorization mints a token with a fresh ``iat`` after the
    watermark, so it works again — revocation is "irreversible until re-auth".

    ``db_table`` is pinned because this table is a cross-service contract: the
    MCP resource server reads it by name (``public.oauth_revocation``).
    """

    client_id = models.CharField(max_length=48)
    # Tokens issued strictly before this instant are revoked. Microsecond
    # precision (timezone.now) keeps the comparison decisive against a token
    # whose integer ``iat`` floors to the same wall-clock second.
    revoked_after = models.DateTimeField()
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oauth_revocations",
    )

    class Meta:
        db_table = "oauth_revocation"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "client_id"],
                name="oauth_revocation_user_client_uniq",
            )
        ]

    def __str__(self):
        return f"revocation for user={self.user_id} client={self.client_id}"
