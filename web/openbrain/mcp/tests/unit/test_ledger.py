# ABOUTME: Unit tests for the brain migration-ledger operator helpers.
# ABOUTME: Guards LEDGER_DDL against init/14 so baseline can't create a divergent table.
import re
from pathlib import Path

import pytest
from django.conf import settings

from openbrain.mcp import ledger as ledger_mod
from openbrain.mcp.boot import SPINE
from openbrain.mcp.ledger import LEDGER_DDL


def _normalize(sql: str) -> str:
    """Collapse whitespace so formatting differences don't trip the comparison."""
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").lower()


def test_ledger_ddl_matches_init_14():
    """baseline() creates the ledger on existing volumes; its CREATE TABLE must be
    byte-for-byte equivalent to the one init/14 self-seeds on fresh volumes, or the
    two seeding paths would build different tables."""
    init_sql = (
        Path(settings.BASE_DIR).parent / "init" / "14-schema-migrations.sql"
    ).read_text()
    match = re.search(
        r"create table if not exists brain\.schema_migrations\s*\(.*?\);",
        init_sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert match, "could not find the schema_migrations CREATE TABLE in init/14"
    assert _normalize(LEDGER_DDL) == _normalize(match.group(0))


def test_init_14_self_seed_matches_spine():
    """Fresh volumes get their ledger from init/14's literal list; existing volumes get
    it from baseline(SPINE). A row missing here means a fresh volume boots believing it
    never applied a migration it in fact ran — drift that only shows up in prod."""
    init_sql = (
        Path(settings.BASE_DIR).parent / "init" / "14-schema-migrations.sql"
    ).read_text()
    match = re.search(
        r"insert into brain\.schema_migrations \(id, name, applied_by\) values(.*?)on conflict",
        init_sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert match, "could not find the schema_migrations seed INSERT in init/14"
    seeded = re.findall(r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,", match.group(1))
    assert seeded == list(SPINE)


def test_migrate_refuses_on_uninitialized_ledger(monkeypatch):
    """Applying every "pending" entry on an un-baselined volume would re-run the
    whole init spine — migrate must refuse and point at baseline instead."""
    monkeypatch.setattr(ledger_mod, "ledger_initialized", lambda: False)
    with pytest.raises(RuntimeError) as exc:
        ledger_mod.migrate()
    assert "brain.schema_migrations missing" in str(exc.value)
    assert "brain_ledger baseline" in str(exc.value)
