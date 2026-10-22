"""Expired-position ledger resolution (2026-08-12).

0DTE expiry produces no fill event, so the `positions` rows a held-to-expiry spread
left behind outlive the position itself and read as `missing_at_broker` at the next
reconcile — a process-wide hard halt, in sandbox and live, on the strategy's most
common winning outcome.

These tests are about the BOUNDARIES of the rule that stands that halt down, not
about the happy path. The load-bearing half is the negative direction: a row that
has not expired, or whose expiry cannot be read, must still halt exactly as it did
before. See `docs/decisions/2026-08-12-expired-position-resolution.md`.

**The same-day carve-out (2026-08-25)** adds a second dimension to every one of
those boundaries: today's expiry now resolves too, but ONLY once today's session
has closed and settled. So each case here is asserted against both settlement
states wherever the answer could differ — an evening restart on an expiry day
must resolve, and the identical ledger at 11:00 that morning must still halt.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from argus.account.fake import FakeBrokerAccount
from argus.account.models import Position
from argus.clock import ET
from argus.db import apply_migrations, connect
from argus.execution.models import LegAction, Order, OrderDirection, OrderLeg
from argus.execution.store import InMemoryOrderStore, SqliteOrderStore
from argus.marketcalendar import MarketCalendar
from argus.occ import occ_expiry, occ_strike
from argus.reconcile import (
    EXPIRED_AT_BROKER,
    KEPT_EXPIRY_UNREADABLE,
    KEPT_NOT_YET_EXPIRED,
    KEPT_SESSION_NOT_SETTLED,
    MISSING_AT_BROKER,
    QUANTITY_MISMATCH,
    SETTLED_AT_BROKER,
    UNEXPECTED_AT_BROKER,
    Discrepancy,
    InMemoryPositionStore,
    Reconciler,
    expiry_verdicts,
    reconcile_and_record,
)

TODAY = date(2026, 8, 12)
YESTERDAY = date(2026, 8, 11)

# Yesterday's expiry: the rows a held-to-expiry spread strands.
STALE_SHORT = "SPXW  260811P05000000"
STALE_LONG = "SPXW  260811P04995000"
# Today's expiry: a live 0DTE position, and the regression case.
TODAY_SHORT = "SPXW  260812P05000000"


def missing(symbol: str, qty: int = -1) -> Discrepancy:
    return Discrepancy(symbol, MISSING_AT_BROKER, expected_qty=qty, broker_qty=0)


# -- the OCC parser -----------------------------------------------------------


@pytest.mark.parametrize(
    "symbol,expected",
    [
        # Both spellings appear in captured data; which one an SDK call returns is
        # not something reconciliation may depend on.
        ("SPXW  260811P05000000", date(2026, 8, 11)),
        ("SPXW260811P05000000", date(2026, 8, 11)),
        ("SPXW  260811C05000000", date(2026, 8, 11)),
        ("SPX   261218P05000000", date(2026, 12, 18)),
    ],
)
def test_the_expiry_is_read_off_the_fixed_width_tail(symbol, expected):
    assert occ_expiry(symbol) == expected


@pytest.mark.parametrize(
    "symbol",
    [
        "",
        "SPXW",
        "SPXW  P5000",            # the short pseudo-symbol older tests use
        "SPXW  260811X05000000",  # not a right
        "SPXW  26081P05000000",   # five-digit date
        "SPXW  261311P05000000",  # month 13
        "SPXW  260230P05000000",  # 30 February
    ],
)
def test_an_unreadable_symbol_is_None_and_never_raises(symbol):
    """None means "cannot say", not "no expiry" — and it must not be able to take
    reconciliation itself down, which is why an impossible date returns rather
    than raising."""
    assert occ_expiry(symbol) is None


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("SPXW  260825C07695000", 7695.0),
        ("SPXW260825C07695000", 7695.0),      # unpadded root, same answer
        ("SPXW  260825C07692500", 7692.5),    # half strikes survive the /1000
        ("SPX   261218P05000000", 5000.0),
    ],
)
def test_the_strike_is_read_off_the_fixed_width_tail(symbol, expected):
    """Post-close scoring decides which LEG of a vertical a fill was, and an
    `order.fill` record carries the OCC symbol and nothing else about the
    instrument. Exact by construction: an integer parse over 1000, never a float
    parse of a decimal string."""
    assert occ_strike(symbol) == expected


@pytest.mark.parametrize("symbol", ["", "SPXW", "SPXW  P5000", "SPXW  260811X05000000"])
def test_an_unreadable_symbol_has_no_strike_either(symbol):
    """Same fail-closed contract as the expiry: "cannot say" is a value the caller
    branches on, and `_role_problems` records it rather than assuming a leg."""
    assert occ_strike(symbol) is None


# -- the pure rule ------------------------------------------------------------


@pytest.mark.parametrize("settled", [False, True])
def test_a_past_expiry_absent_at_the_broker_is_an_expected_resolution(settled):
    """A row from a PREVIOUS day resolves whatever time of day it is now.

    Parametrised over settlement to pin that down: `session_settled` is read only
    for a row expiring TODAY, so a yesterday row must not start depending on what
    time the process happened to restart.
    """
    (v,) = expiry_verdicts([missing(STALE_SHORT)], TODAY, session_settled=settled)
    assert v.resolved is True
    assert v.reason == EXPIRED_AT_BROKER
    assert v.expiry == YESTERDAY


def test_todays_expiry_is_NOT_resolved_while_the_session_could_still_hold_it():
    """The original regression case, now scoped to the part of the day it belongs to.

    Before settlement a 0DTE position can genuinely still be held, and the cost of
    being wrong in this direction is deleting Argus's record of a live one. "It is
    past 16:00" is still not evidence on its own — the broker is, and the clock
    only decides whether the broker's silence is CONCLUSIVE yet.
    """
    (v,) = expiry_verdicts([missing(TODAY_SHORT)], TODAY, session_settled=False)
    assert v.resolved is False
    assert v.reason == KEPT_SESSION_NOT_SETTLED


def test_todays_expiry_IS_resolved_once_todays_session_has_settled():
    """The evening-restart case (2026-08-25). SPXW is PM-settled, cash-settled and
    European: after settlement the position definitively cannot still be held, so
    the broker holding nothing is the expected 0DTE outcome and not a mismatch."""
    (v,) = expiry_verdicts([missing(TODAY_SHORT)], TODAY, session_settled=True)
    assert v.resolved is True
    assert v.reason == SETTLED_AT_BROKER
    assert v.expiry == TODAY


@pytest.mark.parametrize("settled", [False, True])
def test_a_future_expiry_is_never_resolved(settled):
    """Settlement of TODAY's session says nothing about a later expiry. This is the
    half the carve-out must not widen into: `>` today is still absolute."""
    (v,) = expiry_verdicts(
        [missing("SPXW  270101P05000000")], TODAY, session_settled=settled
    )
    assert v.resolved is False
    assert v.reason == KEPT_NOT_YET_EXPIRED


@pytest.mark.parametrize("settled", [False, True])
def test_an_unreadable_symbol_keeps_the_halt(settled):
    """Fail closed: a row Argus cannot identify is not a row it may delete — and a
    settled session does not make an unreadable symbol readable."""
    (v,) = expiry_verdicts([missing("SPXW  P5000")], TODAY, session_settled=settled)
    assert v.resolved is False
    assert v.reason == KEPT_EXPIRY_UNREADABLE
    assert v.expiry is None


@pytest.mark.parametrize("settled", [False, True])
@pytest.mark.parametrize("kind", [UNEXPECTED_AT_BROKER, QUANTITY_MISMATCH])
def test_only_missing_at_broker_rows_are_eligible(kind, settled):
    """The other two kinds are the opposite problem or evidence against expiry.

    `unexpected_at_broker` means the broker HOLDS something Argus did not expect —
    expiry says nothing about that. `quantity_mismatch` means the broker still holds
    some of it, so whatever happened, it did not expire away. Both must reach the
    halt untouched even when the symbol's expiry is long past, and a settled
    session must not make either eligible.
    """
    d = Discrepancy(STALE_SHORT, kind, expected_qty=-2, broker_qty=-1)
    assert expiry_verdicts([d], TODAY, session_settled=settled) == ()


def test_a_settled_session_never_resolves_a_position_the_broker_still_holds():
    """The half the carve-out is most easily mistaken for. Settlement widens WHICH
    EXPIRY counts as past; it does not touch broker-confirmed-never-clock-assumed.
    A quantity the broker still reports is still a mismatch at 21:00."""
    d = Discrepancy(TODAY_SHORT, QUANTITY_MISMATCH, expected_qty=-2, broker_qty=-1)
    assert expiry_verdicts([d], TODAY, session_settled=True) == ()


def test_the_verdict_payload_carries_the_whole_reasoning():
    (v,) = expiry_verdicts([missing(STALE_SHORT, -2)], TODAY, session_settled=False)
    assert v.to_payload() == {
        "symbol": STALE_SHORT,
        "expected_qty": -2,
        "expiry": YESTERDAY.isoformat(),
        "resolved": True,
        "reason": EXPIRED_AT_BROKER,
    }


# -- where `session_settled` comes from ---------------------------------------
#
# The rule above takes a bool; this is the boundary that produces it. It lives on
# `MarketCalendar` rather than beside the rule because 16:15 is only right on a
# NORMAL session — the calendar is the thing that knows a half-day closed at
# 13:00 — and because all three reconcile call sites must reach it identically.

_HALF_DAY = date(2026, 11, 27)   # day after Thanksgiving, in the support config


def _calendar() -> MarketCalendar:
    from support import make_config

    return MarketCalendar.from_config(make_config().market)


def _at(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=ET)


@pytest.mark.parametrize(
    "moment, settled",
    [
        (_at(TODAY, 16, 14, 59), False),   # one second early is early
        (_at(TODAY, 16, 15), True),        # INCLUSIVE at the mark, per house convention
        (_at(TODAY, 16, 15, 1), True),
        (_at(TODAY, 21, 2), True),         # the 2026-08-24 evening restart
        (_at(TODAY, 11, 0), False),        # mid-session, the case that must still halt
        (_at(TODAY, 16, 0), False),        # the close itself is not yet settlement
    ],
)
def test_the_settlement_mark_is_the_close_plus_fifteen_minutes(moment, settled):
    """16:15 ET, and inclusive at the boundary.

    The same number and the same reasoning as `tools/mark.py`'s
    `_EARLIEST_FEED_MARK_ET` — "a quote before this is not a settlement price" —
    deliberately reused rather than a second margin invented here.
    """
    assert _calendar().is_settled(TODAY, moment) is settled


@pytest.mark.parametrize(
    "moment, settled",
    [
        (_at(_HALF_DAY, 13, 14, 59), False),
        (_at(_HALF_DAY, 13, 15), True),
        (_at(_HALF_DAY, 16, 15), True),
    ],
)
def test_a_half_day_settles_fifteen_minutes_after_ITS_close(moment, settled):
    """The margin follows `close_time(d)`, not a literal 16:00. A hardcoded 16:15
    would refuse an evening restart for three extra hours on a half-day — safe,
    but wrong, and wrong in a way nobody would notice until a December release."""
    assert _calendar().is_settled(_HALF_DAY, moment) is settled


@pytest.mark.parametrize(
    "moment, settled",
    [
        (_at(YESTERDAY, 23, 59), False),   # the day before is never settled
        (_at(TODAY + timedelta(days=1), 0, 1), True),  # past midnight, still settled
    ],
)
def test_settlement_is_an_absolute_instant_not_a_time_of_day(moment, settled):
    """Asked about a date, answered against an instant — so a reconcile that
    straddles midnight cannot read tomorrow's 00:01 as "before 16:15"."""
    assert _calendar().is_settled(TODAY, moment) is settled


