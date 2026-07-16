# ABOUTME: Read services backing the MCP read tools + resources.
# ABOUTME: hybrid_search, list_thoughts, thought_stats, and the resource queries.
import json
from datetime import UTC, datetime, timedelta

from openbrain.brain.db import (
    brain_cursor,
    dictfetchall,
    parse_json,
    to_vector_literal,
)
from openbrain.brain.embeddings import embed_query
from openbrain.brain.services.reads import get_summary

# ---------------------------------------------------------------------------
# hybrid_search
# ---------------------------------------------------------------------------

_HYBRID_SQL = """
    select id::text as id,
           content,
           metadata,
           captured_at,
           occurred_at,
           vec_score,
           lex_score,
           fused_score
      from brain.match_brain_hybrid(%s, %s::vector, %s, %s::jsonb, %s, %s::uuid[], %s::text)
"""

_PERSON_IDS_SQL = (
    "select brain.experience_ids_mentioning_name("
    "%s, 'person'::brain.entity_kind) as ids"
)
_TOPIC_IDS_SQL = (
    "select brain.experience_ids_mentioning_name("
    "%s, 'concept'::brain.entity_kind) as ids"
)

_PROVENANCE_SQL = """
    select cs.experience_id::text as experience_id,
           c.id::text             as claim_id,
           c.predicate            as predicate,
           c.predicate_detail     as predicate_detail,
           c.object_literal       as object_literal,
           c.polarity::text       as polarity,
           c.confidence           as confidence,
           c.superseded_by::text  as superseded_by,
           cs.support_kind::text  as support_kind,
           cs.source_confidence   as source_confidence,
           cs.extracted_by        as extracted_by
      from brain.claim_sources cs
      join brain.claims c on c.id = cs.claim_id
     where cs.experience_id = any(%s::uuid[])
       and (%s or c.polarity <> 'retracted')
       and (c.confidence is null or c.confidence >= %s)
     order by c.confidence desc nulls last, c.created_at
"""

# Provenance field order matches schemas.ProvenanceClaim (experience_id
# is the grouping key and is dropped from each claim).
_PROVENANCE_FIELDS = (
    "claim_id",
    "predicate",
    "predicate_detail",
    "object_literal",
    "polarity",
    "confidence",
    "support_kind",
    "source_confidence",
    "extracted_by",
    "superseded_by",
)


def intersect_id_sets(sets: list[list[str]]) -> list[str]:
    """Intersect experience-id sets, preserving the first set's order.

    In-memory so the person/topic filters compose: an experience must
    satisfy every entity filter independently.
    """
    if not sets:
        return []
    if len(sets) == 1:
        return list(sets[0])
    first, *rest = sets
    rest_sets = [set(s) for s in rest]
    return [eid for eid in first if all(eid in rs for rs in rest_sets)]


def group_provenance(prov_rows: list[dict]) -> dict[str, list[dict]]:
    """Group raw provenance rows by experience_id, dropping that key per claim."""
    by_experience: dict[str, list[dict]] = {}
    for row in prov_rows:
        claim = {field: row[field] for field in _PROVENANCE_FIELDS}
        if claim["confidence"] is not None:
            claim["confidence"] = float(claim["confidence"])
        if claim["source_confidence"] is not None:
            claim["source_confidence"] = float(claim["source_confidence"])
        by_experience.setdefault(row["experience_id"], []).append(claim)
    return by_experience


def _resolve_experience_ids(cursor, person, topic, caller_ids) -> list[str] | None:
    if not person and not topic and not caller_ids:
        return None
    sets: list[list[str]] = []
    if person:
        cursor.execute(_PERSON_IDS_SQL, [person])
        sets.append(cursor.fetchone()[0] or [])
    if topic:
        cursor.execute(_TOPIC_IDS_SQL, [topic])
        sets.append(cursor.fetchone()[0] or [])
    if caller_ids:
        sets.append(list(caller_ids))
    if not sets:
        return None
    return intersect_id_sets(sets)


