"""DailyLossBreaker: trips once on breach, latches, rehydrates, resets."""

from __future__ import annotations

from argus.audit.log import AuditLog
from argus.risk.breaker import DailyLossBreaker


async def test_does_not_trip_above_threshold(audit: AuditLog):
    b = DailyLossBreaker(audit, daily_max_loss=1000.0)
    assert await b.evaluate(-999.99) is False
    assert b.tripped is False
    # No breach record written.
    n = audit._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert n == 0


async def test_trips_at_threshold_and_records_once(audit: AuditLog):
    b = DailyLossBreaker(audit, daily_max_loss=1000.0)
    assert await b.evaluate(-1000.0) is True  # exactly at threshold trips
    assert b.tripped is True
    # Evaluating again (even with worse loss) must not write a second breach.
    assert await b.evaluate(-2000.0) is True
    rows = audit._conn.execute(
        "SELECT category FROM audit_log WHERE category = 'daily_loss.breach'"
    ).fetchall()
    assert len(rows) == 1
    audit.verify_chain()


async def test_latches_even_if_pnl_recovers(audit: AuditLog):
    b = DailyLossBreaker(audit, daily_max_loss=500.0)
    await b.evaluate(-600.0)
    assert b.tripped is True
    # A recovering P&L must NOT re-arm trading — the day's budget is spent.
    assert await b.evaluate(100.0) is True
    assert b.tripped is True


async def test_reset_rearms(audit: AuditLog):
    b = DailyLossBreaker(audit, daily_max_loss=500.0)
    await b.evaluate(-600.0)
    b.reset()
    assert b.tripped is False


async def test_initially_tripped_rehydration(audit: AuditLog):
    # Simulates reconciliation finding a same-day breach after a restart.
    b = DailyLossBreaker(audit, daily_max_loss=500.0, initially_tripped=True)
    assert b.tripped is True
    # Already tripped: a fresh breach event is not re-logged.
    assert await b.evaluate(-600.0) is True
    n = audit._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert n == 0
