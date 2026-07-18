# ABOUTME: Pure batch entity-dedup planner — token + MinHash/LSH blocking feeds the
# ABOUTME: name_matching verification seam to plan safe auto-merges vs queued pairs (#16).
"""Batch pair discovery + dedup planning.

Capture-time resolution only ever sees the one entity a surface trgm-matches, so
duplicates that never co-occur at capture stay fragmented. This module does the
offline pass: block the whole entity set into candidate pairs (cheaply, with
recall over precision), then run each pair through the same `name_matching`
verification seam the capture-time resolver uses and decide auto-merge vs queue.

Everything here is pure (entity dicts in, a plan out) so the management command
that owns the DB I/O stays a thin adapter and the decision logic is unit-tested
without Postgres. Blocking is the union of two channels:

  * token overlap — entities of one kind that share a normalized token of length
    >= 3. Surfaces abbreviations ("Ada"/"Ada Lovelace") and shared-surname
    variants that char-shingle similarity would miss on length alone.
  * MinHash/LSH over character 3-shingles — surfaces single-token spelling
    variants ("Katherine"/"Katharine") that share no whole token.

Verification (not blocking) is what makes a merge safe, so blocking is tuned for
recall; false candidates are harmless — they verify below the queue floor and
are dropped, and only pairs with genuine signal queue (bounded per entity).
"""

import hashlib
import random
from collections import defaultdict
from itertools import combinations

from openbrain.brain.services import name_matching
from openbrain.brain.services.name_matching import _is_abbreviation, _name_forms

_PERSON = "person"
_MIN_TOKEN_LEN = 3

# Backstop: one polluted hub entity must not fan out unbounded review rows (#27).
MAX_QUEUE_PER_ENTITY = 5

# MinHash/LSH geometry: 64 permutations split into 32 bands of 2 rows puts the
# LSH S-curve knee near jaccard (1/32)**(1/2) ≈ 0.18 — a deliberately low bar so
# single-token spelling variants (char-3-shingle jaccard ~0.5) block reliably.
# Unrelated names rarely reach 0.18 shingle jaccard, and blocking is recall-first
# regardless: every false candidate simply verifies below threshold and queues.
_NUM_PERM = 64
_BANDS = 32
_ROWS = 2
_SHINGLE_K = 3
_MERSENNE = (1 << 61) - 1


def _blocking_forms(entity: dict) -> list[str]:
    return _name_forms(entity["canonical_name"], entity.get("aliases"))


def _tokens(forms: list[str]) -> set[str]:
    toks: set[str] = set()
    for form in forms:
        toks.update(form.split())
    return toks


def _shingles(forms: list[str]) -> set[str]:
    grams: set[str] = set()
    for form in forms:
        s = f" {form} "
        if len(s) < _SHINGLE_K:
            grams.add(s)
            continue
        for i in range(len(s) - _SHINGLE_K + 1):
            grams.add(s[i : i + _SHINGLE_K])
    return grams


def _shingle_hash(shingle: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big"
    )


def _minhash_coefficients() -> list[tuple[int, int]]:
    # Deterministic across runs so the same entity set always blocks identically.
    rng = random.Random(0xB0A17)
    return [
        (rng.randrange(1, _MERSENNE), rng.randrange(0, _MERSENNE))
        for _ in range(_NUM_PERM)
    ]


_COEFFS = _minhash_coefficients()


def _signature(shingles: set[str]) -> tuple[int, ...]:
    bases = [_shingle_hash(s) for s in shingles]
    sig = []
    for a, b in _COEFFS:
        sig.append(min((a * h + b) % _MERSENNE for h in bases))
    return tuple(sig)


def _candidate_pairs(entities: list[dict]) -> set[frozenset]:
    """Union of token-overlap and MinHash/LSH candidate pairs, within-kind only."""
    pairs: set[frozenset] = set()

    token_buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    lsh_buckets: dict[tuple, list[str]] = defaultdict(list)

    for ent in entities:
        forms = _blocking_forms(ent)
        if not forms:
            continue
        kind = ent["kind"]
        for tok in _tokens(forms):
            if len(tok) >= _MIN_TOKEN_LEN:
                token_buckets[(kind, tok)].append(ent["id"])
        shingles = _shingles(forms)
        if shingles:
            sig = _signature(shingles)
            for band in range(_BANDS):
                key = (kind, band, sig[band * _ROWS : (band + 1) * _ROWS])
                lsh_buckets[key].append(ent["id"])

    for bucket in (*token_buckets.values(), *lsh_buckets.values()):
        if len(bucket) < 2:
            continue
        for a, b in combinations(sorted(set(bucket)), 2):
            pairs.add(frozenset((a, b)))
    return pairs


