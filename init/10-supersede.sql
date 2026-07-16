-- Issue #48 / Iteration 3: superseded_by + deleted_at on brain.experiences.
--
-- Iteration 3 lets the UI edit and delete experiences. To preserve historical
-- truth we never overwrite a substantively-changed experience: instead we
-- insert a new row and set the original's `superseded_by` self-fk so the old
-- content remains queryable for audit while listings and search filter it
-- out. Deletes are soft via `deleted_at` so the audit trail in
-- brain.correction_events stays intact and a misclick is recoverable.
--
-- `experiences_live_idx` is a partial index over the predicate every read
-- path uses (`superseded_by is null and deleted_at is null`) so listings,
-- entity timelines, and the hybrid-search candidates CTE all share the same
-- selectivity boost without scanning dead rows.

alter table brain.experiences
  add column if not exists superseded_by uuid references brain.experiences (id),
  add column if not exists deleted_at    timestamptz;

create index if not exists experiences_live_idx
  on brain.experiences (id)
  where superseded_by is null and deleted_at is null;

insert into brain.schema_version (version, description)
  values (6, 'supersede: experiences.superseded_by + deleted_at + live partial index')
  on conflict (version) do nothing;