def test_the_shared_helper_asks_the_CALENDAR_never_a_literal():
    """`session.session_settled` is the ONE place the input is built — both of
    `session.py`'s reconcile call sites and `app.Application._reconcile` go
    through it. The clock is the CALLER's (`clock.et_now` here, the app's own
    injectable seam there), so this drives it with a fixed one."""
    from argus.session import session_settled
    from support import make_config

    config = make_config()
    assert session_settled(config, TODAY, _at(TODAY, 11, 0)) is False
    assert session_settled(config, TODAY, _at(TODAY, 21, 2)) is True

    # And it judges the date it was HANDED, not a second reading of the clock.
    assert session_settled(config, TODAY + timedelta(days=1), _at(TODAY, 21, 2)) is False


# -- the whole sequence -------------------------------------------------------


class _Audit:
    """Mirrors `test_sandbox_reset._Audit`: records, telemetry, and a fixed marker.

    `read` returns TODAY so `last_clean_reconciliation` reads as a same-trading-day
    marker and the sandbox reset REFUSES to clear — these tests must exercise the
    expiry rule, never be accidentally satisfied by the nightly wipe.
    """

    def __init__(self, marker: date | None = TODAY) -> None:
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

    @property
    def categories(self) -> list[str]:
        return [c for c, _ in self.records]

    @property
    def telemetry_categories(self) -> list[str]:
        return [c for c, _ in self.telemetry_records]

    def expiry_check(self) -> dict | None:
        """The Tier 2 `reconcile.expiry_check` payload, if one was written.

        Scoped rather than asserting on the whole telemetry list: the sandbox reset
        writes its own `reconcile.reset_check` on every no-clear branch, which is
        every run here.
        """
        for c, p in self.telemetry_records:
            if c == "reconcile.expiry_check":
                return p
        return None


