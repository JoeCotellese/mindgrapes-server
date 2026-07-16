-- Issue #66 / Slice 4.1 (#82): ownership spine on brain.experiences.
--
-- Soft privacy lets a household of members each keep private captures while
-- sharing some. This slice lays the *foundation* — owner, account_id and a
-- visibility column — without changing what anyone can read (read filtering
-- lands in Slice 4.2). New captures stamp these columns at write time;
-- existing rows are backfilled to the primary owner's defaults below.
--
-- visibility is an ENUM, never a bool, so a future hard-private tier
-- (`private_hard`) is a non-breaking ALTER TYPE ... ADD VALUE rather than a
-- schema migration. Slice 4.1 ships only the two values reads will branch on.
--
-- Idempotent: the enum create is wrapped in the duplicate_object guard used
-- throughout init/03-brain.sql; columns use `add column if not exists`; the
-- owner backfill only touches null owners; the schema_version insert is
-- `on conflict do nothing`. Safe to re-run against an existing volume.

do $$ begin
  create type brain.visibility as enum ('private', 'shared');
exception when duplicate_object then null; end $$;

-- account_id / visibility carry NOT NULL defaults, so ADD COLUMN backfills
-- every existing row in place. owner is intentionally nullable (no default):
-- new captures set it from the authenticated viewer, and the UPDATE below
-- backfills the legacy rows that predate ownership.
alter table brain.experiences
  add column if not exists account_id text not null default 'household',
  add column if not exists owner      text,
  add column if not exists visibility brain.visibility not null default 'private';

-- Backfill legacy rows to the household owner. 'owner' matches the documented
-- DEFAULT_OWNER env default. Only null owners are touched, so re-runs never
-- clobber a member's existing owner. (No-op on fresh volumes — the table is
-- empty when init runs.)
update brain.experiences set owner = 'owner' where owner is null;

insert into brain.schema_version (version, description)
  values (8, 'soft-privacy: experiences.owner + account_id + visibility enum + backfill')
  on conflict (version) do nothing;
