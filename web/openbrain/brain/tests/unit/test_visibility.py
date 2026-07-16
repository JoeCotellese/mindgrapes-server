"""Unit tests for the visibility write service (no Postgres).

Drives set_visibility with a StubCursor so the owner guard, the privacy 404-vs-403
ladder, and the correction_event write are all exercised in-memory. The DB-effect
parity against the real schema lives in the integration suite.
"""

import json

import pytest

from openbrain.brain.exceptions import ExperienceNotFound, NotOwner
from openbrain.brain.services.visibility import set_visibility
from openbrain.brain.tests.unit._support import StubCursor, patch_brain_cursor

pytestmark = pytest.mark.django_db

ID = "11111111-1111-1111-1111-111111111111"
_VIS = "openbrain.brain.services.visibility"


def _patch(monkeypatch, cursor):
    patch_brain_cursor(monkeypatch, cursor, module=_VIS)


def test_set_visibility_owner_updates_and_records_correction(monkeypatch):
    cursor = StubCursor(
        [
            (["owner", "visibility"], [("alice", "private")]),  # select for update
            ([], []),  # update
            ([], []),  # correction insert
        ]
    )
    _patch(monkeypatch, cursor)

    result = set_visibility("alice", ID, "shared")

    assert result == {
        "id": ID,
        "visibility": "shared",
        "changed_fields": ["visibility"],
    }
    # Three statements: SELECT … FOR UPDATE, UPDATE visibility, INSERT correction.
    assert len(cursor.calls) == 3
    update_sql, update_params = cursor.calls[1]
    assert "update brain.experiences" in update_sql
    assert update_params == ["shared", ID]
    corr_sql, corr_params = cursor.calls[2]
    assert "correction_events" in corr_sql
    # before/after are json-encoded and capture the visibility flip.
    assert json.loads(corr_params[2]) == {"visibility": "private"}
    assert json.loads(corr_params[3]) == {"visibility": "shared"}
    # created_by stamps the session viewer so the audit trail names the actor.
    assert corr_params[5] == "ui-session:alice"


def test_set_visibility_non_owner_but_readable_raises_not_owner(monkeypatch):
    # A shared row owned by someone else: the viewer may see it but not flip it.
    cursor = StubCursor([(["owner", "visibility"], [("bob", "shared")])])
    _patch(monkeypatch, cursor)

    with pytest.raises(NotOwner):
        set_visibility("alice", ID, "private")
    # Guard fires before any write.
    assert len(cursor.calls) == 1


def test_set_visibility_private_not_mine_raises_not_found(monkeypatch):
    # A private row owned by someone else is an identical 404 to a missing one.
    cursor = StubCursor([(["owner", "visibility"], [("bob", "private")])])
    _patch(monkeypatch, cursor)

    with pytest.raises(ExperienceNotFound):
        set_visibility("alice", ID, "shared")
    assert len(cursor.calls) == 1


def test_set_visibility_missing_raises_not_found(monkeypatch):
    cursor = StubCursor([(["owner", "visibility"], [])])
    _patch(monkeypatch, cursor)

    with pytest.raises(ExperienceNotFound):
        set_visibility("alice", ID, "shared")


def test_set_visibility_rejects_out_of_vocab_value(monkeypatch):
    # The brain.visibility enum only knows private/shared; reject before the txn.
    cursor = StubCursor([])
    _patch(monkeypatch, cursor)

    with pytest.raises(ValueError):
        set_visibility("alice", ID, "public")
    assert cursor.calls == []
