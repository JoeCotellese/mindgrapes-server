"""Core URLs: health probe.

The site root is the login-gated Brain dashboard (openbrain.brain.urls, #101).
The walking-skeleton `landing` view + template were retired in Slice D; the
legacy /ui → / redirect lives in config/urls.py.
"""

from django.urls import path

from . import views

urlpatterns = [
    path("healthz", views.health, name="health"),
    # Bearer-authed bookmark endpoint for the Mind Grapes browser extension (#35).
    path("capture", views.capture_api, name="capture"),
    # Bearer-authed multipart photo intake for the app (#42) — same auth as
    # /capture, same write engine as the MCP capture_image tool.
    path("capture/image", views.capture_image_api, name="capture-image"),
]
