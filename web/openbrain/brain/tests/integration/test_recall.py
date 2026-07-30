"""Integration tests for the recall services (Slice C, #120).

Seeds experiences/entities/mentions/claims, runs a recall service, and asserts
the result against the real brain.* schema; brain_write_txn rolls everything
back. The shared dev DB already holds rows, so assertions check that seeded ids
are present/absent rather than asserting exact counts.

Requires the dev stack up (make dev-up); run via make dev-test-integration.
"""

import uuid

import pytest
from django.db import connection
from django.test import override_settings

from openbrain.brain.services.recall import (
    recall_recent,
    relationships_to,
    who_was_at,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("brain_write_txn")]

VIEWER = "itest-recall-viewer"
OTHER = "itest-recall-other"
_VEC = "[" + ",".join(["0.01"] * 1536) + "]"
_ZERO = [0.0] * 1536


def _zero_embed(text):
    return _ZERO


def _new_id():
    return str(uuid.uuid4())


def _seed_experience(
    *, owner=VIEWER, visibility="private", content="seed", occurred_at=None
):
    eid = _new_id()
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.experiences "
            "(id, content, embedding, owner, visibility, occurred_at) "
            "values (%s::uuid, %s, %s::vector, %s, %s::brain.visibility, %s::timestamptz)",
            [eid, content, _VEC, owner, visibility, occurred_at],
        )
    return eid


def _seed_entity(kind="person", canonical_name=None):
    eid = _new_id()
    name = canonical_name or f"itest-{eid[:8]}"
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.entities (id, kind, canonical_name) "
            "values (%s::uuid, %s::brain.entity_kind, %s)",
            [eid, kind, name],
        )
    return eid


def _seed_mention(experience_id, entity_id, surface_form, field="people"):
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.mentions (experience_id, entity_id, surface_form, field) "
            "values (%s::uuid, %s::uuid, %s, %s)",
            [experience_id, entity_id, surface_form, field],
        )


def _seed_claim(
    subject_id,
    object_entity_id,
    predicate="knows",
    polarity="asserted",
    confidence=None,
):
    cid = _new_id()
    with connection.cursor() as cur:
        cur.execute(
            "insert into brain.claims "
            "(id, subject_id, object_entity_id, predicate, polarity, confidence) "
            "values (%s::uuid, %s::uuid, %s::uuid, %s, %s::brain.polarity, coalesce(%s, 0.5))",
            [cid, subject_id, object_entity_id, predicate, polarity, confidence],
        )
    return cid


# --- recall_recent ------------------------------------------------------------


def test_recall_recent_days_must_be_positive():
    with pytest.raises(ValueError, match="days must be > 0"):
        recall_recent(VIEWER, None, 0)


def test_recall_recent_no_query_lists_recent_owned():
    a = _seed_experience(content="recent thought a")
    b = _seed_experience(content="recent thought b")
    result = recall_recent(VIEWER, None, 7)
    ids = {h["id"] for h in result["hits"]}
    assert {a, b} <= ids
    # No-query path zeroes the scores.
    for hit in result["hits"]:
        if hit["id"] in {a, b}:
            assert hit["vec_score"] == 0 and hit["fused_score"] == 0


def test_recall_recent_excludes_other_members_private_rows():
    mine = _seed_experience(owner=VIEWER, visibility="private")
    theirs_private = _seed_experience(owner=OTHER, visibility="private")
    theirs_shared = _seed_experience(owner=OTHER, visibility="shared")
    ids = {h["id"] for h in recall_recent(VIEWER, None, 7)["hits"]}
    assert mine in ids
    assert theirs_shared in ids
    assert theirs_private not in ids


@override_settings(
    BRAIN_EMBED_FN="openbrain.brain.tests.integration.test_recall._zero_embed"
)
def test_recall_recent_with_query_runs_hybrid():
    seeded = _seed_experience(content="the zorblax planning session went long")
    hits = recall_recent(VIEWER, "zorblax", 7)["hits"]
    assert seeded in {h["id"] for h in hits}


