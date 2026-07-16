"""EnrollmentToken: single-use, expiring links that bootstrap passkey enrollment.

The raw token travels in the enrollment URL and is shown to the operator once;
only its hash is persisted, so a database read never exposes a usable link.
"""

import hashlib
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from openbrain.accounts.models import EnrollmentToken

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(email="member@example.net")


def test_create_for_returns_raw_token_and_persists_only_hash(user):
    token, raw = EnrollmentToken.objects.create_for(user)
    assert raw  # the caller receives the secret exactly once
    assert token.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert token.token_hash != raw  # the raw secret is never stored
    assert token.user == user


def test_tokens_are_unique(user):
    _, raw1 = EnrollmentToken.objects.create_for(user)
    _, raw2 = EnrollmentToken.objects.create_for(user)
    assert raw1 != raw2


def test_fresh_token_is_valid(user):
    token, _ = EnrollmentToken.objects.create_for(user)
    assert token.is_valid() is True


def test_expired_token_is_invalid(user):
    token, _ = EnrollmentToken.objects.create_for(user)
    token.expires_at = timezone.now() - timedelta(seconds=1)
    token.save()
    assert token.is_valid() is False


def test_used_token_is_invalid(user):
    token, _ = EnrollmentToken.objects.create_for(user)
    token.consume()
    assert token.is_valid() is False


def test_validate_returns_token_for_valid_raw(user):
    token, raw = EnrollmentToken.objects.create_for(user)
    found = EnrollmentToken.objects.validate(raw)
    assert found is not None
    assert found.pk == token.pk


def test_validate_rejects_unknown_raw(user):
    EnrollmentToken.objects.create_for(user)
    assert EnrollmentToken.objects.validate("not-a-real-token") is None


def test_validate_rejects_expired(user):
    token, raw = EnrollmentToken.objects.create_for(user)
    token.expires_at = timezone.now() - timedelta(seconds=1)
    token.save()
    assert EnrollmentToken.objects.validate(raw) is None


def test_validate_rejects_used(user):
    token, raw = EnrollmentToken.objects.create_for(user)
    token.consume()
    assert EnrollmentToken.objects.validate(raw) is None


def test_consume_is_single_use(user):
    token, _ = EnrollmentToken.objects.create_for(user)
    assert token.consume() is True
    token.refresh_from_db()
    # A second consume is a no-op and reports that it changed nothing.
    assert token.consume() is False
