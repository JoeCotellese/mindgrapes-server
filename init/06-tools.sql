-- Mind Grapes v2 — review/disambiguation/relationships scaffolding for issue #13.
--
-- Adds two queue tables and a graph-traversal SQL function that back the
-- twelve new MCP tools (merge/rename/retract/recall/resolve/review/
-- disambiguation). All other tools compose existing schema (entities,
-- claims, claim_sources, mentions, correction_events, merge_candidates,
-- experiences) directly from TS.
--
-- New tables:
--   brain.proposed_corrections — non-destructive queue for propose_correction.
--   brain.disambiguations      — token-keyed bag of pending user choices for
--                                request_disambiguation / resolve_disambiguation.
--
-- New function:
--   brain.relationships_to(entity_id, max_hops) — recursive CTE walking
--   non-retracted claims (subject <-> object), following merged_into so a
--   soft-merge can't sever a path, returning each reachable entity with the
--   minimum hop count.
--
-- Idempotent: re-applying against an existing volume is safe via
-- "create table if not exists" + "create or replace function". Apply on
-- existing volumes with: bin/pg psql -f init/06-tools.sql

create table if not exists brain.proposed_corrections (
  id                uuid primary key default gen_random_uuid(),
  target_kind       brain.target_kind not null,
  target_id         uuid not null,
  suggested_change  jsonb not null,
  reason            text,
  status            text not null default 'pending'
    check (status in ('pending', 'applied', 'rejected', 'cancelled')),
  created_at        timestamptz not null default now(),
  resolved_at       timestamptz,
  resolved_by       text
);

create index if not exists proposed_corrections_pending_idx
  on brain.proposed_corrections (created_at) where status = 'pending';

create index if not exists proposed_corrections_target_idx
  on brain.proposed_corrections (target_kind, target_id);

-- Disambiguation tokens. Each request_disambiguation row stays 'pending' until
-- a paired resolve_disambiguation lands the user's choice. options is a jsonb
-- array of {label, value?} objects so callers can pass arbitrary structured
-- payloads (e.g. {label: "Ada Lott", value: {entity_id: "..."}}).
create table if not exists brain.disambiguations (
  token            uuid primary key default gen_random_uuid(),
  question         text not null,
  options          jsonb not null,
  status           text not null default 'pending'
    check (status in ('pending', 'resolved', 'cancelled')),
  resolved_choice  jsonb,
  context          jsonb,
  created_at       timestamptz not null default now(),
  resolved_at      timestamptz
);

create index if not exists disambiguations_pending_idx
  on brain.disambiguations (created_at) where status = 'pending';

-- Recursive relationship traversal. Walks the claim graph treating each
-- non-retracted entity-to-entity claim as an edge between subject_id and
-- object_entity_id. Follows merged_into one level on the seed and on the
-- counter-party of every edge so a soft-merge survivor surfaces in place of
-- its loser. Returns reachable entity ids with the minimum hop count.
create or replace function brain.relationships_to(
  p_entity_id  uuid,
  p_max_hops   int default 2
) returns table (
  entity_id  uuid,
  hops       int
)
language sql stable as $$
  with recursive
  seed as (
    select coalesce(e.merged_into, e.id) as id
      from brain.entities e
     where e.id = p_entity_id
  ),
  walk(node, hops, visited) as (
    select s.id, 0, array[s.id]
      from seed s
    union all
    select coalesce(other.merged_into, other.id) as node,
           w.hops + 1,
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
  )
  select node as entity_id, min(hops) as hops
    from walk
   where hops > 0
   group by node
   order by min(hops), node;
$$;

insert into brain.schema_version (version, description)
  values (4, 'tools: proposed_corrections + disambiguations + relationships_to')
  on conflict (version) do nothing;
