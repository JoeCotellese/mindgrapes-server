"""Unit tests for the navbar review-badge context processor.

The badge is operator-only and must never query brain.* when the schema is
absent (the sqlite local/test default) or the viewer is not a superuser, so the
processor degrades to a 0 total in those cases.
"""

import pytest

from openbrain.brain.context_processors import review_badge

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(django_user_model):
    return django_user_model.objects.create_superuser(email="a@example.com")


@pytest.fixture
def member(django_user_model):
    return django_user_model.objects.create_user(email="m@example.com", password="x")


def _request(rf, user):
    request = rf.get("/")
    request.user = user
    return request


def test_badge_zero_when_schema_absent(rf, admin, monkeypatch):
    monkeypatch.setattr(
        "openbrain.brain.context_processors.brain_schema_present", lambda: False
    )
    assert review_badge(_request(rf, admin)) == {"review_pending_total": 0}


def test_badge_zero_for_non_superuser(rf, member, monkeypatch):
    monkeypatch.setattr(
        "openbrain.brain.context_processors.brain_schema_present", lambda: True
    )
    # Even with a populated brain, a non-operator never sees the operator badge.
    monkeypatch.setattr(
        "openbrain.brain.context_processors.pending_reviews",
        lambda: {"total": 9},
    )
    assert review_badge(_request(rf, member)) == {"review_pending_total": 0}


def test_badge_counts_total_for_superuser(rf, admin, monkeypatch):
    monkeypatch.setattr(
        "openbrain.brain.context_processors.brain_schema_present", lambda: True
    )
    monkeypatch.setattr(
        "openbrain.brain.context_processors.pending_reviews",
        lambda: {"total": 4, "merge_candidates": 4},
    )
    assert review_badge(_request(rf, admin)) == {"review_pending_total": 4}


def test_badge_zero_for_anonymous(rf, monkeypatch):
    from django.contrib.auth.models import AnonymousUser

    monkeypatch.setattr(
        "openbrain.brain.context_processors.brain_schema_present", lambda: True
    )
    assert review_badge(_request(rf, AnonymousUser())) == {"review_pending_total": 0}
