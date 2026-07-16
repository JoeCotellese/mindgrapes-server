"""Shared test doubles for brain service unit tests (no Postgres).

StubCursor replays a queued list of (columns, rows) result sets, one per
execute() call, so a service that runs several queries on one cursor can be
driven entirely in-memory. dictfetchall reads .description + .fetchall(), both
of which StubCursor provides; rows are tuples in column order, like psycopg3.
"""

from contextlib import contextmanager


class StubCursor:
    def __init__(self, results):
        # results: list of (columns: list[str], rows: list[tuple]); one per execute().
        self._results = list(results)
        self.description = None
        self._rows = []
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        columns, rows = self._results.pop(0)
        self.description = [(c,) for c in columns]
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


@contextmanager
def _cursor_cm(cursor):
    yield cursor


def patch_brain_cursor(monkeypatch, cursor, module="openbrain.brain.services.reads"):
    """Point a service module's brain_cursor at a StubCursor."""
    monkeypatch.setattr(f"{module}.brain_cursor", lambda: _cursor_cm(cursor))
