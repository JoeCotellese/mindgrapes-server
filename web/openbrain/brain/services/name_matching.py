# ABOUTME: Pure name-verification seam for entity dedup — Jaro-Winkler + person
# ABOUTME: abbreviation containment fused into one combined match score (#16, shared with #8).
"""Second-stage entity match verification.

`resolve_entity`'s trgm/dmetaphone channels are cheap blocking: they surface
name-similar candidates but land many genuinely-same pairs in the 0.55-0.85
borderline band that then queue as merge_candidates for a human. This module is
the verification stage that runs on such a pair and returns a single combined
score in [0,1]; a caller auto-merges (soft, audited, reversible) only at or above
`AUTO_MERGE_THRESHOLD` and queues everything below.

It is deliberately pure (strings in, float out, no DB) so the capture-time
resolver (#16) and the batch scanner both call the same seam, and so #8
(capture-then-reconcile) can reuse it unchanged.

Safety is the whole point: a false auto-merge is a ship-blocker, so the scoring
is conservative by construction. The rules encode two domain facts:

  * A person is not identified by a bare given name. Two entities that are each
    only "Karen" are ambiguous and must stay human-gated, even at trgm 1.0.
    Person auto-merge therefore requires two *full* (multi-token) names that are
    a near-identical spelling variant of each other. A single bare given name
    that is a strict token-subset of a fuller name (an abbreviation, e.g. "Ada"
    of "Ada Lovelace") is only a best guess — it scores `CONTAINMENT_SCORE`,
    below the auto-merge bar (#27). A full name is not a unique identifier either
    — distinct people commonly share one — so an *identical* full name is not
    evidence of same-entity, and neither is a pair that differs by a whole added
    token: a generational suffix ("John Smith Jr"/"John Smith Sr") or an extra
    name component ("Mary Watson"/"Mary Jane Watson") is a real distinction and
    stays human-gated.
  * A concept/org/place/event *is* its name. Only an exact normalized duplicate
    is a confident auto-merge; fuzzy near-names ("Q3 roadmap"/"Q4 roadmap") stay
    queued because a one-token difference there is usually a real distinction.

Cross-kind pairs never merge.
"""

import re

# JW is generous on shared prefixes, so the bar is set high: distinct full names
# that share a given name (e.g. "Karen Smith"/"Karen Jones" ≈ 0.81) stay safely
# below it while spelling variants of the same full name ("Jon Smith"/"John
# Smith" ≈ 0.94) clear it. Tuned against the labeled fixture in the unit suite
# for zero false auto-merges; lower it only with fresh fixture evidence.
AUTO_MERGE_THRESHOLD = 0.92

# A bare given name that is a strict token-subset of a multi-token full name is a
# *best guess*, not a merge: the bare name may belong to an unseen namesake, and
# aliases on over-collapsed entities make apparent containment unreliable (the
# first live run auto-merged 'Richard' into 'Rich Mironov' while Richard Woundy
# existed, #27). Scored between the disambiguate floor and the auto-merge bar so
# callers bind provisionally / queue it — never auto-merge.
CONTAINMENT_SCORE = 0.86

# Recommendation cut-points applied to resolve_entity's top trgm_score (name
# similarity, 0-1). These are the single source of truth for the capture-then-
# reconcile bands (#8): the resolve_entity tool advertises the banding as a
# `recommendation`, and the capture-time resolver reuses the same two constants
# for its reuse / provisional / create split — so retuning them here retunes both.
REUSE_THRESHOLD = 0.85
DISAMBIGUATE_THRESHOLD = 0.55

# Batch queue floor: the score at/above which the dedup planner keeps a non-merge pair
# for human review (below it, blocking noise is dropped, #27). Part of the scorer-seam
# contract — every scorer module advertises AUTO_MERGE_THRESHOLD + QUEUE_THRESHOLD so the
# planner can swap scorers (#31). Here it is the disambiguate floor.
QUEUE_THRESHOLD = DISAMBIGUATE_THRESHOLD


