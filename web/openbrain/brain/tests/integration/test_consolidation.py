# ABOUTME: Integration tests for the consolidation worker against the real brain.* schema.
# ABOUTME: Handler outcomes + a real pg NOTIFY end-to-end.

import json
import threading
import time
import uuid

import pytest
from django.db import connection

from openbrain.brain.consolidation import (
    CONSOLIDATION_CHANNEL,
    CONSOLIDATION_EXTRACTED_BY,
    handle_notification,
    run_consolidation_listener,
)

_VEC_SEED_LIT = "[" + ",".join(["0.05"] * 1536) + "]"

CLAIMS_A = {
    "claims": [
        {
            "subject": "B",
            "subject_kind": "person",
            "predicate": "works_at",
            "predicate_detail": None,
            "object": "Initech Toronto",
            "object_kind": "org",
            "support_kind": "verbatim",
            "confidence": 0.9,
        }
    ]
}


CLAIMS_ANIMAL = {
    "claims": [
        {
            "subject": "Jim",
            "subject_kind": "person",
            "predicate": "other",
            "predicate_detail": "owns",
            "object": "Roger",
            "object_kind": "animal",
            "support_kind": "verbatim",
            "confidence": 0.9,
        }
    ]
}


CLAIMS_BOUND_SUBJECT = {
    "claims": [
        {
            "subject": "Bonnie",
            "subject_kind": "person",
            "predicate": "works_at",
            "predicate_detail": None,
            "object": "Naftiko",
            "object_kind": "org",
            "support_kind": "verbatim",
            "confidence": 0.9,
        }
    ]
}


def _seed_entity(kind, canonical_name):
    """Insert one canonical entity and return its id."""
    entity_id = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name, aliases, embedding) "
            "values (%s::uuid, %s::brain.entity_kind, %s, array[%s]::text[], %s::vector)",
            [entity_id, kind, canonical_name, canonical_name, _VEC_SEED_LIT],
        )
    return entity_id


def _seed_mention(experience_id, entity_id, surface, field="people"):
    """Link an experience to an entity the way _resolve_participants does at capture."""
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.mentions "
            "(experience_id, entity_id, surface_form, field) "
            "values (%s::uuid, %s::uuid, %s, %s)",
            [experience_id, entity_id, surface, field],
        )


def _seed_inprogress(content="B works at Initech Toronto.", attempts=1):
    """Insert one experience already in_progress with the given attempt count.

    The cron proc's claim step is canonical SQL (init/05-consolidation.sql);
    the worker only needs a row in 'in_progress' to drive handle_notification,
    and seeding it directly keeps the test independent of whatever else is pending
    in the shared dev database.
    """
    eid = str(uuid.uuid4())
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.experiences "
            "(id, content, embedding, consolidation_status, consolidation_attempts) "
            "values (%s::uuid, %s, %s::vector, 'in_progress', %s)",
            [eid, content, _VEC_SEED_LIT, attempts],
        )
    return eid


def _status(eid):
    with connection.cursor() as cur:
        cur.execute(
            "select consolidation_status::text, consolidation_attempts "
            "from brain.experiences where id = %s::uuid",
            [eid],
        )
        return cur.fetchone()


def _mock_extract_ok(**_kwargs):
    return CLAIMS_A


def _mock_extract_boom(**_kwargs):
    raise RuntimeError("simulated transient extractor failure")


