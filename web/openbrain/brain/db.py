"""Raw-SQL data-access seam to the brain.* schema (defined in init/03-brain.sql).

The Brain UI reads and writes brain.* with hand-written parameterized SQL via
Django's default connection (one Postgres, same role as the MCP service). There
are deliberately NO Django models for brain.* — that keeps makemigrations from
ever emitting a migration against a schema this app does not own.

Local dev and unit tests run on sqlite, where brain.* does not exist;
brain_schema_present() lets views degrade gracefully there instead of erroring.
"""

import json
from contextlib import contextmanager

from django.db import connection

_schema_present_cache: bool | None = None


@contextmanager
def brain_cursor():
    """Yield a cursor on the default connection for brain.* queries."""
    with connection.cursor() as cursor:
        yield cursor


def dictfetchall(cursor) -> list[dict]:
    """Return all rows from a cursor as dicts keyed by column name."""
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def brain_schema_present() -> bool:
    """True when the connected database actually has the brain.* schema.

    Cached per-process. Returns False on any non-Postgres backend (sqlite in
    local dev / unit tests) without issuing Postgres-only SQL.
    """
    global _schema_present_cache
    if _schema_present_cache is not None:
        return _schema_present_cache
    if connection.vendor != "postgresql":
        _schema_present_cache = False
        return _schema_present_cache
    with connection.cursor() as cursor:
        cursor.execute("select to_regclass('brain.experiences') is not null")
        _schema_present_cache = bool(cursor.fetchone()[0])
    return _schema_present_cache


def parse_json(value):
    """Decode a jsonb column value, which this stack returns as text.

    psycopg parses `json` columns (json_build_object / json_agg) into Python
    objects automatically, but `jsonb` columns (e.g. metadata, the summary
    cache's top_entities) arrive as strings — callers expect parsed objects for
    both, so we decode here. Already-parsed values and None pass through unchanged.
    """
    return json.loads(value) if isinstance(value, str) else value


def to_vector_literal(embedding: list[float]) -> str:
    """Format a vector as a pgvector text literal for a ::vector cast.

    Hybrid search passes the query embedding
    to brain.match_brain_hybrid as this text literal.
    """
    return "[" + ",".join(map(str, embedding)) + "]"


_INSERT_CORRECTION_SQL = """
    insert into brain.correction_events (
        target_kind, target_id, before, after, reason, created_by
    ) values (
        %s::brain.target_kind, %s::uuid, %s::jsonb, %s::jsonb, %s, %s
    )
    returning id::text
"""


def record_correction(
    cursor,
    *,
    target_kind: str,
    target_id: str,
    before,
    after,
    reason: str,
    created_by: str,
) -> str | None:
    """Append one brain.correction_events row — the audit primitive for writes.

    Every experience/claim
    mutation lands at least one of these so the change is reconstructable. before
    and after are json-encoded (None becomes {} to keep the diff well-formed).
    Returns the new correction_events id so callers that echo it (the Slice C
    repair tools) can; callers that don't simply ignore it.
    """
    cursor.execute(
        _INSERT_CORRECTION_SQL,
        [
            target_kind,
            target_id,
            json.dumps(before or {}),
            json.dumps(after or {}),
            reason,
            created_by,
        ],
    )
    row = cursor.fetchone()
    return row[0] if row else None
