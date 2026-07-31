-- ABOUTME: relationships_to walks a survivor's merge members, so merged nodes keep their edges.
-- ABOUTME: Fixes #81 — a soft-merged node's own edges were unreachable from survivor and loser alike.
--
-- Re-declares brain.relationships_to (last defined in init/15-confidence-traversal.sql).
-- Signature, return shape, hop semantics, and the confidence product are all unchanged —
-- callers must not change. One delta, in how each step finds its edges.
--
-- The walk normalized every node through merged_into (the seed CTE, and the counter-party
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
-- The fix expands each walk node to its *merge members* — itself plus every entity merged
-- into it — and anchors the claim lookup on those stored ids. Edges stored against a loser
-- are found when walking its survivor, and the lookup still hits claims_subject_idx /
-- claims_object_entity_idx on a literal id.
--
-- Deliberately NOT normalizing both endpoints inside the join predicate. The obvious form,
--
--     join brain.claims c on (coalesce(s.merged_into, s.id) = w.node or ...)
--
-- reads cleaner but is unindexable: a single-reference CTE is inlined (PG12+), so the
-- predicate becomes an expression over two joined copies of brain.entities and every
-- recursion level degrades to a seq scan of brain.claims plus two full hash builds. At
-- production scale (~9.7k claims, ~6.2k entities) that measured ~500ms for a degree-300
-- seed at the default max_hops=2, against ~4ms for the member-expansion form below, and it
-- grows with total claim count rather than with the seed's degree — which would have made
-- the "cost grows roughly with the local degree of the seed" line in descriptions.py false.
--
-- The member lookup depends on merged_into being ONE LEVEL DEEP: a pointer to the survivor,
-- never to another tombstone. merge_entities_on_cursor enforces that — it refuses a merged
-- loser or a merged winner, and path-compresses the loser's own losers onto the winner. The
-- partial index below is what keeps the member lookup cheap; without it the same query form
-- measured ~20x slower.
--
-- Two consequences worth naming:
--   * An edge between two members of the same merge group resolves to the same node on both
--     ends. It proposes w.node itself, and the existing visited-array guard drops it — a
--     merged group never reports itself as its own neighbour.
--   * min(hops) can now be shorter for a node reachable through an inherited edge. That is
--     the point: the survivor genuinely is that many hops away once the merge is real.
--
-- Idempotent: CREATE OR REPLACE + CREATE INDEX IF NOT EXISTS; safe to re-apply.

create index if not exists entities_merged_into_idx
  on brain.entities (merged_into) where merged_into is not null;

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
  seed as (
    select coalesce(e.merged_into, e.id) as id
      from brain.entities e
     where e.id = p_entity_id
  ),
  walk(node, hops, conf, visited) as (
    select s.id, 0, 1.0::real, array[s.id]
      from seed s
    union all
    select coalesce(other.merged_into, other.id) as node,
           w.hops + 1,
           (w.conf * c.confidence)::real,
           w.visited || coalesce(other.merged_into, other.id)
      from walk w
      -- The survivor's merge members: itself, plus every entity merged into it.
      -- Claims still store the pre-merge ids, so this is what the lookup anchors on.
      join lateral (
        select w.node as id
         union all
        select m.id from brain.entities m where m.merged_into = w.node
      ) mem on true
      join brain.claims c
        on (c.subject_id = mem.id or c.object_entity_id = mem.id)
       and c.polarity <> 'retracted'
       and c.object_entity_id is not null
      join brain.entities other
        on other.id = case
             when c.subject_id = mem.id then c.object_entity_id
             else c.subject_id
           end
     where w.hops < p_max_hops
       and not (coalesce(other.merged_into, other.id) = any(w.visited))
       and (w.conf * c.confidence) >= p_min_confidence
  )
  select node as entity_id, min(hops) as hops, max(conf)::real as confidence
    from walk
   where hops > 0
   group by node
   order by min(hops), node;
$$;

-- init/15 added the 3-arg form as an OVERLOAD rather than replacing the 2-arg one from
-- init/06, leaving the pre-#81 body reachable. Postgres prefers an exact-arity match over
-- default-filling, so any 2-arg call site would silently get the buggy function. Nothing
-- calls it (recall.py always passes three args), so drop it rather than leave the trap.
drop function if exists brain.relationships_to(uuid, int);

insert into brain.schema_version (version, description)
  values (17, 'traversal: relationships_to walks a survivor''s merge members (#81)')
  on conflict (version) do nothing;
