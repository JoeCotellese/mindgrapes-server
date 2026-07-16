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
