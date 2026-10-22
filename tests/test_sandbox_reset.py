"""The sandbox 24h-reset handling (chunk 5).

This is the one place a safety rail is deliberately allowed to stand down, so the
tests are about the boundaries of that, not the happy path. Confirmed against
tastytrade's documentation (2026-07-30): a SANDBOX account's trades, positions and
balances are wiped every 24 hours, so sandbox presents a flat broker every morning
against a ledger on a persistent volume — a `missing_at_broker` discrepancy, and
therefore a hard halt, every single day.

The rule (operator's resolution, 2026-07-30) reuses Argus's EXISTING day-rollover
notion — an ET trading date compared against another ET trading date — rather than an
elapsed-time threshold guessing at a reset schedule tastytrade does not publish.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from argus.db import apply_migrations, connect
from argus.execution.models import (
    LegAction,
    LiveOrders,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
)
from argus.execution.store import InMemoryOrderStore, LedgerState, SqliteOrderStore
from argus.reconcile import (
    RESET_DAY_ROLLED_OVER,
    RESET_LEDGER_EMPTY,
    RESET_LEDGER_WRITTEN_TODAY,
    RESET_NO_CLEAN_MARKER,
    RESET_NOT_SANDBOX,
    RESET_SAME_TRADING_DAY,
    InMemoryPositionStore,
    Reconciler,
    last_clean_reconciliation,
    reconcile_and_record,
    sandbox_reset_decision,
)
from argus.account.fake import FakeBrokerAccount
from argus.account.models import Position

TODAY = date(2026, 7, 30)
YESTERDAY = date(2026, 7, 29)


def ledger(*, orders=1, positions=2, written=YESTERDAY) -> LedgerState:
    return LedgerState(orders=orders, positions=positions, newest_trading_date=written)


def decide(**overrides):
    kw = dict(
        environment="sandbox",
        today=TODAY,
        last_clean_trading_date=YESTERDAY,
        ledger=ledger(),
    )
    kw.update(overrides)
    return sandbox_reset_decision(**kw)


# -- the pure decision --------------------------------------------------------


def test_a_rolled_over_trading_day_with_a_stale_ledger_is_the_reset():
    d = decide()
    assert d.clear is True
    assert d.reason == RESET_DAY_ROLLED_OVER


@pytest.mark.parametrize("environment", ["shadow", "live"])
def test_only_sandbox_is_ever_cleared(environment):
    """Live is never wiped nightly, so "broker empty, ledger non-empty" there is
    always real — which is why the gate is `environment == "sandbox"` and NOT
    `mismatch_halts_process`. Shadow holds nothing to clear in the first place."""
    d = decide(environment=environment)
    assert d.clear is False
    assert d.reason == RESET_NOT_SANDBOX


def test_the_same_trading_day_is_a_real_mismatch():
    """A crash-restart inside one trading day must halt exactly as it did before."""
    d = decide(last_clean_trading_date=TODAY)
    assert d.clear is False
    assert d.reason == RESET_SAME_TRADING_DAY


def test_a_ledger_written_TODAY_refuses_the_clear_even_after_a_rollover():
    """The condition that makes a same-day crash-restart still halt.

    Scenario: last CLEAN reconcile was yesterday, the process submitted an order today,
    then crashed before reconciling again. The rollover test alone would say "clear
    it" and silently discard today's real order — the masking that ruled out the
    unconditional-clear alternative. This refuses, and the mismatch halts.
    """
    d = decide(last_clean_trading_date=YESTERDAY, ledger=ledger(written=TODAY))
    assert d.clear is False
    assert d.reason == RESET_LEDGER_WRITTEN_TODAY


def test_no_clean_reconciliation_on_record_refuses_the_clear():
    """Fail closed. A non-empty ledger with no clean run behind it is unexplained, and
    an unexplained ledger is not evidence of a reset."""
    d = decide(last_clean_trading_date=None)
    assert d.clear is False
    assert d.reason == RESET_NO_CLEAN_MARKER


def test_an_empty_ledger_is_reported_rather_than_silently_skipped():
    d = decide(ledger=LedgerState(orders=0, positions=0, newest_trading_date=None))
    assert d.clear is False
    assert d.reason == RESET_LEDGER_EMPTY


def test_a_ledger_with_no_readable_date_still_clears_on_a_rollover():
    """`newest_trading_date is None` with rows present means the stamps were
    unreadable. The rollover marker is the authority; an unreadable stamp must not
    become a permanent refusal that halts sandbox every morning forever."""
    d = decide(ledger=ledger(written=None))
    assert d.clear is True


def test_the_decision_payload_carries_the_whole_reasoning():
    payload = decide().to_payload()
    assert payload["clear"] is True
    assert payload["today"] == TODAY.isoformat()
    assert payload["last_clean_trading_date"] == YESTERDAY.isoformat()
    assert payload["ledger"]["orders"] == 1


# -- the persisted marker -----------------------------------------------------


def _db(tmp_path: Path):
    conn = connect(str(tmp_path / "argus.db"))
    apply_migrations(conn)
    return conn


def _write(conn, category: str, payload: dict, seq: int) -> None:
    conn.execute(
        "INSERT INTO audit_log (seq, ts, tier, category, payload, prev_hash, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (seq, "2026-07-30T00:00:00Z", 1, category, json.dumps(payload), "x", f"h{seq}"),
    )
    conn.commit()


def test_the_marker_is_the_trading_date_of_the_latest_CLEAN_result(tmp_path):
    conn = _db(tmp_path)
    _write(conn, "reconcile.result", {"ok": True, "trading_date": "2026-07-29"}, 1)
    assert last_clean_reconciliation(conn) == YESTERDAY


def test_a_mismatch_as_the_latest_result_reads_as_NO_marker(tmp_path):
    """Deliberately not "search backwards for the last clean one": after a non-ok
    result sandbox/live hard-halt and refuse to start until it is fixed, so the latest
    record being a mismatch means an operator is mid-investigation — the worst possible
    moment to stand the ledger-clearing rail down."""
    conn = _db(tmp_path)
    _write(conn, "reconcile.result", {"ok": True, "trading_date": "2026-07-28"}, 1)
    _write(conn, "reconcile.result", {"ok": False, "trading_date": "2026-07-29"}, 2)
    assert last_clean_reconciliation(conn) is None


def test_an_empty_log_or_a_record_predating_the_field_reads_as_no_marker(tmp_path):
    conn = _db(tmp_path)
    assert last_clean_reconciliation(conn) is None
    _write(conn, "reconcile.result", {"ok": True}, 1)  # pre-chunk-5 payload
    assert last_clean_reconciliation(conn) is None


def test_an_unreadable_payload_never_raises(tmp_path):
    """This feeds a safety decision; it must not be able to take reconciliation down."""
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO audit_log (seq, ts, tier, category, payload, prev_hash, hash) "
        "VALUES (1, 'x', 1, 'reconcile.result', 'not json', 'x', 'h')"
    )
    conn.commit()
    assert last_clean_reconciliation(conn) is None


# -- the ledger read and clear ------------------------------------------------


def _order(order_id: int) -> Order:
    return Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", LegAction.SELL_TO_OPEN, 1, remaining_quantity=0),
            OrderLeg("SPXW  P4995", LegAction.BUY_TO_OPEN, 1, remaining_quantity=0),
        ),
        limit_price=Decimal("0.85"),
        external_identifier="argus-open-20260729-put-5000-4995",
        candidate_id="20260729-put-5000-4995",
        broker_order_id=order_id,
    )


async def test_the_ledger_state_reports_counts_and_an_ET_trading_date(tmp_path):
    conn = _db(tmp_path)

    async def run(fn):
        try:
            result = fn(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    # 01:30 UTC is still the PREVIOUS day in ET — the case that makes the ET
    # conversion load-bearing rather than cosmetic. A naive UTC date would call this
    # "today", conclude the ledger was written today, and refuse a legitimate clear
    # every morning.
    utc_stamp = (
        datetime(2026, 7, 30, 1, 30, tzinfo=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    store = SqliteOrderStore(run, clock=lambda: utc_stamp)
    await store.apply(_order(1))

    state = await store.ledger_state()
    assert state.orders == 1
    assert state.positions == 2
    assert state.newest_trading_date == date(2026, 7, 29)
    assert state.empty is False
    assert utc_stamp[:10] == "2026-07-30", "the UTC date really does differ from the ET one"


async def test_clearing_removes_holdings_and_KEEPS_the_order_identity(tmp_path):
    """The reset clears HOLDINGS and keeps the order IDENTITY rows.

    Deleting the `orders` rows caused the 2026-08-14 halt: `get_live_orders()` looks
    back a rolling 24 HOURS, so the broker still reports yesterday's fills after a
    trading-DAY rollover, and an emptied ledger turns Argus's own orders into
    `unexpected_filled_order`. `known_orders()` reads `orders` alone — no join to
    `order_legs` — so keeping that one table is the whole of what v0.3.2 needed.

    `order_legs` goes with `positions` (2026-08-17): spread reassembly derives
    holdings from `applied_quantity` per trade idea, so legs surviving a wipe would
    rebuild yesterday's spreads as phantoms for the watchdog to chase.
    """
    conn = _db(tmp_path)

    async def run(fn):
        result = fn(conn)
        conn.commit()
        return result

    store = SqliteOrderStore(run)
    await store.apply(_order(1))
    assert (await store.ledger_state()).empty is False

    removed = await store.clear_reset_holdings()
    # Reports the WHOLE ledger as it was — that is what lands in the Tier 1 record,
    # which documents the state the reset was assumed against, not just the deletion.
    assert removed.orders == 1 and removed.positions == 2

    after = await store.ledger_state()
    assert after.positions == 0, "the wiped side"
    assert after.orders == 1, "the identity row the broker still reports"
    assert after.empty is False
    # Holdings gone: no phantom spread can be rebuilt from a wiped account.
    assert conn.execute("SELECT COUNT(*) FROM order_legs").fetchone()[0] == 0
    assert (await store.open_spreads(today=TODAY)).spreads == ()
    # And the retained order is still recognisable, which is the entire point.
    assert await store.known_orders([1]) == {1}


async def test_a_retained_order_reconstructs_no_spread_once_positions_are_gone(tmp_path):
    """The hazard retention could plausibly introduce, ruled out: the watchdog must
    not adopt yesterday's spread off an order row whose position no longer exists."""
    conn = _db(tmp_path)

    async def run(fn):
        result = fn(conn)
        conn.commit()
        return result

    store = SqliteOrderStore(run)
    await store.apply(_order(1))
    assert len((await store.open_spreads(today=TODAY)).spreads) == 1, "held: watchable"

    await store.clear_reset_holdings()
    scan = await store.open_spreads(today=TODAY)
    assert list(scan.spreads) == [], "no position, nothing to watch"
    assert list(scan.malformed) == [], "and not reported as corruption either"


