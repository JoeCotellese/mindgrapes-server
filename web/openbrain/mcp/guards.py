# ABOUTME: Shared MCP tool helpers — viewer resolution + the error-sanitizing
# ABOUTME: guard decorator, extracted so server.py and tools/ both import them.
import functools

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from openbrain.mcp.errors import sanitize_error


def viewer_sub() -> str | None:
    """The authenticated member's subject, or None (legacy/system) when absent.

    A null viewer bypasses the read filter and sees
    everything; a member sees their own + shared rows.
    """
    token = get_access_token()
    if token is None:
        return None
    return token.claims.get("sub")


def _guarded(fn):
    """Wrap a tool handler so any error crosses the boundary sanitized.

    Every tool handler funnels errors through sanitize_error here; FastMCP
    turns a ToolError into an isError result with this message.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as err:
            raise ToolError(sanitize_error(err)) from err

    return wrapper
