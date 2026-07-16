"""Integration tests for the review/correction/disambiguation services (#120).

Seeds queue rows, runs a service, asserts the brain.* effect; brain_write_txn
rolls everything back. review_queue is not viewer-scoped, so assertions check
seeded ids are present rather than asserting exact counts.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import uuid

import pytest
from django.db import connection

from openbrain.brain.services.mcp_reads import pending_reviews
from openbrain.brain.services.reviews import (
    attach_entity_names,
    merge_candidate_evidence,
    propose_correction,
    request_disambiguation,
    resolve_correction,
    resolve_disambiguation,
    resolve_merge_candidate,
    review_queue,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

_VEC = "[" + ",".join(["0.01"] * 1536) + "]"


def _new_id():
    return str(uuid.uuid4())


def _seed_entity(kind="person", canonical_name=None):
    eid = _new_id()
    name = canonical_name or f"itest-{eid[:8]}"
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name) "
            "values (%s::uuid, %s::brain.entity_kind, %s)",
            [eid, kind, name],
        )
    return eid


def _seed_experience(content="seed", owner="itest", visibility="private"):
    eid = _new_id()
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.experiences (id, content, embedding, owner, visibility) "
            "values (%s::uuid, %s, %s::vector, %s, %s::brain.visibility)",
            [eid, content, _VEC, owner, visibility],
        )
    return eid


def _seed_claim(
    subject_id,
    *,
    object_entity_id=None,
    predicate="relates_to",
    polarity="asserted",
    confidence=None,
    superseded_by=None,
):
    cid = _new_id()
    cols = ["id", "subject_id", "object_entity_id", "predicate", "polarity"]
    vals = ["%s::uuid", "%s::uuid", "%s::uuid", "%s", "%s::brain.polarity"]
    params = [cid, subject_id, object_entity_id, predicate, polarity]
    if confidence is not None:
        cols.append("confidence")
        vals.append("%s")
        params.append(confidence)
    if superseded_by is not None:
        cols.append("superseded_by")
        vals.append("%s::uuid")
        params.append(superseded_by)
    with connection.cursor() as cur:
        cur.execute(
            f"insert into brain.claims ({', '.join(cols)}) values ({', '.join(vals)})",
            params,
        )
    return cid


def _seed_claim_source(claim_id, experience_id, support_kind="verbatim"):
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.claim_sources (claim_id, experience_id, support_kind) "
            "values (%s::uuid, %s::uuid, %s::brain.support_kind)",
            [claim_id, experience_id, support_kind],
        )


def _seed_mention(experience_id, entity_id, surface_form, field="people"):
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.mentions (experience_id, entity_id, surface_form, field) "
            "values (%s::uuid, %s::uuid, %s, %s)",
            [experience_id, entity_id, surface_form, field],
        )


def _seed_merge_candidate(a, b, similarity=0.7):
    lo, hi = sorted([a, b])
    mid = _new_id()
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.merge_candidates (id, entity_a, entity_b, similarity) "
            "values (%s::uuid, %s::uuid, %s::uuid, %s)",
            [mid, lo, hi, similarity],
        )
    return mid


def _scalar(sql, params):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


# --- review_queue -------------------------------------------------------------


def test_review_queue_all_surfaces_pending_items():
    a = _seed_entity()
    b = _seed_entity()
    mc = _seed_merge_candidate(a, b)

    ent = _seed_entity()
    exp = _seed_experience()
    low = _seed_claim(ent, confidence=0.3)
    _seed_claim_source(low, exp, support_kind="inferred")

    newer = _seed_claim(ent)
    older = _seed_claim(ent, superseded_by=newer)

    dis = request_disambiguation("Which Karen?", [{"label": "A"}, {"label": "B"}])
    pid = propose_correction(
        "entity", ent, {"action": "rename", "new_canonical_name": "Z"}
    )["id"]

    q = review_queue("all")
    assert mc in {r["id"] for r in q["merge_candidates"]}
    assert low in {r["claim_id"] for r in q["low_confidence_claims"]}
    assert older in {r["claim_id"] for r in q["contradictions"]}
    assert dis["token"] in {r["token"] for r in q["disambiguations"]}
    assert pid in {r["id"] for r in q["proposed_corrections"]}
    # jsonb must surface as parsed objects, not strings
    seeded = next(r for r in q["proposed_corrections"] if r["id"] == pid)
    assert seeded["suggested_change"]["action"] == "rename"


def test_review_queue_scoped_skips_other_surfaces():
    a = _seed_entity()
    b = _seed_entity()
    mc = _seed_merge_candidate(a, b)
    q = review_queue("merge_candidates")
    assert mc in {r["id"] for r in q["merge_candidates"]}
    # Scoping skips the other queries entirely, so they stay empty regardless of
    # what else is pending in the shared dev DB.
    assert q["proposed_corrections"] == []
    assert q["disambiguations"] == []
    assert q["contradictions"] == []


# --- propose / resolve_correction ---------------------------------------------


def test_resolve_correction_reject_stamps_without_mutating():
    ent = _seed_entity(canonical_name="Keep Name")
    pid = propose_correction(
        "entity", ent, {"action": "rename", "new_canonical_name": "Nope"}
    )["id"]
    res = resolve_correction(pid, "reject")
    assert res["status"] == "rejected"
    assert (
        _scalar("select canonical_name from brain.entities where id=%s::uuid", [ent])
        == "Keep Name"
    )
    assert (
        _scalar(
            "select status from brain.proposed_corrections where id=%s::uuid", [pid]
        )
        == "rejected"
    )


def test_resolve_correction_apply_rename_dispatches_and_stamps():
    ent = _seed_entity(canonical_name="Wrong Name")
    pid = propose_correction(
        "entity", ent, {"action": "rename", "new_canonical_name": "Right Name"}
    )["id"]
    res = resolve_correction(pid, "apply")
    assert res["status"] == "applied"
    assert res["dispatched_tool"] == "rename_entity"
    assert res["result"]["new_canonical_name"] == "Right Name"
    assert (
        _scalar("select canonical_name from brain.entities where id=%s::uuid", [ent])
        == "Right Name"
    )
    assert (
        _scalar(
            "select status from brain.proposed_corrections where id=%s::uuid", [pid]
        )
        == "applied"
    )


def test_resolve_correction_apply_retract_dispatches():
    ent = _seed_entity()
    claim = _seed_claim(ent)
    pid = propose_correction("claim", claim, {"action": "retract"})["id"]
    res = resolve_correction(pid, "apply")
    assert res["dispatched_tool"] == "retract_claim"
    assert (
        _scalar("select polarity::text from brain.claims where id=%s::uuid", [claim])
        == "retracted"
    )


def test_resolve_correction_apply_repoint_dispatches_split():
    src = _seed_entity(kind="person", canonical_name="Karen")
    exp = _seed_experience()
    _seed_mention(exp, src, "Karen")
    pid = propose_correction(
        "experience",
        exp,
        {
            "action": "repoint_participant",
            "source_entity_id": src,
            "into": {"canonical_name": "Karen B"},
        },
    )["id"]
    res = resolve_correction(pid, "apply")
    assert res["dispatched_tool"] == "split_entity"
    assert (
        _scalar(
            "select count(*) from brain.mentions where entity_id=%s::uuid and experience_id=%s::uuid",
            [src, exp],
        )
        == 0
    )


def test_resolve_correction_already_resolved_raises():
    ent = _seed_entity()
    pid = propose_correction(
        "entity", ent, {"action": "rename", "new_canonical_name": "X"}
    )["id"]
    resolve_correction(pid, "reject")
    with pytest.raises(ValueError, match="already"):
        resolve_correction(pid, "apply")


def test_resolve_correction_dispatch_failure_rolls_back_to_pending():
    # target an entity that does not exist: rename dispatch raises, and the claim
    # stamp must roll back so the proposal returns to the queue.
    missing = _new_id()
    pid = propose_correction(
        "entity", missing, {"action": "rename", "new_canonical_name": "X"}
    )["id"]
    with pytest.raises(ValueError, match="not found"):
        resolve_correction(pid, "apply")
    assert (
        _scalar(
            "select status from brain.proposed_corrections where id=%s::uuid", [pid]
        )
        == "pending"
    )


# --- resolve_merge_candidate (web action path, #137) --------------------------


def test_resolve_merge_candidate_confirm_merges_into_chosen_winner():
    winner = _seed_entity(canonical_name="Keep Me")
    loser = _seed_entity(canonical_name="Fold Me")
    mc = _seed_merge_candidate(winner, loser)
    before = pending_reviews()["merge_candidates"]

    res = resolve_merge_candidate(mc, "confirm", winner_id=winner)

    assert res["decision"] == "confirm"
    assert res["winner_id"] == winner
    # The loser now points at the winner; the candidate is no longer pending.
    assert (
        _scalar("select merged_into::text from brain.entities where id=%s::uuid", [loser])
        == winner
    )
    assert (
        _scalar("select status from brain.merge_candidates where id=%s::uuid", [mc])
        == "merged"
    )
    assert mc not in {r["id"] for r in review_queue("merge_candidates")["merge_candidates"]}
    assert pending_reviews()["merge_candidates"] == before - 1


def test_resolve_merge_candidate_reject_keeps_entities_separate():
    a = _seed_entity(canonical_name="Stay A")
    b = _seed_entity(canonical_name="Stay B")
    mc = _seed_merge_candidate(a, b)
    before = pending_reviews()["merge_candidates"]

    res = resolve_merge_candidate(mc, "reject")

    assert res["decision"] == "reject"
    # Neither entity was merged; the candidate is stamped kept_separate.
    assert _scalar("select merged_into from brain.entities where id=%s::uuid", [a]) is None
    assert _scalar("select merged_into from brain.entities where id=%s::uuid", [b]) is None
    assert (
        _scalar("select status from brain.merge_candidates where id=%s::uuid", [mc])
        == "kept_separate"
    )
    assert pending_reviews()["merge_candidates"] == before - 1


def test_resolve_merge_candidate_confirm_winner_must_be_in_pair():
    a = _seed_entity()
    b = _seed_entity()
    outsider = _seed_entity()
    mc = _seed_merge_candidate(a, b)
    with pytest.raises(ValueError, match="winner_id"):
        resolve_merge_candidate(mc, "confirm", winner_id=outsider)


def test_resolve_merge_candidate_already_resolved_raises():
    a = _seed_entity()
    b = _seed_entity()
    mc = _seed_merge_candidate(a, b)
    resolve_merge_candidate(mc, "reject")
    with pytest.raises(ValueError, match="already"):
        resolve_merge_candidate(mc, "confirm", winner_id=a)


# --- attach_entity_names (web display enrichment, #137) -----------------------


def test_attach_entity_names_enriches_merge_candidates():
    a = _seed_entity(kind="org", canonical_name="Acme")
    b = _seed_entity(kind="org", canonical_name="Acme Inc")
    _seed_merge_candidate(a, b)
    rows = review_queue("merge_candidates")["merge_candidates"]
    enriched = attach_entity_names(rows, "merge_candidates")
    seeded = next(r for r in enriched if {r["entity_a_name"], r["entity_b_name"]} == {"Acme", "Acme Inc"})
    assert seeded["entity_a_kind"] == "org"
    assert seeded["entity_b_kind"] == "org"


def test_attach_entity_names_enriches_subject_for_contradictions():
    ent = _seed_entity(canonical_name="Subject X")
    newer = _seed_claim(ent)
    older = _seed_claim(ent, superseded_by=newer)
    rows = review_queue("contradictions")["contradictions"]
    enriched = attach_entity_names(rows, "contradictions")
    seeded = next(r for r in enriched if r["claim_id"] == older)
    assert seeded["subject_name"] == "Subject X"


def test_attach_entity_names_tolerates_missing_entity():
    # A row whose entity was since hard-deleted falls back to None, never KeyError.
    rows = [{"claim_id": _new_id(), "subject_id": _new_id(), "predicate": "x"}]
    enriched = attach_entity_names(rows, "low_confidence_claims")
    assert enriched[0]["subject_name"] is None


# --- merge-candidate evidence + counts (#155) ---------------------------------


def test_attach_entity_names_attaches_mention_counts():
    # Blast-radius signal: each side carries a distinct-experience mention count.
    heavy = _seed_entity(canonical_name="heavy")
    light = _seed_entity(canonical_name="light")
    for _ in range(3):
        _seed_mention(_seed_experience(), heavy, "heavy")
    _seed_mention(_seed_experience(), light, "light")
    _seed_merge_candidate(heavy, light)

    rows = review_queue("merge_candidates")["merge_candidates"]
    enriched = attach_entity_names(rows, "merge_candidates")
    row = next(r for r in enriched if {r["entity_a"], r["entity_b"]} == {heavy, light})
    by_entity = {
        row["entity_a"]: row["entity_a_count"],
        row["entity_b"]: row["entity_b_count"],
    }
    assert by_entity == {heavy: 3, light: 1}


def test_attach_entity_names_count_follows_merged_into():
    # A candidate entity already merged elsewhere reports the live winner's count.
    winner = _seed_entity(canonical_name="winner")
    loser = _seed_entity(canonical_name="loser")
    # loser points at winner; both their mentions resolve onto winner.
    with connection.cursor() as cur:
        cur.execute(
            "update brain.entities set merged_into = %s::uuid where id = %s::uuid",
            [winner, loser],
        )
    _seed_mention(_seed_experience(), winner, "winner")
    _seed_mention(_seed_experience(), loser, "loser")

    other = _seed_entity(canonical_name="other")
    rows = [{"id": _new_id(), "entity_a": loser, "entity_b": other}]
    enriched = attach_entity_names(rows, "merge_candidates")
    by_entity = {
        enriched[0]["entity_a"]: enriched[0]["entity_a_count"],
        enriched[0]["entity_b"]: enriched[0]["entity_b_count"],
    }
    # loser dereferences to winner: 2 experiences (winner's + loser's) under it.
    assert by_entity[loser] == 2
    assert by_entity[other] == 0


def test_merge_candidate_evidence_respects_viewer_scope():
    ent = _seed_entity(canonical_name="scoped")
    other = _seed_entity(canonical_name="other")
    mine = _seed_experience(content="mine", owner="itest", visibility="private")
    shared = _seed_experience(content="shared", owner="someone", visibility="shared")
    hidden = _seed_experience(content="hidden", owner="someone", visibility="private")
    for exp in (mine, shared, hidden):
        _seed_mention(exp, ent, "scoped")
    cand = _seed_merge_candidate(ent, other)

    evidence = merge_candidate_evidence("itest", cand)
    side = next(s for s in (evidence["a"], evidence["b"]) if s["id"] == ent)
    seen = {e["experience_id"] for e in side["experiences"]}
    assert seen == {mine, shared}
    assert hidden not in seen


def test_merge_candidate_evidence_caps_two_per_side():
    ent = _seed_entity(canonical_name="busy")
    other = _seed_entity(canonical_name="quiet")
    for _ in range(3):
        _seed_mention(_seed_experience(), ent, "busy")
    cand = _seed_merge_candidate(ent, other)

    evidence = merge_candidate_evidence("itest", cand)
    side = next(s for s in (evidence["a"], evidence["b"]) if s["id"] == ent)
    assert len(side["experiences"]) == 2
    assert side["name"] == "busy"


def test_merge_candidate_evidence_missing_candidate_returns_none():
    assert merge_candidate_evidence("itest", _new_id()) is None


# --- request / resolve_disambiguation -----------------------------------------


def test_request_disambiguation_requires_two_options():
    with pytest.raises(ValueError, match="at least 2"):
        request_disambiguation("Q?", [{"label": "only"}])


def test_resolve_disambiguation_by_index_marks_resolved():
    d = request_disambiguation(
        "Which?", [{"label": "A", "value": 1}, {"label": "B", "value": 2}]
    )
    res = resolve_disambiguation(d["token"], 1)
    assert res["resolved_choice"] == {"label": "B", "value": 2}
    assert (
        _scalar(
            "select status from brain.disambiguations where token=%s::uuid",
            [d["token"]],
        )
        == "resolved"
    )


def test_resolve_disambiguation_already_resolved_raises():
    d = request_disambiguation("Which?", [{"label": "A"}, {"label": "B"}])
    resolve_disambiguation(d["token"], "A")
    with pytest.raises(ValueError, match="already"):
        resolve_disambiguation(d["token"], "B")
