"""Passkey registration page + login entry point render correctly.

The registration page must begin a *passwordless* ceremony (resident key +
user verification required) so the resulting passkey is discoverable and can be
found by "Sign in with a passkey". The login page must offer that passkey entry
point. The full browser ceremony is covered by the Playwright e2e test.
"""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(email="member@example.net")


def test_passkey_add_page_begins_passwordless_registration(client, user):
    client.force_login(user)
    resp = client.get(reverse("accounts:passkey_add"))
    assert resp.status_code == 200
    selection = resp.context["js_data"]["creation_options"]["publicKey"][
        "authenticatorSelection"
    ]
    assert selection["residentKey"] == "required"
    assert selection["userVerification"] == "required"


def test_login_page_offers_passkey_signin(client):
    resp = client.get(reverse("account_login"))
    assert resp.status_code == 200
    assert b"Sign in with a passkey" in resp.content


def test_login_page_has_no_password_field(client):
    # DoD: no password anywhere in the flow. allauth drops the password field
    # because ACCOUNT_SIGNUP_FIELDS has no password1.
    resp = client.get(reverse("account_login"))
    assert b'type="password"' not in resp.content
    assert b'name="password"' not in resp.content


def test_login_page_is_passkey_only_no_email_form(client):
    # The passkey-only page must not offer the email login field: submitting it
    # for a passwordless account triggers an email login-code flow that hijacks
    # the passkey login. The passkey button is the sole entry point.
    resp = client.get(reverse("account_login"))
    assert b'name="login"' not in resp.content
    assert b'id="passkey_login"' in resp.content
