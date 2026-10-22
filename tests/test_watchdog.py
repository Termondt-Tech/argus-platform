"""The position risk watchdog — triggers, the debounce, and quote resolution.

No network and no clock: quotes come from `FakeMarketData`'s book shape via a
hand-built directory, and every age/sustain question is answered by an injected
clock that only moves when a test moves it. The exit handoff is covered in
`test_watchdog_exit.py`; here the executor is a recorder, so these tests are about
WHEN the watchdog acts.

The assertions that matter most are the ones a reader cannot check by eye:

  * that an OCC symbol is translated to a streamer symbol before a quote is looked
    up (get this wrong and the stop never fires, silently);
  * that "I cannot evaluate this" never reads as "no trigger";
  * that the debounce does not accumulate time across separate episodes;
  * that an unhandled exception halts the PROCESS rather than idling to tomorrow.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from datetime import date, datetime
from decimal import Decimal

import pytest

import argus.execution.watchdog as watchdog_module
from argus.audit.log import AuditWriteError
from argus.clock import ET
from argus.execution.store import CreditBasis, OpenSpread, OpenSpreadScan
from argus.execution.watchdog import (
    ExitOutcome,
    ExitReport,
    LegQuotes,
    PositionWatchdog,
    SpreadMarket,
    SustainTracker,
    TriggerReason,
    TriggerThresholds,
    _DEGRADED_PAGE_INTERVAL_S,
    _INSTANT_TRIGGER_REASONS,
    _MARKET_TRIGGER_REASONS,
    _SpentAttempt,
    evaluate_triggers,
)
from argus.reconcile import HaltGrade
from argus.execution.zerobid import PRICE_EPSILON
from argus.marketdata.models import OptionGreeks, Quote
from argus.signal.contracts import OptionContract
from tests.support import make_config

# OCC symbol (what an order references) vs streamer symbol (what DXLink is keyed
# by). They are DIFFERENT STRINGS, and that is the whole reason `LegQuotes` exists.
SHORT_OCC, SHORT_STREAM = "SPXW  260729P05000000", ".SPXW260729P5000"
LONG_OCC, LONG_STREAM = "SPXW  260729P04995000", ".SPXW260729P4995"
CALL_SHORT_OCC, CALL_SHORT_STREAM = "SPXW  260729C05100000", ".SPXW260729C5100"
CALL_LONG_OCC, CALL_LONG_STREAM = "SPXW  260729C05105000", ".SPXW260729C5105"


# -- harness ------------------------------------------------------------------


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.payloads: list[tuple[str, dict]] = []

    async def record(self, category, payload):
        self.events.append(category)
        self.payloads.append((category, payload))

    def telemetry(self, category, payload):
        self.events.append(f"tier2:{category}")
        self.payloads.append((category, payload))
        return True

    def payload(self, category: str) -> dict:
        for name, payload in self.payloads:
            if name == category:
                return payload
        raise AssertionError(f"no {category} record: {self.events}")


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


#: A `seconds_to_flatten` far enough out that the clock trigger is never among the
#: reasons. Every trigger/debounce test that is not ABOUT the flatten passes this, so
#: the flatten's own tests are the only place the mark is in play.
_LONG_BEFORE_FLATTEN = 4.0 * 3600.0

#: The trading day the fixtures' OCC symbols expire on. Every ET clock below is on it.
ET_SESSION_DAY = date(2026, 7, 29)          # a normal (16:00) close in the test calendar
ET_HALF_DAY = date(2026, 11, 27)            # in `tests.support`'s `market.half_days`


class FakeETClock:
    """Exchange wall-clock, injected. The watchdog reads it once per `check_once`.

    A SEPARATE seam from `FakeClock`, which is monotonic and cannot answer a date at
    all — the same split the watchdog itself makes between `clock` and `trading_date`.
    Pinned in every construction below rather than left to default to `clock.et_now`:
    with a real clock the flatten mark is 15:30 ET *today*, so the suite's behaviour
    would depend on what time of day it is run.
    """

    def __init__(self, day: date = ET_SESSION_DAY, hour: int = 11, minute: int = 0) -> None:
        self.now = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)

    def __call__(self) -> datetime:
        return self.now

    def at(self, hour: int, minute: int = 0) -> None:
        self.now = self.now.replace(hour=hour, minute=minute)

    @property
    def date(self) -> date:
        return self.now.date()


class FakeMarket:
    """The `MarketDataPort` surface `LegQuotes` uses, keyed by STREAMER symbol."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._quotes: dict[str, Quote] = {}
        self._greeks: dict[str, OptionGreeks] = {}

    def set(self, streamer_symbol: str, bid: float, ask: float, *, at: float | None = None):
        self._quotes[streamer_symbol] = Quote(
            symbol=streamer_symbol,
            bid=bid,
            ask=ask,
            bid_size=10.0,
            ask_size=10.0,
            received_at=self._clock.now if at is None else at,
        )

    def set_greeks(self, streamer_symbol: str, delta: float) -> None:
        self._greeks[streamer_symbol] = OptionGreeks(
            symbol=streamer_symbol,
            delta=delta,
            gamma=0.01,
            theta=-1.0,
            vega=0.5,
            implied_volatility=0.2,
            received_at=self._clock.now,
        )

    def drop(self, streamer_symbol: str) -> None:
        self._quotes.pop(streamer_symbol, None)

    def latest(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    def greeks(self, symbol: str) -> OptionGreeks | None:
        return self._greeks.get(symbol)

    def age(self, symbol: str) -> float:
        quote = self._quotes.get(symbol)
        return float("inf") if quote is None else max(0.0, self._clock.now - quote.received_at)


class FakeDirectory:
    def __init__(self, contracts: tuple[OptionContract, ...]) -> None:
        self._contracts = contracts

    @property
    def contracts(self) -> tuple[OptionContract, ...]:
        return self._contracts

    def replace(self, contracts: tuple[OptionContract, ...]) -> None:
        self._contracts = contracts


class FakeSpreadSource:
    def __init__(self, scan: OpenSpreadScan | None = None) -> None:
        self.scan = scan or OpenSpreadScan()
        self.calls = 0
        #: Every trading date the watchdog read the ledger as of.
        self.asked_for: list = []

    async def open_spreads(self, *, today) -> OpenSpreadScan:
        self.calls += 1
        self.asked_for.append(today)
        return self.scan


class RecordingExecutor:
    """Stands in for `ExitExecutor`: records the handoff, returns a scripted outcome."""

    def __init__(self, outcome: ExitOutcome = ExitOutcome.FILLED) -> None:
        self.calls: list[tuple[OpenSpread, frozenset[TriggerReason]]] = []
        #: `stub=` per call, index-aligned with `calls` (2026-09-24).
        self.stubs: list[bool] = []
        self.outcome = outcome
        self.raises: Exception | None = None
        self.bound = None
        self.scan_age = None

    def bind_quotes(self, quotes) -> None:
        self.bound = quotes

    def bind_scan_age(self, scan_age) -> None:
        self.scan_age = scan_age

    async def close(self, spread, market, reasons, *, stub=False) -> ExitReport:
        self.calls.append((spread, reasons))
        self.stubs.append(stub)
        if self.raises is not None:
            raise self.raises
        return ExitReport(
            outcome=self.outcome,
            external_identifier=f"argus-close-{spread.candidate_id}",
            reasons=tuple(sorted(reasons, key=str)),
        )


def contracts(*, call: bool = False) -> tuple[OptionContract, ...]:
    from datetime import date

    if call:
        return (
            OptionContract(CALL_SHORT_OCC, CALL_SHORT_STREAM, 5100.0, "call", date(2026, 7, 29)),
            OptionContract(CALL_LONG_OCC, CALL_LONG_STREAM, 5105.0, "call", date(2026, 7, 29)),
        )
    return (
        OptionContract(SHORT_OCC, SHORT_STREAM, 5000.0, "put", date(2026, 7, 29)),
        OptionContract(LONG_OCC, LONG_STREAM, 4995.0, "put", date(2026, 7, 29)),
    )


def spread(*, credit: str = "0.85", call: bool = False, contracts_held: int = 2) -> OpenSpread:
    short, long_ = (
        (CALL_SHORT_OCC, CALL_LONG_OCC) if call else (SHORT_OCC, LONG_OCC)
    )
    side = "call" if call else "put"
    return OpenSpread(
        external_identifier=f"argus-open-20260729-{side}-x",
        broker_order_id=1000,
        candidate_id=f"20260729-{side}-x",
        underlying="SPXW",
        short_symbol=short,
        long_symbol=long_,
        short_quantity=contracts_held,
        long_quantity=contracts_held,
        entry_credit=Decimal(credit),
    )


def build_quotes(clock: FakeClock, *, call: bool = False, max_age_s: float = 2.0):
    market = FakeMarket(clock)
    directory = FakeDirectory(contracts(call=call))
    quotes = LegQuotes(
        market, directory, underlying_symbol="SPX", max_age_s=max_age_s
    )
    return market, directory, quotes


def build_watchdog(
    *,
    clock: FakeClock,
    quotes,
    scan: OpenSpreadScan,
    executor: RecordingExecutor | None = None,
    config=None,
    broker_position_count=None,
    ledger_positions: int | None = None,
    confirm_broker_position_count=None,
    audit=None,
    alerts=None,
    alert=None,
    et_clock: FakeETClock | None = None,
    live_orders=None,
    #: The residual-order recheck (P5), stepped off the same `live_orders` answer.
    #: None everywhere it is not the subject, which is the pre-P5 behaviour.
    residuals=None,
    #: The shared session stop event, when a test asserts on it (2026-09-10).
    stop: asyncio.Event | None = None,
    #: The flatten deferral's ATM IV source (2026-09-25). None everywhere it is not
    #: the subject — which refuses every deferral, the pre-2026-09-25 behaviour.
    implied_vol=None,
):
    # `audit`/`alerts` are injectable so an integration test can put a REAL
    # `ExitExecutor` and this watchdog on ONE trail. Two separate FakeAudits would
    # split `watchdog.exit_finished` from `watchdog.exit_spent_still_held`, which is
    # precisely the sequence such a test is about.
    #
    # `alert` replaces the recorder outright, for the one test that needs a REAL
    # `SlackNotifier` on the other end: whether two positions page separately is a
    # question about the notifier's throttle, and a recording list cannot answer it.
    audit = audit if audit is not None else FakeAudit()
    halts: list[dict] = []

    async def halt(reason, *, source, detail=None, grade=None):
        halts.append(
            {"reason": reason, "source": source, "detail": detail, "grade": grade}
        )

    alerts = alerts if alerts is not None else []
    # Default: 11:00 ET on a normal-close day, i.e. 4.5 hours before the flatten mark,
    # so every test that is not about the flatten sees the two triggers it always saw.
    et_clock = et_clock if et_clock is not None else FakeETClock()
    watchdog = PositionWatchdog(
        spreads=FakeSpreadSource(scan),
        quotes=quotes,
        executor=executor or RecordingExecutor(),
        audit=audit,
        config=config or make_config(),
        halt=halt,
        alert=alert if alert is not None else (lambda k, d: alerts.append((k, d))),
        clock=clock,
        now_et=et_clock,
        # Read together with the clock above, never separately: the flatten mark is
        # built for THIS date and compared against THAT instant.
        trading_date=lambda: et_clock.date,
        broker_position_count=broker_position_count,
        ledger_state=(
            None
            if ledger_positions is None
            else _ledger_state_returning(ledger_positions)
        ),
        confirm_broker_position_count=confirm_broker_position_count,
        live_orders=live_orders,
        residuals=residuals,
        stop=stop,
        implied_vol=implied_vol,
    )
    return watchdog, audit, alerts, halts


def _ledger_state_returning(positions: int):
    from datetime import date as _date

    from argus.execution.store import LedgerState

    async def _state() -> LedgerState:
        return LedgerState(orders=1, positions=positions,
                           newest_trading_date=_date(2026, 8, 11))

    return _state


# -- the OCC -> streamer translation (the silent-failure guard) ---------------


def test_a_leg_is_priced_via_its_streamer_symbol_not_its_occ_symbol():
    """The bug this prevents: `market.latest(order_leg.symbol)` returns None forever,
    so the stop never fires and nothing ever says why."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 1.00, 1.10)

    # The OCC symbol is what the ORDER carries, and it is what the watchdog is asked
    # about — but the quote only exists under the streamer symbol.
    assert market.latest(SHORT_OCC) is None
    resolved = quotes.resolve(SHORT_OCC)
    assert resolved.ok
    assert (resolved.leg.bid, resolved.leg.ask) == (1.00, 1.10)
    assert resolved.leg.symbol == SHORT_OCC  # what an order must reference


def test_an_unknown_occ_symbol_says_so_rather_than_returning_nothing():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    resolved = quotes.resolve("SPXW  260729P00001000")
    assert not resolved.ok
    assert "not in the subscribed chain" in resolved.problem


def test_a_stale_quote_is_a_stated_problem_not_a_price():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock, max_age_s=2.0)
    market.set(SHORT_STREAM, 1.00, 1.10)
    clock.advance(3.0)

    resolved = quotes.resolve(SHORT_OCC)
    assert not resolved.ok
    assert "3.00s old" in resolved.problem


def test_a_missing_quote_is_distinguished_from_a_stale_one():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    assert "no quote yet" in quotes.resolve(SHORT_OCC).problem


def test_spot_is_fail_closed_on_staleness():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock, max_age_s=2.0)
    assert quotes.spot() is None
    market.set("SPX", 4990.0, 4991.0)
    assert quotes.spot() == pytest.approx(4990.5)
    clock.advance(5.0)
    assert quotes.spot() is None


def test_the_contract_index_is_invalidated_when_the_chain_is_reloaded():
    """Cached on the tuple's identity, which a reloaded chain replaces wholesale."""
    from datetime import date

    clock = FakeClock()
    market, directory, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 1.0, 1.1)
    assert quotes.resolve(SHORT_OCC).ok

    directory.replace(
        (OptionContract("SPXW  260730P05000000", ".SPXW260730P5000", 5000.0, "put",
                        date(2026, 7, 30)),)
    )
    assert not quotes.resolve(SHORT_OCC).ok  # yesterday's contract is gone
    market.set(".SPXW260730P5000", 1.0, 1.1)
    assert quotes.resolve("SPXW  260730P05000000").ok


# -- trigger 1: the credit multiple -------------------------------------------


def thresholds(**overrides) -> TriggerThresholds:
    base = dict(
        credit_multiple=2.0,
        underlying_buffer_points=5.0,
        sustained_s=2.0,
        profit_target_enabled=False,
        credit_trigger_enabled=True,
    )
    base.update(overrides)
    return TriggerThresholds(**base)


