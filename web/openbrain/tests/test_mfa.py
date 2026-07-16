"""MFA is configured for passwordless passkeys only — no password, no TOTP.

These lock the auth posture for Slice 1 (#63): WebAuthn passkeys plus recovery
codes, passkey login on, self-serve signup off. The recovery-code test exercises
allauth's generator directly so the "shown once at enrollment" codes are proven
to generate and verify before the enrollment flow depends on them.
"""

import pytest
from allauth.mfa import app_settings as mfa_settings
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse

User = get_user_model()


def test_mfa_app_installed():
    assert "allauth.mfa" in settings.INSTALLED_APPS


def test_supported_types_are_webauthn_and_recovery_only():
    assert settings.MFA_SUPPORTED_TYPES == ["webauthn", "recovery_codes"]
    assert "totp" not in settings.MFA_SUPPORTED_TYPES


def test_passkey_login_enabled():
    assert settings.MFA_PASSKEY_LOGIN_ENABLED is True


def test_passkey_signup_disabled():
    # No self-serve signup: users are provisioned by an admin (Slice 2).
    assert settings.MFA_PASSKEY_SIGNUP_ENABLED is False


def test_mfa_and_passkey_login_urls_mounted():
    assert reverse("mfa_index") == "/accounts/2fa/"
    assert reverse("mfa_login_webauthn") == "/accounts/2fa/webauthn/login/"


@pytest.mark.django_db
def test_recovery_codes_generate_and_verify():
    user = User.objects.create_user(email="member@example.net")
    rc = RecoveryCodes.activate(user)

    codes = rc.get_unused_codes()
    assert len(codes) == mfa_settings.RECOVERY_CODE_COUNT

    # A valid code verifies once, then is spent (single-use).
    code = codes[0]
    assert rc.validate_code(code) is True
    assert rc.validate_code(code) is False

    # A code that was never issued is rejected.
    assert rc.validate_code("not-a-valid-code") is False
