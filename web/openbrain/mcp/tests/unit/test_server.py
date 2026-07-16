# ABOUTME: Unit tests for the FastMCP server wiring via the in-memory client.
# ABOUTME: Services are monkeypatched (no DB); asserts dual output, viewer threading, errors.
import asyncio
import json
from datetime import UTC, datetime

import pytest
from django.conf import settings
from fastmcp import Client
from fastmcp.exceptions import ToolError

from openbrain.brain.services import (
    captures,
    edits,
    entities,
    mcp_reads,
    reads,
    recall,
    reviews,
)
from openbrain.mcp import guards as guards_mod
from openbrain.mcp.server import build_server


def _run(coro):
    return asyncio.run(coro)


def _hit(**overrides):
    base = {
        "id": "e1",
        "content": "a thought",
        "metadata": {"type": "observation"},
        "captured_at": datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC),
        "occurred_at": None,
        "vec_score": 0.5,
        "lex_score": 0.1,
        "fused_score": 0.016,
    }
    base.update(overrides)
    return base


def test_search_thoughts_dual_output_and_serialization(monkeypatch):
    monkeypatch.setattr(mcp_reads, "hybrid_search", lambda *a, **k: [_hit()])
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.call_tool("search_thoughts", {"query": "hi"})

    res = _run(go())
    sc = res.structured_content
    assert sc["count"] == 1
    hit = sc["hits"][0]
    # datetime coerced to JS-style ISO; nullable occurred_at present; provenance omitted.
    assert hit["captured_at"] == "2026-06-20T12:00:00.000Z"
    assert hit["occurred_at"] is None
    assert "provenance" not in hit


def test_search_thoughts_threads_viewer_from_token(monkeypatch):
    captured = {}

    def fake(viewer, query, **kw):
        captured["viewer"] = viewer
        return []

    monkeypatch.setattr(mcp_reads, "hybrid_search", fake)

    class _Tok:
        claims = {"sub": "42"}

    monkeypatch.setattr(guards_mod, "get_access_token", lambda: _Tok())
    server = build_server()

    async def go():
        async with Client(server) as c:
            await c.call_tool("search_thoughts", {"query": "hi"})

    _run(go())
    assert captured["viewer"] == "42"


def test_search_thoughts_null_viewer_without_token(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        mcp_reads,
        "hybrid_search",
        lambda viewer, query, **k: captured.setdefault("viewer", viewer) or [],
    )
    server = build_server()

    async def go():
        async with Client(server) as c:
            await c.call_tool("search_thoughts", {"query": "hi"})

    _run(go())
    assert captured["viewer"] is None


def test_tool_error_is_sanitized(monkeypatch):
    def boom(*a, **k):
        raise ValueError("merge boom")

    monkeypatch.setattr(mcp_reads, "hybrid_search", boom)
    server = build_server()

    async def go():
        async with Client(server) as c:
            await c.call_tool("search_thoughts", {"query": "hi"})

    with pytest.raises(ToolError) as exc:
        _run(go())
    assert "merge boom" in str(exc.value)


def test_list_thoughts_wraps_count(monkeypatch):
    monkeypatch.setattr(
        mcp_reads,
        "list_thoughts",
        lambda *a, **k: [
            {
                "content": "c",
                "metadata": {},
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        ],
    )
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.call_tool("list_thoughts", {"limit": 5})

    sc = _run(go()).structured_content
    assert sc["count"] == 1
    assert sc["thoughts"][0]["created_at"].endswith("Z")


def test_thought_stats_passthrough(monkeypatch):
    monkeypatch.setattr(
        mcp_reads,
        "thought_stats",
        lambda: {
            "total": 0,
            "date_range": None,
            "types": {},
            "top_topics": [],
            "top_people": [],
        },
    )
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.call_tool("thought_stats", {})

    sc = _run(go()).structured_content
    assert sc["total"] == 0 and sc["date_range"] is None


def _experience_detail(**overrides):
    base = {
        "experience": {
            "id": "exp-1",
            "content": "a full thought",
            "captured_at": datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC),
            "occurred_at": None,
            "occurred_window": None,
            "source_kind": "manual",
            "source_ref": None,
            "metadata": {"type": "observation"},
            "consolidation_status": "pending",
            "superseded_by": None,
            "deleted_at": None,
            "owner": "42",
            "visibility": "private",
            "is_live": True,
            "can_change_visibility": True,
        },
        "mentions": [
            {
                "entity_id": "ent-1",
                "canonical_name": "Grace",
                "kind": "person",
                "surface_form": "Grace",
                "merged_into": None,
            }
        ],
        "claims_sourced_here": [
            {
                "claim_id": "c-1",
                "predicate": "works_at",
                "predicate_detail": None,
                "polarity": "asserted",
                "confidence": 0.9,
                "support_kind": "verbatim",
                "source_confidence": None,
                "extracted_by": None,
                "subject": {"id": "ent-1", "canonical_name": "Grace", "kind": "person"},
                "object": {
                    "id": "ent-2",
                    "canonical_name": "Initech",
                    "kind": "org",
                    "literal": None,
                },
            }
        ],
    }
    base.update(overrides)
    return base