def hybrid_search(
    viewer_sub: str | None,
    query: str,
    *,
    limit: int = 10,
    threshold: float = 0,
    person: str | None = None,
    topic: str | None = None,
    experience_ids: list[str] | None = None,
    metadata_filter: dict | None = None,
    with_provenance: bool = False,
    min_confidence: float = 0,
    include_retracted: bool = False,
) -> list[dict]:
    """Hybrid RRF search with the full shipped contract (person/topic/provenance).

    Embeds the query first so an EmbeddingError raises before any DB work. When
    with_provenance is set, every hit gets a provenance list (possibly empty);
    otherwise the key is omitted entirely (see schemas.SearchHit).
    """
    embedding = embed_query(query)
    with brain_cursor() as cursor:
        resolved = _resolve_experience_ids(cursor, person, topic, experience_ids)
        # A person/topic filter that matched no entities -> empty result; never
        # pass [] to the SQL function (it would mean "no allowlist").
        if resolved is not None and len(resolved) == 0:
            return []

        cursor.execute(
            _HYBRID_SQL,
            [
                query,
                to_vector_literal(embedding),
                limit,
                json.dumps(metadata_filter or {}),
                threshold,
                resolved,
                viewer_sub,
            ],
        )
        rows = dictfetchall(cursor)
        for row in rows:
            row["metadata"] = parse_json(row["metadata"])
            row["vec_score"] = float(row["vec_score"])
            row["lex_score"] = float(row["lex_score"])
            row["fused_score"] = float(row["fused_score"])

        if not with_provenance or not rows:
            return rows

        cursor.execute(
            _PROVENANCE_SQL,
            [[r["id"] for r in rows], include_retracted, min_confidence],
        )
        by_experience = group_provenance(dictfetchall(cursor))

    for row in rows:
        row["provenance"] = by_experience.get(row["id"], [])
    return rows


# ---------------------------------------------------------------------------
# list_thoughts
# ---------------------------------------------------------------------------


def list_thoughts(
    viewer_sub: str | None,
    *,
    limit: int = 10,
    type: str | None = None,
    topic: str | None = None,
    person: str | None = None,
    days: int | None = None,
) -> list[dict]:
    """Recency-ordered metadata-filtered listing, viewer-scoped (own + shared)."""
    where: list[str] = []
    params: list = []

    if type:
        params.append(json.dumps({"type": type}))
        where.append("metadata @> %s::jsonb")
    if topic:
        params.append(json.dumps({"topics": [topic]}))
        where.append("metadata @> %s::jsonb")
    if person:
        params.append(json.dumps({"people": [person]}))
        where.append("metadata @> %s::jsonb")
    if days:
        since = datetime.now(UTC) - timedelta(days=days)
        params.append(since)
        where.append("captured_at >= %s")

    # Viewer filter (#83): own + shared only; a null viewer bypasses the filter.
    params.append(viewer_sub)
    where.append("(%s::text is null or owner = %s or visibility = 'shared')")
    params.append(viewer_sub)

    params.append(limit)
    sql = f"""select content, metadata, captured_at as created_at
                from brain.experiences
               where {" and ".join(where)}
               order by captured_at desc
               limit %s"""

    with brain_cursor() as cursor:
        cursor.execute(sql, params)
        rows = dictfetchall(cursor)
    for row in rows:
        row["metadata"] = parse_json(row["metadata"]) or {}
    return rows


# ---------------------------------------------------------------------------
# thought_stats
# ---------------------------------------------------------------------------


def _top(counts: dict[str, int]) -> list[dict]:
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    return [{"name": name, "count": count} for name, count in ranked]


