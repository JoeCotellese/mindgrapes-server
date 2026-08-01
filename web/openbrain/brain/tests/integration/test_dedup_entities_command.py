# ABOUTME: Integration tests for `manage.py dedup_entities --apply` against brain.*.
# ABOUTME: Covers #95 — the batch merge leg must not override a human's verdict.
"""The dedup command's merge leg versus prior human review decisions (#95).

`--apply` auto-merges every pair at/above AUTO_MERGE_THRESHOLD. It used to do
that without consulting brain.merge_candidates, so a pair a reviewer had already
rejected was re-merged on the next run — silently, because merge_entities only
restamps rows `where status = 'pending'`, and a `kept_separate` row does not
match. Nothing logged that a decision had been overridden.

These tests run the real command inside brain_write_txn, so the shared dev
database is never mutated.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import uuid

import pytest
from django.core.management import call_command
from django.db import connection

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]


def _new_id():
    return str(uuid.uuid4())


def _seed_entity(canonical_name, kind="org", merged_into=None):
    eid = _new_id()
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name, aliases, merged_into) "
            "values (%s::uuid, %s::brain.entity_kind, %s, '{}'::text[], %s::uuid)",
            [eid, kind, canonical_name, merged_into],
        )
    return eid


def _seed_candidate(a, b, status):
    lo, hi = sorted([a, b])
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.merge_candidates (id, entity_a, entity_b, similarity, status) "
            "values (%s::uuid, %s::uuid, %s::uuid, %s, %s)",
            [_new_id(), lo, hi, 1.0, status],
        )


def _merged_into(entity_id):
    with connection.cursor() as cur:
        cur.execute(
            "select merged_into::text from brain.entities where id = %s::uuid",
            [entity_id],
        )
        return cur.fetchone()[0]


def _correction_count(entity_id):
    with connection.cursor() as cur:
        cur.execute(
            "select count(*) from brain.correction_events "
            "where target_kind = 'entity' and target_id = %s::uuid",
            [entity_id],
        )
        return cur.fetchone()[0]


def _twin_orgs():
    """Two live orgs sharing a canonical name — verification 1.0, above
    AUTO_MERGE_THRESHOLD (0.92), so the planner always proposes this merge."""
    name = f"Zzdedup {uuid.uuid4().hex[:10]} Holdings"
    return _seed_entity(name), _seed_entity(name)


def _run_apply():
    call_command("dedup_entities", "--kind", "org", "--apply", verbosity=0)


def test_kept_separate_pair_is_not_remerged_by_apply():
    a, b = _twin_orgs()
    _seed_candidate(a, b, "kept_separate")

    _run_apply()

    assert _merged_into(a) is None
    assert _merged_into(b) is None
    # Nothing was written at all — not merged and then reverted.
    assert _correction_count(a) == 0
    assert _correction_count(b) == 0


def test_skipped_pair_is_not_remerged_by_apply():
    # A skipped row is a deferral, which is a fine thing for a human draining the
    # queue to revisit. An unattended batch cannot see the row or change its mind,
    # so for this path 'not pending' means 'not the batch's call'.
    a, b = _twin_orgs()
    _seed_candidate(a, b, "skipped")

    _run_apply()

    assert _merged_into(a) is None
    assert _merged_into(b) is None


def test_kept_separate_survives_one_side_being_merged_away():
    # The stored pair key goes stale: a verdict on (A, B) stops matching once B
    # merges into C, and the live pair the planner proposes is (A, C). Resolving
    # both endpoints through coalesce(merged_into, id) keeps the decision binding.
    a, b = _twin_orgs()
    _seed_candidate(a, b, "kept_separate")
    c = _seed_entity("Zzdedup Successor Group")
    with connection.cursor() as cur:
        cur.execute(
            "update brain.entities set merged_into = %s::uuid where id = %s::uuid",
            [c, b],
        )
        # C inherits B's name so the planner still proposes the A/C pair at 1.0.
        cur.execute(
            "update brain.entities set canonical_name = "
            "(select canonical_name from brain.entities where id = %s::uuid) "
            "where id = %s::uuid",
            [a, c],
        )

    _run_apply()

    assert _merged_into(a) is None, "the (A,B) verdict stopped protecting (A,C)"
    assert _merged_into(c) is None


def test_pending_pair_is_still_merged_by_apply():
    # The guard must not disarm the command: a pending row is exactly what the
    # batch is for.
    a, b = _twin_orgs()
    _seed_candidate(a, b, "pending")

    _run_apply()

    winners = {_merged_into(a), _merged_into(b)}
    assert winners == {None, a} or winners == {None, b}, (
        f"expected exactly one of the pair to survive, got {winners}"
    )


def test_pair_with_no_candidate_row_is_still_merged_by_apply():
    a, b = _twin_orgs()

    _run_apply()

    winners = {_merged_into(a), _merged_into(b)}
    assert winners == {None, a} or winners == {None, b}
