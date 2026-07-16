-- Mind Grapes v2 — hybrid search (issue #12, Phase 2B Stream 2B).
--
-- Adds a tsvector column on brain.experiences (generated, indexed via GIN) and
-- two SQL functions:
--   - brain.match_brain_hybrid(...) — vector + lexical fused via reciprocal
--     rank fusion (k=60), gated by an optional jsonb filter and an optional
--     pre-computed experience_id allowlist. Mirrors the RRF pattern in
--     brain.resolve_entity for consistency.
--   - brain.experience_ids_mentioning_name(name, kind) — looks up entities by
--     canonical_name or alias (case-insensitive), walks merged_into so
--     mentions pointing to the loser of a soft-merge still surface, and
--     returns the union of experience ids from brain.mentions.
--
-- Idempotent: re-applying against an existing volume is a no-op via
-- "add column if not exists" + "create index if not exists" + "create or
-- replace function". Apply on existing volumes with:
--   bin/pg psql -f init/04-hybrid-search.sql

-- Stored tsvector so the lexical channel uses a GIN index instead of a per-row
-- to_tsvector() recompute.
alter table brain.experiences
  add column if not exists content_tsv tsvector
    generated always as (to_tsvector('english', content)) stored;

create index if not exists experiences_content_tsv_gin
  on brain.experiences using gin (content_tsv);

-- Hybrid retrieval over brain.experiences. Returns an unbounded ranking
-- column (fused_score) plus the per-channel scores so callers can decide
-- whether to surface "vector match" vs "lexical match" affordances.
--
-- Channels:
--   vec  — 1 - cosine_distance(embedding, query_embedding), top 50
--   lex  — ts_rank_cd(content_tsv, websearch_to_tsquery(query)), top 50
-- Fused: sum of 1/(60+rank) per channel — matches the RRF coefficient used
-- elsewhere in the brain so the two retrievers stay comparable.
--
-- p_filter         — metadata @> filter (jsonb containment); '{}'::jsonb means
--                    no filter
-- p_experience_ids — optional id allowlist; the entity-aware filter path
--                    pre-resolves names to ids and passes them here. NULL
--                    means no allowlist.
-- p_threshold      — minimum fused_score to return (default 0.0 = unfiltered)
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
    select e.id, e.content, e.metadata, e.captured_at, e.occurred_at,
           e.embedding, e.content_tsv
      from brain.experiences e
     where (p_experience_ids is null or e.id = any(p_experience_ids))
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

-- Resolve a person/topic name to the set of experience ids that mention it,
-- via brain.mentions, with two layers of dereference:
--   (1) canonical_name OR alias match (case-insensitive)
--   (2) walk merged_into so mentions still pointing to a soft-merged loser
--       surface when searching by the survivor's canonical name. Recursive
--       in case of multi-hop merges.
-- Returns an empty array when no match — callers can pass the result straight
-- into match_brain_hybrid.p_experience_ids for the entity-aware filter.
create or replace function brain.experience_ids_mentioning_name(
  p_name text,
  p_kind brain.entity_kind
) returns uuid[]
language sql stable as $$
  with recursive
  seed as (
    select id
      from brain.entities
     where kind = p_kind
       and (
         lower(canonical_name) = lower(p_name)
         or exists (
           select 1 from unnest(aliases) a where lower(a) = lower(p_name)
         )
       )
  ),
  walk(id) as (
    select id from seed
    union
    select e.id from brain.entities e
      join walk w on e.merged_into = w.id
  )
  select coalesce(array_agg(distinct m.experience_id), array[]::uuid[])
    from brain.mentions m
   where m.entity_id in (select id from walk);
$$;

insert into brain.schema_version (version, description)
  values (2, 'hybrid search: content_tsv + match_brain_hybrid + entity dereference')
  on conflict (version) do nothing;