def test_the_credit_trigger_does_not_fire_at_exactly_the_multiple():
    """2026-09-01. The boundary is not a value this arithmetic can express.

    These four quotes are the fixture the whole of this file used to mean
    "triggered", and they are a live specimen of the defect: 2.10 - 0.40 comes out
    1.7000000000000002 against a stop of 1.7, so the trigger was firing on
    2.2e-16. The market state here is EXACTLY at the stop; the arithmetic's answer
    about which side of it that lands on is decided by rounding.

    So the rule is now "by an amount a price can express", and at the mark that
    means quiet. What it costs is the next achievable price up — a mid moves on a
    0.025 grid — and the test below spends it.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)          # far from the 5000 short put
    # Entered at 0.85; the stop is at 1.70. Cost to close = short mid - long mid.
    market.set(SHORT_STREAM, 2.00, 2.20)       # mid 2.10
    market.set(LONG_STREAM, 0.30, 0.50)        # mid 0.40  -> net 1.70, AT the stop
    held = spread(credit="0.85")

    evaluation = evaluate_triggers(held, quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert evaluation.cost_to_close == pytest.approx(1.70)
    assert evaluation.credit_stop == pytest.approx(1.70)
    # The raw comparison the old code did — still True, which is the whole point.
    assert evaluation.cost_to_close >= evaluation.credit_stop
    assert TriggerReason.CREDIT_MULTIPLE not in evaluation.true_reasons
    assert evaluation.fully_evaluable       # evaluated, and the answer is "no"


def test_the_credit_trigger_fires_one_grid_step_above_the_multiple():
    """What the tolerance costs, priced: the next mid a market can actually print."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 2.00, 2.25)       # mid 2.125 — a half-tick higher
    market.set(LONG_STREAM, 0.30, 0.50)        # net 1.725 > 1.70
    evaluation = evaluate_triggers(spread(credit="0.85"), quotes, thresholds(),
                                   seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert evaluation.cost_to_close == pytest.approx(1.725)
    assert TriggerReason.CREDIT_MULTIPLE in evaluation.true_reasons


def test_the_2026_09_01_pair_does_not_fire():
    """The verbatim numbers off the 09:34 page, against the comparison itself.

    `call-7670-7675`: cost_to_close 1.6000000000000005, credit_stop 1.6, entry
    credit 0.8. Realized -160.00 on a spread that expired worthless. 4.4e-16.
    """
    t = thresholds()
    assert t.credit_stop(Decimal("0.8")) == 1.6
    assert not t.credit_breached(cost_to_close=1.6000000000000005, credit_stop=1.6)
    # ... and the raw comparison it replaced said the opposite.
    assert 1.6000000000000005 >= 1.6


def test_the_credit_tolerance_is_far_below_any_real_price_and_far_above_the_noise():
    """The boundary, pinned in both directions, so neither can drift.

    Sub-epsilon is absorbed; one epsilon is a breach. The four-quote noise this
    exists to absorb is ~1e-16, and the smallest difference a spread mid can
    actually express is 0.025 — the tolerance has to be strictly between them and
    nowhere near either end.
    """
    t = thresholds()
    assert not t.credit_breached(cost_to_close=1.70, credit_stop=1.70)
    assert not t.credit_breached(cost_to_close=1.70 + PRICE_EPSILON / 2, credit_stop=1.70)
    # Two, not one: `(1.7 + 1e-6) - 1.7` is 9.999999999177334e-07 in binary, so the
    # boundary is not exactly representable EITHER. Same class of problem, and
    # exactly why the tolerance is placed orders of magnitude from both ends
    # rather than tuned to one of them — nothing here depends on its last digit.
    assert t.credit_breached(cost_to_close=1.70 + PRICE_EPSILON * 2, credit_stop=1.70)
    # Every price increment that can exist is a breach — down to a quarter tick,
    # which is already finer than a spread mid can move.
    for increment in (0.0125, 0.025, 0.05):
        assert t.credit_breached(cost_to_close=1.70 + increment, credit_stop=1.70)
    assert PRICE_EPSILON < 0.0125 / 100      # cannot mask a real move
    assert PRICE_EPSILON > 1e-12             # cannot be reached by float noise


def test_the_underlying_trigger_keeps_firing_at_exactly_the_mark():
    """The asymmetry is a decision, not an oversight (2026-09-02).

    `underlying_breached` deliberately does NOT take the credit tolerance. Its two
    sides come from unrelated places — an integral strike plus a configured buffer,
    against a continuously-moving index mid — so there is no equality-by-
    construction to remove, and a tolerance would only delay the backstop that
    exists for when the option market cannot be priced.
    """
    t = thresholds()                                   # buffer 5.0
    assert t.underlying_breached(side="put", spot=5005.0, short_strike=5000.0)
    assert t.underlying_breached(side="call", spot=4995.0, short_strike=5000.0)
    assert not t.underlying_breached(side="put", spot=5005.0 + 1e-9, short_strike=5000.0)


def test_the_credit_trigger_stays_quiet_one_tick_below_the_stop():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 1.95, 2.15)       # mid 2.05
    market.set(LONG_STREAM, 0.30, 0.50)        # net 1.65 < 1.70
    evaluation = evaluate_triggers(spread(credit="0.85"), quotes, thresholds(),
                                   seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert evaluation.true_reasons == frozenset()
    assert evaluation.fully_evaluable


def test_the_cost_to_close_uses_the_shared_zero_bid_floor():
    """`spread_mid` from `zerobid` — the same function that priced the entry, so the
    stop is measured against an arithmetically comparable number."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.0, 4.00)        # zero bid: floored to 0.05 -> mid 2.025
    market.set(LONG_STREAM, 0.0, 0.10)         # floored -> mid 0.075
    evaluation = evaluate_triggers(spread(credit="0.85"), quotes, thresholds(),
                                   seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert evaluation.cost_to_close == pytest.approx(2.025 - 0.075)


# -- trigger 2: the underlying backstop ---------------------------------------


def test_the_underlying_trigger_fires_when_spot_falls_toward_a_short_put():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    market.set("SPX", 5005.0, 5005.0)          # 5000 short put + 5pt buffer

    evaluation = evaluate_triggers(spread(), quotes, thresholds(underlying_buffer_points=5.0),
                                   seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE in evaluation.true_reasons
    assert TriggerReason.CREDIT_MULTIPLE not in evaluation.true_reasons


def test_the_underlying_trigger_fires_when_spot_rises_toward_a_short_call():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock, call=True)
    market.set(CALL_SHORT_STREAM, 0.50, 0.60)
    market.set(CALL_LONG_STREAM, 0.20, 0.30)
    market.set("SPX", 5095.0, 5095.0)          # 5100 short call - 5pt buffer

    evaluation = evaluate_triggers(
        spread(call=True), quotes, thresholds(underlying_buffer_points=5.0),
        seconds_to_flatten=_LONG_BEFORE_FLATTEN,
    )
    assert TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE in evaluation.true_reasons


def test_a_zero_buffer_triggers_only_when_the_strike_is_touched():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    zero = thresholds(underlying_buffer_points=0.0)

    market.set("SPX", 5000.5, 5000.5)
    assert evaluate_triggers(
        spread(), quotes, zero, seconds_to_flatten=_LONG_BEFORE_FLATTEN
    ).true_reasons == frozenset()
    market.set("SPX", 5000.0, 5000.0)
    assert TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE in evaluate_triggers(
        spread(), quotes, zero, seconds_to_flatten=_LONG_BEFORE_FLATTEN
    ).true_reasons


def test_the_side_comes_from_the_resolved_contract_not_from_a_parsed_string():
    """The contract is already in hand for the quote lookup and carries the strike
    and right together — no OCC parsing and no candidate-id parsing anywhere."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock, call=True)
    market.set(CALL_SHORT_STREAM, 0.50, 0.60)
    market.set(CALL_LONG_STREAM, 0.20, 0.30)
    market.set("SPX", 4000.0, 4000.0)  # far BELOW a short call: safe

    evaluation = evaluate_triggers(spread(call=True), quotes, thresholds(),
                                   seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert evaluation.true_reasons == frozenset()


# -- "cannot evaluate" is never "no trigger" ----------------------------------


def test_a_missing_leg_quote_makes_the_credit_trigger_unknown_not_false():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 2.05, 2.25)       # long leg has no quote

    evaluation = evaluate_triggers(spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert TriggerReason.CREDIT_MULTIPLE in evaluation.unknown_reasons
    assert TriggerReason.CREDIT_MULTIPLE not in evaluation.true_reasons
    assert not evaluation.fully_evaluable
    assert evaluation.market is None
    # No market, so no offer to report — "cannot say", never NaN (2026-09-17). This is
    # the REACHABLE `None`, and it is what a degraded tick looks like: 1,447 of
    # 2026-09-17's 19,626 live ticks had one leg dark.
    assert evaluation.crossing_debit is None
    assert evaluation.to_payload()["crossing_debit"] is None


def test_every_mark_carries_the_OFFER_beside_the_MID():
    """The number the `exit_walk_away_pct` pass needs, on every tick. (2026-09-17)

    `cost_to_close` is the zero-bid-floored MID; `crossing_debit` is the short leg's
    ask minus the long leg's bid — the price a market-crossing limit actually fills at.
    The gap between them is the only thing that says whether the ladder stops short of
    a resting offer, and before this it existed in the record ONLY when a cross had
    already fired: n=3 across 780,853 rows, right-censored by `min(debit, width)`.

    **Nothing decides on it.** It rides on the mark so the floor can be re-derived from
    live data later; reading it as a fillable price would repeat 2026-08-31's mistake
    in the other direction.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 2.00, 2.20)
    market.set(LONG_STREAM, 0.30, 0.50)

    evaluation = evaluate_triggers(
        spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN
    )
    # mid: 2.10 - 0.40 = 1.70.  offer: 2.20 - 0.30 = 1.90.  The ladder would have to
    # concede 0.20 to reach the market.
    assert evaluation.cost_to_close == pytest.approx(1.70)
    assert evaluation.crossing_debit == pytest.approx(1.90)
    payload = evaluation.to_payload()
    assert payload["cost_to_close"] == pytest.approx(1.70)
    assert payload["crossing_debit"] == pytest.approx(1.90)


def test_the_offer_is_RAW_and_never_zero_bid_floored():
    """`crossing_debit` deliberately does not apply the zero-bid floor: flooring the
    long bid UP to a tick would LOWER the debit and make the number look better than
    the market is. A genuine 0.00 bid on the long leg means it is worth nothing and we
    should expect nothing for it — so the offer is the full short ask."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.00, 0.05)
    market.set(LONG_STREAM, 0.00, 0.05)

    evaluation = evaluate_triggers(
        spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN
    )
    # THE FLATTEN'S MODAL REGIME, and the two numbers disagree completely: the floored
    # mid is 0.00 — which `_limit_price` cannot express, so no order is built at all —
    # while a real 0.05 offer is standing. That divergence is the 2026-08-31 finding
    # ("a mid of 0.00 can never reach the cross that would have priced it at 0.05"),
    # now VISIBLE in the trail on every tick instead of inferred after the fact.
    assert evaluation.cost_to_close == pytest.approx(0.00)
    assert evaluation.crossing_debit == pytest.approx(0.05)


def test_the_underlying_trigger_still_works_when_the_legs_cannot_be_priced():
    """The blown-out-spread case it is the backstop FOR. Spot survives an options
    feed gap, so the trigger that reads spot must survive it too."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 0.50, 0.60)       # short resolves (gives us the strike)
    market.set("SPX", 4990.0, 4990.0)          # through the 5000 short put

    evaluation = evaluate_triggers(spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE in evaluation.true_reasons
    assert TriggerReason.CREDIT_MULTIPLE in evaluation.unknown_reasons


def test_a_stale_short_quote_costs_the_price_not_the_strike():
    """THE 2026-08-13 regression. `resolve` looks the contract up in the chain index
    BEFORE it ever touches a quote, so strike and right are known whether or not the
    leg is priceable — and the underlying backstop needs only those and spot.

    Live: a ~55-point-OTM 0DTE call updating every few seconds went 2.0-2.9s stale
    against a 2.0s bound, and the position was left with NO trigger evaluable at
    all on 14% of ticks by the end of the session."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    clock.advance(2.9)                         # both legs now stale (max_age 2.0)
    market.set("SPX", 4990.0, 4990.0)          # spot is fresh, and through the strike

    resolution = quotes.resolve(SHORT_OCC)
    assert resolution.leg is None, "stale means unpriced"
    assert resolution.contract is not None and resolution.contract.strike == 5000.0
    assert "is 2.90s old" in (resolution.problem or "")

    evaluation = evaluate_triggers(spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE in evaluation.true_reasons
    assert TriggerReason.CREDIT_MULTIPLE in evaluation.unknown_reasons
    assert not evaluation.blind, "the backstop is live — this is degraded, not blind"


def test_a_symbol_outside_the_chain_is_still_genuinely_unresolved():
    """The other failure `LegResolution` exists to keep separate. No chain entry means
    no strike and no right, so the backstop really cannot be evaluated — and unlike a
    stale quote, nothing about waiting fixes it."""
    clock = FakeClock()
    market, directory, quotes = build_quotes(clock)
    market.set("SPX", 4990.0, 4990.0)
    directory.replace(())                      # chain emptied / never loaded

    resolution = quotes.resolve(SHORT_OCC)
    assert resolution.leg is None and resolution.contract is None
    assert "not in the subscribed chain" in (resolution.problem or "")

    evaluation = evaluate_triggers(
        spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN
    )
    # `_MARKET_TRIGGER_REASONS`, not the whole enum (2026-08-28). The flatten trigger
    # has no market input to be missing, so it is never unknown — which is exactly
    # why `blind` must not be measured against every reason.
    assert evaluation.unknown_reasons == _MARKET_TRIGGER_REASONS
    assert evaluation.blind, "no strike and no price is genuinely blind"
    assert any("not in the subscribed chain" in p for p in evaluation.problems)


def test_losing_spot_alone_is_degraded_when_the_legs_still_price():
    """Symmetry check: a dead SPX feed costs the backstop, and the credit trigger
    carries on. Only losing both is blind."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    evaluation = evaluate_triggers(spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert evaluation.unknown_reasons == frozenset({TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE})
    assert not evaluation.blind


def test_losing_spot_makes_the_underlying_trigger_unknown():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    evaluation = evaluate_triggers(spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE in evaluation.unknown_reasons
    assert "no fresh underlying quote" in evaluation.problems


# -- what it would COST to close, beside what it is marked at (2026-09-15) ----
#
# `cost_to_close` is a MARK and has never been a fillable price — the repo has
# learned that narratively twice, on 2026-08-31's flatten and in the 2026-08-20
# assessment's point that a widened spread is exactly where the `credit_multiple`
# math is least trustworthy. `crossing_debit` is the quantitative version: it was
# already computed (it is what `_cross_limit` acts on) and it had never reached a
# record. One field, because `crossing_debit - cost_to_close` is algebraically
# exactly the sum of the two half-spreads.


def test_the_mark_and_the_crossing_price_are_both_recorded():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 2.00, 2.20)       # mid 2.10, ask 2.20
    market.set(LONG_STREAM, 0.30, 0.50)        # mid 0.40, bid 0.30

    payload = evaluate_triggers(
        spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN
    ).to_payload()
    assert payload["cost_to_close"] == pytest.approx(1.70)    # 2.10 - 0.40
    assert payload["crossing_debit"] == pytest.approx(1.90)   # 2.20 - 0.30
    # The gap IS the friction: both half-spreads, 0.10 + 0.10, halved and summed.
    assert payload["crossing_debit"] - payload["cost_to_close"] == pytest.approx(0.20)
    json.dumps(payload, allow_nan=False)


def test_an_unpriceable_position_records_null_and_never_zero():
    """Same rule `cost_to_close` already holds, and the reason `/status` renders
    `unpriced` rather than a number. A 0.0 here would tell a reader the spread was
    free to cross on exactly the ticks where nothing could be seen at all."""
    clock = FakeClock()
    market, directory, quotes = build_quotes(clock)
    market.set("SPX", 4990.0, 4990.0)
    directory.replace(())                      # no chain, no legs, no market

    evaluation = evaluate_triggers(
        spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN
    )
    assert evaluation.blind and evaluation.market is None
    payload = evaluation.to_payload()
    assert payload["crossing_debit"] is None
    assert payload["cost_to_close"] is None
    json.dumps(payload, allow_nan=False)


def test_a_degraded_position_still_records_no_crossing_price():
    """One leg priceable is not a spread. `SpreadMarket` needs both, so a market
    that cannot be built is the same `None` — the credit trigger is unknown for
    exactly the same reason, and the two must not disagree."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 4990.0, 4990.0)
    market.set(SHORT_STREAM, 2.05, 2.25)       # the long leg has no quote

    payload = evaluate_triggers(
        spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN
    ).to_payload()
    assert payload["crossing_debit"] is None
    assert TriggerReason.CREDIT_MULTIPLE.value in payload["unknown_reasons"]


def test_an_unusable_cost_to_close_no_longer_takes_its_own_record_down():
    """The latent defect this change fixes on the way.

    `evaluate_triggers` already refuses a non-finite cost to close — it files a
    `problems` entry and marks the credit trigger unknown — and `to_payload` then
    recorded the raw value anyway. So in the ONE branch where the number is known
    to be unusable, the record that would have explained the refusal is the record
    that gets dropped (Tier 2) or raises (Tier 1). Unreachable through DXLink,
    which maps every non-finite side to 0.0; reachable through any other port, and
    the guard belongs to the payload boundary rather than to whichever adapter
    happens to be installed.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 2.00, float("inf"))
    market.set(LONG_STREAM, 0.30, 0.50)

    evaluation = evaluate_triggers(
        spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN
    )
    assert TriggerReason.CREDIT_MULTIPLE in evaluation.unknown_reasons
    assert any("not a usable number" in p for p in evaluation.problems)

    payload = evaluation.to_payload()
    assert payload["cost_to_close"] is None
    assert payload["crossing_debit"] is None
    json.dumps(payload, allow_nan=False)        # this is what used to raise
    # The problem text still names the raw value, so the evidence is not lost —
    # it just stops being a number the encoder has to accept.
    assert "inf" in payload["problems"][0]


async def test_every_record_of_a_check_carries_the_crossing_price():
    """ONE method, FOUR consumers. `watchdog.check` and `watchdog.check_spent` both
    splat `evaluation.to_payload()`, as do the evaluability page and `_trigger`, so
    a field added to the method reaches all four — which is the shape that cannot
    reproduce the 2026-09-14 failure, where a reader named one audit category and
    the runtime had since added another."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 2.00, 2.20)
    market.set(LONG_STREAM, 0.30, 0.50)
    watchdog, audit, _halts, _alerts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    await watchdog.check_once()

    payload = audit.payload("watchdog.check")
    assert payload["crossing_debit"] == pytest.approx(1.90)


# -- the debounce -------------------------------------------------------------


def test_one_observation_never_fires():
    clock = FakeClock()
    tracker = SustainTracker(2.0, clock)
    assert tracker.observe("k", true_reasons=frozenset({TriggerReason.CREDIT_MULTIPLE})) == frozenset()


def test_a_condition_held_for_the_full_duration_fires():
    clock = FakeClock()
    tracker = SustainTracker(2.0, clock)
    reasons = frozenset({TriggerReason.CREDIT_MULTIPLE})
    tracker.observe("k", true_reasons=reasons)
    clock.advance(1.9)
    assert tracker.observe("k", true_reasons=reasons) == frozenset()
    clock.advance(0.1)
    assert tracker.observe("k", true_reasons=reasons) == reasons


def test_a_condition_that_clears_resets_and_does_not_accumulate():
    """The whole point of the debounce: two 1.5s episodes are not one 3s episode."""
    clock = FakeClock()
    tracker = SustainTracker(2.0, clock)
    reasons = frozenset({TriggerReason.CREDIT_MULTIPLE})

    tracker.observe("k", true_reasons=reasons)
    clock.advance(1.5)
    tracker.observe("k", true_reasons=frozenset())      # a momentary blowout, gone
    clock.advance(0.1)
    tracker.observe("k", true_reasons=reasons)          # new episode starts here
    clock.advance(1.5)
    assert tracker.observe("k", true_reasons=reasons) == frozenset()
    clock.advance(0.5)
    assert tracker.observe("k", true_reasons=reasons) == reasons


def test_each_reason_has_its_own_window():
    """A run of the credit condition must not hand the underlying trigger a head
    start — "whichever fires first" is per-condition."""
    clock = FakeClock()
    tracker = SustainTracker(2.0, clock)
    credit = frozenset({TriggerReason.CREDIT_MULTIPLE})
    underlying = frozenset({TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE})

    tracker.observe("k", true_reasons=credit)
    clock.advance(1.9)
    assert tracker.observe("k", true_reasons=underlying) == frozenset()
    clock.advance(0.2)
    # The credit window was dropped when it went false; only the underlying one is
    # running, and it has been going 0.2s.
    assert tracker.observe("k", true_reasons=underlying) == frozenset()
    clock.advance(1.9)
    assert tracker.observe("k", true_reasons=underlying) == underlying


def test_an_undetermined_condition_neither_clears_nor_starts_a_window():
    """A stale quote is not evidence the condition went away — so the window is left
    exactly as it was. A blind gap counting toward the sustain is the fail-closed
    direction here: exiting is the protective act."""
    clock = FakeClock()
    tracker = SustainTracker(2.0, clock)
    credit = frozenset({TriggerReason.CREDIT_MULTIPLE})

    tracker.observe("k", true_reasons=credit)
    clock.advance(1.0)
    tracker.observe("k", true_reasons=frozenset(), unknown_reasons=credit)
    assert tracker.held_s("k", TriggerReason.CREDIT_MULTIPLE) == pytest.approx(1.0)
    clock.advance(1.1)
    assert tracker.observe("k", true_reasons=credit) == credit


def test_windows_are_per_position():
    clock = FakeClock()
    tracker = SustainTracker(2.0, clock)
    credit = frozenset({TriggerReason.CREDIT_MULTIPLE})
    tracker.observe("a", true_reasons=credit)
    clock.advance(2.1)
    tracker.observe("b", true_reasons=credit)
    assert tracker.observe("a", true_reasons=credit) == credit
    assert tracker.observe("b", true_reasons=credit) == frozenset()


def test_forgetting_a_closed_position_drops_its_windows():
    clock = FakeClock()
    tracker = SustainTracker(2.0, clock)
    tracker.observe("k", true_reasons=frozenset({TriggerReason.CREDIT_MULTIPLE}))
    assert tracker.tracking == 1
    tracker.forget("k")
    assert tracker.tracking == 0


# -- the watchdog loop --------------------------------------------------------


async def test_a_sustained_trigger_hands_the_exit_off_once():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)        # net 1.75 > 2 x 0.85
    held = spread(credit="0.85")
    executor = RecordingExecutor()
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(held,)), executor=executor
    )

    await watchdog.check_once()
    assert executor.calls == []                # debounce: one observation
    assert "tier2:watchdog.check" in audit.events

    clock.advance(2.5)                         # sustained_s is 2.0 in the test config
    market.set(SHORT_STREAM, 2.05, 2.25)       # keep the quotes fresh
    market.set(LONG_STREAM, 0.30, 0.50)
    market.set("SPX", 5100.0, 5100.0)
    await watchdog.check_once()
    await watchdog.stop()

    assert len(executor.calls) == 1
    assert executor.calls[0][1] == frozenset({TriggerReason.CREDIT_MULTIPLE})
    assert "watchdog.triggered" in audit.events
    assert any(k == "watchdog_triggered" for k, _ in alerts)


async def test_the_offer_an_exit_opens_against_is_on_the_triggering_row():
    """On the trigger's RISING edge, the triggering row carries the offer the exit
    opens against. (2026-09-18)

    `watchdog.check` is written only on NO-ACTION ticks, so the tick that starts an
    exit has no check row; `_trigger` splats `evaluation.to_payload()` into Tier 1
    `watchdog.triggered` BEFORE it hands `evaluation.market` to the executor, and the
    offer there is the one on the market the executor received.

    NOT a licence to drop the copy on `watchdog.exit_started`: past the rising edge the
    triggering row is Tier 2 `watchdog.triggered_still` — queued and droppable — so
    the join is not exact there. `exit_started` carries its own `crossing_debit`
    (`tests/test_watchdog_exit.py`).
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)

    def fresh() -> None:
        market.set("SPX", 5100.0, 5100.0)
        market.set(SHORT_STREAM, 2.05, 2.25)
        market.set(LONG_STREAM, 0.30, 0.50)    # mid 1.75 > 2 x 0.85; offer 1.95

    class Capturing(RecordingExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.handed = []
            self.events_at_handoff = None

        async def close(self, spread, market_, reasons, *, stub=False) -> ExitReport:
            self.handed.append(market_)
            self.events_at_handoff = list(audit.events)
            return await super().close(spread, market_, reasons, stub=stub)

    executor = Capturing()
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes,
        scan=OpenSpreadScan(spreads=(spread(credit="0.85"),)), executor=executor,
    )
    fresh()
    await watchdog.check_once()
    clock.advance(2.5)
    fresh()
    await watchdog.check_once()
    await watchdog.stop()

    assert len(executor.handed) == 1
    assert "watchdog.triggered" in executor.events_at_handoff  # recorded FIRST
    triggered = audit.payload("watchdog.triggered")
    assert triggered["crossing_debit"] == pytest.approx(1.95)
    assert triggered["crossing_debit"] == pytest.approx(executor.handed[0].crossing_debit)
    # The join key, and it sits at the SAME path on both rows — `watchdog.exit_started`
    # carries `position` too — so the join needs no translation between shapes.
    assert triggered["position"]["candidate_id"] == spread(credit="0.85").candidate_id


async def test_the_routine_no_action_check_is_tier_2_and_the_trigger_is_tier_1():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    await watchdog.check_once()
    assert audit.events == ["tier2:watchdog.check"]


async def test_an_exit_is_attempted_only_once_per_position():
    """A second closing order could stack on the first and close the spread twice,
    which does not flatten it — it re-opens it inverted."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 4990.0, 4990.0)          # through the short put, stays true
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    executor = RecordingExecutor(ExitOutcome.CROSS_REFUSED)   # not closed!
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)), executor=executor
    )

    for _ in range(4):
        clock.advance(2.5)
        market.set(SHORT_STREAM, 2.05, 2.25)
        market.set(LONG_STREAM, 0.30, 0.50)
        market.set("SPX", 4990.0, 4990.0)
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert len(executor.calls) == 1


async def test_a_triggered_position_that_cannot_be_priced_keeps_its_attempt():
    """The underlying trigger fired with no leg quotes: no order can be built, so
    this must be retryable rather than consuming the one attempt."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 0.50, 0.60)       # short only: strike known, no spread
    market.set("SPX", 4990.0, 4990.0)
    executor = RecordingExecutor()
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)), executor=executor
    )

    for _ in range(3):
        clock.advance(2.5)
        market.set(SHORT_STREAM, 0.50, 0.60)
        market.set("SPX", 4990.0, 4990.0)
        await watchdog.check_once()

    assert executor.calls == []
    # Three passes: the first is the debounce's opening observation, the next two
    # fire. Escalated on the RISING edge only, so both the trigger and the
    # unpriceable condition are ONE Tier 1 record each with Tier 2 repeats — a
    # position stuck in this state must not write a crash-critical record per tick.
    assert audit.events.count("watchdog.triggered") == 1
    assert audit.events.count("tier2:watchdog.triggered_still") == 1
    assert audit.events.count("watchdog.exit_unpriceable") == 1
    assert audit.events.count("tier2:watchdog.exit_unpriceable_still") == 1
    assert [k for k, _ in alerts].count("watchdog_triggered") == 1
    assert [k for k, _ in alerts].count("watchdog_exit_unpriceable") == 1

    # Quotes come back: the latched trigger now produces a real handoff.
    market.set(LONG_STREAM, 0.30, 0.50)
    await watchdog.check_once()
    await watchdog._drain_exits()
    assert len(executor.calls) == 1


# -- the bound on SILENCE for a triggered position with no order (2026-08-20) --
#
# Retrying forever is correct: giving up strands an open 0DTE position, and burning
# the one attempt on an order that was never sent is worse. What was NOT bounded is
# how long that can go on without saying so — rising-edge escalation pages once and
# then drops to Tier 2 telemetry indefinitely. Test config:
# exit_unattempted_max_s = 20.0, sustained_s = 2.0, cadence_s = 0.5.


def _triggered_and_priceable(clock, market):
    """Keep the credit trigger true AND the legs priceable, on a fresh tick."""
    market.set(SHORT_STREAM, 2.05, 2.25)       # mid 2.15 -> net 1.75 > 1.70
    market.set(LONG_STREAM, 0.30, 0.50)
    market.set("SPX", 5100.0, 5100.0)


async def test_an_exit_that_never_gets_built_pages_again_once_the_bound_passes():
    """Today's failure mode, generalized: construction refuses every tick, so after
    the first page the operator hears nothing more. It must page again."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    executor = RecordingExecutor(ExitOutcome.NOT_ATTEMPTED)
    watchdog, audit, alerts, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor,
    )

    for _ in range(3):                          # ~7.5s in: first page, then quiet
        clock.advance(2.5)
        _triggered_and_priceable(clock, market)
        await watchdog.check_once()
        await watchdog._drain_exits()
    assert audit.events.count("watchdog.exit_unattempted") == 1
    assert "watchdog.exit_still_unplaced" not in audit.events

    clock.advance(20.0)                         # past exit_unattempted_max_s
    _triggered_and_priceable(clock, market)
    await watchdog.check_once()
    await watchdog._drain_exits()

    payload = audit.payload("watchdog.exit_still_unplaced")
    assert payload["condition"] == "unattempted"
    assert payload["unresolved_for_s"] >= 20.0
    assert payload["bound_s"] == 20.0
    # Top level, not only inside `position`: this is the field `notify.alert_scope`
    # throttles on. It is the CANDIDATE, not either order identifier — the report
    # names the closing order (and may name nothing at all: NOT_ATTEMPTED built no
    # order), while the spread names the opening one.
    assert payload["candidate_id"] == spread().candidate_id
    assert [k for k, _ in alerts].count("watchdog_exit_still_unplaced") == 1
    # It pages; it does not halt. Halting sets the stop event, which would end the
    # retries that are the only thing still trying to close the position.
    assert halts == []
    assert len(executor.calls) > 1, "and it keeps retrying"


