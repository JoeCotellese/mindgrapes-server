# ABOUTME: Unit tests for the boot-gate status reconciler.
# ABOUTME: Also guards SPINE against the init/NN-*.sql files so they can't diverge.
from pathlib import Path

import pytest
from django.conf import settings

from openbrain.mcp.boot import (
    MANIFEST_IDS,
    SPINE,
    SchemaDriftError,
    compute_migration_status,
    evaluate,
)

ALL = MANIFEST_IDS


def test_in_sync_when_ledger_matches_manifest():
    s = compute_migration_status(ALL, ALL)
    assert s.status == "in-sync"
    assert s.pending == [] and s.extra == [] and s.out_of_order == []


def test_trailing_pending_is_recoverable_drift():
    s = compute_migration_status(ALL, ALL[:-2])
    assert s.status == "drift"
    assert s.pending == ALL[-2:]
    assert s.extra == [] and s.out_of_order == []


def test_unknown_applied_id_is_extra():
    s = compute_migration_status(ALL, [*ALL, "99"])
    assert s.status == "drift"
    assert s.extra == ["99"]


def test_gap_in_sequence_is_out_of_order():
    # Applied everything except "05", but "06".."14" are applied -> "05" is a hole.
    ledger = [mid for mid in ALL if mid != "05"]
    s = compute_migration_status(ALL, ledger)
    assert "05" in s.pending
    assert "05" in s.out_of_order


def test_evaluate_passes_when_in_sync():
    assert evaluate(ALL, ALL) is None


def test_evaluate_raises_recoverable_remedy_on_pending():
    with pytest.raises(SchemaDriftError) as exc:
        evaluate(ALL, ALL[:-1])
    msg = str(exc.value)
    assert f"pending=[{ALL[-1]}]" in msg
    assert "run `python manage.py brain_ledger migrate`" in msg


def test_evaluate_raises_manual_remedy_on_extra():
    with pytest.raises(SchemaDriftError) as exc:
        evaluate(ALL, [*ALL, "99"])
    assert "manual intervention required" in str(exc.value)


def test_spine_matches_init_sql_files():
    init_dir = Path(settings.BASE_DIR).parent / "init"
    for mid, name in SPINE:
        sql = init_dir / f"{mid}-{name}.sql"
        assert sql.exists(), f"manifest entry {mid}-{name} has no init file {sql}"