# --- who_was_at ---------------------------------------------------------------


def test_who_was_at_requires_an_argument():
    with pytest.raises(ValueError, match="must supply experience_id or date"):
        who_was_at()


def test_who_was_at_by_experience():
    exp = _seed_experience()
    ent = _seed_entity(canonical_name="Dinner Guest")
    _seed_mention(exp, ent, "Dinner Guest")
    result = who_was_at(experience_id=exp)
    assert result["resolved_via"] == "experience_id"
    assert ent in {e["entity_id"] for e in result["entities"]}


def test_who_was_at_by_date():
    exp = _seed_experience(occurred_at="2026-03-14T19:00:00+00:00")
    ent = _seed_entity(canonical_name="Pi Day Attendee")
    _seed_mention(exp, ent, "Pi Day Attendee")
    result = who_was_at(date="2026-03-14")
    assert result["resolved_via"] == "date"
    assert ent in {e["entity_id"] for e in result["entities"]}


# --- relationships_to ---------------------------------------------------------


def test_relationships_to_bounds_max_hops():
    with pytest.raises(ValueError, match="max_hops must be between 1 and 3"):
        relationships_to(_new_id(), max_hops=0)


def test_relationships_to_rejects_depth_past_the_ceiling():
    # Measured on a production-shaped copy (5.7k entities, 9.7k claims): each hop
    # past 3 costs ~8x more and adds almost nothing — h3 685ms/1229 nodes,
    # h4 6.4s/1346, h5 47s/1389, h6 153s. The walk expands per path, not per node,
    # so depth explodes while reachability plateaus. Ceiling is 3 until that is
    # fixed properly.
    with pytest.raises(ValueError, match="max_hops must be between 1 and 3"):
        relationships_to(_new_id(), max_hops=4)


def test_relationships_to_reaches_one_hop_neighbor():
    seed = _seed_entity(canonical_name="Seed Person")
    neighbor = _seed_entity(canonical_name="Neighbor Person")
    _seed_claim(seed, neighbor)
    result = relationships_to(seed, max_hops=2)
    assert result["seed_entity_id"] == seed
    by_id = {r["entity_id"]: r for r in result["related"]}
    assert neighbor in by_id
    assert by_id[neighbor]["hops"] == 1


def _seed_confidence_fixture():
    """A seed with a strong 2-hop chain (0.9·0.9=0.81) and a shaky 3-hop chain
    (0.9·0.5·0.4=0.18). Returns the entity ids keyed by role."""
    seed = _seed_entity(canonical_name="Conf Seed")
    a = _seed_entity(canonical_name="Conf Strong A")
    b = _seed_entity(canonical_name="Conf Strong B")
    x = _seed_entity(canonical_name="Conf Shaky X")
    y = _seed_entity(canonical_name="Conf Shaky Y")
    z = _seed_entity(canonical_name="Conf Shaky Z")
    _seed_claim(seed, a, confidence=0.9)
    _seed_claim(a, b, confidence=0.9)
    _seed_claim(seed, x, confidence=0.9)
    _seed_claim(x, y, confidence=0.5)
    _seed_claim(y, z, confidence=0.4)
    return {"seed": seed, "a": a, "b": b, "x": x, "y": y, "z": z}


def test_relationships_to_propagates_edge_confidence():
    f = _seed_confidence_fixture()
    by_id = {
        r["entity_id"]: r
        for r in relationships_to(f["seed"], max_hops=3, min_confidence=0)["related"]
    }
    # confidence = product of edge confidences along the path.
    assert by_id[f["a"]]["confidence"] == pytest.approx(0.9, abs=1e-3)
    assert by_id[f["b"]]["confidence"] == pytest.approx(0.81, abs=1e-3)
    assert by_id[f["y"]]["confidence"] == pytest.approx(0.45, abs=1e-3)
    assert by_id[f["z"]]["confidence"] == pytest.approx(0.18, abs=1e-3)


