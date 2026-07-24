# ABOUTME: Registers the capture_image MCP tool (#42) wrapping the image ingest service.
# ABOUTME: capture_thought for pixels — same owner/visibility/participant handling.
from typing import Annotated, Literal

from django.conf import settings
from pydantic import Field

from openbrain.brain.services import image_captures
from openbrain.mcp import annotations, descriptions, schemas
from openbrain.mcp.guards import _guarded, viewer_sub
from openbrain.mcp.serialization import serialize


def register_image_tools(mcp) -> None:
    """Register the image-capture tool onto a FastMCP server."""

    @mcp.tool(
        name="capture_image",
        title=annotations.TITLES["capture_image"],
        description=descriptions.CAPTURE_IMAGE,
        annotations=annotations.WRITE_ADDITIVE,
        output_schema=schemas.CaptureImageResult.model_json_schema(),
    )
    @_guarded
    def capture_image(
        image_base64: Annotated[
            str | None,
            Field(
                description="Base64 of a SMALL image (<=256KB base64 — pasted "
                "screenshots only). For real photos use object_key instead. "
                "Supply exactly one of image_base64 / object_key."
            ),
        ] = None,
        object_key: Annotated[
            str | None,
            Field(
                description="Key of a derivative the app already uploaded via a "
                "presigned S3 PUT. Requires original_sha256 + mime (+ width/height). "
                "The durable path for real photos; no bytes cross this boundary."
            ),
        ] = None,
        original_sha256: Annotated[
            str | None,
            Field(description="Hex sha256 of the ORIGINAL bytes (object_key path)."),
        ] = None,
        mime: Annotated[
            str | None, Field(description="Derivative MIME type (object_key path).")
        ] = None,
        width: Annotated[
            int | None, Field(description="Derivative pixel width (object_key path).")
        ] = None,
        height: Annotated[
            int | None, Field(description="Derivative pixel height (object_key path).")
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description="Human/device description — the primary, embedded "
                "content. Strongly preferred; omitting it triggers the vision "
                "fallback only for 'shared' captures."
            ),
        ] = None,
        ocr: Annotated[
            str | None,
            Field(
                description="On-device OCR / detected text; folded into the "
                "embedded content so it's searchable, raw copy kept in metadata."
            ),
        ] = None,
        occurred_at: Annotated[
            str | None,
            Field(description="ISO 8601 time the photo was taken; EXIF is the fallback."),
        ] = None,
        location: Annotated[
            schemas.LocationInput | None,
            Field(description="{lat,lng,label,accuracy_m,source}; params beat EXIF GPS."),
        ] = None,
        event: Annotated[
            str | None,
            Field(description="Event this image belongs to; linked as an entity."),
        ] = None,
        participants: Annotated[
            list[schemas.ParticipantInput] | None,
            Field(description="People present, resolved like capture_thought."),
        ] = None,
        metadata: Annotated[
            dict | None,
            Field(description="Open-ended structured metadata (bounded size/depth)."),
        ] = None,
        visibility: Annotated[
            Literal["private", "shared"] | None,
            Field(
                description="'private' (default) never egresses bytes to vision; "
                "'shared' permits the vision fallback when no description is given."
            ),
        ] = None,
    ) -> dict:
        result = image_captures.capture_image(
            owner=viewer_sub() or settings.BRAIN_DEFAULT_OWNER,
            account_id=settings.BRAIN_HOUSEHOLD_ACCOUNT_ID,
            visibility=visibility or "private",
            image_base64=image_base64,
            object_key=object_key,
            original_sha256=original_sha256,
            mime=mime,
            width=width,
            height=height,
            description=description,
            ocr=ocr,
            occurred_at=occurred_at,
            location=location.model_dump() if location is not None else None,
            event=event,
            participants=(
                [p.model_dump() for p in participants]
                if participants is not None
                else None
            ),
            metadata=metadata,
        )
        return serialize(result)
