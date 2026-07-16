# ABOUTME: Recent-memories feed read service — reverse-chronological live experiences.
# ABOUTME: Viewer-scoped (own + shared), with entity tags and source/visibility badges.
"""Read service backing /recent (issue #134).

One viewer-filtered query over brain.experiences, newest-first, with a deduped
entity-tag list per row (the json_agg subquery mirrors reads.py's search). It is
deliberately separate from recall_recent, which lacks the tags/visibility
projection and caps at 500 — see #134. Read-only; the privacy rule is the same
own + shared filter the rest of the brain UI uses.
"""

from datetime import timedelta

from django.utils import timezone

from openbrain.brain.db import brain_cursor, dictfetchall

SNIPPET_CHARS = 280

# Live (not superseded / deleted), viewer-scoped experiences, newest-first.
# entities: the deduped, merged-excluded json_agg subquery copied from reads.py
# _SEARCH_SQL — `json` type, so psycopg parses it; no parse_json needed.
# source: metadata->>'source' is the capturing tool (e.g. 'mcp'); source_kind is
# the processing role, not the tool, so it is deliberately not used here.
_RECENT_FEED_SQL = """
    select ex.id::text            as id,
           ex.content             as content,
           ex.captured_at         as captured_at,
           ex.metadata->>'source' as source,
           ex.visibility::text    as visibility,
           coalesce((
             select json_agg(json_build_object(
                      'id', e.id::text,
                      'canonical_name', e.canonical_name,
                      'kind', e.kind::text
                    ) order by e.canonical_name)
               from (select distinct entity_id
                       from brain.mentions
                      where experience_id = ex.id) m
               join brain.entities e on e.id = m.entity_id
              where e.merged_into is null
           ), '[]'::json) as entities
      from brain.experiences ex
     where ex.superseded_by is null
       and ex.deleted_at is null
       and (%(viewer)s::text is null or ex.owner = %(viewer)s
            or ex.visibility = 'shared')
     order by ex.captured_at desc
     limit %(limit)s offset %(offset)s
"""


def _snippet(content: str) -> str:
    """First ~SNIPPET_CHARS of content; ellipsis only when it was truncated."""
    text = (content or "").strip()
    if len(text) <= SNIPPET_CHARS:
        return text
    return text[:SNIPPET_CHARS].rstrip() + "…"


def _format_feed_row(row: dict) -> dict:
    return {
        "id": row["id"],
        "snippet": _snippet(row["content"]),
        "captured_at": row["captured_at"],
        "source": row["source"],
        "visibility": row["visibility"],
        "entities": row["entities"],
    }


def get_recent_feed(viewer: str | None, limit: int, offset: int) -> dict:
    """One page of live, viewer-scoped experiences, newest-first (#134).

    Asks for limit + 1 rows so a further page can be detected without a separate
    count query; the probe row is trimmed off and reported via has_more.
    """
    params = {"viewer": viewer, "limit": limit + 1, "offset": offset}
    with brain_cursor() as cursor:
        cursor.execute(_RECENT_FEED_SQL, params)
        rows = dictfetchall(cursor)
    has_more = len(rows) > limit
    experiences = [_format_feed_row(row) for row in rows[:limit]]
    return {
        "experiences": experiences,
        "next_offset": offset + len(experiences),
        "has_more": has_more,
    }


# Day-bucket order + labels for the timeline view (#135). Fixed order; empty
# buckets are dropped at render time, never reordered. Weeks start Monday (ISO).
_BUCKET_ORDER = (
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("this-week", "This week"),
    ("earlier", "Earlier"),
)


def _bucket_slug(captured_at, today):
    """Which day-bucket a row falls in, relative to today (configured tz).

    Future captures fold into Today (real captures are never future); a missing
    captured_at lands in Earlier defensively.
    """
    if captured_at is None:
        return "earlier"
    local_date = timezone.localtime(captured_at).date()
    if local_date >= today:
        return "today"
    if local_date == today - timedelta(days=1):
        return "yesterday"
    if local_date >= today - timedelta(days=today.weekday()):
        return "this-week"
    return "earlier"


def bucket_by_day(experiences, today):
    """Group feed rows into ordered day buckets, omitting empty ones (#135).

    A pure presentation transform over get_recent_feed's output; today is injected
    so it is independently testable. Returns a list of {"slug", "label", "rows"}
    in _BUCKET_ORDER, with each bucket's input order (newest-first) preserved.
    """
    by_slug = {}
    for exp in experiences:
        by_slug.setdefault(_bucket_slug(exp["captured_at"], today), []).append(exp)
    return [
        {"slug": slug, "label": label, "rows": by_slug[slug]}
        for slug, label in _BUCKET_ORDER
        if slug in by_slug
    ]
