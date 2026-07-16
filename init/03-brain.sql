-- Mind Grapes v2 — episodic / semantic / provenance schema.
-- See docs/spec.md §1 and docs/implementation-plan.md Phase 1.
--
-- This file lives alongside (and never modifies) public.thoughts. Init scripts
-- only run on a fresh data dir; for existing volumes apply with:
--   bin/pg psql -f init/03-brain.sql

create schema if not exists brain;

-- Enums. Wrapped in do-blocks so re-applying against an existing volume
-- doesn't error on duplicate type. (create type has no `if not exists`.)

-- source_kind is the *processing role* (how the experience was created),
-- NOT an origin tag (where it came from). Origin lives in source_ref via
-- URI-style schemes ('vault:...', 'drafts:...', 'meet:...', 'https:...').
-- Adding a new origin is a convention change, not a migration. Do not
-- extend this enum to add new sources.
do $$ begin
  create type brain.source_kind as enum ('transcript', 'manual', 'derived', 'imported');
exception when duplicate_object then null; end $$;

do $$ begin
  create type brain.entity_kind as enum ('person', 'org', 'event', 'place', 'concept');
exception when duplicate_object then null; end $$;

do $$ begin
  create type brain.support_kind as enum ('verbatim', 'paraphrased', 'inferred', 'imported');
exception when duplicate_object then null; end $$;

do $$ begin
  create type brain.polarity as enum ('asserted', 'suspected', 'denied', 'retracted');
exception when duplicate_object then null; end $$;

do $$ begin
  create type brain.target_kind as enum ('experience', 'claim', 'entity');
exception when duplicate_object then null; end $$;

do $$ begin
  create type brain.recall_outcome as enum ('helpful', 'stale', 'wrong');
exception when duplicate_object then null; end $$;

do $$ begin
  create type brain.consolidation_status as enum ('pending', 'in_progress', 'complete', 'failed');
exception when duplicate_object then null; end $$;

-- EPISODIC: bounded events anchored in time, immutable.
create table if not exists brain.experiences (
  id                          uuid primary key default gen_random_uuid(),
  captured_at                 timestamptz not null default now(),
  occurred_at                 timestamptz,
  occurred_window             tstzrange,
  source_kind                 brain.source_kind,
  source_ref                  text,
  content                     text not null,
  embedding                   vector(1536),
  metadata                    jsonb not null default '{}'::jsonb,
  consolidation_status        brain.consolidation_status not null default 'pending',
  consolidation_attempted_at  timestamptz
);

-- SEMANTIC ENTITIES: people, orgs, events, concepts as first-class nodes.
-- This is the canonical identity layer for *any* domain — a "contact",
-- a "book", a "vendor", a "customer" is just an entity with the right
-- kind. Do not introduce per-domain identity tables (no
-- professional_contacts, no books, etc.). Domain-specific *operational*
-- state (CRM pipeline stage, reading-log status, follow-up date) belongs
-- in small side tables keyed by entity_id — never duplicate identity.
-- merged_into is a self-fk for soft-merging duplicates without rewriting claims.
-- No updated_at: mutations flow through brain.correction_events, which is the
-- audit trail. A bare timestamp column without a trigger would silently lag
-- on UPDATE and lie about freshness.
create table if not exists brain.entities (
  id              uuid primary key default gen_random_uuid(),
  kind            brain.entity_kind not null,
  canonical_name  text not null,
  aliases         text[] not null default '{}',
  embedding       vector(1536),
  merged_into     uuid references brain.entities (id),
  confidence      real not null default 0.5,
  metadata        jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now()
);

-- Drop legacy updated_at on already-applied volumes (idempotent: no-op on fresh).
alter table if exists brain.entities drop column if exists updated_at;

-- SEMANTIC CLAIMS: atomic facts about entities (graph edges in relational form).
-- predicate_detail is the Q2 escape hatch for novel relations (predicate='other').
-- superseded_by is a self-fk for explicit retractions.
create table if not exists brain.claims (
  id                uuid primary key default gen_random_uuid(),
  subject_id        uuid not null references brain.entities (id),
  predicate         text not null,
  predicate_detail  text,
  object_entity_id  uuid references brain.entities (id),
  object_literal    text,
  polarity          brain.polarity not null default 'asserted',
  confidence        real not null default 0.5,
  valid_during      tstzrange,
  superseded_by     uuid references brain.claims (id),
  created_at        timestamptz not null default now()
);

