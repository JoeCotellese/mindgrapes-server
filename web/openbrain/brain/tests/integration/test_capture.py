"""capture_thought write-service integration tests against the real brain.* schema.

Each test stubs the embedding (and, for the bare path, metadata) seams via
@override_settings, runs capture(), and asserts the resulting brain.experiences /
brain.entities / brain.mentions rows directly — then brain_write_txn rolls the
whole transaction back, so the shared dev database is never mutated. This is the
DB-effect + structuredContent contract for the capture path.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import uuid

import pytest
from django.db import connection
from django.test import override_settings

from openbrain.brain.embeddings import EmbeddingError
from openbrain.brain.services.captures import capture

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

OWNER = "itest-capture-owner"
_MOD = "openbrain.brain.tests.integration.test_capture"

# All-positive seed vector; a seeded entity given the same embedding ranks #1 in
# both the trgm and vector channels, making the matched case deterministic.
_VEC = [0.05] * 1536
_VEC_LIT = "[" + ",".join(["0.05"] * 1536) + "]"


def _embed(_text):
    return _VEC


def _embed_boom(_text):
    raise EmbeddingError("openrouter unavailable")


def _metadata(_text):
    return {
        "people": [],
        "action_items": [],
        "dates_mentioned": [],
        "topics": ["itest"],
        "type": "observation",
    }


def _scalar(sql, params):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def _unique_name(prefix):
    return f"{prefix} {uuid.uuid4().hex}"


@override_settings(
    BRAIN_EMBED_FN=f"{_MOD}._embed", BRAIN_METADATA_FN=f"{_MOD}._metadata"
)
def test_bare_capture_writes_pending_experience_with_extracted_metadata():
    result = capture(content="a bare thought", owner=OWNER, account_id="household")

    assert result["is_structured"] is False
    assert "extracted_entities" not in result
    eid = result["experience_id"]
    assert result["metadata"]["source"] == "mcp"
    assert result["metadata"]["topics"] == ["itest"]

    assert (
        _scalar(
            "select consolidation_status::text from brain.experiences where id=%s::uuid",
            [eid],
        )
        == "pending"
    )
    assert (
        _scalar("select owner from brain.experiences where id=%s::uuid", [eid]) == OWNER
    )
    assert (
        _scalar(
            "select visibility::text from brain.experiences where id=%s::uuid", [eid]
        )
        == "private"
    )


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed")
def test_structured_capture_new_participant_creates_entity_and_mention():
    name = _unique_name("zzqx")
    result = capture(
        content="met someone new today",
        owner=OWNER,
        account_id="household",
        occurred_at="2025-05-01T12:00:00Z",
        participants=[{"name": name}],
        source_kind="transcript",
        source_ref="gdrive:abc",
    )

    assert result["is_structured"] is True
    assert result["claims_pending"] is True
    assert len(result["extracted_entities"]) == 1
    extracted = result["extracted_entities"][0]
    assert extracted["action"] == "created"
    # structuredContent.metadata echoes only the args that were passed.
    assert result["metadata"]["source_kind"] == "transcript"
    assert result["metadata"]["source_ref"] == "gdrive:abc"

    eid = result["experience_id"]
    assert (
        _scalar(
            "select count(*) from brain.mentions "
            "where experience_id=%s::uuid and entity_id=%s::uuid",
            [eid, extracted["entity_id"]],
        )
        == 1
    )
    assert (
        _scalar(
            "select source_kind::text from brain.experiences where id=%s::uuid", [eid]
        )
        == "transcript"
    )
    assert (
        _scalar("select source_ref from brain.experiences where id=%s::uuid", [eid])
        == "gdrive:abc"
    )
    assert (
        _scalar(
            "select occurred_at is not null from brain.experiences where id=%s::uuid",
            [eid],
        )
        is True
    )


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed")
def test_structured_capture_provided_entity_id_links_without_resolving():
    ent_id = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name) "
            "values (%s::uuid, 'person'::brain.entity_kind, %s)",
            [ent_id, _unique_name("Provided")],
        )

    result = capture(
        content="talked to them",
        owner=OWNER,
        account_id="household",
        participants=[{"name": "Surface Name", "entity_id": ent_id}],
    )

    extracted = result["extracted_entities"][0]
    assert extracted["action"] == "provided"
    assert extracted["entity_id"] == ent_id
    eid = result["experience_id"]
    assert (
        _scalar(
            "select count(*) from brain.mentions "
            "where experience_id=%s::uuid and entity_id=%s::uuid",
            [eid, ent_id],
        )
        == 1
    )


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed")
def test_structured_capture_matched_participant_appends_alias():
    name = _unique_name("Exact")
    ent_id = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name, aliases, embedding) "
            "values (%s::uuid, 'person'::brain.entity_kind, %s, array[%s]::text[], %s::vector)",
            [ent_id, name, name, _VEC_LIT],
        )

    result = capture(
        content="saw them again",
        owner=OWNER,
        account_id="household",
        participants=[{"name": name}],
    )

    extracted = result["extracted_entities"][0]
    assert extracted["action"] == "matched"
    assert extracted["entity_id"] == ent_id


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed")
def test_structured_capture_invalid_entity_id_raises_and_rolls_back():
    bogus = str(uuid.uuid4())
    with pytest.raises(ValueError, match="not found or merged"):
        capture(
            content="x",
            owner=OWNER,
            account_id="household",
            participants=[{"name": "Ghost", "entity_id": bogus}],
        )


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed_boom")
def test_capture_embedding_failure_writes_nothing():
    marker = f"no-write-{uuid.uuid4().hex}"
    with pytest.raises(EmbeddingError):
        capture(content=marker, owner=OWNER, account_id="household")
    assert (
        _scalar("select count(*) from brain.experiences where content=%s", [marker])
        == 0
    )