async def test_the_silence_bound_repeats_per_interval_never_per_tick():
    """It must NOT be once-only: a single page is one missed notification away from
    the silence this exists to prevent. It must also not be per-tick. One per
    `exit_unattempted_max_s` (20.0 in the test config) is the property.

    20 ticks of 5.0s = 100s of unresolved condition => 5 bounds crossed, 5 pages."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    executor = RecordingExecutor(ExitOutcome.NOT_ATTEMPTED)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor,
    )

    for _ in range(20):
        clock.advance(5.0)
        _triggered_and_priceable(clock, market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    pages = [k for k, _ in alerts].count("watchdog_exit_still_unplaced")
    assert audit.events.count("watchdog.exit_still_unplaced") == pages
    # Bounded: far fewer than the 20 ticks, and not the once-only version.
    assert 1 < pages <= 5, f"expected ~one page per 20s bound, got {pages}"


async def test_the_silence_bound_keeps_paging_while_the_position_stays_unclosed():
    """The specific regression: a slow phone response must not leave it silent.
    Ten minutes of a position that cannot be closed keeps producing pages."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    executor = RecordingExecutor(ExitOutcome.NOT_ATTEMPTED)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor,
    )

    for _ in range(60):                         # 60 x 10s = 10 minutes
        clock.advance(10.0)
        _triggered_and_priceable(clock, market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    pages = [k for k, _ in alerts].count("watchdog_exit_still_unplaced")
    assert pages >= 25, f"10 min at a 20s bound should keep paging, got {pages}"
    # Each one says how long, so the trail reads as a duration, not a repeat count.
    durations = [d["unresolved_for_s"] for k, d in alerts
                 if k == "watchdog_exit_still_unplaced"]
    assert durations == sorted(durations) and durations[-1] >= 570.0


async def test_the_unpriceable_path_is_bounded_the_same_way():
    """Same class of silence: the watchdog wants out and no order exists. The only
    difference is why, which is in the record's `condition`."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 0.50, 0.60)       # short only: strike known, no spread
    market.set("SPX", 4990.0, 4990.0)          # through the short put
    executor = RecordingExecutor()
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor,
    )

    for _ in range(4):
        clock.advance(10.0)
        market.set(SHORT_STREAM, 0.50, 0.60)
        market.set("SPX", 4990.0, 4990.0)
        await watchdog.check_once()

    assert executor.calls == []
    assert audit.payload("watchdog.exit_still_unplaced")["condition"] == "unpriceable"
    assert [k for k, _ in alerts].count("watchdog_exit_still_unplaced") == 1


async def test_a_resolved_episode_re_arms_the_bound():
    """The position stops being triggered, then trips again later: the second
    episode gets its own window and its own page."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set("SPX", 4990.0, 4990.0)
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
    )

    for _ in range(4):                          # episode 1: unpriceable, pages twice
        clock.advance(10.0)
        market.set(SHORT_STREAM, 0.50, 0.60)
        market.set("SPX", 4990.0, 4990.0)
        await watchdog.check_once()
    assert audit.events.count("watchdog.exit_still_unplaced") == 1

    market.set(LONG_STREAM, 0.30, 0.50)         # priceable again -> falling edge
    await watchdog.check_once()
    await watchdog._drain_exits()

    first = audit.payload("watchdog.exit_still_unplaced")["unresolved_for_s"]

    watchdog._attempted.clear()                 # pretend the exit never happened
    market.drop(LONG_STREAM)
    for _ in range(4):                          # episode 2: its own window
        clock.advance(10.0)
        market.set(SHORT_STREAM, 0.50, 0.60)
        market.set("SPX", 4990.0, 4990.0)
        await watchdog.check_once()

    assert audit.events.count("watchdog.exit_still_unplaced") == 2
    # A count alone would pass even on a PARTIAL reset. The duration is what proves
    # the new episode is measured from its own rising edge: episode 2's first page
    # must report roughly the same elapsed as episode 1's, not a total carried over.
    second = [d for c, d in audit.payloads
              if c == "watchdog.exit_still_unplaced"][1]["unresolved_for_s"]
    assert second == pytest.approx(first, abs=1e-6), (
        f"episode 2 reported {second}s against episode 1's {first}s — the window "
        "did not restart from its own rising edge"
    )


# -- and the bound has to be per POSITION, all the way to the phone (B1) -------
#
# `overdue` is per (position, condition) — `_Escalation` keys on the identifier —
# but the page it produces went through a notifier whose throttle keyed on `kind`
# ALONE. So the second position crossing its own bound inside the same 15 minutes
# was folded into the first position's suppressed count. That is the correlated
# case, not a tail: overlapping verticals are the documented common shape here, and
# one move down through both short strikes leaves both unclosable at once.
#
# This is the only watchdog test with a REAL `SlackNotifier` behind the alert seam,
# because the defect lives in the seam: with a recording list on the other end,
# every version of this passes.

# A second put vertical, chained above the first — a shared-ladder shape CLAUDE.md
# calls the common case for two same-side spreads.
UP_SHORT_OCC, UP_SHORT_STREAM = "SPXW  260729P05010000", ".SPXW260729P5010"
UP_LONG_OCC, UP_LONG_STREAM = "SPXW  260729P05005000", ".SPXW260729P5005"


def _chained_put_spread() -> OpenSpread:
    return OpenSpread(
        external_identifier="argus-open-20260729-put-y",
        broker_order_id=1001,
        candidate_id="20260729-put-y",
        underlying="SPXW",
        short_symbol=UP_SHORT_OCC,
        long_symbol=UP_LONG_OCC,
        short_quantity=2,
        long_quantity=2,
        entry_credit=Decimal("0.85"),
    )


async def test_two_positions_crossing_the_bound_together_both_reach_the_phone():
    """One move through both short strikes, both wings quiet, both unclosable. Two
    positions, two full pages — the second must not arrive as `(+1 suppressed)` on
    the first's next message up to fifteen minutes later."""
    from datetime import date as _date

    from argus.notify import SlackNotifier

    clock = FakeClock()
    market = FakeMarket(clock)
    directory = FakeDirectory(
        contracts()
        + (
            OptionContract(UP_SHORT_OCC, UP_SHORT_STREAM, 5010.0, "put", _date(2026, 7, 29)),
            OptionContract(UP_LONG_OCC, UP_LONG_STREAM, 5005.0, "put", _date(2026, 7, 29)),
        )
    )
    quotes = LegQuotes(market, directory, underlying_symbol="SPX", max_age_s=2.0)

    posts: list[str] = []
    notifier = SlackNotifier(
        "https://hooks.slack.com/services/T0/B1/xxx",
        environment="sandbox",
        post=lambda url, body, timeout: posts.append(json.loads(body)["text"]),
        throttle_s=900.0,
        clock=clock,
    )
    notifier.start()
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes,
        scan=OpenSpreadScan(spreads=(spread(), _chained_put_spread())),
        alert=notifier,
    )

    def _both_breached_and_unpriceable() -> None:
        # Short legs quote (so the strikes resolve), wings do not: triggered on the
        # underlying, and no exit can be built for either.
        market.set(SHORT_STREAM, 0.50, 0.60)
        market.set(UP_SHORT_STREAM, 0.70, 0.80)
        market.set("SPX", 4980.0, 4980.0)       # through BOTH short strikes

    for _ in range(4):                          # past the 20s bound
        clock.advance(10.0)
        _both_breached_and_unpriceable()
        await watchdog.check_once()
    await asyncio.wait_for(notifier.aclose(), timeout=2.0)

    # Both crossed their own bound and both were recorded Tier 1 — that part never
    # broke; the audit trail was always right.
    paged = [d for c, d in audit.payloads if c == "watchdog.exit_still_unplaced"]
    assert {d["candidate_id"] for d in paged} == {"20260729-put-x", "20260729-put-y"}
    # The regression: both must have reached the PHONE, in full, and be tellable
    # apart in a one-line notification preview where the kind is identical.
    unplaced = [t for t in posts if "watchdog_exit_still_unplaced" in t]
    assert len(unplaced) == 2, f"expected one page per position, got {posts}"
    assert sum("20260729-put-x" in t for t in unplaced) == 1
    assert sum("20260729-put-y" in t for t in unplaced) == 1
    assert not any("suppressed" in t for t in unplaced)


# -- the same bound, for a position whose one attempt is SPENT (2026-08-21, A2) --
#
# The end-to-end reproduction — trigger, order sent, status never confirms, spent
# attempt, repeating page — lives in test_watchdog_exit.py, over the real walk.
# What is worth pinning HERE is the part that decides whether the page is signal or
# noise: which spent outcomes reach it, and whether the slot survives a position
# leaving and re-entering the ledger.


async def _spend_the_attempt(watchdog, clock, market, outcome: ExitOutcome):
    """Trigger the position and let the (scripted) exit consume its one attempt."""
    watchdog._executor.outcome = outcome
    for _ in range(3):
        clock.advance(1.5)
        _triggered_and_priceable(clock, market)
        await watchdog.check_once()
        await watchdog._drain_exits()
    spent = watchdog._attempted[spread().external_identifier]
    assert (spent.outcome, len(watchdog._attempted)) == (outcome, 1)


async def test_a_clean_fill_that_books_never_pages():
    """The page must not fire for an exit that did its job. Nothing excludes FILLED
    by name — the position simply leaves `positions` on the closing fill and the next
    refresh drops it from the scan, taking `_escalation.forget` with it. The bound is
    what makes that safe: booking lag was 4-5s in live fire, well inside the 20s."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.FILLED)

    watchdog._spreads.scan = OpenSpreadScan()   # the fill books; the row is gone
    for _ in range(10):                         # 10 x 30s, far past the 20s bound
        clock.advance(30.0)
        await watchdog.check_once()

    assert "watchdog.exit_spent_still_held" not in audit.events
    assert not [k for k, _ in alerts if k == "watchdog_exit_spent_still_held"]


async def test_a_fill_whose_row_never_clears_does_page():
    """The deliberate corollary of not excluding FILLED by name. A closing fill that
    never books, or a partial close that left a stub, is a position with real risk and
    no trigger being evaluated — and no other rail says that at this granularity
    (`booked_but_absent` compares LEG COUNTS across the whole book)."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, audit, alerts, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.FILLED)

    clock.advance(25.0)                         # the row is STILL there
    await watchdog.check_once()

    assert audit.payload("watchdog.exit_spent_still_held")["exit_outcome"] == "filled"
    assert [k for k, _ in alerts].count("watchdog_exit_spent_still_held") == 1
    assert halts == []


async def test_the_page_carries_the_mark():
    """The phone gets both facts or neither is actionable: nothing will close this,
    and here is what it is worth. The page used to carry only the first."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.NOT_RESOLVED)

    clock.advance(25.0)
    _triggered_and_priceable(clock, market)
    await watchdog.check_once()

    detail = audit.payload("watchdog.exit_spent_still_held")
    assert detail["cost_to_close"] == pytest.approx(1.75)
    assert detail["credit_stop"] == pytest.approx(1.70)
    assert detail["still_triggered"] is True
    assert [k for k, _ in alerts if k == "watchdog_exit_spent_still_held"]


async def test_a_spent_position_that_leaves_the_ledger_and_returns_still_pages():
    """`_refresh_if_due` calls `_escalation.forget` for anything that leaves the scan,
    and `_attempted` deliberately keeps its key across that. Without the defensive
    re-arm the position would come back with the slot cleared, and `overdue` answers
    None for a slot that is not active — silent forever, which is the exact outcome
    this escalation exists to make impossible."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.NOT_RESOLVED)

    held_scan = watchdog._spreads.scan
    watchdog._spreads.scan = OpenSpreadScan()   # a read that cannot see it...
    clock.advance(30.0)
    await watchdog.check_once()
    assert "watchdog.exit_spent_still_held" not in audit.events
    watchdog._spreads.scan = held_scan          # ...and it is back

    for _ in range(3):
        clock.advance(30.0)
        await watchdog.check_once()

    assert audit.events.count("watchdog.exit_spent_still_held") >= 2, (
        "the episode did not re-arm — a spent position that flickers out of the "
        "ledger and back went permanently silent"
    )
    assert [k for k, _ in alerts if k == "watchdog_exit_spent_still_held"]


# -- a confirmed-empty broker is retried, under a budget (2026-08-28, P0) -------
#
# `NOTHING_AT_BROKER` is the outcome where the send failed AND the broker, asked
# under this identifier, named nothing. The one-attempt rule's safety argument —
# a second order could stack onto one we cannot see — does not apply to it, and
# applying it anyway cost a triggered 0DTE spread its only exit on 2026-08-28.


async def test_a_confirmed_empty_broker_does_not_spend_the_attempt():
    """It retries instead. The licence is `DuplicateOrderGuard`, which the exit walk
    already runs with — never this outcome's own answer."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOTHING_AT_BROKER),
    )

    # Three ticks: the trigger has to SUSTAIN before an exit is handed off at all.
    for _ in range(3):
        clock.advance(1.5)
        _triggered_and_priceable(clock, market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert watchdog._attempted == {}, "the position must keep its attempt"
    assert watchdog._sends[spread().external_identifier] == 1
    assert audit.payload("watchdog.exit_nothing_sent")["send_budget"] == 3
    assert [k for k, _ in alerts if k == "watchdog_exit_nothing_sent"]


async def test_the_retry_budget_is_finite():
    """Retryable is not infinite. Each retry costs a SUBMISSION, and a broker that
    fails every send while answering every read would otherwise be asked all
    afternoon — the runaway `ratelimit.py` exists to cap."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOTHING_AT_BROKER),
    )

    for _ in range(6):
        clock.advance(1.5)
        _triggered_and_priceable(clock, market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    key = spread().external_identifier
    assert watchdog._sends[key] == 3, "more sends went out than the budget allows"
    assert watchdog._attempted[key].outcome is ExitOutcome.NOTHING_AT_BROKER
    assert len(watchdog._executor.calls) == 3


# -- an unpriceable cross is retried too, on the SAME budget (2026-08-31) -------
#
# `CROSS_UNPRICEABLE` is unreachable except through `_await_settled` returning
# `outcome=None` — the broker NAMED the abandoned order and every disposition it gave
# for it was terminal. That is a POSITIVE confirmation, stronger than the absence
# `NOTHING_AT_BROKER` reports, and `_cross` already acts on it by submitting
# unguarded. On 2026-08-31 it spent a 0DTE spread's whole exit — the ordinary attempt
# and the flatten's one-shot override — in 1.3 seconds, on a short leg that had no
# offer for one moment.


async def test_an_unpriceable_cross_does_not_spend_the_attempt():
    """The prior order was confirmed dead; only the PRICE failed, and a feed with no
    offer a second ago may have one now."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.CROSS_UNPRICEABLE),
    )

    for _ in range(3):
        clock.advance(1.5)
        _triggered_and_priceable(clock, market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert watchdog._attempted == {}, "the position must keep its attempt"
    assert watchdog._sends[spread().external_identifier] == 1
    assert audit.payload("watchdog.exit_cross_unpriceable")["send_budget"] == 3
    # Its OWN tag and its own page: `_NOTHING_SENT` says look at the broker, this
    # one says look at the feed, and on a phone they read identically otherwise.
    assert [k for k, _ in alerts if k == "watchdog_cross_unpriceable"]
    assert not [k for k, _ in alerts if k == "watchdog_exit_nothing_sent"]


async def test_the_unpriceable_cross_shares_the_one_send_budget():
    """ONE budget, not one each. The hazard bounded is how many closing orders a
    broker sees from a position that will not close, and that is a single hazard
    however the attempt failed — so two burned on `NOTHING_AT_BROKER` leaves one,
    not another three."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOTHING_AT_BROKER),
    )
    key = spread().external_identifier

    for i in range(6):
        clock.advance(1.5)
        _triggered_and_priceable(clock, market)
        # The broker comes back; the feed goes away. Two sends, then the third.
        if i == 2:
            watchdog._executor.outcome = ExitOutcome.CROSS_UNPRICEABLE
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert watchdog._sends[key] == 3
    assert watchdog._attempted[key].outcome is ExitOutcome.CROSS_UNPRICEABLE
    assert len(watchdog._executor.calls) == 3


