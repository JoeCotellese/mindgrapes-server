-- Issue #48 / Iteration 3: filter superseded and soft-deleted experiences
-- out of every read path.
--
-- init/10-supersede.sql added superseded_by + deleted_at to
-- brain.experiences. The hybrid-search function from init/04 and the
-- summary-cache matview from init/07 were both built before those columns
-- existed, so they still surface dead rows in search results, top-N
-- aggregates, and stats counters. This migration re-declares both so the
-- UI's listing surfaces (search results, entity timelines, stats) only
-- show the live corpus. Direct fetches of an experience id continue to
-- return the row regardless of lifecycle state — the audit trail must
-- stay queryable.
--
-- Order matters: this file must sort after init/10-supersede.sql so the
-- referenced columns exist at apply time.
--
-- Idempotent:
--   - brain.match_brain_hybrid uses CREATE OR REPLACE, which works even
--     when the body changes.
--   - brain.summary_cache is a materialized view (no CREATE OR REPLACE
--     in PostgreSQL) so it is dropped and recreated; the cascade drops
--     the singleton unique index too, and the rebuild reinstalls it. The
--     brain.refresh_summary_cache() procedure and the cron schedule both
--     reference the matview by name and survive the drop/recreate.

-- ---------------------------------------------------------------------------
-- 1. Hybrid-search candidates CTE
-- ---------------------------------------------------------------------------

create or replace function brain.match_brain_hybrid(
  p_query             text,
  p_query_embedding   vector(1536),
  p_limit             int default 10,
  p_filter            jsonb default '{}'::jsonb,
  p_threshold         real default 0.0,
  p_experience_ids    uuid[] default null
) returns table (
  id            uuid,
  content       text,
  metadata      jsonb,
  captured_at   timestamptz,
  occurred_at   timestamptz,
  vec_score     real,
  lex_score     real,
  fused_score   real
)
language sql stable as $$
  with
  parsed as (
    select case
      when nullif(trim(p_query), '') is null then null
      else websearch_to_tsquery('english', p_query)
    end as q
  ),
  candidates as (
    -- Live rows only: superseded and soft-deleted experiences are part of
    -- the audit trail but must not feed the retrieval channels (#48).
    select e.id, e.content, e.metadata, e.captured_at, e.occurred_at,
           e.embedding, e.content_tsv
      from brain.experiences e
     where e.superseded_by is null
       and e.deleted_at is null
       and (p_experience_ids is null or e.id = any(p_experience_ids))
       and (p_filter = '{}'::jsonb or e.metadata @> p_filter)
  ),
  vec as (
    -- Exclude orthogonal/anti-aligned rows: a row with zero cosine similarity
    -- should not earn a vec rank, otherwise small candidate pools allow
    -- irrelevant rows to perturb RRF ordering (issue #33).
    select c.id,
           (1 - (c.embedding <=> p_query_embedding))::real as score,
           row_number() over (order by c.embedding <=> p_query_embedding) as rnk
      from candidates c
     where c.embedding is not null
       and p_query_embedding is not null
       and (c.embedding <=> p_query_embedding) < 1
     order by c.embedding <=> p_query_embedding
     limit 50
  ),
  lex as (
    select c.id,
           ts_rank_cd(c.content_tsv, p.q)::real as score,
           row_number() over (order by ts_rank_cd(c.content_tsv, p.q) desc) as rnk
      from candidates c, parsed p
     where p.q is not null
       and c.content_tsv @@ p.q
     order by ts_rank_cd(c.content_tsv, p.q) desc
     limit 50
  ),
  fused as (
    select coalesce(v.id, l.id)               as id,
           coalesce(v.score, 0)::real         as vec_score,
           coalesce(l.score, 0)::real         as lex_score,
           (
             coalesce(1.0 / (60 + v.rnk), 0)
             + coalesce(1.0 / (60 + l.rnk), 0)
           )::real                            as fused_score
      from vec v
      full outer join lex l on l.id = v.id
  )
  select e.id, e.content, e.metadata, e.captured_at, e.occurred_at,
         f.vec_score, f.lex_score, f.fused_score
    from fused f
    join brain.experiences e on e.id = f.id
   where f.fused_score >= p_threshold
   order by f.fused_score desc, f.vec_score desc, f.lex_score desc
   limit p_limit;
$$;

-- ---------------------------------------------------------------------------
-- 2. summary_cache matview — rebuild against a live-only CTE so the
--    experience count, time range, top entities (mention ranking), and
--    top topics all reflect the live corpus.
-- ---------------------------------------------------------------------------

drop materialized view if exists brain.summary_cache cascade;

create materialized view brain.summary_cache as
with
  singleton as (select 1::int as id),
  live as (
    select * from brain.experiences
     where superseded_by is null
       and deleted_at is null
  ),
  exp_counts as (
    select count(*)::bigint as c,
           min(captured_at) as earliest,
           max(captured_at) as latest
      from live
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
          join live           on live.id = m.experience_id
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
          from live,
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

create unique index summary_cache_singleton_idx
  on brain.summary_cache (singleton);

refresh materialized view brain.summary_cache;

insert into brain.schema_version (version, description)
  values (7, 'live-filter: hybrid search + summary_cache exclude superseded/deleted')
  on conflict (version) do nothing;
