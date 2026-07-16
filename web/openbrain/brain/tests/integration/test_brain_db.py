"""Trivial reads against the real brain.* schema — the Slice A done-signal.

Requires the dev stack up (`make dev-up`); run via `make dev-test-integration`.
Confirms the data-access seam (db.py) reaches the brain.* schema.
"""

import pytest

from openbrain.brain.db import brain_cursor, brain_schema_present

pytestmark = pytest.mark.integration


def test_brain_schema_present(brain_db):
    assert brain_schema_present() is True


def test_can_read_experiences_count(brain_db):
    with brain_cursor() as cursor:
        cursor.execute("select count(*) from brain.experiences")
        count = cursor.fetchone()[0]
    assert count >= 0


def test_core_brain_objects_exist(brain_db):
    with brain_cursor() as cursor:
        cursor.execute(
            "select to_regclass('brain.summary_cache') is not null, "
            "exists(select 1 from pg_proc p "
            "       join pg_namespace n on n.oid = p.pronamespace "
            "       where n.nspname = 'brain' "
            "         and p.proname = 'match_brain_hybrid')"
        )
        summary_cache_present, match_fn_present = cursor.fetchone()
    assert summary_cache_present is True
    assert match_fn_present is True