def _order(order_id: int, short: str, long_: str) -> Order:
    return Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg(short, LegAction.SELL_TO_OPEN, 1, remaining_quantity=0),
            OrderLeg(long_, LegAction.BUY_TO_OPEN, 1, remaining_quantity=0),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=f"argus-open-{order_id}",
        candidate_id=f"cand-{order_id}",
        broker_order_id=order_id,
    )


async def _run(store: InMemoryOrderStore, *, broker: list[Position], audit: _Audit,
               environment: str = "sandbox", settled: bool = False):
    """Reconcile with the ledger's own positions as the expected set.

    `InMemoryPositionStore` is seeded FROM the order store, which is what the two
    really are in production: `FillConsumer` writes `positions` through the order
    store and `SqlitePositionStore` reads that same table back.

    `settled` defaults to FALSE — mid-session, the strictest state — so every test
    that does not name it is asserting the pre-carve-out behaviour and a
    same-day row still halts by default.
    """
    alerts: list[str] = []
    result = await reconcile_and_record(
        Reconciler(FakeBrokerAccount(broker), InMemoryPositionStore(store.positions)),
        audit=audit,
        environment=environment,
        today=TODAY,
        session_settled=settled,
        alert=lambda k, d: alerts.append(k),
        order_store=store,
    )
    return result, alerts


