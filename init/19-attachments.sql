-- Issue #42: attachments in the brain — a file-type-agnostic blob substrate and
-- the per-experience attachment link. Images are the first ingest path; PDFs and
-- other files slot in later with no schema change (hence the generic names).
--
-- Two tables, refcounted (one blob, N attachments), NOT a 1:1 attachments row:
--
--   brain.blobs        content-addressed object-store records. One row per stored
--                      object. Dedup + idempotent re-capture + GC refcounting all
--                      fall out of this shape.
--   brain.attachments  experience -> blob links. FK ON DELETE CASCADE from the
--                      experience; the blob outlives the experience so a shared
--                      blob survives when one referencing experience is deleted.
--
-- Content addressing (see services/blobstore.py + extraction/images.py):
--   * object_key = {account_id}/{original_sha256}.webp — the prefix is
--     ORGANIZATIONAL, never a security boundary (account_id is a stamped
--     'household' constant read by no WHERE clause; see init/18's note).
--   * Identity is the sha256 of the ORIGINAL decoded bytes (original_sha256),
--     NOT the re-encoded WebP — WebP output can drift across libwebp upgrades, so
--     hashing the original keeps dedup stable. byte_len + sha256 store the
--     DERIVATIVE (what actually landed in the bucket) for transfer integrity.
--
-- GC: a blob is reap-eligible only when zero live attachments reference its
-- blob_id AND its object is older than a grace horizon that safely exceeds the
-- longest capture (decode+vision+embed+put). The reaper is a scoped follow-up;
-- the orphan-detection reconciliation query (services/image_captures.py) + its
-- test land now. No destructive edit here — the schema is append-mostly.
--
-- Ordering: sorts after init/18-experience-geo.sql. Idempotent: CREATE TABLE /
-- INDEX IF NOT EXISTS + on-conflict schema_version insert. Safe to re-run against
-- an existing volume via `manage.py brain_ledger migrate`.

create table if not exists brain.blobs (
  id              uuid primary key default gen_random_uuid(),
  bucket          text not null,
  object_key      text not null,
  mime            text not null,
  byte_len        bigint not null,
  sha256          text not null,
  original_sha256 text,
  created_at      timestamptz not null default now(),
  unique (bucket, object_key)
);

create table if not exists brain.attachments (
  id             uuid primary key default gen_random_uuid(),
  experience_id  uuid not null references brain.experiences (id) on delete cascade,
  blob_id        uuid not null references brain.blobs (id),
  width          int,
  height         int,
  created_at     timestamptz not null default now()
);

create index if not exists attachments_experience_idx
  on brain.attachments (experience_id);
create index if not exists attachments_blob_idx
  on brain.attachments (blob_id);

insert into brain.schema_version (version, description)
  values (14, 'attachments: brain.blobs + brain.attachments (refcounted blob substrate)')
  on conflict (version) do nothing;
