"""Integration tests for the activity-log read service against real brain.*.

Seeds correction_events (a UI supersede, a worker claim-retract, an entity merge)
plus the records they target, all stamped with far-future created_at so they sort
to the top of the shared dev database; asserts the service's change-type derivation,
actor classification, target/survivor links, before/after diff, and newest-first
ordering, then brain_write_txn rolls the whole transaction back. This is the drift
contract for the new SQL (the target LEFT JOINs and the merged_into resolution).

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import json
import uuid

import pytest
from django.db import connection

from openbrain.brain.services.activity import get_activity

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

# Far future so seeded rows sort above any pre-existing correction_events in the
# shared dev database; tests assert on membership/relative order of these rows.
FAR_MERGE = "2999-01-03T00:00:00+00:00"
FAR_WORKER = "2999-01-02T00:00:00+00:00"
FAR_SUPERSEDE = "2999-01-01T00:00:00+00:00"


def _seed_experience(content="itest activity experience"):
    eid = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.experiences (id, content, owner, visibility) "
            "values (%s::uuid, %s, %s, 'private'::brain.visibility)",
            [eid, content, "itest-activity"],
        )
    return eid


def _seed_entity(name, merged_into=None):
    ent_id = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name, merged_into) "
            "values (%s::uuid, 'org'::brain.entity_kind, %s, %s::uuid)",
            [ent_id, name, merged_into],
        )
    return ent_id


def _seed_claim(subject_id, predicate="works_at"):
    claim_id = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.claims (id, subject_id, predicate) "
            "values (%s::uuid, %s::uuid, %s)",
            [claim_id, subject_id, predicate],
        )
    return claim_id


def _seed_correction(target_kind, target_id, before, after, reason, created_by, when):
    cid = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            """
            insert into brain.correction_events
              (id, target_kind, target_id, before, after, reason, created_at, created_by)
            values (%s::uuid, %s::brain.target_kind, %s::uuid, %s::jsonb, %s::jsonb,
                    %s, %s::timestamptz, %s)
            """,
            [
                cid,
                target_kind,
                target_id,
                json.dumps(before),
                json.dumps(after),
                reason,
                when,
                created_by,
            ],
        )
    return cid


def _event(page, event_id):
    return next(ev for ev in page["events"] if ev["id"] == event_id)


def test_supersede_event_typed_attributed_and_diffed():
    exp_id = _seed_experience(content="a thought worth keeping")
    new_id = _seed_experience(content="the revised thought")
    ce_id = _seed_correction(
        "experience",
        exp_id,
        before={"content": "a thought worth keeping", "superseded_by": None},
        after={"content": "the revised thought", "superseded_by": new_id},
        reason="supersede (cosine 0.50)",
        created_by="ui-session:itest-activity",
        when=FAR_SUPERSEDE,
    )

    ev = _event(get_activity(100, 0), ce_id)

    assert ev["change_type"] == "supersede"
    assert ev["change_label"] == "Superseded"
    assert ev["target"]["href"] == f"/experience/{exp_id}"
    assert ev["target"]["label"] == "a thought worth keeping"
    assert ev["actor"]["kind"] == "human"
    assert ev["actor"]["is_auto"] is False
    # Before/after preserved for the expander.
    assert ev["has_diff"] is True
    assert ev["before"]["content"] == "a thought worth keeping"
    assert ev["after"]["content"] == "the revised thought"


def test_worker_claim_retract_is_auto_and_labeled():
    subject = _seed_entity("Acme Corp")
    claim_id = _seed_claim(subject, predicate="works_at")
    ce_id = _seed_correction(
        "claim",
        claim_id,
        before={"polarity": "asserted"},
        after={"polarity": "retracted"},
        reason="auto-retract: source experience superseded",
        created_by="consolidation",
        when=FAR_WORKER,
    )

    ev = _event(get_activity(100, 0), ce_id)

    assert ev["change_type"] == "retract"
    assert ev["change_label"] == "Retracted"
    assert ev["actor"]["kind"] == "consolidation"
    assert ev["actor"]["is_auto"] is True
    # Claim has no detail route; label resolves predicate (and subject).
    assert ev["target"]["href"] is None
    assert "works_at" in ev["target"]["label"]
    assert "Acme Corp" in ev["target"]["label"]


def test_merge_event_links_source_and_survivor():
    winner = _seed_entity("Acme Inc")
    source = _seed_entity("Acme", merged_into=winner)
    ce_id = _seed_correction(
        "entity",
        source,
        before={"canonical_name": "Acme", "merged_into": None},
        after={"merged_into": winner},
        reason="duplicate",
        created_by="mcp:merge_entities",
        when=FAR_MERGE,
    )

    ev = _event(get_activity(100, 0), ce_id)

    assert ev["change_type"] == "merge"
    assert ev["change_label"] == "Merged"
    assert ev["target"]["href"] == f"/entity/{source}"
    assert ev["target"]["label"] == "Acme"
    assert ev["secondary"]["href"] == f"/entity/{winner}"
    assert ev["secondary"]["label"] == "Acme Inc"


def test_events_ordered_newest_first():
    exp_id = _seed_experience()
    supersede = _seed_correction(
        "experience",
        exp_id,
        before={"superseded_by": None},
        after={"superseded_by": exp_id},
        reason="supersede",
        created_by="ui-session:itest-activity",
        when=FAR_SUPERSEDE,
    )
    subject = _seed_entity("Order Co")
    claim_id = _seed_claim(subject)
    worker = _seed_correction(
        "claim",
        claim_id,
        before={"polarity": "asserted"},
        after={"polarity": "retracted"},
        reason="auto-retract",
        created_by="consolidation",
        when=FAR_WORKER,
    )
    winner = _seed_entity("Order Co Inc")
    source = _seed_entity("Order Co Dup", merged_into=winner)
    merge = _seed_correction(
        "entity",
        source,
        before={"merged_into": None},
        after={"merged_into": winner},
        reason="duplicate",
        created_by="mcp:merge_entities",
        when=FAR_MERGE,
    )

    seeded = {supersede, worker, merge}
    order = [ev["id"] for ev in get_activity(100, 0)["events"] if ev["id"] in seeded]

    # created_at desc: merge (01-03) > worker (01-02) > supersede (01-01).
    assert order == [merge, worker, supersede]
