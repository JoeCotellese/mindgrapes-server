-- Phase 5 / Issue #17: read cutover from public.thoughts to brain.experiences.
--
-- Renames the legacy table to thoughts_archive_2026_05_08 (one-time snapshot;
-- safety net for rollback) and replaces it with a view over
-- brain.experiences. Rewrites match_thoughts() and upsert_thought() to
-- operate on brain.experiences so legacy single-arg clients (which call the
-- function or SELECT FROM thoughts) keep working transparently.
--
-- This file runs both as the final init script (fresh container) and as a
-- live migration via `pg psql -f`. Both paths converge to the same end state:
-- public.thoughts is a view, the archive table holds the pre-cutover snapshot
-- (empty on fresh init), and the legacy SQL functions write/read brain.

-- Drop functions first — they reference the legacy `thoughts` row type and
-- block the table rename.
drop function if exists match_thoughts(vector, float, int, jsonb);
drop function if exists upsert_thought(text, jsonb, vector);

-- Rename the legacy table out of the way (fresh-init parity: 02-thoughts.sql
-- created it as an empty table, so the rename is harmless there).
do $$
begin
  if exists (
    select 1
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'public'
       and c.relname = 'thoughts'
       and c.relkind = 'r'  -- ordinary table only; skip if already a view
  ) then
    execute 'alter table public.thoughts rename to thoughts_archive_2026_05_08';
  end if;
end $$;

-- Compat view. CREATE OR REPLACE so re-running this file is a no-op.
create or replace view public.thoughts as
  select
    e.id,
    e.content,
    e.metadata,
    e.embedding,
    e.captured_at as created_at
  from brain.experiences e;

-- match_thoughts now reads from brain.experiences directly.
create or replace function match_thoughts(
  query_embedding  vector(1536),
  match_threshold  float,
  match_count      int,
  filter           jsonb default '{}'::jsonb
) returns table (
  id          uuid,
  content     text,
  metadata    jsonb,
  similarity  float,
  created_at  timestamptz
)
language sql stable
as $$
  select
    e.id,
    e.content,
    e.metadata,
    1 - (e.embedding <=> query_embedding) as similarity,
    e.captured_at as created_at
  from brain.experiences e
  where e.embedding is not null
    and (filter = '{}'::jsonb or e.metadata @> filter)
    and 1 - (e.embedding <=> query_embedding) >= match_threshold
  order by e.embedding <=> query_embedding
  limit match_count;
$$;

-- upsert_thought writes through to brain.experiences with manual defaults so
-- legacy single-arg clients (still calling the 3-arg signature) keep working
-- unchanged. Returns view-shaped columns explicitly because the named
-- composite type `thoughts` no longer exists (it's a view now).
create or replace function upsert_thought(
  p_content    text,
  p_metadata   jsonb           default '{}'::jsonb,
  p_embedding  vector(1536)    default null
) returns table (
  id          uuid,
  content     text,
  metadata    jsonb,
  embedding   vector(1536),
  created_at  timestamptz
)
language sql
as $$
  insert into brain.experiences (
    content, metadata, embedding,
    source_kind, consolidation_status
  ) values (
    p_content, p_metadata, p_embedding,
    'manual'::brain.source_kind, 'pending'::brain.consolidation_status
  )
  returning id, content, metadata, embedding, captured_at as created_at;
$$;
