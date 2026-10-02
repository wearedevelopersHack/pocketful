"""Connection factory and transaction control — Pocketful ledger.

This is the ONLY module that opens a SQLite connection. Every connection it
hands out is configured, explicitly and in this order, with:

    journal_mode = WAL
    foreign_keys = ON
    busy_timeout = BUSY_TIMEOUT_MS
    synchronous  = FULL

``synchronous=FULL`` is deliberate: WAL's default ``NORMAL`` is not durable
across a power loss, so an acknowledged transfer could vanish. On a ledger the
acknowledged write is the promise, so we pay the fsync.

Isolation is stated explicitly, never inherited. The connection is opened with
``isolation_level=None`` (autocommit), which switches OFF the stdlib driver's
implicit BEGIN, and every money-moving transaction is opened by this module with
``BEGIN IMMEDIATE`` (see :func:`run_immediate`). Writers therefore serialize on
the database write lock instead of doing check-then-act across transactions.
"""

from __future__ import annotations

import sqlite3
import time

from . import schema

# How long a blocked writer waits inside SQLite before surfacing SQLITE_BUSY.
BUSY_TIMEOUT_MS = 5000

# Bounded retry with backoff for a busy/locked database. These are retry
# delays in seconds, not amounts; nothing here is money. Deadlock is handled by
# retrying the whole unit of work, never by lowering the isolation level.
_BACKOFF_SECONDS = (0.002, 0.005, 0.011, 0.023, 0.047)
_MAX_ATTEMPTS = len(_BACKOFF_SECONDS) + 1


def connect(path: str) -> sqlite3.Connection:
    """Create and fully configure a ledger connection to ``path``.

    The schema is ensured on the way out, so a freshly opened connection is
    ready for ``Ledger(conn)``.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=FULL")
    schema.initialize(conn)
    return conn


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _backoff(attempt: int) -> None:
    """Sleep before retry ``attempt`` (0-based), capped at the last delay."""
    time.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])


def run_immediate(conn: sqlite3.Connection, work):
    """Run ``work()`` inside a single ``BEGIN IMMEDIATE`` transaction.

    ``work`` is a zero-argument callable that performs its reads and writes on
    ``conn`` and returns a value. The whole unit is wrapped in one write
    transaction: if it raises, it is rolled back and the exception propagates;
    if it commits, its return value is returned.

    A busy or locked database rolls the unit back and retries it from the top
    (the caller's ``work`` must therefore be safe to re-run — the ledger's
    transfer reads its idempotency record first, so a retry is a no-op replay
    rather than a second application). Isolation is never reduced.
    """
    attempt = 0
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or attempt >= _MAX_ATTEMPTS:
                raise
            _backoff(attempt)
            attempt += 1
            continue

        try:
            result = work()
        except BaseException as exc:
            conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc) and attempt < _MAX_ATTEMPTS:
                _backoff(attempt)
                attempt += 1
                continue
            raise

        try:
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            conn.execute("ROLLBACK")
            if not _is_busy(exc) or attempt >= _MAX_ATTEMPTS:
                raise
            _backoff(attempt)
            attempt += 1
            continue
        return result