# -- the whole sequence -------------------------------------------------------


class _Audit:
    def __init__(self, marker: date | None = YESTERDAY) -> None:
        self.records: list[tuple[str, dict]] = []
        self.telemetry_records: list[tuple[str, dict]] = []
        self._marker = marker

    async def record(self, category, payload):
        self.records.append((category, payload))

    def telemetry(self, category, payload):
        self.telemetry_records.append((category, payload))
        return True

    async def read(self, fn):
        return self._marker

    def category(self, name: str) -> dict:
        for c, p in self.records:
            if c == name:
                return p
        raise AssertionError(f"no {name}: {[c for c, _ in self.records]}")


async def test_a_stale_sandbox_ledger_is_cleared_and_then_reconciles_clean():
    """The end-to-end point of the whole mechanism: sandbox starts clean the morning
    after a reset instead of hard-halting on a discrepancy that is not a fault."""
    audit = _Audit(marker=YESTERDAY)
    store = InMemoryOrderStore(trading_date=YESTERDAY)
    await store.apply(_order(1))
    assert store.positions, "precondition: the ledger holds something"

    result = await reconcile_and_record(
        Reconciler(FakeBrokerAccount([]), InMemoryPositionStore()),
        audit=audit,
        environment="sandbox",
        today=TODAY,
        session_settled=False,
        alert=lambda k, d: None,
        order_store=store,
    )

    assert result.ok is True
    assert audit.category("reconcile.sandbox_reset")["reason"] == RESET_DAY_ROLLED_OVER
    assert store.positions == {}, "the side the reset actually wipes"
    assert store.orders, "the order record survives — the broker still reports it"
    # And the stamp the NEXT morning's rollover check will read back.
    assert audit.category("reconcile.result")["trading_date"] == TODAY.isoformat()