def test_get_experience_returns_detail_and_serializes(monkeypatch):
    monkeypatch.setattr(
        reads, "get_experience_detail", lambda *a, **k: _experience_detail()
    )
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.call_tool("get_experience", {"experience_id": "exp-1"})

    sc = _run(go()).structured_content
    assert sc["found"] is True
    assert sc["experience"]["content"] == "a full thought"
    # datetime coerced to JS-style ISO, mentions + claim provenance surfaced.
    assert sc["experience"]["captured_at"] == "2026-06-20T12:00:00.000Z"
    assert sc["mentions"][0]["canonical_name"] == "Grace"
    assert sc["claims_sourced_here"][0]["object"]["canonical_name"] == "Initech"


def test_get_experience_not_found_returns_found_false(monkeypatch):
    monkeypatch.setattr(reads, "get_experience_detail", lambda *a, **k: None)
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.call_tool("get_experience", {"experience_id": "missing"})

    sc = _run(go()).structured_content
    # Missing id and private-not-yours both collapse here; existence never leaks.
    assert sc["found"] is False
    assert "experience" not in sc


def test_get_experience_threads_viewer_from_token(monkeypatch):
    captured = {}

    def fake(viewer, experience_id):
        captured["viewer"] = viewer
        captured["eid"] = experience_id
        return None

    monkeypatch.setattr(reads, "get_experience_detail", fake)

    class _Tok:
        claims = {"sub": "77"}

    monkeypatch.setattr(guards_mod, "get_access_token", lambda: _Tok())
    server = build_server()

    async def go():
        async with Client(server) as c:
            await c.call_tool("get_experience", {"experience_id": "exp-9"})

    _run(go())
    assert captured["viewer"] == "77"
    assert captured["eid"] == "exp-9"


def test_capture_thought_bare_threads_owner_and_returns_structured(monkeypatch):
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return {
            "experience_id": "e1",
            "is_structured": False,
            "metadata": {"source": "mcp", "topics": ["x"]},
        }

    monkeypatch.setattr(captures, "capture", fake)

    class _Tok:
        claims = {"sub": "42"}

    monkeypatch.setattr(guards_mod, "get_access_token", lambda: _Tok())
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.call_tool("capture_thought", {"content": "a bare thought"})

    sc = _run(go()).structured_content
    assert sc["is_structured"] is False
    assert sc["metadata"]["source"] == "mcp"
    # Owner threaded from the token sub; bare path leaves structured fields absent.
    assert captured["owner"] == "42"
    assert captured["participants"] is None
    assert captured["account_id"] == settings.BRAIN_HOUSEHOLD_ACCOUNT_ID


def test_capture_thought_owner_defaults_without_token(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        captures,
        "capture",
        lambda **kw: (
            captured.update(kw)
            or {"experience_id": "e1", "is_structured": False, "metadata": {}}
        ),
    )
    server = build_server()

    async def go():
        async with Client(server) as c:
            await c.call_tool("capture_thought", {"content": "x"})

    _run(go())
    assert captured["owner"] == settings.BRAIN_DEFAULT_OWNER


def test_capture_thought_structured_converts_input_models(monkeypatch):
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return {
            "experience_id": "e9",
            "is_structured": True,
            "metadata": {"source_kind": "transcript"},
            "extracted_entities": [
                {"surface": "Grace", "entity_id": "ent-1", "action": "created"}
            ],
            "borderline_matches": [],
            "claims_pending": True,
        }

    monkeypatch.setattr(captures, "capture", fake)
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.call_tool(
                "capture_thought",
                {
                    "content": "met Grace",
                    "participants": [{"name": "Grace"}],
                    "predicate_hints": [
                        {
                            "subject": "Grace",
                            "predicate": "works_at",
                            "object": "Initech",
                            "support_kind": "verbatim",
                        }
                    ],
                    "source_kind": "transcript",
                },
            )

    sc = _run(go()).structured_content
    assert sc["is_structured"] is True
    assert sc["claims_pending"] is True
    assert sc["extracted_entities"][0]["action"] == "created"
    # Pydantic input models are dumped to plain dicts for the service layer.
    assert captured["participants"] == [{"name": "Grace", "entity_id": None}]
    assert captured["predicate_hints"][0]["predicate"] == "works_at"
    assert captured["source_kind"] == "transcript"


