-- Mind Grapes v2 — OAuth 2.1 authorization-server + resource-server state.
-- See issue #41. Replaces the URL-key auth on /mcp.
--
-- This file lives alongside (and never modifies) brain.* and public.thoughts.
-- Init scripts only run on a fresh data dir; for existing volumes apply with:
--   bin/pg psql -f init/09-auth-schema.sql

create schema if not exists auth;

-- DCR-registered clients. is_seeded distinguishes well-known UAs (claude.ai
-- connector, Claude Desktop, Claude Code) added via `openbrain auth init`
-- from clients self-registered through /oauth/register; the consent screen
-- shows an "unverified client" banner for is_seeded=false rows.
create table if not exists auth.clients (
  client_id                     text primary key,
  client_name                   text,
  redirect_uris                 text[] not null,
  grant_types                   text[] not null,
  token_endpoint_auth_method    text not null,
  registered_at                 timestamptz not null default now(),
  is_seeded                     boolean not null default false,
  metadata                      jsonb
);

-- Authorization codes. Stored as SHA-256(raw); the raw code is never written
-- to disk so a Postgres backup leak doesn't expose live codes inside their
-- 5-minute TTL window. code_challenge_method is constrained to S256 — `plain`
-- is rejected at the DB layer as defense-in-depth.
create table if not exists auth.codes (
  code_hash                  text primary key,
  client_id                  text not null references auth.clients(client_id) on delete cascade,
  redirect_uri               text not null,
  code_challenge             text not null,
  code_challenge_method      text not null default 'S256'
                               check (code_challenge_method = 'S256'),
  scopes                     text[],
  resource                   text,
  user_id                    text not null,
  expires_at                 timestamptz not null,
  used_at                    timestamptz
);

-- Refresh tokens. Stored hashed (SHA-256) — raw refresh tokens are 256-bit
-- random, so argon2 buys nothing here and slows the hot path. Rotation is
-- enforced via an atomic UPDATE conditioned on `rotated_to is null and
-- revoked_at is null`; reuse of a rotated token revokes the entire family
-- (transitive closure of rotated_to).
create table if not exists auth.refresh_tokens (
  token_hash    text primary key,
  client_id     text not null references auth.clients(client_id) on delete cascade,
  user_id       text not null,
  scopes        text[],
  resource      text,
  issued_at     timestamptz not null default now(),
  expires_at    timestamptz not null,
  revoked_at    timestamptz,
  rotated_to    text
);

create index if not exists refresh_tokens_expires_at_idx
  on auth.refresh_tokens (expires_at)
  where revoked_at is null;

-- Access-token (JWT) revocation list. Cheap kill-switch for leaked JWTs;
-- bearer middleware checks here on every /mcp request. Rows can be GC'd
-- once expires_at passes (the JWT's natural expiry).
create table if not exists auth.revoked_jti (
  jti           text primary key,
  expires_at    timestamptz not null
);

-- Per-user TOTP step tracking. Rejects re-submission of a TOTP code within
-- the same 30s window — closes the in-window replay attack that ±1-window
-- TOTP otherwise leaves open.
create table if not exists auth.totp_state (
  user_id           text primary key,
  last_totp_step    bigint not null default 0
);

-- Audit log. Outcomes split passphrase/TOTP failures so a breach
-- investigation can distinguish "attacker had passphrase but lost on TOTP"
-- from "attacker fuzzed bad passphrases for an hour."
create table if not exists auth.attempts (
  id            bigserial primary key,
  ip            text,
  user_agent    text,
  outcome       text not null,
  client_id     text,
  jti           text,
  reason        text,
  occurred_at   timestamptz not null default now()
);

create index if not exists attempts_ip_occurred_at_idx
  on auth.attempts (ip, occurred_at desc);
