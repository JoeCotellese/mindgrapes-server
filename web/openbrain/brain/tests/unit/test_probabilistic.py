# ABOUTME: Unit tests for the Fellegi-Sunter batch scorer (#31 phase 1) — the held-out
# ABOUTME: FIXTURE acceptance gate (0 false merges, recall >= current, queue <= current).

from openbrain.brain.services import name_matching, probabilistic
from openbrain.brain.services.dedup import plan_dedup
from openbrain.brain.tests.unit.test_name_matching import FIXTURE


def _score(scorer, row):
    ka, na, aa, kb, nb, ab, _ = row
    return scorer.match_score(ka, na, aa, kb, nb, ab)


def _auto_merges(scorer):
    """same-labeled fixture rows this scorer would auto-merge."""
    return [
        r
        for r in FIXTURE
        if r[6] is True and _score(scorer, r) >= scorer.AUTO_MERGE_THRESHOLD
    ]


def _queued(scorer):
    """fixture rows landing in the review band [QUEUE_THRESHOLD, AUTO_MERGE)."""
    return [
        r
        for r in FIXTURE
        if scorer.QUEUE_THRESHOLD <= _score(scorer, r) < scorer.AUTO_MERGE_THRESHOLD
    ]


# --- FS scoring primitives ----------------------------------------------------


def test_match_probability_bounds():
    """Every pair scores a calibrated probability in [0,1]; no evidence => 0.5."""
    assert probabilistic.match_probability([]) == 0.5
    for row in FIXTURE:
        assert 0.0 <= _score(probabilistic, row) <= 1.0


def test_match_probability_monotonic():
    """A strong-agreement feature raises P above the no-evidence prior; a decisive
    disagreement drops it below."""
    prior = probabilistic.match_probability([])
    assert probabilistic.match_probability([("name", "exact_light")]) > prior
    assert probabilistic.match_probability([("numeric", "disagree")]) < prior


def test_cross_kind_is_non_match():
    """A concept and a person that share a surface form never reach the queue band."""
    cross = [r for r in FIXTURE if r[0] != r[3]]
    assert cross, "fixture should contain cross-kind trap rows"
    for row in cross:
        assert _score(probabilistic, row) < probabilistic.QUEUE_THRESHOLD


# --- The held-out FIXTURE acceptance gate (fs_fixture) ------------------------


def test_fs_fixture_zero_false_auto_merges():
    """The costly direction: no labeled-different pair may reach the FS merge bar."""
    offenders = [
        (row[1], row[4], round(_score(probabilistic, row), 3))
        for row in FIXTURE
        if row[6] is False
        and _score(probabilistic, row) >= probabilistic.AUTO_MERGE_THRESHOLD
    ]
    assert offenders == [], f"false auto-merges: {offenders}"


def test_fs_fixture_recall_ge_current():
    """FS must auto-merge at least as many true same-pairs as the current matcher —
    zero-false-merges alone is passable by merging nothing, so recall is floored."""
    assert len(_auto_merges(probabilistic)) >= len(_auto_merges(name_matching))


def test_fs_fixture_queue_le_current():
    """FS must not push more pairs into human review than the current matcher."""
    assert len(_queued(probabilistic)) <= len(_queued(name_matching))


# --- The scorer seam in the planner ------------------------------------------

_ENTS = [
    {"id": "a", "kind": "person", "canonical_name": "Jon Smith", "aliases": []},
    {"id": "b", "kind": "person", "canonical_name": "John Smith", "aliases": []},
    {"id": "c", "kind": "concept", "canonical_name": "Documentation", "aliases": []},
    {"id": "d", "kind": "concept", "canonical_name": "documentation", "aliases": []},
]


def test_plan_dedup_scorer_default_unchanged():
    """The default scorer is the current matcher — existing behavior is unchanged."""
    assert plan_dedup(_ENTS) == plan_dedup(_ENTS, scorer=name_matching)


def test_plan_dedup_accepts_fs_scorer():
    """The planner runs on the FS scorer and returns the same plan shape."""
    plan = plan_dedup(_ENTS, scorer=probabilistic)
    assert set(plan) == {"merges", "queue"}
    # the two clear duplicates (spelling variant + exact concept dup) still merge.
    assert len(plan["merges"]) >= 1
