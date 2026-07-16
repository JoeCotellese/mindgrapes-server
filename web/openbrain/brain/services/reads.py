"""Read-side queries for the Brain UI.

Each function backs one Brain UI view (#101)
and returns plain dicts for the templates. Viewer filtering and the
identical-404 privacy rule live here; claims stay
unfiltered — a documented soft-privacy leak.
"""

from django.utils import timezone

from openbrain.brain.auth import can_edit_visibility, can_viewer_read
from openbrain.brain.db import (
    brain_cursor,
    dictfetchall,
    parse_json,
    to_vector_literal,
)
from openbrain.brain.embeddings import embed_query
from openbrain.brain.excerpts import format_excerpt

_SUMMARY_SQL = """
    select experience_count,
           entity_count,
           claim_count,
           time_range_earliest,
           time_range_latest,
           top_entities,
           top_topics,
           refreshed_at
      from brain.summary_cache
"""


def get_summary() -> dict | None:
    """The dashboard summary row, or None when the cache has no row (US-1)."""
    with brain_cursor() as cursor:
        cursor.execute(_SUMMARY_SQL)
        rows = dictfetchall(cursor)
    if not rows:
        return None
    summary = rows[0]
    summary["top_entities"] = parse_json(summary["top_entities"])
    summary["top_topics"] = parse_json(summary["top_topics"])
    return summary


# The row resolves even when superseded or deleted so a deep-link to an
# audit-trail entry still loads; the template surfaces a lifecycle banner.
_EXPERIENCE_SQL = """
    select id::text,
           content,
           captured_at,
           occurred_at,
           occurred_window::text      as occurred_window,
           source_kind::text          as source_kind,
           source_ref,
           metadata,
           consolidation_status::text as consolidation_status,
           superseded_by::text        as superseded_by,
           deleted_at,
           owner,
           visibility::text           as visibility
      from brain.experiences
     where id = %s::uuid
"""

_EXPERIENCE_MENTIONS_SQL = """
    select e.id::text          as entity_id,
           e.canonical_name    as canonical_name,
           e.kind::text        as kind,
           m.surface_form      as surface_form,
           e.merged_into::text as merged_into
      from brain.mentions m
      join brain.entities e on e.id = m.entity_id
     where m.experience_id = %s::uuid
     order by m.field, e.canonical_name
"""

_EXPERIENCE_CLAIMS_SQL = """
    select c.id::text             as claim_id,
           c.predicate            as predicate,
           c.predicate_detail     as predicate_detail,
           c.polarity::text       as polarity,
           c.confidence           as confidence,
           cs.support_kind::text  as support_kind,
           cs.source_confidence   as source_confidence,
           cs.extracted_by        as extracted_by,
           json_build_object(
             'id', sub.id::text,
             'canonical_name', sub.canonical_name,
             'kind', sub.kind::text
           ) as subject,
           json_build_object(
             'id', obj.id::text,
             'canonical_name', obj.canonical_name,
             'kind', obj.kind::text,
             'literal', c.object_literal
           ) as object
      from brain.claims c
      join brain.claim_sources cs
        on cs.claim_id = c.id and cs.experience_id = %s::uuid
      join brain.entities sub on sub.id = c.subject_id
      left join brain.entities obj on obj.id = c.object_entity_id
     order by c.confidence desc nulls last, c.created_at
"""


def get_experience_detail(viewer: str, experience_id: str) -> dict | None:
    """One experience with its mentions and claims-sourced-here (US-3).

    Returns None when the row is missing OR private-and-not-the-viewer's, so a
    private id is an identical 404 to a missing one. Lifecycle (superseded /
    deleted) is NOT gated — the audit row stays reachable, flagged is_live.
    """
    with brain_cursor() as cursor:
        cursor.execute(_EXPERIENCE_SQL, [experience_id])
        rows = dictfetchall(cursor)
        if not rows:
            return None
        experience = rows[0]
        if not can_viewer_read(viewer, experience["owner"], experience["visibility"]):
            return None
        experience["metadata"] = parse_json(experience["metadata"])
        experience["is_live"] = (
            experience["superseded_by"] is None and experience["deleted_at"] is None
        )
        experience["can_change_visibility"] = can_edit_visibility(
            viewer, experience["owner"]
        )

        cursor.execute(_EXPERIENCE_MENTIONS_SQL, [experience_id])
        mentions = dictfetchall(cursor)

        cursor.execute(_EXPERIENCE_CLAIMS_SQL, [experience_id])
        claims = dictfetchall(cursor)

    return {
        "experience": experience,
        "mentions": mentions,
        "claims_sourced_here": claims,
    }


_ENTITY_SQL = """
    select id::text,
           kind::text        as kind,
           canonical_name,
           aliases,
           confidence,
           metadata,
           merged_into::text as merged_into,
           created_at
      from brain.entities
     where id = %s::uuid
"""

_ENTITY_MERGE_AUDIT_SQL = """
    select id::text as correction_event_id,
           before,
           after,
           reason,
           created_at,
           created_by
      from brain.correction_events
     where target_kind = 'entity' and target_id = %s::uuid
     order by created_at asc
"""

