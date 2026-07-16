# ABOUTME: Registers the review/correction/disambiguation + update_experience MCP
# ABOUTME: tools (Slice C, #120) onto a FastMCP server, delegating to the services.
from typing import Annotated, Literal

from pydantic import Field

from openbrain.brain.services import edits, reviews
from openbrain.mcp import annotations, descriptions, schemas
from openbrain.mcp.guards import _guarded, viewer_sub
from openbrain.mcp.serialization import serialize


def register_review_tools(mcp) -> None:
    """Register review_queue/propose/resolve_correction/disambiguation + update."""

    @mcp.tool(
        name="review_queue",
        title=annotations.TITLES["review_queue"],
        description=descriptions.REVIEW_QUEUE,
        annotations=annotations.READ,
        output_schema=schemas.ReviewQueueResult.model_json_schema(),
    )
    @_guarded
    def review_queue(
        kind: Literal[
            "all",
            "merge_candidates",
            "low_confidence_claims",
            "contradictions",
            "disambiguations",
            "proposed_corrections",
        ] = "all",
    ) -> dict:
        return serialize(reviews.review_queue(kind))

    @mcp.tool(
        name="propose_correction",
        title=annotations.TITLES["propose_correction"],
        description=descriptions.PROPOSE_CORRECTION,
        annotations=annotations.WRITE_ADDITIVE,
        output_schema=schemas.ProposeCorrectionResult.model_json_schema(),
    )
    @_guarded
    def propose_correction(
        target_kind: Literal["experience", "claim", "entity"],
        target_id: str,
        suggested_change: Annotated[
            dict, Field(description="Free-form jsonb describing the proposed mutation.")
        ],
        reason: str | None = None,
    ) -> dict:
        return serialize(
            reviews.propose_correction(
                target_kind, target_id, suggested_change, reason=reason
            )
        )

    @mcp.tool(
        name="resolve_correction",
        title=annotations.TITLES["resolve_correction"],
        description=descriptions.RESOLVE_CORRECTION,
        annotations=annotations.WRITE_DESTRUCTIVE,
        output_schema=schemas.ResolveCorrectionResult.model_json_schema(),
    )
    @_guarded
    def resolve_correction(
        id: Annotated[
            str, Field(description="UUID of the brain.proposed_corrections row.")
        ],
        decision: Annotated[
            Literal["apply", "reject"],
            Field(
                description="apply dispatches the suggested change; reject just "
                "stamps the row."
            ),
        ],
        reason: Annotated[
            str | None,
            Field(description="Why; passed through to the dispatched repair on apply."),
        ] = None,
    ) -> dict:
        return serialize(reviews.resolve_correction(id, decision, reason=reason))

    @mcp.tool(
        name="request_disambiguation",
        title=annotations.TITLES["request_disambiguation"],
        description=descriptions.REQUEST_DISAMBIGUATION,
        annotations=annotations.WRITE_ADDITIVE,
        output_schema=schemas.RequestDisambiguationResult.model_json_schema(),
    )
    @_guarded
    def request_disambiguation(
        question: str,
        options: Annotated[list[schemas.DisambiguationOption], Field(min_length=2)],
        context: Annotated[
            dict | None,
            Field(
                description="Free-form jsonb attached to the disambiguation row for "
                "context."
            ),
        ] = None,
    ) -> dict:
        return serialize(
            reviews.request_disambiguation(
                question,
                [o.model_dump(exclude_unset=True) for o in options],
                context=context,
            )
        )

    @mcp.tool(
        name="resolve_disambiguation",
        title=annotations.TITLES["resolve_disambiguation"],
        description=descriptions.RESOLVE_DISAMBIGUATION,
        annotations=annotations.WRITE_DESTRUCTIVE,
        output_schema=schemas.ResolveDisambiguationResult.model_json_schema(),
    )
    @_guarded
    def resolve_disambiguation(
        token: str,
        choice: int | str | schemas.DisambiguationOption,
    ) -> dict:
        choice_value = (
            choice.model_dump(exclude_unset=True)
            if isinstance(choice, schemas.DisambiguationOption)
            else choice
        )
        return serialize(reviews.resolve_disambiguation(token, choice_value))

    @mcp.tool(
        name="update_experience",
        title=annotations.TITLES["update_experience"],
        description=descriptions.UPDATE_EXPERIENCE,
        annotations=annotations.WRITE_IDEMPOTENT_DESTRUCTIVE,
        output_schema=schemas.UpdateExperienceResult.model_json_schema(),
    )
    @_guarded
    def update_experience(
        id: str,
        patch: schemas.UpdateExperiencePatch,
        reason: str | None = None,
    ) -> dict:
        return serialize(
            edits.update_experience(
                viewer_sub(),
                id,
                patch.model_dump(exclude_unset=True),
                reason=reason,
            )
        )
