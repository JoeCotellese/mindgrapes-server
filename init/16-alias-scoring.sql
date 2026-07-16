-- ABOUTME: resolve_entity scores each alias individually, not only as one concatenated haystack.
-- ABOUTME: Fixes #171 — exact alias hits scored ~0.26 and spawned duplicate entities.
--
-- Re-declares brain.resolve_entity (last defined in init/03-brain.sql) to fix the trgm
-- channel. Signature, return shape, phon/vec channels and the RRF fusion are unchanged.
--
-- The bug: the trgm channel scored p_name against brain.aliases_haystack(e.aliases) —
-- array_to_string(aliases, ' '). For entity 'Ada Lott' with aliases {Ada,"Ms. Lott"},
--
--   similarity('Ada Ms. Lott', 'Ada') ~= 0.26
--
-- so an *exact* alias hit landed below both pg_trgm.similarity_threshold (0.3, applied by
-- the `%` prefilter, which dropped the row before it could even be scored) and the caller's
-- MATCH_THRESHOLD (0.85, brain/services/claim_writer.py). The entity never bound and a
-- duplicate was minted instead. The property was perverse: every alias added lengthened the
-- haystack and lowered similarity for all the *other* aliases, so the best-annotated
-- entities were the hardest to find. Live damage as of 2026-07-15: 'Ada' (288 claims),
-- 'Bea' (68), 'Ms. Lott' (43) each accumulated their own claims while already listed
-- in the winner's aliases array.
--
-- The fix: add max(similarity(alias, p_name)) over unnest(aliases) as a scoring term, so
-- similarity('Ada','Ada') = 1.0 and the bind happens. The prefilter gets the same treatment
-- (exists over unnest) or it would keep filtering out the rows we now score correctly.
--
-- The haystack STAYS as a third greatest() term rather than being replaced, because it was
-- never purely harmful — concatenating aliases reassembles a full name out of single-token
-- aliases, which per-alias scoring cannot see. Entity 'ZQ' with aliases {Zephyrine,Quux},
-- looked up as 'Zephyrine Quux':
--
--   per-alias  max(similarity(a, 'Zephyrine Quux'))          = 0.667   -- misses at 0.85
--   haystack   similarity('Zephyrine Quux', 'Zephyrine Quux') = 1.000
--
-- and canonical_name ('ZQ') can't rescue it. Scoring is therefore the max of all three
-- signals: canonical, best single alias, and the concatenation. Each covers what the others
-- miss; the original bug was using the haystack as the *only* alias signal, not using it.
--
-- The `order by score desc` before `limit 50` is new and load-bearing: the per-alias
-- prefilter is less restrictive than the diluted one (it no longer dilutes true matches
-- away), so more rows qualify and an unordered cut could discard the right entity.
--
-- brain.experience_ids_mentioning_name (init/04) was audited for the same bug and is clean:
-- it already matches per-alias via unnest.
--
-- Idempotent: CREATE OR REPLACE; re-applying on an existing volume is safe.

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
    select id, score, row_number() over (order by score desc) as rnk
      from (
        select e.id,
               greatest(
                 similarity(e.canonical_name, p_name),
                 coalesce((select max(similarity(a, p_name)) from unnest(e.aliases) a), 0),
                 similarity(brain.aliases_haystack(e.aliases), p_name)
               )::real as score
          from brain.entities e
         where e.kind = p_kind
           and e.merged_into is null
           and (e.canonical_name % p_name
                or brain.aliases_haystack(e.aliases) % p_name
                or exists (select 1 from unnest(e.aliases) a where a % p_name))
         order by score desc
         -- ponytail: the unnest branch can't use an index, so this seq-scans whatever the
         -- other two branches could have used their GIN indexes for. 4,208 entities as of
         -- 2026-07-15; revisit if that passes ~100k.
         limit 50
      ) s
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

insert into brain.schema_version (version, description)
  values (11, 'alias-scoring: resolve_entity scores aliases per-alias, not per-haystack (#171)')
  on conflict (version) do nothing;