async def test_a_stranded_expired_spread_resolves_instead_of_halting():
    """The end-to-end point: the morning after a held-to-expiry win reconciles clean
    instead of hard-halting on rows nothing was ever going to remove."""
    audit = _Audit()
    store = InMemoryOrderStore(trading_date=YESTERDAY)
    await store.apply(_order(1, STALE_SHORT, STALE_LONG))
    assert store.positions, "precondition: the ledger holds yesterday's legs"

    result, alerts = await _run(store, broker=[], audit=audit)

    assert result.ok is True
    assert alerts == [], "an explained resolution is not a mismatch"
    assert store.positions == {}, "the stale rows are gone"

    payload = audit.category("reconcile.expired_positions")
    assert payload["trading_date"] == TODAY.isoformat()
    assert {v["symbol"] for v in payload["verdicts"]} == {STALE_SHORT, STALE_LONG}
    assert all(v["reason"] == EXPIRED_AT_BROKER for v in payload["verdicts"])
    # And the surviving verdict is what gets recorded and read back tomorrow.
    assert audit.category("reconcile.result")["ok"] is True


async def test_the_order_ledger_survives_the_position_resolution():
    """Scoped to `positions`, by design. `orders`/`order_legs` are the durable record
    of what was traded and the only place a spread's entry credit lives; order
    reconciliation never treats a recorded order as a discrepancy, so there is
    nothing to gain by deleting it and a P&L history to lose."""
    audit = _Audit()
    store = InMemoryOrderStore(trading_date=YESTERDAY)
    await store.apply(_order(1, STALE_SHORT, STALE_LONG))

    result, _ = await _run(store, broker=[], audit=audit)

    assert result.ok is True
    assert store.orders, "the order ledger is untouched"


