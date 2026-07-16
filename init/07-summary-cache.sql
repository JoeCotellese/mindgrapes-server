-- Mind Grapes v2 — brain://summary materialized view (issue #16, Phase 4).
--
-- Powers the brain://summary MCP resource (spec §3.5). Keeping the
-- aggregation in a matview means a session-start read costs one
-- single-row fetch even on a corpus of tens of thousands of experiences;
-- a nightly pg_cron job at 03:00 UTC keeps it fresh without colliding
-- with brain-recompute-salience at 04:00.
--
-- Counts:
--   - experience_count: every captured experience (no policy on consolidation status)
--   - entity_count:     canonical entities only (merged_into is null)
--   - claim_count:      non-retracted claims
--
-- top_entities ranks by mention count, restricted to canonical entities so
-- a soft-merged loser doesn't double up the winner; mention rows for the
-- loser still flow into the winner's count by joining on the canonical id.
--
-- top_topics walks metadata->'topics' across all experiences. We keep
-- this in plain SQL rather than maintaining a separate topic table — the
-- aggregation runs once per refresh, so the cost is acceptable while the
-- corpus is small.
--
-- Idempotent: re-applying against an existing volume is safe via
-- "create materialized view if not exists" + "create or replace function"
-- + the unschedule/schedule pattern. Apply on existing volumes with:
--   bin/pg psql -f init/07-summary-cache.sql

create materialized view if not exists brain.summary_cache as
with
  singleton as (select 1::int as id),
  exp_counts as (
    select count(*)::bigint as c,
           min(captured_at) as earliest,
           max(captured_at) as latest
      from brain.experiences
  ),
  ent_count as (
    select count(*)::bigint as c
      from brain.entities
     where merged_into is null
  ),
  claim_count as (
    select count(*)::bigint as c
      from brain.claims
     where polarity <> 'retracted'
  ),
  top_entities as (
    select coalesce(jsonb_agg(row_to_json(t)::jsonb order by t.mention_count desc), '[]'::jsonb) as arr
      from (
        select e.id::text     as id,
               e.canonical_name,
               e.kind::text   as kind,
               count(m.*)::int as mention_count
          from brain.entities e
          join brain.mentions m on m.entity_id = e.id
         where e.merged_into is null
         group by e.id, e.canonical_name, e.kind
         order by count(m.*) desc, e.canonical_name
         limit 10
      ) t
  ),
  top_topics as (
    select coalesce(jsonb_agg(row_to_json(t)::jsonb order by t.count desc), '[]'::jsonb) as arr
      from (
        select topic, count(*)::int as count
          from brain.experiences,
               lateral jsonb_array_elements_text(coalesce(metadata->'topics', '[]'::jsonb)) as topic
         group by topic
         order by count(*) desc, topic
         limit 10
      ) t
  )
select
  singleton.id              as singleton,
  exp_counts.c              as experience_count,
  ent_count.c               as entity_count,
  claim_count.c             as claim_count,
  exp_counts.earliest       as time_range_earliest,
  exp_counts.latest         as time_range_latest,
  top_entities.arr          as top_entities,
  top_topics.arr            as top_topics,
  now()                     as refreshed_at
from singleton, exp_counts, ent_count, claim_count, top_entities, top_topics;

-- Unique single-row index lets us refresh CONCURRENTLY without exclusive
-- lock (matters when summary readers are hitting the view during refresh).
-- The view always returns exactly one row keyed on the singleton column;
-- PostgreSQL rejects unique indexes built on constant expressions for the
-- concurrent-refresh check, so we synthesize the constant as a view column.
create unique index if not exists summary_cache_singleton_idx
  on brain.summary_cache (singleton);

create or replace procedure brain.refresh_summary_cache()
language plpgsql
as $$
begin
  -- CONCURRENTLY requires a populated view; the first refresh on a fresh
  -- volume must be non-concurrent, after which the unique index allows
  -- concurrent refreshes. matviewname-qualified existence check picks the
  -- right path automatically.
  if not exists (
    select 1
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'brain'
       and c.relname = 'summary_cache'
       and c.relkind = 'm'
       and pg_relation_size(c.oid) > 0
  ) then
    refresh materialized view brain.summary_cache;
  else
    refresh materialized view concurrently brain.summary_cache;
  end if;
end;
$$;

-- Populate on first install so reads against a fresh volume don't error
-- with "materialized view ... has not been populated".
do $$ begin
  call brain.refresh_summary_cache();
end $$;

do $$
begin
  begin perform cron.unschedule('brain-refresh-summary-cache'); exception when others then null; end;
  perform cron.schedule(
    'brain-refresh-summary-cache',
    '0 3 * * *',
    $cron$ call brain.refresh_summary_cache(); $cron$
  );
end $$;

insert into brain.schema_version (version, description)
  values (5, 'summary_cache: nightly-refreshed materialized view for brain://summary')
  on conflict (version) do nothing;