def test_capture_thought_error_is_sanitized(monkeypatch):
    def boom(**kw):
        raise ValueError("capture boom")

    monkeypatch.setattr(captures, "capture", boom)
    server = build_server()

    async def go():
        async with Client(server) as c:
            await c.call_tool("capture_thought", {"content": "x"})

    with pytest.raises(ToolError) as exc:
        _run(go())
    assert "capture boom" in str(exc.value)


def test_server_advertises_instructions():
    server = build_server()

    async def go():
        async with Client(server) as c:
            return c.initialize_result.instructions

    instructions = _run(go())
    assert instructions
    # Orients the client across the read/write/maintenance surface.
    for token in (
        "search_thoughts",
        "recall_recent",
        "capture_thought",
        "review_queue",
        "brain://workflows",
    ):
        assert token in instructions


def test_all_tools_and_resources_registered():
    server = build_server()

    async def go():
        async with Client(server) as c:
            tools = {t.name for t in await c.list_tools()}
            resources = {str(r.uri) for r in await c.list_resources()}
            return tools, resources

    tools, resources = _run(go())
    assert tools == {
        # Slice A + B
        "search_thoughts",
        "list_thoughts",
        "thought_stats",
        "get_experience",
        "capture_thought",
        # Slice C — entity repair + recall
        "merge_entities",
        "rename_entity",
        "retract_claim",
        "split_entity",
        "unmerge_entity",
        "resolve_entity",
        "recall_recent",
        "who_was_at",
        "relationships_to",
        # Slice C — review / correction / disambiguation + update
        "review_queue",
        "propose_correction",
        "resolve_correction",
        "request_disambiguation",
        "resolve_disambiguation",
        "update_experience",
    }
    assert resources == {
        "brain://workflows",
        "brain://summary",
        "brain://entities/recent",
        "brain://reviews/pending",
    }


def test_workflows_resource_is_static_document():
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.read_resource("brain://workflows")

    contents = _run(go())
    doc = json.loads(contents[0].text)
    assert doc["schema_version"] == 1
    assert {w["name"] for w in doc["workflows"]} == {
        "capture_with_dedup",
        "research_topic",
        "correct_identity",
    }


def test_summary_resource_serializes_datetimes(monkeypatch):
    monkeypatch.setattr(
        mcp_reads,
        "summary_for_resource",
        lambda: {
            "experience_count": 1,
            "entity_count": 2,
            "claim_count": 3,
            "time_range": {
                "earliest": datetime(2026, 1, 1, tzinfo=UTC),
                "latest": datetime(2026, 6, 1, tzinfo=UTC),
            },
            "top_entities": [],
            "top_topics": [],
            "refreshed_at": datetime(2026, 6, 20, tzinfo=UTC),
        },
    )
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.read_resource("brain://summary")

    doc = json.loads(_run(go())[0].text)
    assert doc["time_range"]["earliest"] == "2026-01-01T00:00:00.000Z"
    assert doc["refreshed_at"].endswith("Z")


# --- Slice C wiring -----------------------------------------------------------


def test_merge_entities_serializes_result(monkeypatch):
    monkeypatch.setattr(
        entities,
        "merge_entities",
        lambda *a, **k: {
            "loser_id": "l",
            "winner_id": "w",
            "correction_event_id": "ce",
            "alias_appended": True,
        },
    )
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.call_tool(
                "merge_entities", {"loser_id": "l", "winner_id": "w"}
            )

    sc = _run(go()).structured_content
    assert sc["winner_id"] == "w"
    assert sc["alias_appended"] is True


def test_who_was_at_serializes_datetimes(monkeypatch):
    monkeypatch.setattr(
        recall,
        "who_was_at",
        lambda **k: {
            "resolved_via": "experience_id",
            "entities": [
                {
                    "entity_id": "e",
                    "canonical_name": "X",
                    "kind": "person",
                    "surface_form": "X",
                    "occurred_at": datetime(2026, 3, 14, 19, 0, tzinfo=UTC),
                }
            ],
        },
    )
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.call_tool("who_was_at", {"experience_id": "e"})

    sc = _run(go()).structured_content
    assert sc["entities"][0]["occurred_at"] == "2026-03-14T19:00:00.000Z"


