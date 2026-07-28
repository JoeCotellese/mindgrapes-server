-- ABOUTME: resolve_entity returns a perfect name match first, whatever fusion scores.
-- ABOUTME: Fixes #73 — a competitor scoring on trgm AND vec displaced an exact match.
--
-- Re-declares brain.resolve_entity (last defined in init/17-phon-tiebreak.sql). Signature,
-- return shape, and all three channels are unchanged — callers must not change. Two deltas:
--
--   (a) The result ordering puts trgm_score = 1.0 ahead of everything else. #17 stopped a
--       phon-ONLY competitor from outranking a perfect trgm match, but said nothing about a
--       competitor scoring on two channels at once: trgm rank 2 + vec rank 1 sums two full
--       RRF terms (1/62 + 1/61 ≈ 0.0325) and beats an exact match holding trgm rank 1 alone
--       (1/61 ≈ 0.0164). The displaced exact match is still IN the result, just not first —
--       which is invisible to the top_k=1 callers (entity_resolver, claim_writer) and is how
--       a trgm 1.0 candidate came back recommending 'create' in production.
--
--       Only a perfect 1.0 is promoted, not a general trgm preference: 1.0 means the name or
--       one alias matched exactly, where fusion has nothing left to arbitrate. Everything
--       below that still ranks by fused_score, so the semantic channel keeps its say on the
--       genuinely ambiguous cases fusion exists for.
--
--   (b) The vec CTE gains an ORDER BY before its limit. It selected `limit 50` with no
--       ordering, so which 50 rows reached the fusion was whatever the plan happened to
--       emit — the rnk window ordered only what survived. Same ordering the rnk uses, so
--       the 50 kept are now the 50 nearest.
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
       and (dmetaphone(e.canonical_name) = dmetaphone(p_name)
            -- alias-aware like the trgm channel (#171): an alias may phon-match even when
            -- the canonical name does not — e.g. 'Ada Lovelace' via its alias 'Ada'.
            -- dmetaphone is unindexed, so this filters the kind-scoped set row by row
            -- before the limit — same seq-scan class as the trgm unnest; revisit ~100k.
            or exists (select 1 from unnest(e.aliases) a
                        where dmetaphone(a) = dmetaphone(p_name)))
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
     -- Nearest 50, not an arbitrary 50: without this the limit cut the set before the
     -- rnk window ever saw it (#73).
     order by e.embedding <=> p_context_embedding
     limit 50
  ),
  fused as (
    select coalesce(t.id, p.id, v.id) as id,
           coalesce(t.score, 0)        as trgm_score,
           coalesce(p.is_match, false) as phon_match,
           coalesce(v.score, 0)        as vec_score,
           (
             coalesce(1.0 / (60 + t.rnk), 0)
             -- Tiebreak only: the top-rank RRF step at k=60 is 1/61 - 1/62 ≈ 0.000264,
             -- so this bonus must stay below 0.000264 or a phon-only match could
             -- displace a top-ranked trgm match (#17). Steps shrink with rank and dip
             -- under 0.0001 past rank ~40, so deep ranks can still reorder — harmless
             -- while callers take top_k <= 5; shrink the bonus if that changes.
             + case when p.is_match then 0.0001 else 0 end
             + coalesce(1.0 / (60 + v.rnk), 0)
           )::real as fused_score
      from trgm t
      full outer join phon p on p.id = t.id
      full outer join vec  v on v.id = coalesce(t.id, p.id)
  )
  select id, trgm_score, phon_match, vec_score, fused_score
    from fused
   where id is not null
   -- An exact name/alias match outranks fusion (#73). Ties among exact matches, and every
   -- inexact candidate, fall through to the fused ordering unchanged.
   order by (trgm_score >= 1.0) desc, fused_score desc, trgm_score desc
   limit p_top_k;
$$;

insert into brain.schema_version (version, description)
  values (16, 'resolve-entity-exact-first: exact trgm match outranks fused competitors (#73)')
  on conflict (version) do nothing;
