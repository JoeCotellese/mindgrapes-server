"""Unit tests for the edit write service (no Postgres).

StubCursor replays each branch of editExperience — metadata-only, in-place
(cosine > 0.95), supersede (cosine <= 0.95, with verbatim/inferred auto-retract
and paraphrased-pending bucketing), and explicit paraphrased retraction — plus
the owner guard and empty-patch rejection. Embeddings are stubbed via the module
seam so no OpenRouter call happens. Real-schema SQL parity is in the integration
suite.
"""

import json

import pytest

from openbrain.brain.exceptions import ExperienceNotFound, NotOwner
from openbrain.brain.services.edits import edit_experience
from openbrain.brain.tests.unit._support import StubCursor, patch_brain_cursor

pytestmark = pytest.mark.django_db

ID = "11111111-1111-1111-1111-111111111111"
_EDIT = "openbrain.brain.services.edits"

# Column order of the SELECT … FOR UPDATE the service issues first.
_SELECT_COLS = [
    "id",
    "content",
    "metadata",
    "occurred_at",
    "source_kind",
    "source_ref",
    "owner",
    "account_id",
    "visibility",
    "lat",
    "lng",
]


def _row(owner="alice", visibility="private", metadata='{"k": 1}', content="old"):
    return (ID, content, metadata, None, "note", None, owner, "acct", visibility, None, None)


def _patch(monkeypatch, cursor, embedding=None):
    patch_brain_cursor(monkeypatch, cursor, module=_EDIT)
    monkeypatch.setattr(f"{_EDIT}.embed_query", lambda text: embedding or [0.01] * 1536)


def test_edit_metadata_only_updates_in_place_with_correction(monkeypatch):
    cursor = StubCursor(
        [
            (_SELECT_COLS, [_row()]),
            ([], []),  # update metadata
            ([], []),  # correction
        ]
    )
    _patch(monkeypatch, cursor)

    result = edit_experience("alice", ID, metadata={"k": 2})

    assert result["mode"] == "metadata_only"
    corr_sql, corr_params = cursor.calls[2]
    assert "correction_events" in corr_sql
    assert json.loads(corr_params[2]) == {"metadata": {"k": 1}}
    assert json.loads(corr_params[3]) == {"metadata": {"k": 2}}


def test_edit_content_in_place_when_cosine_above_threshold(monkeypatch):
    cursor = StubCursor(
        [
            (_SELECT_COLS, [_row()]),
            (["similarity"], [(0.99,)]),  # cosine
            ([], []),  # update content in place
            ([], []),  # correction
        ]
    )
    _patch(monkeypatch, cursor)

    result = edit_experience("alice", ID, content="old, lightly tweaked")

    assert result["mode"] == "in_place"
    assert result["similarity"] == pytest.approx(0.99)
    update_sql, update_params = cursor.calls[2]
    assert "update brain.experiences" in update_sql
    assert update_params[0] == "old, lightly tweaked"


def test_edit_content_supersedes_and_auto_retracts(monkeypatch):
    cursor = StubCursor(
        [
            (_SELECT_COLS, [_row()]),
            (["similarity"], [(0.42,)]),  # cosine below threshold
            (["id"], [("22222222-2222-2222-2222-222222222222",)]),  # insert returning
            ([], []),  # set superseded_by
            ([], []),  # carry attachments forward (#42)
            (
                ["claim_id", "support_kind"],
                [("c-verbatim", "verbatim"), ("c-para", "paraphrased")],
            ),
            (["id"], [("c-verbatim",)]),  # retract verbatim
            ([], []),  # correction: claim
            ([], []),  # correction: experience supersede
        ]
    )
    _patch(monkeypatch, cursor)

    result = edit_experience("alice", ID, content="a completely different thought")

    assert result["mode"] == "superseded"
    assert result["new_id"] == "22222222-2222-2222-2222-222222222222"
    assert result["auto_retracted_claim_ids"] == ["c-verbatim"]
    # Paraphrased claims are surfaced for review, not retracted automatically.
    assert result["paraphrased_claim_ids_pending"] == ["c-para"]
    assert result["paraphrased_retracted_claim_ids"] == []


def test_edit_paraphrased_retractions_only(monkeypatch):
    cursor = StubCursor(
        [
            (_SELECT_COLS, [_row()]),
            (["claim_id"], [("p1",)]),  # eligible paraphrased
            (["id"], [("p1",)]),  # retract p1
            ([], []),  # correction
        ]
    )
    _patch(monkeypatch, cursor)

    result = edit_experience("alice", ID, paraphrased_claim_retractions=["p1"])

    assert result["mode"] == "metadata_only"
    assert result["paraphrased_retracted_claim_ids"] == ["p1"]


def test_edit_non_owner_but_readable_raises_not_owner(monkeypatch):
    cursor = StubCursor([(_SELECT_COLS, [_row(owner="bob", visibility="shared")])])
    _patch(monkeypatch, cursor)

    with pytest.raises(NotOwner):
        edit_experience("alice", ID, metadata={"k": 2})
    assert len(cursor.calls) == 1


def test_edit_private_not_mine_raises_not_found(monkeypatch):
    cursor = StubCursor([(_SELECT_COLS, [_row(owner="bob", visibility="private")])])
    _patch(monkeypatch, cursor)

    with pytest.raises(ExperienceNotFound):
        edit_experience("alice", ID, metadata={"k": 2})


def test_edit_missing_raises_not_found(monkeypatch):
    cursor = StubCursor([(_SELECT_COLS, [])])
    _patch(monkeypatch, cursor)

    with pytest.raises(ExperienceNotFound):
        edit_experience("alice", ID, metadata={"k": 2})


def test_edit_empty_patch_rejected(monkeypatch):
    cursor = StubCursor([])
    _patch(monkeypatch, cursor)

    with pytest.raises(ValueError):
        edit_experience("alice", ID)
    assert cursor.calls == []
