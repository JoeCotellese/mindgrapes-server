"""Account services: session revocation, member enable/disable, link email.

These back the admin user-management actions (Slice 2, #64). Disabling a member
soft-disables the account (is_active=False) and purges their sessions so a live
cookie dies immediately; the email helper delivers the enrollment link.
"""

import time
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core import mail
from django.test import Client

from openbrain.accounts.services import (
    deliver_enrollment_link,
    disable_member,
    enable_member,
    revoke_sessions,
)
from openbrain.oauth.models import OAuthRevocation, OAuthToken

pytestmark = pytest.mark.django_db

User = get_user_model()


def test_revoke_sessions_only_targets_the_given_user():
    a = User.objects.create_user(email="a@example.net")
    b = User.objects.create_user(email="b@example.net")
    Client().force_login(a)
    Client().force_login(b)
    assert Session.objects.count() == 2

    killed = revoke_sessions(a)

    assert killed == 1
    assert Session.objects.count() == 1  # b's session survives


def test_disable_member_deactivates_and_revokes_sessions():
    user = User.objects.create_user(email="member@example.net")
    Client().force_login(user)

    killed = disable_member(user)

    user.refresh_from_db()
    assert user.is_active is False
    assert killed == 1
    assert Session.objects.count() == 0


def test_disable_member_cascades_to_connected_oauth_clients():
    user = User.objects.create_user(email="member@example.net")
    Client().force_login(user)
    OAuthToken.objects.create(
        user=user,
        client_id="claude",
        sub=str(user.pk),
        family_id=uuid.uuid4(),
        access_token="h.p.s",
        refresh_token=uuid.uuid4().hex,
        scope="brain:read",
        issued_at=int(time.time()),
        expires_in=600,
        refresh_token_revoked_at=0,
        access_token_revoked_at=0,
    )

    killed = disable_member(user)

    user.refresh_from_db()
    assert user.is_active is False
    assert killed == 1  # return value still reports sessions, not clients
    # The cascade kills the token family and writes the revocation watermark the
    # MCP resource server enforces, so the token is rejected at the next call.
    token = OAuthToken.objects.get(user=user, client_id="claude")
    assert token.refresh_token_revoked_at != 0
    assert OAuthRevocation.objects.filter(user=user, client_id="claude").exists()


def test_enable_member_reactivates():
    user = User.objects.create_user(email="member@example.net", is_active=False)

    enable_member(user)

    user.refresh_from_db()
    assert user.is_active is True


def test_deliver_enrollment_link_emails_the_link():
    user = User.objects.create_user(email="member@example.net")
    link = "https://brain.example.net/accounts/enroll/abc123/"

    sent = deliver_enrollment_link(user, link)

    assert sent is True
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["member@example.net"]
    assert link in message.body
