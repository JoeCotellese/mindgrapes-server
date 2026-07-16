"""Write-service integration tests against the real brain.* schema.

Each test seeds rows, runs a write service, and asserts the resulting
experiences / correction_events / claims directly — then brain_write_txn rolls the
whole transaction back, so the shared dev database is never mutated. This is the
DB-effect contract for the write path (init/*.sql is canonical); the
unit suite covers the branch logic in isolation.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import uuid

import pytest
from django.db import connection
from django.test import override_settings

from openbrain.brain.exceptions import NotOwner
from openbrain.brain.services.deletes import soft_delete_experience
from openbrain.brain.services.edits import edit_experience, update_experience
from openbrain.brain.services.visibility import set_visibility

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

VIEWER = "itest-viewer"
OTHER = "itest-other"

# All-positive vs sign-alternating unit vectors: identical → cosine 1.0 (in-place),
# orthogonal → cosine 0.0 (supersede). The seed embedding is the all-positive one.
_VEC_NEAR = [0.05] * 1536
_VEC_FAR = [0.05 if i % 2 == 0 else -0.05 for i in range(1536)]
_VEC_SEED_LIT = "[" + ",".join(["0.05"] * 1536) + "]"


def _embed_near(text):
    return _VEC_NEAR


def _embed_far(text):
    return _VEC_FAR


def _seed_experience(owner=VIEWER, visibility="private", content="seed content"):
    eid = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.experiences (id, content, embedding, owner, visibility) "
            "values (%s::uuid, %s, %s::vector, %s, %s::brain.visibility)",
            [eid, content, _VEC_SEED_LIT, owner, visibility],
        )
    return eid


def _seed_claim_sourced_only_by(experience_id):
    entity_id = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name) "
            "values (%s::uuid, 'concept'::brain.entity_kind, %s)",
            [entity_id, f"itest-entity-{entity_id[:8]}"],
        )
        cur.execute(
            "insert into brain.claims (id, subject_id, predicate, polarity) "
            "values (%s::uuid, %s::uuid, 'relates_to', 'asserted'::brain.polarity)",
            [claim_id, entity_id],
        )
        cur.execute(
            "insert into brain.claim_sources (claim_id, experience_id, support_kind) "
            "values (%s::uuid, %s::uuid, 'verbatim'::brain.support_kind)",
            [claim_id, experience_id],
        )
    return claim_id


def _scalar(sql, params):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def test_set_visibility_flips_row_and_writes_correction():
    eid = _seed_experience(visibility="private")

    result = set_visibility(VIEWER, eid, "shared")

    assert result["visibility"] == "shared"
    assert (
        _scalar(
            "select visibility::text from brain.experiences where id = %s::uuid", [eid]
        )
        == "shared"
    )
    assert (
        _scalar(
            "select count(*) from brain.correction_events "
            "where target_kind = 'experience' and target_id = %s::uuid",
            [eid],
        )
        == 1
    )


def test_set_visibility_non_owner_raises_not_owner():
    eid = _seed_experience(owner=OTHER, visibility="shared")
    with pytest.raises(NotOwner):
        set_visibility(VIEWER, eid, "private")


@override_settings(
    BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_brain_writes._embed_near"
)
def test_edit_content_in_place_when_cosine_high():
    eid = _seed_experience(content="original thought")

    result = edit_experience(VIEWER, eid, content="original thought, refined")

    assert result["mode"] == "in_place"
    content, superseded_by = (
        _scalar("select content from brain.experiences where id = %s::uuid", [eid]),
        _scalar(
            "select superseded_by from brain.experiences where id = %s::uuid", [eid]
        ),
    )
    assert content == "original thought, refined"
    assert superseded_by is None


@override_settings(
    BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_brain_writes._embed_far"
)
def test_edit_content_supersedes_and_inherits_owner_visibility():
    eid = _seed_experience(content="original thought", visibility="shared")

    result = edit_experience(VIEWER, eid, content="a wholly unrelated idea")

    assert result["mode"] == "superseded"
    new_id = result["new_id"]
    # Old row back-links to the new one.
    assert (
        _scalar(
            "select superseded_by::text from brain.experiences where id = %s::uuid",
            [eid],
        )
        == new_id
    )
    # New row inherits owner + visibility verbatim and lands as pending (#85).
    owner = _scalar("select owner from brain.experiences where id = %s::uuid", [new_id])
    visibility = _scalar(
        "select visibility::text from brain.experiences where id = %s::uuid", [new_id]
    )
    status = _scalar(
        "select consolidation_status::text from brain.experiences where id = %s::uuid",
        [new_id],
    )
    assert owner == VIEWER
    assert visibility == "shared"
    assert status == "pending"


def test_soft_delete_sets_deleted_at_and_retracts_sole_source_claim():
    eid = _seed_experience()
    claim_id = _seed_claim_sourced_only_by(eid)

    result = soft_delete_experience(VIEWER, eid)

    assert result["retracted_claim_ids"] == [claim_id]
    assert (
        _scalar(
            "select deleted_at is not null from brain.experiences where id = %s::uuid",
            [eid],
        )
        is True
    )
    assert (
        _scalar(
            "select polarity::text from brain.claims where id = %s::uuid", [claim_id]
        )
        == "retracted"
    )
    # One correction for the claim retraction, one for the experience delete.
    assert (
        _scalar(
            "select count(*) from brain.correction_events "
            "where target_id in (%s::uuid, %s::uuid)",
            [eid, claim_id],
        )
        == 2
    )


def test_soft_delete_is_idempotent():
    eid = _seed_experience()

    first = soft_delete_experience(VIEWER, eid)
    second = soft_delete_experience(VIEWER, eid)

    assert first["already_deleted"] is False
    assert second["already_deleted"] is True
    # The re-delete writes no further corrections.
    assert (
        _scalar(
            "select count(*) from brain.correction_events "
            "where target_kind = 'experience' and target_id = %s::uuid",
            [eid],
        )
        == 1
    )


# --- update_experience (MCP tool, Slice C) ------------------------------------


def test_update_experience_patches_source_ref_and_audits():
    eid = _seed_experience()
    res = update_experience(VIEWER, eid, {"source_ref": "vault/note.md"})
    assert res["changed_fields"] == ["source_ref"]
    assert res["correction_event_id"]
    assert (
        _scalar("select source_ref from brain.experiences where id = %s::uuid", [eid])
        == "vault/note.md"
    )
    assert (
        _scalar(
            "select count(*) from brain.correction_events "
            "where target_kind = 'experience' and target_id = %s::uuid",
            [eid],
        )
        == 1
    )


def test_update_experience_sets_occurred_at():
    eid = _seed_experience()
    update_experience(VIEWER, eid, {"occurred_at": "2026-03-14T19:00:00+00:00"})
    stored = _scalar(
        "select occurred_at::text from brain.experiences where id = %s::uuid", [eid]
    )
    assert stored.startswith("2026-03-14 19:00:00")


def test_update_experience_owner_can_flip_visibility():
    eid = _seed_experience(owner=VIEWER, visibility="private")
    res = update_experience(VIEWER, eid, {"visibility": "shared"})
    assert res["changed_fields"] == ["visibility"]
    assert (
        _scalar(
            "select visibility::text from brain.experiences where id = %s::uuid", [eid]
        )
        == "shared"
    )


def test_update_experience_non_owner_cannot_flip_visibility():
    eid = _seed_experience(owner=OTHER, visibility="shared")
    with pytest.raises(NotOwner):
        update_experience(VIEWER, eid, {"visibility": "private"})
    # The refused write left the row untouched.
    assert (
        _scalar(
            "select visibility::text from brain.experiences where id = %s::uuid", [eid]
        )
        == "shared"
    )


def test_update_experience_rejects_non_editable_field():
    eid = _seed_experience()
    with pytest.raises(ValueError, match="not editable"):
        update_experience(VIEWER, eid, {"content": "rewrite"})


def test_update_experience_rejects_empty_patch():
    eid = _seed_experience()
    with pytest.raises(ValueError, match="at least one field"):
        update_experience(VIEWER, eid, {})


def test_update_experience_rejects_bad_visibility():
    eid = _seed_experience()
    with pytest.raises(ValueError, match="must be 'private' or 'shared'"):
        update_experience(VIEWER, eid, {"visibility": "public"})
