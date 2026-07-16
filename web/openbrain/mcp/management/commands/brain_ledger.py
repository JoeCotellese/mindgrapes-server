# ABOUTME: Operator CLI for the brain schema-migration ledger — status/migrate/baseline.
# ABOUTME: Thin Django management-command dispatch over ledger.py.
from django.core.management.base import BaseCommand

from openbrain.mcp import ledger


class Command(BaseCommand):
    help = (
        "Inspect/apply/baseline the brain schema-migration ledger.\n"
        "  status     show applied vs pending migrations\n"
        "  migrate    apply pending migrations (each in one transaction)\n"
        "  baseline   stamp an existing at-HEAD volume into the ledger (no SQL run)"
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["status", "migrate", "baseline"])

    def handle(self, *args, **options):
        action = options["action"]
        if action == "status":
            raise SystemExit(self._status())
        if action == "migrate":
            raise SystemExit(self._migrate())
        raise SystemExit(self._baseline())

    def _status(self) -> int:
        result = ledger.status()
        if not result.initialized:
            self.stdout.write(
                "[migrate] ledger not initialized — run "
                "`python manage.py brain_ledger baseline`"
            )
            return 1
        s = result.status
        self.stdout.write(
            f"[migrate] ledger has {len(result.ledger)} applied migration(s)"
        )
        if s.pending:
            self.stdout.write(f"  pending:       {', '.join(s.pending)}")
        if s.extra:
            self.stdout.write(f"  ahead/unknown: {', '.join(s.extra)}")
        if s.out_of_order:
            self.stdout.write(f"  out-of-order:  {', '.join(s.out_of_order)}")
        if s.status == "in-sync":
            self.stdout.write("[migrate] schema up to date")
        elif s.extra or s.out_of_order:
            self.stdout.write(
                "[migrate] DRIFT — manual intervention required "
                "(ledger diverges from this build)"
            )
        else:
            self.stdout.write(
                "[migrate] DRIFT — run `python manage.py brain_ledger migrate`"
            )
        return 0 if s.status == "in-sync" else 1

    def _migrate(self) -> int:
        applied = ledger.migrate()
        self.stdout.write(
            f"[migrate] applied: {', '.join(applied)}"
            if applied
            else "[migrate] nothing to apply — already up to date"
        )
        return 0

    def _baseline(self) -> int:
        ledger.baseline()
        self.stdout.write("[migrate] baseline complete — ledger stamped to HEAD")
        return 0
