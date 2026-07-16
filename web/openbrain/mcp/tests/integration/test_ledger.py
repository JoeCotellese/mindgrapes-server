# ABOUTME: Integration tests for the brain migration-ledger operator helpers.
# ABOUTME: Exercises status/baseline/migrate against the real brain.schema_migrations.
import tempfile
from pathlib import Path

import pytest
from django.db import connection

from openbrain.mcp import ledger
from openbrain.mcp.boot import MANIFEST_IDS, SPINE

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]


def _ledger_ids() -> set[str]:
    return {row.id for row in ledger.read_ledger()}


def test_status_in_sync_on_dev_db():
    """The dev volume self-seeds the ledger to HEAD via init/14, so a fresh status
    read reconciles in-sync against the manifest."""
    result = ledger.status()
    assert result.initialized is True
    assert result.status.status == "in-sync"
    assert {row.id for row in result.ledger} == set(MANIFEST_IDS)


def test_baseline_is_idempotent_at_head():
    before = _ledger_ids()
    ledger.baseline()  # ON CONFLICT DO NOTHING — a no-op on an at-HEAD ledger
    assert _ledger_ids() == before


def test_migrate_applies_a_pending_entry():
    """Delete the trailing ledger row to manufacture a pending entry, then migrate
    re-applies it (running an idempotent stub SQL) and re-stamps the row. All of
    this rolls back with brain_write_txn."""
    last_id, last_name = SPINE[-1]
    with connection.cursor() as cur:
        cur.execute("delete from brain.schema_migrations where id = %s", [last_id])
    assert last_id not in _ledger_ids()

    with tempfile.TemporaryDirectory() as tmp:
        # The real init file is idempotent, but a harmless stub keeps the test from
        # depending on the init/ mount and proves the apply+stamp path runs the SQL.
        (Path(tmp) / f"{last_id}-{last_name}.sql").write_text("select 1;")
        applied = ledger.migrate(init_dir=Path(tmp))

    assert applied == [last_id]
    assert last_id in _ledger_ids()


def test_migrate_refuses_when_ledger_diverges():
    """A manifest shorter than the ledger makes the extra rows read as ahead/unknown
    drift — migrate must refuse rather than mask a history mismatch."""
    short_manifest = SPINE[:-1]  # ledger still has the trailing id -> 'extra'
    with pytest.raises(RuntimeError) as exc:
        ledger.migrate(manifest=short_manifest)
    assert "refusing to apply" in str(exc.value)