async def test_an_exhausted_unpriceable_cross_still_reaches_the_spent_page():
    """Retryable is not infinite, and the terminus is the one it always was."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.CROSS_UNPRICEABLE),
    )
    key = spread().external_identifier

    for _ in range(6):
        clock.advance(1.5)
        _triggered_and_priceable(clock, market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert watchdog._attempted[key].outcome is ExitOutcome.CROSS_UNPRICEABLE
    clock.advance(25.0)
    _triggered_and_priceable(clock, market)
    await watchdog.check_once()
    assert audit.payload("watchdog.exit_spent_still_held")["still_triggered"] is True
    assert [k for k, _ in alerts if k == "watchdog_exit_spent_still_held"]


async def test_a_REFUSED_cross_still_spends_the_attempt_on_the_first_try():
    """The one that must NOT change. `CROSS_REFUSED` shares a code path with
    `CROSS_UNPRICEABLE` and is its opposite: it is exactly the case where the
    abandoned order was NOT confirmed terminal, so a second closing order could stack
    on one that can still fill — which on expiry day re-opens the position inverted.
    It returns before `_cross_limit` is ever called, and it is not in
    `_RETRYABLE_EXITS`."""
    from argus.execution.watchdog import _RETRYABLE_EXITS

    assert ExitOutcome.CROSS_REFUSED not in _RETRYABLE_EXITS

    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.CROSS_REFUSED),
    )
    key = spread().external_identifier

    for _ in range(6):
        clock.advance(1.5)
        _triggered_and_priceable(clock, market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert watchdog._attempted[key].outcome is ExitOutcome.CROSS_REFUSED
    assert key not in watchdog._sends, "a refusal must not charge the retry budget"
    assert len(watchdog._executor.calls) == 1, "one attempt, as it has always been"


# -- the time-based flatten (2026-08-28, P2) -----------------------------------
#
# The third trigger, and the only one with no market input. It exists because both
# price triggers had already fired on 2026-08-28 and the position was still held 76
# minutes later: the thing that costs money is not failing to SEE the loss, it is
# being unable to ACT, and a second mechanism reading the same broken market is not a
# second mechanism.
#
# Test config: flatten_before_close_s = 1800.0, market close 16:00 / half-day 13:00,
# so the mark is 15:30 ET normally and 12:30 on a half-day.


def _quiet_and_priceable(market):
    """Both legs priceable, spot far from the strike: NO price trigger is true."""
    market.set(SHORT_STREAM, 0.50, 0.60)       # cost to close 0.30, stop is 1.70
    market.set(LONG_STREAM, 0.20, 0.30)
    market.set("SPX", 5100.0, 5100.0)          # short put 5000, 5pt buffer


def test_the_flatten_mark_follows_the_days_own_close_never_a_literal():
    """The half-day case, on the calendar where the number is built.

    A hardcoded 15:30 falls two and a half hours AFTER a half-day's 13:00
    settlement, so the control would never fire on the compressed sessions it is most
    needed on. `flatten_mark` anchors on `close_time(d)`, which is the same reason
    `settlement_mark` lives here.
    """
    from argus.marketcalendar import MarketCalendar

    calendar = MarketCalendar.from_config(make_config().market)

    assert calendar.flatten_mark(ET_SESSION_DAY, 1800.0) == datetime(
        2026, 7, 29, 15, 30, tzinfo=ET
    )
    assert calendar.flatten_mark(ET_HALF_DAY, 1800.0) == datetime(
        2026, 11, 27, 12, 30, tzinfo=ET
    ), "a half-day's flatten must be 30 minutes before ITS close, not before 16:00"
    # And it is genuinely the offset applied to that day's close, not a coincidence
    # of the two numbers chosen: a different offset moves both marks together.
    assert calendar.flatten_mark(ET_HALF_DAY, 900.0) == datetime(
        2026, 11, 27, 12, 45, tzinfo=ET
    )


async def test_the_same_instant_flattens_on_a_half_day_and_not_on_a_normal_one():
    """The half-day behaviour end to end, and the pair is the test — either half
    alone proves nothing. 12:35 ET is past the mark on a 13:00 close and nowhere near
    it on a 16:00 one, and the ONLY difference between these two runs is the date."""
    for day, should_flatten in ((ET_HALF_DAY, True), (date(2026, 11, 30), False)):
        clock = FakeClock()
        market, _, quotes = build_quotes(clock)
        _quiet_and_priceable(market)
        executor = RecordingExecutor()
        watchdog, _, _, _ = build_watchdog(
            clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
            executor=executor, et_clock=FakeETClock(day=day, hour=12, minute=35),
        )

        await watchdog.check_once()
        await watchdog._drain_exits()

        assert bool(executor.calls) is should_flatten, (
            f"{day}: expected flatten={should_flatten}"
        )
        if should_flatten:
            assert executor.calls[0][1] == frozenset(
                {TriggerReason.FLATTEN_BEFORE_CLOSE}
            ), "no price trigger was true; the clock alone must have fired it"


def test_the_loader_refuses_a_flatten_that_could_precede_a_half_days_open():
    """The cross-section rail. An offset at or beyond the SHORTEST session the
    calendar can produce puts the mark before the open on a half-day, and every
    position that day is born triggered — the one way this control can do harm rather
    than merely fail to fire. 09:40 -> 13:00 is 12,000s."""
    from argus.config import ConfigError

    make_config(watchdog={"flatten_before_close_s": 11999.0})   # accepted

    with pytest.raises(ConfigError, match="shortest session"):
        make_config(watchdog={"flatten_before_close_s": 12000.0})
    with pytest.raises(ConfigError, match="must be > 0"):
        make_config(watchdog={"flatten_before_close_s": 0.0})


def test_the_flatten_fires_on_one_observation_where_a_price_trigger_would_not():
    """The debounce is BYPASSED, and explicitly (`_INSTANT_TRIGGER_REASONS`).

    `sustained_s` exists to filter a quote artifact — "a 0DTE vertical's mid can print
    through 2x credit on one wide quote and come straight back". A clock reading is
    monotonic and has nothing to filter, so a sustain requirement could only delay the
    control. The contrast is the point: same tracker, same one observation.
    """
    clock = FakeClock()
    sustain = SustainTracker(2.0, clock)

    assert sustain.observe(
        "k", true_reasons=frozenset({TriggerReason.CREDIT_MULTIPLE})
    ) == frozenset(), "a price trigger must not fire on its first observation"
    assert sustain.observe(
        "k", true_reasons=frozenset({TriggerReason.FLATTEN_BEFORE_CLOSE})
    ) == frozenset({TriggerReason.FLATTEN_BEFORE_CLOSE})

    # The window is still OPENED, so `held_s` measures how long the flatten has been
    # due with the position still held — which is what the repeating page wants.
    # Recording the window and gating on it are separate concerns.
    clock.advance(45.0)
    sustain.observe("k", true_reasons=frozenset({TriggerReason.FLATTEN_BEFORE_CLOSE}))
    assert sustain.held_s("k", TriggerReason.FLATTEN_BEFORE_CLOSE) == 45.0
    assert TriggerReason.FLATTEN_BEFORE_CLOSE in _INSTANT_TRIGGER_REASONS


def test_the_flatten_is_never_unevaluable_so_blind_stays_reachable():
    """The consequence that would have silently retired a page (2026-08-28).

    `blind` — the ONE evaluability state that pages — was `unknown_reasons >=
    frozenset(TriggerReason)`. The flatten trigger needs no quote, no chain lookup and
    no feed, so it can never be unknown; measured against the full enum, `blind` would
    be False for every possible input and `watchdog.unevaluable` would stop firing
    with no test failing.
    """
    clock = FakeClock()
    market, directory, quotes = build_quotes(clock)
    directory.replace(())                       # no chain: no strike, no price
    # Nothing at all is readable, and the flatten mark is long past.
    evaluation = evaluate_triggers(
        spread(), quotes, thresholds(), seconds_to_flatten=-60.0
    )

    assert TriggerReason.FLATTEN_BEFORE_CLOSE not in evaluation.unknown_reasons
    assert TriggerReason.FLATTEN_BEFORE_CLOSE in evaluation.true_reasons
    assert evaluation.blind, (
        "both MARKET triggers are unevaluable — that a clock will fire later is not "
        "a substitute for watching the position now"
    )
    assert TriggerReason.FLATTEN_BEFORE_CLOSE not in _MARKET_TRIGGER_REASONS
    assert _MARKET_TRIGGER_REASONS < frozenset(TriggerReason)


# -- the allowance: a SPENT position is flattened, exactly once -----------------
#
# The sharpest part of P2. `_attempted` makes `check_once` never reach `_trigger`, so
# without an allowance the flatten would be inert for exactly the position that
# motivated it — one spent at 10:38 could not be flattened at 15:30.


async def test_a_spent_position_is_not_flattened_before_the_mark():
    """The one-attempt rule is untouched for the whole entry window and most of the
    session. The allowance is granted by the flatten FIRING, never by its existing."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    et_clock = FakeETClock(hour=11)
    executor = RecordingExecutor()
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=et_clock,
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.CROSS_REFUSED)
    assert len(executor.calls) == 1

    et_clock.at(15, 29)                          # one minute short of the mark
    for _ in range(5):
        clock.advance(1.5)
        _triggered_and_priceable(clock, market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert len(executor.calls) == 1, "a spent position must stay spent before the mark"


async def test_a_spent_position_gets_one_flatten_send_past_the_mark():
    """And it is ONE, however many ticks pass.

    Safe because the send goes out through `ExitExecutor.close`, whose walk runs
    `guard_first_submission=True` — so `DuplicateOrderGuard` re-asks the BROKER and
    blocks on WORKING (the stacking hazard), on FILLED, and on an order it cannot
    read, permitting only DEAD. `_attempted` blocks DEAD too, and DEAD is the only
    state this unblocks.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    et_clock = FakeETClock(hour=11)
    executor = RecordingExecutor()
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=et_clock,
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.CROSS_REFUSED)

    et_clock.at(15, 30)
    executor.outcome = ExitOutcome.CROSS_REFUSED
    for _ in range(6):
        clock.advance(1.5)
        # No price trigger is true — the clock alone drives this.
        _quiet_and_priceable(market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert len(executor.calls) == 2, "exactly one further send, then spent for good"
    assert executor.calls[1][1] == frozenset({TriggerReason.FLATTEN_BEFORE_CLOSE})

    # Overriding a stated non-negotiable is Tier 1 and pages, once per position.
    assert audit.events.count("watchdog.flatten_override") == 1
    override = audit.payload("watchdog.flatten_override")
    assert override["spent_outcome"] == str(ExitOutcome.CROSS_REFUSED)
    # Top level, so `notify.alert_scope` gives this position its own throttle (B1).
    assert override["candidate_id"] == spread().candidate_id
    assert [k for k, _ in alerts].count("watchdog_flatten_override") == 1


async def test_a_flatten_that_cannot_price_its_legs_spends_nothing_and_retries():
    """The honest limit of P2, pinned so nobody 'fixes' it by inventing a price.

    A limit at the spread WIDTH would survive a dead quote feed and is arithmetically
    wrong: a vertical can never settle for more than its width, so paying the width
    guarantees the maximum loss where holding realizes at most it. So an unpriceable
    flatten sends nothing, keeps its allowance, and retries the moment quotes return.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    et_clock = FakeETClock(hour=15, minute=30)
    executor = RecordingExecutor()
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=et_clock,
    )

    market.set("SPX", 5100.0, 5100.0)            # spot only: no leg can be priced
    for _ in range(4):
        clock.advance(1.5)
        market.set("SPX", 5100.0, 5100.0)
        await watchdog.check_once()
    assert executor.calls == []
    assert watchdog._flatten_attempted == set(), "an unsent order must spend nothing"
    # The page carries the runway: "unpriceable" reads differently with 25 minutes
    # left than with 25 minutes to go.
    assert audit.payload("watchdog.exit_unpriceable")["seconds_to_flatten"] == 0.0

    _quiet_and_priceable(market)                 # quotes return
    await watchdog.check_once()
    await watchdog._drain_exits()
    assert len(executor.calls) == 1


async def test_a_position_flattened_on_the_ordinary_path_gets_no_second_send():
    """A position that was never spent takes the ordinary path — no override record —
    and its post-mark allowance goes with that send. Otherwise a spread that triggers
    AT the mark would get two."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    executor = RecordingExecutor(ExitOutcome.CROSS_REFUSED)
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=FakeETClock(hour=15, minute=31),
    )

    for _ in range(6):
        clock.advance(1.5)
        _quiet_and_priceable(market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert len(executor.calls) == 1
    assert "watchdog.flatten_override" not in audit.events
    assert watchdog._attempted[spread().external_identifier].outcome is (
        ExitOutcome.CROSS_REFUSED
    )


# -- the allowance is spent by a SEND, never by a look (2026-09-02) -------------
#
# The gap between the two: `_trigger` claims the allowance once `evaluation.market is
# not None` — the legs have QUOTES — and construction can still refuse afterwards, in
# `_build_exit_order` -> `build.py`'s `_limit_price`, which will not express a net mid
# that rounds to 0.00 at the tick. That refusal is the flatten's MODAL regime (a
# winning 0DTE spread is nearly worthless by 15:30, which is what makes it winning) and
# was the observed result on 2 of 2 flattens on 2026-09-01. Harmless only because
# neither position had spent its ordinary attempt that day.


async def test_a_flatten_whose_order_cannot_be_built_keeps_its_override():
    """T1. The reported defect. `NOT_ATTEMPTED` means nothing was sent, so nothing was
    spent — the same sentence `_attempted` has always been read by, one allowance over.

    Distinct from `test_a_flatten_that_cannot_price_its_legs_spends_nothing_and_retries`
    above: there the legs never priced and `_trigger` returned before the spend, so the
    allowance survived by an accident of placement. Here the legs price cleanly and the
    ORDER is what cannot be built, which is the case that placement never covered.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    et_clock = FakeETClock(hour=11)
    executor = RecordingExecutor()
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=et_clock,
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.CROSS_REFUSED)

    et_clock.at(15, 30)
    executor.outcome = ExitOutcome.NOT_ATTEMPTED     # built no order; sent nothing
    key = spread().external_identifier
    for _ in range(4):
        clock.advance(1.5)
        _quiet_and_priceable(market)
        await watchdog.check_once()
        await watchdog._drain_exits()
        assert watchdog._flatten_attempted == set(), (
            "an order that was never built cannot have spent the override"
        )

    # And the trigger stays latched — which is what the repeated handoff demonstrates:
    # four flatten ticks each reached `_trigger` again on top of the one that spent the
    # ordinary attempt.
    assert len(executor.calls) == 5, "one look per tick while construction refuses"
    assert key not in watchdog._attempted or watchdog._attempted[key].outcome is (
        ExitOutcome.CROSS_REFUSED
    ), "and the ordinary attempt's record is untouched by any of it"


async def test_the_restored_override_is_spent_once_and_only_once():
    """T2. The trap the restore must not fall into: giving a look back is not granting
    a second send. Once construction succeeds, the allowance goes with THAT order and
    no later tick gets another."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    et_clock = FakeETClock(hour=11)
    executor = RecordingExecutor()
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=et_clock,
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.CROSS_REFUSED)

    et_clock.at(15, 30)
    executor.outcome = ExitOutcome.NOT_ATTEMPTED
    for _ in range(3):
        clock.advance(1.5)
        _quiet_and_priceable(market)
        await watchdog.check_once()
        await watchdog._drain_exits()
    assert len(executor.calls) == 4

    # The order builds. The allowance goes with it — and stays gone, whatever the
    # outcome was, because this one reached the broker.
    executor.outcome = ExitOutcome.NOT_RESOLVED
    for _ in range(6):
        clock.advance(1.5)
        _quiet_and_priceable(market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert len(executor.calls) == 5, "exactly one send survives the restore"
    assert watchdog._flatten_attempted == {spread().external_identifier}


async def test_a_spent_position_unpriceable_at_the_mark_still_sends_when_quotes_return():
    """T3. The 2026-09-01 shape end to end, and the regression this change is for.

    A position that arrives at 15:30 with `_attempted` already claimed — precisely the
    position the override exists for — gets its send when the market becomes
    expressible, instead of one unpriceable look and nothing for the rest of expiry day.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    et_clock = FakeETClock(hour=11)
    executor = RecordingExecutor()
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=et_clock,
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.CROSS_REFUSED)

    et_clock.at(15, 30)
    executor.outcome = ExitOutcome.NOT_ATTEMPTED     # the mid rounds to 0.00
    for _ in range(8):
        clock.advance(1.5)
        _quiet_and_priceable(market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    executor.outcome = ExitOutcome.FILLED            # a tick later it is expressible
    clock.advance(1.5)
    _quiet_and_priceable(market)
    await watchdog.check_once()
    await watchdog._drain_exits()

    assert executor.calls[-1][1] == frozenset({TriggerReason.FLATTEN_BEFORE_CLOSE})
    assert watchdog._attempted[spread().external_identifier].outcome is (
        ExitOutcome.FILLED
    ), "the override reached the market on the tick the market allowed it to"
    # Nothing was invented to get there: every refused tick sent nothing.
    assert "watchdog.exit_unattempted" in audit.events


async def test_an_exit_that_raises_hands_out_no_second_override():
    """T4. The other side of the restore, and the reason the SPEND stays at the handoff.

    A crash is an exception; `NOT_ATTEMPTED` is a returned value. Only the second
    reaches the restore, so a task that dies leaves the allowance spent — the
    fail-closed default — and the process halts on top of that.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    et_clock = FakeETClock(hour=11)
    executor = RecordingExecutor()
    watchdog, _, _, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=et_clock,
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.CROSS_REFUSED)

    et_clock.at(15, 30)
    executor.raises = RuntimeError("mid-exit")
    clock.advance(1.5)
    _quiet_and_priceable(market)
    await watchdog.check_once()
    await watchdog._drain_exits()

    assert watchdog._flatten_attempted == {spread().external_identifier}, (
        "a crash must never be able to GRANT an override; only a value may restore one"
    )
    assert halts, "and it halts"

    # Belt and braces: even if the loop kept running, the allowance is gone.
    executor.raises = None
    executor.outcome = ExitOutcome.FILLED
    for _ in range(4):
        clock.advance(1.5)
        _quiet_and_priceable(market)
        await watchdog.check_once()
        await watchdog._drain_exits()
    assert len(executor.calls) == 2


async def test_the_override_pages_once_per_position_not_once_per_tick():
    """T6. A second defect at the same site, and the reason this could not ship alone.

    The page is at the ELIGIBILITY site and the spend is one frame further on, past
    `_trigger`'s `market is None` early return — so a spent position past the mark whose
    legs cannot be priced never reaches the spend and re-qualifies every tick. Measured
    at 8 Tier 1 records for 8 ticks before the fix. It never fired in live fire because
    2026-09-01's flattens were refused at the BUILD rather than at the legs, so they
    spent the allowance and the spend silenced the repeat: the allowance bug was masking
    this one, and giving the allowance back un-masks it.

    `SlackNotifier`'s 900s throttle would have kept the phone to ~1 per 15 minutes, so
    what this actually costs is the AUDIT trail — a Tier 1 crash-critical record per
    cadence tick, which is what `_Escalation` exists to prevent.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    et_clock = FakeETClock(hour=11)
    executor = RecordingExecutor()
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=et_clock,
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.CROSS_REFUSED)

    et_clock.at(15, 30)
    for _ in range(8):
        clock.advance(3.0)                   # past max_age_s: the legs go stale
        market.set("SPX", 5100.0, 5100.0)    # spot only, so nothing can be priced
        await watchdog.check_once()
        await watchdog._drain_exits()
    assert len(executor.calls) == 1, (
        "nothing beyond the send that spent the ordinary attempt: `_trigger` returns at "
        "the `market is None` guard, so these ticks never even reach the spend"
    )

    # Then the legs return but the ORDER still cannot be built: the other half of the
    # repeat, which only exists once the allowance is restorable.
    executor.outcome = ExitOutcome.NOT_ATTEMPTED
    for _ in range(4):
        clock.advance(1.5)
        _quiet_and_priceable(market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert audit.events.count("watchdog.flatten_override") == 1
    assert [k for k, _ in alerts].count("watchdog_flatten_override") == 1
    # Suppressed, never silent — the repeats are Tier 2, on the same payload.
    assert audit.events.count("tier2:watchdog.flatten_override_still") == 11


async def test_a_spread_that_leaves_the_scan_and_returns_gets_no_fresh_override():
    """T7. The restore is keyed on a REPORT from the exit path, never on a spread's
    presence in a scan — so the scan-forget path still does not clear the allowance.

    That exclusion is deliberate and predates this change (`_attempted` and `_sends`
    share it): a `positions` row can vanish and return for reasons that are not a closed
    position — a partial close, booking lag, a leg the scan could not attribute — and a
    budget that resets on that is a budget that resets on noise.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    et_clock = FakeETClock(hour=11)
    executor = RecordingExecutor()
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=et_clock,
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.CROSS_REFUSED)

    et_clock.at(15, 30)
    executor.outcome = ExitOutcome.NOT_RESOLVED
    clock.advance(1.5)
    _quiet_and_priceable(market)
    await watchdog.check_once()
    await watchdog._drain_exits()
    assert len(executor.calls) == 2
    assert watchdog._flatten_attempted == {spread().external_identifier}

    watchdog._spreads.scan = OpenSpreadScan()        # the row flickers out ...
    watchdog.request_refresh()
    await watchdog.check_once()
    watchdog._spreads.scan = OpenSpreadScan(spreads=(spread(),))   # ... and back
    watchdog.request_refresh()
    for _ in range(4):
        clock.advance(1.5)
        _quiet_and_priceable(market)
        await watchdog.check_once()
        await watchdog._drain_exits()

    assert len(executor.calls) == 2, "the override does not come back with the row"
    assert watchdog._flatten_attempted == {spread().external_identifier}


def test_the_flatten_has_no_submission_path_of_its_own():
    """Structural. The flatten changes WHEN, never HOW: it reaches the market through
    the same `ExitExecutor.close` — and therefore the same guarded walk, the same
    walk-away escalation, and the same width-capped cross — as any other trigger.

    Skipping the walk for a direct cross was considered and rejected: `_cross`
    deliberately submits UNGUARDED because the walk already guarded and
    `_await_settled` already confirmed the abandoned order dead, neither of which is
    true with no walk in front of it. A second submission path on the exit side is how
    this module acquires a second way to double-close.
    """
    import ast

    tree, _ = _parse("watchdog.py")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    # The walk is asked to guard its first submission, in exactly one place, and the
    # answer is True. Counted on the AST rather than the text so the several
    # docstrings that (correctly) discuss the flag are not what is asserted.
    guarded = [
        kw
        for call in calls
        for kw in call.keywords
        if kw.arg == "guard_first_submission"
    ]
    assert len(guarded) == 1
    assert guarded[0].value.value is True

    # One handoff and one executor call, so the flatten cannot have grown its own.
    def _attr_calls(name: str) -> int:
        return sum(
            1
            for call in calls
            if isinstance(call.func, ast.Attribute) and call.func.attr == name
        )

    assert _attr_calls("close") == 1, "the exit reaches the market in ONE place"

    # And the handoff is one `create_task` inside `_trigger` — the watchdog's OWN
    # `run()` task is the other one in the module and is not an exit.
    trigger = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_trigger"
    )
    handoffs = [
        n
        for n in ast.walk(trigger)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create_task"
    ]
    assert len(handoffs) == 1, "the exit is handed off in ONE place"


# -- and the same state on `/status`, not only on the phone (2026-08-21, A2b) ---
#
# The page above was the ONLY surviving signal: `check_once` pops a spent position
# out of `_impaired` on the way past, so `/status`'s exit-triggers row lost it too
# and the phone view rendered a position with no automated path to close as an
# ordinary open position. `spent_exit_attempts()` is the live read that fixes it.


async def test_a_spent_attempt_is_reported_live_while_the_position_is_still_held():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.NOT_RESOLVED)

    # Nothing is unevaluable here — the legs price cleanly, and since 2026-08-28 a
    # spent position is still evaluated, so this row is empty on its merits rather
    # than because the position was dropped from it.
    assert watchdog.impaired_positions() == ()
    # And it is in this one, naming what it needs a human for.
    (spent,) = watchdog.spent_exit_attempts()
    assert spent.external_identifier == spread().external_identifier
    assert spent.candidate_id == "20260729-put-x"
    assert spent.outcome == "not_resolved"
    assert spent.contracts == spread().contracts
    assert spent.spent_for_s == 0.0

    clock.advance(900.0)
    assert watchdog.spent_exit_attempts()[0].spent_for_s == 900.0


async def test_a_spent_position_is_still_marked_every_tick():
    """P1, 2026-08-28. The row said a position could not be closed and could not say
    what it was worth — the half a human needs to decide whether to leg out by hand.

    The mark MOVES, which is the whole point: the position that went 76 minutes
    unpriced had its underlying come back eight points in its favour, quite possibly
    back inside the stop, and nothing was measuring."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.NOT_RESOLVED)

    # The attempt is spent on the tick that handed the exit off, so there is no mark
    # until the NEXT one. Reported as None rather than as a number nobody measured.
    assert watchdog.spent_exit_attempts()[0].still_triggered is None

    clock.advance(1.5)
    _triggered_and_priceable(clock, market)
    await watchdog.check_once()

    (spent,) = watchdog.spent_exit_attempts()
    assert spent.cost_to_close == pytest.approx(1.75)
    assert spent.credit_stop == pytest.approx(1.70)
    assert spent.still_triggered is True

    # The market comes back in. Nothing acts on it — but it is now VISIBLE.
    clock.advance(1.5)
    market.set(SHORT_STREAM, 1.00, 1.10)
    market.set(LONG_STREAM, 0.30, 0.50)
    market.set("SPX", 5100.0, 5100.0)
    await watchdog.check_once()

    (spent,) = watchdog.spent_exit_attempts()
    assert spent.cost_to_close == pytest.approx(0.65)
    assert spent.still_triggered is False
    # The trail carries the same number, from the same computation — a spent
    # position is Tier 2 on every tick, like any other check.
    marks = [p for name, p in audit.payloads if name == "watchdog.check_spent"]
    assert marks[-1]["cost_to_close"] == pytest.approx(0.65)
    assert marks[-1]["exit_outcome"] == "not_resolved"


async def test_a_spent_position_that_cannot_be_priced_reports_no_mark():
    """None, never a placeholder. On a row whose entire purpose is telling a human
    what to do by hand, a 0.0 cost-to-close is not a missing value — it is a wrong
    one, and it is the kind that gets acted on."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.NOT_RESOLVED)

    clock.advance(30.0)          # every quote is now stale
    await watchdog.check_once()

    assert watchdog.spent_exit_attempts()[0].cost_to_close is None


async def test_a_clean_fill_that_books_is_not_reported_as_spent():
    """FILLED spends the attempt like every other outcome and nothing excludes it by
    name. It must not show here anyway: the closing fill takes the row out of
    `positions` and the scan is what this read is driven by. A row that lingers past
    the bound is a different matter and pages — see the test above."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.FILLED)
    # Briefly true and correct: the ledger still carries the row, and a fill that
    # never books is exactly the case the page exists for.
    assert len(watchdog.spent_exit_attempts()) == 1

    watchdog._spreads.scan = OpenSpreadScan()   # the fill books; the row is gone
    watchdog.request_refresh()
    await watchdog.check_once()
    assert watchdog.spent_exit_attempts() == ()


async def test_a_closed_position_does_not_stay_on_the_page_forever():
    """`_attempted` is deliberately never pruned — clearing a key on scan-exit would
    hand a second attempt to a spread that flickered. So this read is driven by the
    SCAN and filtered by `_attempted`, never the reverse: iterating `_attempted`
    would turn the row into a process-lifetime tally of everything that ever ended
    badly, which is the failure mode this page has already had once."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.CROSS_REFUSED),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.CROSS_REFUSED)
    assert len(watchdog.spent_exit_attempts()) == 1

    watchdog._spreads.scan = OpenSpreadScan()   # a human closed it by hand
    watchdog.request_refresh()
    await watchdog.check_once()
    clock.advance(3600.0)
    assert watchdog.spent_exit_attempts() == ()
    assert watchdog._attempted, "the one-attempt rule itself must be untouched"


async def test_the_spent_duration_is_measured_from_the_attempt_not_the_episode():
    """The defensive re-arm in `_page_if_spent_and_still_held` restarts the
    `_Escalation` episode when a spread flickers out of the ledger and back — it has
    to, or the position goes silent. That is a CADENCE, and it must not silently
    restart the duration either channel reports: the attempt was spent once, and
    "45 minutes ago" is what the operator acts on. Both readers take it from
    `_SpentAttempt.since`, so they also cannot disagree with each other."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.NOT_RESOLVED)

    held_scan = watchdog._spreads.scan
    watchdog._spreads.scan = OpenSpreadScan()   # out of the ledger: episode forgotten
    clock.advance(600.0)
    await watchdog.check_once()
    watchdog._spreads.scan = held_scan          # and back, 10 minutes later
    watchdog.request_refresh()
    clock.advance(30.0)
    await watchdog.check_once()                 # re-arms the episode at 0s: no page
    assert "watchdog.exit_spent_still_held" not in audit.events

    clock.advance(30.0)                         # past the 20s bound of episode 2
    await watchdog.check_once()

    live = watchdog.spent_exit_attempts()[0]
    assert live.spent_for_s == 660.0, "the flicker restarted the reported duration"
    assert audit.payload("watchdog.exit_spent_still_held")["held_for_s"] == 660.0, (
        "the page reported the re-armed episode's age, not the age of the attempt"
    )


# -- `_Escalation.overdue` directly: counter scope and the exact boundary -------
#
# Same discipline as the 1.313 / 2.001 bracket on the freshness gate. A multiple
# counter is easy to get silently wrong in two ways — keyed on the wrong lifetime
# scope, or fragile at the boundary it is defined by — so both are pinned on the
# primitive rather than inferred from watchdog-level behaviour.


def _esc():
    from argus.execution.watchdog import _Escalation

    clock = FakeClock()
    return _Escalation(clock), clock


def test_overdue_fires_exactly_at_the_boundary_not_a_hair_before():
    esc, clock = _esc()
    esc.rising("k", "tag")

    clock.advance(19.999)
    assert esc.overdue("k", "tag", 20.0) is None, "must not fire early"
    clock.advance(0.001)                                   # exactly 20.000
    assert esc.overdue("k", "tag", 20.0) == 20.0, "must fire ON the boundary"
    clock.advance(0.001)
    assert esc.overdue("k", "tag", 20.0) is None, "and not twice for one interval"

    clock.advance(19.998)                                  # 39.999
    assert esc.overdue("k", "tag", 20.0) is None
    clock.advance(0.001)                                   # exactly 40.000
    assert esc.overdue("k", "tag", 20.0) == 40.0, "second interval, same rule"


def test_accumulated_float_drift_delays_by_at_most_one_tick_and_skips_nothing():
    """`//` on an accumulated float can land a hair under the boundary. That is
    tolerable — one tick late, never early — but it must NOT accumulate into
    unbounded drift or drop an interval, which is what a "seconds since last page"
    formulation would have done."""
    esc, clock = _esc()
    esc.rising("k", "tag")

    fired_at_tick = []
    for tick in range(1, 1001):                            # 100s of 0.1s ticks
        clock.advance(0.1)
        if esc.overdue("k", "tag", 20.0) is not None:
            fired_at_tick.append(tick)

    ideal = [200, 400, 600, 800, 1000]
    assert len(fired_at_tick) >= 4, f"intervals were dropped: {fired_at_tick}"
    lateness = [got - want for got, want in zip(fired_at_tick, ideal)]
    assert all(0 <= n <= 1 for n in lateness), f"drift is not bounded: {lateness}"
    assert lateness == sorted(lateness) and max(lateness) <= 1, (
        f"lateness accumulates instead of staying bounded: {lateness}"
    )


def test_a_long_gap_collapses_to_one_page_carrying_the_true_elapsed():
    """An irregular tick rate must not produce a backlog of stale pages. One report
    naming how long it has actually been is the useful thing to wake someone with."""
    esc, clock = _esc()
    esc.rising("k", "tag")

    clock.advance(20.0)
    assert esc.overdue("k", "tag", 20.0) == 20.0
    clock.advance(80.0)                                    # four intervals in one gap
    assert esc.overdue("k", "tag", 20.0) == 100.0, "one page, true elapsed"
    clock.advance(20.0)
    assert esc.overdue("k", "tag", 20.0) == 120.0, "and the cadence resumes"


def test_the_multiple_counter_restarts_from_1x_on_a_new_episode():
    """The counter is keyed to an EPISODE, not to the process. A count that outlived
    its episode would silently make the next one's first page late — or absent."""
    esc, clock = _esc()
    esc.rising("k", "tag")
    clock.advance(60.0)
    assert esc.overdue("k", "tag", 20.0) == 60.0           # counter now at 3x

    esc.falling("k", "tag")
    esc.rising("k", "tag")                                 # new episode
    clock.advance(19.999)
    assert esc.overdue("k", "tag", 20.0) is None, "restarted window, not yet due"
    clock.advance(0.001)
    assert esc.overdue("k", "tag", 20.0) == 20.0, (
        "the new episode must page at ITS first 1x, not inherit a 3x count"
    )


def test_overdue_counters_do_not_leak_across_positions_or_conditions():
    """Scope is (key, tag). One position's long-running condition must not consume
    another's first page, and neither must the other CONDITION on the same position."""
    esc, clock = _esc()
    esc.rising("pos-a", "unattempted")
    clock.advance(60.0)
    assert esc.overdue("pos-a", "unattempted", 20.0) == 60.0

    # A different position, and a different condition on the same one, both start
    # their own count — each pages at its own first 1x.
    esc.rising("pos-b", "unattempted")
    esc.rising("pos-a", "unpriceable")
    clock.advance(20.0)
    assert esc.overdue("pos-b", "unattempted", 20.0) == 20.0
    assert esc.overdue("pos-a", "unpriceable", 20.0) == 20.0


def test_forget_clears_the_counter_with_the_episode():
    """`forget` is what runs when a spread leaves the scan. It must clear the count
    too, or a re-entry of the same trade idea inherits a spent one."""
    esc, clock = _esc()
    esc.rising("k", "tag")
    clock.advance(60.0)
    assert esc.overdue("k", "tag", 20.0) == 60.0

    esc.forget("k")
    esc.rising("k", "tag")
    clock.advance(20.0)
    assert esc.overdue("k", "tag", 20.0) == 20.0


def test_overdue_is_none_for_a_condition_that_is_not_up():
    esc, clock = _esc()
    clock.advance(1000.0)
    assert esc.overdue("never-risen", "tag", 20.0) is None


# -- live evaluability for /status (added 2026-08-13) -------------------------
#
# The phone view structurally cannot read this from the audit tail: payloads sort
# their keys, `unknown_reasons` sorts LAST, and the HTML tail truncates at 240
# chars, so a degraded position's reasons are always cut. This is the live read
# that sidesteps it — sourced from the same TriggerEvaluation as the audit record.


async def _degraded_tick(watchdog, market, *, spot: bool = True) -> None:
    """One tick with the long leg unquoted (credit trigger dark). `spot=False` also
    kills the underlying feed, which makes it blind."""
    if spot:
        market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.50, 0.60)
    await watchdog.check_once()


