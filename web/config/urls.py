"""Root URL configuration.

Mostly human-facing paths. The Caddy edge routes /mcp* and the RS metadata to
the Python MCP service (run_mcp); Django additionally owns the OAuth
authorization server surface (Slice 3): the JWKS at /oauth/jwks.json (the MCP
resource server fetches it internally) and the RFC 8414 discovery document.
"""

from django.contrib import admin
from django.urls import include, path, re_path

from openbrain.core import views as core_views
from openbrain.oauth import views as oauth_views

urlpatterns = [
    # The Brain UI owns the site root: `/` is the login-gated dashboard (#101).
    path("", include("openbrain.brain.urls")),
    # The legacy /ui surface (#101 Slice D) is retired; any /ui* path 302s to `/`.
    re_path(r"^ui(?:/.*)?$", core_views.ui_legacy_redirect, name="ui-legacy-redirect"),
    path("", include("openbrain.core.urls")),
    path("admin/", admin.site.urls),
    # OAuth authorization server (Slice 3, #73): JWKS + AS discovery.
    path("", include("openbrain.oauth.urls")),
    path(
        ".well-known/oauth-authorization-server",
        oauth_views.as_discovery,
        name="oauth-as-metadata",
    ),
    # Our passkey-enrollment routes take precedence over allauth's at /accounts/.
    path("accounts/", include("openbrain.accounts.urls")),
    path("accounts/", include("allauth.urls")),
]