def test_relationships_to_floor_prunes_shaky_chain():
    f = _seed_confidence_fixture()
    by_id = {
        r["entity_id"]: r
        for r in relationships_to(f["seed"], max_hops=3, min_confidence=0.6)["related"]
    }
    # Strong chain survives the 0.6 floor; the shaky chain is pruned at Y (0.45).
    assert f["a"] in by_id
    assert f["b"] in by_id
    assert f["x"] in by_id  # first shaky edge is 0.9, still above the floor
    assert f["y"] not in by_id
    assert f["z"] not in by_id


def test_relationships_to_floor_omitted_drops_nothing():
    # Guardrail: omitting min_confidence (service default 0) returns the same node
    # set the old 2-arg signature did — the shaky terminal still surfaces.
    f = _seed_confidence_fixture()
    reached = {
        r["entity_id"] for r in relationships_to(f["seed"], max_hops=3)["related"]
    }
    assert {f["a"], f["b"], f["x"], f["y"], f["z"]} <= reached


def test_relationships_to_rejects_out_of_range_min_confidence():
    with pytest.raises(ValueError, match="min_confidence must be between 0 and 1"):
        relationships_to(_new_id(), min_confidence=1.5)


def _merge_entity(loser_id, winner_id):
    """Soft-merge without the service: the contract under test is that traversal
    follows merged_into, not that merge_entities sets it."""
    with connection.cursor() as cur:
        cur.execute(
            "update brain.entities set merged_into = %s::uuid where id = %s::uuid",
            [winner_id, loser_id],
        )


def test_relationships_to_reaches_outbound_edge_of_a_merged_loser():
    # A soft merge never rewrites claims, so the loser's edges stay stored against
    # the loser id. Walking the survivor must still reach them (#81).
    survivor = _seed_entity(canonical_name="Merge Survivor Out")
    loser = _seed_entity(canonical_name="Merge Loser Out")
    own = _seed_entity(canonical_name="Survivor Neighbor Out")
    inherited = _seed_entity(canonical_name="Loser Neighbor Out")
    _seed_claim(survivor, own)
    _seed_claim(loser, inherited)
    _merge_entity(loser, survivor)

    reached = {
        r["entity_id"]
        for r in relationships_to(survivor, max_hops=1, min_confidence=0)["related"]
    }
    assert own in reached
    assert inherited in reached
    # The tombstone itself is normalized away, never returned as a node.
    assert loser not in reached


def test_relationships_to_reaches_inbound_edge_of_a_merged_loser():
    # Same defect on the object side: a claim pointing AT the loser.
    survivor = _seed_entity(canonical_name="Merge Survivor In")
    loser = _seed_entity(canonical_name="Merge Loser In")
    pointer = _seed_entity(canonical_name="Points At Loser")
    _seed_claim(pointer, loser)
    _merge_entity(loser, survivor)

    reached = {
        r["entity_id"]
        for r in relationships_to(survivor, max_hops=1, min_confidence=0)["related"]
    }
    assert pointer in reached


def test_relationships_to_seeding_a_tombstone_matches_its_survivor():
    # Seeding the loser id resolves to the survivor, so both ids see one graph.
    # Both sides must carry the inherited edge — asserting only set equality would
    # pass on the pre-fix function, where both sides were equally empty.
    survivor = _seed_entity(canonical_name="Merge Survivor Seed")
    loser = _seed_entity(canonical_name="Merge Loser Seed")
    inherited = _seed_entity(canonical_name="Loser Neighbor Seed")
    own = _seed_entity(canonical_name="Survivor Neighbor Seed")
    _seed_claim(loser, inherited)
    _seed_claim(survivor, own)
    _merge_entity(loser, survivor)

    from_survivor = {
        r["entity_id"]
        for r in relationships_to(survivor, max_hops=1, min_confidence=0)["related"]
    }
    from_loser = {
        r["entity_id"]
        for r in relationships_to(loser, max_hops=1, min_confidence=0)["related"]
    }
    assert {inherited, own} <= from_survivor
    assert {inherited, own} <= from_loser
    assert from_survivor == from_loser


