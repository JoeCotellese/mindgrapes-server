# ABOUTME: Registers fetch-by-id read MCP tools wrapping the brain web-read layer —
# ABOUTME: get_experience, the fetch half of the search/fetch pair (issue #149).
from typing import Annotated

from pydantic import Field

from openbrain.brain.services import reads
from openbrain.mcp import annotations, descriptions, schemas
from openbrain.mcp.guards import _guarded, viewer_sub
from openbrain.mcp.serialization import serialize


def register_read_tools(mcp) -> None:
    """Register the fetch-by-id read tools onto a FastMCP server."""

    @mcp.tool(
        name="get_experience",
        title=annotations.TITLES["get_experience"],
        description=descriptions.GET_EXPERIENCE,
        annotations=annotations.READ,
        output_schema=schemas.GetExperienceResult.model_json_schema(),
    )
    @_guarded
    def get_experience(
        experience_id: Annotated[
            str, Field(description="UUID of the experience to fetch.")
        ],
    ) -> dict:
        # found=false is the identical response for a missing id and a private id
        # the viewer can't read — the service collapses both to None, so existence
        # is never leaked.
        detail = reads.get_experience_detail(viewer_sub(), experience_id)
        if detail is None:
            return {"found": False}
        return serialize({"found": True, **detail})
