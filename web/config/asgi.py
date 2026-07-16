"""ASGI entrypoint for the Mind Grapes Django app.

A plain Django ASGI application. The MCP server runs as its own process —
the Python `run_mcp` service at /mcp, reached through the Caddy path split —
so no MCP routes are mounted here (epic #117).
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

application = get_asgi_application()
