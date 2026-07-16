"""Read-service integration tests against the real brain.* schema.

Requires the dev stack up (make dev-up); run via make dev-test-integration. These
exercise the raw SQL against the brain.* schema so column / enum / function
drift — owner, visibility, and match_brain_hybrid's p_viewer arg — surfaces here
even against an empty database. The unit suite covers the row→view-model
transforms; this is the drift contract.
"""

import uuid

import pytest
from django.db import connection
from django.test import override_settings

from openbrain.brain.services.reads import (
    get_entity_detail,
    get_entity_mentions,
    get_experience_detail,
    get_summary,
    search_experiences,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_db")]

VIEWER = "1"
OTHER = "itest-other"

# All-positive unit vector satisfies the vector(1536) NOT NULL column on seeds.
_VEC_SEED_LIT = "[" + ",".join(["0.05"] * 1536) + "]"


def _seed_experience(owner=VIEWER, visibility="private", content="seed content"):
    eid = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.experiences (id, content, embedding, owner, visibility) "
            "values (%s::uuid, %s, %s::vector, %s, %s::brain.visibility)",
            [eid, content, _VEC_SEED_LIT, owner, visibility],
        )
    return eid


def _seed_mention_and_claim(experience_id):
    """Attach one entity mention plus one claim sourced from the experience."""
    entity_id = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name) "
            "values (%s::uuid, 'person'::brain.entity_kind, %s)",
            [entity_id, f"itest-entity-{entity_id[:8]}"],
        )
        cur.execute(
            "insert into brain.mentions (experience_id, entity_id, surface_form, field) "
            "values (%s::uuid, %s::uuid, %s, 'people')",
            [experience_id, entity_id, "Grace"],
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
    return entity_id, claim_id


def _zero_embed(text):
    # A constant 1536-dim vector satisfies the vector(1536) cast; relevance is
    # beside the point here — we only assert the hybrid SQL runs against the
    # real function with the viewer filter.
    return [0.001] * 1536


def test_get_summary_returns_cache_row():
    summary = get_summary()
    assert summary is not None
    assert "experience_count" in summary
    assert isinstance(summary["top_entities"], list)
    assert isinstance(summary["top_topics"], list)


def test_get_experience_detail_unknown_id_is_none():
    # Exercises the experience SQL (owner / visibility columns) and the privacy
    # gate against the real schema.
    assert get_experience_detail(VIEWER, str(uuid.uuid4())) is None


@pytest.mark.usefixtures("brain_write_txn")
def test_get_experience_detail_returns_content_mentions_claims():
    # The fetch half of the search/fetch pair (#149): a seeded capture comes back
    # in full — content, its mention, and the claim sourced from it — and a live
    # row is flagged is_live.
    eid = _seed_experience(content="met Grace at the the accelerator demo")
    entity_id, claim_id = _seed_mention_and_claim(eid)

    detail = get_experience_detail(VIEWER, eid)

    assert detail is not None
    assert detail["experience"]["content"] == "met Grace at the the accelerator demo"
    assert detail["experience"]["is_live"] is True
    assert [m["entity_id"] for m in detail["mentions"]] == [entity_id]
    assert [c["claim_id"] for c in detail["claims_sourced_here"]] == [claim_id]


@pytest.mark.usefixtures("brain_write_txn")
def test_get_experience_detail_private_non_owner_is_none():
    # Privacy contract: a private experience owned by someone else returns the
    # same None as a missing id — existence is never leaked.
    eid = _seed_experience(owner=OTHER, visibility="private")
    assert get_experience_detail(VIEWER, eid) is None


def test_get_entity_detail_unknown_id_is_none():
    assert get_entity_detail(VIEWER, str(uuid.uuid4()), 50, 0) is None


def test_get_entity_mentions_unknown_id_is_empty_page():
    page = get_entity_mentions(VIEWER, str(uuid.uuid4()), 50, 0)
    assert page["mention_count"] == 0
    assert page["mentions"] == []
    assert page["has_more"] is False


@override_settings(
    BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_brain_reads._zero_embed"
)
def test_search_runs_hybrid_with_viewer_filter():
    # Exercises match_brain_hybrid's p_viewer named arg + the ::vector cast.
    results = search_experiences(VIEWER, "anything", 10)
    assert isinstance(results, list)
