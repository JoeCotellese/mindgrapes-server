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
from openbrain.brain.services.reviews import resolve_disambiguation, review_queue

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


# --- #8: capture-then-reconcile with provisional participant bindings ----------
#
# similarity('Vorptangle','Zorptangle') ≈ 0.57 lands in the 0.55-0.85 borderline
# band; a single-token person pair verifies at 0.0 (< 0.92), so the surface is
# provisionally bound to the best guess and a disambiguation token is opened —
# the exact shape the capture-then-reconcile contract exists to produce. Names
# are distinctive so they don't collide with the shared dev brain, and the
# existing entity carries no embedding so the vec channel doesn't dominate.


def _seed_person(canonical_name):
    ent_id = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name, aliases) "
            "values (%s::uuid, 'person'::brain.entity_kind, %s, array[%s]::text[])",
            [ent_id, canonical_name, canonical_name],
        )
    return ent_id


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed")
def test_structured_capture_borderline_binds_provisional_and_returns_token():
    existing = _seed_person("Zorptangle")

    result = capture(
        content="ran into them",
        owner=OWNER,
        account_id="household",
        participants=[{"name": "Vorptangle"}],
    )

    extracted = result["extracted_entities"][0]
    assert extracted["action"] == "provisional"
    assert extracted["provisional"] is True
    # Bound to the best guess (the existing entity), not a fresh duplicate.
    assert extracted["entity_id"] == existing
    assert (
        _scalar(
            "select count(*) from brain.entities where canonical_name=%s",
            ["Vorptangle"],
        )
        == 0
    )

    # The mention is linked provisionally to the best-guess entity.
    eid = result["experience_id"]
    assert (
        _scalar(
            "select count(*) from brain.mentions "
            "where experience_id=%s::uuid and entity_id=%s::uuid",
            [eid, existing],
        )
        == 1
    )

    # A needs_disambiguation block carries the candidate ids + a token.
    assert len(result["needs_disambiguation"]) == 1
    block = result["needs_disambiguation"][0]
    assert block["surface"] == "Vorptangle"
    assert block["provisional_entity_id"] == existing
    assert block["candidate_entity_ids"] == [existing]
    token = block["token"]
    assert len(block["options"]) == 2

    # The provisional bind surfaces in review_queue's disambiguations lane.
    q = review_queue("disambiguations")
    assert token in {r["token"] for r in q["disambiguations"]}


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed")
def test_structured_capture_confirm_reconciles_and_keeps_binding():
    existing = _seed_person("Zorptangle")
    result = capture(
        content="saw them",
        owner=OWNER,
        account_id="household",
        participants=[{"name": "Vorptangle"}],
    )
    eid = result["experience_id"]
    token = result["needs_disambiguation"][0]["token"]

    # Confirm (option 0 = "Same as Zorptangle").
    res = resolve_disambiguation(token, 0)
    assert res["reconciliation"]["action"] == "confirmed"
    assert res["reconciliation"]["entity_id"] == existing

    # The bind stands: the mention still points at the best-guess entity…
    assert (
        _scalar(
            "select count(*) from brain.mentions "
            "where experience_id=%s::uuid and entity_id=%s::uuid",
            [eid, existing],
        )
        == 1
    )
    # …and the token drops out of the review queue.
    assert (
        _scalar(
            "select status from brain.disambiguations where token=%s::uuid", [token]
        )
        == "resolved"
    )
    q = review_queue("disambiguations")
    assert token not in {r["token"] for r in q["disambiguations"]}


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed")
def test_structured_capture_reject_reconciles_and_repoints_mention():
    existing = _seed_person("Zorptangle")
    result = capture(
        content="met them",
        owner=OWNER,
        account_id="household",
        participants=[{"name": "Vorptangle"}],
    )
    eid = result["experience_id"]
    token = result["needs_disambiguation"][0]["token"]

    # Reject (option 1 = "Different — Vorptangle is another person").
    res = resolve_disambiguation(token, 1)
    assert res["reconciliation"]["action"] == "repointed"
    assert res["reconciliation"]["mentions_repointed"] == 1
    target = res["reconciliation"]["target_entity_id"]
    assert target != existing

    # The mention now points at the fresh entity, not the best guess.
    assert (
        _scalar(
            "select count(*) from brain.mentions "
            "where experience_id=%s::uuid and entity_id=%s::uuid",
            [eid, existing],
        )
        == 0
    )
    assert (
        _scalar(
            "select count(*) from brain.mentions "
            "where experience_id=%s::uuid and entity_id=%s::uuid",
            [eid, target],
        )
        == 1
    )
    assert (
        _scalar("select canonical_name from brain.entities where id=%s::uuid", [target])
        == "Vorptangle"
    )
    assert (
        _scalar(
            "select status from brain.disambiguations where token=%s::uuid", [token]
        )
        == "resolved"
    )


