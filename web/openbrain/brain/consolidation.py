# ABOUTME: pg_cron consolidation bridge (the run_consolidation worker process).
# ABOUTME: handle_notification runs extraction for one in_progress experience and writes claims.

import json
import logging
import time

import psycopg
from django.db import connections, transaction

from openbrain.brain.db import brain_cursor, dictfetchall
from openbrain.brain.extraction.claims import extract_claims
from openbrain.brain.services.claim_writer import (
    new_accumulator,
    write_claim_for_experience,
)

# Retry policy is split between SQL and Python by design:
#   - init/05-consolidation.sql owns the eligibility windows (5/30-min backoff,
#     15-min stale recovery) and increments consolidation_attempts before it
#     NOTIFYs, so by the time we see a row the failing attempt is already counted.
#   - This module owns only the terminal decision after an extraction fails:
#     leave the row 'pending' (a later cron tick retries it) or mark it 'failed'.
# MAX_CONSOLIDATION_ATTEMPTS must match c_max_attempts in the SQL proc.
CONSOLIDATION_CHANNEL = "brain_consolidate"
MAX_CONSOLIDATION_ATTEMPTS = 3
CONSOLIDATION_EXTRACTED_BY = "anthropic/claude-haiku-4.5-consolidation-v1"


def decide_after_failure(
    current_attempts: int, max_attempts: int = MAX_CONSOLIDATION_ATTEMPTS
) -> str:
    """Decide whether a failed extraction leaves the row 'pending' or 'failed'.

    The SQL proc has already incremented attempts before notifying, so
    ``current_attempts`` is the attempt that just failed. Returns 'failed' once
    the budget is exhausted, else 'pending' (the next eligible cron tick retries).
    """
    return "failed" if current_attempts >= max_attempts else "pending"


_logger = logging.getLogger(__name__)

_FETCH_INPROGRESS_SQL = """
    select id::text                 as id,
           content,
           captured_at,
           embedding::text          as embedding,
           consolidation_attempts   as attempts
      from brain.experiences
     where id = %s::uuid
       and consolidation_status = 'in_progress'
"""

_SET_STATUS_SQL = """
    update brain.experiences
       set consolidation_status = %s::brain.consolidation_status
     where id = %s::uuid
"""


def default_consolidation_extractor(*, experience_id, content, captured_at) -> dict:
    """Production extractor: Slice B's claim extractor; ignores experience_id.

    Injectable as a seam — tests pass their
    own callable so they never hit OpenRouter.
    """
    return extract_claims(content=content, captured_at=captured_at)


def _set_status(experience_id: str, status: str, logger) -> None:
    # Best-effort: runs OUTSIDE any rolled-
    # back write txn so the terminal status still lands even if the claim write
    # was what failed.
    try:
        with brain_cursor() as cursor:
            cursor.execute(_SET_STATUS_SQL, [status, experience_id])
    except Exception as err:  # noqa: BLE001 — last-ditch; we can only log.
        logger.error(
            "consolidation: %s also failed to record %s -> %s",
            experience_id,
            status,
            err,
        )


def handle_notification(
    experience_id: str,
    *,
    extract,
    max_attempts: int = MAX_CONSOLIDATION_ATTEMPTS,
    extracted_by: str = CONSOLIDATION_EXTRACTED_BY,
    logger=None,
) -> dict:
    """Handle one consolidation notification for ``experience_id``.

    Loads the in_progress row, runs ``extract`` against it, and on success writes
    its claims + claim_sources and flips the row to 'complete'. On extraction or
    write failure, applies decide_after_failure to leave the row 'pending' (a
    later cron tick retries) or 'failed'. Returns an outcome dict for the log.
    Safe to call directly from tests; the listen loop just fans ids into here.
    """
    logger = logger or _logger

    with brain_cursor() as cursor:
        cursor.execute(_FETCH_INPROGRESS_SQL, [experience_id])
        rows = dictfetchall(cursor)
    if not rows:
        # Lost the race to another worker (already complete/failed/pending) or
        # the SP rolled back. Either way: nothing to do.
        return {
            "experience_id": experience_id,
            "status": "skipped",
            "reason": "not_in_progress",
        }
    row = rows[0]
    attempts = row["attempts"]

    try:
        extracted = extract(
            experience_id=row["id"],
            content=row["content"],
            captured_at=row["captured_at"],
        )
    except Exception as err:  # noqa: BLE001 — any extractor failure is a retry.
        next_status = decide_after_failure(attempts, max_attempts)
        _set_status(experience_id, next_status, logger)
        logger.warning(
            "consolidation: %s attempt=%s extract failed -> %s (%s)",
            experience_id,
            attempts,
            next_status,
            err,
        )
        return {
            "experience_id": experience_id,
            "status": next_status,
            "attempts": attempts,
            "error": str(err),
        }

    acc = new_accumulator()
    try:
        with transaction.atomic(), brain_cursor() as cursor:
            for claim in extracted["claims"]:
                write_claim_for_experience(
                    cursor, row["id"], row["embedding"], claim, extracted_by, acc
                )
            cursor.execute(_SET_STATUS_SQL, ["complete", row["id"]])
    except Exception as err:  # noqa: BLE001 — a failed write is also a retry.
        next_status = decide_after_failure(attempts, max_attempts)
        _set_status(experience_id, next_status, logger)
        logger.error(
            "consolidation: %s attempt=%s write failed -> %s (%s)",
            experience_id,
            attempts,
            next_status,
            err,
        )
        return {
            "experience_id": experience_id,
            "status": next_status,
            "attempts": attempts,
            "error": str(err),
        }

    logger.info(
        "consolidation: %s attempt=%s complete claims=%s",
        experience_id,
        attempts,
        acc["claims_inserted"],
    )
    return {
        "experience_id": experience_id,
        "status": "complete",
        "attempts": attempts,
        "claims_inserted": acc["claims_inserted"],
        "claim_sources_inserted": acc["claim_sources_inserted"],
    }


