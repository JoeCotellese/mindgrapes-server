# ABOUTME: Locks production settings that keep the container healthcheck reachable.
# ABOUTME: Guards against SSL-redirect and ALLOWED_HOSTS hardening breaking /healthz.
"""The compose healthcheck hits http://127.0.0.1:8000/healthz directly — no Caddy
hop, so no X-Forwarded-Proto and a loopback Host. Two production hardening
settings would otherwise break that probe: SECURE_SSL_REDIRECT (301 -> https on a
plaintext port) and a restrictive ALLOWED_HOSTS (400 DisallowedHost for the
loopback host). These tests import the real production module and lock in the
exemptions that keep the probe green.
"""

import importlib
import re

import pytest


@pytest.fixture
def production_settings(monkeypatch):
    # production.py reads these with no default; set them so the module imports
    # outside a real deployment. ALLOWED_HOSTS is an *external* host on purpose,
    # so the loopback-append behavior is observable.
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-real")
    monkeypatch.setenv("POSTGRES_DB", "openbrain")
    monkeypatch.setenv("POSTGRES_USER", "openbrain")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
    monkeypatch.setenv("ALLOWED_HOSTS", "brain.example.net")

    import config.settings.production as prod

    return importlib.reload(prod)


def test_loopback_host_always_allowed(production_settings):
    # External host comes from the env var; loopback is appended so the internal
    # healthcheck (Host: 127.0.0.1) is never rejected by ALLOWED_HOSTS.
    assert "brain.example.net" in production_settings.ALLOWED_HOSTS
    assert "127.0.0.1" in production_settings.ALLOWED_HOSTS
    assert "localhost" in production_settings.ALLOWED_HOSTS


def test_allowed_hosts_deduped(monkeypatch):
    # When the operator already lists loopback (the .env.example value does),
    # the append must not produce duplicates.
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-real")
    monkeypatch.setenv("POSTGRES_DB", "openbrain")
    monkeypatch.setenv("POSTGRES_USER", "openbrain")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
    monkeypatch.setenv("ALLOWED_HOSTS", "brain.example.net,127.0.0.1,localhost")

    import config.settings.production as prod

    prod = importlib.reload(prod)
    assert prod.ALLOWED_HOSTS.count("127.0.0.1") == 1
    assert prod.ALLOWED_HOSTS.count("localhost") == 1


@pytest.mark.parametrize("bad_key", ["", "django-insecure-CHANGE-ME-before-prod"])
def test_placeholder_secret_key_refuses_to_boot(monkeypatch, bad_key):
    # The .env.example key is public on GitHub; production must fail loudly
    # rather than serve with it (or with no key at all).
    from django.core.exceptions import ImproperlyConfigured

    monkeypatch.setenv("SECRET_KEY", bad_key)
    monkeypatch.setenv("POSTGRES_DB", "openbrain")
    monkeypatch.setenv("POSTGRES_USER", "openbrain")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
    monkeypatch.setenv("ALLOWED_HOSTS", "brain.example.net")

    import config.settings.production as prod

    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
        importlib.reload(prod)
    # Leave the module usable for later tests.
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-real")
    importlib.reload(prod)


def test_healthz_exempt_from_ssl_redirect(production_settings):
    assert any(
        re.search(pattern, "healthz")
        for pattern in production_settings.SECURE_REDIRECT_EXEMPT
    )


def test_ssl_redirect_on_by_default(production_settings):
    # The exemption is targeted; real traffic is still forced to https.
    assert production_settings.SECURE_SSL_REDIRECT is True


@pytest.mark.django_db
def test_healthz_not_redirected_under_ssl_redirect(client):
    # Behavioral: with the production redirect + exemption applied to the live
    # middleware, the plain-http /healthz probe returns 200, not a 301.
    from django.test import override_settings

    with override_settings(
        SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^healthz$"]
    ):
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.content == b"ok"


@pytest.mark.django_db
def test_non_exempt_path_is_redirected(client):
    # Control: a non-exempt path under the same settings IS forced to https,
    # proving the exemption is scoped to /healthz and not a blanket opt-out.
    from django.test import override_settings

    with override_settings(
        SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^healthz$"]
    ):
        resp = client.get("/")
    assert resp.status_code == 301
    assert resp["Location"].startswith("https://")
