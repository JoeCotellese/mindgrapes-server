"""The /connect on-ramp page (Slice 3.4, #76).

A logged-in member finds their brain's address and copy-paste setup steps for
each Claude surface, ending in a real next action — "Check connected apps" —
rather than a server-built /authorize link, which would lack the client's PKCE
params and only render the OAuth error page (issue decision 1). Here we assert
the HTTP seam: auth-gating, the environment-aware address, the per-app blocks,
and the honest closing link.
"""

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = pytest.mark.django_db

User = get_user_model()

CONNECT_URL = "/connect"


def _logged_in(email="member@example.net"):
    user = User.objects.create_user(email=email)
    http = Client()
    http.force_login(user)
    return http, user


def test_connect_requires_login():
    res = Client().get(CONNECT_URL)
    assert res.status_code == 302
    assert "/accounts/login" in res["Location"]


def test_connect_shows_the_brain_address_in_a_readonly_input():
    http, _ = _logged_in()

    res = http.get(CONNECT_URL)

    assert res.status_code == 200
    # The environment-aware address, in a readonly (selectable, not disabled) input.
    assert settings.BRAIN_MCP_URL.encode() in res.content
    assert b"readonly" in res.content
    assert b"disabled" not in res.content


def test_connect_renders_a_block_per_claude_surface_under_how_to_connect():
    http, _ = _logged_in()

    res = http.get(CONNECT_URL)

    assert b"How to connect" in res.content
    for app in (b"Claude Code", b"Claude.ai", b"Claude Desktop"):
        assert app in res.content
    # Claude Code gets a copy-paste command carrying the address.
    assert b"claude mcp add" in res.content
    assert settings.BRAIN_MCP_URL.encode() in res.content


def test_connect_ends_with_check_connected_apps_not_a_server_built_authorize():
    http, _ = _logged_in()

    res = http.get(CONNECT_URL)

    assert b'href="/connect/clients"' in res.content
    # The literal Authorize button was dropped (decision 1) — no /authorize link.
    assert b"/oauth/authorize" not in res.content


def test_connect_is_reachable_from_the_nav_and_active_when_on_it():
    http, _ = _logged_in()

    res = http.get(CONNECT_URL)

    # The onboarding page is reachable from the main nav (#156); the closing
    # link is href="/connect/clients", so match the trailing quote to isolate
    # the nav anchor.
    assert b'href="/connect"' in res.content
    # On /connect, no other nav item matches request.path, so the active-state
    # markup can only belong to the Connect link.
    assert b'aria-current="page"' in res.content


def test_connect_covers_non_claude_clients_and_a_generic_bridge():
    http, _ = _logged_in()

    res = http.get(CONNECT_URL)

    for app in (b"ChatGPT", b"Cursor", b"VS Code"):
        assert app in res.content
    # The generic bridge entry carries a copyable mcp-remote command (#156) that
    # interpolates the live address — covers any client we don't list by name.
    assert b"npx -y mcp-remote" in res.content
    assert settings.BRAIN_MCP_URL.encode() in res.content