def test_relationships_to_inherited_edge_carries_correct_hops_and_confidence():
    # An inherited edge is a real 1-hop edge of the survivor, at the claim's own
    # confidence — not a discounted or extra-hop path.
    survivor = _seed_entity(canonical_name="Metric Survivor")
    loser = _seed_entity(canonical_name="Metric Loser")
    near = _seed_entity(canonical_name="Metric Near")
    far = _seed_entity(canonical_name="Metric Far")
    _seed_claim(loser, near, confidence=0.9)
    _seed_claim(near, far, confidence=0.5)
    _merge_entity(loser, survivor)

    by_id = {
        r["entity_id"]: r
        for r in relationships_to(survivor, max_hops=2, min_confidence=0)["related"]
    }
    assert by_id[near]["hops"] == 1
    assert by_id[near]["confidence"] == pytest.approx(0.9, abs=1e-3)
    # And the chain continues through the inherited edge, compounding normally.
    assert by_id[far]["hops"] == 2
    assert by_id[far]["confidence"] == pytest.approx(0.45, abs=1e-3)


def test_relationships_to_floor_prunes_a_shaky_inherited_edge():
    # The confidence floor applies to inherited edges exactly as to native ones.
    survivor = _seed_entity(canonical_name="Floor Survivor")
    loser = _seed_entity(canonical_name="Floor Loser")
    strong = _seed_entity(canonical_name="Floor Strong")
    shaky = _seed_entity(canonical_name="Floor Shaky")
    _seed_claim(loser, strong, confidence=0.9)
    _seed_claim(loser, shaky, confidence=0.5)
    _merge_entity(loser, survivor)

    reached = {
        r["entity_id"]
        for r in relationships_to(survivor, max_hops=1, min_confidence=0.6)["related"]
    }
    assert strong in reached
    assert shaky not in reached


def test_relationships_to_collapses_an_edge_inside_one_merge_group():
    # Both endpoints merge into the same survivor, so the claim between them
    # resolves to a self-edge; the visited guard drops it and neither loser is
    # reported as a neighbour of the group. Documented in init/22, pinned here.
    survivor = _seed_entity(canonical_name="Selfedge Survivor")
    left = _seed_entity(canonical_name="Selfedge Left")
    right = _seed_entity(canonical_name="Selfedge Right")
    outside = _seed_entity(canonical_name="Selfedge Outside")
    _seed_claim(left, right)
    _seed_claim(left, outside)
    _merge_entity(left, survivor)
    _merge_entity(right, survivor)

    reached = {
        r["entity_id"]
        for r in relationships_to(survivor, max_hops=2, min_confidence=0)["related"]
    }
    assert outside in reached
    assert survivor not in reached
    assert left not in reached and right not in reached


def test_relationships_to_after_a_chained_merge_is_flattened_by_path_compression():
    # merge_entities path-compresses, so A->B plus merge(B, C) leaves A and B both
    # pointing at C. Traversal from C must therefore see A's edges too — the case
    # that stranded four entities on the live brain during #79.
    from openbrain.brain.services.entities import merge_entities

    c = _seed_entity(canonical_name="Chained Root")
    b = _seed_entity(canonical_name="Chained Middle")
    a = _seed_entity(canonical_name="Chained Leaf")
    a_edge = _seed_entity(canonical_name="Chained Leaf Neighbor")
    b_edge = _seed_entity(canonical_name="Chained Middle Neighbor")
    _seed_claim(a, a_edge)
    _seed_claim(b, b_edge)
    _merge_entity(a, b)

    merge_entities(b, c)

    reached = {
        r["entity_id"]
        for r in relationships_to(c, max_hops=1, min_confidence=0)["related"]
    }
    assert b_edge in reached
    assert a_edge in reached, "the twice-merged node's edges were lost"
