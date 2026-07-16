# ABOUTME: Tests that the retired legacy /ui surface (#101 Slice D) redirects to the
# ABOUTME: Django dashboard root, so old bookmarks and deep links still land somewhere.

"""Legacy /ui redirect.

The legacy /ui SPA is retired in Slice D; any /ui* path 302s to `/` (the
login-gated Brain dashboard). The redirect itself is not login-gated — an
unauthenticated hit becomes /ui → / → /accounts/login/?next=/.
"""

import pytest

pytestmark = pytest.mark.django_db


def test_ui_root_redirects_to_dashboard(client):
    resp = client.get("/ui")
    assert resp.status_code == 302
    assert resp["Location"] == "/"


def test_ui_trailing_slash_redirects_to_dashboard(client):
    resp = client.get("/ui/")
    assert resp.status_code == 302
    assert resp["Location"] == "/"


def test_ui_deep_link_redirects_to_dashboard(client):
    resp = client.get("/ui/experience/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 302
    assert resp["Location"] == "/"
