"""Viewer/owner authorization for the Brain UI.

The predicates keep the web read/write gates in agreement with the MCP service
on who may see and edit a row. The web viewer id is
str(request.user.pk), which equals the OAuth `sub` the authorization server
mints (oauth/jwt.py) and that captures stamp as brain.experiences.owner — so the
same person resolves to the same owner string across both surfaces.

Unlike the MCP service there is no null-viewer operator bypass in normal web
use: every web request is an authenticated member. The predicates still accept a
None viewer (mirroring the SQL filter) for completeness and direct unit testing.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse


def can_viewer_read(viewer: str | None, owner: str | None, visibility: str) -> bool:
    """Mirror the SQL filter: null viewer, own row, or a shared row."""
    return viewer is None or owner == viewer or visibility == "shared"


def can_edit_visibility(viewer: str | None, owner: str | None) -> bool:
    """Only the owner (or a null viewer) may flip visibility; seeing != editing."""
    return viewer is None or owner == viewer


def viewer_id(request) -> str:
    """The brain viewer id for the logged-in member — matches the OAuth sub."""
    return str(request.user.pk)


def brain_login_required(view):
    """Gate a brain view behind an authenticated session.

    Full-page requests get the usual 302 to the login page with ?next. htmx
    requests get a 204 + HX-Redirect so htmx performs a client-side redirect
    instead of swapping a login form into a partial target.
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view(request, *args, **kwargs)
        redirect = redirect_to_login(request.get_full_path())
        if getattr(request, "htmx", False):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = redirect.url
            return response
        return redirect

    return wrapper
