-- ABOUTME: Adds indexed lat/lng columns to brain.experiences plus a bounding-box read path.
-- ABOUTME: Fixes #43 — location is an experience property, shared by map reads and hybrid search.
--
-- Issue #43: experience geolocation — indexed lat/lng columns + the shared
-- live+visible read predicate that the bounding-box helper and match_brain_hybrid
-- both consume.
--
-- Location is an experience property, not an image property: a geotagged text
-- note is as mappable as a photo. Lat/lng are first-class nullable columns
-- (null = no location, never an error). Richer, non-geometric fields
-- (place_label / accuracy_m / source) stay in experience.metadata; the `place`
-- entity carries the semantic label. lat/lng are the geometric source of truth.
--
-- Ordering: sorts after init/13-viewer-filter.sql, so match_brain_hybrid's
-- p_viewer signature already exists when we re-declare it below.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS,
-- CREATE OR REPLACE FUNCTION, and an on-conflict schema_version insert. Safe to
-- re-run against an existing volume via `manage.py brain_ledger migrate`.

alter table brain.experiences
  add column if not exists lat double precision,
  add column if not exists lng double precision;

-- Two PARTIAL btrees, not a composite (lat, lng). A composite index only prunes
-- on its leading column for a 2-D range scan and would index null-heavy rows for
-- nothing; two partial btrees let the planner BitmapAnd the lat range against the
-- lng range and skip the (majority) rows with no location at all.
--
-- No CREATE INDEX CONCURRENTLY: it cannot run inside the single transaction that
-- brain_ledger migrate() wraps each init file in (it throws). The table is small,
-- so the brief ACCESS EXCLUSIVE lock ADD-then-index takes is acceptable. On a
-- large volume, build these out of band (CONCURRENTLY, autocommit) BEFORE
-- stamping the ledger row, then run migrate to record it.
--
-- PostGIS upgrade path (deferred, #43): when radius / nearest / distance-sorted
-- queries earn it, add a geography(Point,4326) column + GiST index, backfill from
-- lat/lng, and switch the bbox helper to ST_MakeEnvelope. Plain btrees prune one
-- axis each; geography+GiST is the real 2-D index. Not a v1 gap.
create index if not exists experiences_lat_idx
  on brain.experiences (lat) where lat is not null;
create index if not exists experiences_lng_idx
  on brain.experiences (lng) where lng is not null;

-- Shared live+visible read predicate. Extracted so every read path — hybrid
-- search (below), the bbox helper (services/geo.py), and future readers — apply
-- the SAME rule and can never drift on who may see a row:
--
--   superseded_by is null and deleted_at is null
--   and (p_viewer is null or owner = p_viewer or visibility = 'shared')
--
-- A null p_viewer (legacy operator / consolidation worker) bypasses the
-- owner/visibility filter and sees every live row, mirroring init/13.
--
-- LANGUAGE SQL + STABLE + a single SELECT so Postgres inlines this set-returning
-- function into its callers; the candidate scan then plans identically to the
-- inlined predicate and keeps using the HNSW / tsv / lat-lng indexes. Validate
-- with EXPLAIN after any change to the body.
--
-- NOTE: account_id is a stamped constant ('household') that no WHERE clause here
-- reads — the key prefix and account_id give ZERO tenant isolation today.
-- Enforcement is owner/visibility only. Real account_id -> viewer tenancy (adding
-- `and account_id = <viewer_account>` to every read path) is a separate blocker
-- that must land before any multi-tenant deployment.
create or replace function brain.live_visible_experiences(p_viewer text default null)
returns setof brain.experiences
language sql stable as $$
  select e.*
    from brain.experiences e
   where e.superseded_by is null
     and e.deleted_at is null
     and (p_viewer is null or e.owner = p_viewer or e.visibility = 'shared');
$$;

-- Re-declare match_brain_hybrid (last defined in init/13) so its candidates CTE
-- reads through the shared predicate instead of re-implementing it inline. The
-- signature, RRF fusion, and returned columns are unchanged — the existing
-- hybrid-search integration suite must return identical result sets.
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
    -- Live + viewer-visible rows only, via the shared predicate (init/18). The
    -- id / filter narrowing that is specific to search stays here.
    select e.id, e.content, e.metadata, e.captured_at, e.occurred_at,
           e.embedding, e.content_tsv
      from brain.live_visible_experiences(p_viewer) e
     where (p_experience_ids is null or e.id = any(p_experience_ids))
       and (p_filter = '{}'::jsonb or e.metadata @> p_filter)
  ),
  vec as (
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
  values (13, 'experience-geo: lat/lng columns + partial btrees + live_visible_experiences predicate')
  on conflict (version) do nothing;
