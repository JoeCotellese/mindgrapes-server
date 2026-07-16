"""The enrollment flow: a single-use link, hardened against GET prefetch (#69).

GET renders a confirmation page and consumes nothing — so an email scanner or
browser prefetch can't burn the link. The user's explicit POST consumes the
token atomically, logs them in, and sends them to passkey registration. Invalid,
expired, or already-used links are rejected without authenticating anyone.
"""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from openbrain.accounts.models import EnrollmentToken

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(email="member@example.net")


def confirm_url(raw):
    return reverse("accounts:enroll", args=[raw])


def consume_url(raw):
    return reverse("accounts:enroll_consume", args=[raw])


def test_get_renders_confirm_without_consuming_or_authenticating(client, user):
    token, raw = EnrollmentToken.objects.create_for(user)
    resp = client.get(confirm_url(raw))
    assert resp.status_code == 200
    assert "_auth_user_id" not in client.session
    token.refresh_from_db()
    assert token.used_at is None  # GET did not consume


def test_post_consumes_logs_in_and_redirects_to_passkey_add(client, user):
    _, raw = EnrollmentToken.objects.create_for(user)
    resp = client.post(consume_url(raw))
    assert resp.status_code == 302
    assert resp.url == reverse("accounts:passkey_add")
    assert client.session.get("_auth_user_id") == str(user.pk)


def test_prefetch_then_real_click_still_succeeds(client, user):
    """A GET prefetch must leave the link usable for the human's POST."""
    _, raw = EnrollmentToken.objects.create_for(user)
    assert client.get(confirm_url(raw)).status_code == 200  # prefetch
    resp = client.post(consume_url(raw))  # real click
    assert resp.status_code == 302
    assert resp.url == reverse("accounts:passkey_add")


def test_post_is_single_use(client, user):
    _, raw = EnrollmentToken.objects.create_for(user)
    client.post(consume_url(raw))
    resp = Client().post(consume_url(raw))
    assert resp.status_code == 410


def test_expired_link_rejected_on_get_and_post(client, user):
    token, raw = EnrollmentToken.objects.create_for(user)
    token.expires_at = timezone.now() - timedelta(seconds=1)
    token.save()
    assert client.get(confirm_url(raw)).status_code == 410
    assert client.post(consume_url(raw)).status_code == 410
    assert "_auth_user_id" not in client.session


def test_unknown_link_rejected(client, user):
    assert client.get(confirm_url("not-a-real-token")).status_code == 410
    assert client.post(consume_url("not-a-real-token")).status_code == 410
    assert "_auth_user_id" not in client.session
