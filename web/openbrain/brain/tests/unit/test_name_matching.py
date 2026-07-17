# ABOUTME: Unit tests for the pure name-verification seam (#16), incl. the labeled
# ABOUTME: trap fixture and the before/after queue-delta the acceptance asks for.

from openbrain.brain.services.name_matching import (
    AUTO_MERGE_THRESHOLD,
    DISAMBIGUATE_THRESHOLD,
    REUSE_THRESHOLD,
    jaro,
    jaro_winkler,
    match_score,
    recommend_action,
)

# --- Jaro / Jaro-Winkler known-value cases ------------------------------------


def test_jaro_identical_is_one():
    assert jaro("martha", "martha") == 1.0


def test_jaro_disjoint_is_zero():
    assert jaro("abc", "xyz") == 0.0


def test_jaro_known_value_martha_marhta():
    # Winkler's canonical example: Jaro('MARTHA','MARHTA') = 0.9444…
    assert round(jaro("martha", "marhta"), 4) == 0.9444


def test_jaro_winkler_boosts_shared_prefix():
    # Same Jaro base, but the shared 'mar' prefix lifts JW above it.
    assert round(jaro_winkler("martha", "marhta"), 4) == 0.9611


def test_jaro_winkler_no_prefix_equals_jaro():
    assert jaro_winkler("abcde", "xbcde") == jaro("abcde", "xbcde")


# --- The labeled fixture ------------------------------------------------------
#
# Each row is (kind_a, name_a, aliases_a, kind_b, name_b, aliases_b, same?).
# `same` True means "should be safe to auto-merge"; False means "must stay
# human-gated". The trap rows exercise the costly direction — a False row that
# scores at/above threshold is a false auto-merge and a ship-blocker.

FIXTURE = [
    # -- same: spelling variants of a full (multi-token) name --
    ("person", "Jon Smith", [], "person", "John Smith", [], True),
    ("person", "Katherine Johnson", [], "person", "Katharine Johnson", [], True),
    ("person", "Acme Robotics Inc", [], "person", "Acme Robotcs Inc", [], True),
    # -- same: a bare given name that abbreviates a fuller name --
    ("person", "Ada Lovelace", [], "person", "Ada", [], True),
    ("person", "Ada", [], "person", "Ada Lovelace", ["Ada"], True),
    # -- same: exact-duplicate concept (the epic's exact-name dup) --
    ("concept", "documentation", [], "concept", "Documentation", [], True),
    ("org", "Northwind", ["the product"], "org", "northwind", [], True),
    # -- trap: two distinct people who share a common given name --
    ("person", "Karen", [], "person", "Karen", [], False),
    ("person", "Karen Smith", [], "person", "Karen Jones", [], False),
    # -- trap: distinct people who share an *identical* full name (a full name is
    #    not a unique identifier — there are many Michael Jordans) --
    ("person", "Michael Jordan", [], "person", "Michael Jordan", [], False),
    # -- trap: father/son separated only by a generational suffix --
    ("person", "John Smith", [], "person", "John Smith Jr", [], False),
    ("person", "Robert Downey", [], "person", "Robert Downey Jr", [], False),
    ("person", "John Smith Jr", [], "person", "John Smith Sr", [], False),
    ("person", "Ada", [], "person", "Ada Jr", [], False),
    # -- trap: an added name component is a real distinction, not an abbreviation --
    ("person", "Mary Watson", [], "person", "Mary Jane Watson", [], False),
    # -- trap: short-token collisions --
    ("person", "Ad", [], "person", "Ada", [], False),
    ("person", "Robert", [], "person", "Roberta", [], False),
    # -- trap: initialism vs the expansion it abbreviates --
    ("person", "AL", [], "person", "Ada Lovelace", [], False),
    ("org", "IBM", [], "org", "International Business Machines", [], False),
    # -- trap: near-collision across kinds must never merge --
    ("concept", "Ada", [], "person", "Ada Lovelace", ["Ada"], False),
    ("concept", "Mercury", [], "place", "Mercury", [], False),
    # -- trap: distinct concepts one token apart --
    ("concept", "Q3 roadmap", [], "concept", "Q4 roadmap", [], False),
    ("concept", "Documentation Review", [], "concept", "documentation", [], False),
]


