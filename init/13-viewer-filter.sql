-- Issue #66 / Slice 4.2 (#83): read enforcement — thread a viewer filter
-- through hybrid search so member-facing reads return own + shared only.
--
-- init/12-soft-privacy.sql added brain.experiences.owner + visibility. This
-- file re-declares brain.match_brain_hybrid (last defined in
-- init/11-live-filter.sql) with an extra p_viewer parameter and a predicate in
-- the candidates CTE:
--
--   (p_viewer is null or e.owner = p_viewer or e.visibility = 'shared')
--
-- A null p_viewer (legacy x-brain-key operator, or any out-of-request caller
-- like the consolidation worker) bypasses the filter and sees everything —
-- Option A, removed end-of-v1 with MCP_ACCESS_KEY. An authenticated member sees
-- their own rows plus anything marked shared.
--
-- Only match_brain_hybrid changes. summary_cache / thought_stats stay
-- unfiltered by design (aggregate surfaces are an accepted soft-privacy leak
-- for the married-couple threat model; private_hard is future).
--
-- Order matters: this file must sort after init/12-soft-privacy.sql so the
-- owner / visibility columns exist at apply time.
--
-- Idempotent: CREATE OR REPLACE works even when the body changes, and adding a
-- parameter with a default keeps existing 6-arg callers working unchanged.

create or replace function brain.match_brain_hybrid(
  p_query             text,
  p_query_embedding   vector(1536),
  p_limit             int default 10,
  p_filter            jsonb default '{}'::jsonb,
  p_threshold         real default 0.0,
  p_experience_ids    uuid[] default null,
  p_viewer            text default null
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
    -- Viewer filter (#83): a member sees their own rows plus anything shared;
    -- a null viewer (legacy / system) bypasses the filter.
    select e.id, e.content, e.metadata, e.captured_at, e.occurred_at,
           e.embedding, e.content_tsv
      from brain.experiences e
     where e.superseded_by is null
       and e.deleted_at is null
       and (p_experience_ids is null or e.id = any(p_experience_ids))
       and (p_filter = '{}'::jsonb or e.metadata @> p_filter)
       and (p_viewer is null or e.owner = p_viewer or e.visibility = 'shared')
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

insert into brain.schema_version (version, description)
  values (9, 'viewer-filter: match_brain_hybrid owner/visibility read enforcement')
  on conflict (version) do nothing;
