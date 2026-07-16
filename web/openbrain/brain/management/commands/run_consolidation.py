# ABOUTME: Management command running the pg_cron consolidation worker (entrypoint #3).
# ABOUTME: Runs the schema boot-gate, then LISTENs on brain_consolidate and consolidates each row.

import logging
import signal
import threading

from django.core.management.base import BaseCommand
from django.db import connection

from openbrain.brain.consolidation import (
    default_consolidation_extractor,
    handle_notification,
    run_consolidation_listener,
)
from openbrain.mcp.boot import SchemaDriftError, assert_schema_up_to_date

logger = logging.getLogger("openbrain.brain.consolidation")


class Command(BaseCommand):
    help = "Run the Mind Grapes consolidation worker (LISTEN brain_consolidate)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-boot-gate",
            action="store_true",
            help="Skip the schema-drift gate — DEV ONLY.",
        )

    def handle(self, *args, **options):
        # Boot gate (#91/#115): refuse to consolidate against a drifted schema.
        if not options["skip_boot_gate"]:
            try:
                assert_schema_up_to_date()
            except SchemaDriftError as exc:
                self.stderr.write(self.style.ERROR(f"[migrate] FATAL: {exc}"))
                raise SystemExit(1) from exc
            self.stdout.write("[migrate] schema up to date")

        stop_event = threading.Event()

        def _stop(signum, _frame):
            self.stdout.write(f"consolidation: signal {signum} received; stopping")
            stop_event.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        def _handle(experience_id):
            # Self-heal the work connection after a DB blip before each unit of
            # work; the dedicated LISTEN connection has its own reconnect loop.
            connection.close_if_unusable_or_obsolete()
            handle_notification(
                experience_id,
                extract=default_consolidation_extractor,
                logger=logger,
            )

        self.stdout.write("consolidation worker starting; LISTEN brain_consolidate")
        run_consolidation_listener(
            handle=_handle,
            should_stop=stop_event.is_set,
            logger=logger,
        )
        self.stdout.write("consolidation worker stopped")
