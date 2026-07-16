# ABOUTME: Django AppConfig for the Python MCP server (Mind Grapes port, epic #117).
# ABOUTME: Hosts the FastMCP protocol adapter; label "mcpserver" avoids the mcp pkg name.
from django.apps import AppConfig


class MCPConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "openbrain.mcp"
    label = "mcpserver"