async def test_a_position_expiring_TODAY_still_halts_before_settlement():
    """The regression test. This is the property the carve-out must not weaken: a
    0DTE row for today's expiry, absent at the broker, is still an unexplained
    mismatch for as long as the position could genuinely still be open."""
    audit = _Audit()
    store = InMemoryOrderStore(trading_date=TODAY)
    store.positions[TODAY_SHORT] = -1

    result, alerts = await _run(store, broker=[], audit=audit)

    assert result.ok is False
    assert result.discrepancies[0].kind == MISSING_AT_BROKER
    assert alerts == ["reconciliation_mismatch"]
    assert store.positions == {TODAY_SHORT: -1}, "nothing was deleted"
    # No Tier 1 record: nothing was resolved, so there is no change to explain. The
    # reasoning rides in Tier 2 next to the halt.
    assert "reconcile.expired_positions" not in audit.categories
    assert audit.expiry_check()["verdicts"][0]["reason"] == KEPT_SESSION_NOT_SETTLED
    assert audit.expiry_check()["session_settled"] is False


async def test_an_evening_restart_on_an_expiry_day_resolves_instead_of_halting():
    """The 2026-08-24 incident, end to end.

    A v0.3.8 deploy restarted the process at 21:02 ET on a day that held two 0DTE
    spreads to expiry. The broker held nothing (correctly — they settled at 16:00),
    Argus's four `positions` rows had nothing that would ever remove them, and the
    process hard-halted until a human waited for the ET date to roll over. Same
    ledger, same flat broker, same trading date — settled.
    """
    audit = _Audit()
    store = InMemoryOrderStore(trading_date=TODAY)
    await store.apply(_order(1, TODAY_SHORT, "SPXW  260812P04995000"))
    assert store.positions, "precondition: the ledger holds today's expired legs"

    result, alerts = await _run(store, broker=[], audit=audit, settled=True)

    assert result.ok is True
    assert alerts == [], "an explained resolution is not a mismatch"
    assert store.positions == {}
    payload = audit.category("reconcile.expired_positions")
    assert payload["session_settled"] is True
    assert all(v["reason"] == SETTLED_AT_BROKER for v in payload["verdicts"])
    assert store.orders, "and the order ledger is still untouched"


async def test_an_unreadable_symbol_still_halts():
    audit = _Audit()
    store = InMemoryOrderStore(trading_date=TODAY)
    store.positions["SPXW  P5000"] = -1

    result, alerts = await _run(store, broker=[], audit=audit)

    assert result.ok is False
    assert alerts == ["reconciliation_mismatch"]
    assert store.positions == {"SPXW  P5000": -1}
    assert audit.expiry_check()["verdicts"][0]["reason"] == KEPT_EXPIRY_UNREADABLE


async def test_only_the_explainable_rows_are_cleared_and_the_real_mismatch_halts():
    """The mixed case — the one that proves the rule is per-row and not per-run.

    Two stale legs from yesterday's expiry, plus a genuinely missing position that
    expires today. The stale pair is resolved; the real mismatch survives, the halt
    stands, and the alert still fires.
    """
    audit = _Audit()
    store = InMemoryOrderStore(trading_date=TODAY)
    await store.apply(_order(1, STALE_SHORT, STALE_LONG))
    store.positions[TODAY_SHORT] = -2

    result, alerts = await _run(store, broker=[], audit=audit)

    assert result.ok is False
    assert [d.symbol for d in result.discrepancies] == [TODAY_SHORT]
    assert alerts == ["reconciliation_mismatch"]
    assert store.positions == {TODAY_SHORT: -2}

    payload = audit.category("reconcile.expired_positions")
    resolved = {v["symbol"] for v in payload["verdicts"] if v["resolved"]}
    kept = {v["symbol"] for v in payload["verdicts"] if not v["resolved"]}
    assert resolved == {STALE_SHORT, STALE_LONG}
    assert kept == {TODAY_SHORT}, "the unexplained row is recorded, not silently dropped"


