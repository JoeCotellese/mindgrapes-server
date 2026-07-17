-- ABOUTME: resolve_entity's phon channel becomes a true tiebreak and gains alias-awareness.
-- ABOUTME: Fixes #17 — a flat 0.05 phon bonus outranked a perfect trgm match; phon ignored aliases.
--
-- Re-declares brain.resolve_entity (last defined in init/16-alias-scoring.sql). Signature,
-- return shape, and the trgm/vec channels are unchanged — callers must not change. Two
-- deltas, both in the RRF fusion of the phon channel:
--
--   (a) The additive fused-score bonus for a phon match drops from 0.05 to 0.0001. The
--       stated design (entity_resolver.py) is that phon only breaks ties in fused_score,
--       but 0.05 dominated the channel it was meant to tiebreak: a trgm rank-1 hit is worth
--       only 1/(60+1) ≈ 0.0164, so any phon-only competitor outranked a *perfect* name/alias
--       match, and both top_k=1 callers bound to the wrong entity. The bonus must stay below
--       one RRF step so it can never move a trgm match off its rank — see the comment at the
--       `case when p.is_match` line for the exact bound.
--
--   (b) The phon CTE is now alias-aware: it matches when dmetaphone(canonical_name) OR any
--       dmetaphone(alias) equals dmetaphone(p_name). #171 gave the trgm channel per-alias
--       scoring; the phon channel kept matching canonical_name only, so 'Ada Lovelace' could
--       never phon-match 'Ada' no matter how many times 'Ada' was listed as an alias. Same
--       alias-blindness #171 fixed, still live in the phon channel — this is its other half.
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
   order by fused_score desc, trgm_score desc
   limit p_top_k;
$$;

insert into brain.schema_version (version, description)
  values (12, 'phon-tiebreak: phon bonus below one RRF step + alias-aware phon channel (#17)')
  on conflict (version) do nothing;