class _StillReportsYesterdaysOrders:
    """`get_live_orders()` reaches back a rolling 24 HOURS, not to the trading-day
    boundary, so the morning after a trading day the broker still reports yesterday's
    filled orders. Measured 2026-08-14: fills at 14:00 and 14:22 UTC were still
    returned at 13:40 UTC the next day — 23h18m to 23h40m later."""

    def __init__(self, orders: list[Order]) -> None:
        self._orders = orders

    async def get_live_orders(self) -> LiveOrders:
        return LiveOrders(orders=tuple(self._orders))


async def test_yesterdays_fills_still_at_the_broker_do_NOT_halt_the_morning_after():
    """The 2026-08-14 regression, end to end.

    The reset clearing the ORDER ledger emptied `known_orders()` against orders the
    broker was still reporting, so Argus's own fills came back as
    `unexpected_filled_order` and hard-halted the process — 34 restart loops. The
    reset must clear only what the broker actually wiped.
    """
    audit = _Audit(marker=YESTERDAY)
    store = InMemoryOrderStore(trading_date=YESTERDAY)
    filled = dataclasses.replace(_order(1311730), status=OrderStatus.FILLED)
    await store.apply(filled)
    assert store.positions, "precondition: yesterday's ledger holds the spread"
    alerts: list[str] = []

    result = await reconcile_and_record(
        Reconciler(
            FakeBrokerAccount([]),  # positions wiped, which the reset IS right about
            InMemoryPositionStore(),
            execution=_StillReportsYesterdaysOrders([filled]),
            order_store=store,
        ),
        audit=audit,
        environment="sandbox",
        today=TODAY,
        session_settled=False,
        alert=lambda k, d: alerts.append(k),
        order_store=store,
    )

    assert result.order_discrepancies == (), "Argus's own order, still recognised"
    assert result.ok is True
    assert alerts == [], "and no page"


