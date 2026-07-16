-- Mind Grapes v2 — pg_cron consolidation jobs (issue #14, Phase 2B Stream 2B).
--
-- Wires the async half of Q1's hybrid timing: capture lands an experience
-- with consolidation_status='pending', and a periodic pg_cron tick picks
-- pending rows up, marks them in_progress, and emits a NOTIFY on the
-- 'brain_consolidate' channel so the existing MCP container can run the
-- LLM extractor (same prompt + model as migration 03) and write
-- claims + claim_sources back. We chose the NOTIFY/LISTEN bridge over
-- pgai so the OpenRouter call stays in the TS code path that already has
-- prompt versioning, schema validation, and timeouts.
--
-- Schema deltas:
--   - brain.experiences.consolidation_attempts: integer retry counter.
--     Incremented atomically by consolidate_pending_experiences when a
--     row is picked up; the worker doesn't touch it. The SP transitions
--     a row to 'failed' once attempts reaches the cap.
--   - brain.experiences.salience and brain.claims.salience: derived
--     scalars updated by recompute_salience() from brain.recall_events.
--
-- Retry policy:
--   - Cap at 3 attempts. Reaching cap with status still pending → 'failed'.
--   - Backoff between attempts: 5 min → 30 min → terminal.
--   - Stale 'in_progress' rows (worker died or container restarted before
--     ack) are reclaimed back to 'pending' after 15 minutes; the previous
--     attempt didn't actually finish so we DON'T burn a retry slot for it.
--
-- Idempotent: re-applying against an existing volume is safe via
-- "add column if not exists" + "create or replace procedure" + the
-- unschedule/schedule pattern below. Apply on existing volumes with:
--   bin/pg psql -f init/05-consolidation.sql

alter table brain.experiences
  add column if not exists consolidation_attempts int not null default 0,
  add column if not exists salience real not null default 0;

alter table brain.claims
  add column if not exists salience real not null default 0;

-- Hot-path index for the eligibility scan in consolidate_pending_experiences.
-- Partial so it stays small as the bulk of experiences move to 'complete'.
create index if not exists experiences_consolidation_pending_idx
  on brain.experiences (consolidation_attempted_at nulls first, captured_at)
  where consolidation_status in ('pending', 'in_progress');

-- ---------------------------------------------------------------------------
-- consolidate_pending_experiences(batch_size)
--
-- One cron tick:
--   1) Reclaim stale in_progress rows back to pending (no attempt charge).
--   2) Promote rows that have exhausted their retry budget to 'failed'.
--   3) Pick up to batch_size retry-eligible pending rows; mark them
--      in_progress, increment attempts, stamp attempted_at; NOTIFY each id
--      on 'brain_consolidate' for the bridge worker.
--
-- Eligibility is ANDed with attempts < cap so step 2 is a strict precondition
-- (rows promoted in step 2 cannot be picked again in step 3 in the same call).
-- ---------------------------------------------------------------------------
create or replace procedure brain.consolidate_pending_experiences(batch_size int default 50)
language plpgsql
as $$
declare
  v_id              uuid;
  c_max_attempts    constant int := 3;
  c_stale_after     constant interval := interval '15 minutes';
  c_backoff_first   constant interval := interval '5 minutes';
  c_backoff_second  constant interval := interval '30 minutes';
