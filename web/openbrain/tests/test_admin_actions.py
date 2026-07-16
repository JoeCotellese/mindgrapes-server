"""Admin user-management: add a member, list status, disable/enable (Slice 2, #64).

Adding a member by email provisions a passwordless account and hands out a
single-use enrollment link (emailed and surfaced to copy). Disabling a member
soft-deactivates and revokes their live sessions so access stops on the next
request; re-enabling reverses it.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def admin_client():
    admin = User.objects.create_superuser(email="admin@example.net")
    client = Client()
    client.force_login(admin)
    return client


def test_add_member_provisions_passwordless_user_and_emails_link(admin_client):
    resp = admin_client.post(
        reverse("admin:accounts_user_add"),
        {"email": "member@example.net"},
        follow=True,
    )
    assert resp.status_code == 200

    member = User.objects.get(email="member@example.net")
    assert member.has_usable_password() is False
    assert member.enrollment_tokens.count() == 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["member@example.net"]
    assert "/accounts/enroll/" in mail.outbox[0].body


def test_add_member_rejects_duplicate_email(admin_client):
    User.objects.create_user(email="member@example.net")
    resp = admin_client.post(
        reverse("admin:accounts_user_add"),
        {"email": "member@example.net"},
    )
    # Re-renders the form with an error instead of creating a second user.
    assert resp.status_code == 200
    assert User.objects.filter(email="member@example.net").count() == 1


def test_issue_link_action_emails_existing_member(admin_client):
    member = User.objects.create_user(email="member@example.net")
    admin_client.post(
        reverse("admin:accounts_user_changelist"),
        {"action": "issue_link", "_selected_action": [member.pk]},
    )
    assert member.enrollment_tokens.count() == 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["member@example.net"]


def test_disable_action_terminates_session_on_next_request(admin_client):
    member = User.objects.create_user(email="member@example.net")
    member_client = Client()
    member_client.force_login(member)
    # Sanity: the member can reach a login-required page before disabling.
    assert member_client.get(reverse("accounts:passkey_add")).status_code == 200

    admin_client.post(
        reverse("admin:accounts_user_changelist"),
        {"action": "disable_members", "_selected_action": [member.pk]},
    )

    member.refresh_from_db()
    assert member.is_active is False
    # Next request from the member's session is no longer authenticated.
    resp = member_client.get(reverse("accounts:passkey_add"))
    assert resp.status_code == 302
    assert reverse("account_login") in resp.url


def test_enable_action_reactivates_member(admin_client):
    member = User.objects.create_user(email="member@example.net", is_active=False)
    admin_client.post(
        reverse("admin:accounts_user_changelist"),
        {"action": "enable_members", "_selected_action": [member.pk]},
    )
    member.refresh_from_db()
    assert member.is_active is True


def test_has_passkey_column_reflects_authenticator(admin_client):
    from openbrain.accounts.admin import UserAdmin

    member = User.objects.create_user(email="member@example.net")
    user_admin = UserAdmin(User, admin_site=None)
    assert user_admin.has_passkey(member) is False
