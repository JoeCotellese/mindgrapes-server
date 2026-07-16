# ABOUTME: Unit tests for sanitize_error.
# ABOUTME: DB errors reduce to sqlstate; application errors pass through verbatim.
import psycopg

from openbrain.mcp.errors import sanitize_error


def test_application_error_passes_through_verbatim():
    err = ValueError("merge_entities: loser_id is already merged")
    assert sanitize_error(err) == "merge_entities: loser_id is already merged"


def test_non_exception_is_stringified():
    assert sanitize_error("boom") == "boom"


def test_psycopg_error_reduced_to_sqlstate():
    err = psycopg.errors.UniqueViolation(
        'duplicate key value violates unique constraint "entities_pkey"'
    )
    out = sanitize_error(err)
    assert out.startswith("database error (sqlstate=")
    # Must not leak the constraint/table name that the driver message carried.
    assert "entities_pkey" not in out


def test_django_wrapped_db_error_reduced_to_sqlstate():
    # Raw cursor.execute surfaces driver errors as django.db.* types (not a
    # psycopg.Error) with the original chained via `raise ... from e`. The leak
    # must still be reduced to the sqlstate the psycopg cause carries (the #40
    # merge NOT-NULL case is exactly this shape).
    from django.db.utils import IntegrityError

    cause = psycopg.errors.NotNullViolation(
        'null value in column "kind" of relation "entities" violates not-null '
        "constraint\nDETAIL:  Failing row contains (secret, values)."
    )
    wrapped = IntegrityError("null value in column ...")
    wrapped.__cause__ = cause

    out = sanitize_error(wrapped)
    assert out == "database error (sqlstate=23502)"
    assert "DETAIL" not in out
    assert "secret" not in out


def test_app_error_raised_while_handling_db_error_passes_through():
    # An intentional ValueError raised inside an except block has the DB error as
    # its implicit __context__ but no explicit __cause__; its message must NOT be
    # swallowed into a generic sqlstate.
    try:
        try:
            raise psycopg.errors.NotNullViolation("driver detail")
        except psycopg.Error:
            # Intentionally no `raise ... from`: this reproduces implicit
            # __context__ chaining, which sanitize_error must NOT follow.
            raise ValueError("merge_entities: loser_id is already merged")  # noqa: B904
    except ValueError as err:
        assert sanitize_error(err) == "merge_entities: loser_id is already merged"