def _token_for(result, surface):
    for block in result["needs_disambiguation"]:
        if block["surface"] == surface:
            return block["token"]
    raise AssertionError(f"no disambiguation block for surface {surface!r}")


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed")
def test_structured_capture_reject_repoints_only_the_provisional_mention():
    # Two surfaces provisional-bind to the SAME best-guess entity in one capture
    # (both trgm ≈ 0.57 to the seed). Rejecting one guess must unbind only that
    # mention and leave the sibling on the best-guess entity — not sweep the whole
    # entity-in-experience the way an experience-scoped split would.
    existing = _seed_person("Zorptangle")
    result = capture(
        content="ran into both of them",
        owner=OWNER,
        account_id="household",
        participants=[{"name": "Vorptangle"}, {"name": "Worptangle"}],
    )
    eid = result["experience_id"]
    assert all(e["action"] == "provisional" for e in result["extracted_entities"])
    # Both mentions landed on the best-guess entity.
    assert (
        _scalar(
            "select count(*) from brain.mentions where experience_id=%s::uuid "
            "and entity_id=%s::uuid",
            [eid, existing],
        )
        == 2
    )

    res = resolve_disambiguation(_token_for(result, "Vorptangle"), 1)
    assert res["reconciliation"]["action"] == "repointed"
    assert res["reconciliation"]["mentions_repointed"] == 1
    target = res["reconciliation"]["target_entity_id"]

    # The rejected surface moved onto a fresh entity…
    assert (
        _scalar(
            "select count(*) from brain.mentions where experience_id=%s::uuid "
            "and entity_id=%s::uuid and surface_form=%s",
            [eid, target, "Vorptangle"],
        )
        == 1
    )
    # …and the co-mentioned sibling is UNTOUCHED on the best-guess entity.
    assert (
        _scalar(
            "select count(*) from brain.mentions where experience_id=%s::uuid "
            "and entity_id=%s::uuid",
            [eid, existing],
        )
        == 1
    )
    assert (
        _scalar(
            "select surface_form from brain.mentions where experience_id=%s::uuid "
            "and entity_id=%s::uuid",
            [eid, existing],
        )
        == "Worptangle"
    )
    # The sibling's token is still pending for its own decision.
    q = review_queue("disambiguations")
    assert _token_for(result, "Worptangle") in {
        r["token"] for r in q["disambiguations"]
    }


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed")
def test_structured_capture_reject_leaves_explicitly_provided_mention():
    # A provided (non-provisional) participant and a provisional sibling both bind
    # to the same entity. Rejecting the provisional guess must never drag the
    # explicitly-provided mention off the entity the caller pinned.
    existing = _seed_person("Zorptangle")
    result = capture(
        content="the two of them",
        owner=OWNER,
        account_id="household",
        participants=[
            {"name": "Zorptangle", "entity_id": existing},
            {"name": "Vorptangle"},
        ],
    )
    eid = result["experience_id"]
    provided = next(
        e for e in result["extracted_entities"] if e["action"] == "provided"
    )
    assert provided["provisional"] is False
    assert (
        _scalar(
            "select count(*) from brain.mentions where experience_id=%s::uuid "
            "and entity_id=%s::uuid",
            [eid, existing],
        )
        == 2
    )

    resolve_disambiguation(_token_for(result, "Vorptangle"), 1)

    # The provided mention stays pinned to the entity the caller chose.
    assert (
        _scalar(
            "select count(*) from brain.mentions where experience_id=%s::uuid "
            "and entity_id=%s::uuid",
            [eid, existing],
        )
        == 1
    )
    assert (
        _scalar(
            "select surface_form from brain.mentions where experience_id=%s::uuid "
            "and entity_id=%s::uuid",
            [eid, existing],
        )
        == "Zorptangle"
    )


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed")
def test_structured_capture_confirm_appends_alias_so_next_capture_reuses():
    # Confirming a provisional bind must teach the resolver: append the surface as
    # an alias so the NEXT capture of it strong-matches and reuses the entity
    # instead of re-opening a disambiguation.
    existing = _seed_person("Zorptangle")
    first = capture(
        content="saw them once",
        owner=OWNER,
        account_id="household",
        participants=[{"name": "Vorptangle"}],
    )
    token = first["needs_disambiguation"][0]["token"]

    res = resolve_disambiguation(token, 0)
    assert res["reconciliation"]["action"] == "confirmed"
    assert res["reconciliation"]["alias_appended"] is True
    assert "Vorptangle" in _scalar(
        "select aliases from brain.entities where id=%s::uuid", [existing]
    )

    # The next capture of that surface now strong-matches — no fresh guess.
    second = capture(
        content="saw them again",
        owner=OWNER,
        account_id="household",
        participants=[{"name": "Vorptangle"}],
    )
    extracted = second["extracted_entities"][0]
    assert extracted["action"] == "matched"
    assert extracted["provisional"] is False
    assert extracted["entity_id"] == existing
    assert second["needs_disambiguation"] == []


@override_settings(BRAIN_EMBED_FN=f"{_MOD}._embed")
def test_structured_capture_confident_pair_auto_merges_non_provisionally():
    # #16 interaction: a borderline pair that clears match_score >= 0.92 is
    # auto-merged and bound NON-provisionally — no needs_disambiguation block.
    existing = _seed_person("Jon Zorptangle")

    result = capture(
        content="talked with them",
        owner=OWNER,
        account_id="household",
        participants=[{"name": "John Zorptangle"}],
    )

    extracted = result["extracted_entities"][0]
    assert extracted["action"] == "auto_merged"
    assert extracted["provisional"] is False
    assert extracted["entity_id"] == existing  # links to the surviving entity
    assert result["needs_disambiguation"] == []
