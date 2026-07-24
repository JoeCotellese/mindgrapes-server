-- ABOUTME: Adds 'animal' to brain.entity_kind so pets/animals are a first-class kind.
-- ABOUTME: Fixes #57 — animals no longer misclassify as 'person' or force into 'concept'.
--
-- Issue #57: named animals (a pet, "Roger the dog") had no home in the entity_kind
-- enum, so extraction forced them into 'person' (making a dog a merge-candidate
-- against a person of the same name) or 'concept' (where the claim writer often
-- demotes the object to a free-form literal instead of an entity). 'animal' gives
-- kind-scoped resolution a distinct bucket, keeping animals out of the person graph.
--
-- Enum-only delta. ADD VALUE IF NOT EXISTS is idempotent and, on PG 12+, is
-- allowed inside the transaction that `brain_ledger migrate` wraps each file in
-- BECAUSE this migration only ADDS the label and never USES it in the same
-- transaction (the only other statement is the schema_version insert). Safe to
-- re-run against an existing volume.

alter type brain.entity_kind add value if not exists 'animal';

insert into brain.schema_version (version, description)
  values (15, 'animal-entity-kind: add ''animal'' to brain.entity_kind (#57)')
  on conflict (version) do nothing;