async def test_impaired_positions_reports_the_reasons_and_the_duration():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    await _degraded_tick(watchdog, market)

    (impaired,) = watchdog.impaired_positions()
    assert impaired.external_identifier == spread().external_identifier
    # The profit target reads the same mid, so it is unknown with the credit stop
    # on every input (2026-09-23) — and the backstop is still live.
    assert impaired.unknown_reasons == ("credit_multiple", "profit_target")
    assert impaired.blind is False
    assert impaired.impaired_for_s == 0.0

    clock.advance(1200.0)                      # 20 minutes, still not clearing
    await _degraded_tick(watchdog, market)
    assert watchdog.impaired_positions()[0].impaired_for_s == 1200.0


async def test_a_fully_evaluable_position_is_not_impaired():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    await watchdog.check_once()
    assert watchdog.impaired_positions() == ()


async def test_the_duration_restarts_when_the_severity_changes():
    """Otherwise a position that just went BLIND would report the milder episode's
    age, and the duration and the severity would describe different states."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    await _degraded_tick(watchdog, market)
    clock.advance(100.0)
    await _degraded_tick(watchdog, market)
    assert watchdog.impaired_positions()[0].impaired_for_s == 100.0

    clock.advance(30.0)
    await _degraded_tick(watchdog, market, spot=False)   # spot now stale too -> blind
    blind = watchdog.impaired_positions()[0]
    assert blind.blind is True
    assert blind.unknown_reasons == (
        "credit_multiple", "profit_target", "underlying_near_short_strike"
    )
    assert blind.impaired_for_s == 0.0, "the blind episode is 0s old, not 130s"


async def test_a_flap_back_to_the_same_reasons_keeps_one_episode():
    """"Still not clearing after 20 minutes" has to survive the reason set being
    recomputed identically on every single tick."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    for _ in range(5):
        await _degraded_tick(watchdog, market)
        clock.advance(10.0)
    # Five ticks, and the clock advanced after each: one episode, 50s old.
    assert watchdog.impaired_positions()[0].impaired_for_s == 50.0


async def test_a_closed_position_stops_being_reported():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    await _degraded_tick(watchdog, market)
    assert len(watchdog.impaired_positions()) == 1

    watchdog._spreads.scan = OpenSpreadScan()  # closed out of the ledger
    watchdog.request_refresh()
    await watchdog.check_once()
    assert watchdog.impaired_positions() == ()


async def test_a_position_with_an_exit_in_flight_stops_being_reported():
    """It is not evaluated while its own exit task is running, so a frozen reading
    would be a lie that only gets older — and the duration is the whole point of the
    field. The exit will report; nothing is lost by not re-reading it underneath."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    await _degraded_tick(watchdog, market)
    assert len(watchdog.impaired_positions()) == 1

    async def never() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(never())
    watchdog._exiting[spread().external_identifier] = task
    try:
        await _degraded_tick(watchdog, market)
        assert watchdog.impaired_positions() == ()
    finally:
        task.cancel()


async def test_a_spent_position_keeps_being_evaluated_and_reported():
    """P1, 2026-08-28. The old code popped a spent position out of `_impaired` and
    skipped `evaluate_triggers` entirely — so a held, triggered 0DTE spread went 76
    minutes unpriced, with no surface able to say what it was worth.

    Refusing to SEND another order and refusing to LOOK were the same branch. Only
    the first is justified, so only the first survives: the position stays in the
    live evaluability read, and its mark keeps updating."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    await _degraded_tick(watchdog, market)
    assert len(watchdog.impaired_positions()) == 1

    watchdog._attempted[spread().external_identifier] = _SpentAttempt(
        ExitOutcome.NOT_RESOLVED, clock()
    )
    await _degraded_tick(watchdog, market)
    assert len(watchdog.impaired_positions()) == 1


async def test_the_live_read_and_the_audit_record_agree():
    """One computation, two consumers. If these ever disagree the phone view is
    telling the operator something the trail cannot corroborate."""
    clock = FakeClock()
    market, audit_quotes, quotes = build_quotes(clock)
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    del audit_quotes
    await _degraded_tick(watchdog, market)

    live = watchdog.impaired_positions()[0]
    assert list(live.unknown_reasons) == audit.payload("watchdog.check")["unknown_reasons"]
    assert list(live.unknown_reasons) == audit.payload("watchdog.degraded")["unknown_reasons"]


async def test_the_backstop_fires_end_to_end_with_both_legs_stale():
    """What the 2026-08-13 defect actually cost: not a log line, a STOP. Spot through
    the short strike with unpriceable legs is the blown-out-spread case the backstop
    exists for, and it must reach `watchdog.triggered` and page — the exit then
    retries until a leg prices, rather than the trigger never firing at all."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    clock.advance(2.9)                         # legs stale; identities still known
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )

    # Sustained past the debounce (2.0s in the test config) with spot through 5000.
    for _ in range(3):
        market.set("SPX", 4990.0, 4990.0)
        await watchdog.check_once()
        clock.advance(1.0)
    market.set("SPX", 4990.0, 4990.0)
    await watchdog.check_once()

    fired = audit.payload("watchdog.triggered")
    assert fired["fired"] == ["underlying_near_short_strike"]
    assert [k for k, _ in alerts].count("watchdog_triggered") == 1
    # The legs still cannot be priced, so no order can be built — recorded and
    # retried, never silently dropped, and never a false "no trigger".
    assert "watchdog.exit_unpriceable" in audit.events


async def test_being_blind_is_reported_on_the_edge_and_on_recovery():
    """BLIND: no trigger evaluable at all. Needs the underlying feed gone too — a
    missing leg alone only costs the credit trigger (see the degraded test below)."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 0.50, 0.60)       # long leg missing AND no spot
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )

    for _ in range(3):
        market.set(SHORT_STREAM, 0.50, 0.60)
        await watchdog.check_once()
    assert audit.events.count("watchdog.unevaluable") == 1
    assert audit.events.count("tier2:watchdog.unevaluable_still") == 2
    assert [k for k, _ in alerts].count("watchdog_position_unevaluable") == 1

    market.set("SPX", 5100.0, 5100.0)
    market.set(LONG_STREAM, 0.20, 0.30)
    await watchdog.check_once()
    assert "watchdog.evaluable_again" in audit.events


async def test_recovering_from_blind_to_merely_degraded_still_closes_the_page():
    """The page said "nothing is watching this". Getting the backstop back ends that
    episode even though the credit trigger is still dark — otherwise the Tier 1
    record has no counterpart and the operator cannot tell it is over."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 0.50, 0.60)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    await watchdog.check_once()                # blind -> page
    assert len(alerts) == 1

    market.set("SPX", 5100.0, 5100.0)          # spot back; long leg still missing
    market.set(SHORT_STREAM, 0.50, 0.60)
    await watchdog.check_once()

    payload = audit.payload("watchdog.evaluable_again")
    assert payload["unknown_reasons"] == ["credit_multiple", "profit_target"], (
        "still degraded, not clean"
    )
    assert len(alerts) == 1, "recovery does not page again"


async def test_a_degraded_position_is_recorded_but_never_paged():
    """The 2026-08-13 rate was ~14% of ticks by session end. Paging on that is the
    2026-08-05 alert-storm shape, and it buries the blind case that does matter."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )

    for _ in range(4):
        market.set("SPX", 5100.0, 5100.0)      # backstop live throughout
        market.set(SHORT_STREAM, 0.50, 0.60)   # long leg never quotes
        await watchdog.check_once()

    assert alerts == [], "a live backstop is not a reason to wake anyone"
    assert "watchdog.unevaluable" not in audit.events
    assert audit.events.count("tier2:watchdog.degraded") == 1, "rising edge only"
    # The per-tick trail is not duplicated here — watchdog.check already carries
    # unknown_reasons on every single tick.
    assert audit.events.count("tier2:watchdog.check") == 4
    assert audit.payload("watchdog.degraded")["unknown_reasons"] == [
        "credit_multiple", "profit_target"
    ]


async def test_a_degraded_episode_re_arms_after_recovering():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )

    async def tick(*, long_quoted: bool) -> None:
        market.set("SPX", 5100.0, 5100.0)
        market.set(SHORT_STREAM, 0.50, 0.60)
        if long_quoted:
            market.set(LONG_STREAM, 0.20, 0.30)
        else:
            market.drop(LONG_STREAM)
        await watchdog.check_once()

    await tick(long_quoted=False)              # degraded
    await tick(long_quoted=True)               # clean -> re-arms
    await tick(long_quoted=False)              # degraded again
    assert audit.events.count("tier2:watchdog.degraded") == 2


async def test_a_position_the_ledger_cannot_explain_is_tier_1_and_alerts():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    scan = OpenSpreadScan(
        unattributed=("SPXW  260729P04000000",),
        malformed=({"broker_order_id": 7, "problem": "1 applied leg(s), expected 2"},),
    )
    watchdog, audit, alerts, _ = build_watchdog(clock=clock, quotes=quotes, scan=scan)
    await watchdog.check_once()
    assert "watchdog.unwatchable" in audit.events
    assert any(k == "watchdog_unwatchable_position" for k, _ in alerts)


async def test_an_imbalanced_position_is_flagged_tier_1():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    held = OpenSpread(
        external_identifier="argus-open-x",
        broker_order_id=1000,
        candidate_id="20260729-put-x",
        underlying="SPXW",
        short_symbol=SHORT_OCC,
        long_symbol=LONG_OCC,
        short_quantity=2,
        long_quantity=1,
        entry_credit=Decimal("0.85"),
    )
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(held,))
    )
    await watchdog.check_once()
    assert "watchdog.leg_imbalance" in audit.events
    assert any(k == "watchdog_leg_imbalance" for k, _ in alerts)


async def test_an_incoherent_credit_basis_pages_once_and_is_still_watched():
    """The fills say a credit spread was opened for a debit. (2026-09-02)

    `store` keeps it on the watch list, on the LIMIT — routing it to `malformed`
    would take a live vertical's credit trigger, underlying trigger, flatten and
    `/status` row over a price-arithmetic contradiction. Which is exactly why the
    third basis value has to be LOUD: a quieter `LIMIT` would leave the operator
    reading an ordinary position and never learning that two of Argus's own
    accounts of the same order disagree.

    Once per position, not once per refresh — the ledger is re-read every 15s and
    this fact does not change between reads.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    held = OpenSpread(
        external_identifier="argus-open-20260729-put-x",
        broker_order_id=1000,
        candidate_id="20260729-put-x",
        underlying="SPXW",
        short_symbol=SHORT_OCC,
        long_symbol=LONG_OCC,
        short_quantity=2,
        long_quantity=2,
        entry_credit=Decimal("0.85"),
        entry_credit_basis=CreditBasis.LIMIT_FILLS_INCOHERENT,
        entry_limit=Decimal("0.85"),
        fills_credit=Decimal("-0.50"),
        priced_contracts=2,
    )
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(held,))
    )
    for _ in range(3):
        clock.advance(30.0)                      # past the 15s ledger cadence
        market.set("SPX", 5100.0, 5100.0)        # keep the legs priceable
        market.set(SHORT_STREAM, 0.50, 0.60)
        market.set(LONG_STREAM, 0.20, 0.30)
        watchdog.request_refresh()
        await watchdog.check_once()

    assert audit.events.count("watchdog.entry_credit_incoherent") == 1
    pages = [d for k, d in alerts if k == "watchdog_entry_credit_incoherent"]
    assert len(pages) == 1
    # Top level, or `notify.alert_scope`'s per-position throttle folds it in with
    # the next unrelated `watchdog_*` alert (2026-08-21).
    assert pages[0]["candidate_id"] == "20260729-put-x"
    assert pages[0]["external_identifier"] == "argus-open-20260729-put-x"
    # And it names the number it rejected — a page that only says "incoherent" is
    # one nobody can act on.
    assert pages[0]["fills_credit"] == -0.50
    assert pages[0]["entry_credit"] == 0.85
    assert pages[0]["entry_credit_basis"] == "limit_fills_incoherent"
    # Still watched: the stop is being evaluated against the limit basis.
    assert [s.external_identifier for s in watchdog.watching] == [
        "argus-open-20260729-put-x"
    ]


async def test_a_realized_basis_never_pages():
    """The ordinary case is silent — the page is for the contradiction, not for the
    fact that a credit has a basis at all."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    held = OpenSpread(
        external_identifier="argus-open-20260729-put-x",
        broker_order_id=1000,
        candidate_id="20260729-put-x",
        underlying="SPXW",
        short_symbol=SHORT_OCC,
        long_symbol=LONG_OCC,
        short_quantity=2,
        long_quantity=2,
        entry_credit=Decimal("0.50"),
        entry_credit_basis=CreditBasis.REALIZED,
        entry_limit=Decimal("0.45"),
        fills_credit=Decimal("0.50"),
        priced_contracts=2,
    )
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(held,))
    )
    await watchdog.check_once()
    assert "watchdog.entry_credit_incoherent" not in audit.events
    assert not [k for k, _ in alerts if k == "watchdog_entry_credit_incoherent"]


