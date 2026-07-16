"""Fixtures for MCP integration tests against the real brain.* schema.

Mirrors openbrain/brain/tests/integration/conftest.py: these tests talk to the
EXISTING dev Postgres (brain.* + the schema_migrations ledger already loaded)
rather than a pytest-django-managed test database. The write fixture wraps each
test in one transaction that is always rolled back, so the shared dev database is
never mutated.
"""

import pytest
from django.db import transaction


@pytest.fixture
def brain_write_txn(django_db_blocker):
    with django_db_blocker.unblock(), transaction.atomic():
        yield
        transaction.set_rollback(True)
