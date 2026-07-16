# ABOUTME: Unit tests for the consolidation worker's retry decision (no DB).
# ABOUTME: decide_after_failure is a pure function, tested exhaustively here.

from openbrain.brain.consolidation import (
    MAX_CONSOLIDATION_ATTEMPTS,
    decide_after_failure,
)


def test_keeps_pending_while_retries_remain():
    assert decide_after_failure(1) == "pending"
    assert decide_after_failure(2) == "pending"


def test_fails_once_budget_exhausted():
    assert decide_after_failure(MAX_CONSOLIDATION_ATTEMPTS) == "failed"
    assert decide_after_failure(MAX_CONSOLIDATION_ATTEMPTS + 1) == "failed"


def test_respects_custom_cap():
    assert decide_after_failure(1, 2) == "pending"
    assert decide_after_failure(2, 2) == "failed"
    assert decide_after_failure(3, 2) == "failed"


def test_cap_matches_sql_proc():
    # If this drifts from c_max_attempts in init/05-consolidation.sql, the SQL
    # eligibility check and the Python terminal decision disagree, and rows end
    # up either over-retried or prematurely failed.
    assert MAX_CONSOLIDATION_ATTEMPTS == 3
