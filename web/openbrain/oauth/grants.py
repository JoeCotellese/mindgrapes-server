"""OAuth 2.1 grants (Slice 3.2, #74).

Authorization-code grant (PKCE S256-only) and refresh-token grant with
single-use rotation and family revocation. Both serve **public** clients
(``token_endpoint_auth_method="none"``) — Claude-style clients hold no secret.
"""

import time

from authlib.oauth2.rfc6749.grants import (
    AuthorizationCodeGrant as _AuthorizationCodeGrant,
)
from authlib.oauth2.rfc6749.grants import (
    RefreshTokenGrant as _RefreshTokenGrant,
)
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oauth2.rfc7636.challenge import compare_s256_code_challenge
from django.contrib.auth import get_user_model

from .models import OAuthAuthorizationCode, OAuthToken

User = get_user_model()


class S256CodeChallenge(CodeChallenge):
    """PKCE restricted to S256. ``plain`` is rejected as an unsupported method
    at the authorize endpoint and is never a verifiable method at the token
    endpoint."""

    SUPPORTED_CODE_CHALLENGE_METHOD = ["S256"]
    DEFAULT_CODE_CHALLENGE_METHOD = "S256"
    CODE_CHALLENGE_METHODS = {"S256": compare_s256_code_challenge}


class AuthorizationCodeGrant(_AuthorizationCodeGrant):
    # Public clients authenticate by client_id alone (no secret).
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]

    def save_authorization_code(self, code, request):
        payload = request.payload
        OAuthAuthorizationCode.objects.create(
            code=code,
            client_id=request.client.client_id,
            redirect_uri=payload.redirect_uri or "",
            response_type=payload.response_type or "",
            scope=payload.scope or "",
            code_challenge=payload.data.get("code_challenge"),
            code_challenge_method=payload.data.get("code_challenge_method"),
            user=request.user,
        )

    def query_authorization_code(self, code, client):
        item = OAuthAuthorizationCode.objects.filter(
            code=code, client_id=client.client_id
        ).first()
        if item and not item.is_expired():
            return item
        return None

    def delete_authorization_code(self, authorization_code):
        # Single-use: the code is destroyed on first successful exchange, so a
        # replay finds nothing and fails with invalid_grant.
        authorization_code.delete()

    def authenticate_user(self, authorization_code):
        return User.objects.filter(pk=authorization_code.user_id).first()


class RefreshTokenGrant(_RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]
    # Rotate: every refresh issues a fresh refresh token in the same family.
    INCLUDE_NEW_REFRESH_TOKEN = True

    def authenticate_refresh_token(self, refresh_token):
        token = OAuthToken.objects.filter(refresh_token=refresh_token).first()
        if token is None:
            return None
        if token.refresh_token_revoked_at:
            # Replay of an already-rotated refresh token is the canonical
            # reuse-detection signal: revoke the entire lineage and refuse.
            now = int(time.time())
            OAuthToken.objects.filter(family_id=token.family_id).update(
                refresh_token_revoked_at=now, access_token_revoked_at=now
            )
            return None
        return token

    def authenticate_user(self, refresh_token):
        return User.objects.filter(pk=refresh_token.user_id).first()

    def revoke_old_credential(self, refresh_token):
        now = int(time.time())
        refresh_token.refresh_token_revoked_at = now
        refresh_token.access_token_revoked_at = now
        refresh_token.save(
            update_fields=["refresh_token_revoked_at", "access_token_revoked_at"]
        )