begin
  -- 1) Stale-recovery: a worker died or restarted mid-flight. The previous
  --    attempt didn't get to either commit or fail, so flip it back to
  --    pending without burning a retry slot.
  update brain.experiences
     set consolidation_status = 'pending'
   where consolidation_status = 'in_progress'
     and consolidation_attempted_at is not null
     and consolidation_attempted_at < now() - c_stale_after;

  -- 2) Retry budget exhaustion. Anything still pending after 3 attempts is
  --    a terminal failure; the worker reset it to pending after the third
  --    failed extraction, and we've now waited the third backoff window.
  update brain.experiences
     set consolidation_status = 'failed'
   where consolidation_status = 'pending'
     and consolidation_attempts >= c_max_attempts;

  -- 3) Pick + claim the next batch. The inner SELECT ... FOR UPDATE SKIP
  --    LOCKED is the standard way to grab a batch atomically without two
  --    concurrent ticks (or a manual CALL racing the cron job) double-
  --    notifying the same row. UPDATE...RETURNING wrapped in a CTE so
  --    plpgsql's FOR loop can iterate over the returned ids.
  for v_id in
    with updated as (
      update brain.experiences e
         set consolidation_status     = 'in_progress',
             consolidation_attempts   = e.consolidation_attempts + 1,
             consolidation_attempted_at = now()
       where e.id in (
         select id
           from brain.experiences
          where consolidation_status = 'pending'
            and consolidation_attempts < c_max_attempts
            and (
              consolidation_attempts = 0
              or (consolidation_attempts = 1
                    and (consolidation_attempted_at is null
                         or consolidation_attempted_at < now() - c_backoff_first))
              or (consolidation_attempts = 2
                    and (consolidation_attempted_at is null
                         or consolidation_attempted_at < now() - c_backoff_second))
            )
          order by consolidation_attempted_at nulls first, captured_at
          limit batch_size
          for update skip locked
       )
       returning e.id
    )
    select id from updated
  loop
    -- One NOTIFY per row keeps the payload bounded (well under the 8KB
    -- async-notify limit) and lets the worker fan out concurrently.
    perform pg_notify(
      'brain_consolidate',
      jsonb_build_object('experience_id', v_id::text)::text
    );
  end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- recompute_salience()
--
-- Salience = exponentially decayed weighted recall count over the last 180
-- days, with a 30-day half-life-ish scale. Outcomes weight as:
--   helpful = +1, stale = 0, wrong = -0.5
-- Rows with no recent recall events are reset to 0 so cold rows decay all
-- the way out instead of getting stuck at their last computed value.
-- ---------------------------------------------------------------------------
create or replace procedure brain.recompute_salience()
language plpgsql
as $$
declare
  c_window  constant interval := interval '180 days';
  c_scale   constant double precision := 30.0 * 86400.0;  -- 30 days in seconds
begin
  -- Per-claim.
  with scored as (
    select target_id::uuid as id,
           sum(
             case outcome
               when 'helpful' then 1.0
               when 'wrong'   then -0.5
               else 0.0
             end
             * exp(-extract(epoch from (now() - recalled_at)) / c_scale)
           )::real as score
      from brain.recall_events
     where target_kind = 'claim'
       and recalled_at > now() - c_window
     group by target_id
  )
  update brain.claims c
     set salience = coalesce(s.score, 0)
    from scored s
   where s.id = c.id;

  -- Decay-to-zero for claims that fell off the recall window entirely.
  update brain.claims c
     set salience = 0
   where c.salience <> 0
     and not exists (
       select 1 from brain.recall_events r
        where r.target_kind = 'claim'
          and r.target_id = c.id
          and r.recalled_at > now() - c_window
     );

  -- Per-experience: same shape, independent stream.
  with scored as (
    select target_id::uuid as id,
           sum(
             case outcome
               when 'helpful' then 1.0
               when 'wrong'   then -0.5
               else 0.0
             end
             * exp(-extract(epoch from (now() - recalled_at)) / c_scale)
           )::real as score
      from brain.recall_events
     where target_kind = 'experience'
       and recalled_at > now() - c_window
     group by target_id
  )
  update brain.experiences e
     set salience = coalesce(s.score, 0)
    from scored s
   where s.id = e.id;

  update brain.experiences e
     set salience = 0
   where e.salience <> 0
     and not exists (
       select 1 from brain.recall_events r
        where r.target_kind = 'experience'
          and r.target_id = e.id
          and r.recalled_at > now() - c_window
     );
end;
$$;

-- ---------------------------------------------------------------------------
-- Cron schedules. cron.schedule errors on duplicate jobname, so unschedule
-- first; wrap each in a sub-block so a missing previous schedule doesn't
-- stop the next from being installed.
-- ---------------------------------------------------------------------------
do $$
begin
  begin perform cron.unschedule('brain-consolidate');         exception when others then null; end;
  begin perform cron.unschedule('brain-recompute-salience');  exception when others then null; end;
  perform cron.schedule(
    'brain-consolidate',
    '*/5 * * * *',
    $cron$ call brain.consolidate_pending_experiences(50); $cron$
  );
  perform cron.schedule(
    'brain-recompute-salience',
    '0 4 * * *',
    $cron$ call brain.recompute_salience(); $cron$
  );
end $$;

insert into brain.schema_version (version, description)
  values (3, 'consolidation: salience + retry counter + pg_cron jobs')
  on conflict (version) do nothing;