-- PROVENANCE: every claim points back to its source experiences.
create table if not exists brain.claim_sources (
  claim_id           uuid not null references brain.claims (id) on delete cascade,
  experience_id      uuid not null references brain.experiences (id) on delete cascade,
  support_kind       brain.support_kind not null,
  source_confidence  real,
  extracted_by       text,
  primary key (claim_id, experience_id)
);

-- REINFORCEMENT: recall feeds back into salience.
create table if not exists brain.recall_events (
  id             uuid primary key default gen_random_uuid(),
  target_kind    brain.target_kind not null,
  target_id      uuid not null,
  recalled_at    timestamptz not null default now(),
  query_context  text,
  outcome        brain.recall_outcome
);

-- RECONSOLIDATION AUDIT: every correction is a typed event, not a mutation.
create table if not exists brain.correction_events (
  id           uuid primary key default gen_random_uuid(),
  target_kind  brain.target_kind not null,
  target_id    uuid not null,
  before       jsonb not null,
  after        jsonb not null,
  reason       text,
  created_at   timestamptz not null default now(),
  created_by   text
);

-- Indexes per spec §1. HNSW for vector recall, GIN trgm for fuzzy name match,
-- GIN jsonb_path_ops for metadata filters, GiST for tstzrange overlap, partial
-- BTrees for the hot read paths (non-retracted, non-merged).
create index if not exists experiences_embedding_hnsw
  on brain.experiences using hnsw (embedding vector_cosine_ops);

create index if not exists experiences_occurred_at_idx
  on brain.experiences (occurred_at desc nulls last);

create index if not exists experiences_metadata_gin
  on brain.experiences using gin (metadata jsonb_path_ops);

create index if not exists entities_canonical_trgm
  on brain.entities using gin (canonical_name gin_trgm_ops);

