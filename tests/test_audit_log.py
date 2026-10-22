"""AuditLog: tier-1 durability, tier-2 bounded telemetry, and chain integrity."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from argus.audit.log import AuditLog, ChainIntegrityError
from argus.db import apply_migrations, connect


async def test_tier1_record_is_persisted_and_chained(audit: AuditLog):
    r1 = await audit.record("gate.decision", {"allow": True, "n": 1})
    r2 = await audit.record("order.fill", {"price": 1.25})
    assert r1.seq == 1 and r2.seq == 2
    assert r2.prev_hash == r1.hash
    assert audit.last_seq == 2
    audit.verify_chain()


async def test_tier2_telemetry_is_written_and_chains_with_tier1(audit: AuditLog):
    # Tier 2 is decoupled: telemetry lands in the chain whenever the background
    # consumer processes it, not at a fixed position relative to Tier 1. So the
    # guarantee is that all events land, the chain stays valid, and the tier
    # counts are correct — NOT a specific interleaving.
    audit.start()
    await audit.record("gate.decision", {"allow": False})
    assert audit.telemetry("signal.eval", {"delta": 0.12}) is True
    await audit.record("order.submit", {"legs": 2})
    await audit.aclose()  # drains the Tier 2 queue

    audit.verify_chain()
    tiers = sorted(r["tier"] for r in audit._conn.execute("SELECT tier FROM audit_log"))
    assert tiers == [1, 1, 2]  # two Tier 1, one Tier 2, all persisted and chained


async def test_tier2_drops_when_queue_full_without_blocking(db: sqlite3.Connection):
    # Do NOT start the consumer, so the bounded queue fills and stays full.
    log = AuditLog(db, tier2_queue_maxsize=3, tier2_queue_alert_depth=2)
    accepted = [log.telemetry("x", {"i": i}) for i in range(5)]
    assert accepted == [True, True, True, False, False]
    assert log.dropped == 2
    assert log.queue_depth == 3


# -- per-category counts (the O(1) replacement for a 15s full scan) -----------
#
# `session.py`'s status line used to run `SELECT category, COUNT(*) ... GROUP BY
# category` against a freshly-opened connection ON THE EVENT LOOP, every 15s, for
# a whole session — a stall that grew with the table (~88k rows on the droplet at
# 2026-08-05, +14k/day). These pin that the in-memory tally is not an
# approximation of that query but exactly equal to it.


def _db_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """The query the in-memory tally replaces. The oracle for these tests."""
    return {
        str(c): int(n)
        for c, n in conn.execute("SELECT category, COUNT(*) FROM audit_log GROUP BY category")
    }


async def test_counts_equal_the_query_they_replace_across_both_tiers(audit: AuditLog):
    audit.start()
    await audit.record("gate.decision", {"allow": True})
    await audit.record("gate.decision", {"allow": False})
    await audit.record("shadow.would_submit", {"n": 1})
    audit.telemetry("signal.none", {"reason": "no_credit"})
    audit.telemetry("signal.none", {"reason": "no_credit"})
    await audit.aclose()  # drains Tier 2

    assert audit.counts == {
        "gate.decision": 2,
        "shadow.would_submit": 1,
        "signal.none": 2,
    }
    assert audit.counts == _db_counts(audit._conn)


async def test_counts_are_seeded_from_the_table_so_a_restart_does_not_reset_them(
    tmp_path: Path,
):
    """The status line shows an ALL-TIME ledger; a process restart must not zero
    it. This is why the seed exists rather than counting only what we wrote."""
    path = tmp_path / "audit.db"
    conn = connect(path)
    apply_migrations(conn)
    log = AuditLog(conn, tier2_queue_maxsize=8, tier2_queue_alert_depth=6)
    for _ in range(3):
        await log.record("process.start", {})
    conn.close()

    conn2 = connect(path)
    log2 = AuditLog(conn2, tier2_queue_maxsize=8, tier2_queue_alert_depth=6)
    assert log2.counts == {"process.start": 3}
    await log2.record("process.start", {})
    assert log2.counts == {"process.start": 4}
    assert log2.counts == _db_counts(conn2)


async def test_a_dropped_tier2_line_is_not_counted(db: sqlite3.Connection):
    """A dropped telemetry line is never written, so counting it would make the
    tally disagree with the table — the one thing it must never do."""
    log = AuditLog(db, tier2_queue_maxsize=2, tier2_queue_alert_depth=1)
    for i in range(5):  # consumer not started: 2 queued, 3 dropped
        log.telemetry("signal.none", {"i": i})
    assert log.dropped == 3
    assert log.counts == {}  # nothing WRITTEN yet
    assert log.counts == _db_counts(db)


async def test_an_unserializable_tier1_payload_is_not_counted(audit: AuditLog):
    with pytest.raises(ValueError):
        await audit.record("bad", {"obj": object()})
    assert audit.counts == {}
    assert audit.counts == _db_counts(audit._conn)


async def test_counts_is_a_copy_and_cannot_be_mutated_by_a_caller(audit: AuditLog):
    await audit.record("gate.decision", {})
    snapshot = audit.counts
    snapshot["gate.decision"] = 9999
    snapshot["fabricated"] = 1
    assert audit.counts == {"gate.decision": 1}


def test_the_session_status_line_never_full_scans_on_a_cadence():
    """A REGRESSION GUARD, by AST rather than by grep.

    The defect was not that `_counts(db_path)` existed — it is still correct for
    `tools.shadow`, which calls it once after shutdown, when there is no AuditLog
    left to ask. The defect was calling it from `run_session`'s 15-second print
    loop. Structure is what has to be pinned, because the two call sites look
    identical at the line level.
    """
    import ast
    import inspect

    import argus.session as session_mod

    tree = ast.parse(inspect.getsource(session_mod))
    body = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_session"
    )
    called = {
        node.func.id
        for node in ast.walk(body)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_counts" not in called, (
        "run_session must read AuditLog.counts (O(1), in memory), never _counts() "
        "— that opens a connection and full-scans a table that grows ~14k rows/day"
    )


async def test_tier1_rejects_unserializable_payload(audit: AuditLog):
    with pytest.raises(ValueError, match="not JSON-serializable"):
        await audit.record("bad", {"obj": object()})
    # Nothing should have been written.
    assert audit.last_seq == 0


async def test_chain_survives_restart(tmp_path: Path):
    path = tmp_path / "audit.db"
    conn = connect(path)
    apply_migrations(conn)
    log = AuditLog(conn, tier2_queue_maxsize=8, tier2_queue_alert_depth=6)
    await log.record("a", {"n": 1})
    r2 = await log.record("b", {"n": 2})
    conn.close()

    # Reopen: the new AuditLog must resume seq/hash from the persisted head.
    conn2 = connect(path)
    log2 = AuditLog(conn2, tier2_queue_maxsize=8, tier2_queue_alert_depth=6)
    assert log2.last_seq == 2
    r3 = await log2.record("c", {"n": 3})
    assert r3.seq == 3
    assert r3.prev_hash == r2.hash
    log2.verify_chain()
    conn2.close()


async def test_verify_chain_detects_tampering(audit: AuditLog):
    await audit.record("a", {"secret": "original"})
    await audit.record("b", {"n": 2})
    audit.verify_chain()  # clean

    # Tamper with a persisted payload directly, bypassing the hashing path.
    audit._conn.execute("UPDATE audit_log SET payload = ? WHERE seq = 1", ('{"secret":"forged"}',))
    audit._conn.commit()
    with pytest.raises(ChainIntegrityError):
        audit.verify_chain()


async def test_verify_chain_detects_deletion(audit: AuditLog):
    await audit.record("a", {"n": 1})
    await audit.record("b", {"n": 2})
    await audit.record("c", {"n": 3})
    audit._conn.execute("DELETE FROM audit_log WHERE seq = 2")
    audit._conn.commit()
    with pytest.raises(ChainIntegrityError, match="seq gap"):
        audit.verify_chain()


async def test_concurrent_tier1_writes_keep_chain_consistent(audit: AuditLog):
    # Fire many Tier 1 records concurrently; the chain lock must serialize them
    # into a gap-free, correctly linked chain.
    await asyncio.gather(*(audit.record("burst", {"i": i}) for i in range(50)))
    assert audit.last_seq == 50
    audit.verify_chain()
    seqs = [r["seq"] for r in audit._conn.execute("SELECT seq FROM audit_log ORDER BY seq")]
    assert seqs == list(range(1, 51))