def aggregate_stats(rows: list[dict]) -> dict:
    """Aggregate type/topic/people histograms + date range from thought rows.

    rows are ordered captured_at desc (newest first), each carrying `metadata`
    (dict) and `created_at` (datetime); aggregation happens here, not in SQL.
    """
    types: dict[str, int] = {}
    topics: dict[str, int] = {}
    people: dict[str, int] = {}
    for row in rows:
        meta = row.get("metadata") or {}
        if meta.get("type"):
            types[meta["type"]] = types.get(meta["type"], 0) + 1
        if isinstance(meta.get("topics"), list):
            for topic in meta["topics"]:
                topics[topic] = topics.get(topic, 0) + 1
        if isinstance(meta.get("people"), list):
            for person in meta["people"]:
                people[person] = people.get(person, 0) + 1

    date_range = None
    if rows:
        date_range = {"first": rows[-1]["created_at"], "last": rows[0]["created_at"]}

    return {
        "total": len(rows),
        "date_range": date_range,
        "types": types,
        "top_topics": _top(topics),
        "top_people": _top(people),
    }


_STATS_SQL = "select metadata, created_at from thoughts order by created_at desc"


def thought_stats() -> dict:
    """Global corpus stats (deliberately NOT viewer-scoped)."""
    with brain_cursor() as cursor:
        cursor.execute(_STATS_SQL)
        rows = dictfetchall(cursor)
    for row in rows:
        row["metadata"] = parse_json(row["metadata"]) or {}
    return aggregate_stats(rows)


# ---------------------------------------------------------------------------
# Resource-backing reads
# ---------------------------------------------------------------------------


class SummaryCacheEmpty(RuntimeError):
    """brain.summary_cache has no row yet (run brain.refresh_summary_cache())."""


def summary_for_resource() -> dict:
    """Reshape the summary cache into the brain://summary resource shape."""
    summary = get_summary()
    if summary is None:
        raise SummaryCacheEmpty(
            "brain.summary_cache is empty — run brain.refresh_summary_cache()"
        )
    return {
        "experience_count": summary["experience_count"],
        "entity_count": summary["entity_count"],
        "claim_count": summary["claim_count"],
        "time_range": {
            "earliest": summary["time_range_earliest"],
            "latest": summary["time_range_latest"],
        },
        "top_entities": summary["top_entities"],
        "top_topics": summary["top_topics"],
        "refreshed_at": summary["refreshed_at"],
    }


_RECENT_ENTITIES_SQL = """
    select id::text,
           kind::text        as kind,
           canonical_name,
           aliases,
           merged_into::text as merged_into,
           created_at
      from brain.entities
     where created_at > now() - (%s::int * interval '1 day')
     order by created_at desc
     limit 200
"""


def recent_entities(window_days: int = 30) -> dict:
    """Entities created/merged in the last window_days, newest first."""
    with brain_cursor() as cursor:
        cursor.execute(_RECENT_ENTITIES_SQL, [window_days])
        rows = dictfetchall(cursor)
    for row in rows:
        row["aliases"] = row["aliases"] or []
    return {"window_days": window_days, "entities": rows}


_PENDING_REVIEWS_SQL = """
    select
      (select count(*) from brain.merge_candidates
        where status = 'pending')::int as merge_candidates,
      (select count(*) from brain.claims c
         join brain.claim_sources cs on cs.claim_id = c.id
        where c.polarity <> 'retracted'
          and cs.support_kind = 'inferred'
          and c.confidence < 0.6)::int as low_confidence_claims,
      (select count(*) from brain.claims
        where superseded_by is not null
          and polarity <> 'retracted')::int as contradictions,
      (select count(*) from brain.disambiguations
        where status = 'pending')::int as disambiguations,
      (select count(*) from brain.proposed_corrections
        where status = 'pending')::int as proposed_corrections
"""


def pending_reviews() -> dict:
    """Counts awaiting human review across the five review_queue surfaces."""
    with brain_cursor() as cursor:
        cursor.execute(_PENDING_REVIEWS_SQL)
        counts = dictfetchall(cursor)[0]
    counts = {key: int(value) for key, value in counts.items()}
    counts["total"] = sum(counts.values())
    return counts