-- gin_trgm_ops only accepts text, not text[]. Spec §1.128 wrote the column form
-- as `gin (aliases gin_trgm_ops)` but that doesn't compile, and a naked
-- array_to_string expression is rejected because the function isn't marked
-- IMMUTABLE. Wrap it in our own immutable SQL function so resolve_entity()
-- (issue #9) can do trigram fuzzy match across every alias via
-- `brain.aliases_haystack(aliases) % 'X'`.
create or replace function brain.aliases_haystack(a text[]) returns text
language sql immutable parallel safe
as $$ select coalesce(array_to_string(a, ' '), '') $$;

create index if not exists entities_aliases_trgm
  on brain.entities using gin (brain.aliases_haystack(aliases) gin_trgm_ops);

create index if not exists entities_embedding_hnsw
  on brain.entities using hnsw (embedding vector_cosine_ops);

create index if not exists entities_kind_idx
  on brain.entities (kind) where merged_into is null;

create index if not exists claims_subject_idx
  on brain.claims (subject_id) where polarity <> 'retracted';

create index if not exists claims_object_entity_idx
  on brain.claims (object_entity_id) where polarity <> 'retracted';

create index if not exists claims_predicate_idx
  on brain.claims (predicate);

create index if not exists claims_valid_during_gist
  on brain.claims using gist (valid_during);

create index if not exists claim_sources_experience_idx
  on brain.claim_sources (experience_id);

-- Mention links: experience -> entity (raw, pre-claim). Issue #9 populates this
-- during the entity extraction migration. Full graph edges (claims) come later.
create table if not exists brain.mentions (
  experience_id  uuid not null references brain.experiences(id) on delete cascade,
  entity_id      uuid not null references brain.entities(id)    on delete cascade,
  surface_form   text not null,
  field          text not null check (field in ('people','topics')),
  created_at     timestamptz not null default now(),
  primary key (experience_id, entity_id, surface_form, field)
);
create index if not exists mentions_entity_idx on brain.mentions(entity_id);

-- Borderline entity-pair queue for Phase 3 review. Populated by issue #9
-- (entity extraction migration) when name resolution lands in the 0.55-0.85
-- similarity band. Drained via the request_disambiguation MCP tool (issue #13).
-- entity_a < entity_b is the canonical ordering so the unique pair index
-- dedupes regardless of which side surfaces first.
create table if not exists brain.merge_candidates (
  id           uuid primary key default gen_random_uuid(),
  entity_a     uuid not null references brain.entities(id) on delete cascade,
  entity_b     uuid not null references brain.entities(id) on delete cascade,
  similarity   real not null,
  evidence     jsonb not null default '{}'::jsonb,
  status       text not null default 'pending'
    check (status in ('pending','merged','kept_separate','skipped')),
  created_at   timestamptz not null default now(),
  resolved_at  timestamptz,
  check (entity_a < entity_b)
);
create unique index if not exists merge_candidates_pair
  on brain.merge_candidates(entity_a, entity_b);
create index if not exists merge_candidates_pending
  on brain.merge_candidates(created_at) where status = 'pending';

-- Fused entity resolver: trigram similarity + dmetaphone phonetic equality +
-- pgvector cosine similarity, combined via reciprocal-rank fusion (k=60).
-- Returns the top-K candidate entities of the given kind with each component
-- score so the caller can apply its own match-vs-borderline-vs-new policy.
-- Only fused_score is used for ranking; trgm_score and vec_score are 0-1
-- comparable and are what the migration thresholds (0.85, 0.55) test against.
create or replace function brain.resolve_entity(
  p_name              text,
  p_context_embedding vector(1536),
  p_kind              brain.entity_kind,
  p_top_k             int default 5
) returns table(
  entity_id   uuid,
  trgm_score  real,
  phon_match  boolean,
  vec_score   real,
  fused_score real
)
language sql stable as $$
  with
  trgm as (
    select e.id,
           greatest(
             similarity(e.canonical_name, p_name),
             coalesce(similarity(brain.aliases_haystack(e.aliases), p_name), 0)
           )::real as score,
           row_number() over (order by greatest(
             similarity(e.canonical_name, p_name),
             coalesce(similarity(brain.aliases_haystack(e.aliases), p_name), 0)
           ) desc) as rnk
      from brain.entities e
     where e.kind = p_kind
       and e.merged_into is null
       and (e.canonical_name % p_name
            or brain.aliases_haystack(e.aliases) % p_name)
     limit 50
  ),
  phon as (
    select e.id,
           true as is_match
      from brain.entities e
     where e.kind = p_kind
       and e.merged_into is null
       and dmetaphone(e.canonical_name) = dmetaphone(p_name)
     limit 50
  ),
  vec as (
    select e.id,
           (1 - (e.embedding <=> p_context_embedding))::real as score,
           row_number() over (order by e.embedding <=> p_context_embedding) as rnk
      from brain.entities e
     where e.kind = p_kind
       and e.merged_into is null
       and e.embedding is not null
       and p_context_embedding is not null
     limit 50
  ),
  fused as (
    select coalesce(t.id, p.id, v.id) as id,
           coalesce(t.score, 0)        as trgm_score,
           coalesce(p.is_match, false) as phon_match,
           coalesce(v.score, 0)        as vec_score,
           (
             coalesce(1.0 / (60 + t.rnk), 0)
             + case when p.is_match then 0.05 else 0 end
             + coalesce(1.0 / (60 + v.rnk), 0)
           )::real as fused_score
      from trgm t
      full outer join phon p on p.id = t.id
      full outer join vec  v on v.id = coalesce(t.id, p.id)
  )
  select id, trgm_score, phon_match, vec_score, fused_score
    from fused
   where id is not null
   order by fused_score desc, trgm_score desc
   limit p_top_k;
$$;

-- Stored procedure stubs. Bodies are filled by issue #14 (pg_cron consolidation
-- jobs). Declared here so the cron schedules in #14 can reference them, and so
-- the CALL signatures are part of the schema baseline tested by Phase 5 cutover.
create or replace procedure brain.consolidate_pending_experiences(batch_size int default 50)
language plpgsql
as $$
begin
  -- body in issue #14
  null;
end;
$$;

create or replace procedure brain.recompute_salience()
language plpgsql
as $$
begin
  -- body in issue #14
  null;
end;
$$;

-- Schema version. Phase 5 cutover (issue #17) reads this to confirm the brain
-- schema is at the expected baseline before flipping read paths.
create table if not exists brain.schema_version (
  version      int primary key,
  applied_at   timestamptz not null default now(),
  description  text not null
);

insert into brain.schema_version (version, description)
  values (1, 'initial brain schema')
  on conflict (version) do nothing;
