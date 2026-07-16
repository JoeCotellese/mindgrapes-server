"""Permissive CORS for the OAuth JSON endpoints (Slice 3.2, #74).

Browser-based OAuth clients (the MCP Inspector, SPA connectors) fetch the
discovery doc, run Dynamic Client Registration, and exchange tokens with
cross-origin ``fetch`` calls, so these endpoints must answer CORS preflight and
echo an allow-origin header. ``*`` is safe here because every client is a public
client protected by PKCE — there is no cookie or secret to exfiltrate — and it
matches the CORS the MCP resource server applies to its own metadata.

The browser-navigated ``authorize`` endpoint is a top-level redirect, not a
cross-origin fetch, so it deliberately does not use this.
"""

from functools import wraps

from django.http import HttpResponse

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "authorization, content-type, accept",
    "Access-Control-Max-Age": "600",
}


def cors(view):
    """Answer OPTIONS preflight and stamp CORS headers on the response.

    Wraps the method guards (``require_GET`` / ``require_POST``) so a preflight
    OPTIONS is handled here instead of being rejected with 405.
    """

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
        else:
            response = view(request, *args, **kwargs)
        for header, value in CORS_HEADERS.items():
            response[header] = value
        return response

    return wrapped
