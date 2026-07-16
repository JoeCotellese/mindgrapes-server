"""End-to-end passkey ceremony with a virtual authenticator.

Drives a real headless-Chromium WebAuthn ceremony through the live app:
enroll via a single-use link, register a discoverable passkey, see recovery
codes once, sign out, sign back in with the passkey, and register a second
passkey. This is the only test that exercises the actual browser ceremony; the
faster boundary tests cover tokens, settings, views, and the command.

Run it with the dev browser installed:
    cd web && uv run pytest -m e2e        # after `uv run playwright install chromium`
"""

import pytest
from allauth.mfa.models import Authenticator
from django.contrib.auth import get_user_model
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from openbrain.accounts.models import EnrollmentToken

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

User = get_user_model()


def _webauthn_count(user):
    return Authenticator.objects.filter(
        user=user, type=Authenticator.Type.WEBAUTHN
    ).count()


def test_enroll_login_logout_login_and_second_passkey(
    live_server, page, virtual_authenticators
):
    _, add_authenticator = virtual_authenticators
    user = User.objects.create_user(email="member@example.net")
    _, raw = EnrollmentToken.objects.create_for(user)

    # 1. Open the single-use enrollment link -> confirm page (GET consumes
    #    nothing, #69), then Continue spends the token and lands on passkey
    #    registration.
    page.goto(f"{live_server.url}/accounts/enroll/{raw}/")
    page.click("button[type=submit]")
    page.wait_for_url("**/accounts/passkeys/add/")

    # 2. Register the first passkey. On the first authenticator, allauth
    #    generates recovery codes and redirects to show them once.
    page.click("#mfa_webauthn_add")
    page.wait_for_url("**/accounts/2fa/recovery-codes/")
    assert "recovery codes available" in page.content().lower()
    assert _webauthn_count(user) == 1

    # 3. Sign out (allauth confirmation form).
    page.goto(f"{live_server.url}/accounts/logout/")
    page.click("button[type=submit]")
    page.wait_for_url(f"{live_server.url}/")

    # 4. Sign back in with the passkey — no password. The page auto-fires the
    #    ceremony on load; click the button only if it hasn't navigated yet.
    page.goto(f"{live_server.url}/accounts/login/")
    try:
        page.click("#passkey_login", timeout=3000)
    except PlaywrightTimeoutError:
        pass  # auto-trigger already started the login
    page.wait_for_url(f"{live_server.url}/")
    assert "member@example.net" in page.content()

    # 5. Register a second passkey on a second device.
    add_authenticator()
    page.goto(f"{live_server.url}/accounts/passkeys/add/")
    page.click("#mfa_webauthn_add")
    page.wait_for_url("**/accounts/2fa/**")
    assert _webauthn_count(user) == 2
