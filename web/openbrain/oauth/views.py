"""OAuth 2.1 authorization-server endpoints (Slice 3, #73 + #74).

Public metadata (#73): the JWKS for offline access-token validation and the
RFC 8414 discovery document.

Browser flow (#74): ``authorize`` (passkey-gated consent), ``token`` (PKCE
authorization-code + refresh-token grants), and ``register`` (RFC 7591 Dynamic
Client Registration). The heavy lifting lives in Authlib; these views are the
thin HTTP seam plus the consent UI.
"""

import json
import secrets
import time
from urllib.parse import urlsplit

from authlib.oauth2 import OAuth2Error
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import jwt
from .cors import cors
from .models import OAuthClient, OAuthToken
from .scopes import DEFAULT_SCOPE, SCOPES, describe
from .server import get_server
from .services import list_active_grants, revoke_client

# Dynamic Client Registration is open (Claude relies on it) but household-
# constrained: a registrant may only ever be a public client doing the
# authorization-code + refresh flow, and may declare at most this many
# redirect URIs.
MAX_REDIRECT_URIS = 5


@cors
@require_GET
def jwks_json(request):
    """RFC 7517 JWK Set with the Ed25519 public signing key(s)."""
    return JsonResponse(jwt.jwks())


@cors
@require_GET
def as_discovery(request):
    """RFC 8414 authorization-server metadata."""
    issuer = settings.OAUTH_ISSUER.rstrip("/")
    return JsonResponse(
        {
            "issuer": issuer,
            "jwks_uri": f"{issuer}/oauth/jwks.json",
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "registration_endpoint": f"{issuer}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(SCOPES.keys()),
        }
    )


@login_required
def authorize(request):
    """Consent endpoint, gated by the Slice 1 passkey session.

    ``GET`` validates the request and renders the consent screen (anonymous
    users are bounced to login by ``@login_required``). ``POST`` approves
    (mint code → 302 to ``redirect_uri``) or denies (302 with
    ``error=access_denied``).
    """
    server = get_server()

    if request.method == "GET":
        try:
            grant = server.get_consent_grant(request, end_user=request.user)
        except OAuth2Error as error:
            return _error_page(request, error)
        client = grant.client
        requested = grant.request.payload.scope or client.scope or DEFAULT_SCOPE
        redirect_uri = grant.redirect_uri or client.get_default_redirect_uri() or ""
        return render(
            request,
            "oauth/consent.html",
            {
                "client_name": client.client_name or "An unnamed application",
                "redirect_host": urlsplit(redirect_uri).netloc or redirect_uri,
                "scope_sentences": describe(requested),
            },
        )

    grant_user = request.user if request.POST.get("action") == "allow" else None
    try:
        oauth_request = server.create_oauth2_request(request)
        grant = server.get_authorization_grant(oauth_request)
        return server.create_authorization_response(
            request=oauth_request, grant_user=grant_user, grant=grant
        )
    except OAuth2Error as error:
        return _error_page(request, error)


@cors
@csrf_exempt
@require_POST
def token(request):
    """RFC 6749 token endpoint (authorization_code + refresh_token grants)."""
    return get_server().create_token_response(request)


@cors
@csrf_exempt
@require_POST
def register(request):
    """RFC 7591 Dynamic Client Registration — open but household-constrained."""
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return _dcr_error("invalid_client_metadata", "request body must be JSON")
    if not isinstance(data, dict):
        return _dcr_error(
            "invalid_client_metadata", "request body must be a JSON object"
        )

    redirect_uris = data.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _dcr_error("invalid_redirect_uri", "redirect_uris is required")
    if len(redirect_uris) > MAX_REDIRECT_URIS:
        return _dcr_error(
            "invalid_redirect_uri", f"at most {MAX_REDIRECT_URIS} redirect_uris allowed"
        )
    for uri in redirect_uris:
        if not _valid_redirect_uri(uri):
            return _dcr_error("invalid_redirect_uri", f"invalid redirect_uri: {uri!r}")

    name = data.get("client_name")
    metadata = {
        "client_name": name if isinstance(name, str) else None,
        "redirect_uris": redirect_uris,
        # Constrained: ignore whatever the client asked for — public client,
        # authorization-code + refresh only.
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": DEFAULT_SCOPE,
    }
    client = OAuthClient(
        client_id=secrets.token_urlsafe(24),
        client_id_issued_at=int(time.time()),
    )
    client.set_client_metadata(metadata)
    client.save()

    body = {
        "client_id": client.client_id,
        "client_id_issued_at": client.client_id_issued_at,
        "redirect_uris": redirect_uris,
        "grant_types": metadata["grant_types"],
        "response_types": metadata["response_types"],
        "token_endpoint_auth_method": "none",
        "scope": DEFAULT_SCOPE,
    }
    if metadata["client_name"]:
        body["client_name"] = metadata["client_name"]
    return JsonResponse(body, status=201)


