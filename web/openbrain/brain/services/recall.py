# ABOUTME: Recall read services
# ABOUTME: (recall_recent, who_was_at, relationships_to). Read-only, viewer-scoped.
"""Recall services for the MCP server (issue #120, Slice C).

recall_recent applies a recency window then hybrid search inside it (reusing the
shared hybrid_search so there is one ranking path); who_was_at resolves the
entities of one experience or one calendar date; relationships_to walks the
claim graph via brain.relationships_to. All read-only.
"""

from openbrain.brain.db import brain_cursor, dictfetchall, parse_json
from openbrain.brain.services.mcp_reads import hybrid_search


def recall_recent(
    viewer_sub: str | None,
    query: str | None,
    days: int,
    *,
    source_kind: str | None = None,
    limit: int = 20,
) -> dict:
    """Recency-windowed recall: last N days, then hybrid search inside the window.

    Gathers the viewer's own + shared experience ids in the window (a null viewer
    bypasses the filter), then either lists them by recency (no query) or runs
    hybrid search restricted to that allowlist.
    """
    if days <= 0:
        raise ValueError("recall_recent: days must be > 0")

    where = ["captured_at >= now() - %s::interval"]
    params: list = [f"{days} days"]
    if source_kind:
        where.append("source_kind = %s::brain.source_kind")
        params.append(source_kind)
    # Viewer filter (#83): own + shared only; a null viewer bypasses the filter.
    where.append("(%s::text is null or owner = %s or visibility = 'shared')")
    params.extend([viewer_sub, viewer_sub])

    with brain_cursor() as cursor:
        cursor.execute(
            "select id::text from brain.experiences where "
            + " and ".join(where)
            + " order by captured_at desc limit 500",
            params,
        )
        ids = [row["id"] for row in dictfetchall(cursor)]

    if not ids:
        return {"hits": []}

    if not query or not query.strip():
        with brain_cursor() as cursor:
            cursor.execute(
                "select id::text, content, metadata, captured_at, occurred_at "
                "from brain.experiences where id = any(%s::uuid[]) "
                "order by captured_at desc limit %s",
                [ids, limit],
            )
            rows = dictfetchall(cursor)
        return {
            "hits": [
                {
                    "id": row["id"],
                    "content": row["content"],
                    "metadata": parse_json(row["metadata"]),
                    "captured_at": row["captured_at"],
                    "occurred_at": row["occurred_at"],
                    "vec_score": 0,
                    "lex_score": 0,
                    "fused_score": 0,
                }
                for row in rows
            ]
        }

    # Reuse hybrid_search's allowlist plumbing — recall_recent is "hybrid search
    # restricted to the last N days."
    hits = hybrid_search(viewer_sub, query, limit=limit, experience_ids=ids)
    return {"hits": hits}


def who_was_at(*, experience_id: str | None = None, date: str | None = None) -> dict:
    """Resolve the entities mentioned at one experience or on one calendar date."""
    if not experience_id and not date:
        raise ValueError("who_was_at: must supply experience_id or date")

    if experience_id:
        sql = """
            select e.id::text          as entity_id,
                   e.canonical_name    as canonical_name,
                   e.kind::text        as kind,
                   m.surface_form      as surface_form,
                   ex.occurred_at      as occurred_at
              from brain.mentions m
              join brain.entities e on e.id = m.entity_id
              join brain.experiences ex on ex.id = m.experience_id
             where m.experience_id = %s::uuid
             order by m.field, e.canonical_name
        """
        params = [experience_id]
        resolved_via = "experience_id"
    else:
        # Match occurred_at::date OR captured_at::date as a fallback so dates land
        # for experiences whose occurred_at was never populated.
        sql = """
            select distinct on (e.id)
                   e.id::text          as entity_id,
                   e.canonical_name    as canonical_name,
                   e.kind::text        as kind,
                   m.surface_form      as surface_form,
                   ex.occurred_at      as occurred_at
              from brain.mentions m
              join brain.entities e on e.id = m.entity_id
              join brain.experiences ex on ex.id = m.experience_id
             where (ex.occurred_at::date = %s::date)
                or (ex.occurred_at is null and ex.captured_at::date = %s::date)
             order by e.id, m.field, e.canonical_name
        """
        params = [date, date]
        resolved_via = "date"

    with brain_cursor() as cursor:
        cursor.execute(sql, params)
        entities = dictfetchall(cursor)

    return {"resolved_via": resolved_via, "entities": entities}


def relationships_to(
    entity_id: str, *, max_hops: int = 2, min_confidence: float = 0.0
) -> dict:
    """Recursive walk over non-retracted entity-to-entity claims (BFS hop count).

    confidence is the product of edge confidences along each surviving path;
    paths whose running confidence drops below min_confidence are pruned
    (min_confidence 0 = no floor, the back-compatible default).
    """
    if max_hops < 1 or max_hops > 6:
        raise ValueError("relationships_to: max_hops must be between 1 and 6")
    if not 0 <= min_confidence <= 1:
        raise ValueError("relationships_to: min_confidence must be between 0 and 1")

    with brain_cursor() as cursor:
        cursor.execute(
            """
            select r.entity_id::text   as entity_id,
                   e.canonical_name    as canonical_name,
                   e.kind::text        as kind,
                   r.hops              as hops,
                   r.confidence        as confidence
              from brain.relationships_to(%s::uuid, %s::int, %s::real) r
              join brain.entities e on e.id = r.entity_id
             order by r.hops, e.canonical_name
            """,
            [entity_id, max_hops, min_confidence],
        )
        related = dictfetchall(cursor)
        for row in related:
            row["confidence"] = float(row["confidence"])

    return {"seed_entity_id": entity_id, "related": related}