def recommend_action(trgm_score: float) -> str:
    """Band a top trgm_score into 'reuse' / 'disambiguate' / 'create' (#8).

    trgm > REUSE_THRESHOLD reuses the existing entity; a score in the
    (DISAMBIGUATE_THRESHOLD, REUSE_THRESHOLD] borderline band recommends
    disambiguation; anything at/below DISAMBIGUATE_THRESHOLD (including a missing
    candidate, which scores 0) recommends creating a fresh entity.
    """
    if trgm_score > REUSE_THRESHOLD:
        return "reuse"
    if trgm_score > DISAMBIGUATE_THRESHOLD:
        return "disambiguate"
    return "create"

_PERSON = "person"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")

# Generational suffixes distinguish separate people who share a full name
# (father/son). They are never the token that expands a bare given name into a
# confident abbreviation, and a pair that disagrees on one is a real distinction.
_GENERATIONAL_SUFFIXES = frozenset({"jr", "sr", "jnr", "snr", "ii", "iii", "iv", "v"})


def _normalize(name: str) -> str:
    """Lowercase, fold every run of non-alphanumerics to a single space, trim."""
    return _NON_ALNUM.sub(" ", (name or "").lower()).strip()


def _light_normalize(name: str) -> str:
    """Lowercase and collapse whitespace only — punctuation shape is preserved.

    Punctuation carries identity for non-person names: '~/obsidian-vault' is a
    directory and 'Obsidian vault' a concept, yet both fold to the same words
    under _normalize (#29). Auto-merge equality must not erase that.
    """
    return _WHITESPACE.sub(" ", (name or "").lower()).strip()


def _name_forms(canonical: str, aliases) -> list[str]:
    """Distinct normalized surface forms for an entity (canonical + aliases)."""
    forms: list[str] = []
    for raw in [canonical, *(aliases or [])]:
        norm = _normalize(raw)
        if norm and norm not in forms:
            forms.append(norm)
    return forms


def _jaro_matches(s1: str, s2: str, window: int):
    """Flag matched characters within the sliding window; return (count, flags)."""
    s1_matched = [False] * len(s1)
    s2_matched = [False] * len(s2)
    matches = 0
    for i in range(len(s1)):
        lo = max(0, i - window)
        hi = min(i + window + 1, len(s2))
        for j in range(lo, hi):
            if not s2_matched[j] and s1[i] == s2[j]:
                s1_matched[i] = s2_matched[j] = True
                matches += 1
                break
    return matches, s1_matched, s2_matched


def _jaro_transpositions(s1, s2, s1_matched, s2_matched) -> int:
    """Half the count of matched characters that appear out of order."""
    swaps = 0
    k = 0
    for i in range(len(s1)):
        if not s1_matched[i]:
            continue
        while not s2_matched[k]:
            k += 1
        if s1[i] != s2[k]:
            swaps += 1
        k += 1
    return swaps // 2


