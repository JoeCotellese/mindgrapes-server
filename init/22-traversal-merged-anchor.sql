-- ABOUTME: relationships_to now matches edges on the merge-normalized anchor, not the stored id.
-- ABOUTME: Fixes #81 — a soft-merged node's own edges were unreachable from survivor and loser alike.
--
-- Re-declares brain.relationships_to (last defined in init/15-confidence-traversal.sql).
-- Signature, return shape, hop semantics, and the confidence product are all unchanged —
-- callers must not change. One delta, in the join that finds each edge.
--
-- The walk normalizes every node through merged_into (the seed CTE, and the counter-party
-- of each step). But it matched claims on the *stored* anchor:
--
--     join brain.claims c on (c.subject_id = w.node or c.object_entity_id = w.node)
--
-- w.node is always a survivor id; a soft merge never rewrites claims, so subject_id still
-- holds the loser id. They never meet. The result: merging a node made its own edges
-- invisible from the survivor AND from the loser (whose seed resolves to the survivor
-- first) — traversal silently lost every edge the merge was supposed to consolidate.
-- init/06-tools.sql's comment already promised the fixed behaviour ("follows merged_into
-- ... so a soft-merge survivor surfaces in place of its loser"); only the counter-party
-- half was ever implemented.
--
-- The fix normalizes both endpoints once, in an `edges` CTE, so the walk compares
-- survivor ids to survivor ids on both sides. Materializing the edge set also drops the
-- per-step join to brain.entities the old form needed for the counter-party.
--
-- Two consequences worth naming:
--   * An edge between two members of the same merge group collapses to a self-edge
--     (a = b). It matches, proposes w.node itself, and the existing visited-array guard
--     drops it — a merged group never reports itself as its own neighbour.
--   * min(hops) can now be shorter for a node reachable through an inherited edge. That
--     is the point: the survivor genuinely is that many hops away once the merge is real.
--
-- Idempotent: CREATE OR REPLACE; re-applying on an existing volume is safe.

create or replace function brain.relationships_to(
  p_entity_id       uuid,
  p_max_hops        int default 2,
  p_min_confidence  real default 0
) returns table (
  entity_id   uuid,
  hops        int,
  confidence  real
)
language sql stable as $$
  with recursive
  -- Every non-retracted entity-to-entity claim as an undirected edge between the
  -- *survivors* of its two endpoints, so a claim stored against a merged loser is
  -- found when walking the node it was merged into.
  edges as (
    select coalesce(s.merged_into, s.id) as a,
           coalesce(o.merged_into, o.id) as b,
           c.confidence                  as confidence
      from brain.claims c
      join brain.entities s on s.id = c.subject_id
      join brain.entities o on o.id = c.object_entity_id
     where c.polarity <> 'retracted'
       and c.object_entity_id is not null
  ),
  seed as (
    select coalesce(e.merged_into, e.id) as id
      from brain.entities e
     where e.id = p_entity_id
  ),
  walk(node, hops, conf, visited) as (
    select s.id, 0, 1.0::real, array[s.id]
      from seed s
    union all
    select case when e.a = w.node then e.b else e.a end as node,
           w.hops + 1,
           (w.conf * e.confidence)::real,
           w.visited || case when e.a = w.node then e.b else e.a end
      from walk w
      join edges e
        on (e.a = w.node or e.b = w.node)
     where w.hops < p_max_hops
       and not (case when e.a = w.node then e.b else e.a end = any(w.visited))
       and (w.conf * e.confidence) >= p_min_confidence
  )
  select node as entity_id, min(hops) as hops, max(conf)::real as confidence
    from walk
   where hops > 0
   group by node
   order by min(hops), node;
$$;

insert into brain.schema_version (version, description)
  values (17, 'traversal: relationships_to matches merge-normalized anchors (#81)')
  on conflict (version) do nothing;
