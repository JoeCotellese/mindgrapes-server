# ABOUTME: Unit tests for the pure batch dedup planner (#16): blocking discovery,
# ABOUTME: verification-gated merge/queue split, and the abbreviation uniqueness guard.

from openbrain.brain.services.dedup import _candidate_pairs, plan_dedup


def _e(eid, kind, name, aliases=None):
    return {"id": eid, "kind": kind, "canonical_name": name, "aliases": aliases or []}


# --- blocking ------------------------------------------------------------------


def test_blocking_surfaces_shared_token_pair():
    ents = [
        _e("a", "person", "Ada Lovelace"),
        _e("b", "person", "Ada"),
        _e("z", "person", "Grace Hopper"),
    ]
    pairs = _candidate_pairs(ents)
    assert frozenset(("a", "b")) in pairs
    assert frozenset(("a", "z")) not in pairs


def test_blocking_surfaces_single_token_typo_via_minhash():
    # No shared whole token, but char-shingle similarity is high → MinHash blocks.
    ents = [
        _e("a", "person", "Katherine"),
        _e("b", "person", "Katharine"),
    ]
    assert frozenset(("a", "b")) in _candidate_pairs(ents)


def test_blocking_stays_within_kind():
    ents = [
        _e("a", "person", "Mercury"),
        _e("b", "place", "Mercury"),
    ]
    assert _candidate_pairs(ents) == set()


# --- planning ------------------------------------------------------------------


def test_plan_auto_merges_full_name_spelling_variant():
    ents = [
        _e("a", "person", "Jon Smith"),
        _e("b", "person", "John Smith"),
    ]
    plan = plan_dedup(ents)
    assert len(plan["merges"]) == 1
    loser, winner, score = plan["merges"][0]
    assert {loser, winner} == {"a", "b"}
    assert score >= 0.92
    assert plan["queue"] == []


def test_plan_auto_merges_unique_abbreviation_into_full_name():
    ents = [
        _e("full", "person", "Ada Lovelace"),
        _e("abbr", "person", "Ada"),
    ]
    plan = plan_dedup(ents)
    assert plan["merges"] == [("abbr", "full", 0.95)]


def test_plan_merges_exact_duplicate_concepts():
    ents = [
        _e("a", "concept", "documentation"),
        _e("b", "concept", "Documentation"),
    ]
    plan = plan_dedup(ents)
    assert len(plan["merges"]) == 1
    assert plan["queue"] == []


def test_plan_queues_ambiguous_abbreviation_rather_than_guessing():
    # "Chris" is a strict subset of two distinct full names — auto-merging either
    # is a coin flip, so both go to the queue and nothing auto-merges.
    ents = [
        _e("c", "person", "Chris"),
        _e("t", "person", "Chris Taylor"),
        _e("m", "person", "Chris Martin"),
    ]
    plan = plan_dedup(ents)
    assert plan["merges"] == []
    queued = {frozenset((a, b)) for a, b, _ in plan["queue"]}
    assert frozenset(("c", "t")) in queued
    assert frozenset(("c", "m")) in queued


def test_plan_never_merges_two_distinct_karens():
    ents = [
        _e("k1", "person", "Karen"),
        _e("k2", "person", "Karen"),
    ]
    plan = plan_dedup(ents)
    assert plan["merges"] == []
    # They share the token "karen" so they surface as a candidate — but stay queued.
    assert {frozenset((a, b)) for a, b, _ in plan["queue"]} == {frozenset(("k1", "k2"))}


def test_plan_never_merges_across_kinds():
    ents = [
        _e("p", "person", "Ada Lovelace", ["Ada"]),
        _e("c", "concept", "Ada"),
    ]
    plan = plan_dedup(ents)
    assert plan["merges"] == []
    assert plan["queue"] == []  # cross-kind never even blocks together