_ENTITY_WINNER_SQL = """
    select id::text, canonical_name, kind::text as kind
      from brain.entities
     where id = %s::uuid
"""

# mention_count and the timeline both exclude superseded/soft-deleted source
# experiences and apply the viewer filter (the timeline surfaces experience
# content). The audit trail stays queryable via experience detail; this view
# shows only live, readable rows.
_ENTITY_MENTION_COUNT_SQL = """
    select count(distinct m.experience_id)::int as count
      from brain.mentions m
      join brain.experiences ex on ex.id = m.experience_id
     where m.entity_id = %(id)s::uuid
       and ex.superseded_by is null
       and ex.deleted_at is null
       and (%(viewer)s::text is null or ex.owner = %(viewer)s
            or ex.visibility = 'shared')
"""

# Page by captured_at desc, deduping experiences via EXISTS (one row per
# experience even if it mentions the entity twice). The surface form shown is
# the lexicographically-first one for the entity in that experience — stable
# across runs.
_ENTITY_MENTIONS_SQL = """
    select ex.id::text     as experience_id,
           ex.captured_at  as captured_at,
           ex.occurred_at  as occurred_at,
           ex.content      as content,
           (select m.surface_form
              from brain.mentions m
             where m.experience_id = ex.id and m.entity_id = %(id)s::uuid
             order by m.surface_form
             limit 1)      as surface_form
      from brain.experiences ex
     where ex.superseded_by is null
       and ex.deleted_at is null
       and (%(viewer)s::text is null or ex.owner = %(viewer)s
            or ex.visibility = 'shared')
       and exists (
         select 1 from brain.mentions m
         where m.experience_id = ex.id and m.entity_id = %(id)s::uuid
       )
     order by ex.captured_at desc
     limit %(limit)s offset %(offset)s
"""


def _entity_claims_sql(column: str) -> str:
    # `column` is an internal literal ('subject_id' / 'object_entity_id'), never
    # user input; claims stay unfiltered (the documented soft-privacy leak).
    return f"""
        select c.id::text         as claim_id,
               c.predicate        as predicate,
               c.predicate_detail as predicate_detail,
               c.polarity::text   as polarity,
               c.confidence       as confidence,
               json_build_object(
                 'id', sub.id::text,
                 'canonical_name', sub.canonical_name,
                 'kind', sub.kind::text
               ) as subject,
               json_build_object(
                 'id', obj.id::text,
                 'canonical_name', obj.canonical_name,
                 'kind', obj.kind::text,
                 'literal', c.object_literal
               ) as object,
               (select count(*)::int from brain.claim_sources cs
                 where cs.claim_id = c.id) as source_count
          from brain.claims c
          join brain.entities sub on sub.id = c.subject_id
          left join brain.entities obj on obj.id = c.object_entity_id
         where c.{column} = %s::uuid
         order by c.confidence desc nulls last, c.created_at
    """


def _format_mention_rows(rows: list[dict]) -> list[dict]:
    return [
        {
            "experience_id": row["experience_id"],
            "captured_at": row["captured_at"],
            "occurred_at": row["occurred_at"],
            "surface_form": row["surface_form"],
            "content_excerpt": format_excerpt(row["content"], row["surface_form"]),
        }
        for row in rows
    ]


def _entity_mentions_page(cursor, viewer, entity_id, limit, offset) -> dict:
    params = {"id": entity_id, "viewer": viewer, "limit": limit, "offset": offset}
    cursor.execute(_ENTITY_MENTION_COUNT_SQL, params)
    mention_count = cursor.fetchone()[0]
    cursor.execute(_ENTITY_MENTIONS_SQL, params)
    mentions = _format_mention_rows(dictfetchall(cursor))
    next_offset = offset + len(mentions)
    return {
        "mentions": mentions,
        "mention_count": mention_count,
        "next_offset": next_offset,
        "has_more": next_offset < mention_count,
    }


def get_entity_mentions(viewer: str, entity_id: str, limit: int, offset: int) -> dict:
    """One page of an entity's live, viewer-filtered mention timeline (US-4)."""
    with brain_cursor() as cursor:
        return _entity_mentions_page(cursor, viewer, entity_id, limit, offset)


