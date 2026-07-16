# ABOUTME: Builds the Mind Grapes FastMCP server — registers the read tools +
# ABOUTME: brain:// resources over the shared brain service layer (epic #117, Slice A).
from typing import Annotated, Literal

from django.conf import settings
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from openbrain.brain.services import captures, mcp_reads
from openbrain.mcp import annotations, descriptions, schemas
from openbrain.mcp.guards import _guarded, viewer_sub
from openbrain.mcp.resources import workflows_document
from openbrain.mcp.serialization import serialize
from openbrain.mcp.tools.entity_tools import register_entity_tools
from openbrain.mcp.tools.read_tools import register_read_tools
from openbrain.mcp.tools.review_tools import register_review_tools

SERVER_NAME = "open-brain"
SERVER_VERSION = "0.1.0"


def build_server(auth=None) -> FastMCP:
    """Construct the FastMCP server. Pass auth=build_auth() in production; tests
    build it without auth and drive it through the in-memory client."""
    mcp = FastMCP(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        auth=auth,
        instructions=descriptions.SERVER_INSTRUCTIONS,
    )

    @mcp.tool(
        name="search_thoughts",
        title=annotations.TITLES["search_thoughts"],
        description=descriptions.SEARCH_THOUGHTS,
        annotations=annotations.READ,
        output_schema=schemas.SearchThoughtsResult.model_json_schema(),
    )
    @_guarded
    def search_thoughts(
        query: Annotated[str, Field(description="Natural-language query")],
        limit: int = 10,
        threshold: Annotated[
            float, Field(description="Minimum fused_score (0 = no minimum)")
        ] = 0,
        person: Annotated[
            str | None,
            Field(
                description="Restrict to experiences mentioning this person "
                "(matches canonical name or any alias, follows soft-merges)."
            ),
        ] = None,
        topic: Annotated[
            str | None,
            Field(
                description="Restrict to experiences mentioning this "
                "topic/concept (alias-aware)."
            ),
        ] = None,
        with_provenance: Annotated[
            bool, Field(description="Attach the claim provenance block to each result.")
        ] = False,
        min_confidence: Annotated[
            float,
            Field(
                description="Minimum claim confidence for the provenance block "
                "(0 = include all)."
            ),
        ] = 0,
        include_retracted: Annotated[
            bool,
            Field(
                description="Include retracted claims in the provenance block "
                "(default false hides them)."
            ),
        ] = False,
    ) -> dict:
        hits = mcp_reads.hybrid_search(
            viewer_sub(),
            query,
            limit=limit,
            threshold=threshold,
            person=person,
            topic=topic,
            with_provenance=with_provenance,
            min_confidence=min_confidence,
            include_retracted=include_retracted,
        )
        return serialize({"count": len(hits), "hits": hits})

    @mcp.tool(
        name="list_thoughts",
        title=annotations.TITLES["list_thoughts"],
        description=descriptions.LIST_THOUGHTS,
        annotations=annotations.READ,
        output_schema=schemas.ListThoughtsResult.model_json_schema(),
    )
    @_guarded
    def list_thoughts(
        limit: int = 10,
        type: Annotated[
            str | None,
            Field(
                description="Filter by type: observation, task, idea, reference, "
                "person_note"
            ),
        ] = None,
        topic: Annotated[str | None, Field(description="Filter by topic tag")] = None,
        person: Annotated[
            str | None, Field(description="Filter by person mentioned")
        ] = None,
        days: Annotated[
            int | None, Field(description="Only thoughts from the last N days")
        ] = None,
    ) -> dict:
        rows = mcp_reads.list_thoughts(
            viewer_sub(),
            limit=limit,
            type=type,
            topic=topic,
            person=person,
            days=days,
        )
        return serialize({"count": len(rows), "thoughts": rows})

    @mcp.tool(
        name="thought_stats",
        title=annotations.TITLES["thought_stats"],
        description=descriptions.THOUGHT_STATS,
        annotations=annotations.READ,
        output_schema=schemas.ThoughtStatsResult.model_json_schema(),
    )
    @_guarded
    def thought_stats() -> dict:
        return serialize(mcp_reads.thought_stats())

    @mcp.tool(
        name="capture_thought",
        title=annotations.TITLES["capture_thought"],
        description=descriptions.CAPTURE_THOUGHT,
        annotations=annotations.WRITE_ADDITIVE,
        output_schema=schemas.CaptureThoughtResult.model_json_schema(),
    )
    @_guarded
    def capture_thought(
        content: Annotated[
            str,
            Field(
                description="The thought to capture — a clear, standalone "
                "statement that will make sense when retrieved later by any AI"
            ),
        ],
        occurred_at: Annotated[
            str | None,
            Field(
                description="ISO 8601 timestamp of when the event happened "
                "(distinct from when it was captured). Triggers the structured path."
            ),
        ] = None,
        participants: Annotated[
            list[schemas.ParticipantInput] | None,
            Field(
                description="People/entities present at the event. If `entity_id` "
                "is provided, the participant is linked directly; otherwise the "
                "server resolves the name against existing entities (creating one "
                "if no match). Triggers the structured path."
            ),
        ] = None,
        predicate_hints: Annotated[
            list[schemas.PredicateHintInput] | None,
            Field(
                description="Caller-asserted claims to seed async claim extraction "
                "(consumed by pg_cron consolidation in issue #14). Triggers the "
                "structured path."
            ),
        ] = None,
        source_kind: Annotated[
            Literal["transcript", "manual", "derived", "imported"] | None,
            Field(
                description="Where the content came from. Defaults to 'manual'. "
                "Triggers the structured path when set explicitly."
            ),
        ] = None,
        source_ref: Annotated[
            str | None,
            Field(
                description="Pointer back to the original source (vault path, "
                "transcript id, URL). Triggers the structured path."
            ),
        ] = None,
        visibility: Annotated[
            Literal["private", "shared"] | None,
            Field(
                description="Who in the household can read this experience. "
                "Defaults to 'private' (only the owner). 'shared' marks it "
                "readable by other members. Does not trigger the structured path."
            ),
        ] = None,
    ) -> dict:
        result = captures.capture(
            content=content,
            owner=viewer_sub() or settings.BRAIN_DEFAULT_OWNER,
            account_id=settings.BRAIN_HOUSEHOLD_ACCOUNT_ID,
            visibility=visibility or "private",
            occurred_at=occurred_at,
            participants=(
                [p.model_dump() for p in participants]
                if participants is not None
                else None
            ),
            predicate_hints=(
                [h.model_dump() for h in predicate_hints]
                if predicate_hints is not None
                else None
            ),
            source_kind=source_kind,
            source_ref=source_ref,
        )
        return serialize(result)

    @mcp.resource(
        "brain://workflows",
        name="workflows",
        title="Brain Workflows",
        description=(
            "Named tool-composition recipes (capture_with_dedup, research_topic, "
            "correct_identity) so consuming LLMs can follow canonical patterns "
            "instead of rediscovering them from individual tool descriptions."
        ),
        mime_type="application/json",
    )
    def workflows_resource() -> dict:
        return workflows_document()

    @mcp.resource(
        "brain://summary",
        name="summary",
        title="Brain Summary",
        description=(
            "Aggregate counts (experiences, entities, claims), top-mentioned "
            "entities, top topics, and the captured-at time range. Backed by a "
            "nightly-refreshed materialized view so reads are cheap on session start."
        ),
        mime_type="application/json",
    )
    def summary_resource() -> dict:
        return serialize(mcp_reads.summary_for_resource())

    @mcp.resource(
        "brain://entities/recent",
        name="entities-recent",
        title="Recent Entities",
        description=(
            "Entities created or merged in the last 30 days, newest first. Includes "
            "merged_into so reviewers can see consolidations alongside fresh creations."
        ),
        mime_type="application/json",
    )
    def recent_entities_resource() -> dict:
        return serialize(mcp_reads.recent_entities(30))

    @mcp.resource(
        "brain://reviews/pending",
        name="reviews-pending",
        title="Pending Reviews",
        description=(
            "Counts of items awaiting human review across the five surfaces of "
            "review_queue: borderline merge_candidates, low-confidence inferred "
            "claims, contradictions, pending disambiguations, and pending "
            "propose_correction rows."
        ),
        mime_type="application/json",
    )
    def pending_reviews_resource() -> dict:
        return serialize(mcp_reads.pending_reviews())

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> PlainTextResponse:
        # Liveness for the container healthcheck.
        return PlainTextResponse("ok")

    # Fetch-by-id reads (#149) and the Slice C entity-repair / recall / review
    # tools live in their own modules to keep build_server readable; they register
    # onto the same server.
    register_read_tools(mcp)
    register_entity_tools(mcp)
    register_review_tools(mcp)

    return mcp
