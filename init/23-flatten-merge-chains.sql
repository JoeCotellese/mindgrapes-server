-- ABOUTME: Flattens pre-existing merged_into chains so every pointer names a live survivor.
-- ABOUTME: Fixes #81 — init/22's member lookup requires merged_into to be exactly one level deep.
--
-- Data migration, not DDL. Nothing here changes a table or a function; it repoints
-- brain.entities.merged_into rows that reference another tombstone.
--
-- Why this file exists, and why it must ship WITH init/22:
--
-- init/22 expands each walk node to its merge members with a single non-recursive
-- lookup (`where m.merged_into = w.node`), which finds only the entities pointing
-- DIRECTLY at the survivor. Given A -> B -> C, walking C finds B and stops; A's
-- edges stay invisible, and B — a tombstone — is reachable as a result row, which
-- violates the rule that a merged entity never surfaces in place of its survivor.
-- init/22's own comment names the precondition: "The member lookup depends on
-- merged_into being ONE LEVEL DEEP: a pointer to the survivor, never to another
-- tombstone."
--
-- merge_entities_on_cursor now enforces that going forward. It refuses a merged
-- loser and a merged winner, and path-compresses the loser's own losers onto the
-- winner (#81). But that guard only constrains merges made AFTER it ships — it
-- cannot see chains that already exist. Six do, on the production volume; four of
-- them were created by #79's exact-name cleanup batch, which merged in an order
-- the old code had no reason to reject.
--
-- So the invariant has two halves and both are required: the service-layer guard
-- stops new chains, this migration retires the old ones. Applied in ledger order,
-- 22 lands first and is briefly live against unflattened data. That window is
-- bounded by one `brain_ledger migrate` run and costs at most a stale read — the
-- boot gate in boot.py refuses to serve while any manifest entry is pending, so
-- no MCP traffic reaches init/22's function until 23 is stamped.
--
-- The audit rows use the same shape merge_entities_on_cursor writes for path
-- compression — before {merged_into: <old>}, after {merged_into: <survivor>,
-- compressed_via: <old>} — so unmerge_entity's decompression reverses a pointer
-- this migration moved exactly as it reverses one the service moved. Unmerging a
-- mid-chain tombstone restores the entities that used to point at it.
--
-- Idempotent: it resolves chains to their terminal survivor, so a second run finds
-- nothing chained, updates zero rows, and writes zero audit rows. Safe to re-apply.

with recursive
-- Every merged entity, walked forward through merged_into to the end of its chain.
-- seen[] is a cycle guard: a pointer already on the path is not followed, so a
-- cyclic pointer set terminates here and is caught by the verification below
-- rather than silently resolving to an arbitrary node.
chain(id, ptr, depth, seen) as (
  select e.id, e.merged_into, 1, array[e.id, e.merged_into]
    from brain.entities e
   where e.merged_into is not null
  union all
  select c.id, p.merged_into, c.depth + 1, c.seen || p.merged_into
    from chain c
    join brain.entities p on p.id = c.ptr
   where p.merged_into is not null
     and not (p.merged_into = any(c.seen))
),
terminal as (
  select distinct on (id) id, ptr as survivor, depth
    from chain
   order by id, depth desc
),
-- depth > 1 means the stored pointer was NOT the terminal survivor: this row is
-- chained. depth = 1 rows already point straight at a live entity — left alone.
chained as (
  select t.id, e.merged_into as old_ptr, t.survivor
    from terminal t
    join brain.entities e on e.id = t.id
   where t.depth > 1
),
-- Data-modifying CTEs execute exactly once and to completion whether or not the
-- primary query reads them, so the update runs even though only `chained` is
-- selected from below. Both CTEs read the same snapshot, so `chained.old_ptr`
-- holds the pre-update pointer.
moved as (
  update brain.entities e
     set merged_into = c.survivor
    from chained c
   where e.id = c.id
  returning e.id
)
insert into brain.correction_events
  (target_kind, target_id, before, after, reason, created_by)
select 'entity'::brain.target_kind,
       c.id,
       jsonb_build_object('merged_into', c.old_ptr::text),
       jsonb_build_object('merged_into', c.survivor::text,
                          'compressed_via', c.old_ptr::text),
       format('path compression: chain flattened, %s was already merged into %s',
              c.old_ptr, c.survivor),
       'migration:23-flatten-merge-chains'
  from chained c;

-- Verify the invariant init/22 depends on, in the same transaction that claimed to
-- establish it. A survivor is live by definition, so no live merged_into pointer may
-- reference an entity that is itself merged. Raising here rolls back the flatten and
-- leaves the ledger row unwritten (ledger.py applies one transaction per entry), so a
-- cyclic or otherwise unresolvable pointer set fails the migrate loudly instead of
-- handing init/22 data it cannot walk.
do $$
declare
  n int;
begin
  select count(*) into n
    from brain.entities e
    join brain.entities p on p.id = e.merged_into
   where e.merged_into is not null
     and p.merged_into is not null;
  if n > 0 then
    raise exception
      'init/23: % merged_into pointer(s) still reference a tombstone after flattening '
      '— merged_into is likely cyclic; resolve by hand before applying init/22''s traversal',
      n;
  end if;
end $$;

insert into brain.schema_version (version, description)
  values (18, 'flatten-merge-chains: merged_into resolves to a live survivor in one hop (#81)')
  on conflict (version) do nothing;
