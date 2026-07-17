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
    Person auto-merge therefore requires either two *full* (multi-token) names
    that are near-identical, or a bare given name that is a strict token-subset
    of a fuller name (an abbreviation, e.g. "Ada" of "Ada Lovelace").
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
# confident person abbreviation; score it just above threshold rather than 1.0 so
# it never outranks an exact full-name match in any downstream ordering.
CONTAINMENT_SCORE = 0.95

_PERSON = "person"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(name: str) -> str:
    """Lowercase, fold every run of non-alphanumerics to a single space, trim."""
    return _NON_ALNUM.sub(" ", (name or "").lower()).strip()


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
    """True when sub's tokens are a strict subset of a strictly-longer super."""
    return (
        len(super_tokens) > len(sub_tokens)
        and set(sub_tokens) < set(super_tokens)
    )


def _person_score(forms_a: list[str], forms_b: list[str]) -> float:
    # A bare given name that is a strict token-subset of a fuller name is a
    # confident abbreviation ("Ada" of "Ada Lovelace").
    for na in forms_a:
        ta = na.split()
        for nb in forms_b:
            tb = nb.split()
            if _is_abbreviation(ta, tb) or _is_abbreviation(tb, ta):
                return CONTAINMENT_SCORE

    # Otherwise fuzzy-match, but only between full (multi-token) names. Bare
    # shared given names ("Karen"/"Karen", "Ad"/"Ada") are inherently ambiguous
    # and stay human-gated regardless of string similarity.
    best = 0.0
    for na in forms_a:
        if len(na.split()) < 2:
            continue
        for nb in forms_b:
            if len(nb.split()) < 2:
                continue
            best = max(best, jaro_winkler(na, nb))
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
        # A concept/org/place/event is its name: only an exact normalized
        # duplicate is a safe auto-merge.
        return 1.0 if set(forms_a) & set(forms_b) else 0.0

    return _person_score(forms_a, forms_b)
