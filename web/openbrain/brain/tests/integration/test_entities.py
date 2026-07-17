"""Integration tests for the entity-identity repair services (Slice C, #120).

Each test seeds entities/claims/mentions/merge_candidates, runs a service, and
asserts the brain.* effect (rows + correction_events) directly — then
brain_write_txn rolls the whole transaction back so the shared dev database is
never mutated. This is the DB-effect contract for the identity block;
the unit suite covers the pure branch logic.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import uuid

import pytest
from django.db import connection
from django.test import override_settings

from openbrain.brain.db import dictfetchall
from openbrain.brain.services.claim_writer import (
    _resolve_or_create_entity,
    new_accumulator,
)
from openbrain.brain.services.entities import (
    merge_entities,
    rename_entity,
    resolve_entity,
    retract_claim,
    split_entity,
    unmerge_entity,
)
from openbrain.brain.services.entity_resolver import (
    resolve_or_create_entity,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

_VEC = "[" + ",".join(["0.01"] * 1536) + "]"
_ZERO = [0.0] * 1536


def _zero_embed(text):
    return _ZERO


def _new_id():
    return str(uuid.uuid4())


def _seed_entity(kind="person", canonical_name=None, aliases=None, merged_into=None):
    eid = _new_id()
    name = canonical_name or f"itest-{eid[:8]}"
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name, aliases, merged_into) "
            "values (%s::uuid, %s::brain.entity_kind, %s, %s::text[], %s::uuid)",
            [eid, kind, name, aliases if aliases is not None else [], merged_into],
        )
    return eid


def _seed_experience(content="seed"):
    eid = _new_id()
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.experiences (id, content, embedding, owner, visibility) "
            "values (%s::uuid, %s, %s::vector, %s, %s::brain.visibility)",
            [eid, content, _VEC, "itest", "private"],
        )
    return eid


def _seed_claim(
    subject_id, object_entity_id=None, predicate="relates_to", polarity="asserted"
):
    cid = _new_id()
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.claims (id, subject_id, object_entity_id, predicate, polarity) "
            "values (%s::uuid, %s::uuid, %s::uuid, %s, %s::brain.polarity)",
            [cid, subject_id, object_entity_id, predicate, polarity],
        )
    return cid


def _seed_claim_source(claim_id, experience_id, support_kind="verbatim"):
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.claim_sources (claim_id, experience_id, support_kind) "
            "values (%s::uuid, %s::uuid, %s::brain.support_kind)",
            [claim_id, experience_id, support_kind],
        )


def _seed_mention(experience_id, entity_id, surface_form, field="people"):
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.mentions (experience_id, entity_id, surface_form, field) "
            "values (%s::uuid, %s::uuid, %s, %s)",
            [experience_id, entity_id, surface_form, field],
        )


def _seed_merge_candidate(a, b, similarity=0.7, status="pending"):
    lo, hi = sorted([a, b])
    mid = _new_id()
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.merge_candidates (id, entity_a, entity_b, similarity, status) "
            "values (%s::uuid, %s::uuid, %s::uuid, %s, %s)",
            [mid, lo, hi, similarity, status],
        )
    return mid


def _scalar(sql, params):
    with connection.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


# --- merge_entities -----------------------------------------------------------


def test_merge_appends_aliases_sets_merged_into_and_audits():
    winner = _seed_entity(canonical_name="Ada Lott", aliases=["Ada L"])
    loser = _seed_entity(canonical_name="Adaline Lott", aliases=["AL"])

    res = merge_entities(loser, winner)

    assert res["winner_id"] == winner
    assert res["alias_appended"] is True
    assert res["correction_event_id"]
    assert (
        _scalar(
            "select merged_into::text from brain.entities where id=%s::uuid", [loser]
        )
        == winner
    )
    aliases = _scalar("select aliases from brain.entities where id=%s::uuid", [winner])
    assert {"Adaline Lott", "AL", "Ada L"} <= set(aliases)
    assert (
        _scalar(
            "select count(*) from brain.correction_events "
            "where target_kind='entity' and target_id=%s::uuid",
            [loser],
        )
        == 1
    )


def test_merge_already_merged_loser_raises():
    other = _seed_entity()
    winner = _seed_entity()
    loser = _seed_entity(merged_into=other)
    with pytest.raises(ValueError, match="already merged"):
        merge_entities(loser, winner)


def test_merge_kind_mismatch_raises():
    winner = _seed_entity(kind="person")
    loser = _seed_entity(kind="org")
    with pytest.raises(ValueError, match="kind mismatch"):
        merge_entities(loser, winner)


def test_merge_resolves_pending_merge_candidate():
    winner = _seed_entity()
    loser = _seed_entity()
    mc = _seed_merge_candidate(loser, winner)
    merge_entities(loser, winner)
    assert (
        _scalar("select status from brain.merge_candidates where id=%s::uuid", [mc])
        == "merged"
    )


def test_merge_identical_name_empty_aliases_succeeds():
    # #2: when both entities share canonical_name and carry no other aliases, the
    # alias-append filter (a <> winner.canonical_name) strips every candidate row
    # and array_agg over zero rows returns NULL — which used to trip the aliases
    # NOT NULL constraint (sqlstate 23502) and block reconciling exact-name
    # duplicates by hand.
    winner = _seed_entity(canonical_name="PostgreSQL", aliases=[])
    loser = _seed_entity(canonical_name="PostgreSQL", aliases=[])
    res = merge_entities(loser, winner)
    assert res["alias_appended"] is False
    assert (
        _scalar("select aliases from brain.entities where id=%s::uuid", [winner]) == []
    )
    assert (
        _scalar(
            "select merged_into::text from brain.entities where id=%s::uuid", [loser]
        )
        == winner
    )


def test_merge_identical_name_keeps_loser_distinct_aliases():
    # The same-name path must still carry over the aliases that aren't the shared
    # canonical_name — the coalesce fixes the empty case without swallowing these.
    winner = _seed_entity(canonical_name="PostgreSQL", aliases=[])
    loser = _seed_entity(
        canonical_name="PostgreSQL", aliases=["Postgres", "PostgreSQL"]
    )
    res = merge_entities(loser, winner)
    assert res["alias_appended"] is True
    assert _scalar(
        "select aliases from brain.entities where id=%s::uuid", [winner]
    ) == ["Postgres"]


# --- rename_entity ------------------------------------------------------------


def test_rename_updates_canonical_and_keeps_old_as_alias():
    e = _seed_entity(canonical_name="Old Name", aliases=[])
    res = rename_entity(e, "New Name")
    assert res["old_canonical_name"] == "Old Name"
    assert res["correction_event_id"]
    assert (
        _scalar("select canonical_name from brain.entities where id=%s::uuid", [e])
        == "New Name"
    )
    assert "Old Name" in _scalar(
        "select aliases from brain.entities where id=%s::uuid", [e]
    )


def test_rename_noop_when_unchanged_skips_audit():
    e = _seed_entity(canonical_name="Same Name")
    res = rename_entity(e, "Same Name")
    assert res["correction_event_id"] == ""
    assert (
        _scalar(
            "select count(*) from brain.correction_events where target_id=%s::uuid", [e]
        )
        == 0
    )


# --- retract_claim ------------------------------------------------------------


def test_retract_sets_polarity_and_audits():
    ent = _seed_entity()
    claim = _seed_claim(ent)
    res = retract_claim(claim, "fact was wrong")
    assert res["prior_polarity"] == "asserted"
    assert (
        _scalar("select polarity::text from brain.claims where id=%s::uuid", [claim])
        == "retracted"
    )
    assert (
        _scalar(
            "select count(*) from brain.correction_events "
            "where target_kind='claim' and target_id=%s::uuid",
            [claim],
        )
        == 1
    )


def test_retract_already_retracted_raises():
    ent = _seed_entity()
    claim = _seed_claim(ent, polarity="retracted")
    with pytest.raises(ValueError, match="already retracted"):
        retract_claim(claim, "again")


# --- split_entity -------------------------------------------------------------


def test_split_mint_repoints_mentions_and_claims():
    src = _seed_entity(canonical_name="Karen", kind="person")
    exp = _seed_experience()
    _seed_mention(exp, src, "Karen")
    claim = _seed_claim(src)
    _seed_claim_source(claim, exp)

    res = split_entity(src, [exp], {"canonical_name": "Karen B"})

    tgt = res["target_entity_id"]
    assert res["target_created"] is True
    assert res["mentions_repointed"] == 1
    assert res["claims_repointed"] == 1
    assert res["correction_event_ids"]
    assert (
        _scalar(
            "select count(*) from brain.mentions where entity_id=%s::uuid and experience_id=%s::uuid",
            [tgt, exp],
        )
        == 1
    )
    assert (
        _scalar(
            "select count(*) from brain.mentions where entity_id=%s::uuid and experience_id=%s::uuid",
            [src, exp],
        )
        == 0
    )
    assert (
        _scalar("select subject_id::text from brain.claims where id=%s::uuid", [claim])
        == tgt
    )


def test_split_into_existing_entity():
    src = _seed_entity(kind="person")
    tgt = _seed_entity(kind="person")
    exp = _seed_experience()
    _seed_mention(exp, src, "Shared")
    res = split_entity(src, [exp], {"entity_id": tgt})
    assert res["target_created"] is False
    assert res["target_entity_id"] == tgt
    assert res["mentions_repointed"] == 1


def test_split_merged_source_raises():
    winner = _seed_entity()
    src = _seed_entity(merged_into=winner)
    exp = _seed_experience()
    with pytest.raises(ValueError, match="unmerge it first"):
        split_entity(src, [exp], {"canonical_name": "Z"})


def test_split_zero_scope_writes_no_correction():
    src = _seed_entity()
    exp = _seed_experience()  # no mentions/claims bind src here
    res = split_entity(src, [exp], {"canonical_name": "Lonely"})
    assert res["mentions_repointed"] == 0
    assert res["claims_repointed"] == 0
    assert res["correction_event_ids"] == []


# --- unmerge_entity -----------------------------------------------------------


def test_unmerge_clears_pointer_and_reopens_candidate():
    winner = _seed_entity()
    loser = _seed_entity(merged_into=winner)
    mc = _seed_merge_candidate(loser, winner, status="merged")
    res = unmerge_entity(loser)
    assert res["prior_merged_into"] == winner
    assert (
        _scalar("select merged_into from brain.entities where id=%s::uuid", [loser])
        is None
    )
    assert (
        _scalar("select status from brain.merge_candidates where id=%s::uuid", [mc])
        == "pending"
    )


def test_unmerge_not_merged_raises():
    e = _seed_entity()
    with pytest.raises(ValueError, match="is not merged"):
        unmerge_entity(e)


# --- resolve_entity -----------------------------------------------------------


@override_settings(
    BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_entities._zero_embed"
)
def test_resolve_entity_ranks_candidate_by_name():
    ent = _seed_entity(kind="person", canonical_name="Zephyrine Quux")
    res = resolve_entity("Zephyrine Quux", kind="person", top_k=5)
    assert res["query_name"] == "Zephyrine Quux"
    assert res["query_kind"] == "person"
    by_id = {c["entity_id"]: c for c in res["candidates"]}
    assert ent in by_id
    top = by_id[ent]
    assert isinstance(top["trgm_score"], float) and top["trgm_score"] > 0.3
    assert isinstance(top["phon_match"], bool)
    assert isinstance(top["fused_score"], float)


# --- #171: alias scoring is per-alias, not against a concatenated haystack ---
#
# These call brain.resolve_entity directly with a NULL context embedding so the vec
# channel drops out entirely and the assertions isolate the trgm channel, which is what
# the fix changes. Names are synthetic and distinctive because these run against the
# shared dev brain and must not collide with its real entities.


def _resolve_trgm(name, kind="person", top_k=5):
    with connection.cursor() as cur:
        cur.execute(
            "select entity_id::text as entity_id, trgm_score "
            "from brain.resolve_entity(%s, null::vector, %s::brain.entity_kind, %s)",
            [name, kind, top_k],
        )
        return dictfetchall(cur)


def _trgm_score(name, entity_id):
    rows = _resolve_trgm(name)
    by_id = {r["entity_id"]: r for r in rows}
    assert entity_id in by_id, f"{name!r} did not surface the expected entity at all"
    return float(by_id[entity_id]["trgm_score"])


def test_resolve_entity_scores_exact_alias_at_full_similarity():
    """An exact alias hit must score 1.0. Against the concatenated haystack it scored
    similarity('Zephyrine Quux Zeph Mr. Quux', 'Zeph') — far below the 0.3 prefilter, so
    the entity never even surfaced and a duplicate got minted instead (#171)."""
    ent = _seed_entity(
        kind="person", canonical_name="Zephyrine Quux", aliases=["Zeph", "Mr. Quux"]
    )
    assert _trgm_score("Zeph", ent) == 1.0
    assert _resolve_trgm("Zeph")[0]["entity_id"] == ent


def test_resolve_entity_alias_score_does_not_dilute_with_more_aliases():
    """The haystack made every added alias lower similarity for all the others — the
    best-annotated entity was the hardest to find. Per-alias scoring is independent of
    how many siblings an alias has."""
    two = _seed_entity(
        kind="person",
        canonical_name="Zephyrine Quux",
        aliases=["Zephalpha", "Mr. Quux"],
    )
    five = _seed_entity(
        kind="person",
        canonical_name="Zephyrine Quux",
        aliases=["Zephbeta", "Mr. Quux", "Zed Quux", "Quuxy", "Z. Quux"],
    )
    assert _trgm_score("Zephalpha", two) == _trgm_score("Zephbeta", five) == 1.0


def test_resolve_entity_matches_full_name_split_across_aliases():
    """The haystack was not purely harmful: concatenating aliases reassembled a full name
    out of single-token aliases, and scoring per-alias alone loses that. An entity whose
    canonical_name is initials with the name split across two aliases tops out at 0.667 on
    per-alias scoring — under MATCH_THRESHOLD, so it mints the duplicate #171 exists to
    prevent. greatest() keeps the haystack as a third term, so both cases are covered."""
    ent = _seed_entity(
        kind="person", canonical_name="ZQ", aliases=["Zephyrine", "Quux"]
    )
    assert _trgm_score("Zephyrine Quux", ent) == 1.0


def test_claim_writer_binds_full_name_split_across_aliases():
    """The above, at the call site that mints duplicates when scoring comes up short."""
    ent = _seed_entity(
        kind="person", canonical_name="ZQ", aliases=["Zephyrine", "Quux"]
    )
    acc = new_accumulator()
    with connection.cursor() as cur:
        bound = _resolve_or_create_entity(cur, "Zephyrine Quux", "person", None, acc)
    assert bound == ent
    assert acc["entities_created_for_objects"] == 0


def test_claim_writer_binds_existing_entity_on_exact_alias():
    """The end of the chain the bug actually broke: _resolve_or_create_entity gates on
    trgm_score >= MATCH_THRESHOLD (0.85), so a diluted ~0.26 fell through to _insert_entity
    and minted the duplicate. Passing embedding=None keeps the vec channel out of it."""
    ent = _seed_entity(
        kind="person", canonical_name="Zephyrine Quux", aliases=["Zeph", "Mr. Quux"]
    )
    acc = new_accumulator()
    with connection.cursor() as cur:
        bound = _resolve_or_create_entity(cur, "Zeph", "person", None, acc)
    assert bound == ent
    assert acc["entities_created_for_objects"] == 0


def test_claim_writer_still_creates_entity_for_a_genuinely_new_name():
    """Regression guard in the opposite direction: the 2026-06-05 failure was resolve_entity
    over-collapsing distinct people onto a shared token. A different surname must not bind
    to the seeded entity."""
    ent = _seed_entity(
        kind="person", canonical_name="Zephyrine Quux", aliases=["Zeph", "Mr. Quux"]
    )
    acc = new_accumulator()
    with connection.cursor() as cur:
        created = _resolve_or_create_entity(
            cur, "Zephyrine Hansen", "person", None, acc
        )
    assert created != ent
    assert acc["entities_created_for_objects"] == 1


# --- #17: phon is a tiebreak, not a channel that can outrank a trgm match ------
#
# brain.resolve_entity fuses trgm + phon + vec via RRF. A trgm rank-1 hit is worth
# 1/(60+1) ≈ 0.0164; the phon channel's additive bonus was 0.05 — ~3x the biggest a
# trgm rank can contribute — so a phonetic-only competitor always outranked a *perfect*
# name/alias match, and both top_k=1 callers bound to the wrong entity (#17). Fix drops
# the phon bonus to 0.0001 (below one RRF step, 1/61-1/62 ≈ 0.000264, so it only breaks
# genuine ties) and makes the phon channel alias-aware like #171 did for trgm.
#
# dmetaphone('Ada') = dmetaphone('Ad') = 'AT'; dmetaphone('Ada Lovelace') = 'ATLF'.
# So the decoy 'Ad' is a phon-only competitor and 'Ada Lovelace' phon-matches solely
# through its alias 'Ada' — exercising both halves of the fix at once. Verified against
# the dev brain: no live person entity collides on dmetaphone 'AT' or trgm-matches 'Ada'.


def _resolve_full(name, kind="person", top_k=5):
    """brain.resolve_entity with a NULL context embedding so the vec channel drops out —
    the shared dev brain has embedded entities that would otherwise crowd the fused top-k.
    Isolates the trgm + phon channels this fix touches, same tactic as the #171 helpers."""
    with connection.cursor() as cur:
        cur.execute(
            "select entity_id::text as entity_id, trgm_score, phon_match, "
            "vec_score, fused_score "
            "from brain.resolve_entity(%s, null::vector, %s::brain.entity_kind, %s)",
            [name, kind, top_k],
        )
        rows = dictfetchall(cur)
    for r in rows:
        r["trgm_score"] = float(r["trgm_score"])
        r["phon_match"] = bool(r["phon_match"])
        r["fused_score"] = float(r["fused_score"])
    return rows


def _seed_ada_and_decoy():
    ada = _seed_entity(kind="person", canonical_name="Ada Lovelace", aliases=["Ada"])
    decoy = _seed_entity(kind="person", canonical_name="Ad")
    return ada, decoy


def test_resolve_entity_perfect_alias_outranks_phonetic_decoy():
    """With the phon-only decoy 'Ad' present, resolving 'Ada' must return the exact
    alias holder 'Ada Lovelace' at rank 1. The 0.05 phon bonus put 'Ad' at rank 1 (#17)."""
    ada, decoy = _seed_ada_and_decoy()
    order = [r["entity_id"] for r in _resolve_full("Ada")]
    assert ada in order and decoy in order
    assert order.index(ada) < order.index(decoy)
    assert order[0] == ada


def test_claim_writer_binds_exact_alias_over_phonetic_decoy():
    """The claim_writer top_k=1 path gated on trgm >= 0.85. It got the phon-ranked
    'Ad' at trgm 0.4, failed the gate, and minted the duplicate #171 exists to prevent."""
    ada, _ = _seed_ada_and_decoy()
    acc = new_accumulator()
    with connection.cursor() as cur:
        bound = _resolve_or_create_entity(cur, "Ada", "person", None, acc)
    assert bound == ada
    assert acc["entities_created_for_objects"] == 0


def test_entity_resolver_matches_alias_and_queues_no_candidate_against_decoy():
    """entity_resolver's top_k=1 path got 'Ad' at trgm 0.4, created a new entity, and could
    queue a merge_candidate against the wrong existing one. It must instead match 'Ada
    Lovelace' and leave the decoy out of the review queue entirely."""
    ada, decoy = _seed_ada_and_decoy()
    exp = _seed_experience()
    with connection.cursor() as cur:
        outcome = resolve_or_create_entity(
            cur,
            exp,
            None,
            surface="Ada",
            field="people",
            kind="person",
        )
    assert outcome["action"] == "matched"
    assert outcome["entity_id"] == ada
    decoy_candidates = _scalar(
        "select count(*) from brain.merge_candidates "
        "where entity_a = %s::uuid or entity_b = %s::uuid",
        [decoy, decoy],
    )
    assert decoy_candidates == 0


def test_resolve_entity_alias_only_phonetic_match_sets_phon_match():
    """Option 4: the phon channel is alias-aware. 'Ada Lovelace' (dmetaphone 'ATLF')
    phon-matches 'Ada' ('AT') only through its alias, so phon_match proves the alias
    branch fired — canonical-only phon left it false (#17)."""
    ada, _ = _seed_ada_and_decoy()
    by_id = {r["entity_id"]: r for r in _resolve_full("Ada")}
    assert ada in by_id
    assert by_id[ada]["phon_match"] is True


def test_resolve_entity_phonetic_tie_no_trgm_signal_ranks_deterministically():
    """Regression against the 2026-06-05 over-collapse: two entities that only phon-match
    the query (no trgm signal on either side) must tie at the tiebreak-only bonus — equal
    fused_scores, each below one RRF step (0.000264) — not the old 0.05 that let phon
    dominate. dmetaphone('Ksyth') = dmetaphone('Qseth') = dmetaphone('Cassoth') = 'KS0';
    similarity('Qseth','Ksyth') and similarity('Cassoth','Ksyth') are both < 0.3, so
    neither surfaces in the trgm channel."""
    a = _seed_entity(kind="person", canonical_name="Qseth")
    b = _seed_entity(kind="person", canonical_name="Cassoth")
    by_id = {r["entity_id"]: r for r in _resolve_full("Ksyth", top_k=50)}
    assert a in by_id and b in by_id
    assert by_id[a]["phon_match"] is True and by_id[b]["phon_match"] is True
    assert by_id[a]["trgm_score"] == 0.0 and by_id[b]["trgm_score"] == 0.0
    assert by_id[a]["fused_score"] == by_id[b]["fused_score"]
    assert by_id[a]["fused_score"] < 0.000264

# --- #16: second-stage verification in the resolver's borderline band ----------
#
# The trgm band (0.55-0.85) that used to queue every pair now runs a Jaro-Winkler
# verification first. A confident match is soft-auto-merged (audited + reversible)
# instead of queued; a weak one still queues exactly as before. Names are
# distinctive so they don't collide with the shared dev brain, and embedding=None
# keeps the vec channel out so trgm alone lands the pair in the band.
#
# similarity('John Zorptangle','Jon Zorptangle') ≈ 0.72 (in band, below the 0.85
# match bar) while Jaro-Winkler ≈ 0.98 (>= AUTO_MERGE_THRESHOLD) — the exact shape
# the second stage exists to rescue from the queue.


def _resolve_new(surface, kind, exp):
    with connection.cursor() as cur:
        return resolve_or_create_entity(
            cur, exp, None, surface=surface, field="people", kind=kind
        )


def test_borderline_confident_verification_auto_merges_and_is_reversible():
    existing = _seed_entity(kind="person", canonical_name="Jon Zorptangle")
    exp = _seed_experience()

    outcome = _resolve_new("John Zorptangle", "person", exp)

    assert outcome["action"] == "auto_merged"
    assert outcome["entity_id"] == existing  # mentions link to the surviving entity
    assert outcome["verification_score"] >= 0.92
    new_id = outcome["merged_from_entity_id"]

    # The freshly-created entity is soft-merged into the existing one.
    assert (
        _scalar(
            "select merged_into::text from brain.entities where id=%s::uuid", [new_id]
        )
        == existing
    )
    # The merge is audited: exactly one correction_events row for the loser.
    assert (
        _scalar(
            "select count(*) from brain.correction_events "
            "where target_kind='entity' and target_id=%s::uuid",
            [new_id],
        )
        == 1
    )
    # The candidate row is recorded and resolved to 'merged' so a reversal reopens it.
    lo, hi = sorted([existing, new_id])
    assert (
        _scalar(
            "select status from brain.merge_candidates "
            "where entity_a=%s::uuid and entity_b=%s::uuid",
            [lo, hi],
        )
        == "merged"
    )

    # Reversibility: unmerge clears the pointer and reopens the candidate.
    unmerge_entity(new_id)
    assert (
        _scalar("select merged_into from brain.entities where id=%s::uuid", [new_id])
        is None
    )
    assert (
        _scalar(
            "select status from brain.merge_candidates "
            "where entity_a=%s::uuid and entity_b=%s::uuid",
            [lo, hi],
        )
        == "pending"
    )


def test_borderline_weak_verification_still_queues():
    # similarity('Vorptangle','Zorptangle') ≈ 0.57 (in band) but a single-token
    # person pair verifies at 0.0 — it must queue, not auto-merge, exactly as before.
    existing = _seed_entity(kind="person", canonical_name="Zorptangle")
    exp = _seed_experience()

    outcome = _resolve_new("Vorptangle", "person", exp)

    assert outcome["action"] == "borderline"
    assert outcome["borderline_entity_id"] == existing
    assert outcome["verification_score"] < 0.92
    new_id = outcome["entity_id"]
    assert (
        _scalar("select merged_into from brain.entities where id=%s::uuid", [new_id])
        is None
    )
    lo, hi = sorted([existing, new_id])
    assert (
        _scalar(
            "select status from brain.merge_candidates "
            "where entity_a=%s::uuid and entity_b=%s::uuid",
            [lo, hi],
        )
        == "pending"
    )


# --- #16: the batch dedup_entities command's write path ------------------------


def test_dedup_command_apply_merges_executes_and_audits():
    from openbrain.brain.management.commands.dedup_entities import Command

    winner = _seed_entity(kind="person", canonical_name="Jon Zorptangle")
    loser = _seed_entity(kind="person", canonical_name="John Zorptangle")

    merged = Command()._apply_merges([(loser, winner, 0.98)])

    assert merged == 1
    assert (
        _scalar(
            "select merged_into::text from brain.entities where id=%s::uuid", [loser]
        )
        == winner
    )
    assert (
        _scalar(
            "select count(*) from brain.correction_events "
            "where target_kind='entity' and target_id=%s::uuid",
            [loser],
        )
        == 1
    )


def test_dedup_command_apply_queue_writes_pending_candidate():
    from openbrain.brain.management.commands.dedup_entities import Command

    a = _seed_entity(kind="person", canonical_name="Zorptangle")
    b = _seed_entity(kind="person", canonical_name="Vorptangle")
    lo, hi = sorted([a, b])

    queued = Command()._apply_queue([(lo, hi, 0.57)])

    assert queued == 1
    assert (
        _scalar(
            "select status from brain.merge_candidates "
            "where entity_a=%s::uuid and entity_b=%s::uuid",
            [lo, hi],
        )
        == "pending"
    )
