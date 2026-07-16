"""OAuth authorization-server URLs (Slice 3, #73 + #74).

The JWKS lives at /oauth/jwks.json; the RFC 8414 discovery document is mounted
at the root .well-known path in config/urls.py. The browser flow
(authorize/token/register) lands here in #74.
"""

from django.urls import path

from . import views

app_name = "oauth"

urlpatterns = [
    path("oauth/jwks.json", views.jwks_json, name="jwks"),
    path("oauth/authorize", views.authorize, name="authorize"),
    path("oauth/token", views.token, name="token"),
    path("oauth/register", views.register, name="register"),
    # The human on-ramp: brain address + per-app setup steps (Slice 3.4, #76).
    path("connect", views.connect, name="connect"),
    # Connected-clients screen + self-revoke (Slice 3.3, #75).
    path("connect/clients", views.client_list, name="clients"),
    path(
        "connect/clients/<str:client_id>/revoke/confirm",
        views.revoke_confirm,
        name="revoke_confirm",
    ),
    path(
        "connect/clients/<str:client_id>/revoke",
        views.revoke_do,
        name="revoke_do",
    ),
]
