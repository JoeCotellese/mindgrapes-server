# ABOUTME: Applies/baselines/inspects the brain schema-migration ledger.
# ABOUTME: Wraps boot.py's pure reconciler with the Postgres I/O the operator CLI needs.
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import ProgrammingError, connection, transaction

from openbrain.mcp.boot import (
    SPINE,
    MigrationStatus,
    compute_migration_status,
)

# Mirrors init/14-schema-migrations.sql. Init creates this on fresh volumes;
# baseline creates it on existing ones (where init never re-runs). A unit test
# asserts this matches the init/14 CREATE TABLE so the two can't diverge.
LEDGER_DDL = """
create table if not exists brain.schema_migrations (
  id          text primary key,
  name        text not null,
  applied_at  timestamptz not null default now(),
  applied_by  text
)"""

_LEDGER_PRESENT_SQL = "select to_regclass('brain.schema_migrations') is not null"
_LEDGER_ROWS_SQL = "select id, name from brain.schema_migrations order by id"
_INSERT_ROW_SQL = (
    "insert into brain.schema_migrations (id, name, applied_by) "
    "values (%s, %s, %s) on conflict (id) do nothing"
)


def default_init_dir() -> Path:
    """Where the init/NN-*.sql spine lives — repo-root/init.

    Resolves the same way the spine guard in test_boot.py does
    (BASE_DIR is .../web, so its parent is the repo root). In a container this
    only resolves when ./init is mounted (docker-compose mounts it for the MCP
    services); status/baseline never read the files, so they work regardless.
    """
    return Path(settings.BASE_DIR).parent / "init"


@dataclass
class LedgerRow:
    id: str
    name: str


def ledger_initialized() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(_LEDGER_PRESENT_SQL)
        return bool(cursor.fetchone()[0])


def read_ledger() -> list[LedgerRow]:
    """Applied rows, ordered by id. Missing table reads as empty (not baselined)."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(_LEDGER_ROWS_SQL)
            return [LedgerRow(id=row[0], name=row[1]) for row in cursor.fetchall()]
    except ProgrammingError:  # undefined_table — report empty, don't crash
        return []


def ensure_ledger() -> None:
    with connection.cursor() as cursor:
        cursor.execute(LEDGER_DDL)


def migrate(
    manifest: tuple[tuple[str, str], ...] = SPINE,
    init_dir: Path | None = None,
) -> list[str]:
    """Apply each pending init/NN-*.sql and stamp its ledger row, one txn per entry.

    Refuses on an un-baselined volume (applying every "pending" entry would re-run
    the whole init spine) and on extra/out-of-order drift (the ledger disagrees
    with this build's manifest — that needs manual intervention, not an apply).
    """
    if not ledger_initialized():
        raise RuntimeError(
            "[migrate] brain.schema_migrations missing — run "
            "`python manage.py brain_ledger baseline` first"
        )
    manifest_ids = [mid for mid, _ in manifest]
    ledger_ids = [row.id for row in read_ledger()]
    s = compute_migration_status(manifest_ids, ledger_ids)
    if s.extra or s.out_of_order:
        raise RuntimeError(
            f"[migrate] refusing to apply — ledger diverges from the manifest "
            f"(ahead/unknown=[{','.join(s.extra)}] "
            f"out-of-order=[{','.join(s.out_of_order)}]); "
            f"this needs manual intervention, not an apply"
        )
    base = init_dir or default_init_dir()
    by_id = {mid: name for mid, name in manifest}
    applied: list[str] = []
    for mid in s.pending:
        name = by_id[mid]
        sql = (base / f"{mid}-{name}.sql").read_text()
        # One transaction per entry: a failure mid-apply leaves neither the schema
        # change nor the ledger row.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql)
            cursor.execute(_INSERT_ROW_SQL, [mid, name, "migrate"])
        applied.append(mid)
    return applied


def baseline(manifest: tuple[tuple[str, str], ...] = SPINE) -> None:
    """Stamp an existing at-HEAD volume into the ledger without running any SQL.

    One atomic multi-row insert — a partial stamp would read as drift and refuse
    boot until re-run.
    """
    ensure_ledger()
    if not manifest:
        return
    tuples = ", ".join(["(%s, %s, 'baseline')"] * len(manifest))
    params: list[str] = [value for entry in manifest for value in entry]
    with connection.cursor() as cursor:
        cursor.execute(
            f"insert into brain.schema_migrations (id, name, applied_by) "
            f"values {tuples} on conflict (id) do nothing",
            params,
        )


@dataclass
class LedgerStatus:
    status: MigrationStatus
    initialized: bool
    ledger: list[LedgerRow]


def status(manifest: tuple[tuple[str, str], ...] = SPINE) -> LedgerStatus:
    initialized = ledger_initialized()
    ledger = read_ledger() if initialized else []
    s = compute_migration_status(
        [mid for mid, _ in manifest], [row.id for row in ledger]
    )
    return LedgerStatus(status=s, initialized=initialized, ledger=ledger)
