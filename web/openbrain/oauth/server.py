"""Authlib authorization server wiring (Slice 3.2, #74).

Builds the Django ``AuthorizationServer`` with a JWT access-token generator (so
access tokens are the EdDSA JWTs the MCP resource server validates offline) and
registers the PKCE-gated authorization-code grant plus the rotating
refresh-token grant. The server is built lazily and cached so the access-token
generator dotted path resolves after this module is fully imported.
"""

import uuid

from authlib.integrations.django_oauth2 import AuthorizationServer

from . import jwt as oauth_jwt
from .grants import AuthorizationCodeGrant, RefreshTokenGrant, S256CodeChallenge
from .models import OAuthClient, OAuthToken
from .scopes import DEFAULT_SCOPE


def generate_jwt_access_token(*, client, grant_type, user, scope):
    """Authlib access-token generator → a signed EdDSA JWT.

    Called by Authlib's ``BearerTokenGenerator`` as
    ``fn(client=, grant_type=, user=, scope=)``. An empty granted scope falls
    back to the full household scope so a token always carries a meaningful
    ``scope`` claim. The client is stamped into the token so the MCP resource
    server can key revocation on ``(sub, client_id)`` (Slice 3.3, #75).
    """
    return oauth_jwt.sign_access_token(
        user, scope=scope or DEFAULT_SCOPE, client_id=client.get_client_id()
    )


class OpenBrainAuthorizationServer(AuthorizationServer):
    """Stamps refresh-lineage (``family_id``) and the ``sub`` claim on tokens.

    A fresh ``family_id`` starts a new lineage on the authorization-code grant;
    the refresh-token grant inherits the prior token's ``family_id`` (set on the
    request by ``RefreshTokenGrant``) so a rotated lineage can be revoked whole.
    """

    def save_token(self, token, request):
        client = request.client
        user = request.user
        user_id = user.pk if user else client.user_id
        previous = getattr(request, "refresh_token", None)
        family_id = previous.family_id if previous is not None else uuid.uuid4()
        item = self.token_model(
            client_id=client.client_id,
            user_id=user_id,
            family_id=family_id,
            sub=str(user_id),
            **token,
        )
        item.save()
        return item


_server = None


def get_server() -> OpenBrainAuthorizationServer:
    """The process-wide authorization server (built once, then cached)."""
    global _server
    if _server is None:
        server = OpenBrainAuthorizationServer(OAuthClient, OAuthToken)
        # PKCE S256 is mandatory: public clients with no verifier are rejected,
        # and the S256-only subclass refuses `plain` at authorize and token time.
        server.register_grant(
            AuthorizationCodeGrant, [S256CodeChallenge(required=True)]
        )
        server.register_grant(RefreshTokenGrant)
        _server = server
    return _server
