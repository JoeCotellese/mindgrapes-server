# ABOUTME: Registers the entity-repair + recall MCP tools (Slice C, issue #120)
# ABOUTME: onto a FastMCP server, delegating to brain/services/{entities,recall}.py.
from typing import Annotated, Literal

from pydantic import Field

from openbrain.brain.services import entities, recall
from openbrain.mcp import annotations, descriptions, schemas
from openbrain.mcp.guards import _guarded, viewer_sub
from openbrain.mcp.serialization import serialize


def register_entity_tools(mcp) -> None:
    """Register merge/rename/retract/split/unmerge/resolve_entity + recall tools."""

    @mcp.tool(
        name="merge_entities",
        title=annotations.TITLES["merge_entities"],
        description=descriptions.MERGE_ENTITIES,
        annotations=annotations.WRITE_DESTRUCTIVE,
        output_schema=schemas.MergeEntitiesResult.model_json_schema(),
    )
    @_guarded
    def merge_entities(
        loser_id: Annotated[
            str, Field(description="UUID of the duplicate entity to retire.")
        ],
        winner_id: Annotated[str, Field(description="UUID of the canonical survivor.")],
        reason: Annotated[
            str | None, Field(description="Why this merge is correct (audit trail).")
        ] = None,
    ) -> dict:
        return serialize(entities.merge_entities(loser_id, winner_id, reason=reason))

    @mcp.tool(
        name="rename_entity",
        title=annotations.TITLES["rename_entity"],
        description=descriptions.RENAME_ENTITY,
        annotations=annotations.WRITE_IDEMPOTENT_DESTRUCTIVE,
        output_schema=schemas.RenameEntityResult.model_json_schema(),
    )
    @_guarded
    def rename_entity(
        entity_id: str,
        new_canonical_name: str,
        reason: str | None = None,
    ) -> dict:
        return serialize(
            entities.rename_entity(entity_id, new_canonical_name, reason=reason)
        )

    @mcp.tool(
        name="retract_claim",
        title=annotations.TITLES["retract_claim"],
        description=descriptions.RETRACT_CLAIM,
        annotations=annotations.WRITE_DESTRUCTIVE,
        output_schema=schemas.RetractClaimResult.model_json_schema(),
    )
    @_guarded
    def retract_claim(
        claim_id: str,
        reason: Annotated[
            str,
            Field(
                min_length=1,
                description="Why the claim is wrong; written to correction_events.",
            ),
        ],
    ) -> dict:
        return serialize(entities.retract_claim(claim_id, reason))

    @mcp.tool(
        name="split_entity",
        title=annotations.TITLES["split_entity"],
        description=descriptions.SPLIT_ENTITY,
        annotations=annotations.WRITE_DESTRUCTIVE,
        output_schema=schemas.SplitEntityResult.model_json_schema(),
    )
    @_guarded
    def split_entity(
        source_entity_id: Annotated[
            str, Field(description="UUID of the over-collapsed entity to split.")
        ],
        experience_ids: Annotated[
            list[str],
            Field(
                min_length=1,
                description="Experiences whose references move off the source onto "
                "the target.",
            ),
        ],
        into: Annotated[
            dict,
            Field(
                description="Either {canonical_name, kind?, aliases?, metadata?} to "
                "mint a new entity or {entity_id} for an existing one."
            ),
        ],
        reason: Annotated[
            str | None, Field(description="Why this split is correct (audit trail).")
        ] = None,
    ) -> dict:
        return serialize(
            entities.split_entity(source_entity_id, experience_ids, into, reason=reason)
        )

    @mcp.tool(
        name="unmerge_entity",
        title=annotations.TITLES["unmerge_entity"],
        description=descriptions.UNMERGE_ENTITY,
        annotations=annotations.WRITE_DESTRUCTIVE,
        output_schema=schemas.UnmergeEntityResult.model_json_schema(),
    )
    @_guarded
    def unmerge_entity(
        entity_id: Annotated[
            str, Field(description="UUID of the entity whose merged_into to clear.")
        ],
        reason: Annotated[
            str | None, Field(description="Why the merge was wrong (audit trail).")
        ] = None,
    ) -> dict:
        return serialize(entities.unmerge_entity(entity_id, reason=reason))

    @mcp.tool(
        name="resolve_entity",
        title=annotations.TITLES["resolve_entity"],
        description=descriptions.RESOLVE_ENTITY,
        annotations=annotations.READ,
        output_schema=schemas.ResolveEntityResult.model_json_schema(),
    )
    @_guarded
    def resolve_entity(
        name: str,
        context_text: Annotated[
            str | None,
            Field(
                description="Optional disambiguating sentence/paragraph; embedded "
                "server-side."
            ),
        ] = None,
        kind: Literal["person", "org", "event", "place", "concept"] = "person",
        top_k: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> dict:
        return serialize(
            entities.resolve_entity(
                name, context_text=context_text, kind=kind, top_k=top_k
            )
        )

    @mcp.tool(
        name="recall_recent",
        title=annotations.TITLES["recall_recent"],
        description=descriptions.RECALL_RECENT,
        annotations=annotations.READ,
        output_schema=schemas.RecallRecentResult.model_json_schema(),
    )
    @_guarded
    def recall_recent(
        query: Annotated[
            str | None,
            Field(
                description="Optional natural-language query; omit for a pure recency "
                "listing."
            ),
        ] = None,
        days: Annotated[int, Field(ge=1, description="Look-back window in days.")] = 7,
        source_kind: Annotated[
            Literal["transcript", "manual", "derived", "imported"] | None,
            Field(description="Restrict by capture source."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        return serialize(
            recall.recall_recent(
                viewer_sub(), query, days, source_kind=source_kind, limit=limit
            )
        )

    @mcp.tool(
        name="who_was_at",
        title=annotations.TITLES["who_was_at"],
        description=descriptions.WHO_WAS_AT,
        annotations=annotations.READ,
        output_schema=schemas.WhoWasAtResult.model_json_schema(),
    )
    @_guarded
    def who_was_at(
        experience_id: str | None = None,
        date: Annotated[
            str | None, Field(description="ISO calendar date (YYYY-MM-DD).")
        ] = None,
    ) -> dict:
        return serialize(recall.who_was_at(experience_id=experience_id, date=date))

    @mcp.tool(
        name="relationships_to",
        title=annotations.TITLES["relationships_to"],
        description=descriptions.RELATIONSHIPS_TO,
        annotations=annotations.READ,
        output_schema=schemas.RelationshipsToResult.model_json_schema(),
    )
    @_guarded
    def relationships_to(
        entity_id: str,
        max_hops: Annotated[int, Field(ge=1, le=6)] = 2,
        min_confidence: Annotated[float, Field(ge=0, le=1)] = 0.6,
    ) -> dict:
        return serialize(
            recall.relationships_to(
                entity_id, max_hops=max_hops, min_confidence=min_confidence
            )
        )