async def test_an_unexpected_broker_position_is_untouched_by_expiry():
    """The broker holding something Argus did not expect is the opposite problem, and
    a long-past expiry on the symbol must not launder it into an explained one."""
    audit = _Audit()
    store = InMemoryOrderStore(trading_date=TODAY)
    held = Position(STALE_SHORT, "SPXW", "Equity Option", -1, 1.0, 1.0)

    result, alerts = await _run(store, broker=[held], audit=audit)

    assert result.ok is False
    assert result.discrepancies[0].kind == UNEXPECTED_AT_BROKER
    assert alerts == ["reconciliation_mismatch"]
    # Not even eligible, so nothing is judged and nothing is recorded either way.
    assert audit.expiry_check() is None


async def test_a_clean_reconcile_records_nothing_extra():
    """Silent by design. A record on every clean morning would say nothing and sit in
    front of every session."""
    audit = _Audit()
    store = InMemoryOrderStore(trading_date=TODAY)

    result, _ = await _run(store, broker=[], audit=audit)

    assert result.ok is True
    assert audit.categories == ["reconcile.result"]
    assert audit.expiry_check() is None, "no missing rows, so nothing was judged"


async def test_the_resolution_is_distinguishable_from_the_sandbox_wipe():
    """Both remove ledger rows; they are not the same event and must never be read as
    one. The wipe is environment-gated and clears EVERYTHING (orders included) on a
    day rollover; this is row-scoped, environment-independent, and justified per row
    by that row's own expiry.
    """
    audit = _Audit()
    store = InMemoryOrderStore(trading_date=YESTERDAY)
    await store.apply(_order(1, STALE_SHORT, STALE_LONG))

    await _run(store, broker=[], audit=audit)

    assert "reconcile.expired_positions" in audit.categories
    assert "reconcile.sandbox_reset" not in audit.categories
    assert "reconcile.sandbox_reset" not in audit.telemetry_categories
    # Tier 1 (awaited `record`), like the wipe — a ledger that shrank is a
    # crash-relevant fact about expected state — but its own category, carrying
    # per-row reasoning the wipe's payload has no shape for.
    payload = audit.category("reconcile.expired_positions")
    assert "verdicts" in payload and "ledger" not in payload


@pytest.mark.parametrize("environment", ["sandbox", "live"])
async def test_the_rule_is_environment_independent(environment):
    """Unlike the sandbox wipe, which is gated on `environment == "sandbox"`. Expiry
    is a fact about the instrument, not about which broker endpoint is answering —
    and live is precisely where the stranded row would halt for real."""
    audit = _Audit()
    store = InMemoryOrderStore(trading_date=YESTERDAY)
    await store.apply(_order(1, STALE_SHORT, STALE_LONG))

    result, _ = await _run(store, broker=[], audit=audit, environment=environment)

    assert result.ok is True
    assert store.positions == {}


async def test_shadow_and_any_caller_without_an_order_store_is_skipped_entirely():
    audit = _Audit()
    result = await reconcile_and_record(
        Reconciler(
            FakeBrokerAccount([]), InMemoryPositionStore({STALE_SHORT: -1})
        ),
        audit=audit,
        environment="shadow",
        today=TODAY,
        # Settled, to prove the skip is about the missing ledger and nothing else.
        session_settled=True,
        alert=lambda k, d: None,
    )
    assert result.ok is False, "no ledger to write to means no resolution, and no halt excused"
    assert audit.telemetry_records == [], "not even the reset check runs without a store"


# -- the guarded delete -------------------------------------------------------


async def test_a_row_that_has_moved_since_the_compare_is_not_deleted():
    """`remove_positions` deletes only rows still holding the quantity the caller
    saw. A row that has moved is not the row the verdict reasoned about, so the
    discrepancy survives and the halt stands — fail-closed by construction."""
    store = InMemoryOrderStore()
    store.positions[STALE_SHORT] = -1
    removed = await store.remove_positions({STALE_SHORT: -2})
    assert removed == {}
    assert store.positions == {STALE_SHORT: -1}


