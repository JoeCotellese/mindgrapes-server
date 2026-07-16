-- Pre-cutover (Phase 1 — Phase 4) schema for the legacy `thoughts` table.
-- Post-cutover (issue #17), 08-thoughts-view.sql renames this table to
-- `thoughts_archive_2026_05_08` and replaces it with a view over
-- brain.experiences. Kept here so fresh-init containers reach the same end
-- state as a live-migrated DB: empty archive table + view.

create table thoughts (
  id          uuid primary key default gen_random_uuid(),
  content     text not null,
  metadata    jsonb not null default '{}'::jsonb,
  embedding   vector(1536),
  created_at  timestamptz not null default now()
);

-- HNSW: better recall than IVFFLAT, no train step, fine memory at personal scale.
-- pgvector >= 0.5 (pg18 image has it).
create index thoughts_embedding_hnsw
  on thoughts using hnsw (embedding vector_cosine_ops);

create index thoughts_metadata_gin
  on thoughts using gin (metadata jsonb_path_ops);

create index thoughts_created_at_idx
  on thoughts (created_at desc);

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
    t.id,
    t.content,
    t.metadata,
    1 - (t.embedding <=> query_embedding) as similarity,
    t.created_at
  from thoughts t
  where t.embedding is not null
    and (filter = '{}'::jsonb or t.metadata @> filter)
    and 1 - (t.embedding <=> query_embedding) >= match_threshold
  order by t.embedding <=> query_embedding
  limit match_count;
$$;

-- Deviates from OB1's two-step pattern: takes the embedding directly so insert
-- + embedding land atomically. One roundtrip, no race.
create or replace function upsert_thought(
  p_content    text,
  p_metadata   jsonb           default '{}'::jsonb,
  p_embedding  vector(1536)    default null
) returns thoughts
language sql
as $$
  insert into thoughts (content, metadata, embedding)
  values (p_content, p_metadata, p_embedding)
  returning *;
$$;
