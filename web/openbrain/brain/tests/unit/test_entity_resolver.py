# ABOUTME: Unit tests for the entity resolve/link policy (no Postgres).
# ABOUTME: Drives resolve_or_create_entity's matched/borderline/created branches via StubCursor.

import json

from openbrain.brain.services.entity_resolver import (
    BORDERLINE_THRESHOLD,
    MATCH_THRESHOLD,
    link_mention,
    resolve_or_create_entity,
)
from openbrain.brain.tests.unit._support import StubCursor

EXP = "00000000-0000-0000-0000-000000000001"
EXISTING = "11111111-1111-1111-1111-111111111111"
NEW = "22222222-2222-2222-2222-222222222222"
_EMB = "[" + ",".join(["0.1"] * 1536) + "]"

_RESOLVE_COLS = ["entity_id", "trgm_score", "phon_match", "vec_score", "fused_score"]


def _resolve_row(trgm):
    return (EXISTING, trgm, True, 0.9, 0.5)


def test_resolve_matched_appends_alias():
    cursor = StubCursor(
        [
            (_RESOLVE_COLS, [_resolve_row(0.95)]),  # resolve_entity
            ([], []),  # append alias
        ]
    )

    outcome = resolve_or_create_entity(
        cursor, EXP, _EMB, surface="Grace", field="people", kind="person"
    )

    assert outcome["action"] == "matched"
    assert outcome["entity_id"] == EXISTING
    assert "borderline_entity_id" not in outcome
    # Second call appends the surface to the matched entity's aliases.
    alias_sql, alias_params = cursor.calls[1]
    assert "array_append(aliases" in alias_sql
    assert alias_params == ["Grace", "Grace", EXISTING]


def test_resolve_borderline_binds_provisional_to_best_guess():
    # Existing top is a bare given name that does NOT verify against "Jon"
    # (person single-token pair → verification 0.0). Below the auto-merge bar the
    # surface is provisionally bound to the best guess (the existing entity); no
    # duplicate entity is minted and no merge_candidate is queued.
    cursor = StubCursor(
        [
            (_RESOLVE_COLS, [_resolve_row(0.70)]),  # resolve_entity (in 0.55..0.85)
            (["canonical_name", "aliases"], [("Jonas", [])]),  # fetch top name
        ]
    )

    outcome = resolve_or_create_entity(
        cursor, EXP, _EMB, surface="Jon", field="people", kind="person"
    )

    assert outcome["action"] == "provisional"
    assert outcome["provisional"] is True
    assert outcome["entity_id"] == EXISTING  # bound to the best guess, not a new row
    assert outcome["candidate_entity_id"] == EXISTING
    assert outcome["verification_score"] == 0.0
    # Exactly two queries: resolve + fetch-top-name. No INSERT, no merge_candidate.
    assert len(cursor.calls) == 2
    assert all("insert" not in sql.lower() for sql, _ in cursor.calls)


def test_resolve_borderline_auto_merges_on_confident_verification(monkeypatch):
    # trgm lands in the borderline band (would queue today), but the second-stage
    # seam verifies "John Smith" against the existing "Jon Smith" at ~0.97 → a
    # fresh entity is soft-auto-merged into the existing one via the shared merge
    # core instead of queueing. Verification runs BEFORE the entity is minted.
    merge_calls = []

    def _record_merge(cur, loser, winner, **kwargs):
        merge_calls.append((loser, winner, kwargs))
        return {"correction_event_id": "corr-1"}

    monkeypatch.setattr(
        "openbrain.brain.services.entity_resolver.merge_entities_on_cursor",
        _record_merge,
    )

    cursor = StubCursor(
        [
            (_RESOLVE_COLS, [_resolve_row(0.70)]),  # resolve_entity (in band)
            (["canonical_name", "aliases"], [("Jon Smith", [])]),  # fetch top name
            (["id"], [(NEW,)]),  # insert entity returning id
            (["id"], [("mc-1",)]),  # insert merge candidate (pending)
        ]
    )

    outcome = resolve_or_create_entity(
        cursor, EXP, _EMB, surface="John Smith", field="people", kind="person"
    )

    assert outcome["action"] == "auto_merged"
    assert outcome["entity_id"] == EXISTING  # mentions link to the surviving entity
    assert outcome["merged_from_entity_id"] == NEW
    assert outcome["verification_score"] >= 0.92
    # The candidate row is recorded (pending) before the merge core flips it.
    mc_sql, mc_params = cursor.calls[3]
    assert "merge_candidates" in mc_sql
    assert mc_params[:4] == [EXISTING, NEW, EXISTING, NEW]
    evidence = json.loads(mc_params[5])
    assert evidence["surface_form"] == "John Smith"
    assert evidence["experience_id"] == EXP
    # Loser is the freshly-created entity; winner is the existing top.
    assert merge_calls == [(NEW, EXISTING, merge_calls[0][2])]
    assert merge_calls[0][2]["created_by"] == "mcp:entity_resolver:auto_merge"


def test_resolve_created_when_no_candidate():
    cursor = StubCursor(
        [
            (_RESOLVE_COLS, []),  # resolve_entity finds nothing
            (["id"], [(NEW,)]),  # insert entity returning id
        ]
    )

    outcome = resolve_or_create_entity(
        cursor, EXP, _EMB, surface="Brand New", field="people", kind="person"
    )

    assert outcome["action"] == "created"
    assert outcome["entity_id"] == NEW
    assert "borderline_entity_id" not in outcome
    assert len(cursor.calls) == 2  # no merge_candidate insert


def test_resolve_created_when_below_borderline():
    cursor = StubCursor(
        [
            (_RESOLVE_COLS, [_resolve_row(0.30)]),  # below BORDERLINE_THRESHOLD
            (["id"], [(NEW,)]),
        ]
    )

    outcome = resolve_or_create_entity(
        cursor, EXP, _EMB, surface="Distant", field="people", kind="person"
    )

    assert outcome["action"] == "created"
    assert len(cursor.calls) == 2


def test_link_mention_returns_true_when_inserted():
    cursor = StubCursor([(["experience_id"], [(EXP,)])])
    assert link_mention(cursor, EXP, EXISTING, "Grace", "people") is True


def test_link_mention_returns_false_on_conflict():
    cursor = StubCursor([(["experience_id"], [])])
    assert link_mention(cursor, EXP, EXISTING, "Grace", "people") is False


def test_thresholds_match_node():
    assert MATCH_THRESHOLD == 0.85
    assert BORDERLINE_THRESHOLD == 0.55
