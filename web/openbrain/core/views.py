"""Health + legacy-redirect views.

The walking-skeleton landing page is retired with the legacy /ui surface (#101
Slice D); the site root is now the login-gated Brain dashboard.
"""

from django.http import HttpResponse
from django.shortcuts import redirect


def health(request):
    """Liveness probe used by the container healthcheck and integration tests."""
    return HttpResponse("ok", content_type="text/plain")


def ui_legacy_redirect(request):
    """The legacy /ui surface (#101 Slice D) is retired; bounce to the dashboard."""
    return redirect("brain-dashboard")
