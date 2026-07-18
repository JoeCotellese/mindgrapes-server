# ABOUTME: Fellegi-Sunter batch entity scorer (#31 phase 1) — a comparison-feature vector
# ABOUTME: per pair with pinned log2 Bayes-factor weights and a calibrated P(match).
"""Probabilistic second-stage entity match scoring.

Successor to `name_matching`'s single scalar (#31). Instead of one Jaro-Winkler +
containment number patched with hand-written vetoes, each candidate pair is turned
into a small vector of agreement *features* (`compare`); every feature level carries
a pinned log2 Bayes-factor `WEIGHTS`; their sum is a match weight that maps to a
calibrated `match_probability` in [0,1]. A caller auto-merges at/above
`AUTO_MERGE_THRESHOLD` and queues down to `QUEUE_THRESHOLD`.

Vetoes become features: the #29 numeric-token distinction and the generational-suffix
distinction are `("numeric","disagree")` / `("suffix","disagree")` levels with decisive
negative weight, so a strong name agreement plus a numeric disagreement *sums* to a
non-match rather than needing a bespoke `if`. The #27 bare-name containment call is the
`("name","containment")` level, pinned into the review band — never the merge bar.

Phase-1 discipline: weights are **pinned by hand** to reproduce the decisions the
`name_matching` seam already makes on the held-out FIXTURE — this ports a known-good
decision function into additive form. Estimating them (m/u from `correction_events`,
term-frequency adjustment) is phase 3; the margin-over-runner-up gate is phase 2.

Pure by construction (strings in, float out, no DB), mirroring `name_matching` so the
batch planner swaps scorers by passing the module.
"""

from openbrain.brain.services.name_matching import (
    _is_abbreviation,
    _light_normalize,
    _name_forms,
    _numeric_tokens,
    _split_suffix,
    jaro_winkler,
)

_PERSON = "person"

# Decision cut-points in probability space. The gap between them is the asymmetric
# cost made concrete: a false merge is far worse than a false queue-row, so the
# auto-merge bar is deliberately high while the queue floor sits just above the 0.5
# no-evidence prior — a pair needs real positive signal to reach human review (bare
# noise is dropped, mirroring the #27 floor).
AUTO_MERGE_THRESHOLD = 0.90
QUEUE_THRESHOLD = 0.60

# Margin-over-runner-up gate (#31 phase 2). Auto-merge is a claim of *unique* identity,
# but a pairwise score can't see that a second entity fits nearly as well — the class
# that produced Richard->Rich Mironov *because Richard Woundy also existed*. When an
# entity's best merge candidate does not beat its runner-up by at least this margin in
# P(match), the tie is unresolvable from the pair alone: the planner demotes it to the
# review queue instead of guessing. The planner reads this via getattr, so a scorer that
# doesn't advertise it simply runs without the gate.
AUTO_MERGE_MARGIN = 0.05

# A full-name spelling variant clears this Jaro-Winkler bar and merges; a pair below it
# lands in the review band. Pinned to name_matching.AUTO_MERGE_THRESHOLD (0.92), NOT
# lower: the #31 prod dry-run showed 0.90 auto-merged 'Dave Mess'/'Dave Sykes' (0.913)
# and 'Ukrainian woman'/'Ukrainian founder' (0.901) — distinct people in the 0.90-0.92
# band the current matcher correctly queues. The 29-row fixture missed it (no pair sat
# in that band); the two pairs are now fixture rows so the bar can't drift back.
_JW_MERGE = 0.92
_JW_REVIEW = 0.75

# Per-feature, per-level log2 Bayes factors. Positive = evidence for same-entity,
# negative = against. Decisive blocks (cross-kind, numeric/suffix disagreement) carry a
# weight large enough to dominate any name agreement — that is a veto expressed as a
# feature. Pinned to reproduce the name_matching decisions on the FIXTURE; phase 3
# re-estimates these from labeled correction_events.
WEIGHTS: dict[str, dict[str, float]] = {
    "kind": {"agree": 0.0, "cross": -20.0},
    "name": {
        "exact_light": 9.0,  # exact light-normalized dup (case/space) — merge
        "jw_hi": 9.0,  # full-name spelling variant — merge
        "punct_folded": 2.4,  # equal only after punctuation-folding — review (#29)
        "containment": 2.4,  # bare given name abbreviating a fuller name — review (#27)
        "jw_mid": 1.4,  # shared given name / moderate similarity — review
        "identical_full": 0.0,  # identical full name is not a unique identifier
        "jw_low": -2.0,
        "added_component": -4.0,  # an extra name token is a real distinction
        "disjoint": -8.0,
    },
    "numeric": {
        "agree": 0.0,
        "disagree": -20.0,
    },  # differing digit token is decisive (#29)
    "suffix": {"agree": 0.0, "disagree": -20.0},  # Jr/Sr distinguishes separate people
}