def test_recall_recent_threads_viewer(monkeypatch):
    captured = {}

    def fake(viewer, query, days, **kw):
        captured["viewer"] = viewer
        captured["days"] = days
        return {"hits": []}

    monkeypatch.setattr(recall, "recall_recent", fake)

    class _Tok:
        claims = {"sub": "99"}

    monkeypatch.setattr(guards_mod, "get_access_token", lambda: _Tok())
    server = build_server()

    async def go():
        async with Client(server) as c:
            await c.call_tool("recall_recent", {"days": 5})

    _run(go())
    assert captured["viewer"] == "99"
    assert captured["days"] == 5


def test_update_experience_threads_viewer_and_dumps_patch(monkeypatch):
    captured = {}

    def fake(viewer, experience_id, patch, **kw):
        captured.update(viewer=viewer, eid=experience_id, patch=patch)
        return {
            "id": experience_id,
            "changed_fields": list(patch),
            "correction_event_id": "ce",
        }

    monkeypatch.setattr(edits, "update_experience", fake)

    class _Tok:
        claims = {"sub": "owner-1"}

    monkeypatch.setattr(guards_mod, "get_access_token", lambda: _Tok())
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.call_tool(
                "update_experience", {"id": "x1", "patch": {"source_ref": "p"}}
            )

    sc = _run(go()).structured_content
    assert captured["viewer"] == "owner-1"
    assert captured["eid"] == "x1"
    # Patch is dumped exclude_unset: only the touched field crosses the boundary.
    assert captured["patch"] == {"source_ref": "p"}
    assert sc["changed_fields"] == ["source_ref"]


def test_request_disambiguation_dumps_option_models(monkeypatch):
    captured = {}

    def fake(question, options, **kw):
        captured["options"] = options
        return {
            "status": "awaiting_user_disambiguation",
            "token": "tok",
            "question": question,
            "options": options,
        }

    monkeypatch.setattr(reviews, "request_disambiguation", fake)
    server = build_server()

    async def go():
        async with Client(server) as c:
            await c.call_tool(
                "request_disambiguation",
                {
                    "question": "Which?",
                    "options": [{"label": "A"}, {"label": "B", "value": {"id": 2}}],
                },
            )

    _run(go())
    # Option models dumped to plain dicts; the bare label drops value (exclude_unset).
    assert captured["options"][0] == {"label": "A"}
    assert captured["options"][1] == {"label": "B", "value": {"id": 2}}


def test_resolve_disambiguation_dumps_object_choice(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        reviews,
        "resolve_disambiguation",
        lambda token, choice: (
            captured.update(choice=choice)
            or {"token": token, "resolved_choice": choice, "question": "Q"}
        ),
    )
    server = build_server()

    async def go():
        async with Client(server) as c:
            await c.call_tool(
                "resolve_disambiguation", {"token": "t", "choice": {"label": "A"}}
            )

    _run(go())
    assert captured["choice"] == {"label": "A"}


def test_resolve_disambiguation_passes_scalar_choice(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        reviews,
        "resolve_disambiguation",
        lambda token, choice: (
            captured.update(choice=choice)
            or {"token": token, "resolved_choice": {}, "question": "Q"}
        ),
    )
    server = build_server()

    async def go():
        async with Client(server) as c:
            await c.call_tool("resolve_disambiguation", {"token": "t", "choice": 1})

    _run(go())
    assert captured["choice"] == 1


def test_split_entity_error_is_sanitized(monkeypatch):
    def boom(*a, **k):
        raise ValueError("split boom")

    monkeypatch.setattr(entities, "split_entity", boom)
    server = build_server()

    async def go():
        async with Client(server) as c:
            await c.call_tool(
                "split_entity",
                {
                    "source_entity_id": "s",
                    "experience_ids": ["e"],
                    "into": {"canonical_name": "X"},
                },
            )

    with pytest.raises(ToolError) as exc:
        _run(go())
    assert "split boom" in str(exc.value)


def test_review_queue_serializes(monkeypatch):
    monkeypatch.setattr(
        reviews,
        "review_queue",
        lambda kind: {
            "merge_candidates": [],
            "low_confidence_claims": [],
            "contradictions": [],
            "disambiguations": [],
            "proposed_corrections": [],
        },
    )
    server = build_server()

    async def go():
        async with Client(server) as c:
            return await c.call_tool("review_queue", {})

    sc = _run(go()).structured_content
    assert sc["merge_candidates"] == []
