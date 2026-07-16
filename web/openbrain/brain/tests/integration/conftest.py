"""Fixtures for brain.* integration tests.

These tests talk to the EXISTING dev Postgres (brain.* already loaded) rather
than a pytest-django-managed test database, so they unblock DB access directly
instead of using the `db` fixture (which would create/destroy a test DB without
the brain.* schema). Keep them read-only.
"""

import pytest
from django.db import transaction


@pytest.fixture
def brain_db(django_db_blocker):
    with django_db_blocker.unblock():
        yield


@pytest.fixture
def brain_write_txn(django_db_blocker):
    """Run a write test inside one transaction that is always rolled back.

    The write services open their own transaction.atomic(); nested inside this
    outer atomic those become savepoints that never reach the shared dev database.
    set_rollback(True) discards everything on exit, so write integration tests can
    seed, mutate, and assert against the real schema without persisting a thing.
    """
    with django_db_blocker.unblock(), transaction.atomic():
        yield
        transaction.set_rollback(True)
