"""Custom User model: email identity, no username, passwordless-ready."""

import pytest
from django.contrib.auth import get_user_model

pytestmark = pytest.mark.django_db

User = get_user_model()


def test_email_is_the_identifier():
    assert User.USERNAME_FIELD == "email"
    assert "email" not in User.REQUIRED_FIELDS


def test_user_has_no_username_field():
    field_names = {f.name for f in User._meta.get_fields()}
    assert "username" not in field_names


def test_create_user_with_email():
    user = User.objects.create_user(email="joe@example.net")
    assert user.email == "joe@example.net"
    assert user.is_active is True
    assert user.is_staff is False
    assert user.is_superuser is False


def test_create_user_requires_email():
    with pytest.raises(ValueError):
        User.objects.create_user(email="")


def test_create_user_normalizes_email_domain():
    user = User.objects.create_user(email="Ada@Example.NET")
    # BaseUserManager.normalize_email lowercases the domain part.
    assert user.email == "Ada@example.net"


def test_create_superuser():
    admin = User.objects.create_superuser(email="admin@example.net")
    assert admin.is_staff is True
    assert admin.is_superuser is True


def test_str_is_email():
    user = User.objects.create_user(email="spouse@example.net")
    assert str(user) == "spouse@example.net"
