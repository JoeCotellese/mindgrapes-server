# ABOUTME: Fixtures for core.* integration tests against the real brain.* schema.
# ABOUTME: Provides brain_write_txn — a rolled-back transaction on the dev Postgres.
"""Fixtures for core integration tests against the real brain.* schema.

Mirrors openbrain/brain/tests/integration/conftest.py and the mcp one: these
tests talk to the EXISTING dev Postgres rather than a pytest-django-managed test
database. brain_write_txn wraps each test in one transaction that is always
rolled back, so the shared dev database is never mutated.
"""

import pytest
from django.db import transaction


@pytest.fixture
def brain_write_txn(django_db_blocker):
    with django_db_blocker.unblock(), transaction.atomic():
        yield
        transaction.set_rollback(True)