async def test_the_ledger_is_not_re_read_every_cadence_tick():
    """The ledger read runs on the audit log's writer lock, whose contract is that
    reads are RARE. Trigger evaluation needs no database at all."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    source = FakeSpreadSource(OpenSpreadScan(spreads=(spread(),)))
    watchdog = PositionWatchdog(
        spreads=source,
        quotes=quotes,
        executor=RecordingExecutor(),
        audit=FakeAudit(),
        config=make_config(),          # refresh_interval_s = 5.0, cadence_s = 0.5
        halt=_noop_halt,
        clock=clock,
        now_et=FakeETClock(),
    )
    for _ in range(4):
        market.set("SPX", 5100.0, 5100.0)
        market.set(SHORT_STREAM, 0.50, 0.60)
        market.set(LONG_STREAM, 0.20, 0.30)
        await watchdog.check_once()
        clock.advance(0.5)
    assert source.calls == 1

    clock.advance(5.0)
    await watchdog.check_once()
    assert source.calls == 2


async def test_the_ledger_is_read_as_of_the_watchdogs_own_trading_date():
    """Which legs count as expired — and therefore closed — is decided by the date
    the ledger is read AS OF (`store._leg_is_expired`, 2026-08-25).

    The session passes its own trading date here, exactly as it does to
    `ExitExecutor`, so a watchdog that outlived an ET midnight cannot start
    judging its own positions against a day it was never started for.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    from datetime import date

    source = FakeSpreadSource(OpenSpreadScan(spreads=(spread(),)))
    session_day = date(2026, 8, 24)
    watchdog = PositionWatchdog(
        spreads=source,
        quotes=quotes,
        executor=RecordingExecutor(),
        audit=FakeAudit(),
        config=make_config(),
        halt=_noop_halt,
        clock=clock,
        now_et=FakeETClock(day=session_day),
        trading_date=lambda: session_day,
    )
    await watchdog.check_once()
    assert source.asked_for == [session_day]


async def test_request_refresh_forces_a_re_read_immediately():
    """Chunk 5's wiring point: a fill makes a position watchable NOW, not up to
    `refresh_interval_s` later."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    source = FakeSpreadSource(OpenSpreadScan(spreads=(spread(),)))
    watchdog = PositionWatchdog(
        spreads=source,
        quotes=quotes,
        executor=RecordingExecutor(),
        audit=FakeAudit(),
        config=make_config(),
        halt=_noop_halt,
        clock=clock,
        now_et=FakeETClock(),
    )
    await watchdog.check_once()
    watchdog.request_refresh()
    await watchdog.check_once()
    assert source.calls == 2


async def test_a_closed_position_loses_its_debounce_window():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 4990.0, 4990.0)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    source = FakeSpreadSource(OpenSpreadScan(spreads=(spread(),)))
    executor = RecordingExecutor()
    watchdog = PositionWatchdog(
        spreads=source,
        quotes=quotes,
        executor=executor,
        audit=FakeAudit(),
        config=make_config(),
        halt=_noop_halt,
        clock=clock,
        now_et=FakeETClock(),
    )
    await watchdog.check_once()          # window opens

    source.scan = OpenSpreadScan()       # closed
    watchdog.request_refresh()
    await watchdog.check_once()

    source.scan = OpenSpreadScan(spreads=(spread(),))   # re-entered
    watchdog.request_refresh()
    clock.advance(2.5)
    market.set("SPX", 4990.0, 4990.0)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    await watchdog.check_once()
    # The re-entry starts from zero rather than inheriting the first window.
    assert executor.calls == []


# -- the exception policy (2026-09-10: degrade, don't die) ----------------------
#
# The old posture — halt ALL, set the shared stop event, return — was signed off on
# the reasoning that a crashed iteration leaves a real position with nobody watching
# it, and it PRODUCED that outcome rather than preventing it: on 2026-09-03 it cost
# ≈$670 on one cert 502, because everything else the stop event and the parked
# process took with them was the only machinery that could have closed the position.
# The new posture: halt ENTRIES, keep the scheduler, keep the loop.
# → docs/designs/2026-09-10-a-watchdog-exception-must-not-take-the-watchdog.md


class _BrokenSource:
    """An `OpenSpreadSource` whose ledger read always raises."""

    async def open_spreads(self, *, today):
        raise RuntimeError("ledger read blew up")


class _FlakySource:
    """`FakeSpreadSource` plus a switchable raise, for refresh-abort tests."""

    def __init__(self, scan):
        self.scan = scan
        self.raises: Exception | None = None

    async def open_spreads(self, *, today):
        if self.raises is not None:
            raise self.raises
        return self.scan


async def _noop_halt(reason, *, source, detail=None, grade=None):
    return None


async def test_an_unhandled_exception_halts_entries_and_keeps_watching():
    """The load-bearing half of the 2026-09-10 change. `run()` loops under this
    design — that is the point — so the pass is driven directly and the halt is
    asserted through the injected seam. Surviving assertions from the old test:
    exactly one halt, `source == "watchdog_exception"`, the cause in the reason."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    halts: list[dict] = []

    async def halt(reason, *, source, detail=None, grade=None):
        halts.append(
            {"reason": reason, "source": source, "detail": detail, "grade": grade}
        )

    audit = FakeAudit()
    alerts: list[tuple[str, dict]] = []
    watchdog = PositionWatchdog(
        spreads=_BrokenSource(),
        quotes=quotes,
        executor=RecordingExecutor(),
        audit=audit,
        config=make_config(),
        halt=halt,
        alert=lambda k, d: alerts.append((k, d)),
        clock=clock,
        now_et=FakeETClock(),
    )
    # One pass, then its settle: the pass edge is where the episode's Tier 1 record,
    # the page and the halt all fire.
    await watchdog.check_once()
    await watchdog._settle_pass()

    assert len(halts) == 1
    assert halts[0]["source"] == "watchdog_exception"
    assert "ledger read blew up" in halts[0]["reason"]
    assert halts[0]["grade"] == HaltGrade.ENTRIES
    assert "watchdog.degraded" in audit.events
    assert any(k == "watchdog_degraded" for k, _ in alerts)
    # The watchdog survived its own failure — the state the old posture destroyed.
    health = watchdog.watchdog_health()
    assert (health.degraded, health.failures) == (True, 1)
    assert watchdog.last_failure is not None
    assert watchdog.last_failure.error_type == "RuntimeError"


async def test_a_failing_pass_does_not_set_the_shared_stop_event():
    """T1, and mutation 2's catcher. The old posture's first casualty:
    `self._stop.set()` ended the trading day and aborted the exit walk trying to
    close the very position the exception was about. The degrade path must leave
    the shared event alone — it is the SESSION's, not the watchdog's."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    stop = asyncio.Event()
    watchdog, _, _, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(), stop=stop,
    )
    watchdog._spreads = _BrokenSource()
    for _ in range(3):
        watchdog.request_refresh()
        await watchdog.check_once()
        await watchdog._settle_pass()

    assert len(halts) == 1          # the halt fires (once per episode); the event does not
    assert stop.is_set() is False


async def test_a_failing_pass_halts_at_entries_never_all():
    """T2, and mutation 1's catcher: the grade is `ENTRIES`, never `ALL`."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    watchdog, _, _, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
    )
    watchdog._spreads = _BrokenSource()
    await watchdog.check_once()
    await watchdog._settle_pass()

    assert len(halts) == 1
    assert halts[0]["grade"] == HaltGrade.ENTRIES
    assert all(h["grade"] == HaltGrade.ENTRIES for h in halts)


async def test_the_loop_keeps_running_after_a_failing_pass():
    """T3, and mutation 3's catcher: no `return` after the degrade. This test
    drives the REAL `run()` loop — a `return` re-added to the degrade path would
    end the task after the first failing pass, and the position below would never
    be evaluated, triggered or exited."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    # Quotes from the start, so the first evaluated pass opens the sustain window
    # and the 2.5s advance below is what sustains it.
    _triggered_and_priceable(clock, market)
    source = _FlakySource(OpenSpreadScan(spreads=(spread(),)))
    executor = RecordingExecutor()
    stop = asyncio.Event()
    watchdog, _, _, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, stop=stop,
        config=make_config(watchdog={"cadence_s": 0.01}),
    )
    watchdog._spreads = source
    source.raises = RuntimeError("ledger read blew up")

    task = asyncio.create_task(watchdog.run())
    try:
        # The first pass fails in the refresh and degrades: one halt, no exit.
        await asyncio.sleep(0.05)
        assert len(halts) == 1
        assert executor.calls == []

        # The read recovers. The loop never stopped: the position is evaluated,
        # the trigger sustains, and the exit handoff happens exactly as it would
        # have on a day with no failure at all.
        source.raises = None
        watchdog.request_refresh()
        await asyncio.sleep(0.05)             # evaluated; the sustain window opens
        clock.advance(2.5)
        _triggered_and_priceable(clock, market)
        await asyncio.sleep(0.15)             # sustained; the exit is handed off
        assert [s.external_identifier for s, _ in executor.calls] == [
            spread().external_identifier
        ]
        # And the episode CLOSED on the first clean pass: one failure, recovered.
        assert "watchdog.recovered" in watchdog._audit.events
        assert watchdog.watchdog_health().degraded is False
    finally:
        stop.set()
        await task
        await watchdog._drain_exits()


async def test_one_poisonous_position_does_not_blind_its_sibling(monkeypatch):
    """T4, and mutation 4's catcher. Two same-underlying, same-day spreads — the
    routine book — and the FIRST raises in evaluation. `_scan.spreads` ordering is
    stable, so a pass-level catch alone would fail this same position on every tick
    forever and the sibling would never be reached: the 2026-09-03 harm reproduced
    by a different route."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    poison = spread()
    sibling = dataclasses.replace(
        spread(),
        external_identifier="argus-open-20260729-put-y",
        candidate_id="20260729-put-y",
    )
    executor = RecordingExecutor()
    # Past the flatten mark so the sibling's trigger is INSTANT: one pass is enough
    # to evaluate, trigger and hand off — which is the assertion.
    watchdog, _, _, halts = build_watchdog(
        clock=clock, quotes=quotes,
        scan=OpenSpreadScan(spreads=(poison, sibling)),
        executor=executor, et_clock=FakeETClock(hour=15, minute=35),
    )

    original = watchdog_module.evaluate_triggers

    def poisoned(spread, quotes, thresholds, *, seconds_to_flatten):
        if spread.external_identifier == poison.external_identifier:
            raise RuntimeError("poisonous spread")
        return original(
            spread, quotes, thresholds, seconds_to_flatten=seconds_to_flatten
        )

    monkeypatch.setattr(watchdog_module, "evaluate_triggers", poisoned)
    await watchdog.check_once()
    await watchdog._settle_pass()
    await watchdog._drain_exits()

    # The sibling was evaluated, triggered and exited ON THE SAME PASS.
    assert [s.external_identifier for s, _ in executor.calls] == [
        sibling.external_identifier
    ]
    # And the poison was named, not swallowed: one failure, scoped to one position.
    health = watchdog.watchdog_health()
    assert (health.failures, health.affected) == (1, (poison.external_identifier,))
    assert health.degraded is True


async def test_the_halt_fires_once_per_episode_not_once_per_tick():
    """T5, and mutation 10's catcher. Ten failing passes, ONE Tier 1 record, ONE
    halt call, ONE page — the edge shape `_Escalation` exists to enforce, applied
    to the watchdog's own failure."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    watchdog, audit, alerts, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
    )
    watchdog._spreads = _BrokenSource()
    for _ in range(10):
        watchdog.request_refresh()
        await watchdog.check_once()
        await watchdog._settle_pass()

    assert audit.events.count("watchdog.degraded") == 1
    assert len(halts) == 1
    assert [k for k, _ in alerts].count("watchdog_degraded") == 1
    # The repeats are Tier 2, never silent.
    assert audit.events.count("tier2:watchdog.degraded_still") == 9


async def test_a_degraded_episode_repages_on_the_bound():
    """T6, and mutation 11's catcher. `overdue` on `_DEGRADED_PAGE_INTERVAL_S`:
    three pages total across two bounds, `degraded_for_s` rising — the bounded
    RATE, never a single page."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    watchdog, _, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
    )
    watchdog._spreads = _BrokenSource()
    for advance in (0.0, _DEGRADED_PAGE_INTERVAL_S, _DEGRADED_PAGE_INTERVAL_S):
        clock.advance(advance)
        watchdog.request_refresh()
        await watchdog.check_once()
        await watchdog._settle_pass()
    pages = [d for k, d in alerts if k == "watchdog_degraded"]

    assert len(pages) == 3
    # The rising-edge page carries no duration; the two repeats carry the bound's
    # elapsed, rising.
    assert "degraded_for_s" not in pages[0]
    assert [p["degraded_for_s"] for p in pages[1:]] == [
        _DEGRADED_PAGE_INTERVAL_S,
        _DEGRADED_PAGE_INTERVAL_S * 2,
    ]


async def test_recovery_records_and_pages_and_leaves_entries_halted():
    """T7, and mutation 12's catcher. A clean pass after failures: Tier 1
    `watchdog.recovered`, the page, `_affected` cleared — and the halt is NOT
    reversed, because it is permanent by design."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    watchdog, audit, alerts, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
    )
    watchdog._spreads = _BrokenSource()
    await watchdog.check_once()
    await watchdog._settle_pass()
    assert len(halts) == 1

    # The read recovers; the next pass is clean.
    watchdog._spreads = FakeSpreadSource(OpenSpreadScan(spreads=(spread(),)))
    watchdog.request_refresh()
    await watchdog.check_once()
    await watchdog._settle_pass()

    assert "watchdog.recovered" in audit.events
    assert any(k == "watchdog_recovered" for k, _ in alerts)
    # The halt was not reversed — no resume, no second halt, no relaxation.
    assert len(halts) == 1
    assert halts[0]["grade"] == HaltGrade.ENTRIES
    health = watchdog.watchdog_health()
    assert (health.degraded, health.failures, health.affected) == (False, 1, ())
    # ... and the recovery still names what the episode was.
    assert health.error_type == "RuntimeError"


async def test_audit_write_error_keeps_the_old_posture():
    """T8, and mutation 9's catcher. THE exclusion (§3.5): the recorder is dead
    process-wide, `on_failure` has already halted `ALL` and set this very event,
    and there is nothing left for a watchdog to protect. Today's path, verbatim —
    shared stop set, no grade (the default `ALL`), `run()` returns."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    halts: list[dict] = []

    async def halt(reason, *, source, detail=None, grade=None):
        halts.append({"source": source, "detail": detail, "grade": grade})

    audit = FakeAudit()
    watchdog = PositionWatchdog(
        spreads=_FlakySource(OpenSpreadScan()),
        quotes=quotes,
        executor=RecordingExecutor(),
        audit=audit,
        config=make_config(),
        halt=halt,
        clock=clock,
        now_et=FakeETClock(),
    )
    watchdog._spreads.raises = AuditWriteError("audit log has failed")

    await watchdog.run()

    assert len(halts) == 1
    assert halts[0]["source"] == "watchdog_exception"
    assert halts[0]["grade"] is None            # the default ALL, never ENTRIES
    assert watchdog._stop.is_set()              # the shared event, verbatim
    assert "watchdog.fatal" in audit.events
    assert watchdog.watchdog_health().failures == 1


async def test_a_dead_audit_log_still_halts():
    """T9, kept. The recorder may be exactly what failed; the halt must not depend
    on it. (Mechanically adapted to the new loop shape: the degraded Tier 1 write
    is what the dead recorder refuses, and `run()` no longer parks the process.)"""

    class DeadAudit(FakeAudit):
        async def record(self, category, payload):
            raise RuntimeError("audit log unwritable")

    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    halts: list[dict] = []

    async def halt(reason, *, source, detail=None, grade=None):
        halts.append({"source": source})

    watchdog = PositionWatchdog(
        spreads=_BrokenSource(),
        quotes=quotes,
        executor=RecordingExecutor(),
        audit=DeadAudit(),
        config=make_config(),
        halt=halt,
        clock=clock,
        now_et=FakeETClock(),
    )
    await watchdog.check_once()
    await watchdog._settle_pass()
    assert halts == [{"source": "watchdog_exception"}]


async def test_an_exception_inside_an_exit_spends_the_attempt_and_halts_entries():
    """The second replacement. Surviving assertions from the old test: one halt,
    `detail["where"] == "exit"`. Everything else is §2.5(a): the crash SPENDS the
    attempt, because an exception is the one state in which we do not know whether
    an order reached the broker."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 4990.0, 4990.0)
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    executor = RecordingExecutor()
    executor.raises = ValueError("exit path bug")
    watchdog, audit, alerts, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)), executor=executor
    )

    await watchdog.check_once()
    clock.advance(2.5)
    market.set("SPX", 4990.0, 4990.0)
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    await watchdog.check_once()
    await watchdog._drain_exits()

    assert len(halts) == 1
    assert halts[0]["detail"]["where"] == "exit"
    assert halts[0]["grade"] == HaltGrade.ENTRIES
    assert "watchdog.exit_crashed" in audit.events
    assert any(k == "watchdog_exit_crashed" for k, _ in alerts)


async def test_a_crashed_exit_spends_the_attempt():
    """T10, and mutations 5 and 6's catcher. After the crash the next tick
    EVALUATES the position — mark, telemetry, page — and sends NOTHING: the
    one-attempt rule's strongest case, not an exception to it."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 4990.0, 4990.0)
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    executor = RecordingExecutor()
    executor.raises = ValueError("exit path bug")
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor,
    )

    for _ in range(2):
        await watchdog.check_once()
        clock.advance(2.5)
        market.set("SPX", 4990.0, 4990.0)
        market.set(SHORT_STREAM, 2.05, 2.25)
        market.set(LONG_STREAM, 0.30, 0.50)
    await watchdog._drain_exits()
    key = spread().external_identifier

    spent = watchdog._attempted[key]
    assert spent.outcome is ExitOutcome.NOT_RESOLVED
    assert spent.reject_reason is None and spent.mutation_error is None
    assert "ValueError: exit path bug" in spent.crash_error
    # The escalation mirrors the ordinary spend: the repeating page is armed.
    assert watchdog._escalation.active(key, "exit_attempt_spent") is not None

    # Two further ticks, both still triggered and priceable: evaluated, never sent.
    for _ in range(2):
        clock.advance(2.5)
        market.set("SPX", 4990.0, 4990.0)
        market.set(SHORT_STREAM, 2.05, 2.25)
        market.set(LONG_STREAM, 0.30, 0.50)
        await watchdog.check_once()
        await watchdog._drain_exits()
    assert len(executor.calls) == 1
    # And the spent position is still PRICED — the 2026-08-28 rule, intact.
    assert "tier2:watchdog.check_spent" in audit.events


async def test_a_crashed_exit_keeps_its_flatten_override():
    """T11, and mutation 7's catcher. A crash during the ORDINARY attempt leaves
    `_flatten_attempted` untouched, so past the 15:30 mark the position still gets
    exactly ONE further send — through `DuplicateOrderGuard`, which re-asks the
    broker and permits only DEAD."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    et_clock = FakeETClock()
    market.set("SPX", 4990.0, 4990.0)
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    executor = RecordingExecutor()
    executor.raises = ValueError("exit path bug")
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=et_clock,
    )

    for _ in range(2):
        await watchdog.check_once()
        clock.advance(2.5)
        market.set("SPX", 4990.0, 4990.0)
        market.set(SHORT_STREAM, 2.05, 2.25)
        market.set(LONG_STREAM, 0.30, 0.50)
    await watchdog._drain_exits()
    assert len(executor.calls) == 1
    key = spread().external_identifier
    assert key not in watchdog._flatten_attempted

    # Past the mark. The crash did not GRANT a second ordinary attempt, and it did
    # not SPEND the flatten override either: one further send is available.
    executor.raises = None
    executor.outcome = ExitOutcome.FILLED
    et_clock.at(15, 35)
    watchdog.request_refresh()
    clock.advance(2.5)
    market.set("SPX", 4990.0, 4990.0)
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    await watchdog.check_once()
    await watchdog._drain_exits()
    assert len(executor.calls) == 2

    # And exactly one: the next tick past the mark does not send again.
    clock.advance(2.5)
    market.set("SPX", 4990.0, 4990.0)
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    await watchdog.check_once()
    await watchdog._drain_exits()
    assert len(executor.calls) == 2


async def test_an_aborted_pass_still_reaps():
    """T12, and mutation 8's catcher. §2.5(b): a pass that aborts above the reap
    leaves a FINISHED exit task in `_exiting`, and `check_once` skips any key in
    `_exiting` — a permanent, silent removal from the watch list. The `finally`
    is what closes it."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    executor = RecordingExecutor(ExitOutcome.NOT_ATTEMPTED)
    source = _FlakySource(OpenSpreadScan(spreads=(spread(),)))
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor,
    )
    watchdog._spreads = source
    key = spread().external_identifier

    # Two ticks to a sustained trigger; the exit task is created on the second and
    # completes without being reaped.
    for _ in range(2):
        _triggered_and_priceable(clock, market)
        await watchdog.check_once()
        clock.advance(2.5)
    await asyncio.sleep(0)
    assert key in watchdog._exiting

    # The next pass aborts in the refresh, ABOVE the reap. The `finally` runs it.
    source.raises = AuditWriteError("recorder is dead")
    watchdog.request_refresh()
    with pytest.raises(AuditWriteError):
        await watchdog.check_once()
    assert key not in watchdog._exiting

    # So the position is evaluated on the next pass, not skipped as "in flight".
    source.raises = None
    watchdog.request_refresh()
    clock.advance(2.5)
    _triggered_and_priceable(clock, market)
    await watchdog.check_once()
    await watchdog._drain_exits()
    assert len(executor.calls) == 2