async def test_an_order_that_is_NOT_argus_own_still_halts_after_a_reset():
    """The safety half: retention must not blind the rail to a genuinely foreign
    order. Only ids the ledger actually holds are exempt."""
    audit = _Audit(marker=YESTERDAY)
    store = InMemoryOrderStore(trading_date=YESTERDAY)
    await store.apply(_order(1311730))
    foreign = dataclasses.replace(
        _order(9999999),
        external_identifier="somebody-elses-order",
        status=OrderStatus.FILLED,
    )
    alerts: list[str] = []

    result = await reconcile_and_record(
        Reconciler(
            FakeBrokerAccount([]),
            InMemoryPositionStore(),
            execution=_StillReportsYesterdaysOrders([foreign]),
            order_store=store,
        ),
        audit=audit,
        environment="sandbox",
        today=TODAY,
        session_settled=False,
        alert=lambda k, d: alerts.append(k),
        order_store=store,
    )

    assert result.ok is False
    assert [d.broker_order_id for d in result.order_discrepancies] == [9999999]
    assert alerts == ["reconciliation_mismatch"]


async def test_a_same_day_mismatch_is_NOT_cleared_and_stays_a_mismatch():
    """The safety property: the reset handling must not swallow a real discrepancy."""
    audit = _Audit(marker=TODAY)
    store = InMemoryOrderStore(trading_date=TODAY)
    await store.apply(_order(1))
    alerts: list[str] = []

    held = Position(
        symbol="SPXW  P9999",
        underlying="SPXW",
        instrument_type="Equity Option",
        signed_quantity=1,
        average_open_price=1.0,
        mark_price=1.0,
    )
    result = await reconcile_and_record(
        Reconciler(FakeBrokerAccount([held]), InMemoryPositionStore()),
        audit=audit,
        environment="sandbox",
        today=TODAY,
        session_settled=False,
        alert=lambda k, d: alerts.append(k),
        order_store=store,
    )

    assert result.ok is False
    assert alerts == ["reconciliation_mismatch"]
    assert store.orders, "the ledger must survive a same-day mismatch"
    assert [c for c, _ in audit.records] == ["reconcile.result"]
    # The no-clear branch is Tier 2 — "checked, nothing to do" is the every-morning
    # case and would otherwise put a Tier 1 record in front of every session.
    assert audit.telemetry_records[0][0] == "reconcile.reset_check"


async def test_the_check_is_skipped_entirely_without_an_order_store():
    """Shadow, and any caller with no order ledger: no reset check, no telemetry, and
    the position-only comparison it always had."""
    audit = _Audit()
    result = await reconcile_and_record(
        Reconciler(FakeBrokerAccount([]), InMemoryPositionStore()),
        audit=audit,
        environment="shadow",
        today=TODAY,
        session_settled=False,
        alert=lambda k, d: None,
    )
    assert result.ok is True
    assert audit.telemetry_records == []


