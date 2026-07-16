"""Core URLs: health probe.

The site root is the login-gated Brain dashboard (openbrain.brain.urls, #101).
The walking-skeleton `landing` view + template were retired in Slice D; the
legacy /ui → / redirect lives in config/urls.py.
"""

from django.urls import path

from . import views

urlpatterns = [
    path("healthz", views.health, name="health"),
]