def _abbreviation_subset(a: dict, b: dict) -> str | None:
    """Return the id of the side whose bare name abbreviates the other (person
    strict token-subset), or None. Used for the auto-merge uniqueness guard."""
    if a["kind"] != b["kind"] or a["kind"] != _PERSON:
        return None
    forms_a = _blocking_forms(a)
    forms_b = _blocking_forms(b)
    for na in forms_a:
        ta = na.split()
        for nb in forms_b:
            tb = nb.split()
            if _is_abbreviation(ta, tb):
                return a["id"]
            if _is_abbreviation(tb, ta):
                return b["id"]
    return None


def _identical_person_forms(a: dict, b: dict) -> bool:
    """True when two person entities share an exact normalized name form."""
    if a["kind"] != _PERSON or b["kind"] != _PERSON:
        return False
    return bool(set(_blocking_forms(a)) & set(_blocking_forms(b)))


def _cap_queue(queue: list[tuple[str, str, float]]) -> list[tuple[str, str, float]]:
    """Keep at most MAX_QUEUE_PER_ENTITY queued pairs per entity, best first."""
    counts: dict[str, int] = defaultdict(int)
    kept: list[tuple[str, str, float]] = []
    for a_id, b_id, score in sorted(queue, key=lambda t: (-t[2], t[0], t[1])):
        if counts[a_id] >= MAX_QUEUE_PER_ENTITY or counts[b_id] >= MAX_QUEUE_PER_ENTITY:
            continue
        counts[a_id] += 1
        counts[b_id] += 1
        kept.append((a_id, b_id, score))
    return kept


def _pick_winner(a: dict, b: dict) -> tuple[dict, dict]:
    """(winner, loser) for a fuzzy full-name merge: keep the better-annotated,
    longer, then lexicographically-smaller-id side as the winner. Deterministic."""
    key_a = (len(a.get("aliases") or []), len(a["canonical_name"]), b["id"])
    key_b = (len(b.get("aliases") or []), len(b["canonical_name"]), a["id"])
    return (a, b) if key_a >= key_b else (b, a)


def plan_dedup(entities: list[dict], scorer=name_matching) -> dict:
    """Plan auto-merges and queued pairs for a set of entities.

    `scorer` is a match-scoring module exposing match_score + AUTO_MERGE_THRESHOLD +
    QUEUE_THRESHOLD; it defaults to the current name_matching seam. Passing the
    probabilistic scorer swaps the decision policy without touching blocking (#31).

    Each entity is {id, kind, canonical_name, aliases}. Returns
    {merges: [(loser_id, winner_id, score)], queue: [(a_id, b_id, score)]}
    with loser/winner already oriented (the less-annotated side loses). Pairs
    that fail verification are queued only with genuine signal (>= QUEUE_THRESHOLD,
    or identical person names) and capped per entity; the rest are dropped as
    blocking noise (#27). The abbreviation uniqueness guard below is defense in
    depth: containment no longer reaches the merge bar at all.
    """
    by_id = {e["id"]: e for e in entities}
    merges: list[tuple[str, str, float, str | None]] = []
    queue: list[tuple[str, str, float]] = []

    for pair in _candidate_pairs(entities):
        a_id, b_id = sorted(pair)
        a, b = by_id[a_id], by_id[b_id]
        score = scorer.match_score(
            a["kind"],
            a["canonical_name"],
            a.get("aliases"),
            b["kind"],
            b["canonical_name"],
            b.get("aliases"),
        )
        if score >= scorer.AUTO_MERGE_THRESHOLD:
            subset_id = _abbreviation_subset(a, b)
            if subset_id is not None:
                winner = b if subset_id == a_id else a
                loser = a if subset_id == a_id else b
            else:
                winner, loser = _pick_winner(a, b)
            merges.append((loser["id"], winner["id"], score, subset_id))
        # Queue floor: only pairs with real verification signal reach the review
        # queue. Blocking is recall-first, so most surfaced pairs score 0.0 —
        # unfloored, the first live run queued 155k rows against 5.2k entities
        # (#27). Identical-name person pairs are the one exception: they score 0.0
        # by design yet are exactly the call a human should make.
        elif score >= scorer.QUEUE_THRESHOLD or _identical_person_forms(a, b):
            queue.append((a_id, b_id, score))

    # Uniqueness guard: an abbreviation that fits more than one full name is
    # ambiguous — demote every merge that consumes that subset id to the queue.
    subset_winners: dict[str, set[str]] = defaultdict(set)
    for _loser_id, winner_id, _score, subset_id in merges:
        if subset_id is not None:
            subset_winners[subset_id].add(winner_id)
    ambiguous = {sid for sid, winners in subset_winners.items() if len(winners) > 1}

    safe_merges: list[tuple[str, str, float]] = []
    for loser_id, winner_id, score, subset_id in merges:
        if subset_id in ambiguous:
            lo, hi = sorted((loser_id, winner_id))
            queue.append((lo, hi, score))
        else:
            safe_merges.append((loser_id, winner_id, score))

    return {"merges": safe_merges, "queue": _cap_queue(queue)}
