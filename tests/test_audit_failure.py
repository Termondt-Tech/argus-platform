"""A failed audit write must be TERMINAL and must halt trading.

Regression cover for 2026-07-21: a second writer stole three `seq` values, the
running process took a UNIQUE violation, `_advance()` never ran, and the writer
retried the same `seq` every 5s for 80 minutes while the signal loop kept
evaluating — unrecorded, and looking healthy.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from argus.audit.log import AuditLog, AuditWriteError
from argus.db import apply_migrations, connect


@pytest.fixture
def log(tmp_path: Path):
    conn = connect(tmp_path / "t.db")
    apply_migrations(conn)
    yield AuditLog(conn, tier2_queue_maxsize=64, tier2_queue_alert_depth=48)
    conn.close()


def _steal_seq(conn: sqlite3.Connection, seq: int) -> None:
    """Simulate a second writer taking `seq` out from under us."""
    conn.execute(
        "INSERT INTO audit_log (seq, ts, tier, category, payload, prev_hash, hash) "
        "VALUES (?, '2026-07-21T16:40:01.041512Z', 1, 'process.start', '{}', 'x', 'y')",
        (seq,),
    )


async def test_tier1_write_failure_raises_and_latches(log: AuditLog):
    await log.record("first", {})
    _steal_seq(log._conn, log.last_seq + 1)      # the collision

    with pytest.raises(AuditWriteError):
        await log.record("collides", {})
    assert log.failed is not None

    # Terminal: it does NOT retry the same seq forever.
    with pytest.raises(AuditWriteError):
        await log.record("after", {})


async def test_failure_engages_the_halt_callback(log: AuditLog):
    halted: list[str] = []

    async def on_failure(detail: str) -> None:
        halted.append(detail)

    log.on_failure = on_failure
    await log.record("first", {})
    _steal_seq(log._conn, log.last_seq + 1)

    with pytest.raises(AuditWriteError):
        await log.record("collides", {})
    assert len(halted) == 1
    assert "UNIQUE" in halted[0]

    # Idempotent — a second failure must not re-fire the halt.
    with pytest.raises(AuditWriteError):
        await log.record("again", {})
    assert len(halted) == 1


async def test_failure_alerts(log: AuditLog):
    alerts: list[tuple[str, dict]] = []
    log._alert = lambda kind, detail: alerts.append((kind, detail))

    await log.record("first", {})
    _steal_seq(log._conn, log.last_seq + 1)
    with pytest.raises(AuditWriteError):
        await log.record("collides", {})

    assert [k for k, _ in alerts] == ["audit_write_failed"]


async def test_tier2_failure_latches_instead_of_spinning(log: AuditLog):
    """The exact 2026-07-21 shape: Tier 2 must not retry the same seq forever."""
    halted: list[str] = []
    log.on_failure = lambda d: asyncio.sleep(0, result=halted.append(d))

    log.start()
    await log.record("first", {})
    _steal_seq(log._conn, log.last_seq + 1)

    for i in range(5):
        log.telemetry(f"tick.{i}", {})
    await asyncio.wait_for(log._queue.join(), timeout=5)

    assert log.failed is not None
    assert len(halted) == 1                      # latched once, not once per tick
    await log.aclose()


async def test_on_failure_raising_does_not_mask_the_write_error(log: AuditLog):
    async def bad_handler(detail: str) -> None:
        raise RuntimeError("halt path is broken too")

    log.on_failure = bad_handler
    await log.record("first", {})
    _steal_seq(log._conn, log.last_seq + 1)

    with pytest.raises(AuditWriteError):   # not RuntimeError
        await log.record("collides", {})


async def test_healthy_log_is_unaffected(log: AuditLog):
    for i in range(20):
        await log.record(f"c{i}", {"i": i})
    assert log.failed is None
    log.verify_chain()
