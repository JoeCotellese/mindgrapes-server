"""allauth is installed and mounted, but no custom flows are wired (Slice 0).

These lock the auth scaffolding so later slices build on a known baseline.
"""

from django.conf import settings
from django.urls import reverse


def test_custom_user_model_is_active():
    assert settings.AUTH_USER_MODEL == "accounts.User"


def test_allauth_is_installed():
    assert "allauth" in settings.INSTALLED_APPS
    assert "allauth.account" in settings.INSTALLED_APPS


def test_allauth_middleware_present():
    assert "allauth.account.middleware.AccountMiddleware" in settings.MIDDLEWARE


def test_allauth_backend_present():
    assert (
        "allauth.account.auth_backends.AuthenticationBackend"
        in settings.AUTHENTICATION_BACKENDS
    )


def test_login_is_email_based():
    assert settings.ACCOUNT_LOGIN_METHODS == {"email"}


def test_allauth_urls_mounted():
    # allauth.urls is included under /accounts/; its login view reverses.
    assert reverse("account_login") == "/accounts/login/"


def test_no_password_validators_passwordless_target():
    assert settings.AUTH_PASSWORD_VALIDATORS == []


def test_brain_mcp_url_has_dev_default():
    # The /connect page shows this as the brain's address; prod overrides it via
    # the BRAIN_MCP_URL env var (the public https /mcp endpoint).
    assert settings.BRAIN_MCP_URL == "http://localhost:8080/mcp"