def match_probability(features: list[tuple[str, str]]) -> float:
    """Calibrated P(match) in [0,1] from a feature vector: logistic of the summed
    log2 Bayes factors. No features (no evidence either way) is the 0.5 prior."""
    # Strict lookup on purpose: an unknown feature/level is a bug, and on a
    # false-merge-sensitive path a silently-neutralized veto fails open. Every level
    # compare() emits is defined in WEIGHTS, so this never raises in correct operation.
    weight = sum(WEIGHTS[feature][level] for feature, level in features)
    odds = 2.0**weight
    return odds / (1.0 + odds)


def _name_level(core_a: list[str], core_b: list[str]) -> str:
    if core_a == core_b:
        return "identical_full"
    if len(core_a) != len(core_b):
        return "added_component"
    jw = jaro_winkler(" ".join(core_a), " ".join(core_b))
    if jw >= _JW_MERGE:
        return "jw_hi"
    if jw >= _JW_REVIEW:
        return "jw_mid"
    return "jw_low"


def _person_features(forms_a: list[str], forms_b: list[str]) -> list[tuple[str, str]]:
    # A single bare given name that is a strict token-subset of a fuller name is a
    # best-guess abbreviation ("Ada" of "Ada Lovelace") — review, never merge (#27).
    for na in forms_a:
        ta = na.split()
        for nb in forms_b:
            tb = nb.split()
            if _is_abbreviation(ta, tb) or _is_abbreviation(tb, ta):
                return [("name", "containment")]

    # Otherwise compare full (multi-token) names, keeping the strongest interpretation.
    # Suffix and numeric-token agreement ride alongside the name level as their own
    # features, so a name that looks similar but disagrees on a digit or a generational
    # suffix sums to a non-match.
    best: list[tuple[str, str]] | None = None
    best_p = -1.0
    for na in forms_a:
        core_a, suffix_a = _split_suffix(na.split())
        if len(core_a) < 2:
            continue
        for nb in forms_b:
            core_b, suffix_b = _split_suffix(nb.split())
            if len(core_b) < 2:
                continue
            feats = [
                ("name", _name_level(core_a, core_b)),
                (
                    "numeric",
                    "agree"
                    if _numeric_tokens(core_a) == _numeric_tokens(core_b)
                    else "disagree",
                ),
                ("suffix", "agree" if suffix_a == suffix_b else "disagree"),
            ]
            p = match_probability(feats)
            if p > best_p:
                best, best_p = feats, p
    return best if best is not None else [("name", "disjoint")]


def _nonperson_features(
    canonical_a: str, aliases_a, canonical_b: str, aliases_b, forms_a, forms_b
) -> list[tuple[str, str]]:
    # A concept/org/place/event is its name: an exact light-normalized duplicate is a
    # confident merge; a match that appears only after punctuation-folding is a review
    # call, not an identity ('Obsidian vault' vs '~/obsidian-vault', #29).
    light_a = {_light_normalize(x) for x in [canonical_a, *(aliases_a or [])]}
    light_b = {_light_normalize(x) for x in [canonical_b, *(aliases_b or [])]}
    if (light_a - {""}) & (light_b - {""}):
        return [("name", "exact_light")]
    if set(forms_a) & set(forms_b):
        return [("name", "punct_folded")]
    return [("name", "disjoint")]


def compare(
    kind_a: str, canonical_a: str, aliases_a, kind_b: str, canonical_b: str, aliases_b
) -> list[tuple[str, str]]:
    """The comparison-feature vector for a candidate pair (see module docstring)."""
    if kind_a != kind_b:
        return [("kind", "cross")]

    forms_a = _name_forms(canonical_a, aliases_a)
    forms_b = _name_forms(canonical_b, aliases_b)
    if not forms_a or not forms_b:
        return [("name", "disjoint")]

    if kind_a != _PERSON:
        return _nonperson_features(
            canonical_a, aliases_a, canonical_b, aliases_b, forms_a, forms_b
        )
    return _person_features(forms_a, forms_b)


def match_score(
    kind_a: str, canonical_a: str, aliases_a, kind_b: str, canonical_b: str, aliases_b
) -> float:
    """Calibrated P(match) in [0,1] for a candidate pair — the `name_matching.match_score`
    drop-in the batch planner calls behind `--scorer=fs`."""
    return match_probability(
        compare(kind_a, canonical_a, aliases_a, kind_b, canonical_b, aliases_b)
    )