def open_listen_connection() -> psycopg.Connection:
    """Open a DEDICATED autocommit psycopg connection for LISTEN.

    Not Django's connection and not pool-borrowed: a pool client can be returned
    to the pool, which silently drops the LISTEN subscription and its events. A
    standalone connection owns its lifetime. Built from the Django 'default'
    database config so it points at the same Postgres as the work connection.
    """
    cfg = connections["default"].settings_dict
    return psycopg.connect(
        dbname=cfg.get("NAME") or None,
        user=cfg.get("USER") or None,
        password=cfg.get("PASSWORD") or None,
        host=cfg.get("HOST") or None,
        port=str(cfg["PORT"]) if cfg.get("PORT") else None,
        autocommit=True,
    )


def _parse_payload(payload, logger) -> str | None:
    """Extract experience_id from a NOTIFY payload; None (with a warning) if bad."""
    try:
        data = json.loads(payload or "{}")
    except (TypeError, ValueError) as err:
        logger.warning(
            "consolidation: malformed payload on %s: %s", CONSOLIDATION_CHANNEL, err
        )
        return None
    experience_id = data.get("experience_id") if isinstance(data, dict) else None
    if not isinstance(experience_id, str) or not experience_id:
        logger.warning("consolidation: payload missing experience_id")
        return None
    return experience_id


def _drain_notifications(conn, handle, should_stop, logger) -> None:
    """Process one ~1s batch of notifications, then return so the caller can
    re-check should_stop(). Sequential: each id is fully handled before the next.
    """
    for note in conn.notifies(timeout=1.0):
        experience_id = _parse_payload(note.payload, logger)
        if experience_id is not None:
            try:
                handle(experience_id)
            except Exception:  # noqa: BLE001 — one bad id can't kill the loop.
                logger.exception(
                    "consolidation: unhandled handler error for %s", experience_id
                )
        if should_stop():
            return


def run_consolidation_listener(
    *,
    handle,
    should_stop,
    on_ready=None,
    logger=None,
    reconnect_delay: float = 1.0,
) -> None:
    """LISTEN on brain_consolidate and dispatch each id to ``handle``.

    Reconnect loop: on any psycopg error (connection drop, DB restart) it logs,
    backs off ``reconnect_delay`` seconds, and reconnects — nothing queued is lost
    because the SQL cron tick re-NOTIFYs still-pending rows and reclaims stale
    in_progress rows. ``on_ready`` fires once per successful LISTEN (tests wait on
    it before NOTIFYing, since NOTIFY only reaches sessions already listening).

    Processing is SEQUENTIAL — one id fully handled before the next. A slow
    extraction blocks the next id, but psycopg buffers inbound NOTIFYs and the
    15-min stale-recovery window re-NOTIFYs anything we fall behind on, so at
    personal-brain volume nothing is dropped. A synchronous loop was chosen over
    fire-and-forget concurrency to match the rest of
    the service layer.
    """
    logger = logger or _logger
    while not should_stop():
        conn = None
        try:
            conn = open_listen_connection()
            conn.execute(f"LISTEN {CONSOLIDATION_CHANNEL}")
            logger.info("consolidation: listening on %s", CONSOLIDATION_CHANNEL)
            if on_ready is not None:
                on_ready()
            while not should_stop():
                # Each drain returns after ~1s so should_stop() is re-checked
                # roughly every second for a responsive shutdown.
                _drain_notifications(conn, handle, should_stop, logger)
        except psycopg.Error as err:
            logger.error(
                "consolidation: listen connection error -> reconnecting in %ss (%s)",
                reconnect_delay,
                err,
            )
            if not should_stop():
                time.sleep(reconnect_delay)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except psycopg.Error:
                    pass