async def test_the_crash_sentence_reaches_the_spent_page():
    """T14. The repeating page is the one an operator is most likely to actually
    read, so it owes the crash's sentence — `not_resolved` with no broker reason
    anywhere is the reassuring-when-wrong reading the row has been fixed for twice."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 4990.0, 4990.0)
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    executor = RecordingExecutor()
    executor.raises = ValueError("exit path bug")
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor,
    )

    for _ in range(2):
        await watchdog.check_once()
        clock.advance(2.5)
        market.set("SPX", 4990.0, 4990.0)
        market.set(SHORT_STREAM, 2.05, 2.25)
        market.set(LONG_STREAM, 0.30, 0.50)
    await watchdog._drain_exits()

    # Past the bound, still held, still triggered: the SPENT episode pages.
    clock.advance(25.0)
    market.set("SPX", 4990.0, 4990.0)
    market.set(SHORT_STREAM, 2.05, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    await watchdog.check_once()

    page = audit.payload("watchdog.exit_spent_still_held")
    assert page["exit_outcome"] == "not_resolved"
    assert "ValueError: exit path bug" in page["crash_error"]
    assert (page["reject_reason"], page["mutation_error"]) == (None, None)
    _, detail = next(
        (k, d) for k, d in alerts if k == "watchdog_exit_spent_still_held"
    )
    assert "ValueError: exit path bug" in detail["crash_error"]


def test_escalation_active_reports_the_episode_start():
    """T18. `active` is the read-only half of the edge pair, added so the health
    row does not have to keep a second copy of `_on` in sync with rising/falling."""
    from argus.execution.escalation import _Escalation

    clock = FakeClock()
    escalation = _Escalation(clock)
    assert escalation.active("k", "t") is None

    clock.advance(5.0)
    assert escalation.rising("k", "t") is True
    assert escalation.active("k", "t") == 1005.0

    clock.advance(7.0)
    assert escalation.active("k", "t") == 1005.0   # the episode's START, not now
    assert escalation.falling("k", "t") is True
    assert escalation.active("k", "t") is None


async def test_run_stops_cleanly_on_the_stop_event():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    stop = asyncio.Event()
    watchdog = PositionWatchdog(
        spreads=FakeSpreadSource(),
        quotes=quotes,
        executor=RecordingExecutor(),
        audit=FakeAudit(),
        config=make_config(watchdog={"cadence_s": 0.01, "sustained_s": 0.5,
                                     "refresh_interval_s": 0.5}),
        halt=_noop_halt,
        stop=stop,
        clock=clock,
        now_et=FakeETClock(),
    )
    watchdog.start()
    await asyncio.sleep(0.05)
    await watchdog.stop()
    assert watchdog.last_failure is None
    assert watchdog.watchdog_health().failures == 0


# -- structural: the chunk boundary the brief draws ---------------------------


def _parse(module: str):
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "src" / "argus" / "execution" / module
    return ast.parse(path.read_text()), path.read_text()


def test_chunk_3s_loop_knows_nothing_about_this_chunk():
    """The shared walk engine must not learn about "crossing the market".

    Crossing is exit business logic and lives here; `loop.py` gains no concept of it.
    A reverse import is how that would start, so its absence is pinned structurally
    rather than left to reviewer memory. (`WalkPolicy.for_exit` goes the other way:
    chunk 4 asks chunk 3 for a policy, which is the parameterization the design
    always intended.)
    """
    import ast

    tree, _ = _parse("loop.py")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            module = getattr(node, "module", "") or ""
            assert "watchdog" not in module, f"loop.py imports {module}"
            assert not any("watchdog" in n for n in names), f"loop.py imports {names}"


def test_the_watchdog_never_reaches_the_broker_adapter_directly():
    """Same rail chunk 3 has: every order must go through the audited/recorded stack,
    so importing the raw tastytrade adapter is how that gets bypassed."""
    import ast

    tree, _ = _parse("watchdog.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name != "TastytradeOrderExecution"


def test_the_watchdog_does_not_call_asyncio_sleep_directly():
    """Waiting goes through the injected `sleep` seam, or the test suite would have to
    wait in real time for a debounce and a cancel-settle window."""
    import ast

    tree, _ = _parse("watchdog.py")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "sleep":
            continue
        target = node.func.value
        assert not (isinstance(target, ast.Name) and target.id == "asyncio"), (
            f"watchdog.py:{node.lineno} calls asyncio.sleep directly"
        )


def test_the_watchdog_defines_no_second_tick_or_floor():
    """`zerobid.py` is THE zero-bid floor and tick rounding; two copies of a pricing
    rule is how one of them drifts."""
    _, source = _parse("watchdog.py")
    assert "SPX_MIN_TICK" not in source
    assert "0.05" not in source
    # It uses the shared implementations instead.
    assert "round_to_tick" in source and "spread_mid" in source


# -- the book-agreement check (broker vs ledger, added 2026-08-11) ---------
#
# `positions` is written ONLY from AlertStreamer fill events. A fill during a stream
# gap leaves a real 0DTE position with no ledger row, so the watchdog never watches
# it and no virtual stop is evaluated. Both counts are non-flat LEG counts: a
# vertical is TWO, so comparing against len(scan.spreads) would be wrong by 2x.


async def test_broker_holding_more_than_the_ledger_pages_someone():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 2,   # broker: a 2-leg vertical is held
        ledger_positions=0,                # ledger: nothing booked
    )
    await wd._check_broker_agreement()

    payload = audit.payload("watchdog.book_disagreement")
    assert payload["broker_legs"] == 2 and payload["ledger_legs"] == 0
    assert payload["direction"] == "held_but_unbooked"
    assert any(k == "watchdog_book_disagreement" for k, _ in alerts)
    assert halts == [], "it pages; it does not halt (nothing here is corrupted state)"


# -- the cache can lie, so confirm before paging (2026-08-19) ----------------
#
# `broker_position_count` reads AccountStateCache, a stream-fed MIRROR. A close
# event missed on the stream left a phantom leg there for over an hour on
# 2026-08-19, which paged `held_but_unbooked` — the one condition deliberately
# built with no settle allowance — for a position the broker had long since closed.
# → docs/decisions/2026-08-19-account-cache-ghost-positions.md


async def test_a_stale_cache_does_not_page_when_rest_backs_the_ledger():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)

    async def confirm():
        return 2   # REST: only the booked vertical is held. The cache invented one.

    wd, audit, alerts, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 3,   # cache: 3 legs, one of them a ghost
        ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    await wd._check_broker_agreement()

    assert "watchdog.book_disagreement" not in audit.events
    assert alerts == [], "a phantom leg must not page the no-allowance condition"
    assert halts == []
    # Recorded, not silent: Tier 1, so the drift is in the trail and countable.
    assert audit.payload("watchdog.book_cache_over_reporting")["rest_legs"] == 2
    assert audit.events.count("tier2:watchdog.book_disagreement_still") == 1


async def test_rest_backing_the_cache_still_pages():
    """The real unwatched position — the case the check exists for — is unchanged."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)

    async def confirm():
        return 3   # REST agrees with the cache: a leg really is held and unbooked.

    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 3, ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    await wd._check_broker_agreement()

    assert audit.payload("watchdog.book_disagreement")["direction"] == "held_but_unbooked"
    assert any(k == "watchdog_book_disagreement" for k, _ in alerts)


async def test_a_failed_confirm_pages_rather_than_suppressing():
    """'Could not check' must never be the reason an unwatched position goes unreported."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)

    async def confirm():
        raise RuntimeError("cert 502")

    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 3, ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    await wd._check_broker_agreement()

    assert "watchdog.book_disagreement" in audit.events
    assert len(alerts) == 1


# -- and it must not block trigger evaluation while it fails (2026-08-21) -----
#
# The confirm is awaited INSIDE one `check_once` pass, so its bound is a blind
# window: no position is evaluated against its triggers until it returns. It was
# bounded by `watchdog.refresh_interval_s` — the watch-list REBUILD cadence, 15s in
# production — where `staleness.account_refresh_timeout_s` is the tuned per-attempt
# bound for exactly this REST call. These two use real wall time deliberately: a
# fake clock cannot observe an `asyncio.timeout`, which runs on the event loop's.


def _slow_confirm_config():
    """Timeout scaled down 20x from the suite default; the rebuild cadence left
    ALONE at 5.0s, so a test that finishes fast can only have used the timeout."""
    return make_config(staleness={"account_refresh_timeout_s": 0.05})


async def test_a_hanging_confirm_is_bounded_by_the_account_timeout():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)

    async def confirm():
        await asyncio.sleep(30)   # cert hanging, the documented failure mode
        return 2

    wd, audit, alerts, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 3, ledger_positions=2,
        confirm_broker_position_count=confirm,
        config=_slow_confirm_config(),
    )
    started = time.monotonic()
    await wd._check_broker_agreement()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, (
        f"the confirm blocked trigger evaluation for {elapsed:.2f}s — it must be "
        "bounded by staleness.account_refresh_timeout_s, not a watchdog cadence"
    )
    # Fail-closed is unchanged: a timeout proves nothing, so it pages.
    assert "watchdog.book_disagreement" in audit.events
    assert len(alerts) == 1
    assert halts == []


async def test_a_hanging_confirm_still_stalls_once_per_shape_not_once_per_tick():
    """The timeout bounds ONE stall's duration; the per-shape cache bounds how many
    there are. Both are load-bearing, and the bound change must not cost the cache."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    broker = [3]
    calls: list[int] = []

    async def confirm():
        calls.append(1)
        await asyncio.sleep(30)
        return 2

    wd, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: broker[0], ledger_positions=2,
        confirm_broker_position_count=confirm,
        config=_slow_confirm_config(),
    )
    started = time.monotonic()
    for _ in range(4):
        await wd._check_broker_agreement()
    assert calls == [1], "a timed-out verdict caches like any other — one round trip"

    # A NEW shape re-confirms, and so stalls again: the cache is keyed on the
    # numbers, never on "we already gave up once".
    broker[0] = 4
    await wd._check_broker_agreement()
    assert len(calls) == 2
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, (
        f"five ticks over two shapes took {elapsed:.2f}s — expected two bounded "
        "stalls of ~0.05s, not one per tick"
    )


async def test_the_confirm_runs_once_per_shape_then_re_runs_when_it_changes():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    broker = [3]
    calls: list[int] = []

    async def confirm():
        calls.append(1)
        return 2   # REST holds the two booked legs throughout

    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: broker[0], ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    for _ in range(4):
        await wd._check_broker_agreement()
    assert len(calls) == 1, "a steady disagreement is one round trip, not one per tick"

    # A NEW shape must be re-measured: a verdict cannot outlive its numbers.
    broker[0] = 4
    await wd._check_broker_agreement()
    assert len(calls) == 2
    # REST still says 2, so it is still the cache over-reporting — still no page.
    assert alerts == []


# -- and a shape the book keeps RETURNING to must not re-pay it (2026-08-21, A4b) --
#
# The cache was a single SLOT, so it collapsed consecutive repeats only: the real
# rule was one round trip per shape CHANGE. A persistent ghost with the account
# stream churning underneath alternates between two shapes indefinitely and re-paid
# the round trip — and its blind window — on every alternation. It is now a dict,
# which makes the staleness policy the load-bearing part: see
# `PositionWatchdog._book_confirm_lookup` for the three bounds and why each exists.


def _mutable_ledger(wd, cell: list[int]) -> None:
    """Point the watchdog at a ledger count the test can move between observations.

    `build_watchdog` fixes it at construction, and the oscillation A4b is about is
    precisely the case where BOTH counts move."""
    from datetime import date as _date

    from argus.execution.store import LedgerState

    async def _state() -> LedgerState:
        return LedgerState(orders=1, positions=cell[0],
                           newest_trading_date=_date(2026, 8, 11))

    wd._ledger_state = _state


async def test_an_alternating_shape_does_not_re_confirm_on_every_alternation():
    """A stream re-seed churning a ghost leg in and out while the ledger holds
    steady: twelve observations, two shapes. The round trips must scale with the
    number of SHAPES, not with the number of alternations — a single slot makes
    twelve, because each observation displaces the other shape's verdict."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    cache = [4]
    ledger = [2]
    asked: list[tuple[int, int]] = []

    async def confirm():
        asked.append((cache[0], ledger[0]))
        return ledger[0]      # REST backs the ledger throughout: it is cache drift

    wd, _, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: cache[0], ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    _mutable_ledger(wd, ledger)

    for _ in range(6):
        for count in (4, 3):
            cache[0] = count
            clock.advance(1.0)
            await wd._check_broker_agreement()

    assert asked == [(4, 2), (3, 2)], (
        f"twelve observations over two shapes made {len(asked)} round trips: "
        f"{asked}"
    )
    assert alerts == [], "REST backed the ledger every time; nothing to page about"


async def test_a_cached_suppression_is_not_trusted_past_its_ttl():
    """THE test the staleness policy exists for. A `True` verdict suppresses
    `held_but_unbooked` — the one condition built with no timer allowance — so a
    verdict that outlives the state it measured buys silence on a genuinely
    unwatched position. Neither of the stronger invalidations can catch this case:
    the shape has not changed and the episode has not ended. Only the clock can."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    rest = [2]
    asked: list[int] = []

    async def confirm():
        asked.append(1)
        return rest[0]

    wd, _, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 3, ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    await wd._check_broker_agreement()
    assert len(asked) == 1 and alerts == []

    clock.advance(29.0)                 # inside book_confirm_ttl_s (30.0)
    await wd._check_broker_agreement()
    assert len(asked) == 1, "a verdict inside its bound is reused, as designed"
    assert alerts == []

    # The truth underneath has changed: that third leg is genuinely held now.
    rest[0] = 3
    clock.advance(1.5)                  # 30.5s total — past the bound
    await wd._check_broker_agreement()
    assert len(asked) == 2, "the cached verdict outlived its bound and was reused"
    assert len(alerts) == 1, "re-measuring found the real unwatched position"


async def test_a_cached_verdict_is_not_kept_alive_by_being_read():
    """The TTL runs from when the verdict was MEASURED, never from when it was last
    used. Extending it on a hit would make a shape the book sits on for hours carry
    an immortal verdict — reintroducing the unbounded trust the expiry exists to
    end, through the back door, in exactly the steady case that hides it longest."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    asked: list[int] = []

    async def confirm():
        asked.append(1)
        return 2

    wd, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 3, ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    # Read it five times inside the window, then once past it. Under a
    # refreshed-on-use TTL every read would push the expiry out and this stays at 1.
    for _ in range(6):
        await wd._check_broker_agreement()
        clock.advance(5.0)
    await wd._check_broker_agreement()   # t = 30.0, exactly at the bound
    assert len(asked) == 2


async def test_book_agreement_clears_every_cached_shape_not_just_the_last():
    """Agreement is the strongest invalidation and the only evidence-based one: the
    disagreeing book every verdict describes is gone. A dict makes 'clear the slot'
    into 'clear them all', and a partial clear would leave the OTHER shapes' stale
    verdicts to be reused across an episode boundary."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    cache = [4]
    ledger = [2]
    asked: list[tuple[int, int]] = []

    async def confirm():
        asked.append((cache[0], ledger[0]))
        return ledger[0]

    wd, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: cache[0], ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    _mutable_ledger(wd, ledger)

    await wd._check_broker_agreement()          # (4, 2) confirmed
    cache[0] = 3
    await wd._check_broker_agreement()          # (3, 2) confirmed
    assert asked == [(4, 2), (3, 2)]

    cache[0] = 2
    await wd._check_broker_agreement()          # 2 vs 2 — the episode ends

    # The book returns to the FIRST shape, which is not the one the old single slot
    # would have been holding. It must be re-measured all the same.
    cache[0] = 4
    await wd._check_broker_agreement()
    assert asked == [(4, 2), (3, 2), (4, 2)]


async def test_the_shape_cache_is_bounded_and_evicts_the_oldest_first():
    """The TTL bounds the cache's lifetime; this bounds its SIZE, for a churn that
    invents a new shape faster than the TTL retires the last one. Eviction is
    oldest-measured-first and costs a round trip, never a suppressed page — so the
    cap is free to be a plain constant rather than another knob."""
    from argus.execution.watchdog import _BOOK_CONFIRM_CACHE_MAX

    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 3, ledger_positions=2,
        confirm_broker_position_count=lambda: 2,
    )
    for i in range(_BOOK_CONFIRM_CACHE_MAX + 5):
        clock.advance(0.1)                      # all well inside the TTL
        wd._remember_book_confirm((i, 0), True)

    assert len(wd._book_confirm) == _BOOK_CONFIRM_CACHE_MAX
    assert (0, 0) not in wd._book_confirm, "the oldest measurement should go first"
    assert (_BOOK_CONFIRM_CACHE_MAX + 4, 0) in wd._book_confirm


async def test_suppressing_does_not_consume_the_escalation_edge():
    """A suppressed episode that turns real must still be able to page."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    rest = [2]

    async def confirm():
        return rest[0]

    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 3, ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    for _ in range(3):
        await wd._check_broker_agreement()
    assert alerts == []

    # REST now confirms the third leg is genuinely held. Same shape, new truth.
    rest[0] = 3
    wd._book_confirm.clear()   # as a changed shape or a cleared book would do
    await wd._check_broker_agreement()
    assert len(alerts) == 1, "the rising edge was still available"


async def test_without_a_confirm_seam_it_pages_exactly_as_before():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 3, ledger_positions=2,
    )
    await wd._check_broker_agreement()
    assert "watchdog.book_disagreement" in audit.events
    assert len(alerts) == 1


async def test_the_confirm_is_not_consulted_for_booked_but_absent():
    """That direction has its own settle allowance; a round trip there buys nothing."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    calls: list[int] = []

    async def confirm():
        calls.append(1)
        return 0

    wd, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 0, ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    await wd._check_broker_agreement()
    assert calls == []


async def test_booked_but_absent_past_its_settle_window_still_does_not_confirm():
    """The DIRECTION gate, isolated from the settle window that usually hides it.
    A `booked_but_absent` disagreement that has outlived `book_settle_s` escalates
    — and still never asks REST, because the confirm answers "is the account cache
    over-reporting?", which is not a question about a book holding FEWER legs than
    the ledger. The test above only reaches the settle allowance, so on its own it
    would not notice the direction check going away."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    calls: list[int] = []

    async def confirm():
        calls.append(1)
        return 0

    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 0, ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    await wd._check_broker_agreement()      # starts the settle window
    clock.advance(11.0)                     # past book_settle_s (10.0)
    await wd._check_broker_agreement()

    assert audit.payload("watchdog.book_disagreement")["direction"] == "booked_but_absent"
    assert len(alerts) == 1, "it escalated, so the allowance is not what suppressed it"
    assert calls == [], "the confirm is for the held_but_unbooked direction only"


async def test_ordinary_entry_and_exit_lag_never_reaches_the_round_trip():
    """The three gates, end to end, on the common path — a full entry/hold/exit
    cycle with no ghost anywhere. Every disagreement it produces is ordinary
    propagation lag, and none of it may cost a REST round trip.

    This is the property that made the confirm's blind window a latent hazard
    rather than a daily event, and A4b changed the caching layer underneath it, so
    it is asserted directly rather than inferred."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    cache = [0]
    ledger = [0]
    asked: list[tuple[int, int]] = []

    async def confirm():
        asked.append((cache[0], ledger[0]))
        return cache[0]

    wd, _, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: cache[0], ledger_positions=0,
        confirm_broker_position_count=confirm,
    )
    _mutable_ledger(wd, ledger)

    async def observe(dt: float = 0.5):
        clock.advance(dt)
        await wd._check_broker_agreement()

    await observe()                     # flat, agreeing
    # Entry. The ledger books each AlertStreamer fill before the broker's own
    # position book reflects it, sometimes leg by leg — `booked_but_absent`, which
    # has its own settle allowance and never consults the confirm.
    ledger[0] = 1; await observe()
    ledger[0] = 2; await observe()
    cache[0] = 1;  await observe()
    cache[0] = 2;  await observe()      # caught up — agreeing again
    # Held, quiet.
    for _ in range(3):
        await observe(15.0)
    # Exit. Same physics the other way: the ledger drops its rows the instant the
    # closing fill books and the broker's book trails. Suppressed on EVIDENCE —
    # Argus's own closing fills — so it never reaches the confirm either.
    wd.note_fills(_closed(SHORT_OCC, LONG_OCC))
    ledger[0] = 0; await observe(1.2)
    cache[0] = 1;  await observe(2.0)
    cache[0] = 0;  await observe(2.0)   # settled

    assert asked == [], f"ordinary lag cost {len(asked)} REST round trip(s): {asked}"
    assert alerts == []


