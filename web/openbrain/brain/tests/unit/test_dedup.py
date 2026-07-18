# ABOUTME: Unit tests for the pure batch dedup planner (#16): blocking discovery,
# ABOUTME: verification-gated merge/queue split, and the abbreviation uniqueness guard.

from openbrain.brain.services import probabilistic
from openbrain.brain.services.dedup import (
    MAX_QUEUE_PER_ENTITY,
    _candidate_pairs,
    _cap_queue,
    plan_dedup,
)
from openbrain.brain.services.name_matching import CONTAINMENT_SCORE


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


def test_plan_queues_bare_name_abbreviation():
    # A bare given name abbreviating one fuller name is the likely same entity
    # but not a safe auto-merge (field evidence #27: alias pollution and unseen
    # namesakes make "unique containment" unreliable) — it queues for review.
    ents = [
        _e("full", "person", "Ada Lovelace"),
        _e("abbr", "person", "Ada"),
    ]
    plan = plan_dedup(ents)
    assert plan["merges"] == []
    assert plan["queue"] == [("abbr", "full", CONTAINMENT_SCORE)]


def test_plan_merges_exact_duplicate_concepts():
    ents = [
        _e("a", "concept", "documentation"),
        _e("b", "concept", "Documentation"),
    ]
    plan = plan_dedup(ents)
    assert len(plan["merges"]) == 1
    assert plan["queue"] == []


def test_plan_queues_ambiguous_abbreviation_rather_than_guessing():
    # "Chris" fits two distinct full names; both pairs queue and nothing merges.
    # (Containment no longer auto-merges at all, but both pairs must still clear
    # the queue floor rather than being dropped as noise.)
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


def test_plan_never_merges_generational_suffix_pair():
    # Father/son sharing a name minus a Jr suffix must not auto-merge — and with
    # no verification signal (score 0.0, names not identical) the pair is
    # presumptively two people: dropped as noise, not queued (#27).
    ents = [
        _e("s", "person", "John Smith"),
        _e("j", "person", "John Smith Jr"),
    ]
    plan = plan_dedup(ents)
    assert plan["merges"] == []
    assert plan["queue"] == []


def test_plan_never_merges_added_name_component():
    # An inserted middle name is a real distinction, not an abbreviation.
    ents = [
        _e("mw", "person", "Mary Watson"),
        _e("mjw", "person", "Mary Jane Watson"),
    ]
    plan = plan_dedup(ents)
    assert plan["merges"] == []


def test_plan_never_merges_two_identical_full_name_persons():
    # Two distinct people with an identical full name (e.g. a prior split_entity)
    # must not be silently re-merged by the batch scanner.
    ents = [
        _e("mj1", "person", "Michael Jordan"),
        _e("mj2", "person", "Michael Jordan"),
    ]
    plan = plan_dedup(ents)
    assert plan["merges"] == []
    assert {frozenset((a, b)) for a, b, _ in plan["queue"]} == {
        frozenset(("mj1", "mj2"))
    }


def test_plan_never_merges_across_kinds():
    ents = [
        _e("p", "person", "Ada Lovelace", ["Ada"]),
        _e("c", "concept", "Ada"),
    ]
    plan = plan_dedup(ents)
    assert plan["merges"] == []
    assert plan["queue"] == []  # cross-kind never even blocks together


def test_plan_drops_blocking_noise_below_floor():
    # Blocking surfaces the shared-token pair, but with no verification signal
    # it is dropped, not queued — unfloored queueing wrote 155k rows against the
    # 5.2k-entity live brain (#27).
    ents = [
        _e("a", "concept", "Documentation Review"),
        _e("b", "concept", "documentation"),
    ]
    plan = plan_dedup(ents)
    assert plan["merges"] == []
    assert plan["queue"] == []


def test_plan_still_queues_identical_person_names():
    # Identical person names score 0.0 (never merge evidence) yet are exactly
    # the decision a human should see — the floor must not drop them.
    ents = [
        _e("k1", "person", "Karen"),
        _e("k2", "person", "Karen"),
    ]
    plan = plan_dedup(ents)
    assert plan["merges"] == []
    assert [(a, b) for a, b, _ in plan["queue"]] == [("k1", "k2")]


# --- contested-merge gate (#31 phase 2) ----------------------------------------


def test_plan_queues_entity_contested_between_distinct_identities():
    # One entity strongly merges with two OTHER entities that do not merge with each
    # other — it is claimed by two distinct identities and the pair alone can't say
    # which. The gate demotes to review instead of guessing (the Richard->{Woundy,
    # Mironov} class, expressed at the merge level via bridging aliases).
    ents = [
        _e("x", "org", "Apple", ["Apple Inc", "Apple Records"]),
        _e("a", "org", "Apple Inc"),
        _e("b", "org", "Apple Records"),
    ]
    plan = plan_dedup(ents, scorer=probabilistic)
    assert plan["merges"] == []
    queued = {frozenset((a, b)) for a, b, _ in plan["queue"]}
    assert frozenset(("x", "a")) in queued
    assert frozenset(("x", "b")) in queued


def test_plan_merges_clear_winner_over_distant_runner_up():
    # The runner-up (a shared given name, review-band score) is well below the merge
    # bar, so it never competes — the single merge candidate still auto-merges.
    ents = [
        _e("x", "person", "Jon Smith"),
        _e("a", "person", "John Smith"),  # full-name variant — merge candidate
        _e("c", "person", "Jon Baker"),  # shares only "jon" — review band, not a merge
    ]
    plan = plan_dedup(ents, scorer=probabilistic)
    assert len(plan["merges"]) == 1
    loser, winner, _ = plan["merges"][0]
    assert {loser, winner} == {"x", "a"}


def test_plan_single_merge_candidate_unaffected_by_gate():
    # Regression: with exactly one merge candidate the gate has no runner-up to
    # compare against and must not demote the merge.
    ents = [
        _e("a", "person", "Jon Smith"),
        _e("b", "person", "John Smith"),
    ]
    plan = plan_dedup(ents, scorer=probabilistic)
    assert len(plan["merges"]) == 1
    assert plan["queue"] == []


def test_plan_merges_mutual_variant_cluster_not_a_contest():
    # Three spelling variants of ONE person all mutually merge — a single cluster,
    # not a contested identity. The gate must NOT demote them: an entity with several
    # merge partners that themselves merge is unambiguous, unlike an entity torn
    # between two distinct people. (Regression for the count-based gate that demoted
    # every entity with >=2 merges because FS quantizes all merges to ~0.998.)
    ents = [
        _e("a", "person", "Jon Smith"),
        _e("b", "person", "John Smith"),
        _e("c", "person", "Jhon Smith"),
    ]
    plan = plan_dedup(ents, scorer=probabilistic)
    merged_ids = {i for pair in plan["merges"] for i in pair[:2]}
    assert merged_ids == {"a", "b", "c"}
    assert plan["queue"] == []


def test_queue_cap_bounds_per_entity_fanout():
    # One polluted hub must not fan out unbounded review rows; the cap keeps the
    # highest-scoring pairs deterministically.
    queue = [("hub", f"e{i}", 0.60 + i / 100) for i in range(8)]
    capped = _cap_queue(queue)
    assert len(capped) == MAX_QUEUE_PER_ENTITY
    assert {b for _, b, _ in capped} == {f"e{i}" for i in range(3, 8)}
