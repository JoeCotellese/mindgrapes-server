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


def test_resolve_borderline_creates_entity_and_merge_candidate():
    cursor = StubCursor(
        [
            (_RESOLVE_COLS, [_resolve_row(0.70)]),  # resolve_entity (in 0.55..0.85)
            (["id"], [(NEW,)]),  # insert entity returning id
            (["id"], [("mc-1",)]),  # insert merge candidate
        ]
    )

    outcome = resolve_or_create_entity(
        cursor, EXP, _EMB, surface="Jon", field="people", kind="person"
    )

    assert outcome["action"] == "borderline"
    assert outcome["entity_id"] == NEW
    assert outcome["borderline_entity_id"] == EXISTING
    mc_sql, mc_params = cursor.calls[2]
    assert "merge_candidates" in mc_sql
    # least/greatest ordering reuses both ids, then similarity + evidence jsonb.
    assert mc_params[:4] == [EXISTING, NEW, EXISTING, NEW]
    assert mc_params[4] == 0.70
    evidence = json.loads(mc_params[5])
    assert evidence["surface_form"] == "Jon"
    assert evidence["experience_id"] == EXP


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