async def test_the_confirm_is_not_reached_when_the_refresh_is_not_due():
    """The outermost gate. `_check_broker_agreement` runs from `_refresh_if_due`,
    so the confirm is bounded by the refresh cadence before any of the rest of this
    applies — which is also why `book_confirm_ttl_s` must exceed that cadence."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    asked: list[int] = []

    async def confirm():
        asked.append(1)
        return 2

    wd, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 3, ledger_positions=2,
        confirm_broker_position_count=confirm,
    )
    await wd._refresh_if_due()          # first pass is always due
    assert len(asked) == 1

    for _ in range(8):                  # 4.0s, inside refresh_interval_s (5.0)
        clock.advance(0.5)
        await wd._refresh_if_due()
    assert len(asked) == 1, "the check ran without a refresh being due"


# -- working-order poll for the entry gate (Finding 6, 2026-08-28) -------------


def _working_open_order(identifier="argus-open-20260828-put-5000-4995"):
    from decimal import Decimal

    from argus.execution.models import LegAction, LiveOrders, Order, OrderDirection, OrderLeg

    order = Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", LegAction.SELL_TO_OPEN, 2),
            OrderLeg("SPXW  P4995", LegAction.BUY_TO_OPEN, 2),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=identifier,
        status=None,  # -> WORKING
    )
    return LiveOrders(orders=(order,))


async def test_the_refresh_polls_working_orders_and_exposes_the_count():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)

    async def live_orders():
        return _working_open_order()

    wd, _audit, _alerts, _halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(), live_orders=live_orders,
    )
    assert wd.working_open_spread_count == 0
    await wd._refresh_if_due()
    assert wd.working_open_spread_count == 1


async def test_a_failing_working_order_poll_never_halts_the_watchdog():
    """The one place in the module that swallows an exception. The count only gates
    NEW entries; position monitoring must not be taken down by a broker-read blip."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    answers = [_working_open_order(), None]

    async def live_orders():
        a = answers.pop(0)
        if a is None:
            raise RuntimeError("cert 502")
        return a

    wd, audit, _alerts, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(), live_orders=live_orders,
    )
    await wd._refresh_if_due()
    assert wd.working_open_spread_count == 1
    clock.advance(100.0)  # past refresh_interval_s
    await wd._refresh_if_due()  # this poll raises

    assert halts == [], "a working-order read failure must not halt the watchdog"
    assert wd.working_open_spread_count == 1, "last good count stands"
    assert "tier2:watchdog.working_orders_unread" in audit.events


async def test_no_live_orders_source_leaves_the_count_at_zero():
    """Shadow, and every construction site predating this."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, _audit, _alerts, _halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
    )
    await wd._refresh_if_due()
    assert wd.working_open_spread_count == 0


async def test_the_disagreement_escalates_on_the_rising_edge_only():
    """It persists on every refresh, so a Tier 1 record per cycle would be a trail
    nobody can read — the same reasoning as the unevaluable/unpriceable edges."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 2, ledger_positions=0,
    )
    for _ in range(4):
        await wd._check_broker_agreement()

    assert audit.events.count("watchdog.book_disagreement") == 1
    assert len(alerts) == 1
    assert audit.events.count("tier2:watchdog.book_disagreement_still") == 3


async def test_recovery_is_recorded_so_the_episode_has_an_end():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    broker = [2]
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: broker[0], ledger_positions=2,
    )
    await wd._check_broker_agreement()          # 2 vs 2 -> agrees, silent
    assert "watchdog.book_agrees" not in audit.events
    broker[0] = 4                                # disagree
    await wd._check_broker_agreement()
    broker[0] = 2                                # back in agreement
    await wd._check_broker_agreement()

    assert audit.payload("watchdog.book_agrees")["broker_legs"] == 2


async def test_a_ledger_holding_more_than_the_broker_also_flags():
    """Still a disagreement worth paging on — it just has to outlive the settle
    allowance first (see the settle-window block below)."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 0, ledger_positions=2,
    )
    await wd._check_broker_agreement()
    clock.advance(11.0)                      # allowance is 10.0 in the test config
    await wd._check_broker_agreement()
    assert audit.payload("watchdog.book_disagreement")["direction"] == "booked_but_absent"


async def test_an_unreadable_account_snapshot_skips_rather_than_guessing():
    """None means the account snapshot is unusable. The gate already fails closed on
    that, so nothing new is entered; inventing a comparison would be a false alarm."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: None, ledger_positions=5,
    )
    await wd._check_broker_agreement()
    assert audit.events == [] and alerts == []


async def test_the_check_is_off_when_not_wired_so_existing_callers_are_unchanged():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
    )
    await wd._check_broker_agreement()
    assert audit.events == [] and alerts == []


# -- the booked_but_absent settle allowance (added 2026-08-13) --------------
#
# The first live-fire pass produced a booked_but_absent disagreement on EVERY entry
# (0 vs 2, then 1 vs 2), resolving in 5.01s and 4.01s, because the ledger books an
# AlertStreamer fill before the broker's own position book reflects it. That is one
# ERROR page per entry for routine propagation lag. The allowance applies to THAT
# DIRECTION ONLY; held_but_unbooked is the unwatched-position case and stays instant.
# Test config: book_settle_s = 10.0.


async def test_booked_but_absent_is_silent_inside_the_settle_allowance():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 0, ledger_positions=2,
    )
    await wd._check_broker_agreement()
    clock.advance(9.0)                       # still inside the 10.0s allowance
    await wd._check_broker_agreement()

    assert "watchdog.book_disagreement" not in audit.events
    assert alerts == []
    # Suppressed is not silent: the Tier 2 trail says how far into the allowance.
    assert audit.events.count("tier2:watchdog.book_disagreement_still") == 2
    assert audit.payloads[-1][1]["settling_s"] == 9.0
    assert audit.payloads[-1][1]["settle_allowance_s"] == 10.0


async def test_a_disagreement_that_outlives_the_allowance_pages_exactly_as_before():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 1, ledger_positions=2,
    )
    await wd._check_broker_agreement()
    assert alerts == []
    clock.advance(10.0)                      # exactly at the boundary — allowance is over
    await wd._check_broker_agreement()

    payload = audit.payload("watchdog.book_disagreement")
    assert payload["direction"] == "booked_but_absent"
    assert payload["broker_legs"] == 1 and payload["ledger_legs"] == 2
    assert [k for k, _ in alerts] == ["watchdog_book_disagreement"]
    assert halts == [], "it pages; it does not halt"


async def test_a_disagreement_resolving_inside_the_allowance_never_pages():
    """The 2026-08-13 case exactly: 0 vs 2 at the fill, agreed 5.01s later."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    broker = [0]
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: broker[0], ledger_positions=2,
    )
    await wd._check_broker_agreement()
    clock.advance(5.01)
    broker[0] = 2                            # the broker's book caught up
    await wd._check_broker_agreement()

    assert "watchdog.book_disagreement" not in audit.events
    assert alerts == []
    # No Tier 1 pair to close (nothing paged), but the episode is reconstructable —
    # and its duration is what book_settle_s gets re-tuned from.
    assert "watchdog.book_agrees" not in audit.events
    assert audit.payload("watchdog.book_settled")["settled_after_s"] == 5.01


async def test_held_but_unbooked_pages_on_the_first_observation_regardless():
    """The dangerous direction. A real position may be held with nothing watching
    it, so no amount of settle time makes waiting the right answer.

    Nothing here closed anything, so there is no closing fill to explain the
    disagreement — the 2026-08-20 discriminator does not apply and must not fire."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 2, ledger_positions=0,
    )
    await wd._check_broker_agreement()       # no clock advance at all

    assert audit.payload("watchdog.book_disagreement")["direction"] == "held_but_unbooked"
    assert len(alerts) == 1


# -- held_but_unbooked after Argus's OWN closing fill (2026-08-20) -------------
#
# The mirror image of booked_but_absent, produced by the same physics on the way
# out: Argus deletes the `positions` row the moment the closing fill is booked, and
# the broker's own book trails it by seconds. Today's case fired 1.215s after the
# closing fill and cleared 6.012s later on its own.
#
# A blanket timer in this direction is the WRONG fix — it would suppress a
# genuinely orphaned position for the whole window. The discriminator is Argus's
# own record of a closing fill on the legs that left the ledger.


def _closed(*symbols: str, contracts: int = 2):
    """`AppliedFill`s for legs Argus just closed to flat."""
    from argus.execution.models import LegAction
    from argus.execution.store import AppliedFill

    return tuple(
        AppliedFill(
            broker_order_id=1001,
            external_identifier="argus-close-20260729-put-x",
            symbol=symbol,
            action=LegAction.BUY_TO_CLOSE,
            contracts=contracts,
            signed_delta=contracts,
            price=1.70,
            position_before=-contracts,
            position_after=0,
        )
        for symbol in symbols
    )


async def test_held_but_unbooked_right_after_our_own_close_settles_silently():
    """The 2026-08-20 case exactly: fired 1.215s after the closing fill, cleared
    6.012s later. Ordinary post-exit propagation lag, not an unwatched position."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    broker = [2]
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: broker[0], ledger_positions=0,
    )
    wd.note_fills(_closed(SHORT_OCC, LONG_OCC))    # Argus books its own exit ...
    clock.advance(1.215)                           # ... and the broker still shows 2
    await wd._check_broker_agreement()

    assert "watchdog.book_disagreement" not in audit.events
    assert alerts == []
    still = audit.payload("watchdog.book_disagreement_still")
    assert still["direction"] == "held_but_unbooked"
    assert still["closing_fill_legs"] == sorted([SHORT_OCC, LONG_OCC])

    clock.advance(6.012)                           # the broker's book caught up
    broker[0] = 0
    await wd._check_broker_agreement()
    assert alerts == []
    assert audit.payload("watchdog.book_settled")["settled_after_s"] == 6.012


async def test_a_closing_fill_covers_only_the_legs_it_actually_closed():
    """The blanket timer this replaces would suppress the whole window. One closed
    leg does not explain TWO unbooked ones — the extra is unaccounted for."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 3, ledger_positions=1,
    )
    wd.note_fills(_closed(SHORT_OCC))              # one leg closed, excess is 2
    clock.advance(0.5)
    await wd._check_broker_agreement()

    assert audit.payload("watchdog.book_disagreement")["direction"] == "held_but_unbooked"
    assert len(alerts) == 1


async def test_an_opening_fill_never_excuses_a_held_but_unbooked_disagreement():
    """Only a CLOSE explains a ledger that leads the broker downward. An opening
    fill moves the count the other way and must not buy an allowance."""
    from argus.execution.models import LegAction
    from argus.execution.store import AppliedFill

    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 2, ledger_positions=0,
    )
    wd.note_fills((
        AppliedFill(
            broker_order_id=1000, external_identifier="argus-open-20260729-put-x",
            symbol=SHORT_OCC, action=LegAction.SELL_TO_OPEN, contracts=2,
            signed_delta=-2, price=0.85, position_before=0, position_after=-2,
        ),
    ))
    await wd._check_broker_agreement()

    assert audit.payload("watchdog.book_disagreement")["direction"] == "held_but_unbooked"
    assert len(alerts) == 1


async def test_a_relabelled_opening_fill_never_books_exit_cover():
    """Seq 420587's exact shape, and the `note_fills` half of the 2026-09-03 fix.
    An OPENING fill on a chained pair comes back `Buy to Close` with
    `position_after == 0` — both literals of the old rule — because the account
    was short that strike from a neighbouring spread. The identifier Argus minted
    says open, so it is NOT an exit cover: `_closed_legs` stays empty,
    `held_but_unbooked` still pages, and the disagreement is Tier 2 telemetry
    alone."""
    from argus.execution.models import LegAction
    from argus.execution.store import AppliedFill

    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 2, ledger_positions=0,
    )
    wd.note_fills((
        AppliedFill(
            broker_order_id=1405428,
            external_identifier="argus-open-20260825-call-7690-7695",
            symbol="SPXW  260825C07695000",
            action=LegAction.BUY_TO_CLOSE,
            contracts=2,
            signed_delta=2,
            price=1.2,
            position_before=-2,
            position_after=0,
        ),
    ))
    assert wd._closed_legs == {}                 # no false suppression
    await wd._check_broker_agreement()
    assert audit.payload("watchdog.book_disagreement")["direction"] == "held_but_unbooked"
    assert len(alerts) == 1
    # Tier 2 and ONLY Tier 2 — never a record, never an exception.
    assert "tier2:watchdog.note_fills_disagreement" in audit.events
    assert "watchdog.note_fills_disagreement" not in audit.events
    payload = audit.payload("watchdog.note_fills_disagreement")
    assert payload["external_identifier"] == "argus-open-20260825-call-7690-7695"
    assert payload["broker_order_id"] == 1405428
    assert payload["action"] == "Buy to Close"


async def test_the_closing_fill_evidence_expires_and_then_it_pages():
    """The allowance is the fill record's own lifetime. A disagreement that outlives
    it is no longer explained by propagation lag."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 2, ledger_positions=0,
    )
    wd.note_fills(_closed(SHORT_OCC, LONG_OCC))
    clock.advance(1.0)
    await wd._check_broker_agreement()
    assert alerts == []

    clock.advance(10.0)                            # past book_settle_s = 10.0
    await wd._check_broker_agreement()

    assert audit.payload("watchdog.book_disagreement")["direction"] == "held_but_unbooked"
    assert len(alerts) == 1


async def test_a_partial_close_leaves_the_leg_booked_and_buys_no_allowance():
    """Only a leg that went FLAT left the ledger. A partial close keeps its row, so
    it cannot be the reason the ledger is short."""
    from argus.execution.models import LegAction
    from argus.execution.store import AppliedFill

    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 2, ledger_positions=1,
    )
    wd.note_fills((
        AppliedFill(
            broker_order_id=1001, external_identifier="argus-close-20260729-put-x",
            symbol=SHORT_OCC, action=LegAction.BUY_TO_CLOSE, contracts=1,
            signed_delta=1, price=1.70, position_before=-2, position_after=-1,
        ),
    ))
    await wd._check_broker_agreement()

    assert audit.payload("watchdog.book_disagreement")["direction"] == "held_but_unbooked"
    assert len(alerts) == 1


async def test_a_flip_into_held_but_unbooked_ends_the_allowance_immediately():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    broker = [1]
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: broker[0], ledger_positions=2,
    )
    await wd._check_broker_agreement()       # booked_but_absent, settling
    assert alerts == []
    clock.advance(1.0)
    broker[0] = 4                            # now the broker holds MORE than the ledger
    await wd._check_broker_agreement()

    assert audit.payload("watchdog.book_disagreement")["direction"] == "held_but_unbooked"
    assert len(alerts) == 1


async def test_each_episode_gets_a_fresh_allowance():
    """One entry's propagation lag must not spend the next entry's allowance."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    broker = [0]
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: broker[0], ledger_positions=2,
    )
    await wd._check_broker_agreement()       # episode 1 starts
    clock.advance(9.0)
    broker[0] = 2
    await wd._check_broker_agreement()       # episode 1 resolves, 9.0s in
    broker[0] = 0
    await wd._check_broker_agreement()       # episode 2 starts — its own window
    clock.advance(9.0)
    await wd._check_broker_agreement()

    assert "watchdog.book_disagreement" not in audit.events and alerts == []
    clock.advance(1.0)
    await wd._check_broker_agreement()
    assert audit.payload("watchdog.book_disagreement")["direction"] == "booked_but_absent"


async def test_a_zero_allowance_restores_the_instant_page():
    """The escape hatch is real: 0 is the pre-2026-08-13 behaviour, one line of
    config away, with no code path of its own to rot."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        config=make_config(watchdog={"book_settle_s": 0.0}),
        broker_position_count=lambda: 0, ledger_positions=2,
    )
    await wd._check_broker_agreement()

    assert audit.payload("watchdog.book_disagreement")["direction"] == "booked_but_absent"
    assert len(alerts) == 1


async def test_the_allowance_does_not_consume_the_rising_edge():
    """The subtle failure this could have had: marking the escalation slot during
    the window would make `rising()` return False forever after, so the page would
    never come. Suppressed observations must not arm the edge."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(),
        broker_position_count=lambda: 0, ledger_positions=2,
    )
    for _ in range(5):                       # five suppressed observations
        clock.advance(1.0)
        await wd._check_broker_agreement()
    clock.advance(10.0)
    await wd._check_broker_agreement()

    assert audit.events.count("watchdog.book_disagreement") == 1
    assert len(alerts) == 1


def test_half_a_comparison_is_refused_at_construction():
    """Both-or-neither, like Reconciler(execution=, order_store=). One without the
    other is not a weaker check — it is a check that silently never runs."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    with pytest.raises(ValueError, match="must be passed together"):
        build_watchdog(clock=clock, quotes=quotes, scan=OpenSpreadScan(),
                       broker_position_count=lambda: 2)


# -- unwatchable reporting: settle allowance + one page per episode (2026-08-17) --
#
# The two legs of ONE multi-leg order book as separate fill events (~0.25s apart),
# and `on_filled` calls `request_refresh()` when the first lands — so the watchdog
# aims straight at the window where a spread legitimately has one applied leg. And
# until 2026-08-17 this path had no escalation at all: 66 Tier 1 records and a page
# every 15s for one unchanging fact.

_HALF_BOOKED = OpenSpreadScan(
    malformed=(
        {
            "broker_order_id": 1344929,
            "candidate_id": "20260817-put-7750-7745",
            "problem": "outstanding legs are not one short and one long",
        },
    ),
)


def _unwatchable_watchdog(clock, quotes, scan):
    return build_watchdog(clock=clock, quotes=quotes, scan=scan)


async def test_a_half_booked_order_is_suppressed_inside_the_settle_allowance():
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    watchdog, audit, alerts, _ = _unwatchable_watchdog(clock, quotes, _HALF_BOOKED)

    await watchdog.check_once()
    assert alerts == [], "the mid-fill window must not page"
    assert "watchdog.unwatchable" not in audit.events
    assert "tier2:watchdog.unwatchable_still" in audit.events, "suppressed, not silent"


async def test_a_half_booked_order_that_resolves_never_pages_at_all():
    """The whole point: the second leg lands and the episode closes with a Tier 2
    duration record — which is what says whether the allowance is sized right."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    source = FakeSpreadSource(_HALF_BOOKED)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=_HALF_BOOKED
    )
    watchdog._spreads = source

    await watchdog.check_once()
    clock.advance(0.25)                      # the second leg books
    source.scan = OpenSpreadScan()
    watchdog.request_refresh()
    await watchdog.check_once()

    assert alerts == []
    assert "watchdog.unwatchable" not in audit.events
    assert "tier2:watchdog.unwatchable_settled" in audit.events


async def test_a_condition_surviving_the_allowance_pages_ONCE_then_stays_tier_2():
    """The 66-records fix. Persisting is not new information."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    watchdog, audit, alerts, _ = _unwatchable_watchdog(clock, quotes, _HALF_BOOKED)

    await watchdog.check_once()                      # inside the allowance
    clock.advance(30.0)                              # past book_settle_s
    for _ in range(5):
        watchdog.request_refresh()
        await watchdog.check_once()

    assert audit.events.count("watchdog.unwatchable") == 1
    assert len([k for k, _ in alerts if k == "watchdog_unwatchable_position"]) == 1
    assert audit.events.count("tier2:watchdog.unwatchable_still") >= 4


async def test_a_DIFFERENT_problem_is_a_new_episode_and_pages_again():
    """Signature-keyed, so suppression never hides a genuinely new fault."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    source = FakeSpreadSource(_HALF_BOOKED)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=_HALF_BOOKED
    )
    watchdog._spreads = source

    # The window opens on the FIRST observation, so it takes a tick to start it and
    # a later tick to clear the allowance.
    await watchdog.check_once()
    clock.advance(30.0)
    watchdog.request_refresh()
    await watchdog.check_once()
    assert audit.events.count("watchdog.unwatchable") == 1

    source.scan = OpenSpreadScan(
        malformed=({"broker_order_id": 999, "problem": "something else entirely"},),
    )
    watchdog.request_refresh()
    await watchdog.check_once()          # new signature -> new window
    clock.advance(30.0)
    watchdog.request_refresh()
    await watchdog.check_once()
    assert audit.events.count("watchdog.unwatchable") == 2


async def test_an_unattributed_position_pages_on_sight_with_no_allowance():
    """The asymmetry, same as `held_but_unbooked`: a `positions` row no order
    explains has no pending second leg that would resolve it, so waiting only
    delays the page."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    scan = OpenSpreadScan(unattributed=("SPXW  260817P07745000",))
    watchdog, audit, alerts, _ = _unwatchable_watchdog(clock, quotes, scan)

    await watchdog.check_once()          # no clock advance at all
    assert audit.events.count("watchdog.unwatchable") == 1
    assert any(k == "watchdog_unwatchable_position" for k, _ in alerts)


async def test_recovery_after_a_page_is_itself_tier_1():
    """An operator told a position is unwatched must be told when it is watched
    again — the same rising/falling pairing as the book-agreement check."""
    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    source = FakeSpreadSource(_HALF_BOOKED)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=_HALF_BOOKED
    )
    watchdog._spreads = source

    await watchdog.check_once()          # opens the allowance window
    clock.advance(30.0)
    watchdog.request_refresh()
    await watchdog.check_once()          # survives it -> pages
    assert audit.events.count("watchdog.unwatchable") == 1

    source.scan = OpenSpreadScan()
    watchdog.request_refresh()
    await watchdog.check_once()

    assert "watchdog.watchable_again" in audit.events
    assert any(k == "watchdog_watchable_again" for k, _ in alerts)