# -- the real SQLite path -----------------------------------------------------
#
# The one test here that uses actual connections, because the failure it rules out is
# invisible to fakes and would hard-halt sandbox EVERY morning: the ledger is cleared
# on the process's WRITER connection, while the expected-position set reconciliation
# compares against is read through a SEPARATE reader connection (`session.py` opens one
# per reconcile; WAL permits concurrent readers). If that reader held a snapshot from
# before the clear, it would report yesterday's positions, disagree with the now-flat
# broker, and refuse the day — with the ledger already empty, so nothing to see.


async def test_the_clear_is_visible_to_the_separate_reader_connection(tmp_path):
    from argus.audit.log import AuditLog
    from argus.reconcile import SqlitePositionStore

    path = tmp_path / "argus.db"
    writer = connect(path)
    apply_migrations(writer)
    audit = AuditLog(writer, tier2_queue_maxsize=8, tier2_queue_alert_depth=6)
    audit.start()

    # The reader is opened BEFORE the clear, exactly as `_reconcile_or_abort` does.
    reader = connect(path)
    expected = SqlitePositionStore(reader)

    # Stamped YESTERDAY, so the rollover really has happened. (Stamping it "now" is how
    # the first version of this test failed — correctly: `RESET_LEDGER_WRITTEN_TODAY`
    # refused the clear, which is the same-day-crash guard doing its job.)
    yesterday_utc = (
        datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    store = SqliteOrderStore(audit.write, clock=lambda: yesterday_utc)
    await store.apply(_order(1))
    # Force a read on the reader first, so any snapshot it might pin is pinned here.
    assert expected.open_positions(), "precondition: the reader sees the positions"

    result = await reconcile_and_record(
        Reconciler(FakeBrokerAccount([]), expected),
        audit=_WriterAudit(audit, marker=YESTERDAY),
        environment="sandbox",
        today=TODAY,
        session_settled=False,
        alert=lambda k, d: None,
        order_store=store,
    )

    assert expected.open_positions() == {}, "the reader must see the cleared ledger"
    assert result.ok is True, "a cleared ledger against a flat broker reconciles clean"

    await audit.aclose()
    reader.close()
    writer.close()


class _WriterAudit:
    """The real `AuditLog` for `write`, a fixed marker for `read`.

    `read` is stubbed rather than seeded with a real `reconcile.result` row so this test
    stays about connection visibility and not about the marker lookup, which
    `test_the_marker_is_the_trading_date_of_the_latest_CLEAN_result` already covers.
    """

    def __init__(self, audit, *, marker):
        self._audit = audit
        self._marker = marker

    async def record(self, category, payload):
        await self._audit.record(category, payload)

    def telemetry(self, category, payload):
        return self._audit.telemetry(category, payload)

    async def write(self, fn):
        return await self._audit.write(fn)

    async def read(self, fn):
        return self._marker


async def test_a_ledger_written_TODAY_survives_the_real_path_too(tmp_path):
    """The safety direction, on real connections.

    The mirror of the test above, and the one that must never regress: a ledger stamped
    today is NOT cleared even though the last clean reconcile was yesterday, so the
    same-day crash-restart still reaches reconciliation with its real state intact.
    """
    from argus.audit.log import AuditLog
    from argus.reconcile import SqlitePositionStore

    path = tmp_path / "argus.db"
    writer = connect(path)
    apply_migrations(writer)
    audit = AuditLog(writer, tier2_queue_maxsize=8, tier2_queue_alert_depth=6)
    audit.start()
    reader = connect(path)
    expected = SqlitePositionStore(reader)

    # Default clock: stamped now, i.e. today.
    store = SqliteOrderStore(audit.write)
    await store.apply(_order(1))

    from argus.clock import et_today

    result = await reconcile_and_record(
        Reconciler(FakeBrokerAccount([]), expected),
        audit=_WriterAudit(audit, marker=et_today() - timedelta(days=1)),
        environment="sandbox",
        today=et_today(),
        session_settled=False,
        alert=lambda k, d: None,
        order_store=store,
    )

    assert expected.open_positions(), "today's ledger must NOT be cleared"
    assert result.ok is False, "and it must still read as a genuine mismatch"

    await audit.aclose()
    reader.close()
    writer.close()
