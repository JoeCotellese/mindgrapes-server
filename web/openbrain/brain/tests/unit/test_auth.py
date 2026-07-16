"""Unit tests for brain.auth: viewer predicates, viewer_id, login decorator.

The predicates gate who may see and edit brain rows; the decorator adds
htmx-aware session-expiry handling (HX-Redirect) on top of Django's login
redirect.
"""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory

from openbrain.brain.auth import (
    brain_login_required,
    can_edit_visibility,
    can_viewer_read,
    viewer_id,
)


@pytest.mark.parametrize(
    "viewer,owner,visibility,expected",
    [
        (None, "ada", "private", True),  # null viewer bypasses the filter
        ("7", "7", "private", True),  # owner reads their own private row
        ("7", "9", "shared", True),  # shared rows are readable by anyone
        ("7", "9", "private", False),  # another member's private row is hidden
    ],
)
def test_can_viewer_read(viewer, owner, visibility, expected):
    assert can_viewer_read(viewer, owner, visibility) is expected


@pytest.mark.parametrize(
    "viewer,owner,expected",
    [
        (None, "ada", True),  # null viewer bypass
        ("7", "7", True),  # owner may flip visibility
        ("7", "9", False),  # seeing a shared row does not grant edit rights
    ],
)
def test_can_edit_visibility(viewer, owner, expected):
    assert can_edit_visibility(viewer, owner) is expected


def test_viewer_id_returns_str_of_user_pk():
    request = RequestFactory().get("/")

    class FakeUser:
        pk = 42

    request.user = FakeUser()
    assert viewer_id(request) == "42"


def test_brain_login_required_calls_view_when_authenticated():
    @brain_login_required
    def view(request):
        return HttpResponse("ok")

    request = RequestFactory().get("/")

    class FakeUser:
        is_authenticated = True

    request.user = FakeUser()
    response = view(request)
    assert response.status_code == 200
    assert response.content == b"ok"


def test_brain_login_required_full_request_redirects_to_login():
    @brain_login_required
    def view(request):
        return HttpResponse("ok")

    request = RequestFactory().get("/experience/abc")
    request.user = AnonymousUser()
    response = view(request)
    assert response.status_code == 302
    assert "/accounts/login/" in response.url
    assert "next=/experience/abc" in response.url


def test_brain_login_required_htmx_request_returns_hx_redirect():
    @brain_login_required
    def view(request):
        return HttpResponse("ok")

    request = RequestFactory().get("/experience/abc")
    request.user = AnonymousUser()
    request.htmx = True  # django_htmx sets this; truthy when HX-Request present
    response = view(request)
    assert response.status_code == 204
    assert "/accounts/login/" in response["HX-Redirect"]
    assert "next=/experience/abc" in response["HX-Redirect"]