def _score(row):
    ka, na, aa, kb, nb, ab, _ = row
    return match_score(ka, na, aa, kb, nb, ab)


def test_fixture_has_zero_false_auto_merges():
    """The costly direction: no labeled-different pair may reach the merge bar."""
    offenders = [
        (row[1], row[4], _score(row))
        for row in FIXTURE
        if row[6] is False and _score(row) >= AUTO_MERGE_THRESHOLD
    ]
    assert offenders == [], f"false auto-merges: {offenders}"


def test_cross_kind_never_scores():
    for row in FIXTURE:
        ka, _, _, kb, _, _, _ = row
        if ka != kb:
            assert _score(row) == 0.0


def test_generational_suffix_pairs_stay_gated():
    """Father/son and Jr/Sr pairs are distinct people, never an abbreviation."""
    assert match_score("person", "John Smith", [], "person", "John Smith Jr", []) < (
        AUTO_MERGE_THRESHOLD
    )
    assert match_score(
        "person", "John Smith Jr", [], "person", "John Smith Sr", []
    ) < AUTO_MERGE_THRESHOLD


def test_added_name_component_stays_gated():
    """An inserted middle name is a real distinction, not an abbreviation."""
    assert match_score(
        "person", "Mary Watson", [], "person", "Mary Jane Watson", []
    ) < AUTO_MERGE_THRESHOLD


def test_identical_full_name_persons_stay_gated():
    """A full name is not a unique identifier; identical names alone stay gated."""
    assert match_score(
        "person", "Michael Jordan", [], "person", "Michael Jordan", []
    ) < AUTO_MERGE_THRESHOLD


def test_same_suffix_spelling_variant_still_merges():
    """Agreeing on the suffix, a core spelling variant is still a confident match."""
    assert match_score(
        "person", "Jon Smith Jr", [], "person", "John Smith Jr", []
    ) >= AUTO_MERGE_THRESHOLD


def test_queue_delta_report():
    """Before/after the second stage over the labeled same-pairs.

    'Today' every labeled pair sits in the borderline band and would queue.
    'After', the same-pairs at/above threshold are auto-merged; the rest still
    queue. This is the measurable-win number the acceptance asks for.
    """
    same = [r for r in FIXTURE if r[6] is True]
    diff = [r for r in FIXTURE if r[6] is False]

    same_auto_merged = [r for r in same if _score(r) >= AUTO_MERGE_THRESHOLD]
    same_still_queued = [r for r in same if _score(r) < AUTO_MERGE_THRESHOLD]
    diff_auto_merged = [r for r in diff if _score(r) >= AUTO_MERGE_THRESHOLD]

    # The win: most labeled same-pairs move out of the queue…
    assert len(same_auto_merged) >= 5
    # …and the costly direction stays exactly zero.
    assert diff_auto_merged == []

    # Sanity on the counts used for the reported delta.
    assert len(same_auto_merged) + len(same_still_queued) == len(same)


# --- #8: recommend_action banding (the resolve_entity recommendation) ----------


def test_recommend_action_reuse_above_match_bar():
    assert recommend_action(0.90) == "reuse"
    assert recommend_action(REUSE_THRESHOLD + 0.0001) == "reuse"


def test_recommend_action_disambiguate_in_borderline_band():
    assert recommend_action(0.70) == "disambiguate"
    assert recommend_action(REUSE_THRESHOLD) == "disambiguate"
    assert recommend_action(DISAMBIGUATE_THRESHOLD + 0.0001) == "disambiguate"


def test_recommend_action_create_at_or_below_borderline_floor():
    assert recommend_action(DISAMBIGUATE_THRESHOLD) == "create"
    assert recommend_action(0.30) == "create"
    assert recommend_action(0.0) == "create"