def get_entity_detail(
    viewer: str, entity_id: str, limit: int, offset: int
) -> dict | None:
    """An entity with its mention timeline and claims (US-4).

    Returns None when missing. A merged entity returns a redirect shape
    (is_merged, merged_into, merge_audit, winner); a canonical entity returns
    its first mentions page plus claims-as-subject and claims-as-object.
    """
    with brain_cursor() as cursor:
        cursor.execute(_ENTITY_SQL, [entity_id])
        rows = dictfetchall(cursor)
        if not rows:
            return None
        entity = rows[0]

        if entity["merged_into"]:
            cursor.execute(_ENTITY_MERGE_AUDIT_SQL, [entity_id])
            merge_audit = dictfetchall(cursor)
            for event in merge_audit:
                event["before"] = parse_json(event["before"])
                event["after"] = parse_json(event["after"])
            cursor.execute(_ENTITY_WINNER_SQL, [entity["merged_into"]])
            winners = dictfetchall(cursor)
            return {
                "is_merged": True,
                "merged_into": entity["merged_into"],
                "merge_audit": merge_audit,
                "winner": winners[0] if winners else None,
            }

        entity["metadata"] = parse_json(entity["metadata"])
        page = _entity_mentions_page(cursor, viewer, entity_id, limit, offset)

        cursor.execute(_entity_claims_sql("subject_id"), [entity_id])
        claims_as_subject = dictfetchall(cursor)
        cursor.execute(_entity_claims_sql("object_entity_id"), [entity_id])
        claims_as_object = dictfetchall(cursor)

    return {
        "is_merged": False,
        "entity": entity,
        "claims_as_subject": claims_as_subject,
        "claims_as_object": claims_as_object,
        **page,
    }


# p_viewer is the named 7th arg of match_brain_hybrid (the viewer-filter delta
# migration); the positional args keep their defaults. Scopes results to the
# member's own + shared experiences, never the all-seeing null viewer.
_SEARCH_SQL = """
    with hits as (
      select *
        from brain.match_brain_hybrid(%s::text, %s::vector, %s::int,
                                      p_viewer => %s::text)
    )
    select h.id::text,
           h.content,
           h.metadata,
           h.captured_at,
           h.occurred_at,
           h.vec_score,
           h.lex_score,
           h.fused_score,
           coalesce((
             select json_agg(json_build_object(
                      'id', e.id::text,
                      'canonical_name', e.canonical_name,
                      'kind', e.kind::text
                    ) order by e.canonical_name)
               from (select distinct entity_id
                       from brain.mentions
                      where experience_id = h.id) m
               join brain.entities e on e.id = m.entity_id
              where e.merged_into is null
           ), '[]'::json) as mentioned_entities,
           (select count(*)::int
              from brain.claim_sources cs
             where cs.experience_id = h.id) as claim_count
      from hits h
"""


def search_experiences(viewer: str, query: str, limit: int) -> list[dict]:
    """Hybrid RRF search scoped to the viewer's own + shared experiences (US-2).

    Embeds the query first (outside the cursor) so an embedding failure raises
    EmbeddingError before any DB work; each hit carries vec/lex/fused scores,
    its mentioned entities, and a claim count.
    """
    embedding = embed_query(query)
    with brain_cursor() as cursor:
        cursor.execute(
            _SEARCH_SQL,
            [query, to_vector_literal(embedding), limit, viewer],
        )
        results = dictfetchall(cursor)
    for result in results:
        result["metadata"] = parse_json(result["metadata"])
    return results


# Entities ranked by their most-recent mention within the window, viewer-scoped
# (own + shared) and live-only — so a private-not-mine entity never surfaces.
# Recency keys off the mentioning experience's captured_at (mentions have no
# timestamp of their own), the same signal the entity mention timeline and the
# feed use. Distinct from mcp_reads.recent_entities, which keys off entity
# creation/merge and is not viewer-scoped. Merged entities are excluded so a
# duplicate doesn't show, matching the search/feed entity clouds.
_RECENTLY_ACTIVE_SQL = """
    select e.id::text          as id,
           e.canonical_name    as canonical_name,
           e.kind::text        as kind,
           max(ex.captured_at) as last_mentioned_at
      from brain.mentions m
      join brain.entities e     on e.id = m.entity_id
      join brain.experiences ex on ex.id = m.experience_id
     where e.merged_into is null
       and ex.superseded_by is null
       and ex.deleted_at is null
       and ex.captured_at > now() - (%(window_days)s::int * interval '1 day')
       and (%(viewer)s::text is null or ex.owner = %(viewer)s
            or ex.visibility = 'shared')
     group by e.id, e.canonical_name, e.kind
     order by last_mentioned_at desc
     limit %(limit)s
"""


def _recency(when, now) -> str:
    """Compact relative recency for a mention timestamp ("now"/"5m"/"3h"/"2d").

    Future timestamps (clock skew / far-future test seeds) read as "now".
    """
    secs = (now - when).total_seconds()
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def recently_active_entities(
    viewer, window_days: int = 30, limit: int = 20
) -> list[dict]:
    """Entities ranked by most-recent mention within window_days, newest first (#138).

    Viewer-scoped (own + shared) and live-only, so a private-not-mine entity never
    surfaces — distinct from mcp_reads.recent_entities (creation/merge, no viewer
    filter). Each row carries a compact `recency` label for the dashboard chip.
    """
    params = {"viewer": viewer, "window_days": window_days, "limit": limit}
    with brain_cursor() as cursor:
        cursor.execute(_RECENTLY_ACTIVE_SQL, params)
        rows = dictfetchall(cursor)
    now = timezone.now()
    for row in rows:
        row["recency"] = _recency(row["last_mentioned_at"], now)
    return rows
