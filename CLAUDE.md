# Mind Grapes — repo guide

A self-hosted, MCP-accessible second brain. Episodic + semantic schema in
Postgres, served by a Python MCP server (FastMCP) that shares the Django
codebase, fronted by Caddy, reachable over Tailscale and the public internet.
OAuth 2.1 with passkey auth. The goal is one persistent memory layer any
AI tool can plug into.

Naming: the product is **Mind Grapes**; internal identifiers (Docker
services, DB names, the `openbrain` Python package) deliberately keep the
original `openbrain` name — the brand is decoupled from infrastructure
(#175). Don't "fix" internal names to match the brand.

You — agent or human — are working in this repo. Read this file first.

## Where the roadmap lives

GitHub is the source of truth for features, bugs, and the roadmap. New
feature ideas, design discussions, and follow-ups belong in GitHub Issues —
not in new files under `docs/`. `docs/` holds only durable reference
material (deploy runbook, predicate vocabulary, standing design records);
anything speculative or in-flight is an issue.

## Before you suggest a schema or storage change

**Read the canonical schema in `init/`** — it's heavily commented and is
the only thing that doesn't drift. The relevant files, in order:

- `init/03-brain.sql` — base brain schema (experiences, entities, claims,
  claim_sources, mentions, merge_candidates, correction_events,
  recall_events; the `brain.resolve_entity` fused-resolver SQL function;
  the source_kind / entity_kind / support_kind / polarity / target_kind /
  recall_outcome / consolidation_status enums)
- `init/04-hybrid-search.sql` — `brain.match_brain_hybrid` RRF search function
- `init/05-consolidation.sql` — consolidation procedures
- `init/06-tools.sql` — tool helper functions
- `init/07-summary-cache.sql` — summary cache
- `init/08-thoughts-view.sql` — table → view cutover (legacy `public.thoughts`
  now reads from `brain.experiences`)
- `init/09-auth-schema.sql` — `auth.users`, sessions, attempts, oauth clients
- `init/10-supersede.sql` — supersede + soft-delete columns on experiences
- `init/11-live-filter.sql` — live filter view
- `init/12-soft-privacy.sql` — owner / account_id / visibility columns
- `init/13-viewer-filter.sql` — viewer-scoped read filtering
- `init/14-schema-migrations.sql` — the applied-migration ledger + boot gate
- `init/15-confidence-traversal.sql` — claim-confidence in traversal
- `init/16-alias-scoring.sql` — per-alias entity scoring

Schema deltas are append-only `init/NN-*.sql` files, registered in the `SPINE`
in `web/openbrain/mcp/boot.py` and the `init/14` self-seed, and applied to
existing volumes with `manage.py brain_ledger migrate`.

For the predicate vocabulary used by `brain.claims`, see `docs/predicates.md` —
predicates are constrained but the constraint is convention, not a SQL CHECK,
so read it before inventing one.

Read those files end-to-end before adding columns or enums — most "new"
provenance, identity, or predicate concepts already exist. The enum and
table comments in `init/03-brain.sql` call out the load-bearing ones.

## Before you add or change an MCP tool

**Read `web/openbrain/mcp/descriptions.py`** — every description follows a
five-section template (capability; use-when / don't-use-when / on-empty;
then cost / idempotent / reversible / side-effects). That file is both the
canonical description shipped over MCP *and* the human-readable catalog of
what already exists. Read all of it before adding a new tool — most of the
time the affordance is already there under a slightly different name.

Then read the handler — `web/openbrain/mcp/server.py` (the tool registration)
and `web/openbrain/mcp/tools/`, backed by the `web/openbrain/brain/services/`
layer. Tools are how agents experience the brain — sloppy descriptions are
sloppy product. When you add one, update `descriptions.py` in the same change,
following the five-section template.

## Before you re-litigate a decision

The discussion that produced an architectural choice lives in the GitHub
issue or PR that shipped it. Browse recent merged PRs and closed issues
before arguing the other side — the alternatives have usually already
been weighed. Concrete examples: #41 (OAuth 2.1), #48 (supersede pattern),
#50 (public-deploy hardening), #52 (multi-user auth, open). The standing
architecture rationale (one generic entity/claim graph instead of
per-use-case schemas) is `docs/design-overlay-architecture-2026-07-08.md`.

## Layout

```
/                    docker-compose (live/dev/staging), top-level config, Caddy, Tailscale
bin/                 pg helper symlinked into ~/.local/bin by `make install`
caddy/               edge config (TLS, path-split); Caddyfile + Caddyfile.dev
config/              Postgres tuning overrides
init/                Postgres init scripts — schema lives in 03-brain.sql
systemd/             backup timer/service units for Linux hosts
web/                 the Django app + the Python MCP server (one codebase, Python 3.12)
  openbrain/mcp/     FastMCP server: server.py (entry), tools/, descriptions.py,
                     auth.py, boot.py (schema gate), ledger.py (brain_ledger CLI)
  openbrain/brain/   brain.* data-access seam (db.py) + services/ + extraction/
  openbrain/oauth/   OAuth 2.1 authorization server, JWKS, DCR
  openbrain/accounts/ passkey + enrollment auth
  config/            Django settings (base/production/docker/test*), asgi.py
  test layout        unit tests in */tests/unit; integration in */tests/integration
docs/                deploy.md (operational), predicates.md (claim vocabulary),
                     design-overlay-architecture-2026-07-08.md (design record)
tailscale/           sidecar config
backups/             pg_dump outputs (gitignored content)
```

## Local development

```bash
# One-time
cp .env.example .env                # fill in POSTGRES_PASSWORD, OPENROUTER_API_KEY, SECRET_KEY
cd web && uv sync                   # Python deps
make install                        # symlinks bin/pg into ~/.local/bin (override with PREFIX=)

# Bring up the isolated dev stack (postgres + web + mcp + consolidation + caddy)
cp .env.dev.example .env.dev        # one-time
make dev-up                         # docker compose -f docker-compose.dev.yml up -d --build
# http://localhost:8080/  Django ;  http://localhost:8080/mcp  MCP (401 gate)

# Run the tests
cd web && uv run pytest             # unit suite (sqlite, no Postgres)
make dev-test-integration           # integration suite against the dev Postgres

# Apply a brain schema migration to an existing volume
docker compose -f docker-compose.dev.yml exec mcp python manage.py brain_ledger migrate
```

Deployment to a production host (fronted by Tailscale) is documented in
`docs/deploy.md`.

## House rules

- **Match the surrounding style.** The codebase is Python 3.12 / Django:
  function-based views, raw parameterized SQL at the `brain.*` seam (no Django
  models for `brain.*`), Pydantic schemas at the MCP boundary, sync service
  layer, and very few comments. The existing code is the style guide; don't
  impose external conventions.
- **Don't widen the public API casually.** Every new MCP tool is a forever
  commitment to support that shape. Add tools only when the affordance is
  missing, not because it'd be convenient.
- **Use the supersede pattern, not destructive edits.** Mutations on
  experiences/claims flow through `correction_events` for audit. The schema
  is designed to be append-mostly.
- **Write tests in the layer the change touches.** Pure logic gets a unit test
  in the relevant `web/openbrain/*/tests/unit/`; anything that touches Postgres
  or the HTTP layer gets an integration test in `*/tests/integration/`, run
  against the dev Postgres via `make dev-test-integration`.
- **Never commit secrets.** `.env` is gitignored and must stay that way. If
  you find yourself wanting to commit a token to unblock something, stop
  and ask.