def jaro(s1: str, s2: str) -> float:
    """Jaro string similarity in [0,1]."""
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0
    window = max(0, max(len1, len2) // 2 - 1)
    matches, s1_matched, s2_matched = _jaro_matches(s1, s2, window)
    if matches == 0:
        return 0.0
    t = _jaro_transpositions(s1, s2, s1_matched, s2_matched)
    m = matches
    return (m / len1 + m / len2 + (m - t) / m) / 3.0


def jaro_winkler(
    s1: str, s2: str, *, prefix_weight: float = 0.1, max_prefix: int = 4
) -> float:
    """Jaro-Winkler similarity: Jaro boosted for a shared leading prefix."""
    base = jaro(s1, s2)
    prefix = 0
    for a, b in zip(s1, s2, strict=False):
        if a != b or prefix >= max_prefix:
            break
        prefix += 1
    return base + prefix * prefix_weight * (1 - base)


def _is_abbreviation(sub_tokens: list[str], super_tokens: list[str]) -> bool:
    """True when sub is a single bare given name that a fuller super expands.

    The confident abbreviation is one bare given name ("Ada") standing in for a
    fuller name ("Ada Lovelace"): sub is exactly one token, a strict subset of a
    strictly-longer super, and the distinguishing super tokens are a plausible
    surname — not merely a generational suffix ("Ada"/"Ada Jr"). A multi-token
    subset ("Mary Watson" of "Mary Jane Watson", "John Smith" of "John Smith Jr")
    is a different person plus an added component, not an abbreviation.
    """
    if len(sub_tokens) != 1:
        return False
    if len(super_tokens) <= len(sub_tokens):
        return False
    sub, sup = set(sub_tokens), set(super_tokens)
    if not sub < sup:
        return False
    return bool((sup - sub) - _GENERATIONAL_SUFFIXES)


def _numeric_tokens(tokens: list[str]) -> set[str]:
    """Tokens carrying any digit — identity-bearing for numbered person names."""
    return {t for t in tokens if any(c.isdigit() for c in t)}


def _split_suffix(tokens: list[str]) -> tuple[list[str], str | None]:
    """Peel a trailing generational suffix off a name; (core_tokens, suffix)."""
    if len(tokens) > 1 and tokens[-1] in _GENERATIONAL_SUFFIXES:
        return tokens[:-1], tokens[-1]
    return tokens, None


def _person_score(forms_a: list[str], forms_b: list[str]) -> float:
    # A single bare given name that is a strict token-subset of a fuller name is
    # a confident abbreviation ("Ada" of "Ada Lovelace").
    for na in forms_a:
        ta = na.split()
        for nb in forms_b:
            tb = nb.split()
            if _is_abbreviation(ta, tb) or _is_abbreviation(tb, ta):
                return CONTAINMENT_SCORE

    # Otherwise fuzzy-match, but only between full (multi-token) names that agree
    # on generational suffix and core token-count. Bare shared given names
    # ("Karen"/"Karen", "Ad"/"Ada") are inherently ambiguous; a differing suffix
    # ("John Smith Jr"/"John Smith Sr"), an added component ("Mary Watson"/"Mary
    # Jane Watson"), or an identical full name (distinct people commonly share
    # one) is a real distinction — all stay human-gated regardless of string
    # similarity.
    best = 0.0
    for na in forms_a:
        core_a, suffix_a = _split_suffix(na.split())
        if len(core_a) < 2:
            continue
        for nb in forms_b:
            core_b, suffix_b = _split_suffix(nb.split())
            if len(core_b) < 2:
                continue
            if suffix_a != suffix_b or len(core_a) != len(core_b):
                continue
            if core_a == core_b:
                continue
            # Differing numeric tokens are a hard distinction, not spelling
            # variance: 'Engineer 1'/'Engineer 2' are two people (#29).
            if _numeric_tokens(core_a) != _numeric_tokens(core_b):
                continue
            best = max(best, jaro_winkler(" ".join(core_a), " ".join(core_b)))
    return best


def match_score(
    kind_a: str,
    canonical_a: str,
    aliases_a,
    kind_b: str,
    canonical_b: str,
    aliases_b,
) -> float:
    """Combined verification score in [0,1] for a candidate entity pair.

    Returns 0.0 for any pair that must not auto-merge (cross-kind, ambiguous bare
    given names, non-exact concepts). At or above `AUTO_MERGE_THRESHOLD` the pair
    is a confident same-entity match.
    """
    if kind_a != kind_b:
        return 0.0

    forms_a = _name_forms(canonical_a, aliases_a)
    forms_b = _name_forms(canonical_b, aliases_b)
    if not forms_a or not forms_b:
        return 0.0

    if kind_a != _PERSON:
        # A concept/org/place/event is its name: auto-merge only on an exact
        # light-normalized duplicate (case/whitespace variants, alias-backed
        # identity). A match that appears only after punctuation-folding is a
        # review call, not an identity (#29) — it scores in the queue band.
        light_a = {_light_normalize(x) for x in [canonical_a, *(aliases_a or [])]}
        light_b = {_light_normalize(x) for x in [canonical_b, *(aliases_b or [])]}
        if (light_a - {""}) & (light_b - {""}):
            return 1.0
        if set(forms_a) & set(forms_b):
            return CONTAINMENT_SCORE
        return 0.0

    return _person_score(forms_a, forms_b)