@login_required
@require_GET
def connect(request):
    """The human on-ramp: the brain's address + per-app setup steps (#76).

    Server-rendered and fully usable without JS — copy buttons are progressive
    enhancement. It closes with a link to the connected-clients screen (the
    honest post-authorization destination) rather than a server-built
    /authorize link, which would lack the client's PKCE params and only render
    the OAuth error page: no seeded first-party client_id exists, every client
    registers itself via DCR.
    """
    mcp_url = settings.BRAIN_MCP_URL
    connector_steps = [
        "Open Settings, then Connectors.",
        "Choose Add custom connector.",
        "Paste your brain's address (above) as the server URL.",
        "Authorize with your passkey when prompted.",
    ]
    connection_steps = [
        {
            "app": "Claude Code",
            "command": f"claude mcp add --transport http mind-grapes {mcp_url}",
            "steps": [
                "Run this in your terminal — Claude Code opens your browser to "
                "authorize with your passkey.",
            ],
        },
        {"app": "Claude.ai", "command": None, "steps": connector_steps},
        {"app": "Claude Desktop", "command": None, "steps": connector_steps},
        {
            "app": "ChatGPT",
            "command": None,
            "steps": [
                "Open Settings, then Connectors, then Advanced, and turn on "
                "Developer mode (needs a paid ChatGPT plan).",
                "Back in Connectors, choose Create to add a custom connector.",
                "Paste your brain's address (above) as the server URL.",
                "Authorize with your passkey when prompted.",
            ],
        },
        {
            "app": "Cursor",
            "command": None,
            "steps": [
                "Open Settings, then Features, then MCP, and choose Add New "
                "MCP Server.",
                "Pick the streamable-http transport and paste your brain's "
                "address (above) as the URL.",
                "Save, then authorize with your passkey when prompted.",
            ],
        },
        {
            "app": "VS Code (Copilot)",
            "command": None,
            "steps": [
                "Open the Command Palette and run MCP: Add Server, then choose HTTP.",
                "Paste your brain's address (above), name the server, and "
                "pick Global or Workspace.",
                "Authorize with your passkey when prompted.",
            ],
        },
        {
            "app": "Any other MCP client (bridge)",
            "command": f"npx -y mcp-remote {mcp_url}",
            "steps": [
                "For tools that can only reach a local (stdio) MCP server, "
                "this command bridges stdio to your remote brain and handles "
                "passkey authorization in your browser.",
                "Add it the way your client adds a local MCP command — works "
                "for AnythingLLM, Windsurf, Zed, and the like.",
            ],
        },
    ]
    return render(
        request,
        "oauth/connect.html",
        {"mcp_url": mcp_url, "connection_steps": connection_steps},
    )


@login_required
@require_GET
def client_list(request):
    """The connected-clients screen. HTMX requests get the list partial alone
    (so a Cancel can re-render it in place); a normal request gets the page."""
    context = {"grants": list_active_grants(request.user)}
    template = "oauth/_client_list.html" if request.htmx else "oauth/clients.html"
    return render(request, template, context)


@login_required
@require_GET
def revoke_confirm(request, client_id):
    """Confirm step for a self-revoke. HTMX swaps the row into a confirm state;
    without JS we land a standalone confirm page (the no-JS fallback)."""
    grant = _grant_or_404(request.user, client_id)
    template = "oauth/_client_row.html" if request.htmx else "oauth/revoke_confirm.html"
    return render(request, template, {"grant": grant, "confirming": True})


@login_required
@require_POST
def revoke_do(request, client_id):
    """Perform the revoke. Irreversible: the client's tokens die and its refresh
    family can't renew (the MCP resource server enforces the watermark on the
    next call)."""
    name = _client_display_name(request.user, client_id)
    revoke_client(request.user, client_id)
    if request.htmx:
        return render(request, "oauth/_client_revoked.html", {"name": name})
    return redirect("oauth:clients")


def _grant_or_404(user, client_id):
    """The user's own active grant for a client, or 404 — a user can only ever
    see and revoke their own connections."""
    for grant in list_active_grants(user):
        if grant.client_id == client_id:
            return grant
    raise Http404("no such connected client")


def _client_display_name(user, client_id) -> str:
    # Scope the name to the user's own tokens so a crafted POST for a client the
    # user never connected can't echo back another household member's client name.
    if not OAuthToken.objects.filter(user=user, client_id=client_id).exists():
        return "the application"
    client = OAuthClient.objects.filter(client_id=client_id).first()
    return (client.client_name if client else None) or "the application"


def _valid_redirect_uri(uri) -> bool:
    """https anywhere, http on loopback, or a private-use scheme with no
    authority; no wildcards, userinfo, or IDN.

    Applied to every redirect_uri a client registers via Dynamic Client
    Registration, so a hostile registration can't claim a dangerous callback.

    The private-use case is the RFC 8252 §7.1 native-app callback (the iOS
    client's only viable shape). Two things keep it safe: the scheme must be
    reverse-DNS *shaped* — two or more non-empty dot-separated labels — so a
    hijackable bare word like ``javascript`` or ``file`` can't register, and
    neither can a one-character dodge like ``javascript.``; and there must be
    no authority at all, so a private-use registration can't smuggle in a host.

    Shape is all this can check. Nothing here proves the registrant *owns* the
    scheme — RFC 8252 can't either, since scheme ownership is arbitrated by the
    OS that dispatches the callback, not by the authorization server.
    """
    if not isinstance(uri, str) or not uri or any(c.isspace() for c in uri):
        return False
    if "*" in uri or not uri.isascii():
        return False
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.username or parts.password:
        return False
    if parts.scheme == "https" and parts.hostname:
        return True
    if parts.scheme == "http":
        return parts.hostname in {"localhost", "127.0.0.1", "::1"}
    labels = parts.scheme.split(".")
    return len(labels) > 1 and all(labels) and not parts.netloc


def _dcr_error(code: str, description: str) -> JsonResponse:
    return JsonResponse({"error": code, "error_description": description}, status=400)


def _error_page(request, error: OAuth2Error):
    """Render a terminal, non-redirecting error (phishing-safe for bad clients)."""
    return render(
        request,
        "oauth/error.html",
        {"error": error.error, "description": error.get_error_description()},
        status=400,
    )
