# ABOUTME: Strips psycopg/driver internals from error
# ABOUTME: messages before they cross the MCP boundary; app errors pass verbatim.
import psycopg


def _find_db_error(err: object) -> psycopg.Error | None:
    """Find a psycopg error in the exception's explicit cause chain.

    Follows __cause__ only (the link Django's `raise ... from e` sets) — never
    the implicit __context__ — so an intentional app error raised while handling
    a DB error still passes through verbatim rather than collapsing to a sqlstate.
    """
    seen: set[int] = set()
    current = err
    while isinstance(current, BaseException) and id(current) not in seen:
        if isinstance(current, psycopg.Error):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


def sanitize_error(err: object) -> str:
    """Reduce database driver errors to their SQLSTATE; pass app errors verbatim.

    psycopg server errors quote table/column names, query fragments, and values,
    so surface only the sqlstate. Django re-raises driver errors as django.db.*
    types (IntegrityError etc.) whose original psycopg error is chained via
    `raise ... from e`, so check the cause chain too. Application errors
    (ValueError etc.) carry intentional diagnostics like "merge_entities:
    loser_id is already merged", so keep their message.
    """
    db_err = _find_db_error(err)
    if db_err is not None:
        code = getattr(db_err, "sqlstate", None) or "unknown"
        return f"database error (sqlstate={code})"
    if isinstance(err, BaseException):
        return str(err)
    return str(err)
