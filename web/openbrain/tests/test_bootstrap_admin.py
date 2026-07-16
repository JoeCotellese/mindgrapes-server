"""The bootstrap_admin command: create the first admin from the CLI.

There is no web signup, so the first administrator is seeded from the command
line: a passwordless superuser plus a single-use enrollment link to print and
open in a browser to register the first passkey.
"""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

pytestmark = pytest.mark.django_db

User = get_user_model()


def test_creates_passwordless_superuser_and_prints_link():
    out = StringIO()
    call_command(
        "bootstrap_admin",
        "admin@example.net",
        "--base-url",
        "http://localhost:8080",
        stdout=out,
    )

    user = User.objects.get(email="admin@example.net")
    assert user.is_superuser is True
    assert user.is_staff is True
    assert user.has_usable_password() is False

    assert user.enrollment_tokens.count() == 1
    output = out.getvalue()
    assert "http://localhost:8080/accounts/enroll/" in output


def test_rejects_duplicate_email():
    call_command("bootstrap_admin", "admin@example.net", "--base-url", "http://x")
    with pytest.raises(CommandError):
        call_command("bootstrap_admin", "admin@example.net", "--base-url", "http://x")
