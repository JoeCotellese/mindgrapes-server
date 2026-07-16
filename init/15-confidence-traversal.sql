-- ABOUTME: relationships_to now propagates path confidence (∏ edge confidence) + an optional floor.
-- ABOUTME: Consumes claims.confidence in graph traversal (issue #160) — stored-but-unread until now.
--
-- Re-declares brain.relationships_to (last defined in init/06-tools.sql) to:
--   * carry a cumulative path confidence = product of each edge's claims.confidence
--     along the walk (the seed starts at 1.0), returned as a new `confidence` column;
--   * drop any path whose running confidence falls below p_min_confidence. The prune
--     lives in the recursive step, which is sound because the product is monotone
--     non-increasing (every factor is in (0,1]) — once below the floor it stays below.
--
-- p_min_confidence default 0 keeps the old 2-arg signature behaviour intact: with a
-- 0 floor nothing is pruned, so existing callers get the same node set plus the
-- additive confidence column. The opinionated 0.6 product default lives at the MCP
-- tool layer (matches review_queue(low_confidence_claims)), not here.
--
-- Per node we return min(hops) and max(confidence): the shortest reach and the
-- best-supported surviving path to it. These can come from different paths, but each
-- is the strongest signal on its own axis.
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
      join brain.claims c
        on (c.subject_id = w.node or c.object_entity_id = w.node)
       and c.polarity <> 'retracted'
       and c.object_entity_id is not null
      join brain.entities other
        on other.id = case
             when c.subject_id = w.node then c.object_entity_id
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

insert into brain.schema_version (version, description)
  values (10, 'confidence-traversal: relationships_to propagates ∏ edge confidence + floor (#160)')
  on conflict (version) do nothing;
