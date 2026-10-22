"""SQLite connection setup.

Durability/latency posture (spec §5): WAL mode, with `synchronous` now a CONFIG
value (`audit.synchronous`) rather than a literal, because the right answer
differs by environment and that choice should be visible and auditable.

    NORMAL  fsyncs only at checkpoint. WAL guarantees no corruption, but a
            HOST-level unclean shutdown (droplet hard reset, hypervisor failure,
            power loss) can roll back the last committed transactions. A plain
            process crash is already safe — the page cache outlives it.
    FULL    fsyncs every commit. The transaction is on durable media before
            COMMIT returns.

Measured cost of FULL, in-container on a Docker volume, single-record commits:
p50 0.009ms -> 0.081ms, p99 0.024ms -> 1.86ms. At Argus's ~0.6 writes/sec that is
noise, which is why `live` is required to use it (see config.py): the tail of the
log is what reconciliation reads back as expected state, and it is also the part
you most want after a crash.

The audit log routes the actual write through `asyncio.to_thread`, so even a
multi-millisecond fsync outlier can't stall the single event loop the watchdog
and kill switch share (spec §4).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Whitelisted here as well as in config.py: `connect()` is called directly by
# tools and tests, and this value is interpolated into a PRAGMA statement.
VALID_SYNCHRONOUS = ("OFF", "NORMAL", "FULL", "EXTRA")


def connect(db_path: str | Path, *, synchronous: str = "NORMAL") -> sqlite3.Connection:
    """Open a tuned SQLite connection.

    `check_same_thread=False` is deliberate: writes are dispatched to a worker
    thread via `asyncio.to_thread`, but access is serialized by the audit log's
    async chain lock, so only one thread ever touches the connection at a time.

    `synchronous` defaults to NORMAL so direct callers (tools, tests) keep the
    previous behaviour; the application always passes `audit.synchronous`.
    """
    mode = synchronous.upper()
    if mode not in VALID_SYNCHRONOUS:
        raise ValueError(
            f"synchronous must be one of {VALID_SYNCHRONOUS}, got {synchronous!r}"
        )
    conn = sqlite3.connect(
        db_path,
        check_same_thread=False,
        isolation_level=None,  # autocommit off implicit BEGINs; we manage txns explicitly
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA synchronous={mode}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
