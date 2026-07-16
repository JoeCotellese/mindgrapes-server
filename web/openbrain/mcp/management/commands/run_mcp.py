# ABOUTME: Management command serving the Python MCP over Streamable HTTP (entrypoint #2).
# ABOUTME: Runs the brain schema boot-gate first, then serves the FastMCP http app.
from django.conf import settings
from django.core.management.base import BaseCommand

from openbrain.mcp.boot import SchemaDriftError, assert_schema_up_to_date
from openbrain.mcp.server import build_server


class Command(BaseCommand):
    help = "Serve the Mind Grapes MCP server (Streamable HTTP)."

    def add_arguments(self, parser):
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("--port", type=int, default=8000)
        parser.add_argument(
            "--path",
            default=None,
            help="MCP endpoint path (defaults to settings.MCP_PATH).",
        )
        parser.add_argument(
            "--no-auth",
            action="store_true",
            help="Serve without bearer auth — DEV ONLY.",
        )
        parser.add_argument(
            "--skip-boot-gate",
            action="store_true",
            help="Skip the schema-drift gate — DEV ONLY.",
        )

    def handle(self, *args, **options):
        # Boot gate (#91/#115): refuse to serve a brain whose schema has drifted.
        if not options["skip_boot_gate"]:
            try:
                assert_schema_up_to_date()
            except SchemaDriftError as exc:
                self.stderr.write(self.style.ERROR(f"[migrate] FATAL: {exc}"))
                raise SystemExit(1) from exc
            self.stdout.write("[migrate] schema up to date")

        # Auth is built lazily (reads settings.OAUTH_JWKS_URL); --no-auth skips it
        # for local smoke tests where no authorization server is reachable.
        auth = None
        if not options["no_auth"]:
            from openbrain.mcp.auth import build_auth

            auth = build_auth()

        server = build_server(auth=auth)
        path = options["path"] or getattr(settings, "MCP_PATH", "/mcp/")
        self.stdout.write(
            f"open-brain MCP (python) serving on "
            f"{options['host']}:{options['port']}{path} "
            f"(auth={'on' if auth else 'off'})"
        )
        server.run(
            transport="http", host=options["host"], port=options["port"], path=path
        )