@pytest.mark.integration
@pytest.mark.usefixtures("brain_write_txn")
class TestHandleNotification:
    def test_writes_claims_and_completes(self):
        eid = _seed_inprogress()

        outcome = handle_notification(eid, extract=_mock_extract_ok)

        assert outcome["status"] == "complete"
        assert outcome["attempts"] == 1
        assert outcome["claims_inserted"] == 1
        assert outcome["claim_sources_inserted"] == 1
        assert _status(eid)[0] == "complete"

        with connection.cursor() as cur:
            cur.execute(
                "select c.predicate, cs.extracted_by "
                "from brain.claims c "
                "join brain.claim_sources cs on cs.claim_id = c.id "
                "where cs.experience_id = %s::uuid",
                [eid],
            )
            claims = cur.fetchall()
        assert len(claims) == 1
        assert claims[0][0] == "works_at"
        assert claims[0][1] == CONSOLIDATION_EXTRACTED_BY

    def test_animal_object_creates_animal_entity(self):
        # #57: an animal-kind object binds to a dedicated 'animal' entity, not a
        # 'person' (which would make a pet a merge-candidate against a same-named
        # person) nor a demoted 'concept' literal. Seed a PERSON 'Roger' first and
        # assert the animal binds to a DISTINCT entity — proving non-collision
        # directly, not just via the kind-scoped SQL.
        person_roger = str(uuid.uuid4())
        with connection.cursor() as cur:
            cur.execute(
                "insert into brain.entities (id, kind, canonical_name) "
                "values (%s::uuid, 'person'::brain.entity_kind, 'Roger')",
                [person_roger],
            )
        eid = _seed_inprogress(content="Jim's dog Roger.")

        outcome = handle_notification(eid, extract=lambda **_k: CLAIMS_ANIMAL)

        assert outcome["status"] == "complete"
        with connection.cursor() as cur:
            cur.execute(
                "select e.id::text, e.kind::text, e.canonical_name "
                "from brain.claims c "
                "join brain.entities e on e.id = c.object_entity_id "
                "join brain.claim_sources cs on cs.claim_id = c.id "
                "where cs.experience_id = %s::uuid",
                [eid],
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        object_id, kind, name = rows[0]
        assert (kind, name) == ("animal", "Roger")
        assert object_id != person_roger  # the pet did NOT bind to the person

    def test_claim_subject_reuses_the_capture_binding(self):
        # #73: the capture resolved "Bonnie Ravina" and wrote a mention for it.
        # The claim pass must consume that binding rather than re-resolving the
        # shortened surface the extractor emitted — re-resolution scores "Bonnie"
        # against the canonical below the reuse threshold and mints a fork.
        bonnie = _seed_entity("person", "Bonnie Ravina")
        eid = _seed_inprogress(content="Bonnie works at Naftiko.")
        _seed_mention(eid, bonnie, "Bonnie Ravina")

        outcome = handle_notification(eid, extract=lambda **_k: CLAIMS_BOUND_SUBJECT)

        assert outcome["status"] == "complete"
        with connection.cursor() as cur:
            cur.execute(
                "select c.subject_id::text "
                "from brain.claims c "
                "join brain.claim_sources cs on cs.claim_id = c.id "
                "where cs.experience_id = %s::uuid",
                [eid],
            )
            subjects = cur.fetchall()
            # No fork: the shortened surface did not become its own person.
            cur.execute(
                "select count(*) from brain.entities "
                "where kind = 'person'::brain.entity_kind and canonical_name = 'Bonnie'"
            )
            forks = cur.fetchone()[0]

        assert subjects == [(bonnie,)]
        assert forks == 0

    def test_unbound_borderline_surface_leaves_no_live_fork(self):
        # #73: "Jon Smith" against an existing "John Smith" is the borderline band.
        # The claim path used to mint a silent duplicate here — no merge candidate,
        # no queue entry, and name_matching skips identical-name pairs, so nothing
        # downstream would ever have caught it. It now goes through the shared
        # resolver, which leaves an audited trail either way.
        john = _seed_entity("person", "John Smith")
        claims = {
            "claims": [
                {
                    "subject": "Jon Smith",
                    "subject_kind": "person",
                    "predicate": "works_at",
                    "predicate_detail": None,
                    "object": "Initech",
                    "object_kind": "org",
                    "support_kind": "verbatim",
                    "confidence": 0.9,
                }
            ]
        }
        eid = _seed_inprogress(content="Jon Smith works at Initech.")

        outcome = handle_notification(eid, extract=lambda **_k: claims)

        assert outcome["status"] == "complete"
        with connection.cursor() as cur:
            cur.execute(
                "select c.subject_id::text "
                "from brain.claims c "
                "join brain.claim_sources cs on cs.claim_id = c.id "
                "where cs.experience_id = %s::uuid",
                [eid],
            )
            subject_id = cur.fetchone()[0]
            # Any entity minted for the near-miss surface is merged away, not left
            # standing as a second John Smith.
            cur.execute(
                "select count(*) from brain.entities "
                "where kind = 'person'::brain.entity_kind "
                "and canonical_name = 'Jon Smith' and merged_into is null"
            )
            live_forks = cur.fetchone()[0]
            # And the decision is auditable: a merge candidate for the pair, or a
            # queued question if the resolver was not confident enough to merge.
            cur.execute(
                "select count(*) from brain.merge_candidates "
                "where entity_a = %s::uuid or entity_b = %s::uuid",
                [john, john],
            )
            candidates = cur.fetchone()[0]
            cur.execute(
                "select count(*) from brain.disambiguations "
                "where status = 'pending' "
                "and context->>'provisional_entity_id' = %s",
                [john],
            )
            queued = cur.fetchone()[0]

        assert subject_id == john
        assert live_forks == 0
        assert candidates + queued >= 1

    def test_first_person_claim_creates_no_junk_entity(self):
        # #56: "I met Jim" must NOT mint a person entity named "I". The self claim
        # is dropped; the co-mentioned third party ("Jim") is still written.
        claims = {
            "claims": [
                {
                    "subject": "I",
                    "subject_kind": "person",
                    "predicate": "knows",
                    "predicate_detail": None,
                    "object": "Jim",
                    "object_kind": "person",
                    "support_kind": "verbatim",
                    "confidence": 0.9,
                },
                {
                    "subject": "Jim",
                    "subject_kind": "person",
                    "predicate": "works_at",
                    "predicate_detail": None,
                    "object": "Acme",
                    "object_kind": "org",
                    "support_kind": "verbatim",
                    "confidence": 0.9,
                },
            ]
        }
        eid = _seed_inprogress(content="I met Jim who works at Acme.")

        outcome = handle_notification(eid, extract=lambda **_k: claims)

        assert outcome["status"] == "complete"
        assert outcome["claims_inserted"] == 1  # only the (Jim, works_at, Acme) claim
        assert outcome["claims_skipped_self"] == 1  # (I, knows, Jim) dropped

        with connection.cursor() as cur:
            cur.execute(
                "select count(*) from brain.entities "
                "where kind = 'person'::brain.entity_kind and lower(canonical_name) = 'i'"
            )
            assert cur.fetchone()[0] == 0
            # The legitimate third-party claim survived.
            cur.execute(
                "select c.predicate "
                "from brain.claims c "
                "join brain.claim_sources cs on cs.claim_id = c.id "
                "where cs.experience_id = %s::uuid",
                [eid],
            )
            assert cur.fetchall() == [("works_at",)]

    def test_skips_when_not_in_progress(self):
        # A bare 'pending' row was never claimed by the cron proc.
        eid = str(uuid.uuid4())
        with connection.cursor() as cur:
            cur.execute(
                "insert into brain.experiences (id, content, embedding) "
                "values (%s::uuid, %s, %s::vector)",
                [eid, "still pending — never consolidated", _VEC_SEED_LIT],
            )
        called = {"n": 0}

        def _spy(**_kwargs):
            called["n"] += 1
            return CLAIMS_A

        outcome = handle_notification(eid, extract=_spy)

        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "not_in_progress"
        assert called["n"] == 0

    def test_failure_with_retries_left_resets_to_pending(self):
        eid = _seed_inprogress(attempts=1)

        outcome = handle_notification(eid, extract=_mock_extract_boom)

        assert outcome["status"] == "pending"
        assert outcome["attempts"] == 1
        assert "simulated transient" in outcome["error"]
        assert _status(eid)[0] == "pending"

    def test_failure_at_cap_marks_failed(self):
        eid = _seed_inprogress(attempts=3)

        outcome = handle_notification(eid, extract=_mock_extract_boom)

        assert outcome["status"] == "failed"
        assert outcome["attempts"] == 3
        assert _status(eid)[0] == "failed"


def _cleanup(eid, names):
    """Delete every row the end-to-end test created (it commits, so no rollback)."""
    with connection.cursor() as cur:
        cur.execute(
            "select c.id::text from brain.claims c "
            "join brain.claim_sources cs on cs.claim_id = c.id "
            "where cs.experience_id = %s::uuid",
            [eid],
        )
        claim_ids = [r[0] for r in cur.fetchall()]
        cur.execute(
            "delete from brain.claim_sources where experience_id = %s::uuid", [eid]
        )
        if claim_ids:
            cur.execute(
                "delete from brain.claims where id = any(%s::uuid[])", [claim_ids]
            )
        cur.execute("delete from brain.mentions where experience_id = %s::uuid", [eid])
        # Names are unique per run, so this only ever matches entities this test
        # created — never a real dev entity.
        cur.execute(
            "delete from brain.entities where canonical_name = any(%s)", [names]
        )
        cur.execute("delete from brain.experiences where id = %s::uuid", [eid])


@pytest.mark.integration
def test_real_notify_end_to_end(brain_db):
    """A real pg NOTIFY drains through a live LISTEN worker to written claims.

    Unlike the handler tests this CANNOT use the rollback fixture: NOTIFY is
    delivered only on commit and the worker's dedicated LISTEN connection won't
    see an uncommitted row. So it commits real rows and cleans them up in finally.
    It emits the NOTIFY directly for its own id (not via the cron proc) so it
    never marks unrelated dev rows in_progress, and the worker's handler ignores
    any id that isn't ours in case a real cron tick fires mid-test.
    """
    eid = str(uuid.uuid4())
    token = uuid.uuid4().hex[:8]
    subj = f"ITEST-SUBJ-{token}"
    obj = f"ITEST-OBJ-{token}"
    claims = {
        "claims": [
            {
                "subject": subj,
                "subject_kind": "person",
                "predicate": "works_at",
                "predicate_detail": None,
                "object": obj,
                "object_kind": "org",
                "support_kind": "verbatim",
                "confidence": 0.9,
            }
        ]
    }

    def _handle(experience_id):
        if experience_id != eid:  # ignore stray cron NOTIFYs for other dev rows
            return
        handle_notification(experience_id, extract=lambda **_kw: claims)

    ready = threading.Event()
    stop = threading.Event()

    def _run():
        try:
            run_consolidation_listener(
                handle=_handle,
                should_stop=stop.is_set,
                on_ready=ready.set,
                reconnect_delay=0.2,
            )
        finally:
            connection.close()  # this thread's Django work connection

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    try:
        assert ready.wait(timeout=10), "worker never reached LISTEN"

        # Seed the row already in_progress (what the cron proc would do) and emit
        # the same payload shape the proc emits — both commit (autocommit), so the
        # NOTIFY reaches the now-listening worker.
        with connection.cursor() as cur:
            cur.execute(
                "insert into brain.experiences "
                "(id, content, embedding, consolidation_status, consolidation_attempts) "
                "values (%s::uuid, %s, %s::vector, 'in_progress', 1)",
                [eid, f"{subj} works at {obj}.", _VEC_SEED_LIT],
            )
            cur.execute(
                "select pg_notify(%s, %s)",
                [CONSOLIDATION_CHANNEL, json.dumps({"experience_id": eid})],
            )

        deadline = time.time() + 15
        final = None
        while time.time() < deadline:
            final = _status(eid)[0]
            if final == "complete":
                break
            time.sleep(0.1)
        assert final == "complete", f"row never consolidated (status={final})"

        with connection.cursor() as cur:
            cur.execute(
                "select count(*) from brain.claims c "
                "join brain.claim_sources cs on cs.claim_id = c.id "
                "where cs.experience_id = %s::uuid",
                [eid],
            )
            assert cur.fetchone()[0] == 1
    finally:
        stop.set()
        worker.join(timeout=5)
        _cleanup(eid, [subj, obj])
