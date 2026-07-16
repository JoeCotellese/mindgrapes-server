"""Unit tests for the delete write service (no Postgres).

StubCursor replays the multi-statement soft-delete flow so the sole-source
retraction logic, the idempotent re-delete no-op, the owner guard, and the
privacy 404 ladder are all driven in-memory. Sole-source SQL correctness against
the real schema is asserted in the integration suite.
"""

import json
from datetime import UTC, datetime

import pytest

from openbrain.brain.exceptions import ExperienceNotFound, NotOwner
from openbrain.brain.services.deletes import get_usage, soft_delete_experience
from openbrain.brain.tests.unit._support import StubCursor, patch_brain_cursor

pytestmark = pytest.mark.django_db

ID = "11111111-1111-1111-1111-111111111111"
_DEL = "openbrain.brain.services.deletes"
_DT = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


def _patch(monkeypatch, cursor):
    patch_brain_cursor(monkeypatch, cursor, module=_DEL)


def test_soft_delete_owner_retracts_sole_source_claim(monkeypatch):
    cursor = StubCursor(
        [
            (["owner", "visibility", "deleted_at"], [("alice", "private", None)]),
            (["deleted_at"], [(_DT,)]),  # soft-delete returning deleted_at
            (["claim_id"], [("claimA",)]),  # orphaned (sole-source) claims
            ([], []),  # retract claimA
            ([], []),  # correction: claim
            ([], []),  # correction: experience
        ]
    )
    _patch(monkeypatch, cursor)

    result = soft_delete_experience("alice", ID)

    assert result["deleted_at"] == _DT
    assert result["retracted_claim_ids"] == ["claimA"]
    assert result["already_deleted"] is False
    # One claim correction then one experience correction.
    claim_corr_sql, claim_corr_params = cursor.calls[4]
    assert "correction_events" in claim_corr_sql
    assert claim_corr_params[0] == "claim"
    assert json.loads(claim_corr_params[3]) == {"polarity": "retracted"}
    exp_corr_sql, exp_corr_params = cursor.calls[5]
    assert exp_corr_params[0] == "experience"
    assert json.loads(exp_corr_params[2]) == {"deleted_at": None}
    assert exp_corr_params[5] == "ui-session:alice"


def test_soft_delete_is_idempotent_no_op_when_already_deleted(monkeypatch):
    cursor = StubCursor(
        [
            (["owner", "visibility", "deleted_at"], [("alice", "private", _DT)]),
            (["deleted_at"], [(_DT,)]),  # coalesce keeps the original timestamp
        ]
    )
    _patch(monkeypatch, cursor)

    result = soft_delete_experience("alice", ID)

    assert result["already_deleted"] is True
    assert result["retracted_claim_ids"] == []
    # No orphan scan, no corrections: only SELECT + UPDATE ran.
    assert len(cursor.calls) == 2


def test_soft_delete_non_owner_but_readable_raises_not_owner(monkeypatch):
    cursor = StubCursor(
        [(["owner", "visibility", "deleted_at"], [("bob", "shared", None)])]
    )
    _patch(monkeypatch, cursor)

    with pytest.raises(NotOwner):
        soft_delete_experience("alice", ID)
    assert len(cursor.calls) == 1


def test_soft_delete_private_not_mine_raises_not_found(monkeypatch):
    cursor = StubCursor(
        [(["owner", "visibility", "deleted_at"], [("bob", "private", None)])]
    )
    _patch(monkeypatch, cursor)

    with pytest.raises(ExperienceNotFound):
        soft_delete_experience("alice", ID)


def test_soft_delete_missing_raises_not_found(monkeypatch):
    cursor = StubCursor([(["owner", "visibility", "deleted_at"], [])])
    _patch(monkeypatch, cursor)

    with pytest.raises(ExperienceNotFound):
        soft_delete_experience("alice", ID)


def test_get_usage_returns_counts_for_readable_row(monkeypatch):
    cursor = StubCursor(
        [
            (["owner", "visibility"], [("alice", "private")]),
            (
                ["claim_count", "mentioned_entity_count", "mentioned_entity_names"],
                [(3, 2, ["Acme", "Fernworks"])],
            ),
        ]
    )
    _patch(monkeypatch, cursor)

    usage = get_usage("alice", ID)

    assert usage["claim_count"] == 3
    assert usage["mentioned_entity_count"] == 2
    assert usage["mentioned_entity_names"] == ["Acme", "Fernworks"]


def test_get_usage_missing_is_none(monkeypatch):
    cursor = StubCursor([(["owner", "visibility"], [])])
    _patch(monkeypatch, cursor)
    assert get_usage("alice", ID) is None


def test_get_usage_private_not_mine_is_none(monkeypatch):
    cursor = StubCursor([(["owner", "visibility"], [("bob", "private")])])
    _patch(monkeypatch, cursor)
    assert get_usage("alice", ID) is None
