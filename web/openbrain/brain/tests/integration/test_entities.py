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
from django.db import IntegrityError, connection
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


def test_merge_identical_name_empty_aliases_surfaces_not_null():
    # Issue #40 (open): merging two entities that share canonical_name with empty
    # aliases makes array_agg(distinct ...) return NULL, violating aliases NOT
    # NULL. Kept so the merge path keeps surfacing the same
    # sqlstate (23502) — the error shape #120 calls out.
    winner = _seed_entity(canonical_name="PostgreSQL", aliases=[])
    loser = _seed_entity(canonical_name="PostgreSQL", aliases=[])
    with pytest.raises(IntegrityError) as exc:
        merge_entities(loser, winner)
    assert getattr(exc.value.__cause__, "sqlstate", None) == "23502"


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
