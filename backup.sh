#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
set -a; source .env; set +a

mkdir -p backups
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
OUT="/backups/${POSTGRES_DB}-${TIMESTAMP}.dump"

# --exclude-schema=cron: pg_cron's bookkeeping can't be restored outside the
# cron-enabled database, and its schedules are re-established by init/05 on a
# fresh restore — so leave it out of the backup.
docker compose exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -F c --exclude-schema=cron -f "$OUT"

echo "wrote backups/${POSTGRES_DB}-${TIMESTAMP}.dump"

# Record load-bearing row counts as of dump time, as a sidecar. The restore
# drill compares a restored copy against THESE counts, not against live (which
# drifts as the brain is written to). Counted seconds after pg_dump, so the
# window for drift between the snapshot and the count is negligible. Keep this
# table list in sync with the restore-drill tooling when it is (re)introduced.
COUNTS="backups/${POSTGRES_DB}-${TIMESTAMP}.counts.tsv"
: > "$COUNTS"
for t in brain.experiences brain.entities brain.claims auth.clients; do
  n=$(docker compose exec -T postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from $t" \
    | tr -d '[:space:]')
  printf '%s\t%s\n' "$t" "$n" >> "$COUNTS"
done
echo "wrote ${COUNTS}"

# Retain the 14 most recent local dumps (fast on-box restore copy), pruning each
# dump's counts sidecar alongside it.
ls -1t backups/*.dump 2>/dev/null | tail -n +15 | while read -r f; do
  rm -f "$f" "${f%.dump}.counts.tsv"
done

# Ship the dump offsite, encrypted + deduped, via restic. The local dump above
# is the quick-restore copy; restic is the disaster copy. Skipped (with a
# warning) when RESTIC_REPOSITORY is unset so a freshly-cloned box still backs
# up locally. restic reads RESTIC_REPOSITORY / RESTIC_PASSWORD / B2_ACCOUNT_ID /
# B2_ACCOUNT_KEY from the environment (sourced from .env above).
if [ -z "${RESTIC_REPOSITORY:-}" ]; then
  echo "RESTIC_REPOSITORY unset — skipping offsite backup (local dump kept)." >&2
elif ! command -v restic >/dev/null 2>&1; then
  echo "restic not installed — skipping offsite backup (local dump kept)." >&2
else
  restic backup --tag openbrain \
    "backups/${POSTGRES_DB}-${TIMESTAMP}.dump" "$COUNTS"
  echo "pushed backups/${POSTGRES_DB}-${TIMESTAMP}.dump to ${RESTIC_REPOSITORY}"
  # Retention/prune is housekeeping, not the backup itself. A transient failure
  # here (repo lock, B2 hiccup) must not fail the run under `set -e` — the dump
  # is already safely offsite.
  restic forget --tag openbrain \
    --keep-daily "${RESTIC_KEEP_DAILY:-14}" \
    --keep-weekly "${RESTIC_KEEP_WEEKLY:-8}" \
    --keep-monthly "${RESTIC_KEEP_MONTHLY:-12}" \
    --prune \
    || echo "warning: restic forget/prune failed (backup itself succeeded)." >&2
fi
