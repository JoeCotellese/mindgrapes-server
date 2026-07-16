"""End-to-end checks against the running dev stack (the Caddy path split).

Requires the dev stack up:
    docker compose -f docker-compose.dev.yml up
Run with:
    cd web && uv run pytest -m integration
Override the base URL with OPENBRAIN_DEV_URL (default http://localhost:8080).
"""

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE = os.environ.get("OPENBRAIN_DEV_URL", "http://localhost:8080")


def test_django_root_is_login_gated_via_caddy():
    # The site root is the login-gated Brain dashboard (#101). Unauthenticated,
    # Caddy forwards to Django which 302s to the allauth login page.
    resp = httpx.get(f"{BASE}/", timeout=10)
    assert resp.status_code == 302
    assert "/accounts/login/" in resp.headers["location"]


def test_legacy_ui_redirects_via_caddy():
    # The retired legacy /ui surface (#101 Slice D) 302s to the dashboard root.
    resp = httpx.get(f"{BASE}/ui", timeout=10)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_django_health_served_via_caddy():
    resp = httpx.get(f"{BASE}/healthz", timeout=10)
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_mcp_path_reaches_mcp_not_django():
    # Caddy routes /mcp* to the MCP service (run_mcp). Unauthenticated, that
    # server answers 401 with an RFC 9728 WWW-Authenticate challenge — proof
    # the request reached the MCP service and was NOT handled by Django
    # (which has no /mcp route -> 404).
    resp = httpx.get(f"{BASE}/mcp", timeout=10)
    assert resp.status_code == 401
    assert "www-authenticate" in resp.headers