async def test_an_incomplete_removal_keeps_the_halt_and_alerts():
    """The audit trail says a resolution was about to happen; if the ledger does not
    agree, the surviving discrepancy halts and the divergence is alerted rather than
    quietly absorbed."""

    class _StubbornStore(InMemoryOrderStore):
        async def remove_positions(self, expected):
            # Removes only the first of the two legs.
            first = next(iter(expected))
            return await super().remove_positions({first: expected[first]})

    audit = _Audit()
    store = _StubbornStore(trading_date=YESTERDAY)
    await store.apply(_order(1, STALE_SHORT, STALE_LONG))

    result, alerts = await _run(store, broker=[], audit=audit)

    assert result.ok is False
    assert "expired_position_removal_incomplete" in alerts
    assert "reconciliation_mismatch" in alerts


# -- the real SQLite path -----------------------------------------------------
#
# One test on real connections, for the same reason `test_sandbox_reset.py` keeps
# one: the delete runs on the process's WRITER connection while the expected-position
# set is read through a SEPARATE reader connection (`session._reconcile_or_abort`
# opens one per reconcile; WAL permits concurrent readers). A reader pinned to a
# pre-delete snapshot would report the removed rows, disagree with the flat broker,
# and refuse the day with the ledger already empty — invisible to fakes.


async def test_the_removal_is_visible_to_the_separate_reader_connection(tmp_path: Path):
    from argus.audit.log import AuditLog
    from argus.reconcile import SqlitePositionStore

    path = tmp_path / "argus.db"
    writer = connect(path)
    apply_migrations(writer)
    audit = AuditLog(writer, tier2_queue_maxsize=8, tier2_queue_alert_depth=6)
    audit.start()

    reader = connect(path)
    expected = SqlitePositionStore(reader)

    # Stamped TODAY so the sandbox reset refuses to clear (RESET_LEDGER_WRITTEN_TODAY)
    # and this test is really exercising the expiry rule.
    today_utc = (
        datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    store = SqliteOrderStore(audit.write, clock=lambda: today_utc)
    await store.apply(_order(1, STALE_SHORT, STALE_LONG))
    assert expected.open_positions(), "precondition: the reader sees the stale rows"

    result = await reconcile_and_record(
        Reconciler(FakeBrokerAccount([]), expected),
        audit=_WriterAudit(audit, marker=TODAY),
        environment="sandbox",
        today=TODAY,
        session_settled=False,
        alert=lambda k, d: None,
        order_store=store,
    )

    assert expected.open_positions() == {}, "the reader must see the resolved ledger"
    assert result.ok is True
    assert writer.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1

    await audit.aclose()
    reader.close()
    writer.close()


@pytest.mark.parametrize("settled", [False, True])
async def test_a_TODAY_expiry_on_the_real_path_turns_on_settlement(
    tmp_path: Path, settled: bool
):
    """Both directions of the carve-out on real connections.

    Unsettled is the safety direction and the one that must never regress: today's
    0DTE rows stay, and the day still refuses. Settled is the evening restart, and
    it has to survive the same separate-reader visibility the resolution path has
    always needed."""
    from argus.audit.log import AuditLog
    from argus.reconcile import SqlitePositionStore

    path = tmp_path / "argus.db"
    writer = connect(path)
    apply_migrations(writer)
    audit = AuditLog(writer, tier2_queue_maxsize=8, tier2_queue_alert_depth=6)
    audit.start()
    reader = connect(path)
    expected = SqlitePositionStore(reader)

    today_utc = (
        datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    store = SqliteOrderStore(audit.write, clock=lambda: today_utc)
    await store.apply(_order(1, TODAY_SHORT, "SPXW  260812P04995000"))

    result = await reconcile_and_record(
        Reconciler(FakeBrokerAccount([]), expected),
        audit=_WriterAudit(audit, marker=TODAY),
        environment="sandbox",
        today=TODAY,
        session_settled=settled,
        alert=lambda k, d: None,
        order_store=store,
    )

    if settled:
        assert expected.open_positions() == {}, "the reader must see the resolution"
        assert result.ok is True
        assert writer.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    else:
        assert expected.open_positions(), "today's 0DTE rows must NOT be removed"
        assert result.ok is False, "and it must still read as a genuine mismatch"

    await audit.aclose()
    reader.close()
    writer.close()


class _WriterAudit:
    """Real `AuditLog` for `write`/`record`, a fixed marker for `read`.

    Same shim as `test_sandbox_reset._WriterAudit`: `read` is stubbed so these tests
    stay about connection visibility rather than about the marker lookup.
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
