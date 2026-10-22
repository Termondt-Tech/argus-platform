"""The position risk watchdog and the exit handoff (spec §4, §7 exit variant).

Spec §4 makes this a SEPARATE task from the signal engine, and the reason is the
one sentence in the spec that this whole module exists to honour: "gamma risk in
the final hour doesn't care whether entry logic is running." So nothing here is
driven by, gated on, or shares state with `SignalLoop`. It watches what is already
held, continuously, and it can act on its own.

What it decides, and what it delegates:

    decides    WHEN a position must be closed (three emergency triggers, debounced
               or clocked) — and when it MAY be (the profit target, 2026-09-23,
               which never crosses and never spends an emergency allowance)
    delegates  HOW the close is priced and routed:
                 - the order comes from chunk 1's `build.py` (single-legging,
                   signed tick-aligned limit, freshness bound)
                 - the walk comes from chunk 3's `PriceWalkingLoop`, UNMODIFIED,
                   parameterized by `WalkPolicy.for_exit` (or `for_profit_target`)
                 - the record comes from chunk 2's `AuditedOrderExecution`

The market-crossing order is the one piece of pricing that lives here, and
deliberately so: "give up on the auction and pay what it takes" is exit business
logic, not something the shared walk engine should know about. Chunk 3's loop has
no concept of crossing and gains none.

**Three properties worth stating up front.**

**1. The OCC symbol an order references is NOT the symbol market data is keyed
by.** `OptionContract` pairs them (`symbol` vs `streamer_symbol`) and the two are
different strings. A watchdog that asked `market.latest(order_leg.symbol)` would
get `None` forever — a stop that never fires, failing silently, discovered by
losing money. `LegQuotes` is the resolution step, and it reports WHY it could not
resolve rather than returning a bare None that reads as "no trigger".

**2. "I cannot evaluate this" is never "no trigger".** A missing or stale quote
does not clear a sustained condition and does not reset its clock — there is no
evidence the condition went away, and for a watchdog the fail-closed direction is
toward acting, not toward waiting. An unevaluable position is recorded Tier 1 and
alerted on the edge (once on entry, once on recovery, Tier 2 in between, the same
pattern `account/stream.py` uses for its refresh failures).

**3. The underlying trigger is evaluated even when leg quotes are not.** It needs
only spot and the short strike, and the short strike does not come from a quote:
`LegQuotes.resolve` reads the contract out of the subscribed chain index BEFORE it
touches market data, and `LegResolution` carries that identity out of every branch
where the symbol was found — including the stale and no-quote branches. **So a leg
that cannot be PRICED still reports its strike and its right**, which is what keeps
this trigger alive through exactly the blown-out-spread scenario it is the backstop
for. The rule that enforces it: anything wanting a strike or a right reads
`resolution.contract`, never `resolution.leg.contract`; `.leg` is for prices only.
If the trigger fires while the legs cannot be priced, no order can be constructed:
that is recorded, alerted, and RETRIED next cycle rather than consuming the
position's one exit attempt.

**The staleness threshold is not the lever here.** A 0DTE wing tens of points OTM
updates every few seconds and breaches a 2s bound routinely — that is the quote
feed being honest, not a misconfiguration, and `market_data_max_age_s` also governs
entry pricing and the pre-trade gate's staleness rail. Widening it to rescue a
trigger would quietly relax two unrelated fail-closed checks.
→ `docs/decisions/2026-08-13-live-fire-sandbox-pass.md`, Finding 4, for what this
cost before the identity/price split existed.

**On exception policy.** An unhandled exception in this task engages the kill switch
at `HaltGrade.ENTRIES` and **keeps watching**. The old posture — halt `ALL`, set the
shared stop event, return — was signed off on the reasoning that a crashed iteration
leaves a real position with nobody watching it, and it *produced* that outcome rather
than preventing it: on 2026-09-03 it cost ≈$670 on one cert 502. Entries stop
immediately and permanently, because a watchdog that raised cannot be trusted to
stop out what a new entry would open; everything else runs on, because the watchdog
is the only thing that can close or flatten what is already held. `AuditWriteError`
is the one exclusion and keeps the old posture verbatim.
→ docs/designs/2026-09-10-a-watchdog-exception-must-not-take-the-watchdog.md
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dt_time  # `time` is the stdlib module, imported above
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from argus.alerts import AlertFn, log_alert
from argus.audit.log import AuditLog, AuditWriteError
from argus.clock import et_now, et_today
from argus.config import Config
from argus.execution.audited import AuditedOrderExecution
from argus.execution.build import (
    StaleSignalError,
    UnpriceableSpreadError,
    build_order,
    cap_at_width,
)
# Moved out of this module 2026-08-30 so the residual-order recheck can share the
# ONE implementation without closing an import ring (see `escalation.py`).
# Re-exported under its original name: every reference here and in the tests is
# unchanged.
from argus.execution.escalation import _Escalation
from argus.execution.flatten_deferral import (
    DeferralDecision,
    DeferralInputs,
    DeferralMode,
    DeferralParams,
    decide as decide_deferral,
)
from argus.execution.guard import DuplicateOrderGuard
from argus.execution.loop import (
    GuardUnreachable,
    LiveOrderView,
    OrderView,
    PriceWalk,
    PriceWalkingLoop,
    WalkOutcome,
    WalkPolicy,
    WalkResult,
)
from argus.execution.models import (
    LiveOrders,
    Order,
    OrderDirection,
    OrderDisposition,
    classify_order,
    parse_external_identifier,
    sibling_identifier,
)
from argus.execution.residual_orders import ResidualOrderRecheck
from argus.execution.working_orders import (
    open_working_spread_equivalents,
    open_working_spreads_by_side,
    open_working_worst_case,
    side_of_identifier,
)
from argus.execution.store import (
    AppliedFill,
    CreditBasis,
    LedgerState,
    OpenSpread,
    OpenSpreadScan,
    OpenSpreadSource,
)
from argus.execution.zerobid import (
    PRICE_EPSILON,
    TICK,
    is_finite,
    round_to_tick,
    spread_mid,
)
# `HaltGrade` from here rather than a new `argus/halt.py`: reconcile imports only
# `argus.execution.models` and `argus.execution.store`, neither of which imports this
# module, so the dependency adds no ring — and moving the enum would touch `app.py`,
# `session.py`, `status.py`, `control.py` and their tests for no behavioural gain
# (§3.1 of the 2026-09-10 design). The grade is the verdict's own property, so it is
# passed HERE, at the failing call site, exactly as `reconcile.py` passes its own.
from argus.reconcile import HaltGrade
from argus.risk.loss_budget import (
    CONTRACT_MULTIPLIER,
    Exposure,
    trade_idea_key,
    vertical_width,
)
# The stub-close allowance's boot read (2026-09-24). `risk.rehydrate` imports no
# execution module (its own FILL_CATEGORY comment states the boundary, and a test
# pins it): it returns the rows' raw position keys, and the identifier GRAMMAR is
# applied here, in `rehydrate_stub_close`. This module already imports
# `risk.loss_budget`, so the dependency adds no ring.
from argus.risk.rehydrate import (
    StubCloseRehydrationError,
    load_stub_close_spent,
)
from argus.marketcalendar import MarketCalendar
from argus.marketdata.port import MarketDataPort
from argus.signal.chain import ChainSource
from argus.signal.contracts import OptionContract
from argus.signal.models import OptionQuote, SpreadSignal, payload_number
from argus.signal.realized import realized_vol
from argus.signal.regime import MIN_MINUTES_TO_SETTLEMENT, compute_regime
from argus.staleness import is_stale

logger = logging.getLogger("argus.execution.watchdog")

SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]
TradingDateFn = Callable[[], date]
#: Current EXCHANGE time, tz-aware. `clock.et_now` in production; a seam so a test
#: can put the watchdog either side of the day's flatten mark.
NowEtFn = Callable[[], datetime]
#: halt(reason, *, source, detail) — the supervisor's engage-kill-switch +
#: request-stop pairing (`Application._halt_process`). Same seam `session.py` takes
#: for a sandbox/live reconcile mismatch; the watchdog holds neither the kill
#: switch nor the stop event, so the halt is injected.
HaltFn = Callable[..., Awaitable[None]]

#: Attributed to the kill-switch trip when the watchdog dies unexpectedly.
HALT_SOURCE = "watchdog_exception"


# -- resolving an order's legs to live quotes ---------------------------------


@runtime_checkable
class ContractDirectory(Protocol):
    """Anything that knows the OCC <-> streamer-symbol pairing for today's chain.

    Satisfied structurally by `DXLinkChainSource` (its `contracts` property), so
    the watchdog reuses the SAME subscribed chain the signal engine already has
    rather than opening a second quote path.
    """

    @property
    def contracts(self) -> tuple[OptionContract, ...]: ...


@dataclass(frozen=True)
class LegQuote:
    """One leg of a held spread, priced from the live feed."""

    contract: OptionContract
    bid: float
    ask: float
    #: Monotonic timestamp of the quote, on the same clock as `age_s`.
    received_at: float
    age_s: float
    delta: float

    @property
    def symbol(self) -> str:
        """The OCC symbol — what an order references."""
        return self.contract.symbol

    def as_option_quote(self) -> OptionQuote:
        """The signal-layer view, so chunk 1's builder can price this leg.

        `delta` is carried for the record only: `build_order` never reads it, and an
        exit selects no strikes — the strikes are whatever is already held. It is 0.0
        when the greeks feed has not reported, rather than NaN, because a NaN would
        make the surrounding audit payload unserializable.
        """
        return OptionQuote(
            symbol=self.contract.symbol,
            strike=self.contract.strike,
            right=self.contract.right,
            delta=self.delta,
            bid=self.bid,
            ask=self.ask,
        )


@dataclass(frozen=True)
class LegResolution:
    """A resolved leg, or a stated reason it could not be resolved.

    Deliberately not `LegQuote | None`: the two failures (this OCC symbol is not in
    the subscribed chain / it is but the quote is stale) have completely different
    causes and completely different fixes, and collapsing them into None is how the
    silent-never-fires bug in the module docstring happens.

    **IDENTITY AND PRICE ARE SEPARATE FACTS, and conflating them cost a live
    position its backstop on 2026-08-13.** `identity` is the chain lookup — strike
    and right — and it is known whenever the OCC symbol is in the subscribed chain,
    *whether or not a fresh quote exists for it*. Returning `leg=None` on a stale
    quote used to throw the contract away with the price, which made the underlying
    trigger unevaluable for want of a strike that was sitting in the index the whole
    time. Read `contract` (never `leg.contract`) anywhere the strike or the right is
    what is wanted; read `leg` only where a PRICE is needed.
    """

    leg: LegQuote | None = None
    problem: str | None = None
    #: Set whenever the symbol was found in the chain, including the unpriced
    #: branches. Not set in the priced branch — `contract` reads it off the leg
    #: there, so the two can never disagree.
    identity: OptionContract | None = None

    @property
    def ok(self) -> bool:
        return self.leg is not None

    @property
    def contract(self) -> OptionContract | None:
        """The contract, if this leg's IDENTITY is known. Independent of freshness."""
        return self.leg.contract if self.leg is not None else self.identity


@runtime_checkable
class LegQuoteSource(Protocol):
    """The watchdog's view of the market. Narrow, so it fakes in a few lines."""

    def resolve(self, occ_symbol: str) -> LegResolution: ...

    def spot(self) -> float | None:
        """Underlying price, or None if it is missing or stale (fail closed)."""


class LegQuotes:
    """`LegQuoteSource` over a `MarketDataPort` plus the subscribed chain.

    The whole of this class is the OCC -> streamer-symbol translation and the
    staleness gate on top of it. Both are one-liners; the reason it is a class is
    that both must happen at EVERY call site and neither may be skipped.

    Staleness uses `argus.staleness.is_stale` — the one fail-closed definition the
    pre-trade gate uses — so a NaN age or a clock going backwards reads as stale
    here too.
    """

    def __init__(
        self,
        market: MarketDataPort,
        directory: ContractDirectory,
        *,
        underlying_symbol: str,
        max_age_s: float,
    ) -> None:
        self._market = market
        self._directory = directory
        self._underlying = underlying_symbol
        self._max_age_s = max_age_s

        self._cached_for: tuple[OptionContract, ...] | None = None
        self._index_cache: dict[str, OptionContract] = {}

    def _index(self) -> dict[str, OptionContract]:
        """OCC -> contract, cached against the contract tuple's IDENTITY.

        `DXLinkChainSource` replaces its `contracts` tuple wholesale when the chain
        is loaded, and the tuple is immutable, so identity is a sound and exact
        invalidation signal — a reloaded chain cannot be served from a stale cache.
        Rebuilding it per lookup would be a few-hundred-entry dict comprehension
        twice a position per cadence tick, for no reason.
        """
        contracts = self._directory.contracts
        if contracts is not self._cached_for:
            self._index_cache = {c.symbol: c for c in contracts}
            self._cached_for = contracts
        return self._index_cache

    def resolve(self, occ_symbol: str) -> LegResolution:
        contract = self._index().get(occ_symbol)
        if contract is None:
            return LegResolution(
                problem=(
                    f"{occ_symbol} is not in the subscribed chain — it cannot be "
                    "priced, so this position cannot be evaluated or closed"
                )
            )
        # From here the leg's IDENTITY is known and is carried out of every branch.
        # A 0DTE wing tens of points OTM updates every few seconds and goes stale
        # against a 2s bound constantly (14% of ticks by the end of 2026-08-13) —
        # that must cost this leg its PRICE, never its strike.
        quote = self._market.latest(contract.streamer_symbol)
        if quote is None:
            return LegResolution(
                identity=contract,
                problem=f"no quote yet for {contract.streamer_symbol}",
            )
        age = self._market.age(contract.streamer_symbol)
        if is_stale(age, self._max_age_s):
            return LegResolution(
                identity=contract,
                problem=(
                    f"quote for {contract.streamer_symbol} is {age:.2f}s old "
                    f"(max {self._max_age_s:g}s)"
                ),
            )
        greeks = self._market.greeks(contract.streamer_symbol)
        return LegResolution(
            leg=LegQuote(
                contract=contract,
                bid=quote.bid,
                ask=quote.ask,
                received_at=quote.received_at,
                age_s=age,
                delta=greeks.delta if greeks is not None else 0.0,
            )
        )

    def spot(self) -> float | None:
        quote = self._market.latest(self._underlying)
        if quote is None or is_stale(self._market.age(self._underlying), self._max_age_s):
            return None
        mid = quote.mid
        return mid if mid is not None and is_finite(mid) else None


@dataclass(frozen=True)
class SpreadMarket:
    """Both legs of a held spread, priced. The input to every trigger and to pricing."""

    short: LegQuote
    long: LegQuote

    @property
    def width(self) -> float:
        """ACTUAL strike distance — never the configured desired width."""
        return abs(self.short.contract.strike - self.long.contract.strike)

    @property
    def cost_to_close(self) -> float:
        """Net mid to buy the spread back, in points.

        The SAME `zerobid.spread_mid` the entry used, on the same leg ordering, so
        the number the stop is measured against is arithmetically comparable to the
        credit the entry recorded. Positive while the spread still has value.
        """
        return spread_mid(self.short.bid, self.short.ask, self.long.bid, self.long.ask)

    @property
    def crossing_debit(self) -> float:
        """What it costs to take the spread out of the market RIGHT NOW.

        Pay the short leg's ask, receive the long leg's bid. The zero-bid floor is
        deliberately NOT applied: flooring the long bid UP to a tick would lower the
        debit and make the order LESS aggressive, which is backwards for the one
        order whose entire purpose is to be filled. A genuine 0.0 bid on the long
        leg means it is worth nothing and we should expect nothing for it.
        """
        return self.short.ask - self.long.bid

    @property
    def captured_at(self) -> float:
        """The OLDER of the two quote timestamps — the conservative snapshot age.

        Fed to `SpreadSignal.captured_at`, so `build_order`'s freshness bound is
        measured against the stalest input rather than the freshest.
        """
        return min(self.short.received_at, self.long.received_at)


# -- the flatten deferral's volatility inputs ---------------------------------


@runtime_checkable
class ImpliedVolSource(Protocol):
    """The ATM implied vol the flatten deferral standardizes by. (2026-09-25)

    A seam so the watchdog can be tested with a number, and so production's one
    implementation (`ChainImpliedVol`) is pinned against `compute_regime` directly.
    Returns None whenever no usable reading exists — never NaN, never a default.
    """

    def atm_iv(self, *, now_et: datetime, today: date) -> float | None: ...


class ChainImpliedVol:
    """ATM IV for the flatten deferral, computed BY THE WATCHDOG from the shared chain.

    **The same pure `regime.compute_regime` the signal loop runs, on the same chain
    source's snapshot, with the day's own close** (`MarketCalendar.close_time`, so a
    half-day shortens T correctly) — which is what makes the runtime number the one
    `tools.flatten_calibration` standardized every recorded window by. A wiring test
    asserts the two are identical on one snapshot.

    **It reads the CHAIN SOURCE, never `SignalLoop` state.** The chain source is the
    market-data subscription this module already shares through `LegQuotes` (it is
    `LegQuotes`' contract directory), so the spec's independence rule — "gamma risk in
    the final hour doesn't care whether entry logic is running" — holds: the signal loop
    can die and this keeps working. A snapshot the source refuses (None) is no reading.

    Below `regime.MIN_MINUTES_TO_SETTLEMENT` the estimator returns NaN by design; the
    watchdog carries forward its last reading taken since the mark (see
    `PositionWatchdog._deferral_vols`), which errs high because the estimate runs
    1.2–1.7× high in the last hour.
    """

    def __init__(self, chain: ChainSource, calendar: MarketCalendar) -> None:
        self._chain = chain
        self._calendar = calendar

    def atm_iv(self, *, now_et: datetime, today: date) -> float | None:
        snapshot = self._chain.snapshot()
        if snapshot is None:
            return None
        regime = compute_regime(
            snapshot,
            now_et=now_et.time(),
            today=today,
            close_et=self._calendar.close_time(today),
        )
        iv = regime.atm_iv
        return iv if math.isfinite(iv) and iv > 0.0 else None


def _minutes_to_close(now_et: datetime, close_et: dt_time) -> float:
    """Minutes from `now_et` to `close_et` on the same ET day. (2026-09-25 review)

    THE SAME arithmetic `regime._minutes_to_settlement` applies to the same two
    inputs (`now_et.time()`, `MarketCalendar.close_time(today)`), so the watchdog's
    "is this inside the estimator's NaN regime?" can never land on the other side of
    `MIN_MINUTES_TO_SETTLEMENT` from the estimator itself. Two readings of the clock
    a few milliseconds apart could — and a release on that one tick would hand a
    deferred flatten to the exit path. A test pins the equality.
    """
    anchor = date(2000, 1, 1)
    delta = datetime.combine(anchor, close_et) - datetime.combine(anchor, now_et.time())
    return delta.total_seconds() / 60.0


# -- the triggers --------------------------------------------------------------


class TriggerReason(StrEnum):
    """Why the watchdog wants out. Whichever fires first (spec §7 exit variant)."""

    #: Cost to close has reached `credit_multiple` x the credit received. The
    #: dominant, well-established stop for this strategy shape.
    CREDIT_MULTIPLE = "credit_multiple"
    #: The underlying has come within `underlying_buffer_points` of the short
    #: strike. The backstop: it fires on the thing that actually causes the loss,
    #: and it keeps working when the option market is too wide to price.
    UNDERLYING_NEAR_SHORT_STRIKE = "underlying_near_short_strike"
    #: `watchdog.flatten_before_close_s` before THIS DAY'S close (half-day aware —
    #: `MarketCalendar.flatten_mark`). The desk control, and the only trigger with no
    #: market input at all. (2026-08-28)
    #:
    #: The other two share a dependency the strategy cannot rely on at the one moment
    #: it matters: both had already fired on 2026-08-28 and the position was still
    #: held 76 minutes later. The thing that costs money on a 0DTE product is not
    #: failing to SEE the loss — it is seeing it, deciding, and being unable to ACT —
    #: and a second mechanism whose input is the same market that has stopped working
    #: is not a second mechanism. Gamma goes vertical into a PM settlement, so the
    #: standard control is a hard clock, and the clock is the point.
    #: → docs/decisions/2026-08-28-time-based-flatten-trigger.md
    FLATTEN_BEFORE_CLOSE = "flatten_before_close"
    #: The net mid rounds to exactly one tick — `PROFIT_TARGET_MARK`, the cheapest
    #: closing limit that can be expressed at all. (2026-09-23)
    #:
    #: **The only trigger that is not an emergency**, and every rule about it follows
    #: from that. Argus never holds to expiry — the flatten closes every survivor at
    #: 15:30 and pays the exit tick on every winner already — so the comparison is a
    #: tick NOW against the same tick hours later with width-sized gamma in between,
    #: not a tick against nothing. It therefore:
    #:
    #: - is ACTED ON only when nothing else fired (`_EMERGENCY_TRIGGER_REASONS` take
    #:   precedence), only while `watchdog.profit_target_enabled`, only while the
    #:   position's emergency exit is unspent, and only until its own budget retires;
    #: - never crosses the market, and a take that does not FILL never spends
    #:   `_attempted` or the flatten's override — a failed take must cost nothing, or
    #:   the feature manufactures the uncovered tail it exists to remove. (A take that
    #:   fills spends `_attempted` as any fill does: see `_after_profit_take`.)
    #: - is EVALUATED regardless, from the same `cost_to_close` in the same branch as
    #:   `CREDIT_MULTIPLE`, so the two are always evaluable or unknown together.
    #:
    #: **Equality at the tick, never the economic `mid <= 0.075`.** A mid that rounds
    #: to 0.00 is 44% of the near-worthless zone and `build._limit_price` refuses it,
    #: so the inequality would fire where no order can exist and page
    #: `watchdog_exit_still_unplaced` all afternoon on a WINNING position. Buildability
    #: belongs in the trigger.
    #: → docs/decisions/2026-09-22-profit-target-and-re-entry.md (§10, §11)
    PROFIT_TARGET = "profit_target"


#: Every trigger, derived so nothing can go missing from it.
_ALL_TRIGGER_REASONS = frozenset(TriggerReason)

#: The triggers that exist because a position MUST get flat — every one but the
#: profit target. (2026-09-23)
#:
#: What "the other triggers keep precedence" means, as one object: when any of these
#: has fired, the profit target is dropped from the reasons and the exit is the
#: emergency one, with its cross and its one attempt. And what "the stop condition is
#: still true" means on a spent position's mark — a winning spread marked at one tick
#: is not a stop that is still firing.
#:
#: Derived by subtraction, so a fifth trigger is an emergency by default and has to be
#: argued OUT of this set, which is the safe direction to be wrong in.
_EMERGENCY_TRIGGER_REASONS = _ALL_TRIGGER_REASONS - {TriggerReason.PROFIT_TARGET}

#: THE profit target's level: the cheapest closing limit that can be expressed.
#: (2026-09-23)
#:
#: A module constant rather than config, deliberately (§11.7): it is not a tunable
#: number, it is the minimum tick — and the record measured the next two notches up
#: (0.15, 0.20) as WORSE than doing nothing, because they buy the win rate with credit
#: the position would have kept. A knob one notch from the bad region, in a file that
#: already carries four standing landmines, is the wrong trade.
PROFIT_TARGET_MARK: Decimal = TICK
#: `at_profit_target`'s float pre-filter bound, computed once: a mark at or above two
#: ticks cannot round to one, and neither can anything non-positive.
_PROFIT_TARGET_BAND = float(PROFIT_TARGET_MARK * 2)


def at_profit_target(cost_to_close: float) -> bool:
    """Does this mark express exactly the cheapest closing limit? (2026-09-23)

    `round_to_tick` is the SAME rounding `build._limit_price` applies to the same
    number, so this is true exactly where a closing order builds at one tick — the
    trigger IS buildability at the minimum. A mid below 0.025 rounds to 0.00 and would
    be refused as `UnpriceableSpreadError`; 0.075 and up builds at 0.10 or more, which
    is past the point the take is free.

    **Total over every float, and it must stay total** (2026-09-23 review). It runs in
    `evaluate_triggers` AFTER the credit stop has been decided, so a raise here would
    discard an evaluation whose stop is true. `round_to_tick` is not total: it raises
    `ValueError` on a non-finite input and `decimal.InvalidOperation` once the quantized
    coefficient outgrows the context precision (a finite mark around 1e22, i.e. one
    corrupt leg quote). So the float band is tested FIRST, and only a mark already
    inside `(0, 2 x mark)` reaches the Decimal: a strict superset of the answer's true
    region `[0.025, 0.075)`, so the exact rounding below still decides every boundary,
    and NaN and both infinities fail the band. It is also the cheap exit for every
    position that is nowhere near its target, which is nearly all of them, every tick.
    """
    if not 0.0 < cost_to_close < _PROFIT_TARGET_BAND:
        return False
    return round_to_tick(cost_to_close) == PROFIT_TARGET_MARK


def _is_profit_take(reasons: Iterable[TriggerReason]) -> bool:
    """Is this exit a profit take — the profit target alone, and nothing else?

    THE one definition, read by `ExitExecutor.close` (which walk), `_record_exit`
    (whether to page) and `PositionWatchdog._run_exit` (which allowance a report may
    touch). Three readers of one question must not be able to disagree about it: the
    executor walking the no-cross ladder while the watchdog spent the emergency
    attempt on the report would be the exact hole §11.1 describes.

    `frozenset(...) ==`, never `in`: a set that also holds an emergency reason is an
    emergency exit, and the watchdog strips the profit target from such a set before
    it gets here — this is the second statement of that precedence, not the only one.
    """
    return frozenset(reasons) == frozenset({TriggerReason.PROFIT_TARGET})


def _deferral_field(deferral: DeferralDecision | None) -> dict[str, Any]:
    """The `flatten_deferral` field of a per-tick record. (2026-09-28)

    THE one builder for every tick the deferral decided on: `watchdog.check` when
    nothing fired, `watchdog.triggered` / `watchdog.triggered_still` when something did.
    Until 2026-09-28 only the first carried it, so a flatten the deferral DECLINED to
    defer — the one that fired, which is most of them — left its z, P_touch and lhs on
    no record at all (the 2026-09-28 objective audit, P11), and k could not be priced
    on live flattens. One builder, so the two records cannot spell the field
    differently. Absent, never null, on a tick the deferral did not decide on, so every
    older row keeps its exact shape.
    """
    return {} if deferral is None else {"flatten_deferral": deferral.to_payload()}


def _stub_handoff_reasons(
    origin: frozenset[TriggerReason], fired: Iterable[TriggerReason]
) -> frozenset[TriggerReason]:
    """The reasons a stub continuation is handed. (2026-09-24)

    The ORIGIN's reasons — why the close that partially filled was sent — plus
    every EMERGENCY reason firing now, with the profit target stripped whenever
    any emergency reason is present (the precedence `_may_act_on` and
    `_is_profit_take` already state: a set holding an emergency reason is an
    emergency exit). So:

    - a flatten- or stop-origin stub keeps the exit ladder and its cross even
      when nothing fires now — the close decision was already made, and a
      winning spread at 15:35 has no stop condition true;
    - a profit-origin stub re-takes on the no-cross profit ladder while its only
      reason is still the take, and closes on the EXIT ladder the moment a stop,
      the backstop or the flatten fires — a one-tick bid on a spread marked at
      twice its credit is never paid, and `close()` refuses to build one.

    Pure; `fired` is the tick's `_may_act_on` answer.
    """
    emergency_now = frozenset(fired) - {TriggerReason.PROFIT_TARGET}
    if not emergency_now:
        return origin
    return emergency_now | (origin - {TriggerReason.PROFIT_TARGET})

#: The triggers that depend on the MARKET, and therefore the ones that can be
#: UNDETERMINED. `TriggerEvaluation.blind` is "all of these are unknown".
#:
#: **This is not a tidy-up; `blind` is unreachable without it** (2026-08-28). The
#: comment this constant used to carry warned that a third trigger added without
#: updating a literal would silently retire the page, and prescribed deriving from
#: the enum — which is exactly what breaks here. `FLATTEN_BEFORE_CLOSE` needs no
#: quote, no chain lookup and no feed, so it can NEVER be unevaluable; the moment it
#: joins the enum, `unknown_reasons >= _ALL_TRIGGER_REASONS` is false for every
#: possible input and `watchdog.unevaluable` — the one evaluability state that pages
#: — stops firing, with no test failing. The comment was right about the failure and
#: wrong about the direction.
#:
#: So the meaning `blind` always had is stated explicitly: NO MARKET-DRIVEN condition
#: can be seen. A position blind to both price triggers at 11:00 is still an
#: emergency, and a clock that will fire on it at 15:30 is not a substitute for
#: watching it for the six hours in between.
#:
#: Still DERIVED: a fourth, market-dependent trigger joins this set automatically. A
#: second clock-shaped one would have to be excluded by hand, which is the right
#: place for a human decision.
#:
#: `PROFIT_TARGET` (2026-09-23) is that fourth, and joins exactly as written. It reads
#: `cost_to_close` in the same branch of `evaluate_triggers` as `CREDIT_MULTIPLE`, so
#: the two are unknown together on every input and the superset test is unchanged in
#: practice — `test_the_profit_target_is_unknown_exactly_when_the_credit_stop_is`
#: holds that, because evaluating it only while ENABLED would make `blind`
#: unreachable whenever the switch is off.
_MARKET_TRIGGER_REASONS = _ALL_TRIGGER_REASONS - {TriggerReason.FLATTEN_BEFORE_CLOSE}

#: The STOPS: the market-driven triggers that exist to get a losing position flat —
#: the credit trigger and the underlying backstop. Derived, so a new market-driven
#: emergency trigger joins automatically. (2026-09-27 review)
#:
#: **`blind` is measured against the stops that can ACT, never the whole market set**
#: (`TriggerThresholds.acting_stops`). Once `credit_trigger_enabled` could be false
#: (v0.5.2's strike-touch stop), the superset test over `_MARKET_TRIGGER_REASONS`
#: counted a switched-off credit trigger — and a profit target, which is not
#: protection — as "watching": a dead SPX feed with fresh legs read DEGRADED, paged
#: nothing, and left the position with no trigger able to fire. With the credit
#: trigger on, the two tests are identical on every input, because the profit target
#: is unknown exactly when the credit trigger is (same branch of `evaluate_triggers`).
_STOP_TRIGGER_REASONS = _MARKET_TRIGGER_REASONS & _EMERGENCY_TRIGGER_REASONS

#: Triggers that fire on the FIRST observation, bypassing `sustained_s`.
#:
#: The debounce exists for one stated reason (spec §7): "a 0DTE vertical's mid can
#: print through 2x credit on one wide quote and come straight back; acting on that
#: single observation converts a quote artifact into a realized loss." Every word of
#: that is about a MARKET observation. A clock reading is monotonic — it has no
#: artifact to filter and it cannot come straight back — so a sustain requirement can
#: only DELAY the control, never improve it.
#:
#: Explicit rather than incidental, deliberately. Letting the debounce apply
#: "harmlessly" would make a `sustained_s`-length delay that nobody chose, and
#: `argus.toml`'s own tuning note already contemplates changing that value.
_INSTANT_TRIGGER_REASONS = frozenset({TriggerReason.FLATTEN_BEFORE_CLOSE})


@dataclass(frozen=True)
class TriggerThresholds:
    """The two trigger levels, the debounce, and whether the profit target may act.
    Pure; built from `[watchdog]` config."""

    credit_multiple: float
    underlying_buffer_points: float
    sustained_s: float
    #: `watchdog.profit_target_enabled` (2026-09-23). NOT read by `evaluate_triggers`,
    #: deliberately: the trigger is evaluated either way (see `_MARKET_TRIGGER_REASONS`
    #: for why it must be), and this decides only whether the watchdog may ACT on it.
    #: No default, on `WalkPolicy.min_concession_steps`' rule — a default would be the
    #: one silent way to switch a live behaviour on or off.
    profit_target_enabled: bool
    #: `watchdog.credit_trigger_enabled` (2026-09-26). Read by `evaluate_triggers`,
    #: but ONLY where the credit trigger would be added to `true_reasons`: the mark,
    #: `credit_stop` and the `unknown` bookkeeping are computed exactly as before, so
    #: the profit target (same branch) and `blind` (same superset test) cannot notice
    #: the switch. False is the strike-touch stop. No default, same rule as above.
    credit_trigger_enabled: bool

    @classmethod
    def from_config(cls, config: Config) -> "TriggerThresholds":
        wd = config.watchdog
        return cls(
            credit_multiple=wd.credit_multiple,
            underlying_buffer_points=wd.underlying_buffer_points,
            sustained_s=wd.sustained_s,
            profit_target_enabled=wd.profit_target_enabled,
            credit_trigger_enabled=wd.credit_trigger_enabled,
        )

    @property
    def acting_stops(self) -> frozenset[TriggerReason]:
        """The stops that can FIRE under this config. What `blind` is measured against.

        `_STOP_TRIGGER_REASONS` less a switched-off credit trigger (2026-09-27 review).
        Under the strike-touch stop this is the underlying backstop alone, so losing
        spot alone is blind: nothing left can close the position before the flatten.
        """
        if self.credit_trigger_enabled:
            return _STOP_TRIGGER_REASONS
        return _STOP_TRIGGER_REASONS - {TriggerReason.CREDIT_MULTIPLE}

    def credit_stop(self, entry_credit: Decimal) -> float:
        """The cost-to-close level that trips the credit trigger."""
        return float(entry_credit) * self.credit_multiple

    def credit_breached(self, *, cost_to_close: float, credit_stop: float) -> bool:
        """Has the cost to close reached the stop by an amount a PRICE can express?

        The margin is required (2026-09-01). `cost_to_close` is `spread_mid` over
        four float quotes — two divisions and three additions — and `credit_stop`
        is `float(entry_credit) * credit_multiple`. Both are exact multiples of
        0.025 in the arithmetic and neither is exact in binary, so at the boundary
        the two land a half-ULP apart in whichever direction the summation
        happened to go. On 2026-09-01 `call-7670-7675` was stopped out at
        `cost_to_close = 1.6000000000000005` against `credit_stop = 1.6` — a
        breach of 4.4e-16, eleven orders of magnitude below the tick — and
        realized -160.00 on a spread that expired worthless.

        **`Decimal` does not fix this and the precision is not lost here.**
        `float(Decimal("0.8")) * 2.0` is exactly the double nearest 1.6; the noise
        is upstream, in the mid. `Decimal(str(1.6000000000000005)) >=
        Decimal("1.6")` fires just the same. Restoring exactness would mean
        quantizing `spread_mid` itself — shared with the ENTRY path, and the same
        number that prices the exit order — so the tolerance belongs on the one
        comparison that turns the mid into a verdict.

        This makes the trigger fire strictly LESS often, and the size of that is
        bounded: where `credit_stop` is not on the achievable 0.025 grid the
        boundary is unreachable and nothing changes at all; where it IS, the
        position runs on to the next achievable price, at most 0.025 points —
        $2.50 a contract, against $500 of defined risk on a 5-wide. What is given
        up is an answer that was decided by rounding rather than by the market.
        → docs/decisions/2026-09-02-the-credit-stop-basis-and-its-comparison.md
        """
        return cost_to_close - credit_stop >= PRICE_EPSILON

    def underlying_breached(self, *, side: str, spot: float, short_strike: float) -> bool:
        """Has the underlying reached the danger side of the short strike?

        No direction branch beyond the one that is genuinely irreducible: a put
        credit spread is short BELOW spot and is hurt by a fall, a call credit
        spread is short ABOVE spot and is hurt by a rise. `buffer` is how far in
        FRONT of the strike to act, so 0 means "when the strike is touched".

        **This one deliberately does NOT take `credit_breached`'s tolerance**, and
        the reason is structural rather than a judgement about magnitude. There,
        the threshold is derived from `entry_credit` by multiplication, so it lands
        on the same 0.025 grid the input does and `cost_to_close == credit_stop` is
        a RECURRING EXACT STATE. Here the two sides come from unrelated places — an
        integral SPX strike plus a configured buffer in points, against a mid of
        the index quote that prints at 7661.165 — so landing on the boundary is a
        measure-zero event, not a design feature, and the next tick resolves it
        either way. Spot exactly at the mark IS at the trigger level; firing there
        is the trigger working. A tolerance would only delay the backstop — the one
        trigger that still fires when the option market cannot be priced, and the
        one 2026-08-13 went to some trouble to keep live when the credit trigger
        goes blind. The asymmetry is a decision, and `test_watchdog.py` asserts it
        rather than leaving it to this comment.
        """
        if side == "put":
            return spot <= short_strike + self.underlying_buffer_points
        return spot >= short_strike - self.underlying_buffer_points


@dataclass(frozen=True)
class TriggerEvaluation:
    """One look at one position: what is true, and what could not be determined."""

    true_reasons: frozenset[TriggerReason] = frozenset()
    #: Reasons whose inputs were missing or stale. NOT false — undetermined.
    unknown_reasons: frozenset[TriggerReason] = frozenset()
    problems: tuple[str, ...] = ()
    cost_to_close: float | None = None
    credit_stop: float | None = None
    spot: float | None = None
    market: SpreadMarket | None = None
    #: Seconds until this day's flatten mark; negative once it has passed. Recorded
    #: on every tick so the runway is in the trail, and carried onto the unpriceable
    #: page — "triggered, cannot be priced" reads very differently with 25 minutes
    #: left than with 25 minutes to go. (2026-08-28)
    seconds_to_flatten: float | None = None
    #: The stops that could FIRE on this evaluation (`TriggerThresholds.acting_stops`),
    #: which is what `blind` is measured against (2026-09-27 review). REQUIRED and
    #: keyword-only, on `seconds_to_flatten=`'s precedent: both directions of a
    #: default are silent — the full set re-creates the defect under the strike-touch
    #: stop, and a narrow one pages on every stale wing quote.
    acting_stops: frozenset[TriggerReason] = field(kw_only=True)

    @property
    def fully_evaluable(self) -> bool:
        return not self.unknown_reasons

    @property
    def blind(self) -> bool:
        """NO STOP THAT CAN FIRE could be evaluated — nothing is watching the position.

        The distinction that makes the page mean something (2026-08-13). Losing the
        credit trigger to a stale wing quote is DEGRADED: the underlying backstop is
        still live, still debouncing, still able to fire — and after the identity/price
        split above it is the common case, ~14% of ticks late in a session. Losing
        both is the one that warrants waking someone, and it now takes either a
        symbol missing from the chain or a dead SPX feed.

        **Measured against `acting_stops`, the stops that can FIRE under this config**
        (2026-09-27 review). With the credit trigger switched off (the strike-touch
        stop) the underlying backstop is the only one, so a dead SPX feed ALONE is
        blind — the credit trigger's mark is still evaluable, but it cannot close
        anything, and counting it as watching left that state unpaged. With the
        credit trigger on this is the pre-2026-09-26 test exactly. The flatten is
        never in the set: it can never be unevaluable (see `_MARKET_TRIGGER_REASONS`),
        and a clock at 15:30 is not a substitute for a stop at 11:00.
        """
        return self.unknown_reasons >= self.acting_stops

    @property
    def crossing_debit(self) -> float | None:
        """What it would cost to cross RIGHT NOW — the offer, not the mark. (2026-09-17)

        `cost_to_close` beside it is the zero-bid-floored MID; this is the short leg's
        ask minus the long leg's bid, i.e. the price a market-crossing limit actually
        fills at. The gap between the two is the only thing that says whether the exit
        ladder is stopping short of a resting offer, which is the number the
        `exit_walk_away_pct` pass needs and could not get: `watchdog.crossed` carries
        it only when a cross actually fired (n=3 across 780,853 rows), and it is
        right-censored there by `min(debit, width)`.

        **Nothing decides on it.** It is recorded so the floor can be re-derived from
        live data later, and reading it as a fillable price would repeat 2026-08-31's
        mistake in the other direction — `cost_to_close` is a mark, and this is a
        snapshot of one side of a book that may be one tick wide and may be empty.

        `None` rather than NaN when the legs cannot be priced — and the finiteness half
        of that goes through `payload_number`, never an inline `is_finite` ternary.
        This property was written for v0.4.3 with its own ternary, a fourth spelling of
        the rule the quoted-spread change had already collapsed to one; the structural
        test in `tests/test_payload_safety.py` only recognises a SINGLE-return wrapper,
        so a multi-statement spelling like the original would have passed it. Fixed at
        the merge rather than left for the test to miss.
        → docs/decisions/2026-09-17-exit-ladder-second-pass.md,
        docs/designs/2026-09-15-recording-the-quoted-spread.md
        """
        return None if self.market is None else payload_number(self.market.crossing_debit)

    def to_payload(self) -> dict[str, Any]:
        """The evaluation as every record of it is written.

        ONE method, FOUR consumers — `watchdog.check`, `watchdog.check_spent`,
        `_report_evaluability`'s pages and `_trigger`'s — all of which splat it. A
        field added here reaches all four, which is the shape that cannot reproduce
        the 2026-09-14 failure, where a reader named one category and the runtime
        had since added another.

        **Every numeric goes through `payload_number`, including the three that were
        here before.** A non-finite float is a REFUSED record — Tier 1 raises and
        Tier 2 drops the whole row — so the guard is a property of the payload
        boundary, not of whichever adapter happens to be installed. `cost_to_close`
        reaching here unguarded was a live latent defect of exactly this shape: the
        `is_finite` check in `evaluate_triggers` marks `CREDIT_MULTIPLE` unknown and
        adds a `problems` entry, and then this method recorded the raw value anyway —
        so in the one branch where the number is known to be unusable, the record
        that would explain the refusal is the record that got dropped.
        """
        return {
            "true_reasons": sorted(str(r) for r in self.true_reasons),
            "unknown_reasons": sorted(str(r) for r in self.unknown_reasons),
            "problems": list(self.problems),
            "cost_to_close": payload_number(self.cost_to_close),
            # WHAT IT WOULD COST TO TRADE, beside what it is marked at (2026-09-15).
            # `crossing_debit - cost_to_close` is algebraically exactly the sum of
            # the two half-spreads — the friction layer this system has never
            # recorded — so one spread-level number recovers the whole quantity and
            # four raw leg prices would be redundant at the highest-cadence Tier 2
            # category in the system. The entry side goes the other way (raw per
            # leg) because there the leg attribution is a SELECTION question; by exit
            # time the strikes are fixed and there is no decision left.
            #
            # The identity is exact only where both legs are two-sided: with a zero
            # long bid the floor adds 0.025, because `cost_to_close` floors and
            # `crossing_debit` deliberately does not. Analysis conditions on that,
            # which is what the entry side's raw bids are for.
            #
            # `None` when the legs could not be priced, never 0.0 — the same rule
            # `cost_to_close` holds, and the reason `/status` renders `unpriced`
            # rather than a number. NOTHING in `src/` reads this; it is an
            # instrument. → docs/designs/2026-09-15-recording-the-quoted-spread.md
            #
            # Read through the property rather than re-derived here: v0.4.3 and the
            # quoted-spread change each added this key independently, and one
            # expression in one place is the merge's answer to that.
            "crossing_debit": self.crossing_debit,
            "credit_stop": payload_number(self.credit_stop),
            "spot": payload_number(self.spot),
            "seconds_to_flatten": payload_number(self.seconds_to_flatten),
        }


def evaluate_triggers(
    spread: OpenSpread,
    quotes: LegQuoteSource,
    thresholds: TriggerThresholds,
    *,
    seconds_to_flatten: float,
) -> TriggerEvaluation:
    """Evaluate every trigger for one held spread. Pure apart from reading quotes.

    They are evaluated INDEPENDENTLY, so an options-feed gap that makes the credit
    trigger unevaluable does not also blind the underlying trigger — which is the
    one that still works when the spread cannot be priced, and therefore the one
    that matters most in exactly that scenario.

    **`seconds_to_flatten` is required and keyword-only** (2026-08-28), on the
    `reconcile_orders(unmappable=)` / `expiry_verdicts(session_settled=)` /
    `open_spreads(today=)` precedent, and for the same reason: both directions of a
    default are silent. A large default retires the flatten trigger; a small one
    flattens the book on the first tick.

    It is a pre-REDUCED distance rather than a clock or a calendar, which is what
    keeps this function pure. The flatten's threshold is not a number comparable to
    a market quantity — it is an instant on the exchange calendar — and the calendar
    belongs to the watchdog, which owns exactly one reading of it per pass.
    """
    problems: list[str] = []
    true_reasons: set[TriggerReason] = set()
    unknown: set[TriggerReason] = set()

    short = quotes.resolve(spread.short_symbol)
    long_ = quotes.resolve(spread.long_symbol)
    for resolution in (short, long_):
        if resolution.problem:
            problems.append(resolution.problem)

    market: SpreadMarket | None = None
    cost_to_close: float | None = None
    credit_stop = thresholds.credit_stop(spread.entry_credit)
    # The profit target shares this branch with the credit stop because it shares the
    # input, so the two are always evaluable or unknown TOGETHER — which is what keeps
    # `blind`'s superset test meaning what it meant before a fourth trigger existed.
    # Evaluated whether or not `profit_target_enabled` is set: gating the EVALUATION
    # on the switch would leave it out of `unknown` while off, and `blind` would then
    # be false for every input. The switch gates the ACTION, in the watchdog.
    if short.leg is not None and long_.leg is not None:
        market = SpreadMarket(short=short.leg, long=long_.leg)
        cost_to_close = market.cost_to_close
        if not is_finite(cost_to_close):
            problems.append(f"cost to close is not a usable number: {cost_to_close!r}")
            unknown.add(TriggerReason.CREDIT_MULTIPLE)
            unknown.add(TriggerReason.PROFIT_TARGET)
        else:
            # The switch gates the FIRING only (2026-09-26). `unknown` above is still
            # populated when the mark is unusable, because that is a fact about the
            # input the profit target shares, not about whether this trigger acts.
            if thresholds.credit_trigger_enabled and thresholds.credit_breached(
                cost_to_close=cost_to_close, credit_stop=credit_stop
            ):
                true_reasons.add(TriggerReason.CREDIT_MULTIPLE)
            if at_profit_target(cost_to_close):
                true_reasons.add(TriggerReason.PROFIT_TARGET)
    else:
        unknown.add(TriggerReason.CREDIT_MULTIPLE)
        unknown.add(TriggerReason.PROFIT_TARGET)

    spot = quotes.spot()
    # The SIDE comes from the resolved short contract's `right`, not from parsing a
    # symbol or a candidate id: the contract is already in hand for the quote lookup
    # and carries the authoritative strike and right together.
    #
    # `short.contract`, NOT `short.leg.contract`. The strike and the right come from
    # the chain index and do not depend on quote freshness, so a stale short leg must
    # cost the CREDIT trigger above and nothing else — this branch is now reachable
    # only when the symbol is genuinely absent from the subscribed chain. Reading it
    # off `leg` is the 2026-08-13 defect: a 2.9s-old wing quote blinded the backstop
    # that exists precisely for when the option market cannot be priced.
    short_contract = short.contract
    if spot is None:
        problems.append("no fresh underlying quote")
        unknown.add(TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE)
    elif short_contract is None:
        problems.append(
            "short strike unknown (symbol not in the subscribed chain), cannot check "
            "the underlying"
        )
        unknown.add(TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE)
    elif thresholds.underlying_breached(
        side=short_contract.right, spot=spot, short_strike=short_contract.strike
    ):
        true_reasons.add(TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE)

    # The clock. NEVER added to `unknown` under any input — the whole point of this
    # trigger is that it has no input to be missing, which is also why `blind` is
    # measured against `_MARKET_TRIGGER_REASONS` rather than the full enum.
    # Inclusive at the mark, the house convention for every other bound here.
    if seconds_to_flatten <= 0.0:
        true_reasons.add(TriggerReason.FLATTEN_BEFORE_CLOSE)

    return TriggerEvaluation(
        true_reasons=frozenset(true_reasons),
        unknown_reasons=frozenset(unknown),
        problems=tuple(problems),
        cost_to_close=cost_to_close,
        credit_stop=credit_stop,
        spot=spot,
        market=market,
        seconds_to_flatten=seconds_to_flatten,
        acting_stops=thresholds.acting_stops,
    )


# -- the debounce -------------------------------------------------------------


class SustainTracker:
    """How long each trigger condition has been CONTINUOUSLY true, per position.

    Spec §7's exit variant: a threshold must be "sustained for several consecutive
    seconds to filter out a momentary spread blowout". A 0DTE vertical's mid can
    print through 2x credit on one wide quote and come straight back; acting on that
    single observation converts a quote artifact into a realized loss.

    Three rules, and all three are about NOT accumulating credit that was not
    earned:

    - A condition going false **deletes** its window. The next episode starts from
      zero — partial time from a previous episode is never carried forward.
    - Each reason has its OWN window. "Whichever fires first" means a 2s run of the
      credit condition followed by a fresh underlying breach does not hand the
      underlying trigger a 2s head start.
    - An UNDETERMINED condition (stale quote) neither starts, clears, nor advances
      a window beyond what the clock does anyway. There is no evidence it cleared,
      so it is left alone — which means a blind gap counts toward the sustain. That
      is the fail-closed direction for a watchdog: exiting is the protective act.

    **`_INSTANT_TRIGGER_REASONS` are exempt from the gate, not from the tracking**
    (2026-08-28). Every rule above is about not accumulating credit that was not
    earned by a MARKET condition holding; a clock reading has nothing to accumulate.
    See that constant for why the exemption is explicit rather than left implicit in
    a monotonic condition satisfying the sustain anyway.
    """

    def __init__(self, sustained_s: float, clock: ClockFn) -> None:
        self._sustained_s = sustained_s
        self._clock = clock
        self._since: dict[tuple[str, TriggerReason], float] = {}

    def observe(
        self,
        key: str,
        *,
        true_reasons: frozenset[TriggerReason],
        unknown_reasons: frozenset[TriggerReason] = frozenset(),
    ) -> frozenset[TriggerReason]:
        """Record one observation; return the reasons now sustained long enough."""
        now = self._clock()
        fired: set[TriggerReason] = set()
        for reason in TriggerReason:
            slot = (key, reason)
            if reason in true_reasons:
                # The window is opened for EVERY reason, including the instant ones:
                # `held_s` then measures how long the flatten has been due with the
                # position still held, which is what the repeating page and the trail
                # want. Recording the window and GATING on it are separate concerns,
                # and only the second was ever argued for. (2026-08-28)
                started = self._since.setdefault(slot, now)
                if reason in _INSTANT_TRIGGER_REASONS or now - started >= self._sustained_s:
                    fired.add(reason)
            elif reason in unknown_reasons:
                continue  # undetermined: leave the window exactly as it was
            else:
                self._since.pop(slot, None)
        return frozenset(fired)

    def held_s(self, key: str, reason: TriggerReason) -> float:
        started = self._since.get((key, reason))
        return 0.0 if started is None else max(0.0, self._clock() - started)

    def forget(self, key: str) -> None:
        """Drop all windows for a position that is no longer held."""
        for slot in [s for s in self._since if s[0] == key]:
            del self._since[slot]

    @property
    def tracking(self) -> int:
        return len(self._since)


# -- the exit handoff ---------------------------------------------------------


class ExitOutcome(StrEnum):
    """How an exit attempt ended. More than success/failure, for the same reason
    `WalkOutcome` is: the operator response differs sharply between these."""

    #: The walk filled it. Nothing further needed.
    FILLED = "filled"
    #: The walk gave up and the market-crossing order was submitted.
    CROSSED = "crossed"
    #: The walk gave up, but the abandoned order turned out to have filled after
    #: all (a cancel and a fill crossed). No second order was sent.
    FILLED_LATE = "filled_late"
    #: The walk gave up and the cross was REFUSED — the abandoned exit order was
    #: still working, so a second closing order would have risked closing twice.
    CROSS_REFUSED = "cross_refused"
    #: The cross could not be priced (no offer on the short leg / non-finite price).
    #: Retryable, under the SAME budget as `NOTHING_AT_BROKER` (2026-08-31).
    #:
    #: It shares a code path with `CROSS_REFUSED` and is its opposite. This outcome is
    #: unreachable except THROUGH `_await_settled` returning `outcome=None` — the one
    #: positive confirmation in this module, where the broker named
    #: `walk.broker_order_id` and every disposition it gave for that order was
    #: terminal. `CROSS_REFUSED` is exactly the absence of that confirmation and
    #: returns before this point, so the two can never be reached under one another's
    #: conditions. Nothing here weakens it.
    #:
    #: What failed is only the PRICE: a feed that had no offer on the short leg a
    #: moment ago and may have one now. On 2026-08-31 that spent a 0DTE spread's
    #: whole exit — the ordinary attempt and the flatten's one-shot override — in 1.3
    #: seconds, on a transient state. Retrying is safe for the same reason
    #: `NOTHING_AT_BROKER`'s is and then some: the licence is `DuplicateOrderGuard`
    #: re-asking the broker on the next tick, which `_cross` itself does not get.
    #: → docs/decisions/2026-08-31-exit-ladder-near-tick.md
    CROSS_UNPRICEABLE = "cross_unpriceable"
    #: The walk ended in a state that is not self-resolving and must not be crossed:
    #: rejected, never worked, cancelled elsewhere, unresolved, blocked, aborted.
    NOT_RESOLVED = "not_resolved"
    #: No order could be built at all (no usable quotes). Retryable — this does NOT
    #: consume the position's one exit attempt.
    NOT_ATTEMPTED = "not_attempted"
    #: An order was sent, the send failed, and the broker — asked what exists under
    #: this identifier — answered that nothing does. Retryable, under a BUDGET.
    #:
    #: Deliberately not folded into `NOT_ATTEMPTED` (2026-08-28), which would have
    #: been the smaller edit. The two differ in the one way that matters for a
    #: retry: `NOT_ATTEMPTED` never touched the broker, so retrying it costs a
    #: quote read and may repeat forever; this one costs a submission every time,
    #: and a broker failing every submit while answering every read would turn an
    #: unbounded retry into exactly the runaway `ratelimit.py` exists to cap. So it
    #: retries `_EXIT_SEND_ATTEMPTS` times and then becomes spent like any other.
    #:
    #: Safe to retry at all only because the exit walk runs with
    #: `guard_first_submission=True`: the retry re-asks the broker through
    #: `DuplicateOrderGuard` and comes back `BLOCKED` if anything did land. The
    #: licence is the guard, never this outcome's own answer.
    NOTHING_AT_BROKER = "nothing_at_broker"
    #: A PROFIT TAKE went unfilled and its order is POSITIVELY CONFIRMED terminal with
    #: nothing filled. Retryable, under the profit target's own budget. (2026-09-23)
    #:
    #: Reachable ONLY on the profit path, where `_after_walk` settles instead of
    #: crossing: the walk found no fill at one tick or two, it cancelled, and
    #: `_await_settled` — the one positive confirmation in this module — saw the broker
    #: NAME the walk's order terminal with no fill under the identifier. That is the
    #: state `_cross` would have crossed in; a profit take has no deadline that
    #: justifies crossing, so it reports instead and the next look may try again.
    #:
    #: Its own member rather than a reuse, because none of the existing ones says it:
    #: `NOTHING_AT_BROKER` is a SEND that failed, `CROSS_UNPRICEABLE` a cross that
    #: could not be priced, `NOT_RESOLVED` a state that is not self-resolving. This one
    #: is the ordinary, benign "the auction did not pay a tick" — and it must never be
    #: the reason an emergency attempt is spent, which the watchdog guarantees by
    #: letting a profit take's report near `_attempted` only when the take FILLED.
    UNFILLED = "unfilled"

    @property
    def closed(self) -> bool:
        """True only where a closing order actually filled."""
        return self in (ExitOutcome.FILLED, ExitOutcome.FILLED_LATE)

    @property
    def needs_attention(self) -> bool:
        """True where a human has to look. Everything except a clean fill."""
        return self is not ExitOutcome.FILLED


@dataclass(frozen=True)
class ExitReport:
    """What the exit attempt did. Like `WalkResult`, a REPORT and not a guarantee.

    A cancel request and a fill can cross at the broker, so nothing here proves
    what is or is not held. `positions` (chunk 2, written from the `AlertStreamer`
    fill feed) and restart-time reconciliation remain authoritative.
    """

    outcome: ExitOutcome
    external_identifier: str | None
    reasons: tuple[TriggerReason, ...]
    walk: WalkResult | None = None
    cross_limit_price: Decimal | None = None
    cross_broker_order_id: int | None = None
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "external_identifier": self.external_identifier,
            "trigger_reasons": [str(r) for r in self.reasons],
            "walk": self.walk.to_payload() if self.walk is not None else None,
            "cross_limit_price": (
                float(self.cross_limit_price) if self.cross_limit_price is not None else None
            ),
            "cross_broker_order_id": self.cross_broker_order_id,
            "reason": self.reason,
        }


def _alert_reject_reason(walk: WalkResult | None) -> str | None:
    """The broker's verbatim refusal for a per-position ALERT, or None. (2026-09-02)

    **What a per-position alert owes, decided once: an alert that names the outcome of
    an exit attempt carries the broker's reason for that outcome.** Everything else on
    such a page is a category — `watchdog_exit_not_resolved`, `"walk ended rejected"` —
    and a category with no reason is what an operator cannot act on.

    P6 (2026-08-29) added `WalkResult.reject_reason` for exactly this, put it on the
    entry path (which alerts `**result.to_payload()`, so it arrived for free) and
    silently missed the exit path, whose alert dicts are hand-built. The miss surfaced
    the first time an exit was ever rejected. Two call sites read it — `_record_exit`
    for the one-shot page, and `_run_exit` to put it on `_SpentAttempt`, which is what
    the two REPEATING pages (`_page_if_spent_and_still_held`, `_flatten_may_act`) then
    report — so a fourth surface has somewhere obvious to look. And
    `tests/test_watchdog_exit.py` pins the rule structurally rather than trusting the
    convention that already failed once: that sweep is what found `_flatten_may_act`,
    which the design pass had missed.

    `None` for a report with no walk, and `None` rather than `""` when there is nothing
    to say — the discipline `WalkResult.reject_reason` itself keeps.
    """
    return walk.reject_reason if walk is not None else None


def _alert_mutation_error(walk: WalkResult | None) -> str | None:
    """The broker's account of a REFUSED submit, for a per-position alert. (2026-09-04)

    The sibling of `_alert_reject_reason` and the second half of the rule it states.
    The broker refuses an order in two different ways and only one of them ever
    reached a page: `reject_reason` is what it said about an order it ACCEPTED and
    then rejected; this is what it said about one it would not accept at all.

    The second reported as an ABSENCE — `no_order_exists`, `reject_reason: null`, a
    fixed template `reason` — on 7 of 8 failed entry attempts (2026-09-02) and **5 of
    7 (71%), the day's dominant failure mode** (2026-09-03), by which point it had
    also cost the exit path a send from a budget of three. A permanent
    `instrument_validation_failed` naming the exact unsupported symbol and a
    transient `timeout_error` were indistinguishable on the page.

    Two functions rather than one field doing double duty, and two keys rather than
    one: `reject_reason: "timeout_error: Request Timed Out"` would read as *the
    broker rejected this order*, which is the opposite of what happened. `None`
    rather than `""`, the discipline `WalkResult` itself keeps.
    """
    return walk.mutation_error if walk is not None else None


@dataclass(frozen=True)
class _Settlement:
    """What the bounded settle window concluded about the abandoned exit order.

    A named result rather than a bare `ExitOutcome | None` because CROSS_REFUSED has
    FOUR causes and they need different words in the page (see `_SETTLE_REFUSALS`):
    the order still WORKING, the order missing from the broker's answer, a broker
    that could not be read at all, and a walk that never named the order. Composing
    that sentence at the call site would have to guess which one happened — and did,
    calling all of them "still working".
    """

    #: FILLED_LATE, CROSS_REFUSED, or None when the order is confirmed properly dead
    #: and the cross may proceed.
    outcome: ExitOutcome | None
    #: Why, in the words the operator gets. Empty only for the None case, which is
    #: the one outcome that produces no alert.
    reason: str = ""
    #: On a refusal, WHICH of the four `_SETTLE_*` states the broker was last in
    #: (2026-09-23). The profit path has no cross to refuse, so `reason`'s wording —
    #: written for the cross — is the wrong sentence there; this lets it compose its
    #: own from the same fact rather than re-deriving the fact. None on the other two.
    sighting: str | None = None


@dataclass(frozen=True)
class _CrossPrice:
    """The market-crossing limit and what bound it. (2026-09-25)

    `basis` names the rule that set the price — `natural`, `past_ladder`,
    `natural_no_long_bid` or `width` — and is recorded with `natural` (the RAW,
    uncapped `crossing_debit`), `short_ask` and `long_bid` on `watchdog.crossed` and
    its page. **Nothing decides on any of those four**; `limit` is the only field
    the order is built from.
    """

    #: Signed debit (negative), tick-aligned, floor-quantized to the width.
    limit: Decimal
    basis: str
    natural: float
    short_ask: float
    long_bid: float


#: The last thing the broker said about the abandoned order before the deadline.
#: None of the four is a confirmation, so all four refuse — but the operator's
#: next move differs, so the page must not describe one as another.
_SETTLE_WORKING = "working"
_SETTLE_ABSENT = "absent"
_SETTLE_UNREADABLE = "unreadable"
_SETTLE_NO_ID = "unidentified"

_SETTLE_REFUSALS = {
    _SETTLE_WORKING: (
        "abandoned exit order was still working after {bound}; refusing to stack a "
        "second closing order on one that can still fill"
    ),
    _SETTLE_ABSENT: (
        "abandoned exit order was absent from the broker's answer for {bound} and was "
        "never confirmed terminal — even where the answer named its earlier, replaced "
        "rungs; an order nobody mentioned is a read gap, not a dead order, and "
        "crossing against it risks closing the spread twice"
    ),
    _SETTLE_UNREADABLE: (
        "abandoned exit order could not be read in {bound}; refusing to cross against "
        "a state nobody read"
    ),
    _SETTLE_NO_ID: (
        "the walk reported no broker order id for the abandoned exit order, so there "
        "is no specific order to confirm terminal; refusing to cross against an order "
        "Argus cannot name"
    ),
}


def _refusal_prose(exc: Exception) -> str:
    """Why no exit order was built, in a sentence an operator can act on. (2026-09-02)

    All three still produce ONE `ExitOutcome.NOT_ATTEMPTED` — see `close()` for why the
    outcome must not split. This is the page, not the verdict.

    The type prefix is KEPT on every branch: `error_type`-shaped readers and existing
    log filters key on it, and the added prose sits after it rather than replacing it.
    """
    if isinstance(exc, UnpriceableSpreadError):
        # The routine one, and the reason this function exists. Not a fault: the market
        # is saying this spread is worth approximately nothing, which near the close is
        # what a WINNING position looks like. The escalation for "the auction will not
        # pay" is the width-capped crossing limit, and it is reachable only through a
        # built order — so the answer is to look again next cycle, never to invent a
        # price. → docs/decisions/2026-08-28-time-based-flatten-trigger.md
        return (
            f"{type(exc).__name__}: the spread's net mid rounds to "
            f"{exc.magnitude} at the tick, so there is no limit to send and neither "
            "the walk nor the flatten will invent one; nothing was sent, and the next "
            "cycle looks again"
        )
    if isinstance(exc, StaleSignalError):
        # Self-resolving, usually within a tick or two: the quotes aged between the
        # trigger's own evaluation and construction. Same bound on both sides
        # (`staleness.market_data_max_age_s`), so this is latency, not a threshold
        # disagreement — and it must NOT be fixed by raising that key, which also
        # governs entry pricing and the gate's staleness rail.
        return (
            f"{type(exc).__name__}: quotes aged {exc.age_s:.3f}s past the "
            f"{exc.max_age_s:g}s bound between the trigger and the build; nothing was "
            "sent, and the next cycle looks again"
        )
    # Everything else `build_order` raises is a DEFECT SIGNATURE, not a market
    # condition: a credit that does not match its own legs, a non-positive contract
    # count, an unknown direction. Deliberately worded so it does not read like the two
    # above — this one repeats every cadence tick and never clears on its own, and the
    # `_UNATTEMPTED` escalation's `overdue` repeat is what surfaces that.
    return f"{type(exc).__name__}: refused to build the exit order: {exc}"


#: Walk outcomes after which a PROFIT TAKE confirms its order settled rather than
#: crossing (2026-09-23). Each ends with nothing filled and the walk's own order either
#: cancelled by the walk or terminal at the broker — so the question left is the one
#: `_await_settled` answers positively or not at all: is THAT order dead?
#:
#: This is design C of the 2026-09-22 record (§11.5) and the thing it said must be
#: prototyped first: `NEVER_WORKED` — a resting limit that never went `Live` inside
#: `working_timeout_s` — is on the emergency path an ABSOLUTE spend (`NOT_RESOLVED`),
#: the very outcome C exists to avoid arriving through the front door. Here it is
#: settled like a walk-away, and becomes retryable only on a positive confirmation.
_PROFIT_SETTLE = frozenset({
    WalkOutcome.WALKED_AWAY,
    WalkOutcome.NEVER_WORKED,
    WalkOutcome.DRIFTED,
    WalkOutcome.REJECTED,
    WalkOutcome.CANCELLED_ELSEWHERE,
})
#: Walk outcomes that retire a profit take with NO settle, each for its own reason:
#: `UNRESOLVED` — nobody read the broker, and its "two working orders under one key"
#: shape cannot be confirmed through one order id; `BLOCKED` — a close already exists
#: under this identifier, which is the emergency exit's business now; `ABORTED` — the
#: session is stopping, and a halt must not add a broker round trip.
#:
#: `FILLED` and `NO_ORDER_EXISTS` are the two branches the profit take SHARES with the
#: emergency exit. The four sets partition `WalkOutcome`, which
#: `test_every_walk_outcome_has_one_profit_take_disposition` holds, so a new walk
#: outcome cannot reach the profit path unclassified.
_PROFIT_RETIRE = frozenset({
    WalkOutcome.UNRESOLVED,
    WalkOutcome.BLOCKED,
    WalkOutcome.ABORTED,
})

#: What the broker was last seen doing with a profit take's order that could not be
#: confirmed terminal — `_SETTLE_REFUSALS`' four states, in words that do not talk
#: about a cross the profit path never makes.
_PROFIT_UNSETTLED = {
    _SETTLE_WORKING: "still working",
    _SETTLE_ABSENT: "absent from the broker's answer",
    _SETTLE_UNREADABLE: "unreadable",
    _SETTLE_NO_ID: "never named by the walk",
}


def _take_may_be_working(report: ExitReport) -> bool:
    """Could a CLOSED profit take still have an order working at the broker?

    True exactly where the walk ended with a cancel only REQUESTED (a partial fill's
    remainder, or a rung abandoned before the identifier-wide probe found an earlier
    rung filled) or on a non-terminal last sighting — `_request_cancel` is best-effort
    and confirms nothing. `FILLED_LATE` is False: `_await_settled` saw the order
    terminal before reporting it. Feeds `/status` only; nothing decides on it.
    """
    if report.outcome is not ExitOutcome.FILLED:
        return False
    walk = report.walk
    if walk is None:
        return False
    return walk.cancel_requested or (
        walk.last_status is not None and not walk.last_status.is_terminal
    )


class ExitExecutor:
    """Turns a triggered spread into a closed one. Owns the WALKED_AWAY escalation.

    Construction, walking, and recording are all delegated; what is genuinely this
    class's own is the decision tree after the walk returns, and the market-crossing
    price. Both are exit policy, which is why they are not in chunk 3.
    """

    def __init__(
        self,
        *,
        execution: AuditedOrderExecution,
        audit: AuditLog,
        config: Config,
        guard: DuplicateOrderGuard,
        quotes: LegQuoteSource | None = None,
        view: OrderView | None = None,
        alert: AlertFn | None = None,
        stop: asyncio.Event | None = None,
        sleep: SleepFn | None = None,
        clock: ClockFn | None = None,
        trading_date: TradingDateFn | None = None,
        loop_factory: Callable[..., PriceWalkingLoop] | None = None,
        residuals: ResidualOrderRecheck | None = None,
    ) -> None:
        self._execution = execution
        self._audit = audit
        self._config = config
        self._guard = guard
        self._policy = WalkPolicy.for_exit(config)
        # The profit take's ladder (2026-09-23): the exit's, with one one-tick rung —
        # one tick, then two, and no further. Chosen per call in `close`, off the REASONS,
        # so the one walk call site and the one guarded submission stay one.
        self._profit_policy = WalkPolicy.for_profit_target(config)
        # Optional here and settable via `bind_quotes` so the watchdog can construct
        # the executor alongside itself and share the ONE resolver, rather than the
        # two building separate views of the same market.
        self._quotes = quotes
        # Optional and bound the same way (`bind_scan_age`, by the watchdog beside
        # `bind_quotes`): `watchdog.exit_started` carries the scan's age, and the
        # reading lives on the watchdog — this is a seam, not a copy, so the record
        # and `/status` cannot disagree about which scan was the last good one.
        self._scan_age: Callable[[], float | None] | None = None
        self._alert = alert or log_alert
        self._stop = stop
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        self._trading_date = trading_date or et_today
        self._view = view if view is not None else LiveOrderView(execution)
        # An exit walk registers its residuals too, and its residuals are CLOSING
        # orders — which the recheck pages about and never cancels. Passing it here
        # is what makes a stuck close visible; it can never make one cancelled.
        self._residuals = residuals
        self._loop_factory = loop_factory or self._build_loop

    def _build_loop(self, *, policy: WalkPolicy) -> PriceWalkingLoop:
        # `policy` is the exit's or the profit take's, chosen by `close`; everything
        # else about the loop — the guard, the view, the residual registry, the
        # guard's bound, the absent halt check — is identical for both.
        #
        # NO `halt_check`, deliberately, and this omission is a safety decision
        # rather than an oversight (a test pins it). The kill switch abandons ENTRY
        # walks — `entry.py` arms it — because a halt must stop the system taking on
        # new risk. An exit walk is the opposite case: cancelling it would strand an
        # open 0DTE position that the watchdog has already decided must be closed,
        # and the operator who hit the button would be left holding exactly the risk
        # they were trying to shed. `stop` is still passed, so a session close or
        # SIGTERM ends an exit walk; a kill switch trip does not.
        return PriceWalkingLoop(
            execution=self._execution,
            audit=self._audit,
            policy=policy,
            guard=self._guard,
            view=self._view,
            alert=self._alert,
            stop=self._stop,
            sleep=self._sleep,
            clock=self._clock,
            residuals=self._residuals,
            # The bound on ONE duplicate-guard check, and it is the SAME config key
            # the entry path's burst uses (`[execution] guard_timeout_s`), because it
            # bounds the same call to the same endpoint through the same port method.
            # Passing it is what turns the guard's raise from a process halt into a
            # bounded burst and a `NOT_ATTEMPTED` — see `close`. No new config key,
            # so this fix deploys as an image alone.
            guard_timeout_s=self._config.execution.guard_timeout_s,
        )

    async def close(
        self,
        spread: OpenSpread,
        market: SpreadMarket,
        reasons: frozenset[TriggerReason],
        *,
        stub: bool = False,
    ) -> ExitReport:
        """Close `spread`. A market condition is an OUTCOME, not an exception.

        `stub` marks the watchdog's stub-close continuation (2026-09-24). It
        changes ONE thing, and only in `_record_exit`: a walk the guard refused
        (`BLOCKED`) is recorded at Tier 1 as always but NOT paged, because on this
        path the watchdog restores and re-asks a refusal that sent nothing, and
        pages once itself if the refusals run out. The order, the ladder, the guard
        and every other outcome's record and page are unchanged.

        Everything that can be said about the market or the order — stale quotes, an
        unpriceable spread, a rejection, a walk that gave up — comes back as an
        `ExitReport`. Three things still propagate, and all three end in the
        watchdog's process halt:

        - `AuditWriteError`: the recorder is dead, so nothing may be sent anyway.
        - A programming error.

        **The duplicate guard failing to reach the broker used to be a third, and it
        was wrong** (2026-09-03). The posture recorded here was that an unreachable
        broker at the moment an exit is needed should stop the process and page a
        human. It did, on 2026-09-03 at 11:04:05 ET, on ONE cert 502 — and what the
        halt actually took was the watchdog itself: `_halt_on_exception` sets the
        SHARED stop event, so the triggered 0DTE spread it was trying to close lost
        its virtual stop, its mark, its `/status` row and its 15:30
        `FLATTEN_BEFORE_CLOSE` for the rest of expiry day. The position was perfectly
        evaluable at that instant; it had been priced the same second.

        The entry path reached the opposite conclusion on 2026-08-26 and wrote down
        why, naming this exact configuration: *"Delegating it to the loop with
        `guard_first_submission=True` was the first shape of this and it was WRONG …
        a guard failure propagates out of `run()` … which would land it inside the
        hard-halt region and take the whole process down for a question that was
        asked before anything was sent."* That reasoning was scoped to the entry path
        and never carried one module over. It is carried now: the guard is asked
        under a bounded burst (`loop._ask_guard`) and a spent burst arrives here as
        `GuardUnreachable`, which becomes `NOT_ATTEMPTED` — nothing was sent, both
        allowances are kept, the trigger stays latched, and the next tick asks again.
        → docs/decisions/2026-09-03-exit-guard-unreachable.md

        **A profit take is the same call with a different ladder and no cross**
        (2026-09-23). Same builder, same guarded walk, same record — the profit
        target changes HOW MUCH it will pay and what happens when the auction will not
        pay it, never how an order reaches the broker. Which one this is comes from
        `reasons` alone (`_is_profit_take`), the same answer the watchdog uses to
        decide which allowance the report may touch.
        """
        ordered = tuple(sorted(reasons, key=str))
        profit = _is_profit_take(reasons)
        policy = self._profit_policy if profit else self._policy
        try:
            order = self._build_exit_order(spread, market)
        except (StaleSignalError, ValueError) as exc:
            # Retryable, and NOT recorded or alerted here: nothing was sent, so the
            # position keeps BOTH its one attempt and (since 2026-09-02) its flatten
            # override, and the watchdog owns the escalation — edge-triggered, since
            # this path can repeat every cadence tick.
            #
            # ONE outcome for all three causes, deliberately. The allowance downstream
            # asks exactly one question — *was anything sent* — and the answer is "no"
            # for every branch below; splitting the outcome would put a second rule
            # back into the thing `_run_exit`'s restore exists to reduce to one.
            #
            # **Three different sentences, though** (2026-09-02), because they are three
            # different next moves for whoever reads the page, and the caller must not
            # have to tell them apart by matching our prose. `UnpriceableSpreadError` is
            # a `ValueError` subclass raised only by `_limit_price`, so this branches on
            # TYPE. Before it existed, the flatten's MODAL refusal paged as a bare
            # `ValueError: net mid 0.0 rounds to 0.00 ...` — accurate, and it reads as a
            # crash rather than as the designed refusal it is.
            return ExitReport(
                outcome=ExitOutcome.NOT_ATTEMPTED,
                external_identifier=None,
                reasons=ordered,
                reason=_refusal_prose(exc),
            )

        if spread.candidate_id and order.candidate_id != spread.candidate_id:
            # The exit's identity comes from the strikes and side resolved off the
            # chain; the entry's came from the signal. They must agree, or the legs
            # being priced are not the legs being held.
            return ExitReport(
                outcome=ExitOutcome.NOT_ATTEMPTED,
                external_identifier=order.external_identifier,
                reasons=ordered,
                reason=(
                    f"exit candidate {order.candidate_id!r} does not match the held "
                    f"position's {spread.candidate_id!r}"
                ),
            )

        if profit and order.limit_price != -PROFIT_TARGET_MARK:
            # The profit take's MAXIMUM DEBIT rests on this line as much as on its
            # policy: `for_profit_target` floors one tick below the OPENING limit, so
            # the bound is two ticks only because the opening limit is one. The trigger
            # guarantees it — the order is built from the very `market` the trigger
            # evaluated, through the same rounding — and this makes it a property of
            # the call rather than of every caller. Above the walk, so it is the free
            # refusal every `NOT_ATTEMPTED` here is: nothing was sent.
            return ExitReport(
                outcome=ExitOutcome.NOT_ATTEMPTED,
                external_identifier=order.external_identifier,
                reasons=ordered,
                reason=(
                    f"a profit take is sent only at the cheapest expressible limit "
                    f"(-{PROFIT_TARGET_MARK}); this market builds {order.limit_price}, "
                    "so nothing was sent, and the next cycle looks again"
                ),
            )

        await self._audit.record(
            "watchdog.exit_started",
            {
                "position": spread.to_payload(),
                "order": order.to_payload(),
                "trigger_reasons": [str(r) for r in ordered],
                "policy_cadence_s": policy.cadence_s,
                "policy_cycle_wait_s": policy.cycle_wait_s,
                # Seconds since the watchdog's watch list was last successfully
                # rebuilt (§3.7, 2026-09-10) — the one number that would evidence a
                # stale scan firing an exit on a spread that has since closed.
                # RECORDED, never decided on; None before the first successful scan
                # or when no source was bound.
                "scan_age_s": None if self._scan_age is None else self._scan_age(),
                # From `PriceWalk` itself, not recomputed: the number in the record
                # must be the number the walk will actually stop at — which is why
                # `width` is passed here too. Omitting it would record an UNCAPPED
                # floor for a walk that will in fact stop at the width (2026-09-17).
                "walk_away_price": float(
                    PriceWalk.from_policy(
                        order.limit_price, policy, width=order.width
                    ).floor
                ),
                # THE OFFER THE LADDER IS WALKING TOWARD, off the very `market` this
                # walk was handed — so it cannot disagree with the triggering row, which
                # was written from the same object. Telemetry only, and RAW: not capped
                # at the width, because censoring it is what made `watchdog.crossed`'s
                # `crossing_limit` unusable for the distribution.
                #
                # A COPY, deliberately, and not the join the 2026-09-18 merge replaced
                # it with. The join is exact only on the trigger's RISING edge, where
                # `_trigger` writes Tier 1 `watchdog.triggered`. Every later exit on a
                # still-triggered position — the flatten on a spent one, the
                # `NOTHING_AT_BROKER` / `CROSS_UNPRICEABLE` retries, the flatten's
                # override — is preceded by Tier 2 `watchdog.triggered_still` instead:
                # queued, so its `seq` can land AFTER this Tier 1 row, and droppable
                # when the queue is full. Those are the flatten-regime exits the owed
                # `exit_walk_away_pct` re-derivation needs most. This row is Tier 1 and
                # awaited, so the fact is durable and ordered here. (2026-09-18)
                "crossing_debit": payload_number(market.crossing_debit),
            },
        )

        # `guard_first_submission=True`, and it is NOT a formality. The exit carries
        # its own brand-new `external_identifier` (`argus-close-<candidate>`), so the
        # guard's rule — check before a new trade idea's first submission — applies
        # exactly. The scenario it covers is a crash mid-exit: on restart the position
        # is still open, the watchdog triggers again, and without the guard a second
        # closing order would stack on the first. A `BLOCKED` outcome means a close is
        # already in flight, which is the right answer and must never be crossed on
        # top of.
        try:
            walk = await self._loop_factory(policy=policy).run(
                order, guard_first_submission=True
            )
        except GuardUnreachable as exc:
            # The ONE exception-shaped route to `NOT_ATTEMPTED`, and the only reason
            # the report is built in a helper rather than here: every other
            # `NOT_ATTEMPTED` in `close` sits lexically above the walk, which is how
            # the flatten-allowance restore proves it can never describe a sent order
            # (T5's AST test). An `except` clause cannot sit above the thing it wraps,
            # so the proof moves to the helper — which contains no submit path at all,
            # and is swept for one by the same test.
            return self._guard_unreachable_report(order, ordered, exc)
        report = await self._after_walk(order, walk, ordered)
        await self._record_exit(report, spread, stub=stub)
        return report

    def _guard_unreachable_report(
        self,
        order: Order,
        reasons: tuple[TriggerReason, ...],
        exc: GuardUnreachable,
    ) -> ExitReport:
        """A spent guard burst is a REFUSAL TO SEND, not a failure. (2026-09-03)

        `NOT_ATTEMPTED`, a fourth cause joining the three construction refusals — and
        the outcome must not split, for the reason `close` already gives: the
        allowance downstream asks exactly one question, *was anything sent*, and the
        answer here is the same "no". The SENTENCE is what differs, because the
        operator's next move does: not "the feed is thin", but "the broker cannot be
        reached, and Argus is holding a position it has decided to close".

        **Nothing was sent and nothing can have been.** `GuardUnreachable` is raised
        only by `loop._ask_guard`, which runs before `_first_submit` and whose `try`
        contains one await — the guard's GET. So the position keeps its one exit
        attempt AND its flatten override, and `_run_exit`'s restore stays true.

        **This is not a licence to send.** The next tick rebuilds and asks the guard
        again from scratch; the submit path is still reachable only through an
        answered `ConflictVerdict`. What is retried is the QUESTION.
        """
        return ExitReport(
            outcome=ExitOutcome.NOT_ATTEMPTED,
            external_identifier=order.external_identifier,
            reasons=reasons,
            reason=(
                f"{type(exc.last).__name__}: the duplicate guard could not be asked "
                f"in {exc.attempts} attempt(s) ({exc.last}); nothing was sent, and "
                "the next cycle asks again"
            ),
        )

    # -- construction ------------------------------------------------------

    def _build_exit_order(self, spread: OpenSpread, market: SpreadMarket) -> Order:
        """The exit order, from chunk 1's builder. Not a second construction path.

        `build_order` is reused rather than reimplemented because it is where three
        rails live: one multi-leg order (never two), a signed tick-aligned limit, and
        the freshness bound on the quotes behind the price. Feeding it a
        `SpreadSignal` assembled from live quotes is slightly awkward — the type was
        shaped for a candidate — but the awkwardness buys exactly one place in the
        codebase that turns a vertical plus a mid into an order.

        **The freshness bound is `staleness.market_data_max_age_s`, NOT
        `execution.signal_max_age_s`** (2026-08-20). The entry-only value exists to
        keep one invariant true: the price submitted must be the price the pre-trade
        gate approved, so it bounds the handful of milliseconds between gate-pass and
        construction. **No such invariant exists here.** Nothing gates an exit; the
        watchdog decides, and it decides against `market_data_max_age_s` — the bound
        `LegQuotes.resolve` already applied to every quote in this `SpreadMarket`.
        Passing the tighter entry value made the 1.0-2.0s band a dead zone: triggers
        fired there routinely and could never become orders, which on 2026-08-20 was
        a real stop that had to wait for a fresher quote before it could act.

        Keeping a bound at all is deliberate — a quote past the trigger's own
        threshold is one the trigger would refuse to evaluate, so there is nothing
        honest to price from — but it is now the SAME threshold, not a second,
        tighter one that no exit-path invariant asked for.
        → docs/decisions/2026-08-20-exit-order-freshness-bound.md
        """
        signal = SpreadSignal(
            underlying=spread.underlying,
            side=market.short.contract.right,
            short_leg=market.short.as_option_quote(),
            long_leg=market.long.as_option_quote(),
            credit=market.cost_to_close,
            width=market.width,
            captured_at=market.captured_at,
        )
        return build_order(
            signal,
            direction=OrderDirection.CLOSE,
            contracts=spread.contracts,
            trading_date=self._trading_date(),
            now=self._clock(),
            max_signal_age_s=self._config.staleness.market_data_max_age_s,
        )

    # -- what to do with the walk's answer ---------------------------------

    async def _after_walk(
        self, order: Order, walk: WalkResult, reasons: tuple[TriggerReason, ...]
    ) -> ExitReport:
        if walk.outcome is WalkOutcome.FILLED:
            return ExitReport(
                outcome=ExitOutcome.FILLED,
                external_identifier=order.external_identifier,
                reasons=reasons,
                walk=walk,
            )
        if walk.outcome is WalkOutcome.NO_ORDER_EXISTS:
            # The send failed AND the broker, asked under this identifier, named
            # nothing. Reported separately from the group below because the sentence
            # that justifies that group — "a second order would stack onto something
            # we cannot see" — is the one thing this outcome rules out (2026-08-28).
            # Not crossed, though: crossing pays the market to guarantee closure, and
            # nothing here says the ordinary walk has stopped working. What this earns
            # is another try at the same price, through the guard.
            return ExitReport(
                outcome=ExitOutcome.NOTHING_AT_BROKER,
                external_identifier=order.external_identifier,
                reasons=reasons,
                walk=walk,
                reason=f"walk ended {walk.outcome}: {walk.reason or '-'}",
            )
        if _is_profit_take(reasons):
            # Below the two branches a profit take SHARES — a fill is a fill, and a
            # confirmed-empty broker is retryable through the guard on both paths —
            # and above the only one it must never reach: the cross.
            return await self._settle_profit_take(order, walk, reasons)
        if walk.outcome is not WalkOutcome.WALKED_AWAY:
            # REJECTED / NEVER_WORKED / CANCELLED_ELSEWHERE / UNRESOLVED / BLOCKED /
            # ABORTED. None of these is "the auction did not pay enough", so none of
            # them is answered by paying more. REJECTED and UNRESOLVED in particular
            # are not self-resolving and a second order would either be refused the
            # same way or stack onto something we cannot see.
            return ExitReport(
                outcome=ExitOutcome.NOT_RESOLVED,
                external_identifier=order.external_identifier,
                reasons=reasons,
                walk=walk,
                reason=f"walk ended {walk.outcome}: {walk.reason or '-'}",
            )
        return await self._cross(order, walk, reasons)

    async def _settle_profit_take(
        self, order: Order, walk: WalkResult, reasons: tuple[TriggerReason, ...]
    ) -> ExitReport:
        """A profit take the auction did not fill. Confirm it dead; NEVER cross.
        (2026-09-23)

        **Design C of the 2026-09-22 record, §11.** The cross exists because a
        stopped-out position must get flat NOW, and it is where both of the
        position-spending outcomes come from — `CROSS_REFUSED` is its failure mode. A
        profit take has no deadline (the flatten is hours away and a failed tick
        costs nothing), so importing the emergency mechanism would be paying its
        failure modes for nothing. Removing it removes them: `CROSS_REFUSED` and
        `CROSS_UNPRICEABLE` are unreachable from here by construction.

        What is left is the part that made C conditional, and it is built here rather
        than assumed: a walk that ended with nothing filled must not report as an
        absolute spend, AND must not report as retryable while its order may still be
        live. So `_await_settled` — the one POSITIVE confirmation in this module, the
        same one `_cross` requires before it acts — decides:

        - the walk's order filled after all (any rung, identifier-wide) -> `FILLED_LATE`;
        - the broker NAMED the order and it is terminal, nothing filled -> `UNFILLED`,
          retryable under the profit target's own two-send budget;
        - anything less -> `NOT_RESOLVED`, which retires the profit target for this
          position. The EMERGENCY exit is untouched either way — the watchdog lets
          a profit take's report near `_attempted` only on a fill — and should it later fire
          while this order is somehow still live, its duplicate guard BLOCKS rather
          than stacking a second close, exactly as it does behind a `CROSS_REFUSED`.

        That last case is why this does not collapse into §11.4's option D: D's
        guard-gated re-arm would be refused by the same live order. A working close at
        the broker legitimately blocks a second close on every path; the page says so.
        """
        base = f"walk ended {walk.outcome}: {walk.reason or '-'}"
        if walk.outcome not in _PROFIT_SETTLE:
            # `_PROFIT_RETIRE`: nobody read the broker, a close already exists, or the
            # session is stopping. None of them is a state to retry a take from.
            return ExitReport(
                outcome=ExitOutcome.NOT_RESOLVED,
                external_identifier=order.external_identifier,
                reasons=reasons,
                walk=walk,
                reason=base,
            )
        if walk.outcome is WalkOutcome.REJECTED and walk.broker_order_id is None:
            # Refused AT THE DOOR: `_first_submit` saw a REJECTED ack and returned before
            # an id was recorded, so there is no order id for `_await_settled` to
            # confirm — and nothing to confirm, because the broker's own ack is the
            # terminal word on the only order this walk sent (no id, so no reprice).
            # `_finish`'s identifier-wide probe has already found no fill, and the
            # guard permitted only DEAD before the send. Waiting out the settle window
            # to refuse on `_SETTLE_NO_ID` would retire a take over an order the
            # broker never accepted.
            return ExitReport(
                outcome=ExitOutcome.UNFILLED,
                external_identifier=order.external_identifier,
                reasons=reasons,
                walk=walk,
                reason=f"{base}; refused at submission, so nothing is working",
            )
        settled = await self._await_settled(
            order.external_identifier, walk.broker_order_id
        )
        if settled.outcome is ExitOutcome.FILLED_LATE:
            return ExitReport(
                outcome=ExitOutcome.FILLED_LATE,
                external_identifier=order.external_identifier,
                reasons=reasons,
                walk=walk,
                reason=settled.reason,
            )
        if settled.outcome is None:
            return ExitReport(
                outcome=ExitOutcome.UNFILLED,
                external_identifier=order.external_identifier,
                reasons=reasons,
                walk=walk,
                reason=(
                    f"{base}; its order is confirmed terminal and nothing filled — a "
                    "profit take never crosses, so the next look may try again"
                ),
            )
        bound = f"{self._config.watchdog.exit_cancel_settle_s:g}s"
        seen = _PROFIT_UNSETTLED.get(settled.sighting or "", "not confirmed")
        return ExitReport(
            outcome=ExitOutcome.NOT_RESOLVED,
            external_identifier=order.external_identifier,
            reasons=reasons,
            walk=walk,
            reason=(
                f"{base}; the order was not confirmed terminal within {bound} (last "
                f"seen: {seen}), so the profit target sends nothing further for this "
                "position — the emergency exit keeps its attempt, and its duplicate "
                "guard refuses to stack a close on this order while it is live"
            ),
        )

    async def _cross(
        self, order: Order, walk: WalkResult, reasons: tuple[TriggerReason, ...]
    ) -> ExitReport:
        """The walk gave up. Guarantee closure by paying the market.

        Two things happen before the order goes out, and neither is optional.

        **The abandoned order must be confirmed gone.** `cancel()` returning means
        the broker accepted a cancel REQUEST — the order sits at `Cancel Requested`
        and can still fill (chunk 3, property 3). Submitting a second closing order
        on top of one that can still fill closes the spread TWICE, which does not
        flatten the position: it re-opens it, inverted, unmanaged, on expiry day.
        So this waits a bounded `watchdog.exit_cancel_settle_s` for the order to be
        CONFIRMED terminal, and REFUSES to cross on anything less than a confirmation
        — including an answer that does not mention the order at all. Refusing leaves
        a losing position open and pages a human, which is strictly better than a
        position nobody intended.

        **The order in question is `walk.broker_order_id`, not the identifier.** A
        walk reprices, and every rung mints a new broker id under the SAME
        `external_identifier`, so the abandoned exit is one row among several the
        broker will report — and only that row's disposition says anything about
        whether a second closing order is safe.

        **The price is re-derived from a fresh quote.** The walk ran for several
        seconds; the mid it started from is not the market now.
        """
        settled = await self._await_settled(
            order.external_identifier, walk.broker_order_id
        )
        if settled.outcome is ExitOutcome.FILLED_LATE:
            return ExitReport(
                outcome=ExitOutcome.FILLED_LATE,
                external_identifier=order.external_identifier,
                reasons=reasons,
                walk=walk,
                reason=settled.reason,
            )
        if settled.outcome is ExitOutcome.CROSS_REFUSED:
            self._notify(
                "watchdog_cross_refused",
                {
                    "external_identifier": order.external_identifier,
                    "reason": settled.reason,
                },
            )
            return ExitReport(
                outcome=ExitOutcome.CROSS_REFUSED,
                external_identifier=order.external_identifier,
                reasons=reasons,
                walk=walk,
                reason=settled.reason,
            )

        price = self._cross_limit(order, walk)
        if price is None:
            # NOT alerted here (2026-08-31). This outcome is now RETRYABLE, and the
            # thing an operator needs with it — which attempt this was, how many are
            # left — only the watchdog knows. `_record_exit` still fires
            # `watchdog_exit_cross_unpriceable` for the attempt itself, and
            # `watchdog_cross_unpriceable` is now raised by the watchdog's escalation
            # with the budget attached; alerting from both places would page three
            # times for one event. `CROSS_REFUSED` above keeps its own notify because
            # it is terminal — there is no retry context to wait for.
            return ExitReport(
                outcome=ExitOutcome.CROSS_UNPRICEABLE,
                external_identifier=order.external_identifier,
                reasons=reasons,
                walk=walk,
                reason="no usable crossing price (no offer on the short leg?)",
            )

        limit = price.limit
        # `with_limit_price`, not a fresh build: same legs, same external_identifier,
        # same trade idea at a different number — exactly what a reprice is, and the
        # same model method chunk 3's walk uses. A rebuild would also fail
        # `build.py`'s credit-matches-legs assertion, which is correct: a crossing
        # price is deliberately NOT a mid.
        crossing = order.with_limit_price(limit)
        # No duplicate guard here, for the reason a replace is not guarded: this
        # re-sends the same identifier for a trade idea already in flight, and the
        # thing the guard would find is the order we just confirmed is terminal.
        ack = await self._execution.submit(crossing, dry_run=False)
        # ONE builder for the Tier 1 row AND the page (2026-09-25): the two used to be
        # hand-built separately, and both carried the walk's OPENING limit as
        # `walked_away_at` — on 2026-09-24 one tick for a walk that stopped four up,
        # which hid that the cross had priced two ticks BELOW its own ladder.
        detail = self._crossed_detail(order, walk, price)
        await self._audit.record(
            "watchdog.crossed",
            {
                "order": ack.order.to_payload(),
                **detail,
                "trigger_reasons": [str(r) for r in reasons],
            },
        )
        self._register_cross(ack.order, crossing)
        self._notify(
            "watchdog_crossed_market",
            {**detail, "broker_order_id": ack.order.broker_order_id},
        )
        return ExitReport(
            outcome=ExitOutcome.CROSSED,
            external_identifier=order.external_identifier,
            reasons=reasons,
            walk=walk,
            cross_limit_price=limit,
            cross_broker_order_id=ack.order.broker_order_id,
        )

    async def _await_settled(
        self, external_identifier: str, broker_order_id: int | None
    ) -> _Settlement:
        """Wait, bounded, for the abandoned exit order to be CONFIRMED terminal.

        Returns FILLED_LATE if it filled, `outcome=None` only where the broker
        positively confirmed the order is dead and the cross may proceed, and
        CROSS_REFUSED for everything else at the deadline. A broker read that raises
        is retried inside the same window — the deadline is the bound, so an
        unreachable broker ends as CROSS_REFUSED, which is the fail-closed answer.

        **An answer that does not name THIS order is a read gap, not a verdict**
        (2026-08-21, A3 and A3b). `by_identifier` filters `get_live_orders()`, which
        reports all of today's orders including terminal ones — so one exit's
        identifier legitimately matches SEVERAL rows: the walk's abandoned,
        genuinely-terminal earlier rungs plus whatever the current rung is. Reading
        "every row under this identifier is terminal" as a confirmation is wrong two
        ways, and they are the same way:

          - EMPTY answer (A3): `any()` over an empty list is False, so an order
            nobody mentioned satisfied "nothing is working";
          - PARTIAL answer (A3b): the earlier rungs come back terminal and the
            CURRENT one — still at `Cancel Requested` — is simply missing, so a
            non-empty, entirely-terminal answer said "dead" about an order that can
            still fill.

        Both are answered by the same rule at the correct granularity: the only
        order whose disposition authorizes a cross is `walk.broker_order_id`, and it
        must be NAMED in the answer and terminal. Present-and-working, and absent
        from an answer of any size, both refuse. `PriceWalkingLoop` already treats
        the identical signal as a read gap two frames away, bounding it with
        `blind_cycles` rather than concluding from one look; this is that rule at the
        one call site where the conclusion authorizes a SECOND closing order, so
        absence is retried inside the window like an exception is and refuses at the
        deadline.

        **The FILLED check stays WIDE, and that asymmetry is deliberate.** A fill on
        ANY rung of this exit means a closing order filled and the position is
        already flat; narrowing it to `broker_order_id` would let a cancelled current
        rung read as "confirmed terminal, safe to cross" while an earlier rung's fill
        sat right there in the same answer. Wide-FILLED only ever refuses more, which
        is the safe direction; narrow-terminal only ever confirms less. Do not
        "tidy" them into symmetry.
        """
        deadline = self._clock() + self._config.watchdog.exit_cancel_settle_s
        poll = self._config.execution.status_poll_interval_s
        bound = f"{self._config.watchdog.exit_cancel_settle_s:g}s"
        if broker_order_id is None:
            # Nothing to confirm and no amount of polling will produce it — the walk
            # never learned which order it was walking. Refuse immediately rather
            # than burning the settle window on a question that cannot be asked.
            return _Settlement(
                ExitOutcome.CROSS_REFUSED,
                _SETTLE_REFUSALS[_SETTLE_NO_ID].format(bound=bound),
                sighting=_SETTLE_NO_ID,
            )
        # The last thing the broker actually said, for the page. All of these
        # refuse, but they are not the same event and the operator's next move
        # differs: chase a live order, chase a read, or chase both.
        last = _SETTLE_UNREADABLE
        while True:
            try:
                found = await self._view.by_identifier(external_identifier)
            except Exception as exc:  # noqa: BLE001 - retried until the deadline
                last = _SETTLE_UNREADABLE
                logger.warning(
                    "could not read %s while settling its cancel: %s",
                    external_identifier, exc,
                )
            else:
                if any(classify_order(o) is OrderDisposition.FILLED for o in found):
                    # WIDE on purpose — see the docstring. Any rung filling closes
                    # the spread, whichever rung it was.
                    return _Settlement(
                        ExitOutcome.FILLED_LATE,
                        "the abandoned exit order filled while its cancel settled",
                    )
                # NARROW on purpose: the abandoned rung, not its siblings. More than
                # one match is a broker that reused an id, which classifies
                # fail-closed here rather than picking one.
                mine = [o for o in found if o.broker_order_id == broker_order_id]
                if not mine:
                    last = _SETTLE_ABSENT
                elif any(classify_order(o) is OrderDisposition.WORKING for o in mine):
                    last = _SETTLE_WORKING
                else:
                    # The ONLY confirmation: the broker NAMED this order and every
                    # disposition it gave for it is terminal. Nothing else may reach
                    # here — not an empty answer, not a non-empty one that talks
                    # only about the rungs this walk already abandoned.
                    return _Settlement(None)
            if self._clock() >= deadline:
                return _Settlement(
                    ExitOutcome.CROSS_REFUSED,
                    _SETTLE_REFUSALS[last].format(bound=bound),
                    sighting=last,
                )
            await self._sleep(poll)

    def _cross_limit(self, order: Order, walk: WalkResult) -> _CrossPrice | None:
        """The crossing limit and what bound it, or None when it cannot be priced.

        Re-reads the market rather than reusing the walk's opening snapshot, and caps
        the magnitude at the spread's WIDTH (`build.cap_at_width`, the one
        floor-quantized ceiling): a vertical can never be worth more than the distance
        between its strikes. None — `CROSS_UNPRICEABLE`, retryable — exactly where it
        always was: no fresh legs, no offer on the short leg, a non-finite debit.

        **Priced by what can fill (2026-09-25), keyed on the BOOK, not the trigger:**

        - **The long leg has a bid** — a leg-in is possible, so the natural is a price
          that can trade. The cross is `max(natural, |last rung| + one ladder step)`:
          it never re-bids a price the market has just refused, and still pays the
          natural when the natural is higher. `last rung` is `walk.final_limit_price`,
          the price the walk ACTUALLY bid last (for `WALKED_AWAY` it is always set),
          never `order.limit_price`, which is where the walk STARTED. On 2026-09-24 the
          cross read `order` and bid 0.10 after a ladder that had reached 0.20.
        - **The long leg has no bid** — there is nothing to sell it to, so no leg-in
          exists and only a complex-book or auction responder can fill it. 2026-09-24
          measured that a higher price buys nothing there: rungs at and above a 0.10
          natural went unfilled, then a two-tick bid sat 22 minutes against a one-tick
          natural. So the cross stays at the natural (today's arithmetic, on purpose):
          a resting order at the cheapest price the evidence supports, which costs
          less if a responder does arrive.

        → docs/designs/2026-09-25-deferring-a-far-otm-flatten.md §5
        """
        market = self._market_for(order)
        if market is None:
            return None
        debit = market.crossing_debit
        if not is_finite(debit) or debit <= 0:
            return None
        natural = round_to_tick(debit)
        if natural <= 0:
            return None
        if market.long.bid > 0.0:
            last = (
                walk.final_limit_price
                if walk.final_limit_price is not None
                else order.limit_price
            )
            past_ladder = abs(last) + self._policy.step
            if natural >= past_ladder:
                magnitude, basis = natural, "natural"
            else:
                magnitude, basis = past_ladder, "past_ladder"
        else:
            magnitude, basis = natural, "natural_no_long_bid"
        capped = cap_at_width(magnitude, market.width)
        if capped < magnitude:
            magnitude, basis = capped, "width"
        if magnitude <= 0:
            return None
        return _CrossPrice(
            limit=-magnitude,  # a debit: negative, per tastytrade's sign convention
            basis=basis,
            natural=debit,
            short_ask=market.short.ask,
            long_bid=market.long.bid,
        )

    def _crossed_detail(
        self, order: Order, walk: WalkResult, price: _CrossPrice
    ) -> dict[str, Any]:
        """What `watchdog.crossed` and `watchdog_crossed_market` both say. (2026-09-25)

        `walked_away_at` is the rung the walk STOPPED at (`walk.final_limit_price`),
        with the opening limit beside it under its own name. **A row carrying
        `opening_limit` has the corrected `walked_away_at`; a row without it (v0.5.0
        and earlier) carries the opening limit under that name** — right only where
        the walk had 0 steps. The four pricing fields are telemetry nothing decides on.
        """
        stopped_at = (
            walk.final_limit_price
            if walk.final_limit_price is not None
            else order.limit_price
        )
        return {
            "external_identifier": order.external_identifier,
            "walked_away_at": float(stopped_at),
            "opening_limit": float(order.limit_price),
            "walk_steps": walk.steps,
            "crossing_limit": float(price.limit),
            "limit_basis": price.basis,
            "crossing_debit": payload_number(price.natural),
            "short_ask": payload_number(price.short_ask),
            "long_bid": payload_number(price.long_bid),
        }

    def _register_cross(self, placed: Order, crossing: Order) -> None:
        """Register a working cross as a CLOSING residual. (2026-09-25)

        Nothing else tracked it: the cross is submitted outside the walk, so
        `loop._finish` never saw it, and on 2026-09-24 the spent page said *nothing
        automated will try again* 43 times while it was working at the broker — the
        page that invites a manual second close on top of a working one.

        Registered as `working_expected`, because a cross is SUPPOSED to be working:
        it pages only if it is still resting past `watchdog.unmanaged_order_max_s`,
        and — being a CLOSE — it is never cancelled (`cancel_licence`). Direction is
        the order AS ARGUS BUILT IT (`crossing`), never the broker's relabelled legs.
        """
        if self._residuals is None or placed.broker_order_id is None:
            return
        if not placed.is_working:
            return
        self._residuals.register(
            broker_order_id=placed.broker_order_id,
            external_identifier=crossing.external_identifier,
            minted_direction=crossing.direction,
            candidate_id=crossing.candidate_id,
            walk_outcome="crossed",
            cancel_requested=False,
            last_status=None if placed.status is None else str(placed.status),
            limit_price=float(crossing.limit_price),
            working_expected=True,
        )

    def _market_for(self, order: Order) -> SpreadMarket | None:
        """Fresh quotes for the order's own legs, or None if either cannot be priced.

        `legs[0]` is the short leg and `legs[1]` the long one — the ordering
        `build_order` produces and the only ordering it can produce.
        """
        if self._quotes is None or len(order.legs) != 2:
            return None
        short = self._quotes.resolve(order.legs[0].symbol)
        long_ = self._quotes.resolve(order.legs[1].symbol)
        if short.leg is None or long_.leg is None:
            return None
        return SpreadMarket(short=short.leg, long=long_.leg)

    def bind_quotes(self, quotes: LegQuoteSource) -> None:
        """Share the watchdog's resolver, so the cross reprices off the same market."""
        self._quotes = quotes

    def bind_scan_age(self, scan_age: Callable[[], float | None]) -> None:
        """Share the watchdog's scan freshness, so `watchdog.exit_started` carries it.

        Bound by `PositionWatchdog.__init__` right beside `bind_quotes`, for the same
        reason: the record is written here but the reading lives there, and a seam —
        not a copy — is what keeps the record from disagreeing with `/status` about
        which scan was the last good one. (§3.7, 2026-09-10.)
        """
        self._scan_age = scan_age

    # -- recording ---------------------------------------------------------

    async def _record_exit(
        self, report: ExitReport, spread: OpenSpread, *, stub: bool = False
    ) -> None:
        """Tier 1 record + alert for an exit that actually reached the broker.

        NOT_ATTEMPTED never gets here: nothing was sent, and that path can repeat on
        every cadence tick, so the watchdog escalates it edge-triggered instead. A
        Tier 1 write per cycle would not be more safety — it would be a Tier 1 trail
        nobody can read.

        **The alert dict is hand-built and that is deliberate** — `report.to_payload()`
        carries a nested `walk` dict and the caller adds `position`, and this goes to a
        phone. What it owes is settled once, in `_alert_reject_reason`: an alert that
        NAMES the outcome of an exit attempt carries the broker's reason for it. On
        2026-09-01 this dict's six keys did not, and the string it dropped was
        `'Received a fill on the original order'` — the one sentence saying the exit
        had already succeeded, sitting in the Tier 1 record while the page read as a
        live problem. `candidate_id` stays at the TOP level: `notify.alert_scope` (B1)
        reads it there to give each position its own throttle window.
        """
        await self._audit.record(
            "watchdog.exit_finished",
            {**report.to_payload(), "position": spread.to_payload(), "stub_close": stub},
        )
        if (
            stub
            and report.walk is not None
            and report.walk.outcome is WalkOutcome.BLOCKED
        ):
            # Recorded above; NOT paged (2026-09-24). A stub continuation the guard
            # refused sent nothing, and the watchdog restores and re-asks it
            # (`_after_stub_close`), paging once itself if the refusals run out —
            # the ordinary causes (the remainder still cancelling, a fill not yet
            # booked) clear in seconds, and a `watchdog_exit_not_resolved` per ask
            # would read on the phone as an exit failing.
            return
        if _is_profit_take(report.reasons) and not report.outcome.closed:
            # Recorded above like every exit that reached the broker; PAGED only when
            # it closed the position (2026-09-23). An unfilled take is the auction
            # declining a tick on a WINNING position, with every emergency protection
            # intact — paging it is the 2026-08-05 storm shape, a real signal made
            # audible at the wrong threshold. The one unfilled take a human must hear
            # about — retired with its order possibly still live — is paged by the
            # watchdog, once, with the budget attached, because only it knows.
            return
        # Alerted even on a clean fill: a stopped-out 0DTE position is a rare and
        # significant event (at most `risk.max_concurrent_positions` a day) and is
        # exactly what an operator wants on their phone without having to ask.
        self._notify(
            f"watchdog_exit_{report.outcome}",
            {
                "external_identifier": report.external_identifier
                or spread.external_identifier,
                "candidate_id": spread.candidate_id,
                "contracts": spread.contracts,
                **spread.credit_payload(),
                "trigger_reasons": [str(r) for r in report.reasons],
                "reason": report.reason,
                "reject_reason": _alert_reject_reason(report.walk),
                "mutation_error": _alert_mutation_error(report.walk),
                # Which close this was (2026-09-24): the stub continuation's page
                # otherwise reads exactly like the position's first exit.
                "stub_close": stub,
            },
        )

    def _notify(self, kind: str, detail: dict[str, Any]) -> None:
        logger.warning("%s: %s", kind, detail)
        self._alert(kind, detail)


# -- what a page may say about the credit trigger ------------------------------


#: What a page carries in place of `credit_stop` while the credit trigger is off.
CREDIT_TRIGGER_OFF = {"credit_trigger": "off"}


def page_view(detail: dict[str, Any], *, credit_trigger_enabled: bool) -> dict[str, Any]:
    """The detail as a PAGE may show it. Identity while the credit trigger is on.

    **While `watchdog.credit_trigger_enabled` is false, no page names a credit stop**
    (Richard, 2026-09-27, the v0.5.2 review's Q4). Under the strike-touch stop,
    `credit_stop` is the level a switched-off trigger would use and nothing acts on
    it, and a page that prints `cost_to_close` beside `credit_stop` reads as "the
    mark against the stop". So every `credit_stop` key, at any depth, is dropped, and
    the dict that held it says `"credit_trigger": "off"` instead.

    **Pages only, never records.** The audit record is written from the CALLER's dict,
    which this never mutates (it builds a new one), so the record keeps `credit_stop`
    whichever of the two is written first: the decision record keeps it recorded,
    and a reader of the trail can tell from `credit_trigger_enabled` in config which
    meaning it has. This does not break the one-builder rule (2026-09-02): the page is
    still built from the record's dict, by one pure function at the one sink every
    watchdog page passes through (`PositionWatchdog._notify`), and it removes one
    inert number rather than stating a different fact. A new page gets it for free.
    """
    if credit_trigger_enabled:
        return detail
    return _without_credit_stop(detail)


def _without_credit_stop(value: Any) -> Any:
    if isinstance(value, dict):
        out = {k: _without_credit_stop(v) for k, v in value.items() if k != "credit_stop"}
        if "credit_stop" in value:
            out.update(CREDIT_TRIGGER_OFF)
        return out
    if isinstance(value, list):
        return [_without_credit_stop(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_without_credit_stop(v) for v in value)
    return value


def _mark_phrase(cost_to_close: Any, credit_stop: Any, *, credit_trigger_enabled: bool) -> str:
    """The mark as a journal line says it: "vs stop" only while the credit trigger
    can fire (the same rule as `page_view`, 2026-09-27)."""
    if credit_trigger_enabled:
        return f"cost_to_close={cost_to_close} vs stop {credit_stop}"
    return f"cost_to_close={cost_to_close}, credit trigger off"


# -- the watchdog task --------------------------------------------------------


#: `_Escalation` tags.
#: NO trigger is evaluable — nothing is watching the position. This one pages.
_BLIND = "blind"
#: One trigger unevaluable, the other still live (since 2026-08-13, near-always the
#: credit trigger losing a stale wing quote while the underlying backstop runs on).
#: Recorded, never paged — see `_report_evaluability`.
_DEGRADED = "degraded"
_UNPRICEABLE = "unpriceable"
_UNATTEMPTED = "unattempted"
#: An exit order was sent, the send failed, and the broker confirmed nothing landed.
#: The fourth member of the "wants out, is not out" family, and the only one that
#: has already spent a submission — so unlike `_UNATTEMPTED` it is bounded by a
#: COUNT as well as by `overdue`'s bound on silence. (2026-08-28)
_NOTHING_SENT = "nothing_sent"
#: The abandoned exit order was CONFIRMED terminal and the market-crossing limit then
#: could not be priced — no offer on the short leg. The fifth member of the family,
#: budgeted like `_NOTHING_SENT` and tagged separately from it on purpose: on a phone
#: the two read identically ("triggered, nothing in the market, still retrying") and
#: the operator's next move is opposite. `_NOTHING_SENT` says go and look at the
#: BROKER; this one says go and look at the FEED — or at the book, because a short leg
#: with no offer may simply not have one. (2026-08-31)
_CROSS_UNPRICEABLE = "cross_unpriceable"
#: The position's one exit attempt is SPENT and the ledger still says it is held.
#: The third member of the same family as the two above — the watchdog has decided
#: this position must be closed and it is not closed — and the worst of the three,
#: because the other two at least still have a retry running. This one has no
#: automated path left at all, so the only thing that can still get worse is
#: silence. Bounded like them, by `overdue`. (2026-08-21, A2)
_SPENT = "exit_attempt_spent"
#: A SPENT position, past the day's flatten mark, is eligible for its ONE override.
#: (2026-09-02)
#:
#: Tier 1 on the rising edge and **never cleared** — there is no `falling` call for
#: this tag anywhere, deliberately. It is a "we have told you" edge, not a window that
#: re-arms: the override is granted once per position per process, and an operator who
#: has read that sentence does not need it again on every cadence tick.
#:
#: It had to become an edge at all because the page sits at the ELIGIBILITY site while
#: the allowance is spent one frame further on, past `_trigger`'s `market is None`
#: early return. A spent position whose legs cannot be priced therefore never reaches
#: the spend and re-qualifies every tick: measured at 8 Tier 1 records for 8 ticks,
#: which is exactly the trail `_Escalation` exists to prevent. It did not surface in
#: live fire because the 2026-09-01 flattens were refused at the BUILD rather than at
#: the legs, so they spent the allowance and the spend silenced the repeat — the
#: allowance bug was masking this one, and giving the allowance back un-masks it.
#:
#: The repeats' bound on SILENCE is not here and should not be: `_UNATTEMPTED` and
#: `_UNPRICEABLE` already carry it with their `overdue` repeat, and the recurring
#: sentence worth paging is "still not in the market", never "still eligible".
_FLATTEN_OVERRIDE = "flatten_override"
#: The trigger itself. A sustained condition keeps firing on every subsequent
#: evaluation, and while an exit cannot be built (the unpriceable case) that repeats
#: indefinitely — so "this position has tripped" is one event per EPISODE, not one
#: per cadence tick, and the window re-arms when the condition stops firing.
_TRIGGERED = "triggered"
#: The ledger and the broker disagree about how much is held. Not per-position — one
#: sentinel key, because the condition is about the BOOK, not about a spread.
_LEDGER_DISAGREES = "ledger_disagrees"
_BOOK_KEY = "__book__"
#: The WATCHDOG ITSELF is failing — a distinct condition from `_DEGRADED` above,
#: which is one position's trigger being unevaluable. One sentinel key again, because
#: the condition is about the WATCHDOG, not about any spread it holds. (2026-09-10)
_WATCHDOG_FAILING = "watchdog_failing"
#: The spot path's bound: an hour of passes at a 1-second cadence. The deferral reads
#: only the trailing 30 minutes, so this is twice what it needs and still small.
_SPOT_PATH_MAX = 3600
#: The trailing realized-vol window. Matched to the calibration's 30-minute windows,
#: so the runtime RV is the RV the tail was fitted with.
_DEFERRAL_RV_WINDOW = timedelta(minutes=30)
_WATCHDOG_KEY = "__watchdog__"

#: How often a degraded episode re-pages, via `_Escalation.overdue`. A CONSTANT, not a
#: config key: a new REQUIRED key forces a coupled config+image deploy, and this is a
#: paging cadence, not a rail. 300s puts ~3 repeats inside `SlackNotifier`'s 900s
#: window, where they coalesce into one `(+N suppressed)` message — the bounded-RATE
#: shape `_Escalation.overdue` documents, never a single page.
_DEGRADED_PAGE_INTERVAL_S = 300.0
#: Hard cap on how many distinct `(broker, ledger)` shapes hold a cached REST
#: verdict at once. Not a tuning knob and deliberately not config: the TTL is what
#: bounds the cache's LIFETIME, and in every realistic episode a handful of shapes
#: are in flight at a time — this only stops a pathological churn from accumulating
#: entries nobody will ever read. Eviction is oldest-measured-first and is
#: safety-neutral in the only direction that matters: dropping an entry costs a
#: round trip, never a suppressed page.
_BOOK_CONFIRM_CACHE_MAX = 16

#: Closing orders one position may SEND while the exit keeps failing to COMPLETE and
#: the broker has confirmed there is nothing to stack onto — `NOTHING_AT_BROKER` (the
#: send failed and the broker named no order) and, since 2026-08-31,
#: `CROSS_UNPRICEABLE` (the abandoned order was positively confirmed terminal and only
#: the crossing PRICE could not be built). The budget is per position and per process;
#: every other exit outcome still spends the attempt on the first try.
#:
#: **ONE budget shared by both, deliberately.** The hazard being bounded is how many
#: closing orders a broker sees from one position that will not close, and that is a
#: single hazard however the attempt failed. A position that has already burned two on
#: `NOTHING_AT_BROKER` correctly has one left, not another three.
#:
#: NOT config, and deliberately — the same split `entry.py`'s `_GUARD_ATTEMPTS` and
#: `reconcile.py` make: the burst's SHAPE mirrors an established precedent (three
#: attempts, the entry guard's bounded burst) while the numbers an operator has a
#: reason to tune are config. It also keeps this an image-only deploy, which for a
#: change to the exit path is worth something on its own.
#:
#: Three, not more: the retries are spaced by the watchdog cadence and each costs a
#: submission, so the cap is what stops a broker that fails every send from being
#: asked all afternoon. Three, not one: v0.3.11 gave the ENTRY duplicate guard a
#: three-attempt burst because a single unanswered call was ending trading days —
#: and a failed entry costs opportunity where a failed exit costs money, so the path
#: carrying the real downside must not have strictly less resilience than the one
#: carrying opportunity cost. (2026-08-28, finding 4)
_EXIT_SEND_ATTEMPTS = 3

#: The exit outcomes a position may RETRY, and the escalation each one raises:
#: `(escalation tag, Tier 1 audit category, alert kind)`.
#:
#: **Membership is the whole of the one-attempt rule's exception list**, and it is a
#: mapping rather than two `elif`s so that list is one greppable object. Everything
#: absent from it spends the position's attempt on the first try, including — and
#: especially — `CROSS_REFUSED`.
#:
#: What the two members share is not "the exit failed" but a specific, positive answer
#: from the BROKER about what exists:
#:
#: - `NOTHING_AT_BROKER` (2026-08-28) — the send failed, the broker was asked under
#:   this identifier, and it named nothing. Its own record is explicit that this is
#:   *not* proof no order can appear, only that none was seen.
#: - `CROSS_UNPRICEABLE` (2026-08-31) — STRONGER. It is unreachable except through
#:   `_await_settled` returning `outcome=None`, which is the one positive confirmation
#:   in this module: the broker NAMED `walk.broker_order_id` and every disposition it
#:   gave for that order was terminal, and no rung under the identifier filled. The
#:   only thing that then failed is building a crossing price out of a feed that had no
#:   offer a moment ago and may have one now.
#:
#: **Neither is licensed by its own answer.** The licence is `DuplicateOrderGuard`
#: re-asking the broker on the next tick's walk (`guard_first_submission=True`), which
#: comes back BLOCKED if anything did land. For `CROSS_UNPRICEABLE` that is strictly
#: MORE protection than the escalation it replaces: `_cross` acts on the identical
#: confirmation by submitting *unguarded*.
#:
#: **Both members mean NOTHING FILLED, and one thing outside this module depends on
#: that** (2026-09-10): the retry re-runs a whole walk under the same `argus-close-*`
#: key, so `loop._finish`'s identifier-wide FILLED read could in principle
#: hand a later walk an earlier walk's fill. It cannot, because a walk that filled
#: produces neither of these — the first is a positively-empty broker answer, the
#: second `_await_settled`'s positive confirmation that nothing under the identifier
#: filled. A future member that can follow a PARTIAL fill breaks that reasoning and it
#: has to be redone; that is the only way the gate can produce a false `FILLED`.
_RETRYABLE_EXITS: dict[ExitOutcome, tuple[str, str, str]] = {
    ExitOutcome.NOTHING_AT_BROKER: (
        _NOTHING_SENT,
        "watchdog.exit_nothing_sent",
        "watchdog_exit_nothing_sent",
    ),
    ExitOutcome.CROSS_UNPRICEABLE: (
        _CROSS_UNPRICEABLE,
        "watchdog.exit_cross_unpriceable",
        # The kind `_cross` used to emit directly, moved here rather than renamed: an
        # operator's filters and muscle memory keep working, and the payload gets the
        # things the old one had no way to know — the retry budget, and `candidate_id`
        # at the top level so B1's throttle scopes it by TRADE IDEA like every other
        # per-position page, rather than by the closing order's identifier.
        "watchdog_cross_unpriceable",
    ),
}

#: Profit-take ORDERS one position may send before its profit target retires.
#: (2026-09-23)
#:
#: A COUNT, on `_EXIT_SEND_ATTEMPTS`' precedent and for its reason: each attempt is a
#: SUBMISSION. (P5's re-cancel and the guard's burst are RATES because a cancel and a
#: GET are not.) Not config, for the same split that precedent states — the burst's
#: shape is code, and there is no operator decision in it.
#:
#: TWO, not three: the third emergency send exists because a failed EXIT costs money,
#: and nothing about a profit take is urgent — the flatten is hours away, the position
#: keeps every emergency protection, and a failed look costs nothing. Two is one retry
#: for the transient (a thin book for one auction, a send that never landed) without
#: asking a broker that keeps declining a tick all afternoon.
#:
#: **Its own budget, never `_sends`, and exhausting it retires the FEATURE for the
#: position — never the position's emergency exit or its flatten override.** That is
#: the whole of §11.5's "its own `_profit_spent`, never `_attempted`".
_PROFIT_SEND_ATTEMPTS = 2

#: Profit-take outcomes that may be RETRIED within `_PROFIT_SEND_ATTEMPTS`. Both mean
#: the broker positively said nothing is working and nothing filled — `UNFILLED` after
#: `_await_settled` confirmed the order terminal, `NOTHING_AT_BROKER` after the send
#: failed and the identifier came back empty — and both retry through
#: `DuplicateOrderGuard`, which is the licence, exactly as for `_RETRYABLE_EXITS`.
#: Both mean NOTHING FILLED, which is what keeps `loop._finish`'s identifier-wide read
#: from handing a later walk under the same `argus-close-*` key an earlier one's fill.
_RETRYABLE_PROFIT_TAKES = frozenset({ExitOutcome.UNFILLED, ExitOutcome.NOTHING_AT_BROKER})

#: The profit target's own trigger EPISODE. (2026-09-23)
#:
#: Separate from `_TRIGGERED` because sharing one edge would let a profit episode
#: swallow a STOP's page: a take that goes unfilled leaves `_TRIGGERED` risen, and an
#: underlying breach firing on the next tick would arrive as Tier 2 `triggered_still`
#: with no `watchdog_triggered` on the phone. Recorded Tier 1 on its rising edge like
#: any trigger, and never paged — a take pages when it closes the position.
_PROFIT_TRIGGERED = "profit_triggered"

#: The broker REJECTED a profit take's closing order. Paged once per position and
#: never cleared (2026-09-23 review), on `_FLATTEN_OVERRIDE`'s "we have told you"
#: shape. A take is otherwise quiet when it does not fill, but a rejection is not the
#: auction declining a tick: it is the broker refusing a CLOSE on this position, which
#: is C5 — and the emergency exit will send the same kind of order. Hearing it from a
#: winning position at 11:00 instead of from the stop at 14:40 is the whole point.
_PROFIT_REJECTED = "profit_rejected"

#: The stub continuation sent nothing (2026-09-24). A member of `_UNATTEMPTED`'s
#: "wants out, is not out" family scoped to the stub path's own edge, so a
#: construction refusal there cannot arm the emergency path's tag (and vice
#: versa) — each path's page counts its own episodes. It FALLS when a stub
#: continuation reports anything that is not `NOT_ATTEMPTED`, so a later,
#: separate episode on the same position pages again rather than going quiet.
_STUB_UNATTEMPTED = "stub_close_unattempted"

#: Seconds a position's profit target stands down after a take that SENT NOTHING
#: (`NOT_ATTEMPTED`). (2026-09-23 review)
#:
#: A RATE, not a count — the codebase's rule for a retry that costs no submission (P5's
#: re-cancel, the guard's burst), against `_PROFIT_SEND_ATTEMPTS`' count for one that
#: does. Without it a refusal re-fired on the very next tick, forever: in a broker
#: outage every attempt spends up to ~12s in the guard's burst with the position in
#: `_exiting`, where `check_once` skips it WHOLE — so a non-urgent feature held the
#: stop, the backstop and the flatten to one look every ~13s for as long as the outage
#: lasted. With the stand-down the position is evaluated every tick in between, and a
#: take is at most one guard burst in every `12 + 30` seconds.
#:
#: 30s, the same order as `watchdog.exit_unattempted_max_s` and sized by the same
#: sentence: an ordinary quote or broker blip resolves inside it. A constant, not
#: config: a new REQUIRED key would force a coupled deploy for a pacing detail, and the
#: take loses nothing by waiting — the flatten is hours away.
_PROFIT_REFUSAL_BACKOFF_S = 30.0

#: Tier 1, written by `_maybe_stub_close` BEFORE the continuation's FIRST order can
#: go out — the DURABLE spend of a stub's ONE further close (2026-09-24). Written
#: ONCE per stub: a no-send restore re-hands the same allowance, and the durable
#: row it would repeat already says what a boot needs (a re-handoff is Tier 2
#: `watchdog.stub_close_retry`). Re-read on every boot by
#: `risk.rehydrate.load_stub_close_spent`, so a crash loop cannot stack a second
#: allowance-backed send on the same stub. Named here (the writer's file) and
#: pinned equal to `risk.rehydrate.STUB_CLOSE_CATEGORY` (the reader's) by test —
#: the FILL_CATEGORY precedent, which keeps the risk package free of a dependency
#: on the execution package.
STUB_CLOSE_CATEGORY = "watchdog.stub_close"

#: How many guard REFUSALS (`WalkOutcome.BLOCKED`, any reason) may restore the stub
#: allowance before it stays spent for good (2026-09-24). Every `BLOCKED` is
#: returned by the walk BEFORE anything is submitted, so each one proves nothing
#: was sent — which is what makes restoring it safe; the next ask goes back through
#: the guard, which re-blocks for as long as the cause stands. The ordinary causes
#: are transient and short: the partial's own remainder still at `Cancel
#: Requested` (measured dead inside ~1.5s) and a closing fill not yet booked
#: (4-5s). Retries are PACED — one per `watchdog.refresh_interval_s`, which also
#: guarantees a rebuilt watch list between asks — so three refusals span ~45s, an
#: order of magnitude past either cause, and a fourth ask would be asking a
#: question whose answer is not going to change. Unbounded re-asks are the
#: alert-storm shape this repo has rejected three times. A module constant, not
#: config: the bound is code, and there is no operator decision in it.
_STUB_BLOCKED_RETRIES = 3


@dataclass(frozen=True)
class ImpairedPosition:
    """One watched position whose triggers are not all evaluable, right now.

    The live read behind `/status`'s impaired-triggers row (2026-08-13). It exists
    because the audit trail cannot answer this question on a phone: `watchdog.check`
    carries `unknown_reasons` every tick, but the audit log serializes with sorted
    keys, which puts `unknown_reasons` LAST — after the long `problems` strings — and
    `/status`'s HTML tail truncates a payload at 240 characters. A degraded position's
    reasons are therefore always cut. Reconstructing state from that tail would be
    working around the truncation; reading it live off the watchdog sidesteps it.

    `impaired_for_s` is the operationally load-bearing field. "Degraded right now" and
    "degraded for 20 minutes and still not clearing" are different signals, and only
    the second one says go and look.
    """

    external_identifier: str
    candidate_id: str
    #: The unevaluable triggers, sorted. Never empty — a position with none is not
    #: impaired and does not appear at all.
    unknown_reasons: tuple[str, ...]
    #: NO trigger evaluable. The paging condition; see `TriggerEvaluation.blind`.
    blind: bool
    #: How long this exact set of reasons has been continuously unevaluable.
    impaired_for_s: float


@dataclass(frozen=True)
class SpentExitAttempt:
    """One held position whose ONE exit attempt is spent. (2026-08-21, A2b)

    The live read behind `/status`'s spent-exits row, and the counterpart to
    `ImpairedPosition` for the state that position lands in NEXT. A spent attempt
    used to leave `_impaired` on the way past — `check_once` popped it, because the
    position was no longer being evaluated and a frozen reading would be a lie that
    only gets older — which until 2026-08-21 also took it off `/status` entirely.
    The page then rendered a position with no automated path to close as an ordinary
    open position, which is the reassuring reading at the one moment it is wrong.
    Same family as the `daily_loss.breach` bug, and fixed the same way: read the
    source of truth live rather than re-deriving it from a structure the state has
    left.

    **The pop is gone as of 2026-08-28**: the position is evaluated on every tick
    again (only the SENDING is spent), so `_impaired` is live for it and this row
    carries the mark that evaluation produces. The two rows stopped being a handover
    and became what they read as — evaluability, and what the thing is worth.

    **Scoped to what the ledger still says is HELD**, never to `_attempted` itself.
    That set is deliberately never pruned (clearing it on scan-exit would hand a
    second attempt to a spread that flickered), so reporting it directly would turn
    this row into a process-lifetime ledger of everything that ever ended badly —
    the exact failure `/status` already had to be fixed for once.

    `spent_for_s` is the operationally load-bearing field, for the same reason
    `impaired_for_s` is: measured from the attempt itself, so it never restarts.
    """

    external_identifier: str
    candidate_id: str
    contracts: int
    #: The `ExitOutcome` that spent the attempt, as a string. "The walk timed out"
    #: and "the cross was refused" are different next moves for whoever picks it up.
    outcome: str
    #: Seconds since the attempt was spent. Never resets — see `_SpentAttempt.since`.
    spent_for_s: float
    #: The last mark, from the most recent evaluation (2026-08-28). `None` where that
    #: tick could not price the position — never a placeholder number, which on a row
    #: whose entire purpose is telling a human what to do by hand would be the one
    #: kind of wrong that gets acted on.
    cost_to_close: float | None = None
    credit_stop: float | None = None
    #: Whether the stop condition was still true at that evaluation. `None` means
    #: unevaluated (no mark yet), which is distinct from False.
    still_triggered: bool | None = None
    #: The exception that spent the attempt, when a CRASH is what spent it
    #: (2026-09-10). Same contract as `reject_reason`/`mutation_error` above — a
    #: sentence the repeating page owes whoever reads it — except it names Argus, not
    #: the broker: an exception is precisely the state in which no broker was involved
    #: or answered, and `not_resolved` with all three None is the reassuring-when-wrong
    #: reading this row has been fixed for twice already.
    crash_error: str | None = None
    #: The stub-close allowance's state for this row (2026-09-24): `available`
    #: (a FILLED spent row whose origin is known and whose one further close is
    #: unspent — waiting for the partial to book, for a paced retry, or behind
    #: the flatten), `sent` (the allowance is spent — a handoff was made),
    #: `exhausted` (`_STUB_BLOCKED_RETRIES` guard refusals consumed it without a
    #: send), or `disabled` (the
    #: switch is off, or the boot read of the durable spend was unreadable). None
    #: where the row is not a stub as defined — an outcome other than `filled`, or
    #: an unknown origin — because no continuation exists there to describe, and
    #: rendering a state would claim one does.
    stub_close: str | None = None
    #: A CLOSING order for this position still working at the broker, from the
    #: residual registry (2026-09-25) — the market-crossing limit a `crossed` exit left
    #: resting. On 2026-09-24 the spent page said "nothing automated will try again"
    #: 43 times while exactly such an order was working, which is the page that
    #: invites a manual second close on top of it (C5's double close). All three None
    #: when the registry names no working close for it.
    working_close_order_id: int | None = None
    working_close_limit: float | None = None
    working_close_for_s: float | None = None
    #: `watchdog.credit_trigger_enabled` (2026-09-27). False means `credit_stop` above
    #: is None ON PURPOSE — there is no credit stop to show, and the row says "credit
    #: trigger off" rather than "vs stop ?". REQUIRED and keyword-only: a default
    #: would be the silent way for a new construction site to claim a stop.
    credit_trigger_enabled: bool = field(kw_only=True)


@dataclass(frozen=True)
class RetiredProfitTarget:
    """One HELD position whose profit target will send nothing further. (2026-09-23)

    Not a problem row in the sense `SpentExitAttempt` is: for every outcome but a fill,
    every emergency protection — both price triggers, the exit attempt, the 15:30
    flatten — is intact here by construction. It exists so a position still marked at
    one tick with no take going out has a sentence on the page saying WHY, rather than
    reading as a feature that silently stopped working. A `filled` row still held is a
    partial fill's stub, whose close IS spent; `spent_exit_attempts()` carries it.

    Scan-driven like `SpentExitAttempt`, so it clears when the ledger stops holding the
    position; `_profit_spent` itself is never pruned.
    """

    external_identifier: str
    candidate_id: str
    #: The `ExitOutcome` that retired it: `filled` (the take closed it and the row has
    #: not left the ledger yet), `unfilled`/`nothing_at_broker` (the two-send budget is
    #: out), or `not_resolved` (the broker's state was left unconfirmed).
    outcome: str
    reason: str | None
    #: The one state that needs a look at the broker: the take's order was not
    #: confirmed terminal, so a close may be resting there — and, while it is, the
    #: emergency exit's duplicate guard will refuse to stack a second one on it.
    order_may_be_working: bool
    retired_for_s: float


@dataclass(frozen=True)
class ProfitTargetState:
    """The profit target, right now. The live read behind `/status`'s row. (2026-09-23)

    `enabled` is `watchdog.profit_target_enabled` as the RUNNING process loaded it —
    promotion is a config flip and a restart, so the page is where an operator
    confirms which one is live, the reason `ResidualOrderRow.cancel_enabled` rides the
    page too.
    """

    enabled: bool
    #: Profit takes that CLOSED a position this session (filled or filled late).
    taken: int
    retired: tuple[RetiredProfitTarget, ...]


@dataclass(frozen=True)
class FlattenDeferralRow:
    """One HELD position the flatten deferral has decided on. (2026-09-25)

    Every number is the watchdog's own latest `DeferralDecision` for it — one
    computation, every reader, the `_SpentMark` rule. `deferring` False after a
    deferral means the flatten was released (`reason` says why) and has been handed
    to the exit path, or is being retried there.
    """

    external_identifier: str
    candidate_id: str
    deferring: bool
    reason: str
    z: float | None
    p_touch: float | None
    lhs: float | None
    premium: float
    distance_points: float | None
    #: Seconds since this position's FIRST deferral of the day.
    deferred_for_s: float


@dataclass(frozen=True)
class FlattenDeferralState:
    """The flatten deferral, right now. The live read behind `/status`'s row.

    `mode`, `k`, `tail_z99` and `tail_alpha` are the values the RUNNING process
    loaded — a change is a config-only release and a restart, so the page is where an
    operator confirms which parameters are live, the `ProfitTargetState.enabled` rule.
    """

    mode: str
    k: float
    tail_z99: float
    tail_alpha: float
    rows: tuple[FlattenDeferralRow, ...]


@dataclass(frozen=True)
class WatchdogHealth:
    """The watchdog's own health, right now. The live read behind `/status`'s Watchdog
    row (2026-09-10).

    A watchdog that parks the process is loud — the process is gone. A watchdog that
    keeps running while failing every tick is, by construction, the quietest failure
    this system can produce: `phase: running`, sessions scheduled, the 15-second
    status line printing. This record is what makes that state visible, and its three
    shapes are three different operator actions:

    - never failed — `WATCHING`: the steady state;
    - currently degraded — `DEGRADED`: the rows below it on the page are as of the
      last clean pass, and the failure named here is live;
    - recovered, halt still in force — `RECOVERED`: the watchdog is watching again but
      entries stay halted until `POST /resume`, because the halt is permanent by
      design and the failure is not.

    `_failure`-derived fields describe the MOST RECENT failure and deliberately
    survive recovery — the RECOVERED state needs them to say what the episode was.
    All ages are on the watchdog's own monotonic clock, so only DURATIONS leave this
    module, exactly as `_Impairment` and `_SpentAttempt` already rule.
    """

    degraded: bool
    #: Spreads the ledger currently says are open — the count behind "N position(s)".
    watching: int
    #: Process-lifetime total failures, recovered or not.
    failures: int
    #: Of the most recent failure. Survives recovery.
    where: str | None
    error_type: str | None
    error: str | None
    #: How long the CURRENT degradation episode has run. None unless degraded.
    degraded_for_s: float | None
    #: Age of the most recent failure. None if there has never been one.
    last_failure_for_s: float | None
    #: Age of the last pass that completed with no failure at all. None until one has.
    last_clean_pass_s: float | None
    #: Identifiers that failed in THIS episode. Empty once recovered.
    affected: tuple[str, ...]


def _opt_str(value: Any) -> str | None:
    """A `malformed` field that may be absent, None, or already a string."""
    return None if value is None else str(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _malformed_detail(
    entry: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Flatten one `malformed` dict's per-symbol numbers into plain strings.

    Six shapes reach `OpenSpreadScan.malformed` (`store.py`) and only two carry
    per-symbol counts: `outstanding` (a lopsided remainder — `{symbol: qty}`) and
    `divergent` (the ledger and `positions` disagreeing — `{symbol: {attributed,
    positions}}`). The rest carry none, and `()` is the right answer for them rather
    than an invented placeholder.

    Done HERE, and deliberately not in `status.py`: this module is the only one that
    knows these shapes, and pushing the branching downstream would make the status seam
    a second reader of `store.py`'s internals. Past this point every field is a plain
    type and the seam copies without interpreting.

    Tolerant by construction — an unrecognized shape yields no detail rather than
    raising. A `/status` read must not be able to fail on a record that is, by
    definition, already malformed.
    """
    outstanding = entry.get("outstanding")
    if isinstance(outstanding, dict):
        symbols = tuple(str(k) for k in sorted(outstanding))
        return symbols, tuple(
            (str(k), f"{outstanding[k]} still held") for k in sorted(outstanding)
        )
    divergent = entry.get("divergent")
    if isinstance(divergent, dict):
        symbols = tuple(str(k) for k in sorted(divergent))
        detail = []
        for key in sorted(divergent):
            counts = divergent[key]
            if isinstance(counts, dict):
                detail.append((
                    str(key),
                    f"ledger {counts.get('attributed')} / positions {counts.get('positions')}",
                ))
            else:
                detail.append((str(key), str(counts)))
        return symbols, tuple(detail)
    return (), ()


@dataclass(frozen=True)
class UnwatchablePosition:
    """One thing the ledger holds that the watchdog could not turn into a spread.

    The live read behind `/status`'s unwatched-positions row, and the WORST state this
    module can report — strictly worse than `ImpairedPosition` or `SpentExitAttempt`,
    which is why it is a third type rather than a severity on either.

    `check_once` iterates `self._scan.spreads`. Anything in `unattributed` or
    `malformed` is by construction NOT in that tuple, so such a position gets:

      - no `evaluate_triggers`, ever — no credit stop, no underlying backstop;
      - no mark, so nothing can say what it is worth;
      - **no `FLATTEN_BEFORE_CLOSE`**, because the flatten is one more reason inside
        the same loop. On 0DTE that means it rides to settlement.

    And it still consumes a concurrency slot: `RiskSnapshot.open_spread_equivalent`
    counts the broker's LEGS, so the system correctly degrades toward not trading —
    with, until now, nothing anywhere saying why the slot went.

    It pages Tier 1 once per episode signature (`_report_unwatchable`) and, before
    2026-09-02, appeared on no surface between pages. Same family as
    `daily_loss.breach` read off a log row and a spent exit rendering as an ordinary
    open position: a page that reads calm for a position in the worst state the system
    can put one in.

    Built from `_scan` plus the episode state `_report_unwatchable` already keeps, so
    the row and the page cannot disagree about either the age or the severity.
    """

    #: `"unattributed"` (a `positions` row no filled opening order explains) or
    #: `"malformed"` (a ledger order holding a position that is not a well-formed
    #: 2-leg credit vertical). Two causes, one consequence.
    kind: str
    #: What is wrong, in the words `store.py` wrote. This IS the operator's diagnosis,
    #: so it is carried verbatim and never truncated.
    problem: str
    #: OCC symbols involved. One for an `unattributed` row; zero or more for a
    #: `malformed` one, depending on which shape it is.
    symbols: tuple[str, ...]
    #: `(symbol, one-line explanation)` for the shapes that carry per-symbol numbers —
    #: `outstanding` (a lopsided remainder) and `divergent` (the ledger and `positions`
    #: disagreeing). Flattened HERE because this module is the only one that knows the
    #: shape of a `malformed` dict; the status seam past this point copies fields and
    #: formats nothing. Same normalization `impaired_positions()` already does when it
    #: stringifies `TriggerReason`.
    detail: tuple[tuple[str, str], ...]
    candidate_id: str | None
    external_identifier: str | None
    broker_order_id: int | None
    #: The EPISODE's age (`_unwatchable_since`), shared by every row of one reading —
    #: the signature is what defines an episode, so the duration describes the whole
    #: condition rather than any one entry. "Unexplained right now" and "unexplained
    #: for 20 minutes" are different signals, exactly as on `ImpairedPosition`.
    unwatchable_for_s: float
    #: Inside `watchdog.book_settle_s` with NO unattributed symbol — the ordinary
    #: mid-fill window, where the two legs of one multi-leg order book ~0.25s apart and
    #: a spread legitimately has one applied leg. `_report_unwatchable` suppresses its
    #: page for exactly this, and the row is styled amber rather than red for exactly
    #: this. **An `unattributed` symbol is never settling**, at any age: there is no
    #: pending second leg that would resolve it, so waiting only delays the answer.
    settling: bool


@dataclass(frozen=True)
class _Impairment:
    """Internal: the unevaluable reason set and when it started. `since` is on the
    watchdog's own monotonic clock, so only DURATIONS ever leave this module."""

    reasons: frozenset[TriggerReason]
    candidate_id: str
    since: float


@dataclass(frozen=True)
class _SpentMark:
    """Internal: the last evaluation of a position whose exit attempt is spent.

    Exists because such a position is now EVALUATED on every tick but never acted
    on (2026-08-28), and the mark that evaluation produces is the single most useful
    thing an operator can be told about it: the stop fired at some price, and what
    matters an hour later is where it is NOW. Held here so `/status` and the page
    report the same numbers from the same computation, the rule `_Impairment`
    already follows — re-deriving it in the status reader would be a second
    computation free to disagree with the trail.

    Every field is `float | None` for the same reason `TriggerEvaluation`'s are:
    a leg that cannot be priced yields no number, and a zero would be a lie.
    """

    cost_to_close: float | None
    credit_stop: float | None
    spot: float | None
    #: Whether the trigger was STILL firing at this evaluation. The one field that
    #: can turn green: a credit stop that fired at 2x and has come back inside its
    #: threshold is a materially different situation from one still through it, and
    #: nothing else on the page distinguishes them.
    still_triggered: bool


@dataclass(frozen=True)
class _SpentAttempt:
    """Internal: the outcome that spent a position's one exit attempt, and when.

    `since` is on the watchdog's own monotonic clock (only DURATIONS leave this
    module) and is THE answer to "how long has this position had no automated path
    to close" — for the page and for `/status` alike, which is why both read it
    rather than the `_Escalation` episode. The episode is a CADENCE, and
    `_page_if_spent_and_still_held` re-arms it defensively when a spread flickers
    out of the ledger and back; that re-arm must govern when the next page fires
    and must not silently restart the duration it reports.

    **`since` moves for exactly one reason, and only since 2026-08-28**: the flatten
    allowance genuinely spends a SECOND attempt past the day's mark, so `_run_exit`
    overwrites this with that attempt's outcome and instant. That is the truthful
    reading — the operator's "how long has this had no automated path to close" now
    dates from the newer attempt — and it is deliberately NOT the defensive re-arm
    the paragraph above forbids, which invents nothing and must never move `since`.
    """

    outcome: ExitOutcome
    since: float
    #: The broker's verbatim refusal, when the attempt was spent by one (2026-09-02).
    #: Carried on the ATTEMPT rather than re-derived, because the report that had it
    #: is gone by the time the repeating page fires — and that page, not the one-shot
    #: at `_record_exit`, is the one an operator is most likely to actually read: it
    #: repeats, so it survives a missed or throttled notification. See
    #: `_alert_reject_reason` for the rule both obey.
    reject_reason: str | None = None
    #: The broker's account of a submit it REFUSED, when that is what spent the
    #: attempt (2026-09-04). Carried here for the same reason as the field above: the
    #: report is gone by the time the repeating page fires, and on the exit path this
    #: is reached only after `_EXIT_SEND_ATTEMPTS` sends have each been answered with
    #: an absence — so it is precisely the case where "no order exists" three times
    #: running has an explanation the operator has never been shown.
    mutation_error: str | None = None
    #: The exception that spent the attempt, when a CRASH is what spent it
    #: (2026-09-10). A third sentence with the same contract as the two above and
    #: disjoint from both — `reject_reason` is an order the broker accepted then
    #: rejected, `mutation_error` one it would not accept at all, and a crash is
    #: neither — overloading either would break the rule that NOTHING decides on
    #: these fields. `None`-not-`""`, same discipline.
    crash_error: str | None = None
    #: The ORIGIN's trigger reasons — why the close that filled was sent
    #: (2026-09-24). The stub continuation starts from THESE, not from a
    #: re-evaluation alone: a flatten-origin stub re-closes on the exit ladder
    #: even at 15:35 with no stop condition true, because the flatten's own
    #: decision must not be discarded. What IS added is any EMERGENCY reason
    #: firing now (`_stub_handoff_reasons`), so a profit-origin stub whose stop is
    #: true closes on the exit ladder rather than bidding one tick. Empty only
    #: where the origin is unknowable (a crash-spent attempt), and an empty set
    #: refuses the continuation — an origin-less close has no ladder to walk.
    reasons: frozenset[TriggerReason] = frozenset()
    #: How many contracts the ledger held when the close that spent this attempt
    #: was HANDED OFF (2026-09-24). The stub continuation's evidence that a close
    #: has BOOKED: the scan holding FEWER than this means some of the close is on
    #: the ledger and the rest is a stub; holding the SAME number means nothing has
    #: booked yet — a full close whose fills are still in flight looks exactly like
    #: that, and must not read as a stub. None where no handoff sized it (a crash
    #: spend), which refuses the continuation.
    contracts: int | None = None


@dataclass(frozen=True)
class _ProfitSpent:
    """Internal: why a position's PROFIT TARGET will send nothing further. (2026-09-23)

    The profit take's `_SpentAttempt`, kept deliberately apart from it: the whole
    design turns on a take that did not fill never writing `_attempted`, and a shared
    type is one refactor from a shared dict. `since` is on the watchdog's monotonic
    clock, so only durations leave this module.
    """

    outcome: ExitOutcome
    since: float
    reason: str | None
    #: See `RetiredProfitTarget.order_may_be_working`.
    order_may_be_working: bool


@dataclass
class _Deferral:
    """Internal: one position's flatten-deferral episode for the day. (2026-09-25)

    Mutable, written only by `_note_deferral`. `first_at` is on the watchdog's own
    monotonic clock, so only durations leave this module. The `min_*`, `touched` and
    `fired_after` fields are the OUTCOME the calibration is checked against: the model
    predicts a barrier touch at most `premium / (k·W)` per deferral, and
    `watchdog.flatten_deferral_outcome` is where that is counted.
    """

    candidate_id: str
    first_at: float
    first: DeferralDecision
    last: DeferralDecision
    deferring: bool
    min_z: float | None = None
    min_distance: float | None = None
    touched: bool = False
    fired_after: set[str] = field(default_factory=set)
    undeferrals: int = 0
    #: Every release REASON already written at Tier 1 and paged for this position
    #: (2026-09-25 review). A repeat of one is Tier 2: a position hovering at the
    #: boundary on a book that cannot be built re-defers after each `NOT_ATTEMPTED`,
    #: and without this each crossing was a Tier 1 row and a page. Bounded by the
    #: number of distinct reasons, and a NEW reason is always loud.
    released_for: set[str] = field(default_factory=set)
    outcome_written: bool = False

    def observe(self, decision: DeferralDecision, true_reasons: Iterable[TriggerReason]) -> None:
        if decision.z is not None and (self.min_z is None or decision.z < self.min_z):
            self.min_z = decision.z
        if decision.distance_points is not None:
            if self.min_distance is None or decision.distance_points < self.min_distance:
                self.min_distance = decision.distance_points
            if decision.distance_points <= 0.0:
                self.touched = True
        # The PRICE triggers only. The flatten is true on every tick this is called
        # for, so counting it made the outcome's list non-empty by construction.
        self.fired_after.update(
            str(r) for r in true_reasons if r in _DEFERRAL_OUTCOME_REASONS
        )


#: The emergency triggers the deferral outcome counts: every one but the flatten
#: itself, which is true throughout the episode being measured. (2026-09-25 review)
_DEFERRAL_OUTCOME_REASONS = _EMERGENCY_TRIGGER_REASONS - {
    TriggerReason.FLATTEN_BEFORE_CLOSE
}


@dataclass(frozen=True)
class _WatchdogFailure:
    """Internal: the watchdog's own most recent failure. (2026-09-10)

    Written only by `_record_failure`, read by `WatchdogHealth` and the halt/page
    builders. `at` is on the watchdog's own monotonic clock, so only DURATIONS ever
    leave this module. Deliberately NOT an exception: the exception object pins a
    traceback and invites re-raising, and what every reader wants is the sentence,
    the type, the instant and — where the failure was scoped to one position — the
    identifier.
    """

    #: "refresh" | "position" | "check" | "exit" — which frame failed.
    where: str
    error_type: str
    error: str
    at: float
    #: The position, when the failure was scoped to one. None for a pass-wide frame.
    external_identifier: str | None


class PositionWatchdog:
    """Watches open positions continuously and hands off exits. A separate task.

    Independent of `SignalLoop` by construction: it shares no state with it, is not
    driven by it, runs on its own cadence, and its `start()`/`stop()` are its own.
    """

    def __init__(
        self,
        *,
        spreads: OpenSpreadSource,
        quotes: LegQuoteSource,
        executor: ExitExecutor,
        audit: AuditLog,
        config: Config,
        halt: HaltFn,
        alert: AlertFn | None = None,
        stop: asyncio.Event | None = None,
        clock: ClockFn | None = None,
        trading_date: TradingDateFn | None = None,
        now_et: NowEtFn | None = None,
        broker_position_count: Callable[[], int | None] | None = None,
        ledger_state: Callable[[], Awaitable[LedgerState]] | None = None,
        confirm_broker_position_count: Callable[[], Awaitable[int | None]] | None = None,
        live_orders: Callable[[], Awaitable[LiveOrders]] | None = None,
        residuals: ResidualOrderRecheck | None = None,
        implied_vol: ImpliedVolSource | None = None,
    ) -> None:
        self._spreads = spreads
        self._quotes = quotes
        self._executor = executor
        self._executor.bind_quotes(quotes)
        # The executor writes `watchdog.exit_started`, which carries the scan's age
        # (§3.7, 2026-09-10); the reading lives here, so it crosses as a seam.
        self._executor.bind_scan_age(self._scan_age)
        self._audit = audit
        self._config = config
        self._wd = config.watchdog
        # REQUIRED, not optional. A watchdog whose failure posture is "log it" is
        # the failure this chunk exists to prevent, so there is no constructor that
        # produces one — see the module docstring on exception policy.
        if halt is None:  # pragma: no cover - defensive
            raise ValueError("PositionWatchdog requires a halt seam")
        self._halt = halt
        self._alert = alert or log_alert
        self._stop = stop or asyncio.Event()
        self._clock = clock or time.monotonic
        # The ET trading date the LEDGER is read as of — which legs count as
        # expired, and therefore closed (`store._leg_is_expired`). A separate seam
        # from `_clock`, which is monotonic and cannot answer a date at all. The
        # session passes its own date here exactly as it does to `ExitExecutor`,
        # for the same reason given there: a watchdog outliving an ET midnight it
        # should never see must not start judging its positions against a new day.
        self._trading_date = trading_date or et_today
        # EXCHANGE time, never host-local, and a seam so a test can move it (2026-08-28).
        # Read exactly once per `check_once` pass, against the calendar mark for
        # `_trading_date()` — so the two can never straddle a boundary and judge one
        # day's positions against another day's close, the same discipline
        # `session.session_settled` follows for the settlement mark.
        self._now_et = now_et or et_now
        # THE flatten mark's source. Built here rather than injected because — unlike
        # `session_settled`, which is a clock reading a CALLER takes and could
        # therefore be taken twice — this input is fully determined by two REQUIRED
        # config sections plus the trading-date seam above, all of which this object
        # already holds. There is no way to construct a `Config` without
        # `watchdog.flatten_before_close_s`, so the loader is the fail-closed rail
        # here and a silent default is unreachable. (Threading it from `session.py`
        # is not merely unnecessary but impossible: `session` imports this module.)
        self._calendar = MarketCalendar.from_config(config.market)
        # The two sides of the book-agreement check (`_check_broker_agreement`).
        # OPTIONAL so every pre-existing construction site keeps working, but
        # BOTH-OR-NEITHER — the same rule `Reconciler(execution=, order_store=)`
        # follows, and for the same reason: half a comparison is not a weaker check,
        # it is a check that silently never runs.
        #
        # `ledger_state` is injected rather than added to `OpenSpreadSource`, whose
        # docstring earns its keep at "one method, so it fakes trivially". Deriving
        # the count from `OpenSpreadScan` instead was considered and rejected:
        # `unattributed` holds position SYMBOLS while `malformed` holds ORDERS whose
        # legs are also in `positions`, so summing them risks double-counting the
        # exact rows the check exists to notice.
        if (broker_position_count is None) != (ledger_state is None):
            raise ValueError(
                "broker_position_count and ledger_state must be passed together — "
                "one without the other is a comparison that never runs"
            )
        self._broker_position_count = broker_position_count
        self._ledger_state = ledger_state
        # OPTIONAL and asymmetric with the pair above, deliberately. `broker_position_count`
        # reads the account CACHE, which is a stream-fed mirror and can over-report;
        # this is the authoritative REST round trip that says whether it is lying,
        # used only where a page is about to fire. Absent, the check pages exactly as
        # it did before — fail-closed, since "cannot confirm" must never suppress.
        self._confirm_broker_position_count = confirm_broker_position_count
        # OPENING orders still working at the broker, for the pre-trade gate's
        # concurrency count (Finding 6, 2026-08-28). Polled on the refresh cadence
        # below — see `_refresh_working_orders`, which deliberately CANNOT halt the
        # watchdog. Optional so every pre-existing construction site is unchanged.
        self._live_orders = live_orders
        self._working_open_spreads = 0
        # The same count split by side (2026-09-26); same refresh, same last-good
        # rule on a failed read.
        self._working_open_by_side: dict[str, int] = {}
        # The same poll's worst case, for the loss budget (2026-09-21). Same
        # last-good-value rule as the count on a failed read; "nothing working"
        # until the first answer, exactly as the count starts at 0.
        self._working_open_worst = Exposure.none_open()
        # The residual-order recheck (2026-08-29, P5). Rides the SAME `LiveOrders`
        # answer `_refresh_working_orders` fetches — zero extra broker requests, zero
        # new task lifecycle, and the answer is in hand at exactly the moment it is
        # wanted. The policy is not here on purpose: this module monitors POSITIONS,
        # and a separate task polling the same endpoint at the same cadence would buy
        # nothing. Optional, so every pre-existing construction site is unchanged.
        # → docs/decisions/2026-08-29-mid-session-cancel-recheck.md
        self._residuals = residuals
        #: (broker, ledger) pair -> the REST verdict for that pair (True = REST
        #: agreed with the LEDGER, i.e. the cache is over-reporting) and the clock
        #: reading it was MEASURED at. Keyed on the pair so a steady disagreement
        #: confirms once while any change in shape re-confirms — a cached "it's only
        #: cache drift" must never outlive the numbers it was measured against.
        #:
        #: A DICT rather than a single slot since 2026-08-21 (A4b), so a shape the
        #: book keeps returning to does not re-pay the round trip — and its blind
        #: window — on every alternation. Three things bound what a cached verdict
        #: may outlive; see `_book_confirm_lookup`.
        self._book_confirm: dict[tuple[int, int], tuple[bool, float]] = {}
        self._thresholds = TriggerThresholds.from_config(config)
        self._sustain = SustainTracker(self._wd.sustained_s, self._clock)
        self._escalation = _Escalation(self._clock)
        #: When the current book disagreement was FIRST seen, or None if the book
        #: agrees. Drives the settle allowance in `_book_allowance_elapsed`; not
        #: per-position, because the condition is about the BOOK — same reason
        #: `_BOOK_KEY` is a sentinel.
        self._book_settling_since: float | None = None
        #: Which direction that episode is in, so a FLIP starts a fresh window
        #: rather than inheriting a partly-spent one from the opposite condition.
        self._book_settling_direction: str | None = None
        #: OCC symbol -> when Argus's own closing fill took that leg to FLAT, on the
        #: watchdog's monotonic clock. THE discriminator for `held_but_unbooked`
        #: (2026-08-20): a leg Argus just closed is expected to linger in the
        #: broker's book for a few seconds, and a leg nobody closed is not.
        #: Written only by `note_fills`, pruned on read at `book_settle_s`.
        self._closed_legs: dict[str, float] = {}
        #: The current unwatchable episode: a signature of what is unexplained, when
        #: it was first seen, and whether it has paged yet. Signature-keyed so a
        #: DIFFERENT problem is a new episode that pages immediately, while the same
        #: one stays Tier 2 instead of repeating every refresh (2026-08-17).
        self._unwatchable_signature: str | None = None
        self._unwatchable_since: float | None = None
        self._unwatchable_paged: bool = False
        #: Live trigger-evaluability per watched position, for `/status`. Written
        #: ONLY by `_note_evaluability`, from the same `TriggerEvaluation` that
        #: drives the audit records — one computation, so the phone view and the
        #: audit trail cannot drift apart and disagree later.
        self._impaired: dict[str, _Impairment] = {}
        #: Positions already paged for `CreditBasis.LIMIT_FILLS_INCOHERENT`, so the
        #: page fires ONCE rather than every refresh. Deliberately NOT pruned when a
        #: spread leaves the scan, on `_attempted`'s precedent: this is a "we have
        #: told you" latch, not a window that must reset, and a spread flickering out
        #: and back is not new news. Bounded by construction — one entry per position
        #: that ever shows the state, which is single digits a day.
        self._incoherent_credit_paged: set[str] = set()

        self._scan = OpenSpreadScan()
        self._last_refresh: float | None = None
        #: When `open_spreads` last SUCCEEDED, on the same clock — the source of
        #: `watchdog.exit_started`'s `scan_age_s` (§3.7). Written only on success, so
        #: a refresh that raised keeps reporting the age of the last good scan rather
        #: than pretending a failed one counted.
        self._last_scan_ok: float | None = None
        self._refresh_requested = True
        #: Position -> the outcome that spent its one exit attempt. An exit that
        #: reached the broker is never retried: a second closing order could stack on
        #: the first and close the spread twice.
        #:
        #: The OUTCOME is kept, not just the key (2026-08-21, A2). It was a bare set
        #: on the reasoning that "everything except a clean fill alerts, so a position
        #: left here open is one a human has already been told about" — true, and not
        #: enough: that alert is ONE-SHOT, and this set is what stops `check_once`
        #: evaluating the position ever again. Told once, then never mentioned again,
        #: is the same silence `_page_if_no_exit_yet` was built to close for the two
        #: no-order conditions. `_page_if_spent_and_still_held` closes it here, and it
        #: names the outcome, because "the walk timed out" and "the cross was refused"
        #: are different next moves for whoever gets the page.
        #:
        #: The INSTANT is kept alongside it (2026-08-21, A2b), because the page was
        #: not the only thing that went quiet: the position also vanished from
        #: `/status`, and a duration is what makes that row worth reading. Measured
        #: here rather than off the `_Escalation` episode so neither reader can have
        #: it restart under them — see `_SpentAttempt`.
        #:
        #: **What lands here narrowed on 2026-08-28.** A walk that ended
        #: `NO_ORDER_EXISTS` — the send failed and the broker confirmed nothing
        #: landed — used to arrive here like any other, on reasoning about stacking a
        #: second order onto one nobody can see. There is nothing to see in that
        #: case, so it is now budgeted by `_sends` below and only lands here once the
        #: budget is out. Everything else still spends its attempt on the first try.
        self._attempted: dict[str, _SpentAttempt] = {}
        #: Position -> closing orders SENT for it whose send failed with the broker
        #: confirming nothing landed. Counts sends, not triggers: it is incremented
        #: only on `ExitOutcome.NOTHING_AT_BROKER` and capped at
        #: `_EXIT_SEND_ATTEMPTS`, after which the position goes to `_attempted` and
        #: is spent for good. Never pruned, for the same reason `_attempted` is not —
        #: a spread that flickers out of the ledger and back must not come back with
        #: a fresh budget.
        self._sends: dict[str, int] = {}
        #: Positions whose ONE flatten send has been made. (2026-08-28, P2)
        #:
        #: The allowance that stops the flatten being inert in the incident that
        #: motivated it: `_attempted` makes `check_once` never reach `_trigger`, so a
        #: spread that spent its attempts at 10:38 could not be flattened at 15:30.
        #:
        #: **It does not reopen the double-close hazard, and the reason is the guard,
        #: not the passage of time.** `_attempted` answers "does a conflicting order
        #: exist at the broker?" from in-process memory, once, and never re-asks; the
        #: exit walk runs `guard_first_submission=True`, so this send re-asks the
        #: BROKER and `DuplicateOrderGuard` BLOCKS on WORKING (the stacking hazard),
        #: on FILLED (the exit already happened), and on an order it cannot read —
        #: and RAISES if the broker cannot be reached at all, which since 2026-09-03
        #: sends NOTHING and gives the allowance back rather than halting. It allows only
        #: DEAD: terminal with nothing filled, the one state in which a second closing
        #: order is provably not a double close, and the one state `_attempted` also
        #: refused. The licence is the guard re-asking, exactly as it is for P0's
        #: `NOTHING_AT_BROKER` retry — never this trigger's own memory.
        #:
        #: ONE send, not `_EXIT_SEND_ATTEMPTS`: that budget is a burst absorbing a
        #: transient, and this is a last action at a known time that costs a real
        #: submission on a path already known to be failing.
        #:
        #: **The lifecycle, in the sentence an operator can hold: a position's one
        #: flatten override is spent when the closing order is handed to the walk, and
        #: it is given back only when the exit reports that no order was built — which
        #: is the only report that can be made without anything having been sent.**
        #:
        #: Marked at HANDOFF (in `_trigger`, after the unpriceable early return) and
        #: discarded in `_run_exit` on `ExitOutcome.NOT_ATTEMPTED` alone (2026-09-02).
        #: The two halves are one rule read from opposite ends: the handoff is the
        #: fail-closed default, so a crash mid-exit cannot hand out a second one, and
        #: the restore is on a VALUE that only construction-refusal sites can produce,
        #: so it can never describe an order that reached the broker. Until 2026-09-02
        #: only the first half existed and the field doc claimed the second: a flatten
        #: refused by `_limit_price` — the MODAL case, since a winning 0DTE spread is
        #: nearly worthless by 15:30 — spent the override on nothing, which on
        #: 2026-09-01 happened on 2 of 2 flattens and was harmless only because neither
        #: position had spent its ordinary attempt.
        #:
        #: Never pruned by the scan-forget path, for the same reason `_attempted` and
        #: `_sends` are not — and the restore does not weaken that, because it is keyed
        #: on a report from the exit path, never on a spread's presence in a scan.
        self._flatten_attempted: set[str] = set()
        #: Position -> profit-take ORDERS sent for it (2026-09-23). Counts every take
        #: that reached the walk — a `NOT_ATTEMPTED` sent nothing and costs nothing —
        #: against `_PROFIT_SEND_ATTEMPTS`. Never pruned, on `_sends`' reasoning: a
        #: spread that flickers out of the ledger and back must not return with a
        #: fresh budget.
        #:
        #: **Written by the profit path and read by nothing else. A profit take never
        #: writes `_attempted`, `_sends` or `_flatten_attempted` directly, and reaches
        #: `_attempted` only through `_spend_attempt`, only when it FILLED** — a fill
        #: under the shared close key spends the position's close on every path.
        #: `test_a_profit_take_never_touches_an_emergency_allowance` holds that on the
        #: source rather than on this sentence.
        self._profit_sends: dict[str, int] = {}
        #: Position -> why its profit target sends nothing further (2026-09-23). The
        #: design record's `_profit_spent`: written when the budget is out, when a take
        #: ends somewhere it must not be retried from, when a take CLOSED the position
        #: (its job is done, and a fill that has not booked yet must not fire a second
        #: take), and — in memory first — when a take's exit task crashes. Never
        #: pruned, for the same reason as the budget above.
        self._profit_spent: dict[str, _ProfitSpent] = {}
        #: Profit takes that closed a position, this session. For `/status`.
        self._profit_taken = 0
        #: Position -> the monotonic instant before which its profit target stands
        #: down, set by a take that sent nothing (`_PROFIT_REFUSAL_BACKOFF_S`). Read by
        #: `_may_act_on` alone. Never pruned — one float per position that ever
        #: refused, and a stale entry is simply in the past.
        self._profit_retry_at: dict[str, float] = {}
        #: Position -> its last mark, for positions whose attempt is spent. Written
        #: ONLY by `_note_spent_mark`, from the same `TriggerEvaluation` that drives
        #: the page and the telemetry — one computation, three readers.
        self._spent_marks: dict[str, _SpentMark] = {}
        #: Positions whose ONE stub-close continuation has been spent (2026-09-24).
        #: The flatten-override precedent made durable: spent at the HANDOFF, in
        #: `_maybe_stub_close`, AFTER the Tier 1 `watchdog.stub_close` record that
        #: is its durable form — so a crash between record and handoff loses the
        #: allowance and keeps paging, the fail-closed direction. Seeded on boot
        #: from today's `watchdog.stub_close` rows by `rehydrate_stub_close`, so a
        #: crash loop cannot stack a SECOND allowance-backed send on top of the
        #: ordinary attempt a restart legitimately re-arms. Never pruned, on
        #: `_attempted`'s reasoning: a spread that flickers out of the ledger and
        #: back must not return with a fresh allowance. Restored in-memory ONLY by
        #: `_restore_stub_allowance`, on the two proven-no-send values.
        self._stub_spent: set[str] = set()
        #: Positions whose durable Tier 1 `watchdog.stub_close` row is WRITTEN
        #: (2026-09-24) — by this process, or found on boot. The row is the
        #: allowance's durable spend and is never un-written, so a re-handoff after
        #: a no-send restore has nothing to add to it and goes to Tier 2 instead:
        #: without this, a continuation that cannot be built wrote a fsync'd Tier 1
        #: row on every retry for the rest of the session.
        self._stub_recorded: set[str] = set()
        #: Position -> how many guard REFUSALS (`WalkOutcome.BLOCKED`, any reason)
        #: have restored its stub allowance (2026-09-24). Bounded at
        #: `_STUB_BLOCKED_RETRIES`: each refusal PROVES nothing was sent, and the
        #: ordinary causes — the partial's remainder still cancelling, a fill not
        #: yet booked — clear in seconds. The third refusal keeps the allowance
        #: spent and pages once (`watchdog.stub_close_exhausted`).
        self._stub_blocked: dict[str, int] = {}
        #: Position -> the watchdog-clock instant before which a RESTORED stub
        #: allowance may not be handed off again (2026-09-24). Set on every no-send
        #: restore to one `watchdog.refresh_interval_s` ahead, so a retry always
        #: sees a rebuilt watch list and a stub that cannot be closed yet costs one
        #: ask per interval rather than one per cadence tick. The pacing never
        #: delays an EMERGENCY: a handoff whose reasons gain an emergency reason
        #: the last one lacked goes at once (`_stub_last_reasons`), on the same
        #: rule as the profit target's stand-down.
        self._stub_retry_at: dict[str, float] = {}
        #: Position -> the reasons its last stub continuation was handed
        #: (2026-09-24). Read only by the pacing's emergency bypass.
        self._stub_last_reasons: dict[str, frozenset[TriggerReason]] = {}
        #: True when the boot read of today's durable stub spends was UNREADABLE
        #: (2026-09-24). The kill-state posture: fail closed without halting —
        #: refusing an extra send is the safe direction, unlike a loss budget —
        #: so no continuation may fire until a readable boot, and one page says
        #: why. Rendered as `disabled` on the spent row.
        self._stub_close_unreadable = False
        self._exiting: dict[str, asyncio.Task[None]] = {}
        self._task: asyncio.Task[None] | None = None
        #: The watchdog's OWN failure state (2026-09-10). `_failure` is the most
        #: recent failure EVER and deliberately survives recovery — the RECOVERED
        #: row on `/status` needs it to say what the episode was. `_failures` counts
        #: them for the process lifetime. `_pass_failures` counts THIS pass and is
        #: reset at the top of `check_once`. `_affected` holds the identifiers that
        #: failed in the CURRENT episode and is cleared on recovery. `_last_clean_pass`
        #: timestamps the last pass that completed with no failure at all, so
        #: "the rows below are as of the last clean pass" has a number behind it.
        self._failure: _WatchdogFailure | None = None
        self._failures: int = 0
        self._pass_failures: int = 0
        self._last_clean_pass: float | None = None
        self._affected: set[str] = set()
        # -- the flatten deferral (2026-09-25) --------------------------------
        #: `watchdog.flatten_deferral`, parsed. `off` computes nothing at all.
        self._deferral_mode = DeferralMode(self._wd.flatten_deferral)
        #: The decision's parameters, from config and nothing else — the pure core
        #: carries no default for any of them.
        self._deferral_params = DeferralParams(
            insurance_multiple=self._wd.flatten_defer_insurance_multiple,
            tail_z99=self._wd.flatten_defer_tail_z99,
            tail_alpha=self._wd.flatten_defer_tail_alpha,
            buffer_points=self._wd.underlying_buffer_points,
        )
        #: The ATM IV source (`ChainImpliedVol` in production). None means no IV,
        #: which refuses every deferral — the fail-closed direction.
        self._implied_vol = implied_vol
        #: The last FINITE ATM IV read since the flatten mark, and WHEN (monotonic).
        #: Read only while a position's flatten is true, so every value here postdates
        #: the mark. Carried forward only where `_deferral_vols` says — inside the
        #: estimator's own NaN regime, or while no older than a quote may be.
        self._iv_since_mark: tuple[float, float] | None = None
        #: `(et_now, spot)` sampled once per pass, for the trailing-30-minute realized
        #: vol. Bounded: an hour of 1-second passes. EMPTY after a restart.
        self._spot_path: deque[tuple[datetime, float]] = deque(maxlen=_SPOT_PATH_MAX)
        #: The ET instant of this watchdog's FIRST spot sample. Never reset: it is
        #: what makes "a restart after 15:00 disables the deferral for the rest of the
        #: day" TRUE (the intraday rollback, §4.9). `realized_vol` alone accepts 80%
        #: of its window, so without this a restart re-armed the deferral ~24 minutes
        #: later, and a restart before ~15:06 did not disable it at all.
        self._spot_watch_since: datetime | None = None
        #: The (iv, rv) pair, computed at most once per pass.
        self._pass_vols: tuple[float | None, float | None] | None = None
        #: When `get_live_orders()` last ANSWERED (condition 5 of the design's §4.5).
        #: Written only by `_refresh_working_orders` on success.
        self._live_orders_ok_at: float | None = None
        #: Position -> its deferral episode for the day. Never pruned while the
        #: session runs: the outcome row is written once, from here.
        self._deferrals: dict[str, _Deferral] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="position-watchdog")

    async def stop(self) -> None:
        """Stop watching and wait for any exit in flight to finish reporting.

        Then write the flatten deferral's outcome row for every position deferred
        today that has not had one (2026-09-25 review). This is the NORMAL writer of
        that row: the scheduler sets the session's stop AT the close, so the last
        pass runs at about 15:59:59 and the in-pass "close" branch is reached only
        when a pass happens to beat the stop — and a held-to-expiry position leaves
        the ledger after settlement, when this watchdog no longer exists.
        """
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        await self._drain_exits()
        await self._flush_deferral_outcomes()

    @property
    def open_spread_count(self) -> int:
        """Spreads the ledger currently says are open, per TRADE IDEA.

        Fed to the pre-trade gate (`RiskSnapshot.ledger_spread_count`), which takes
        the HIGHER of this and the broker-derived leg count. It exists because the
        broker cannot report overlapping verticals: chained spreads share a strike
        that nets to zero, so k of them present as 2 legs however large k is, and a
        leg-derived limit stops bounding anything.

        Refreshed on the watchdog's own `refresh_interval_s`, and immediately after
        a fill via `request_refresh` — so a freshly-opened spread counts against the
        next entry rather than up to one interval later.
        """
        return len(self._scan.spreads)

    @property
    def open_spreads_by_side(self) -> dict[str, int]:
        """`open_spread_count`, split by side, for the side-balance rule (2026-09-26).

        Per TRADE IDEA off the same scan, the side read from the identifier Argus
        minted — never from a symbol (an AST test pins that). A spread whose side
        cannot be read is counted under `"unknown"`, and the side rule assumes the
        worst side for it: counting it on BOTH sides, the first cut, is neutral for a
        rule on `puts − calls` (2026-09-27 review).
        """
        counts = {"put": 0, "call": 0, "unknown": 0}
        for spread in self._scan.spreads:
            counts[side_of_identifier(spread.external_identifier) or "unknown"] += 1
        return counts

    @property
    def working_open_spreads_by_side(self) -> dict[str, int]:
        """`working_open_spread_count`, split by side (2026-09-26). Same refresh."""
        return dict(self._working_open_by_side)

    @property
    def working_open_spread_count(self) -> int:
        """Trade ideas with an OPENING order still working at the broker.

        Fed to the pre-trade gate (`RiskSnapshot.working_open_spread_count`), where
        it ADDS to the position-derived count: a sent-but-unfilled order is
        exposure the position counts cannot see, and a walk's abandoned order can
        outlive the walk (Finding 6, 2026-08-28).

        Refreshed on the watchdog's own `refresh_interval_s`, and immediately after
        a fill via `request_refresh` — the same cadence as `open_spread_count`. On
        a broker-read failure the last good value stands; see
        `_refresh_working_orders`.
        """
        return self._working_open_spreads

    def open_exposure(self) -> Exposure:
        """Worst-case dollars of every spread the scan holds, per trade idea. (2026-09-21)

        Fed to the loss budget (`risk.loss_budget`) the same late-bound way
        `open_spread_count` feeds the concurrency cap: `width × 100 × contracts`, the
        cost of buying each one back at full width.

        **"Cannot say" is an answer, and it zeroes the budget.** Before the first
        successful scan there is no scan to read; an `unattributed`/`malformed`
        position (the `/status` `unmanaged` row) has no width anyone can bound; an
        imbalanced spread is partly naked. In each case no entry is sized against a
        guess. Pure — tuple and dict reads, no I/O — like the `/status` accessors.
        """
        if self._last_scan_ok is None:
            return Exposure.cannot_say("watchdog_not_scanned")
        scan = self._scan
        if not scan.fully_explained:
            return Exposure.cannot_say("unmanaged_position")
        out: dict[str, float] = {}
        for spread in scan.spreads:
            if spread.imbalanced:
                return Exposure.cannot_say("imbalanced_position")
            key = trade_idea_key(spread.external_identifier) or spread.candidate_id
            width = vertical_width(spread.short_symbol, spread.long_symbol)
            if key is None or width is None:
                return Exposure.cannot_say("unreadable_position")
            out[key] = out.get(key, 0.0) + float(
                width * CONTRACT_MULTIPLIER * spread.contracts
            )
        return Exposure(by_idea=out)

    def working_open_exposure(self) -> Exposure:
        """Worst case of OPENING orders still working, from the last good poll.

        The loss budget's companion to `working_open_spread_count`, off the SAME
        `LiveOrders` answer: `working_orders.open_working_worst_case`.
        """
        return self._working_open_worst

    def request_refresh(self) -> None:
        """Rebuild the watch list on the next cycle instead of waiting for the
        refresh interval.

        Chunk 5's wiring point: called from the fill consumer so a position becomes
        watched the moment it is booked, rather than up to
        `watchdog.refresh_interval_s` later.
        """
        self._refresh_requested = True

    def note_fills(self, applied: Iterable[AppliedFill]) -> None:
        """Remember which legs Argus's OWN closing fills just took to flat. (2026-08-20)

        The second wiring point on the fill path, alongside `request_refresh`, and it
        exists for one job: `_check_broker_agreement` compares COUNTS, and a count
        cannot say whether the leg the broker is still reporting is one Argus just
        closed or one nobody is watching. Those two need opposite answers.

        **Only a leg that went FLAT is recorded**, and only on a closing action.
        That is precisely the set that left `positions` (`store._apply_position`
        DELETEs a row that nets to zero) and therefore the only set that can make
        the ledger read LOWER than the broker's trailing book. A partial close keeps
        its row and moves no count; an opening fill moves the count the other way.

        **"Closing" is decided by the identifier Argus minted, not by the leg
        action the broker computed (2026-09-03).** `AppliedFill.action` is the
        same account-relative field `infer_direction` reads, and on a chained
        book it relabels: an OPENING fill can come back `Buy to Close` with
        `position_after == 0` (seq 420587), which used to buy this allowance and
        suppress `held_but_unbooked` on a leg nobody closed. The identifier is
        echoed verbatim, so when it parses it IS the intent; a foreign fill (no
        Argus identifier) still falls back to the action, which is all it ever
        had. The reverse disagreement — Argus's order says OPEN, the broker's
        action says CLOSE — is Tier 2 telemetry and nothing else.

        Deliberately dumb about repetition, the same way `FillConsumer` is: the
        stream re-sends an order on every status change, but `store.apply` returns
        an `AppliedFill` only for real movement, so re-recording the same instant is
        the worst this can do.
        """
        now = self._clock()
        for fill in applied:
            if fill.position_after != 0:
                continue
            parsed = parse_external_identifier(fill.external_identifier)
            closes = parsed[0] is OrderDirection.CLOSE if parsed else fill.action.closes
            if closes:
                self._closed_legs[fill.symbol] = now
            elif parsed is not None and fill.action.closes:
                # Argus's own order says OPEN, the broker's leg action says CLOSE —
                # the relabelled-entry case (seq 420587). Tier 2 only.
                self._audit.telemetry(
                    "watchdog.note_fills_disagreement",
                    {"symbol": fill.symbol, "external_identifier": fill.external_identifier,
                     "broker_order_id": fill.broker_order_id, "action": str(fill.action)},
                )

    @property
    def last_failure(self) -> _WatchdogFailure | None:
        """The most recent failure, if any. Read by `/status` and the tests.

        Renamed from `fatal` (2026-09-10): a property named `fatal` on an object
        that survives its failures is a name that lies.
        """
        return self._failure

    @property
    def watching(self) -> tuple[OpenSpread, ...]:
        return self._scan.spreads

    def impaired_positions(self) -> tuple[ImpairedPosition, ...]:
        """Positions with at least one unevaluable trigger, right now. For `/status`.

        A live read, not a reconstruction: `_note_evaluability` records this from the
        very `TriggerEvaluation` that produced `watchdog.check`'s `unknown_reasons`,
        so there is no second computation to drift from the audit trail.

        Safe to call from another task on the same loop — a plain dict read, no I/O,
        no lock. Empty means every watched position is fully evaluable (or nothing is
        held), which is the steady state.
        """
        now = self._clock()
        return tuple(
            ImpairedPosition(
                external_identifier=key,
                candidate_id=imp.candidate_id,
                unknown_reasons=tuple(sorted(str(r) for r in imp.reasons)),
                # The SAME set `TriggerEvaluation.blind` is measured against (the
                # stops that can fire under this config, 2026-09-27 review) — the
                # two must answer identically. See `_STOP_TRIGGER_REASONS`.
                blind=imp.reasons >= self._thresholds.acting_stops,
                impaired_for_s=max(0.0, now - imp.since),
            )
            for key, imp in sorted(self._impaired.items())
        )

    def spent_exit_attempts(self) -> tuple[SpentExitAttempt, ...]:
        """Held positions whose one exit attempt is spent, right now. For `/status`.

        The companion live read to `impaired_positions()`, and it exists because
        these two are the SAME row's before and after: `check_once` pops a position
        out of `_impaired` the moment its attempt is spent, so without this the
        worst state the exit path can produce is the one state the page cannot show.

        **Driven by the SCAN, filtered by `_attempted`** — never the other way
        round. `_attempted` is never pruned (deliberately: clearing a key on
        scan-exit would hand a second attempt to a spread that flickered out of the
        ledger and back), so iterating it would report every position that ever
        ended badly today, forever, including the ones that closed cleanly seconds
        later. Iterating the scan means the row clears exactly when the ledger stops
        saying the position is held — the same condition that stops the page.

        Safe to call from another task on the same loop: dict and tuple reads, no
        I/O, no lock. Empty is the steady state and means what it says — nothing
        held has spent its attempt.
        """
        now = self._clock()
        out = []
        for spread in self._scan.spreads:
            spent = self._attempted.get(spread.external_identifier)
            if spent is None:
                continue
            mark = self._spent_marks.get(spread.external_identifier)
            working = self._working_close(spread)
            out.append(
                SpentExitAttempt(
                    external_identifier=spread.external_identifier,
                    candidate_id=spread.candidate_id,
                    contracts=spread.contracts,
                    outcome=str(spent.outcome),
                    spent_for_s=max(0.0, now - spent.since),
                    cost_to_close=None if mark is None else mark.cost_to_close,
                    # No credit stop on /status while the credit trigger is off
                    # (2026-09-27, `page_view`'s rule): the level is inert, and this
                    # row prints the mark against it.
                    credit_stop=(
                        None
                        if mark is None or not self._thresholds.credit_trigger_enabled
                        else mark.credit_stop
                    ),
                    credit_trigger_enabled=self._thresholds.credit_trigger_enabled,
                    still_triggered=None if mark is None else mark.still_triggered,
                    crash_error=spent.crash_error,
                    # The stub-close allowance (2026-09-24), from the same live
                    # objects this row is already built from — one computation,
                    # every reader, the `_SpentMark` rule.
                    stub_close=self._stub_close_state(
                        spread.external_identifier, spent
                    ),
                    # A close still working at the broker (2026-09-25), from the
                    # residual registry — the one fact that turns "nothing is closing
                    # this" into "a close is resting at this price".
                    working_close_order_id=(
                        None if working is None else working.broker_order_id
                    ),
                    working_close_limit=None if working is None else working.limit_price,
                    working_close_for_s=None if working is None else working.working_for_s,
                )
            )
        return tuple(sorted(out, key=lambda s: s.external_identifier))

    def _working_close(self, spread: OpenSpread):
        """The registry's working CLOSE for this position, or None. (2026-09-25)

        Keyed on the position's CLOSE identifier (`sibling_identifier`, the one minting
        rule), because the registry holds orders and this position is known by its
        opening one. None without a registry or for an identifier that does not parse.
        """
        if self._residuals is None:
            return None
        close_id = sibling_identifier(spread.external_identifier, OrderDirection.CLOSE)
        if close_id is None:
            return None
        return self._residuals.working_close_for(close_id)

    def profit_target_state(self) -> ProfitTargetState:
        """The profit target's switch, its closes, and the held positions it has left.
        For `/status`. (2026-09-23)

        Driven by the SCAN and filtered by `_profit_spent`, never the reverse — the
        rule `spent_exit_attempts` states, for the reason it states: the map is never
        pruned, so iterating it would turn the row into a process-lifetime tally.

        Pure, like the accessors beside it: dict, tuple and clock reads, no I/O, and
        deliberately no `try` — a swallow here would render a bug as "nothing
        retired".
        """
        now = self._clock()
        retired = []
        for spread in self._scan.spreads:
            spent = self._profit_spent.get(spread.external_identifier)
            if spent is None:
                continue
            retired.append(
                RetiredProfitTarget(
                    external_identifier=spread.external_identifier,
                    candidate_id=spread.candidate_id,
                    outcome=str(spent.outcome),
                    reason=spent.reason,
                    order_may_be_working=spent.order_may_be_working,
                    retired_for_s=max(0.0, now - spent.since),
                )
            )
        return ProfitTargetState(
            enabled=self._thresholds.profit_target_enabled,
            taken=self._profit_taken,
            retired=tuple(sorted(retired, key=lambda r: r.external_identifier)),
        )

    def unwatchable_positions(self) -> tuple[UnwatchablePosition, ...]:
        """Everything held that the scan could not explain, right now. For `/status`.

        The third live read beside `impaired_positions()` and `spent_exit_attempts()`,
        and the most serious of the three: see `UnwatchablePosition` for what such a
        position does NOT get. Read straight off `self._scan` and the episode state
        `_report_unwatchable` keeps, so the row cannot disagree with the page about the
        age or the severity, and no rule is re-implemented here.

        **Pure, and it must stay pure.** Dict, tuple and clock reads — no `await`, no
        audit write, no ledger query. That is the same contract the two accessors above
        hold, and it is why none of the three is wrapped in a `try`: a swallow here
        would render a genuine bug in this method as "nothing unmanaged", which is the
        reassuring-when-wrong failure the row exists to remove. An AST test pins the
        absence of an I/O path (`tests/test_status_unmanaged.py`).

        Order is deterministic: `unattributed` first, sorted by symbol, then `malformed`
        in scan order. Unattributed leads because it is the one that never waits.
        """
        scan = self._scan
        if scan.fully_explained:
            return ()
        now = self._clock()
        since = self._unwatchable_since
        age = 0.0 if since is None else max(0.0, now - since)
        # The SAME two conditions `_report_unwatchable` suppresses its page on, read
        # from the same fields rather than recomputed from a threshold of its own.
        settling = (
            not scan.unattributed and age < self._wd.book_settle_s
        )

        rows = [
            UnwatchablePosition(
                kind="unattributed",
                problem="held in `positions` with no filled opening order to explain it",
                symbols=(symbol,),
                detail=(),
                candidate_id=None,
                external_identifier=None,
                broker_order_id=None,
                unwatchable_for_s=age,
                # Never, at any age. There is no second leg pending that would resolve
                # this one, so an allowance would only delay the answer.
                settling=False,
            )
            for symbol in scan.unattributed
        ]
        for entry in scan.malformed:
            symbols, detail = _malformed_detail(entry)
            rows.append(
                UnwatchablePosition(
                    kind="malformed",
                    problem=str(entry.get("problem", "unspecified")),
                    symbols=symbols,
                    detail=detail,
                    candidate_id=_opt_str(entry.get("candidate_id")),
                    external_identifier=_opt_str(entry.get("external_identifier")),
                    broker_order_id=_opt_int(entry.get("broker_order_id")),
                    unwatchable_for_s=age,
                    settling=settling,
                )
            )
        return tuple(rows)

    def _note_spent_mark(self, key: str, evaluation: TriggerEvaluation) -> None:
        """Update the live mark for a spent position. THE only writer of `_spent_marks`.

        Unconditional overwrite, unlike `_note_evaluability`'s episode tracking:
        there is no duration to preserve here, only a latest reading. An evaluation
        that priced nothing overwrites one that did, and that is correct — "we could
        not price it this tick" is the truth, and a retained older number would be
        the same frozen-reading lie the `_impaired` pop was written to avoid.
        """
        self._spent_marks[key] = _SpentMark(
            cost_to_close=evaluation.cost_to_close,
            credit_stop=evaluation.credit_stop,
            spot=evaluation.spot,
            # EMERGENCY reasons only (2026-09-23). This field answers "is the stop
            # still firing?", and a spent position that has since come all the way
            # back to one tick is the opposite of that — reading the profit target
            # here would turn the one field on the page that can go green red.
            still_triggered=bool(evaluation.true_reasons & _EMERGENCY_TRIGGER_REASONS),
        )

    def _note_evaluability(self, spread: OpenSpread, evaluation: TriggerEvaluation) -> None:
        """Update the live evaluability map. THE only writer of `_impaired`.

        The `since` clock restarts whenever the reason SET changes, so a position
        going degraded -> blind reports the blind episode's age rather than inheriting
        the milder one's — the duration and the severity always describe the same
        state. A flap back to the same set is one continuous episode and keeps its
        start, which is what makes "still not clearing after 20 minutes" true.
        """
        key = spread.external_identifier
        if evaluation.fully_evaluable:
            self._impaired.pop(key, None)
            return
        current = self._impaired.get(key)
        if current is None or current.reasons != evaluation.unknown_reasons:
            self._impaired[key] = _Impairment(
                reasons=evaluation.unknown_reasons,
                candidate_id=spread.candidate_id,
                since=self._clock(),
            )

    # -- the loop ----------------------------------------------------------

    async def run(self) -> None:
        """Watch until stopped. A watchdog exception DEGRADES; it does not die.

        **On exception policy** (2026-09-10 — the module docstring states it in full;
        this is the operational half). An unhandled exception engages the kill switch
        at `HaltGrade.ENTRIES` and the loop keeps going: entries stop immediately and
        permanently, because a watchdog that raised cannot be trusted to stop out what
        a new entry would open, and everything else runs on because the watchdog is
        the only thing that can close or flatten what is already held. The pass-level
        catch here is the BACKSTOP — `check_once` isolates the refresh and each held
        position, so reaching it means the pass frame itself failed (`_seconds_to_flatten`,
        the iteration).

        `AuditWriteError` is the one exclusion and keeps the old posture verbatim
        (§3.5): by the time it reaches this module, `on_failure` has already halted
        the process `ALL` and set this very stop event, and there is nothing left for
        a watchdog to protect.

        `BaseException` that is not an `Exception` — SystemExit, KeyboardInterrupt,
        GeneratorExit — is deliberately NOT halted: the interpreter is leaving, the
        kill switch is durable since 2026-09-01, and tripping it on the way out would
        cost a human a `POST /resume` on the next boot for a clean shutdown.
        """
        while not self._stop.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except AuditWriteError as exc:
                # THE exclusion (§3.5). The recorder is dead process-wide,
                # `on_failure` has already halted ALL and set this very event, and
                # there is nothing left for a watchdog to protect. Today's path,
                # verbatim.
                await self._halt_on_exception(exc, where="check")
                return
            except Exception as exc:  # noqa: BLE001 - the backstop; see docstring
                await self._degrade("check", exc)
            except BaseException as exc:  # noqa: BLE001 - the interpreter is leaving
                logger.critical(
                    "position watchdog: %r — not a halt", exc, exc_info=exc
                )
                self._notify(
                    "watchdog_fatal",
                    {"where": "check", "error_type": type(exc).__name__},
                )
                raise
            await self._settle_pass()
            await self._interruptible_sleep(self._wd.cadence_s)

    async def check_once(self) -> None:
        """One evaluation pass over every held position.

        **Evaluating and ACTING are separate, and only the second one can be spent**
        (2026-08-28). Until then a position in `_attempted` was skipped whole — no
        `evaluate_triggers`, no mark, nothing — which on 2026-08-28 left a triggered
        0DTE spread unpriced for 76 minutes while the underlying moved eight points
        back in its favour. No Argus surface could answer what it was worth.

        The two concerns the old skip conflated:

        - **Do not send another order.** Correct, and still enforced — `_attempted`
          means `_trigger` is never reached, so no exit task is ever started.
        - **Do not price it any more.** Justified by nothing. A monitor must not be
          disabled by the failure of an action it requested; marking a held position
          is a reporting obligation independent of the ability to trade out of it.

        An exit IN FLIGHT is still skipped whole, and that has not changed: it is
        being handled and will report, and evaluating underneath a running walk would
        put two writers on the same escalation slots.

        **And a spent position is no longer permanently un-actionable either**
        (2026-08-28, P2). Past the day's flatten mark it falls through to `_trigger`
        exactly once — see `_flatten_may_act`. Before the mark, and after that one
        send, the one-attempt rule is untouched.

        **A failure is scoped to its frame** (2026-09-10). The refresh has its own
        catch because `_last_refresh` is advanced BEFORE the await, so a failed
        refresh costs freshness for one interval, not the evaluation pass; each
        position has its own catch so one poisonous spread cannot blind its sibling
        — `_scan.spreads` ordering is stable, so a pass-level catch alone would fail
        the same first position on every tick forever, which is the 2026-09-03 harm
        reproduced by a different route. A caught failure spends NOTHING: no order
        was sent, so the position keeps both allowances and the trigger stays
        latched, the same rule `NOT_ATTEMPTED` follows.
        """
        # THIS pass starts here: `_pass_failures` is reset where the pass begins so
        # a directly-driven `check_once` (the tests, `run()`) counts identically.
        self._pass_failures = 0
        self._pass_vols = None
        try:
            try:
                await self._refresh_if_due()
            except asyncio.CancelledError:
                raise
            except AuditWriteError:
                raise
            except Exception as exc:  # noqa: BLE001 - costs freshness, not the pass
                await self._degrade("refresh", exc)
            # ONE reading per pass, shared by every position: two positions evaluated
            # in the same tick must not be able to land on opposite sides of the mark.
            seconds_to_flatten = self._seconds_to_flatten()
            self._sample_spot()
            for spread in self._scan.spreads:
                try:
                    await self._check_one(spread, seconds_to_flatten)
                except asyncio.CancelledError:
                    raise
                except AuditWriteError:
                    raise
                except Exception as exc:  # noqa: BLE001 - per-position isolation
                    await self._degrade("position", exc, spread=spread)
            if (
                self._deferrals
                and seconds_to_flatten <= -self._wd.flatten_before_close_s
            ):
                # The close has come. Every position deferred today gets its outcome
                # row now, once — the reliability dataset the calibration is checked
                # against (§4.7). A position that left the ledger earlier already
                # got its row in `_refresh_if_due`. Reached only when a pass beats
                # the session's stop, which the scheduler sets AT the close — so the
                # usual writer is `stop()` (`ended: session_end`), not this.
                for key in list(self._deferrals):
                    await self._write_deferral_outcome(key, ended="close")
        finally:
            # In a `finally` since 2026-09-10 (§2.5b), covering the WHOLE pass — the
            # design's first cut wrapped the evaluation loop alone, which left a
            # refresh-abort skipping the reap exactly as §2.5b describes. A pass that
            # aborts anywhere above this leaves a FINISHED exit task in `_exiting`,
            # and `check_once` skips any key in `_exiting` — so the position is
            # dropped from the watch list permanently and silently, with `_impaired`
            # popped every tick so even the evaluability row goes quiet. Synchronous,
            # touches no I/O, and cannot itself fail.
            self._reap_exits()

    async def _check_one(self, spread: OpenSpread, seconds_to_flatten: float) -> None:
        """One held position's evaluation. The `check_once` loop body, extracted
        verbatim (2026-09-10) so the per-position catch can wrap exactly one position.

        Nothing here is new; the `continue`s of the loop body became `return`s.
        """
        key = spread.external_identifier
        if key in self._exiting:
            # Not evaluated this tick, so any impairment recorded for it is now
            # a frozen reading of a position nobody is re-checking. Reporting
            # that as live degradation would be a lie that only gets older.
            self._impaired.pop(key, None)
            return
        evaluation = evaluate_triggers(
            spread,
            self._quotes,
            self._thresholds,
            seconds_to_flatten=seconds_to_flatten,
        )
        await self._report_evaluability(spread, evaluation)
        sustained = self._sustain.observe(
            key,
            true_reasons=evaluation.true_reasons,
            unknown_reasons=evaluation.unknown_reasons,
        )
        fired = self._may_act_on(key, sustained)
        # The flatten deferral (2026-09-25): only ever REMOVES `FLATTEN_BEFORE_CLOSE`,
        # and never for a position whose exit attempt is spent — so the flatten's
        # one-shot override and the stub continuation below are untouched by it.
        deferral: DeferralDecision | None = None
        withheld = False
        if key not in self._attempted:
            before = fired
            fired, deferral = await self._apply_flatten_deferral(
                spread, evaluation, fired, seconds_to_flatten
            )
            withheld = fired != before
        if (spent := self._attempted.get(key)) is not None:
            # Evaluated, recorded, reported — and never acted on. The position
            # has no exit attempt left, so the ledger still saying it is held
            # means the exit did not achieve what it was for, and that needs a
            # bound on how long it can go unsaid. It now goes out WITH the mark
            # the evaluation above just produced.
            self._note_spent_mark(key, evaluation)
            self._audit.telemetry(
                "watchdog.check_spent",
                {
                    "external_identifier": key,
                    "candidate_id": spread.candidate_id,
                    "contracts": spread.contracts,
                    **spread.credit_payload(),
                    "exit_outcome": str(spent.outcome),
                    **evaluation.to_payload(),
                },
            )
            await self._page_if_spent_and_still_held(spread, spent)
            # The stub continuation, BEFORE the flatten check (2026-09-24): a
            # partial close's stub gets its ONE further close seconds after the
            # partial, long before 15:30 — and if the continuation is spent or
            # refuses, the fall-through to the flatten's own allowance below is
            # exactly the backstop the design leaves behind it. Past the flatten
            # mark it YIELDS to that allowance while it is unspent (see
            # `_maybe_stub_close`), so the one trigger nothing can blind is never
            # pre-empted by a continuation that keeps coming back unsent.
            if await self._maybe_stub_close(spread, spent, evaluation, fired):
                return
            if not await self._flatten_may_act(spread, spent, fired):
                return
            # Falls through to `_trigger`: the flatten mark has passed and this
            # position has its one allowance left.
        elif not fired:
            # A tick the flatten deferral WITHHELD is not a falling edge (2026-09-25
            # review): the flatten's CONDITION is still true, only the action is held
            # back — the `_PROFIT_TRIGGERED` rule just below. Falling here re-opened the
            # episode on every release, so a position hovering at the boundary on a
            # book that cannot be built wrote a Tier 1 `watchdog.triggered` and paged
            # `watchdog_triggered` on every crossing. A position deferred since the
            # mark has no episode open, so its first release still pages.
            if not withheld:
                self._escalation.falling(key, _TRIGGERED)
            # The profit EPISODE ends when the CONDITION does, not when the take is
            # merely held back (a refusal's stand-down, a retired budget). Falling on
            # `fired` would re-open it — one more Tier 1 `watchdog.triggered` — on
            # every stand-down cycle of a broker outage, for one unchanging fact.
            if TriggerReason.PROFIT_TARGET not in sustained:
                self._escalation.falling(key, _PROFIT_TRIGGERED)
            # The routine case by volume, and Tier 2 for exactly that reason: a
            # no-action check carries no crash-survival requirement, and at this
            # cadence these would otherwise drown the Tier 1 trail.
            self._audit.telemetry(
                "watchdog.check",
                {
                    "external_identifier": key,
                    "candidate_id": spread.candidate_id,
                    "contracts": spread.contracts,
                    **spread.credit_payload(),
                    **evaluation.to_payload(),
                    "sustaining": {
                        str(r): self._sustain.held_s(key, r)
                        for r in evaluation.true_reasons
                    },
                    # Present only on a tick the deferral decided on, so every
                    # pre-2026-09-25 row keeps its exact shape.
                    **_deferral_field(deferral),
                },
            )
            return
        await self._trigger(spread, evaluation, fired, deferral=deferral)

    def _may_act_on(
        self, key: str, fired: frozenset[TriggerReason]
    ) -> frozenset[TriggerReason]:
        """The sustained reasons this position may ACT on. (2026-09-23)

        Pure, and it only ever REMOVES the profit target — every emergency reason
        passes through untouched, so nothing here can delay or suppress a stop, the
        backstop or the flatten. The profit target survives only when ALL of:

        - **nothing else fired.** The other triggers keep precedence: a set holding an
          emergency reason is an emergency exit, with its cross and its one attempt,
          and the profit target must never be the reason a spread is closed the
          gentle way when it should be closed the hard way (or held when it should be
          closed at all);
        - **the switch is on** (`watchdog.profit_target_enabled`). Off, the trigger is
          still evaluated, recorded and sustained — `watchdog.check` carries it — and
          nothing is sent;
        - **the emergency exit is unspent.** §11.4's option B precondition, kept: a
          spent position's only remaining action is the flatten's one-shot override,
          and a profit take is not a licence to send around the one-attempt rule;
        - **its own budget is not retired** (`_profit_spent`);
        - **it is not standing down after a refusal** (`_profit_retry_at`, the
          2026-09-23 review): a take that sent nothing waits
          `_PROFIT_REFUSAL_BACKOFF_S` before the next, so a broker that cannot be
          asked does not hold the position in `_exiting` — un-evaluated — tick after
          tick.

        Removing it from `fired` rather than branching later is what makes "off" mean
        OFF: every downstream site — the spent branch, the routine telemetry, the
        handoff — sees exactly the set it saw before the profit target existed.
        """
        if TriggerReason.PROFIT_TARGET not in fired:
            return fired
        if (
            fired & _EMERGENCY_TRIGGER_REASONS
            or not self._thresholds.profit_target_enabled
            or key in self._attempted
            or key in self._profit_spent
            or self._clock() < self._profit_retry_at.get(key, float("-inf"))
        ):
            return fired - {TriggerReason.PROFIT_TARGET}
        return fired

    def _seconds_to_flatten(self) -> float:
        """Seconds until this trading day's flatten mark; negative once it is past.

        `MarketCalendar.flatten_mark` is the ONE place the instant is built, and it
        anchors on `close_time(d)` — so this is 15:30 ET on a normal session and
        12:30 on a half-day, with no branch here. A literal would be wrong on exactly
        the days the control matters most.

        Both readings are the watchdog's own seams and are taken together: the ET
        clock and the trading date the ledger was read as of. A mark for one day
        compared against another day's clock is the mistake `session.session_settled`
        already documents for the settlement mark.
        """
        mark = self._calendar.flatten_mark(
            self._trading_date(), self._wd.flatten_before_close_s
        )
        return (mark - self._now_et()).total_seconds()

    # -- the flatten deferral (2026-09-25) ---------------------------------------
    #
    # `FLATTEN_BEFORE_CLOSE` is insurance against the price triggers and the broker
    # failing together inside the last half hour. `flatten_deferral.decide` answers
    # whether that insurance is at least `k` times overpriced for one position; these
    # methods gather its inputs, apply its answer, and record every edge. The one rule
    # everything below keeps: **a deferral only ever REMOVES `FLATTEN_BEFORE_CLOSE`,
    # and every failure of any kind means the flatten runs exactly as before.**
    # → docs/designs/2026-09-25-deferring-a-far-otm-flatten.md §4

    def _sample_spot(self) -> None:
        """Append this pass's spot to the realized-vol path. Never raises.

        Nothing at all when the deferral is `off`, so that mode is byte-identical to
        the pre-2026-09-25 watchdog. A spot that is missing or stale (`spot()` fails
        closed to None) is simply not sampled — `realized_vol` refuses a gappy path
        on its own coverage rule, which refuses the deferral.
        """
        if self._deferral_mode is DeferralMode.OFF:
            return
        try:
            spot = self._quotes.spot()
            if spot is not None and math.isfinite(spot) and spot > 0.0:
                at = self._now_et()
                self._spot_path.append((at, spot))
                if self._spot_watch_since is None:
                    self._spot_watch_since = at
        except Exception as exc:  # noqa: BLE001 - costs the RV input, never the pass
            logger.warning("could not sample spot for the flatten deferral: %s", exc)

    def _deferral_vols(self) -> tuple[float | None, float | None]:
        """`(sigma_implied, sigma_realized)`, computed at most once per pass.

        **The IV is carried forward in exactly two cases** (2026-09-25 review; the
        first cut carried it on ANY missing reading, for the whole window):

        - **inside the estimator's own NaN regime** — `compute_regime` stops
          estimating `MIN_MINUTES_TO_SETTLEMENT` before the close BY DESIGN, so the
          last finite reading since the mark stands in there (it errs high: the
          0DTE strip reads 1.2–1.7× high in the last hour). The minutes are computed
          from the SAME `now_et` and close, with the same arithmetic, as the
          estimator's, so the two cannot disagree about which side of 10 minutes a
          pass is on;
        - **while the reading is no older than a quote may be**
          (`staleness.market_data_max_age_s`): an IV is a function of quotes, so it is
          as fresh as they are, and one missed estimate is tolerated.

        Anywhere else a missing estimate is NO reading, which refuses. An estimator
        that fails mid-window is failing on crossed or stale quotes — a fast market —
        which is exactly when a stale, lower IV would inflate `z`: the one harmful
        failure the design names (§4.9).
        """
        if self._pass_vols is not None:
            return self._pass_vols
        now_et = self._now_et()
        today = self._trading_date()
        iv: float | None = None
        if self._implied_vol is not None:
            reading = self._implied_vol.atm_iv(now_et=now_et, today=today)
            if reading is not None and math.isfinite(reading) and reading > 0.0:
                self._iv_since_mark = (reading, self._clock())
                iv = reading
            elif self._iv_since_mark is not None:
                carried, read_at = self._iv_since_mark
                if (
                    _minutes_to_close(now_et, self._calendar.close_time(today))
                    < MIN_MINUTES_TO_SETTLEMENT
                    or self._clock() - read_at
                    <= self._config.staleness.market_data_max_age_s
                ):
                    iv = carried
        rv = realized_vol(
            list(self._spot_path),
            window_start=now_et - _DEFERRAL_RV_WINDOW,
            window_end=now_et,
        )
        self._pass_vols = (iv, rv.annualized)
        return self._pass_vols

    def _deferral_ineligible(
        self,
        spread: OpenSpread,
        evaluation: TriggerEvaluation,
        fired: frozenset[TriggerReason],
    ) -> str | None:
        """The design's §4.5 SYSTEM-STATE conditions, or None when all hold.

        Every one is a plain in-memory read. The first that fails names the refusal;
        the arithmetic is not attempted behind it.
        """
        key = spread.external_identifier
        others = fired - {TriggerReason.FLATTEN_BEFORE_CLOSE}
        if others:
            # Condition 1: only the clock may be deferred. An emergency reason is an
            # emergency exit, with its cross and its one attempt.
            return "another_trigger_fired:" + ",".join(sorted(str(r) for r in others))
        # Condition 2: nothing Argus sent for it may still be working. `_attempted`
        # is excluded by the caller; `_exiting` never reaches `_check_one`.
        if key in self._attempted:
            return "exit_attempt_spent"
        if key in self._exiting:
            return "exit_in_flight"
        profit = self._profit_spent.get(key)
        if profit is not None and profit.order_may_be_working:
            return "profit_take_may_be_working"
        # Hard constraint 1, read literally (2026-09-25 review): never skip a
        # position whose exit is spent OR FAILING. A closing send that came back
        # `NOTHING_AT_BROKER` / `CROSS_UNPRICEABLE` is charged to `_sends` and leaves
        # the position out of `_attempted`, so without this an emergency exit that
        # failed earlier could be deferred once its trigger cleared. `_sends` is never
        # pruned, and a profit take that does not fill never touches it (AST-pinned),
        # so this bars exactly the positions whose EXIT has already failed at the
        # broker.
        if key in self._sends:
            return "exit_send_failed"
        # Condition 3: the backstop that remains must be able to see.
        if TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE in evaluation.unknown_reasons:
            return "underlying_trigger_blind"
        # Condition 4's precondition, and the intraday rollback (§4.9, 2026-09-25
        # review): the watchdog must have watched spot since the realized-vol window
        # before the mark opened. A restart after that instant (15:00, or 12:00 on a
        # half-day) therefore disables the deferral for the REST OF THE DAY, as
        # documented. `realized_vol`'s own coverage rule accepts 80% of its window,
        # which alone re-armed the deferral ~24 minutes after any restart.
        today = self._trading_date()
        window_opens = (
            self._calendar.flatten_mark(today, self._wd.flatten_before_close_s)
            - _DEFERRAL_RV_WINDOW
        )
        if self._spot_watch_since is None or self._spot_watch_since > window_opens:
            return "not_watching_since_window_start"
        # Condition 6, belt and braces: the loader refuses `on` unless everything the
        # caps can hold open fits the day's loss budget — the most one barrier event
        # can cost across every deferred position. The SAME predicate, never a copy.
        if not self._config.risk.open_risk_is_welded:
            return "open_risk_exceeds_daily_max_loss"
        # Condition 5: the system must be able to act. One missed poll is tolerated,
        # two are not.
        bound = 2.0 * self._wd.refresh_interval_s
        now = self._clock()
        if self._live_orders_ok_at is None or now - self._live_orders_ok_at > bound:
            return "broker_poll_stale"
        if self._last_scan_ok is None or now - self._last_scan_ok > bound:
            return "scan_stale"
        if self._escalation.active(_WATCHDOG_KEY, _WATCHDOG_FAILING) is not None:
            return "watchdog_degraded"
        return None

    def _decide_deferral(
        self,
        spread: OpenSpread,
        evaluation: TriggerEvaluation,
        fired: frozenset[TriggerReason],
        seconds_to_flatten: float,
    ) -> DeferralDecision:
        minutes_to_close = (self._wd.flatten_before_close_s + seconds_to_flatten) / 60.0
        ineligible = self._deferral_ineligible(spread, evaluation, fired)
        if ineligible is not None:
            return decide_deferral(
                DeferralInputs(
                    side=None, spot=None, short_strike=None, width=None,
                    sigma_implied=None, sigma_realized=None,
                    minutes_to_close=minutes_to_close, ineligible=ineligible,
                ),
                self._deferral_params,
            )
        # The strike and the right from the CHAIN IDENTITY, never from a leg quote —
        # `resolution.contract` is known whether or not the quote is fresh, which is
        # the whole point on a book whose legs have gone quiet.
        contract = self._quotes.resolve(spread.short_symbol).contract
        width = vertical_width(spread.short_symbol, spread.long_symbol)
        sigma_implied, sigma_realized = self._deferral_vols()
        return decide_deferral(
            DeferralInputs(
                side=None if contract is None else contract.right,
                spot=evaluation.spot,
                short_strike=None if contract is None else contract.strike,
                width=None if width is None else float(width),
                sigma_implied=sigma_implied,
                sigma_realized=sigma_realized,
                minutes_to_close=minutes_to_close,
            ),
            self._deferral_params,
        )

    async def _apply_flatten_deferral(
        self,
        spread: OpenSpread,
        evaluation: TriggerEvaluation,
        fired: frozenset[TriggerReason],
        seconds_to_flatten: float,
    ) -> tuple[frozenset[TriggerReason], DeferralDecision | None]:
        """Decide, record, and return the fired set with the flatten removed if deferred.

        **Any exception while DECIDING means "not deferred".** This is one of the very
        few swallows in this module, and it is correct here because the fail-safe
        direction IS the action: the flatten proceeds exactly as it would have. The
        records below are outside the `try`, so a dead audit log still halts.
        """
        if (
            self._deferral_mode is DeferralMode.OFF
            or TriggerReason.FLATTEN_BEFORE_CLOSE not in fired
        ):
            return fired, None
        try:
            decision = self._decide_deferral(spread, evaluation, fired, seconds_to_flatten)
        except Exception as exc:  # noqa: BLE001 - the fail-safe direction is the flatten
            logger.error(
                "flatten deferral failed for %s; flattening as usual: %s",
                spread.external_identifier, exc, exc_info=exc,
            )
            self._audit.telemetry(
                "watchdog.flatten_deferral_failed",
                {
                    "external_identifier": spread.external_identifier,
                    "candidate_id": spread.candidate_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
            )
            decision = decide_deferral(
                DeferralInputs(
                    side=None, spot=None, short_strike=None, width=None,
                    sigma_implied=None, sigma_realized=None,
                    minutes_to_close=(
                        self._wd.flatten_before_close_s + seconds_to_flatten
                    ) / 60.0,
                    ineligible=f"deferral_error:{type(exc).__name__}",
                ),
                self._deferral_params,
            )
        await self._note_deferral(spread, evaluation, decision)
        if decision.defer and self._deferral_mode is DeferralMode.ON:
            return fired - {TriggerReason.FLATTEN_BEFORE_CLOSE}, decision
        return fired, decision

    def _deferral_payload(
        self, spread: OpenSpread, decision: DeferralDecision
    ) -> dict[str, Any]:
        """The one payload builder for every deferral record AND page, so the two can
        never disagree (the 2026-09-02 hand-built-dict class). `candidate_id` at the
        top level for `notify.alert_scope` (B1)."""
        return {
            "external_identifier": spread.external_identifier,
            "candidate_id": spread.candidate_id,
            "contracts": spread.contracts,
            "mode": str(self._deferral_mode),
            "acted": self._deferral_mode is DeferralMode.ON,
            **decision.to_payload(),
        }

    async def _note_deferral(
        self,
        spread: OpenSpread,
        evaluation: TriggerEvaluation,
        decision: DeferralDecision,
    ) -> None:
        """Record the deferral's EDGES. THE only writer of `_deferrals`.

        - first deferral of the day: Tier 1 `watchdog.flatten_deferred`, paged once
          (in `on`);
        - a deferral that stops: Tier 1 `watchdog.flatten_undeferred`, paged (in
          `on`), the FIRST time each distinct reason releases it — the flatten is
          handed to the exit path on this same tick, and this is the moment an
          operator should look. A repeat of a reason already on record is Tier 2
          `watchdog.flatten_undeferred_again` and not paged (2026-09-25 review): a
          position hovering at the boundary on a book that cannot be built re-defers
          after each `NOT_ATTEMPTED`, and paging every crossing was the Tier 1 flicker
          the deferral exists to retire. A NEW reason — an emergency trigger firing
          beside the clock, the broker poll going stale — is always loud;
        - a re-deferral after that (possible only if the flatten then sent nothing):
          Tier 2.

        A position that was never deferred writes nothing here: every tick the
        deferral decides on carries the decision already, on `watchdog.check` when
        nothing fired and on `watchdog.triggered`/`watchdog.triggered_still` when the
        flatten did (`_deferral_field`). The second half was false until 2026-09-28
        (P11): `_trigger`'s payload carried no decision, so a flatten the deferral
        declined to defer had its z on no record.
        """
        key = spread.external_identifier
        state = self._deferrals.get(key)
        acting = self._deferral_mode is DeferralMode.ON
        if decision.defer:
            if state is None:
                state = _Deferral(
                    candidate_id=spread.candidate_id,
                    first_at=self._clock(),
                    first=decision,
                    last=decision,
                    deferring=True,
                )
                self._deferrals[key] = state
                state.observe(decision, evaluation.true_reasons)
                payload = self._deferral_payload(spread, decision)
                logger.warning(
                    "FLATTEN DEFERRED (%s): %s z=%s P_touch=%s k*P*W=%s <= %s",
                    self._deferral_mode, key, decision.z, decision.p_touch,
                    decision.lhs, decision.premium,
                )
                await self._audit.record("watchdog.flatten_deferred", payload)
                if acting:
                    self._notify("watchdog_flatten_deferred", payload)
                return
            state.last = decision
            state.observe(decision, evaluation.true_reasons)
            if not state.deferring:
                state.deferring = True
                self._audit.telemetry(
                    "watchdog.flatten_deferred_again",
                    self._deferral_payload(spread, decision),
                )
            return
        if state is None:
            return
        state.last = decision
        state.observe(decision, evaluation.true_reasons)
        if state.deferring:
            state.deferring = False
            state.undeferrals += 1
            payload = {
                **self._deferral_payload(spread, decision),
                "undeferrals": state.undeferrals,
            }
            if decision.reason in state.released_for:
                self._audit.telemetry("watchdog.flatten_undeferred_again", payload)
                return
            # Claimed BEFORE the write, the same order as the first deferral's state
            # above: a record that raises must not make the next crossing retry it.
            state.released_for.add(decision.reason)
            logger.warning(
                "FLATTEN RELEASED (%s): %s — %s; flattening now",
                self._deferral_mode, key, decision.reason,
            )
            await self._audit.record("watchdog.flatten_undeferred", payload)
            if acting:
                self._notify("watchdog_flatten_undeferred", payload)

    async def _write_deferral_outcome(self, key: str, *, ended: str) -> None:
        """Tier 1 `watchdog.flatten_deferral_outcome`, once per deferred position.

        The reliability dataset (§4.7): the model predicts a barrier touch at most
        `premium / (k·W)` per deferral, and this row is where a touch is counted.
        `touched` is true when a decision measured spot at or inside the barrier OR
        the underlying trigger fired after the first deferral — the two readings of
        one event.

        `ended` is `left_the_ledger` (closed before the session ended), `close` (a
        pass ran at or after the close), or `session_end` (written by `stop()` — the
        NORMAL case for a position held to the close, since the scheduler stops the
        session AT the close; `seconds_to_close` ≈ 0 says so, and a larger value is a
        SIGTERM or an aborted session).
        """
        state = self._deferrals.get(key)
        if state is None or state.outcome_written:
            return
        state.outcome_written = True
        touched = state.touched or (
            str(TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE) in state.fired_after
        )
        await self._audit.record(
            "watchdog.flatten_deferral_outcome",
            {
                "external_identifier": key,
                "candidate_id": state.candidate_id,
                "ended": ended,
                "seconds_to_close": round(
                    self._wd.flatten_before_close_s + self._seconds_to_flatten(), 3
                ),
                "mode": str(self._deferral_mode),
                "deferring_at_end": state.deferring,
                "undeferrals": state.undeferrals,
                "touched": touched,
                "min_z_after_first_deferral": payload_number(state.min_z),
                "min_distance_points": payload_number(state.min_distance),
                "emergency_triggers_after_first_deferral": sorted(state.fired_after),
                "deferred_for_s": round(max(0.0, self._clock() - state.first_at), 3),
                "first": state.first.to_payload(),
                "last": state.last.to_payload(),
            },
        )

    async def _flush_deferral_outcomes(self) -> None:
        """Write every outstanding deferral outcome as `session_end`. Never raises.

        Called by `stop()` after the watchdog task has returned and exits in flight
        have reported. It cannot raise into the session's teardown, which still has
        to stop the account and market-data streams behind it. An audit failure has
        already halted through `AuditLog.on_failure` by the time it could reach here,
        so the rule `_halt_on_exception` follows applies: the recorder may be exactly
        what failed, and nothing more can be done about a row it will not take. Each
        position is tried on its own, so one failure does not cost the others.
        """
        for key in list(self._deferrals):
            try:
                await self._write_deferral_outcome(key, ended="session_end")
            except Exception as exc:  # noqa: BLE001 - teardown; see docstring
                logger.error(
                    "could not write the flatten deferral outcome for %s: %s",
                    key, exc, exc_info=exc,
                )

    def flatten_deferral_state(self) -> FlattenDeferralState:
        """The deferral's parameters and every HELD position it has decided on. For
        `/status`. (2026-09-25)

        Driven by the SCAN and filtered by `_deferrals`, never the reverse — the rule
        `spent_exit_attempts` states. Pure: dict, tuple and clock reads, no I/O, and no
        `try`, so a bug renders as a bug rather than as "nothing deferred".
        """
        now = self._clock()
        rows = []
        for spread in self._scan.spreads:
            state = self._deferrals.get(spread.external_identifier)
            if state is None:
                continue
            last = state.last
            rows.append(
                FlattenDeferralRow(
                    external_identifier=spread.external_identifier,
                    candidate_id=spread.candidate_id,
                    deferring=state.deferring,
                    reason=last.reason,
                    z=payload_number(last.z),
                    p_touch=payload_number(last.p_touch),
                    lhs=payload_number(last.lhs),
                    premium=last.premium,
                    distance_points=payload_number(last.distance_points),
                    deferred_for_s=max(0.0, now - state.first_at),
                )
            )
        return FlattenDeferralState(
            mode=str(self._deferral_mode),
            k=self._deferral_params.insurance_multiple,
            tail_z99=self._deferral_params.tail_z99,
            tail_alpha=self._deferral_params.tail_alpha,
            rows=tuple(sorted(rows, key=lambda r: r.external_identifier)),
        )

    async def _flatten_may_act(
        self, spread: OpenSpread, spent: _SpentAttempt, fired: frozenset[TriggerReason]
    ) -> bool:
        """May a SPENT position's flatten send go out? (2026-08-28, P2)

        The narrow, time-gated exception to the one-attempt rule, and the only one.
        True requires both halves:

        - the FLATTEN is among the sustained reasons — not merely that the trigger
          exists, but that the mark has passed. Before it, a spent position is
          exactly as spent as it was, which is the whole entry window and most of the
          session;
        - and this position has not already had its one flatten send.

        **Why this is not the double-close hazard `_attempted` guards.** That rule's
        stated worst case is an order that MAY exist and CANNOT be seen — a
        best-effort `_request_cancel` leaving something at `Cancel Requested` that
        can still fill. `_attempted` answers that from in-process memory and never
        re-asks. This send goes out through `ExitExecutor.close`, whose walk runs
        `guard_first_submission=True`, so `DuplicateOrderGuard` asks the BROKER at
        the moment of the send and refuses on WORKING, on FILLED, on an order it
        cannot read, and by RAISING (which halts) if it cannot reach the broker at
        all. It permits only DEAD — terminal with nothing filled — which is the one
        state where a second closing order is provably not a double close.
        `_attempted` blocks that state too, and it is the only state this unblocks.

        Recorded Tier 1 and paged, once per position: overriding a stated
        non-negotiable, however narrowly, belongs in the trail as its own event
        rather than being inferable from a `watchdog.triggered` that looks like any
        other.

        **"Once per position" is held by `_FLATTEN_OVERRIDE`'s rising edge, and until
        2026-09-02 it was held by nothing** — the comment claimed it and the verdict
        alone delivered it, but only for a position that goes on to reach the spend.
        See the tag's own note for the measurement and for why it never falls.

        **The page is on ELIGIBILITY, not on the send, and that is deliberate.** Since
        the allowance is returned when an exit reports that no order was built, one
        override can span several handoffs — each a real attempt to build the closing
        order, at most one of which becomes one. Pinning the page to the send would
        either go silent on the ticks that refused or fire once per refused build,
        which is the storm the edge above exists to stop. What an operator needs to
        read once is that the non-negotiable is now overridable for this position; what
        happens on each tick after that is `_UNATTEMPTED`'s and `_UNPRICEABLE`'s to
        report, and they already bound it.
        """
        key = spread.external_identifier
        if TriggerReason.FLATTEN_BEFORE_CLOSE not in fired:
            return False
        if key in self._flatten_attempted:
            return False
        detail = {
            # Top level for `notify.alert_scope`'s per-position throttle (B1).
            "external_identifier": key,
            "candidate_id": spread.candidate_id,
            "contracts": spread.contracts,
            **spread.credit_payload(),
            "spent_outcome": str(spent.outcome),
            # It names the outcome that spent the attempt, so it owes the broker's
            # reason for it (2026-09-02) — and here it is load-bearing rather than
            # merely informative: this page announces a stated non-negotiable being
            # overridden, and "the previous close was refused because it had already
            # filled" is a very different thing to read on that page from "the
            # previous close was refused because the account is not entitled".
            "reject_reason": spent.reject_reason,
            "mutation_error": spent.mutation_error,
            "crash_error": spent.crash_error,
            "spent_for_s": round(max(0.0, self._clock() - spent.since), 3),
            "flatten_before_close_s": self._wd.flatten_before_close_s,
        }
        if not self._escalation.rising(key, _FLATTEN_OVERRIDE):
            # Same position, still eligible, still not closed. The verdict is
            # unchanged — it is the REPORTING that is once per position.
            self._audit.telemetry("watchdog.flatten_override_still", detail)
            return True
        logger.error(
            "FLATTEN OVERRIDES A SPENT EXIT ATTEMPT: %s ended %s %.1fs ago and the "
            "ledger still shows it open; past the flatten mark it gets ONE further "
            "closing order, through the duplicate guard",
            key, spent.outcome, detail["spent_for_s"],
        )
        await self._audit.record("watchdog.flatten_override", detail)
        self._notify("watchdog_flatten_override", detail)
        return True

    async def _maybe_stub_close(
        self,
        spread: OpenSpread,
        spent: _SpentAttempt,
        evaluation: TriggerEvaluation,
        fired: frozenset[TriggerReason],
    ) -> bool:
        """A partial close's stub gets ONE further close. (2026-09-24)

        **Detection needs evidence that the close BOOKED.** A stub is a spent
        FILLED position the ledger still holds FEWER contracts of than the close
        was sized for (`spread.contracts < spent.contracts`): some of the close is
        on the ledger, and what is left is the remainder. Holding the SAME number
        is not a stub — it is every close, full or partial, whose fills have not
        booked yet (the walk's poll and the account stream race; a stream
        reconnect widens the gap), and treating it as one sent a doomed guarded
        ask and a false failed-exit page after ordinary full closes. It waits
        instead: if the close was full the scan drops the spread when it books,
        and if it was partial the stub appears. `filled_contracts` is deliberately
        NOT consulted: it is rung-scoped, and the record says the ledger and the
        broker are the authorities — the GUARD's quantity rule then re-proves `q`
        against the broker's own fills on every ask.

        **Past the flatten mark it YIELDS to the flatten's own allowance** while
        that is unspent: the flatten is the trigger nothing can blind, it crosses
        (capped at the width), and a continuation that keeps coming back unsent —
        a profit-origin re-take whose mark has left one tick, a mid that rounds to
        nothing — must never be what stops it being reached.

        **The ladder is the origin's, plus anything urgent now**
        (`_stub_handoff_reasons`): a flatten-origin stub re-closes on the exit
        ladder with no stop true, and a profit-origin stub whose stop IS true
        closes on the exit ladder rather than bidding one tick it will never be
        paid.

        **Retries are paced and the durable spend is written once.** A no-send
        restore (`_after_stub_close`) sets `_stub_retry_at` one
        `watchdog.refresh_interval_s` ahead — bypassed only when the handoff
        gains an EMERGENCY reason the last one lacked, so a stop that starts
        firing on a profit-origin stub is never delayed by a stand-down (the
        profit target's own rule); the Tier 1 `watchdog.stub_close` row
        goes out on the FIRST handoff only (`_stub_recorded`), before any order can
        go out — the audit-before-action rule, and the row the boot read
        rehydrates from — and every later handoff of the same allowance is Tier 2.
        Then the in-memory `_stub_spent`, then the handoff. A crash after the row
        and before the handoff loses the allowance and keeps paging — the
        fail-closed direction, bounded by §2 of the decision record. Returns True
        when the handoff happened, so `_check_one` stops before the flatten's own
        allowance.
        """
        if not self._wd.stub_close_enabled or self._stub_close_unreadable:
            return False
        if spent.outcome is not ExitOutcome.FILLED or not spent.reasons:
            return False
        if spent.contracts is None or not 1 <= spread.contracts < spent.contracts:
            # No booked evidence of a partial yet (or no handoff sized it).
            return False
        key = spread.external_identifier
        if key in self._stub_spent or key in self._exiting:
            return False
        if (
            TriggerReason.FLATTEN_BEFORE_CLOSE in fired
            and key not in self._flatten_attempted
        ):
            return False
        reasons = _stub_handoff_reasons(spent.reasons, fired)
        retry_at = self._stub_retry_at.get(key)
        newly_urgent = (reasons - {TriggerReason.PROFIT_TARGET}) - (
            self._stub_last_reasons.get(key, frozenset())
        )
        if retry_at is not None and self._clock() < retry_at and not newly_urgent:
            # Paced — unless a stop, the backstop or the flatten has fired since
            # the last handoff: an emergency is never delayed by a stand-down.
            return False
        if evaluation.market is None:
            return False
        close_id = sibling_identifier(key, OrderDirection.CLOSE)
        detail = {
            # Top level for `notify.alert_scope`'s per-position throttle (B1). The
            # POSITION's identifier, like every other watchdog record — and the key
            # the boot read rehydrates from; the close sibling rides beside it
            # because it is the key the continuation will submit under.
            "external_identifier": key,
            "candidate_id": spread.candidate_id,
            "close_identifier": close_id,
            "contracts": spread.contracts,
            "origin_contracts": spent.contracts,
            **spread.credit_payload(),
            "origin_reasons": sorted(str(r) for r in spent.reasons),
            "handoff_reasons": sorted(str(r) for r in reasons),
            "retries": self._stub_blocked.get(key, 0),
            # The mark at the moment of the send — the same numbers the spent
            # page carries, from the same evaluation, so the record and the page
            # cannot disagree about what the stub was worth. Through
            # `payload_number`: a non-finite value in a Tier 1 record is a REFUSED
            # write, and here that would surface as a watchdog exception.
            "cost_to_close": payload_number(evaluation.cost_to_close),
            "credit_stop": payload_number(evaluation.credit_stop),
            "spot": payload_number(evaluation.spot),
        }
        if key not in self._stub_recorded:
            logger.error(
                "STUB CLOSE: %s's close of %d filled and the ledger still holds %d "
                "contract(s) — ONE further close goes out (%s), through the duplicate "
                "guard's quantity rule",
                key, spent.contracts, spread.contracts, detail["handoff_reasons"],
            )
            await self._audit.record(STUB_CLOSE_CATEGORY, detail)
            self._stub_recorded.add(key)
        else:
            logger.warning(
                "stub close retry: %s holds %d contract(s); the same allowance, handed "
                "off again after a refusal that sent nothing (%s)",
                key, spread.contracts, detail["handoff_reasons"],
            )
            self._audit.telemetry("watchdog.stub_close_retry", detail)
        self._stub_spent.add(key)
        self._stub_retry_at.pop(key, None)
        self._stub_last_reasons[key] = reasons
        task = asyncio.create_task(
            self._run_exit(spread, evaluation.market, reasons, stub=True),
            name=f"watchdog-stub-close-{key}",
        )
        self._exiting[key] = task
        return True

    async def rehydrate_stub_close(self) -> None:
        """Seed `_stub_spent` from today's `watchdog.stub_close` rows. (2026-09-24)

        Called once, before `start()`, by the session that built this watchdog.
        The durable record is never un-written, so this only ever ADDS spent
        keys — a `NOT_ATTEMPTED` after the record leaves a false durable spend
        (one further close lost, pages continue, flatten backstop intact), which
        is the flatten precedent's own accepted asymmetry: the alternative
        (record after the send) re-grants on every crash between send and
        record.

        The risk package reads the rows and the grammar is applied HERE, on the
        execution side of the boundary: every key must parse as one of Argus's
        OPEN (position) identifiers. An UNREADABLE read, or a key that does not
        parse, fails toward spent without halting — the kill-state posture, not
        the loss-budget one: refusing an extra send is the safe direction. No
        continuation may fire until a readable boot, and one page says why.
        """
        if not self._wd.stub_close_enabled:
            return
        try:
            keys = await load_stub_close_spent(
                self._audit, trading_date=self._trading_date()
            )
            unparseable = sorted(
                k for k in keys
                if (parsed := parse_external_identifier(k)) is None
                or parsed[0] is not OrderDirection.OPEN
            )
            if unparseable:
                raise StubCloseRehydrationError(
                    f"today's {STUB_CLOSE_CATEGORY} rows name position identifiers "
                    f"the grammar cannot parse as opening orders: {unparseable!r}"
                )
        except StubCloseRehydrationError as exc:
            self._stub_close_unreadable = True
            detail = {
                "trading_date": self._trading_date().isoformat(),
                "error": str(exc)[:500],
            }
            logger.error(
                "STUB-CLOSE SPEND UNREADABLE: %s — no stub continuation may fire "
                "this session; positions remain paged with the flatten backstop",
                detail["error"],
            )
            # Suppressed for the same reason `_note_exit_crash` suppresses its
            # record: the recorder may be exactly what failed, and the page must
            # not depend on it.
            with contextlib.suppress(Exception):
                await self._audit.record("watchdog.stub_close_unreadable", detail)
            self._notify("watchdog_stub_close_unreadable", detail)
            return
        self._stub_spent |= keys
        # Their durable rows are what was just read.
        self._stub_recorded |= keys

    def _stub_close_state(self, key: str, spent: _SpentAttempt) -> str | None:
        """The stub-close allowance's state for one SPENT row. For `/status`.

        Pure, like the accessors beside it: dict, set and flag reads, no I/O, and
        deliberately no `try` — a swallow here would render a bug as a calm
        state on the row whose whole purpose is the opposite.
        """
        if spent.outcome is not ExitOutcome.FILLED or not spent.reasons:
            return None
        if not self._wd.stub_close_enabled or self._stub_close_unreadable:
            return "disabled"
        if key not in self._stub_spent:
            return "available"
        if self._stub_blocked.get(key, 0) >= _STUB_BLOCKED_RETRIES:
            return "exhausted"
        return "sent"

    def _restore_stub_allowance(self, key: str) -> None:
        """Give the stub's ONE further close back, in memory only. (2026-09-24)

        Called only from `_after_stub_close`, only on the two PROVEN-no-send
        values: `NOT_ATTEMPTED` (a VALUE constructed only at sites lexically
        above the walk, AST-pinned, the flatten precedent verbatim) and a walk
        that ended `BLOCKED` (returned by the walk immediately after the guard,
        before any submission — whatever the guard's reason). The next handoff is
        paced one `watchdog.refresh_interval_s` out, and still goes through the
        guard, which re-refuses for as long as its cause stands. The durable
        `watchdog.stub_close` record is NEVER un-written; `BLOCKED` restores are
        bounded by `_STUB_BLOCKED_RETRIES`, after which the allowance stays spent
        and the repeating page owns it.
        """
        self._stub_spent.discard(key)
        self._stub_retry_at[key] = self._clock() + self._wd.refresh_interval_s

    async def _check_broker_agreement(self) -> None:
        """Does the BROKER hold what the ledger says it holds? (2026-08-11)

        `OpenSpreadScan.fully_explained` already catches Argus's two OWN records
        disagreeing — a `positions` row no filled order accounts for. Neither of
        those records knows anything about the broker, so this is the missing
        third comparison, and it closes a real gap:

        `positions` is written ONLY by `FillConsumer`, i.e. only from `PlacedOrder`
        events over the `AlertStreamer`. The account REST refresh re-seeds the
        in-memory cache but NEVER that table, reconciliation runs only at process
        and session start, and the watchdog's watch list is the ledger. So an order
        that fills while the account stream is disconnected — six disconnects in one
        sandbox session on 2026-08-11 — leaves a real 0DTE position with no ledger
        row and therefore NO VIRTUAL STOP being evaluated. New entries still stop
        (the gate's concurrent limit reads the broker-derived count), and the next
        session's reconcile hard-halts on the unaccounted FILLED order, so it is
        detected — but only tomorrow, and the exit is unprotected until then.

        **This deliberately does NOT depend on whether the AlertStreamer replays
        missed events on re-subscribe**, which is unverified server behaviour. Both
        numbers already exist, so comparing them is correct whichever way that falls
        — and it catches a `FillConsumer` bug or a hand-placed fill just the same.

        Units matter and they are easy to get wrong: BOTH sides count non-flat LEG
        positions. `AccountSnapshot.open_position_count` is one per OCC symbol, and
        `LedgerState.positions` is a row count of the `positions` table, which
        DELETES a row when it goes flat. Comparing against `len(scan.spreads)` would
        have been wrong by 2x, since a vertical is two legs.

        Either direction is a disagreement worth paging on, so there is one
        condition and one escalation slot, with the direction in the payload:
        broker > ledger is the unwatched-position case; broker < ledger means the
        ledger believes it holds something the broker does not.

        A None count (unusable account snapshot) SKIPS the check rather than
        guessing. That is not a silent hole: the gate already fails closed on
        unusable account state, so nothing new is being entered, and `/status`
        surfaces the staleness directly.

        **The two directions are not symmetric, and since 2026-08-13 they are not
        treated symmetrically.** The first live-fire pass produced a
        `booked_but_absent` disagreement on EVERY entry — 0 vs 2 and 1 vs 2,
        resolving in 5.01s and 4.01s — because the ledger books an AlertStreamer
        fill before the broker's own position book reflects it. That inverts the
        concern this check was built for (the ledger lagging the broker); here it
        leads. Paging on it is one ERROR per entry for ordinary propagation lag,
        the same failure the 2026-08-05 alert storm was: a real signal at the
        wrong threshold. So `booked_but_absent` gets `watchdog.book_settle_s` to
        resolve on its own.

        That allowance is TIME-based, never tick-based: this runs from
        `_refresh_if_due`, which fires on `refresh_interval_s` OR on any
        `request_refresh()` from the fill consumer, so tick counts measure nothing.
        A suppressed episode still leaves the Tier 2 `book_disagreement_still`
        trail plus a closing `watchdog.book_settled`, so it is reconstructable —
        and those records are the measurement the threshold gets re-tuned from.

        **`held_but_unbooked` also has an allowance now, and it is NOT a timer**
        (2026-08-20). The same propagation lag runs the other way on the way OUT:
        Argus deletes the `positions` row the instant its closing fill books, and the
        broker's book trails. Measured on the first intraday stop since v0.3.5 —
        fired 1.215s after the closing fill, cleared 6.012s later. But this direction
        is the dangerous one, so it does not get a blanket window: it is suppressed
        only when Argus's OWN closing fills (`note_fills`) account for the excess
        legs. **A disagreement with no matching recent closing fill still pages on
        the first observation, unconditionally.** See `_book_allowance_elapsed`.
        → docs/decisions/2026-08-20-held-but-unbooked-after-our-own-exit.md
        """
        if self._broker_position_count is None:
            return
        broker = self._broker_position_count()
        if broker is None:
            return
        assert self._ledger_state is not None  # both-or-neither, checked at init
        ledger = (await self._ledger_state()).positions
        if broker == ledger:
            settling_since = self._book_settling_since
            settling_direction = self._book_settling_direction
            self._book_settling_since = None
            self._book_settling_direction = None
            # The episode is over: broker and ledger agree, so every verdict in
            # here describes a book that no longer exists. This is the STRONGEST
            # of the three invalidations and the only evidence-based one — see
            # `_book_confirm_lookup`.
            self._book_confirm.clear()
            if self._escalation.falling(_BOOK_KEY, _LEDGER_DISAGREES):
                await self._audit.record(
                    "watchdog.book_agrees",
                    {"broker_legs": broker, "ledger_legs": ledger},
                )
            elif settling_since is not None:
                # A disagreement that never escalated, now cleared. No Tier 1 pair
                # to close (nothing paged), but the episode must still be
                # reconstructable — and its duration is exactly what says whether
                # `book_settle_s` is sized right. The direction is carried because
                # since 2026-08-20 both of them can settle, on different evidence.
                self._audit.telemetry(
                    "watchdog.book_settled",
                    {
                        "broker_legs": broker,
                        "ledger_legs": ledger,
                        "direction": settling_direction,
                        "settled_after_s": round(self._clock() - settling_since, 3),
                    },
                )
            return
        direction = "held_but_unbooked" if broker > ledger else "booked_but_absent"
        detail = {
            "broker_legs": broker,
            "ledger_legs": ledger,
            "direction": direction,
        }
        if not self._book_allowance_elapsed(direction, detail):
            return
        if direction == "held_but_unbooked" and await self._cache_is_over_reporting(
            broker, ledger, detail
        ):
            # REST — the only authority — agrees with the ledger, so there is no
            # unwatched position and nothing to page about. Still Tier 2 every tick,
            # so the episode's persistence stays reconstructable, and the escalation
            # slot is deliberately NOT consumed: if this turns into a real
            # disagreement later, it must still be able to page.
            self._audit.telemetry(
                "watchdog.book_disagreement_still", {**detail, "cache_over_reporting": True}
            )
            return
        if self._escalation.rising(_BOOK_KEY, _LEDGER_DISAGREES):
            logger.error(
                "BOOK DISAGREEMENT: broker holds %d leg(s), ledger has %d — a held "
                "position may not be watched",
                broker, ledger,
            )
            self._alert("watchdog_book_disagreement", detail)
            await self._audit.record("watchdog.book_disagreement", detail)
        else:
            self._audit.telemetry("watchdog.book_disagreement_still", detail)

    async def _cache_is_over_reporting(
        self, broker: int, ledger: int, detail: dict[str, Any]
    ) -> bool:
        """Is the disagreement the account CACHE's fault rather than a real position?

        `held_but_unbooked` pages on sight and has no settle allowance, because a
        position held with no ledger row has no virtual stop being evaluated. That
        posture is right, but it was reading a number that is not what its own
        docstring claimed: `broker_position_count` comes off `AccountStateCache`, a
        stream-fed mirror that could retain a closed position indefinitely. On
        2026-08-19 that cost a page and a day of blocked entries for a leg the
        broker had not held for over an hour.
        → docs/decisions/2026-08-19-account-cache-ghost-positions.md

        So before the highest-severity, no-allowance page fires, ask the only
        authority there is. REST agreeing with the LEDGER means the cache invented
        the extra leg — suppress. REST agreeing with the CACHE means a real position
        really is unbooked — page, exactly as before.

        **Every failure path returns False**, which pages. A confirm that times out,
        raises, or comes back unreadable proves nothing, and "could not check" must
        never be the reason a real unwatched position goes unreported. The eviction
        in `AccountStateCache.evict_absent` is what actually REPAIRS the cache; this
        only decides whether to wake someone in the meantime.

        One round trip per distinct (broker, ledger) shape, per
        `watchdog.book_confirm_ttl_s`. A steady disagreement confirms once; any
        change in the numbers re-confirms, so a verdict can never outlive the state
        it was measured against. `_book_confirm_lookup` holds the whole staleness
        policy and the reasoning for it.

        **The bound is `staleness.account_refresh_timeout_s`, not a watchdog cadence**
        (2026-08-21). This await is INSIDE one `check_once` pass — `_refresh_if_due`
        → `_check_broker_agreement` → here — so however long it takes, no position is
        evaluated against its triggers. It was bounded by `refresh_interval_s`, the
        watch-list REBUILD cadence: 15s, i.e. 15x `cadence_s` and 3x the `sustained_s`
        debounce, of blind time on the paging edge of a book disagreement — precisely
        the moment the book is least understood. Caching bounds the FREQUENCY (one
        stall per distinct shape) but never the duration of one. The right number
        already existed: `account_refresh_timeout_s` is THE tuned per-attempt bound
        for this broker's documented 502s and hangs, and this is the same REST call
        the liveness refresher makes. A timeout pages, like every other failure path.
        """
        if self._confirm_broker_position_count is None:
            return False
        key = (broker, ledger)
        cached = self._book_confirm_lookup(key)
        if cached is not None:
            return cached
        try:
            async with asyncio.timeout(self._config.staleness.account_refresh_timeout_s):
                confirmed = await self._confirm_broker_position_count()
        except Exception as exc:  # noqa: BLE001 - any failure must page, not suppress
            logger.warning(
                "book disagreement: REST confirm failed (%s: %s) — paging on the "
                "cache count rather than suppressing",
                type(exc).__name__, exc,
            )
            self._remember_book_confirm(key, False)
            return False
        if confirmed is None:
            self._remember_book_confirm(key, False)
            return False
        over_reporting = confirmed == ledger
        self._remember_book_confirm(key, over_reporting)
        if over_reporting:
            logger.warning(
                "book disagreement is a STALE ACCOUNT CACHE, not an unwatched "
                "position: REST holds %d leg(s), matching the ledger; the cache says "
                "%d. Not paging; the next REST re-seed evicts it.",
                confirmed, broker,
            )
            await self._audit.record(
                "watchdog.book_cache_over_reporting",
                {**detail, "rest_legs": confirmed},
            )
        return over_reporting

    def _book_confirm_lookup(self, key: tuple[int, int]) -> bool | None:
        """A still-valid cached verdict for this shape, or None to go ask REST.

        **THE staleness policy for the confirm cache** (2026-08-21, A4b). Caching a
        verdict at all is a safety trade: a `True` suppresses `held_but_unbooked`,
        the one condition deliberately built with no timer allowance, because a
        position held with no ledger row has no virtual stop being evaluated. So
        what a cached verdict is allowed to outlive is the whole design, not a
        detail of it. Three bounds, strongest first:

        1. **The episode.** `_check_broker_agreement` clears the entire cache the
           moment broker == ledger. Every verdict here describes a disagreeing book;
           agreement means that book is gone. Evidence-based, no clock involved, and
           it is what makes the other two backstops rather than the main defence.
        2. **The shape.** A verdict answers for one `(broker_legs, ledger_legs)`
           pair and nothing else. Every fill moves the ledger count and every book
           update moves the cache count, so almost any change in what is held
           re-keys and re-measures on its own.
        3. **`watchdog.book_confirm_ttl_s`, from when the verdict was MEASURED** —
           never extended by a hit, or a shape the book sits on for hours would
           carry an immortal verdict, which is precisely the failure mode (1) and
           (2) exist to prevent. This is the backstop for the case they cannot see:
           the same numbers recurring inside one long episode for a DIFFERENT
           reason. A ghost leg evicted in the same re-seed that a real fill goes
           unbooked leaves the counts identical and the verdict wrong, and neither
           the episode nor the shape has changed. Narrow, but its cost is silence on
           the no-allowance page, so it gets a hard cap rather than an argument
           about likelihood.

        Note which way the three point when they disagree with each other: every one
        of them fails toward asking REST again, and asking again can only cost a
        round trip. Nothing here can produce a suppression that a fresh confirm
        would not have produced.
        """
        entry = self._book_confirm.get(key)
        if entry is None:
            return None
        verdict, measured_at = entry
        if self._clock() - measured_at >= self._wd.book_confirm_ttl_s:
            del self._book_confirm[key]
            return None
        return verdict

    def _remember_book_confirm(self, key: tuple[int, int], verdict: bool) -> None:
        """Record a fresh verdict, dropping expired ones on the way through.

        Pruning on WRITE rather than on a timer keeps the cache's size a function of
        what is actually being confirmed, and means the expiry rule is applied by
        the same clock reading that stamps the new entry.
        """
        now = self._clock()
        ttl = self._wd.book_confirm_ttl_s
        self._book_confirm = {
            shape: entry
            for shape, entry in self._book_confirm.items()
            if now - entry[1] < ttl
        }
        self._book_confirm[key] = (verdict, now)
        while len(self._book_confirm) > _BOOK_CONFIRM_CACHE_MAX:
            oldest = min(self._book_confirm, key=lambda k: self._book_confirm[k][1])
            del self._book_confirm[oldest]

    def _book_allowance_elapsed(self, direction: str, detail: dict[str, Any]) -> bool:
        """May this disagreement escalate yet? (2026-08-13, extended 2026-08-20)

        For `booked_but_absent` it is True only once the disagreement has survived
        `watchdog.book_settle_s` of WALL TIME since Argus first saw it. The window
        deliberately does NOT arm the escalation slot: `_Escalation.rising` is what
        makes an episode's Tier 1 fire exactly once, so marking it during the
        allowance would consume the edge and the page would never come. Nothing
        here is silent — every suppressed observation writes the same Tier 2
        `book_disagreement_still` record the post-edge repeats do, carrying how far
        into the allowance it is.

        **`held_but_unbooked` is the same physics running the other way, and it gets
        an allowance that is NOT a timer** (2026-08-20). `book_settle_s` was
        calibrated on an entries-only day and wired to one direction; the first
        intraday stop since showed the exit produces the mirror image — Argus deletes
        the `positions` row the instant the closing fill books, and the broker's own
        book trails it. Measured: fired 1.215s after the closing fill, cleared 6.012s
        later on its own, one ERROR page for ordinary propagation lag.

        A blanket timer in THIS direction would be the wrong fix, and is rejected
        explicitly: `held_but_unbooked` means a position may be held with no virtual
        stop being evaluated, and suppressing that for 20s because a clock says so
        would delay the one page this check exists to send. So the allowance is
        keyed off EVIDENCE instead — Argus's own record of a closing fill that took
        specific legs to flat (`note_fills`). The excess must be covered by legs
        Argus itself just closed; a leg nobody closed buys nothing, and a
        disagreement with no matching closing fill pages on the first observation,
        exactly as before.

        The evidence's own lifetime IS the window: `_closing_fill_cover` prunes at
        `book_settle_s`, so a disagreement that outlives the lag it was blamed on
        starts paging without a second knob meaning the same thing.
        """
        if direction != self._book_settling_direction:
            # A flip in direction is a different episode: whatever is happening now
            # is not what the elapsed window was measuring.
            self._book_settling_since = None
            self._book_settling_direction = direction
        if direction == "held_but_unbooked":
            covered = self._closing_fill_cover()
            excess = int(detail["broker_legs"]) - int(detail["ledger_legs"])
            if excess > len(covered):
                # Not (or not fully) explained by Argus's own exits. This is the
                # unwatched-position case and it pages on sight.
                self._book_settling_since = None
                return True
            detail = {**detail, "closing_fill_legs": sorted(covered)}
        now = self._clock()
        if self._book_settling_since is None:
            self._book_settling_since = now
        elapsed = now - self._book_settling_since
        if direction == "booked_but_absent" and elapsed >= self._wd.book_settle_s:
            return True
        self._audit.telemetry(
            "watchdog.book_disagreement_still",
            {**detail, "settling_s": round(elapsed, 3),
             "settle_allowance_s": self._wd.book_settle_s},
        )
        return False

    def _closing_fill_cover(self) -> set[str]:
        """Legs Argus's own closing fills took to flat inside `book_settle_s`.

        Pruned here rather than on write so the set is always measured against the
        clock at the moment of the comparison, never against whenever the last fill
        happened to arrive.
        """
        cutoff = self._clock() - self._wd.book_settle_s
        self._closed_legs = {
            symbol: at for symbol, at in self._closed_legs.items() if at > cutoff
        }
        return set(self._closed_legs)

    async def _refresh_if_due(self) -> None:
        now = self._clock()
        due = (
            self._refresh_requested
            or self._last_refresh is None
            or now - self._last_refresh >= self._wd.refresh_interval_s
        )
        if not due:
            return
        self._refresh_requested = False
        self._last_refresh = now
        # The ledger read's own trading date, from the same session-scoped seam the
        # exit path stamps its orders with — so what counts as an expired (and
        # therefore closed) leg cannot disagree with the day this watchdog is
        # running. Legs expiring TODAY are never dropped; only a previous day's.
        scan = await self._spreads.open_spreads(today=self._trading_date())
        previous = {s.external_identifier for s in self._scan.spreads}
        current = {s.external_identifier for s in scan.spreads}
        for gone in previous - current:
            # Closed (or no longer attributable). Drop its debounce windows and edge
            # state so a re-entry of the same spread starts from zero rather than
            # inheriting a partially-elapsed window from the previous position.
            self._sustain.forget(gone)
            self._escalation.forget(gone)
            self._impaired.pop(gone, None)
            # The mark goes with them — it is a reading of a position the ledger no
            # longer says is held, so keeping it could only ever be stale. NOT
            # `_attempted`, `_sends` or `_flatten_attempted`, deliberately: those are
            # what stop a spread that flickers out and back from coming back with a
            # fresh budget.
            self._spent_marks.pop(gone, None)
            # A position deferred today that has now left the ledger (a stop closed
            # it, or it closed some other way) gets its deferral outcome row now.
            if gone in self._deferrals:
                await self._write_deferral_outcome(gone, ended="left_the_ledger")
        self._scan = scan
        # Only on success (§3.7): this is the timestamp `scan_age_s` measures from,
        # and a refresh that raised further down must not reset it.
        self._last_scan_ok = now
        await self._refresh_working_orders()
        await self._check_broker_agreement()
        await self._report_unwatchable(scan)
        for spread in scan.spreads:
            if spread.imbalanced:
                await self._audit.record("watchdog.leg_imbalance", spread.to_payload())
                self._notify("watchdog_leg_imbalance", spread.to_payload())
            await self._report_incoherent_credit(spread)

    async def _report_incoherent_credit(self, spread: OpenSpread) -> None:
        """The fills say this credit spread was opened for a DEBIT. (2026-09-02)

        The spread is still WATCHED — on the limit, which is the basis it had before
        migration 0005 and is the fail-closed direction, since a limit basis is LOW
        and therefore a stop that is TIGHT. Routing it to `malformed` instead would
        have cost a live, well-formed vertical its credit trigger, its underlying
        trigger, its `FLATTEN_BEFORE_CLOSE`, its row on `/status` and its
        concurrency slot, over a price-arithmetic contradiction that leaves every
        other field coherent.

        **Which is exactly why it has to be loud.** A third `CreditBasis` value that
        merely read as a quieter `LIMIT` would be worse than the malformed routing
        it replaced: the operator would see a normal position and never learn that
        two of Argus's own accounts of the same order disagree. So it pages, once,
        with `external_identifier` and `candidate_id` at the TOP level of the detail
        — `notify.alert_scope` reads them there for the per-position throttle
        (2026-08-21), and nested inside `position` they silently fold together.
        """
        if spread.entry_credit_basis is not CreditBasis.LIMIT_FILLS_INCOHERENT:
            return
        if spread.external_identifier in self._incoherent_credit_paged:
            return
        self._incoherent_credit_paged.add(spread.external_identifier)
        payload = spread.to_payload()
        await self._audit.record("watchdog.entry_credit_incoherent", payload)
        self._notify("watchdog_entry_credit_incoherent", payload)

    async def _refresh_working_orders(self) -> None:
        """Poll OPENING orders still working at the broker, for the entry gate.
        (Finding 6, 2026-08-28)

        **A failure here NEVER halts the watchdog** — the one place in this module
        that swallows an exception, and deliberately. The count feeds
        `RiskSnapshot.open_spread_equivalent`, which only ever gates NEW entries;
        position monitoring does not read it and must not be taken down by a
        broker-read blip. On failure the last good count stands (fail toward
        blocking an entry, since a missed working order would only ever LOWER the
        count) and a telemetry record is written. Same asymmetry the exit path
        already draws: an order-level read is not a position-safety signal.

        **The residual-order recheck rides this same answer** (2026-08-29, P5). One
        fetch, two readers: the count, and the orders a finished walk left working at
        the broker. When the fetch fails neither runs, which is right — an answer
        that does not exist says nothing about either, and absence is never a verdict
        for a residual.
        """
        if self._live_orders is None:
            return
        try:
            live = await self._live_orders()
        except Exception as exc:  # noqa: BLE001 - see docstring: must not halt
            self._audit.telemetry(
                "watchdog.working_orders_unread",
                {"error": str(exc), "last": self._working_open_spreads},
            )
            return
        # The broker ANSWERED. The flatten deferral's evidence that the system can
        # still act (2026-09-25): a deferral is only ever taken while this is fresh.
        self._live_orders_ok_at = self._clock()
        self._working_open_spreads = open_working_spread_equivalents(live)
        self._working_open_by_side = open_working_spreads_by_side(live)
        self._working_open_worst = open_working_worst_case(live)
        await self._recheck_residual_orders(live)

    async def _recheck_residual_orders(self, live: LiveOrders) -> None:
        """Step the residual-order recheck on the answer just fetched. (P5)

        **A raise here NEVER halts the watchdog** — the second place in this module
        that swallows an exception, for the reason the first one gives and with more
        force. A failing cancel is the recheck's expected INPUT, not an exception,
        and taking the watchdog down over one would trade a live position's virtual
        stop for an unfilled order's tidiness. The recheck's own state survives, so
        the next poll simply asks again.
        """
        if self._residuals is None:
            return
        try:
            await self._residuals.step(live)
        except Exception as exc:  # noqa: BLE001 - see docstring: must not halt
            logger.error("residual-order recheck failed: %s", exc, exc_info=exc)
            self._audit.telemetry(
                "watchdog.residual_recheck_failed",
                {"error_type": type(exc).__name__, "error": str(exc)[:500]},
            )

    async def _report_unwatchable(self, scan: OpenSpreadScan) -> None:
        """Argus's own two records disagree — report it, ONCE per episode. (2026-08-17)

        A position held that the ledger cannot turn into a watchable spread is an
        OPEN POSITION NOBODY IS WATCHING, the exact gap this module exists to close.
        It stays Tier 1 and it still pages. What changed is how often.

        **Edge-triggered, like every other repetitive condition here.** Until now
        this path had no escalation at all: it wrote Tier 1 and alerted on EVERY
        refresh while the condition persisted, which on 2026-08-17 was 66 Tier 1
        records and a page every 15s for one unchanging fact. The signature is what
        defines an episode, so a genuinely NEW problem still pages immediately while
        the same one stays Tier 2.

        **And a settle allowance, for the same reason the book check has one.** The
        two legs of one multi-leg order book as SEPARATE fill events (~0.25s apart,
        measured 2026-08-13), and `on_filled` calls `request_refresh()` the moment
        the first one lands — so the watchdog aims straight at the window where a
        spread legitimately has one applied leg. That is post-fill propagation lag,
        exactly what `book_settle_s` already exists to absorb, so it governs both
        rather than adding a second knob meaning the same thing.

        **`unattributed` never waits.** A `positions` row no order explains is the
        held-but-unbooked direction: there is no pending second leg that would
        resolve it, so waiting only delays the page. Same asymmetry, same reasoning
        as `_book_allowance_elapsed`.
        """
        detail = {
            "unattributed_symbols": list(scan.unattributed),
            "malformed": list(scan.malformed),
        }
        if scan.fully_explained:
            await self._clear_unwatchable()
            return

        signature = json.dumps(detail, sort_keys=True, default=str)
        if signature != self._unwatchable_signature:
            # A different problem from the one being tracked: new episode, new
            # allowance window, and it is allowed to page again.
            self._unwatchable_signature = signature
            self._unwatchable_since = self._clock()
            self._unwatchable_paged = False

        if not scan.unattributed:
            elapsed = self._clock() - (self._unwatchable_since or self._clock())
            if elapsed < self._wd.book_settle_s:
                # Suppressed, never silent: the Tier 2 trail carries how far into
                # the allowance it got, which is the measurement the threshold is
                # re-tuned from.
                self._audit.telemetry(
                    "watchdog.unwatchable_still",
                    {**detail, "settling_s": round(elapsed, 3),
                     "settle_allowance_s": self._wd.book_settle_s},
                )
                return

        if not self._unwatchable_paged:
            self._unwatchable_paged = True
            logger.error(
                "UNWATCHABLE POSITION: %d unattributed symbol(s), %d malformed "
                "order(s) — a held position has no virtual stop being evaluated",
                len(scan.unattributed), len(scan.malformed),
            )
            await self._audit.record("watchdog.unwatchable", detail)
            self._notify("watchdog_unwatchable_position", detail)
        else:
            self._audit.telemetry("watchdog.unwatchable_still", detail)

    async def _clear_unwatchable(self) -> None:
        """The scan explains everything again. Closes whichever episode was open."""
        signature, paged = self._unwatchable_signature, self._unwatchable_paged
        since = self._unwatchable_since
        self._unwatchable_signature = None
        self._unwatchable_paged = False
        self._unwatchable_since = None
        if signature is None:
            return
        resolved_s = round(self._clock() - since, 3) if since is not None else None
        if paged:
            # Tier 1, pairing the Tier 1 that paged. An operator told a position was
            # unwatched must be told when it is watched again.
            await self._audit.record(
                "watchdog.watchable_again", {"resolved_after_s": resolved_s}
            )
            self._notify("watchdog_watchable_again", {"resolved_after_s": resolved_s})
        else:
            # Absorbed inside the allowance — the mid-fill window, almost always.
            # No Tier 1 pair to close, but the episode's DURATION is what says
            # whether the allowance is sized right, so it stays reconstructable.
            self._audit.telemetry(
                "watchdog.unwatchable_settled", {"settled_after_s": resolved_s}
            )

    async def _report_evaluability(
        self, spread: OpenSpread, evaluation: TriggerEvaluation
    ) -> None:
        """Edge-triggered reporting of "I cannot evaluate this position".

        Tier 1 on the way in and on recovery; Tier 2 while it persists. See
        `_Escalation` for why the middle is Tier 2.

        **TWO SEVERITIES, because they are two different situations (2026-08-13).**

        `blind` — no stop that can FIRE is evaluable (`TriggerEvaluation.blind`) — is
        the page. Nothing is watching the position; a human needs to know now. Under
        the strike-touch stop (credit trigger off) a dead SPX feed alone is blind.

        `degraded` — something unevaluable, but a stop that can fire still live —
        is NOT. After the
        identity/price split in `LegQuotes.resolve`, a stale wing quote costs only
        the credit trigger while the underlying backstop keeps evaluating and
        debouncing normally, and that happens on ~14% of ticks late in a session.
        Paging on it is the 2026-08-05 alert-storm shape: one page per 15 minutes
        with 100+ suppressed behind it, which trains the operator to ignore the
        channel that also carries `blind`. So degraded is Tier 2 on the rising edge
        and nothing more — `watchdog.check` already records `unknown_reasons` on
        every single tick, so the per-tick trail exists without duplicating it here.
        """
        key = spread.external_identifier
        # Live state for /status, from THIS evaluation — before any of the
        # edge-triggered audit branches below, so the two can never disagree about
        # what is currently unevaluable.
        self._note_evaluability(spread, evaluation)
        payload = {
            "external_identifier": key,
            "candidate_id": spread.candidate_id,
            **evaluation.to_payload(),
        }
        if evaluation.blind:
            # Blind SUPERSEDES degraded: clearing the degraded slot re-arms it, so
            # dropping back to degraded records that transition rather than sitting
            # silent on a slot armed before things got worse.
            self._escalation.falling(key, _DEGRADED)
            if self._escalation.rising(key, _BLIND):
                await self._audit.record("watchdog.unevaluable", payload)
                self._notify("watchdog_position_unevaluable", payload)
            else:
                self._audit.telemetry("watchdog.unevaluable_still", payload)
            return

        # At least one trigger is live from here.
        if self._escalation.falling(key, _BLIND):
            # Tier 1, and it fires even if the position is still degraded: this
            # closes the episode the page opened. The payload's `unknown_reasons`
            # says whether anything is still missing.
            await self._audit.record("watchdog.evaluable_again", payload)
        if evaluation.fully_evaluable:
            self._escalation.falling(key, _DEGRADED)
        elif self._escalation.rising(key, _DEGRADED):
            # Name what is still live rather than assume it (2026-09-27 review): this
            # line said "backstop still live" in the one state where the backstop was
            # exactly what had gone.
            logger.warning(
                "position %s degraded: %s unevaluable, still live: %s",
                key, sorted(str(r) for r in evaluation.unknown_reasons),
                sorted(str(r) for r in evaluation.acting_stops - evaluation.unknown_reasons),
            )
            self._audit.telemetry("watchdog.degraded", payload)

    async def _trigger(
        self,
        spread: OpenSpread,
        evaluation: TriggerEvaluation,
        fired: frozenset[TriggerReason],
        *,
        deferral: DeferralDecision | None,
    ) -> None:
        """A trigger has been sustained. Record it, then hand off the exit.

        The RECORD is edge-triggered; the handoff below is not. A sustained condition
        keeps firing on every evaluation, and in the unpriceable case below no exit
        can be built to clear it — so an unconditional Tier 1 write here would
        produce one crash-critical record per cadence tick for as long as the
        condition holds. One event per episode; the repeats are Tier 2.

        `deferral` is this tick's flatten-deferral decision, or None where it made
        none (mode `off`, no flatten in `fired`, a spent position). REQUIRED, with no
        default: a new caller that forgot it would silently drop the decision from
        the record again (2026-09-28, P11). Recorded only; nothing here decides on it.
        """
        key = spread.external_identifier
        payload = {
            "position": spread.to_payload(),
            "fired": sorted(str(r) for r in fired),
            "sustained_s": self._wd.sustained_s,
            "held_s": {str(r): self._sustain.held_s(key, r) for r in fired},
            **evaluation.to_payload(),
            **_deferral_field(deferral),
        }
        # A profit take has its own episode (see `_PROFIT_TRIGGERED`) and no page of
        # its own: `watchdog_triggered` is the stop-loss alert, and a take is paged
        # when — and only if — it closes the position.
        profit = _is_profit_take(fired)
        if self._escalation.rising(key, _PROFIT_TRIGGERED if profit else _TRIGGERED):
            await self._audit.record("watchdog.triggered", payload)
            if not profit:
                self._notify(
                    "watchdog_triggered",
                    {
                        "external_identifier": key,
                        "candidate_id": spread.candidate_id,
                        "fired": sorted(str(r) for r in fired),
                        "cost_to_close": evaluation.cost_to_close,
                        "credit_stop": evaluation.credit_stop,
                        "spot": evaluation.spot,
                        **spread.credit_payload(),
                    },
                )
        else:
            self._audit.telemetry("watchdog.triggered_still", payload)

        if evaluation.market is None:
            # The underlying trigger fired while the legs could not be priced — the
            # blown-out-spread case it exists for. No order can be built from nothing,
            # so this is NOT counted as an attempt: the trigger stays latched and the
            # next cycle tries again the moment quotes return. Escalated on the rising
            # edge only, since it repeats every tick until they do.
            payload = {
                # Top level, not only inside `position`: `notify.alert_scope` reads
                # these to give THIS position its own throttle window, so two
                # spreads going unpriceable in the same move page separately
                # instead of the second folding into the first's suppressed count
                # (2026-08-21, B1). `candidate_id` is the field it prefers — the
                # trade idea outlives both of the position's order identifiers.
                "external_identifier": key,
                "candidate_id": spread.candidate_id,
                "position": spread.to_payload(),
                "fired": sorted(str(r) for r in fired),
                "problems": list(evaluation.problems),
                # The runway (2026-08-28, P2). This is the one path a flatten cannot
                # get past — no quotes, no price, no order, and no version of that
                # respects "never an unbounded market order" — so the page has to say
                # how long the human picking it up has. "Triggered, unpriceable" reads
                # very differently with 25 minutes left than with 25 minutes to go.
                "seconds_to_flatten": evaluation.seconds_to_flatten,
            }
            if self._escalation.rising(key, _UNPRICEABLE):
                await self._audit.record("watchdog.exit_unpriceable", payload)
                self._notify("watchdog_exit_unpriceable", payload)
            else:
                self._audit.telemetry("watchdog.exit_unpriceable_still", payload)
                await self._page_if_no_exit_yet(key, _UNPRICEABLE, payload)
            return
        self._escalation.falling(key, _UNPRICEABLE)

        market = evaluation.market
        if TriggerReason.FLATTEN_BEFORE_CLOSE in fired:
            # Spend the flatten allowance HERE — at the handoff, as the FAIL-CLOSED
            # DEFAULT. Spending it above (at the `market is None` branch) would consume
            # the position's last action on an order that was never sent, which is the
            # exact failure `NOT_ATTEMPTED` exists to avoid; spending it on the REPORT
            # instead would let a crash mid-exit hand out a second one. Unconditional
            # rather than gated on `_attempted`: a position taking the ordinary path now
            # must not arrive at the mark later with an unspent allowance on top of the
            # attempt it has already had.
            #
            # **What this line establishes is "an order was handed to the walk", not
            # "an order can be built"** (2026-09-02). The condition just established is
            # `market is not None` — the legs have QUOTES — and construction can still
            # refuse afterwards, in `_build_exit_order` -> `build.py`. That gap cost
            # both of 2026-09-01's flattens their override on an order that was never
            # sent. It is closed at the other end, in `_run_exit`: `NOT_ATTEMPTED` gives
            # the allowance back. Default spent, restored only by a positive report —
            # which is what keeps the crash clause above true while making the first
            # one true for the build as well as for the quotes.
            self._flatten_attempted.add(key)
        # Exits run as their own tasks so that watching does not stop while one is in
        # flight. An exit walk holds each price for at least a full COA settle window
        # and may step several times; doing it inline would leave every OTHER open
        # position unchecked for the duration, which for a two-position book is half
        # the risk unwatched at the worst possible moment.
        task = asyncio.create_task(
            self._run_exit(spread, market, fired), name=f"watchdog-exit-{key}"
        )
        self._exiting[key] = task

    async def _run_exit(
        self,
        spread: OpenSpread,
        market: SpreadMarket,
        fired: frozenset[TriggerReason],
        *,
        stub: bool = False,
    ) -> None:
        key = spread.external_identifier
        # Decided from `fired`, the set `close` is about to be handed — the executor
        # answers the same question off the same set, so the ladder it walks and the
        # allowance this frame touches cannot disagree (see `_is_profit_take`).
        profit = _is_profit_take(fired)
        try:
            report = await self._executor.close(spread, market, fired, stub=stub)
        except BaseException as exc:  # noqa: BLE001 - the crash path; see below
            if isinstance(exc, asyncio.CancelledError):
                raise
            # IN-MEMORY SAFETY STATE FIRST, before anything that can fail — the same
            # ordering rule `KillSwitch.engage` follows. An exception is the one state
            # in which we do not know whether an order reached the broker, so it is
            # the STRONGEST case for the one-attempt rule, not an exception to it
            # (§2.5a): without this, the next cadence tick re-evaluates the position,
            # the trigger is still sustained, and `_trigger` hands off a second
            # closing order for a position whose first may or may not have landed —
            # the double-close `_attempted` exists to prevent.
            #
            # A crashed PROFIT TAKE retires the profit target instead (2026-09-23),
            # and that is the same rule rather than an exception to it: the one
            # allowance that may have been used is the take's, so the take's is what
            # is spent. The emergency exit keeps its attempt — and if the crashed
            # take's order did land and is live, the emergency walk's duplicate guard
            # BLOCKS rather than stacking on it, which is the licence the flatten
            # override already stands on.
            if stub:
                # The stub allowance was spent at the HANDOFF, before this task
                # existed, so a crash cannot grant a second one — and
                # `_attempted` is deliberately untouched as well: the spent row
                # keeps describing the ORIGINAL partial (outcome FILLED,
                # original `since`), which is what the repeating page,
                # `/status` and the re-detection condition all read. A
                # continuation report that overwrote it with `NOT_RESOLVED`
                # would silently disarm the stub state.
                pass
            elif profit:
                self._spend_profit_target(
                    key, ExitOutcome.NOT_RESOLVED,
                    f"the profit take crashed: {type(exc).__name__}: {exc}"[:500],
                    order_may_be_working=True,
                )
            else:
                self._spend_on_crash(key, exc)
            if isinstance(exc, AuditWriteError):
                # The one exclusion (§3.5), unchanged: the recorder is dead
                # process-wide and the halt has already happened through
                # `on_failure`. Today's path, verbatim.
                await self._halt_on_exception(exc, where="exit")
                return
            if not isinstance(exc, Exception):
                raise
            await self._note_exit_crash(spread, exc, profit=profit, stub=stub)
            return
        if stub:
            await self._after_stub_close(spread, report)
        elif profit:
            await self._after_profit_take(spread, report)
        elif report.outcome is ExitOutcome.NOT_ATTEMPTED:
            # Nothing was sent, so the position keeps BOTH of its allowances and the
            # trigger stays latched. Escalated here rather than in the executor because
            # only the watchdog knows this is a repeat: it can recur every cadence tick.
            #
            # `_attempted` keeps its one attempt by simply not being written below.
            # **The flatten's allowance has to be given BACK** (2026-09-02), because it
            # is claimed at the handoff in `_trigger`, several frames before this: on
            # 2026-09-01 both flattened positions burned the override on an order that
            # was never built, and the failure that built none of them —
            # `_limit_price` refusing a net mid that rounds to 0.00 at the tick — is
            # the flatten's MODAL regime, not its tail. A winning 0DTE spread is nearly
            # worthless by 15:30; that is what makes it winning. Both sentences above
            # were true of `_attempted` and false of this, for the same failure.
            #
            # **Safe on the RETURN path specifically, and that is the whole argument.**
            # `NOT_ATTEMPTED` is a VALUE, constructed only at sites lexically above the
            # walk in `ExitExecutor.close` (an AST test pins that), so it cannot
            # describe an order that reached the broker. A crash is an EXCEPTION: it
            # never reaches here, and leaves the allowance spent — which is why the
            # SPEND stays at the handoff, as the fail-closed default. A crash must not
            # be able to GRANT; only a positive value may RESTORE.
            #
            # This is not a second send. `_flatten_may_act` still permits exactly one
            # order to be handed to the walk, and that order still goes out through
            # `DuplicateOrderGuard`, which re-asks the BROKER and permits only DEAD.
            # What is returned here is a look that never became an order.
            #
            # Gated on the same condition that spent it, so the pair reads as one rule
            # in one place — a position that was never granted an override cannot be
            # handed one by this line.
            # → docs/decisions/2026-09-02-the-flatten-allowance-is-spent-by-a-send.md
            if TriggerReason.FLATTEN_BEFORE_CLOSE in fired:
                self._flatten_attempted.discard(key)
            payload = {
                **report.to_payload(),
                # The subject `notify.alert_scope` throttles on, at the top level
                # (2026-08-21, B1). `candidate_id`, NOT the identifier: the report's
                # own `external_identifier` names the CLOSING order and is
                # `str | None` — NOT_ATTEMPTED is exactly the outcome that can carry
                # None, since no order was built to name it by — while the spread's
                # names the OPENING one. Scoping on either would key one position's
                # episode by a different string depending on which alert fired.
                "candidate_id": spread.candidate_id,
                "position": spread.to_payload(),
            }
            if self._escalation.rising(key, _UNATTEMPTED):
                await self._audit.record("watchdog.exit_unattempted", payload)
                self._notify("watchdog_exit_unattempted", payload)
            else:
                self._audit.telemetry("watchdog.exit_unattempted_still", payload)
                await self._page_if_no_exit_yet(key, _UNATTEMPTED, payload)
        elif (
            (retry := _RETRYABLE_EXITS.get(report.outcome)) is not None
            and self._spend_send(key) < _EXIT_SEND_ATTEMPTS
        ):
            # A send was spent, the broker positively answered that there is nothing
            # to stack onto, and budget is left. The trigger stays latched, so the next
            # cadence tick rebuilds and re-sends — through `DuplicateOrderGuard`, which
            # is what makes the retry safe rather than the outcome's own answer.
            # `_RETRYABLE_EXITS` carries the whole argument for who is in here.
            #
            # `_spend_send` is only reached when the outcome IS retryable — `and`
            # short-circuits — so a spent attempt never charges a budget it cannot use.
            #
            # Escalated on its OWN tag, never `_UNATTEMPTED`'s, and one tag per outcome:
            # all three look identical on the phone ("triggered, nothing in the market,
            # still retrying") and point at three different things. `_UNATTEMPTED` is a
            # nothing-was-sent problem that clears on its own — the feed thinning, or
            # (2026-09-03) the broker not answering the guard, both told apart by
            # `report.reason`'s sentence rather than by a tag; `_NOTHING_SENT` says go and
            # look at the BROKER; `_CROSS_UNPRICEABLE` says the short leg had no offer,
            # which may be the feed and may be the book.
            tag, category, alert_kind = retry
            payload = {
                **report.to_payload(),
                "candidate_id": spread.candidate_id,
                "position": spread.to_payload(),
                "sends": self._sends[key],
                "send_budget": _EXIT_SEND_ATTEMPTS,
            }
            if self._escalation.rising(key, tag):
                await self._audit.record(category, payload)
                self._notify(alert_kind, payload)
            else:
                self._audit.telemetry(f"{category}_still", payload)
                await self._page_if_no_exit_yet(key, tag, payload)
        else:
            # An order reached (or was refused by) the broker. One attempt only: a
            # second closing order could stack on the first and close the spread
            # twice, which does not flatten it — it re-opens it inverted.
            #
            # Also the terminus of the branch above once its budget is out: a
            # confirmed-empty broker is retryable, and retryable is not infinite.
            self._spend_attempt(key, report, contracts=spread.contracts)
        # Whatever happened, the ledger is the truth about what is still held.
        self.request_refresh()

    def _spend_attempt(self, key: str, report: ExitReport, *, contracts: int) -> None:
        """Spend `key`'s ONE exit attempt on `report`. In-memory only; cannot fail.

        THE writer of `_attempted` for an exit that reached the broker — extracted
        unchanged (2026-09-23 review) so the second caller shares it rather than
        re-spelling it: a profit take that FILLED. A fill under the shared
        `argus-close-*` key is what makes every later close of this position
        `BLOCKED` by `DuplicateOrderGuard` (it blocks on FILLED — "the exit already
        happened"), so from that fill on the position's automated close is spent
        whichever trigger sent it; see `_after_profit_take`.
        """
        self._escalation.falling(key, _UNATTEMPTED)
        for tag, _category, _kind in _RETRYABLE_EXITS.values():
            self._escalation.falling(key, tag)
        self._attempted[key] = _SpentAttempt(
            report.outcome,
            self._clock(),
            _alert_reject_reason(report.walk),
            _alert_mutation_error(report.walk),
            # The origin's reasons and size, carried for the stub continuation
            # (2026-09-24): WHICH ladder a partial's remainder re-walks starts from
            # why the filled close was sent, and WHETHER there is a stub at all is
            # read against how much that close was sized for.
            reasons=frozenset(report.reasons),
            contracts=contracts,
        )
        # Arm the "spent but still held" episode HERE rather than on the next
        # tick, so the first page fires a full bound after the exit attempt
        # itself. `rising` is idempotent and does not reset an episode already
        # running, which is what lets `_page_if_spent_and_still_held` re-arm
        # defensively without shortening this window. The DURATION both the page
        # and `/status` report comes off `_SpentAttempt.since` above, not off
        # this episode, so the defensive re-arm cannot restart it.
        self._escalation.rising(key, _SPENT)

    def _spend_on_crash(self, key: str, exc: BaseException) -> None:
        """Spend the attempt. In-memory only; cannot fail; called before anything else.

        The verdict is `NOT_RESOLVED`, and no new `ExitOutcome` member is added: the
        question that enum answers for the retry machinery is *may this position get
        another automated send?*, and for a crash the answer is the absolute NO that
        `NOT_RESOLVED` already carries — a fourth member would force a classification
        decision in `_RETRYABLE_EXITS`, `outcome.closed`, and every reader, for a
        distinction none of them acts on. The SENTENCE is the new field,
        `crash_error`, on the same precedent as `reject_reason` and `mutation_error`.

        The escalation edges mirror the normal spend in `_run_exit` exactly: `_SPENT`
        arms the repeating page (which is what an operator is most likely to actually
        read), and the retry tags fall so a crash cannot leave a stale "still
        retrying" episode armed. **`_flatten_attempted` is NOT touched** — a crash
        during the ordinary attempt leaves the flatten override available, which is
        the correct and useful case: at 15:30 that position still gets ONE further
        send, through `DuplicateOrderGuard`, which re-asks the broker and permits
        only DEAD. A crash must not be able to GRANT; only a positive value may
        RESTORE.
        """
        self._attempted[key] = _SpentAttempt(
            ExitOutcome.NOT_RESOLVED,
            self._clock(),
            reject_reason=None,
            mutation_error=None,
            crash_error=f"{type(exc).__name__}: {exc}"[:500],
        )
        # Arm the repeating page HERE, exactly as the normal spend does, so the first
        # page fires a full bound after the crash rather than immediately.
        self._escalation.rising(key, _SPENT)
        self._escalation.falling(key, _UNATTEMPTED)
        for tag, _c, _k in _RETRYABLE_EXITS.values():
            self._escalation.falling(key, tag)

    def _spend_profit_target(
        self,
        key: str,
        outcome: ExitOutcome,
        reason: str | None,
        *,
        order_may_be_working: bool,
    ) -> None:
        """Retire `key`'s PROFIT TARGET. In-memory only; cannot fail. (2026-09-23)

        THE only writer of `_profit_spent`, and it writes nothing else — no
        `_attempted`, no `_sends`, no `_flatten_attempted`, no escalation tag. That
        is the design's load-bearing sentence ("its own `_profit_spent`, never
        `_attempted`") as a function boundary, and the AST test holding it reads this
        method's body along with the profit path's.

        First writer wins: the retirement that explains the row is the FIRST reason
        the take stopped, and a later one cannot occur anyway — `_may_act_on` strips
        the trigger the moment this is written.
        """
        if key in self._profit_spent:
            return
        self._profit_spent[key] = _ProfitSpent(
            outcome=outcome,
            since=self._clock(),
            reason=reason,
            order_may_be_working=order_may_be_working,
        )

    async def _after_profit_take(self, spread: OpenSpread, report: ExitReport) -> None:
        """Account for one profit take against the TAKE'S OWN allowance. (2026-09-23)

        The profit path's whole counterpart to the emergency branch of `_run_exit`,
        and the reason §11 of the design record exists: on the emergency path an
        outcome that reached the broker spends `_attempted` — the position's ONE
        emergency exit — and a profit take at 10:34 that spent it would retire the
        stop that 20260901-put-7605-7600 needed at 14:40, turning a +$225 save into
        an uncovered tail. So nothing a take reports can reach that allowance UNLESS
        THE TAKE FILLED:

        - `NOT_ATTEMPTED` — nothing was sent, nothing is charged; Tier 2 only, and the
          take stands down for `_PROFIT_REFUSAL_BACKOFF_S` so a broker that cannot be
          asked does not keep the position in `_exiting`. The emergency path pages
          this edge (`_UNATTEMPTED`) because a position that MUST close has no order
          in the market; a winning position with no take in the market is the steady
          state, and paging it every 30s is exactly the 2026-09-22 afternoon the
          trigger's equality was chosen to prevent;
        - closed — the take did its job, and **the position's automated close is
          spent with it, exactly as an emergency fill spends it** (2026-09-23
          review). Not a policy choice: the take's fill sits under the SAME
          `argus-close-*` key every later close would use, and `DuplicateOrderGuard`
          blocks on FILLED, so no automated close of this position can ever be sent
          again. On a full fill that is moot — the ledger drops the row within
          seconds. On a PARTIAL fill (the walk cancels the remainder and reports
          `FILLED`, and `filled_contracts` is rung-scoped so it cannot tell) it is the
          stub that is left, and before this it was left believing its stop was
          intact: the stop's first fire came back `BLOCKED`, and nothing had said the
          stub needed a human. Routing it through `_spend_attempt` hands it to the
          machinery built for exactly this state — the repeating
          `watchdog_exit_spent_still_held` page with the live mark, and `/status`'s
          NO EXIT LEFT row — and `_flatten_attempted` is untouched, as on the
          emergency path;
        - `UNFILLED` / `NOTHING_AT_BROKER` — the broker positively said nothing is
          working and nothing filled; retried until `_PROFIT_SEND_ATTEMPTS`, then
          retired quietly (Tier 1, no page): every emergency protection is intact and
          nothing is at the broker, so there is nothing for a human to do;
        - anything else — retired, Tier 1, and PAGED once: the take ended in a state
          that was not confirmed flat at the broker, so a close may be resting there,
          and while it is the emergency exit's guard will refuse to stack on it. That
          is the one thing about a profit take an operator must hear.
        """
        key = spread.external_identifier
        outcome = report.outcome
        payload = {
            **report.to_payload(),
            # Top level for `notify.alert_scope`'s per-position throttle (B1).
            "candidate_id": spread.candidate_id,
            "position": spread.to_payload(),
        }
        if outcome is ExitOutcome.NOT_ATTEMPTED:
            self._profit_retry_at[key] = self._clock() + _PROFIT_REFUSAL_BACKOFF_S
            payload["retry_in_s"] = _PROFIT_REFUSAL_BACKOFF_S
            self._audit.telemetry("watchdog.profit_target_unattempted", payload)
            return
        sends = self._profit_sends.get(key, 0) + 1
        self._profit_sends[key] = sends
        payload["profit_sends"] = sends
        payload["send_budget"] = _PROFIT_SEND_ATTEMPTS
        if report.walk is not None and report.walk.outcome is WalkOutcome.REJECTED:
            # Before the branching below, because a rejection can end in any of them
            # (confirmed dead -> retried; unconfirmed -> retired) and is news in
            # every one of them.
            await self._page_profit_rejection(spread, report)
        if outcome.closed:
            self._profit_taken += 1
            # In-memory first, both allowances: this cannot fail, and the order
            # matters only in that nothing between here and the refresh may see a
            # filled take as a position with a close still available.
            self._spend_attempt(key, report, contracts=spread.contracts)
            self._spend_profit_target(
                key, outcome, report.reason,
                order_may_be_working=_take_may_be_working(report),
            )
            return
        if outcome in _RETRYABLE_PROFIT_TAKES:
            if sends < _PROFIT_SEND_ATTEMPTS:
                # The trigger is still sustained, so the next cadence tick rebuilds
                # and re-sends — through `DuplicateOrderGuard`, which is the licence.
                self._audit.telemetry("watchdog.profit_target_retry", payload)
                return
            self._spend_profit_target(
                key, outcome, report.reason, order_may_be_working=False
            )
            await self._audit.record(
                "watchdog.profit_target_retired",
                {**payload, "order_may_be_working": False},
            )
            return
        self._spend_profit_target(
            key, outcome, report.reason, order_may_be_working=True
        )
        detail = {
            "external_identifier": key,
            "candidate_id": spread.candidate_id,
            "contracts": spread.contracts,
            **spread.credit_payload(),
            "outcome": str(outcome),
            "reason": report.reason,
            # It names an outcome, so it owes the broker's account of it — the rule
            # `_alert_reject_reason` states and the AST sweep enforces.
            "reject_reason": _alert_reject_reason(report.walk),
            "mutation_error": _alert_mutation_error(report.walk),
            "profit_sends": sends,
            "send_budget": _PROFIT_SEND_ATTEMPTS,
            "order_may_be_working": True,
            # The sentence the operator most needs, as a field: the stop, the
            # backstop and the flatten are all still armed for this position.
            "exit_attempt_intact": key not in self._attempted,
        }
        logger.error(
            "PROFIT TARGET RETIRED UNCONFIRMED: %s's take ended %s and its order was "
            "not confirmed terminal — check the broker for a working close; the "
            "emergency exit keeps its attempt (%s)",
            key, outcome, report.reason,
        )
        await self._audit.record("watchdog.profit_target_retired", detail)
        self._notify("watchdog_profit_target_retired", detail)

    async def _after_stub_close(self, spread: OpenSpread, report: ExitReport) -> None:
        """Account for a stub continuation against the STUB'S OWN allowance. (2026-09-24)

        The stub path's whole counterpart to `_run_exit`'s emergency branch, on
        `_after_profit_take`'s shape, and the design's §5.4 holds the line that
        matters: **the stub's one allowance is its only budget.** A continuation
        report never writes `_attempted` — the spent row keeps describing the
        ORIGINAL partial, which is what the repeating page, `/status` and the
        re-detection condition read — and never writes `_sends`,
        `_flatten_attempted` or `_profit_spent`.

        - `NOT_ATTEMPTED` (construction refusal, or the guard burst exhausted):
          nothing was sent, so the allowance is RESTORED, paced one
          `watchdog.refresh_interval_s` out. Unbounded in count, like the
          emergency path's own `NOT_ATTEMPTED` — it is evidence-driven (a feed
          that thins, a broker that does not answer) — but bounded in RATE, and
          it can never pre-empt the flatten, which `_maybe_stub_close` yields to.
          Paged on the stub's own edge, once per episode.
        - The walk ended `BLOCKED` — the guard refused, for ANY reason: the partial's
          remainder still `Cancel Requested` (`conflicting_working_order`), a
          booking the ledger has not caught up with (`close_quantity_exceeded`), an
          unreadable opener. `BLOCKED` is returned before anything is submitted, so
          NOTHING WAS SENT and the allowance is RESTORED — bounded at
          `_STUB_BLOCKED_RETRIES`, paced like the above. The executor records each
          such ask at Tier 1 but does not page it (`ExitExecutor.close(stub=True)`):
          the ordinary causes clear in seconds, and a page per ask would read as an
          exit failing. The LAST refusal keeps the allowance spent and pages once,
          with the guard's reason.
        - Everything else — `FILLED`, rejections, `NOT_RESOLVED` from an order that
          reached the broker — keeps the allowance spent. The executor's own
          `_record_exit` has already paged by outcome through the existing alert
          kinds, rejection included with the broker's words; the repeating spent
          page and the flatten backstop take it from there.
        """
        key = spread.external_identifier
        outcome = report.outcome
        payload = {
            **report.to_payload(),
            # Top level for `notify.alert_scope`'s per-position throttle (B1).
            "candidate_id": spread.candidate_id,
            "position": spread.to_payload(),
            "stub_close": True,
        }
        if outcome is ExitOutcome.NOT_ATTEMPTED:
            self._restore_stub_allowance(key)
            if self._escalation.rising(key, _STUB_UNATTEMPTED):
                await self._audit.record("watchdog.stub_close_unattempted", payload)
                self._notify("watchdog_stub_close_unattempted", payload)
            else:
                self._audit.telemetry("watchdog.stub_close_unattempted_still", payload)
            return
        # The continuation reached the guard: the nothing-was-built episode, if any,
        # is over, so a later one on this position is a new episode and pages.
        self._escalation.falling(key, _STUB_UNATTEMPTED)
        if report.walk is not None and report.walk.outcome is WalkOutcome.BLOCKED:
            refusals = self._stub_blocked.get(key, 0) + 1
            self._stub_blocked[key] = refusals
            detail = {
                **payload,
                "guard_reason": report.walk.reason,
                "refusals": refusals,
                "bound": _STUB_BLOCKED_RETRIES,
            }
            if refusals < _STUB_BLOCKED_RETRIES:
                self._restore_stub_allowance(key)
                self._audit.telemetry("watchdog.stub_close_blocked", detail)
                return
            logger.error(
                "STUB CLOSE EXHAUSTED: %s — the duplicate guard refused the stub's "
                "close %d times (last: %s); nothing was sent, the allowance stays "
                "spent, and the position is left to the spent page and the flatten",
                key, refusals, report.walk.reason,
            )
            await self._audit.record("watchdog.stub_close_exhausted", detail)
            self._notify("watchdog_stub_close_exhausted", detail)
            return
        # An order reached (or was refused by) the broker, or the state was left
        # unconfirmed: the allowance stays spent. Nothing else is written — see
        # the docstring — and the executor's own by-outcome page plus the
        # repeating spent page carry the news.

    async def _page_profit_rejection(
        self, spread: OpenSpread, report: ExitReport
    ) -> None:
        """Tier 1 + page, ONCE per position: the broker rejected a closing order.
        (2026-09-23 review)

        A take that does not fill is otherwise silent, and should be — but a
        REJECTION is the broker refusing a CLOSE on this position, which is C5, and
        the emergency exit will send the same kind of order. Once per position
        (`_PROFIT_REJECTED` never falls): the second take's rejection is the same fact.
        STATE-based, never prose: the trigger is the walk's `REJECTED` outcome, and
        `reject_reason` is quoted, never matched.
        """
        key = spread.external_identifier
        if not self._escalation.rising(key, _PROFIT_REJECTED):
            return
        detail = {
            # Top level for `notify.alert_scope`'s per-position throttle (B1).
            "external_identifier": key,
            "candidate_id": spread.candidate_id,
            "contracts": spread.contracts,
            **spread.credit_payload(),
            "outcome": str(report.outcome),
            "reason": report.reason,
            "reject_reason": _alert_reject_reason(report.walk),
            "mutation_error": _alert_mutation_error(report.walk),
            "exit_attempt_intact": key not in self._attempted,
        }
        logger.error(
            "BROKER REJECTED A CLOSING ORDER: %s's profit take was rejected (%s) — the "
            "emergency exit will send the same kind of order; check the account's "
            "ability to close before the stop needs it",
            key, detail["reject_reason"] or report.reason,
        )
        await self._audit.record("watchdog.profit_target_rejected", detail)
        self._notify("watchdog_profit_target_rejected", detail)

    async def _note_exit_crash(
        self, spread: OpenSpread, exc: BaseException, *, profit: bool = False,
        stub: bool = False,
    ) -> None:
        """Tier 1 + page + halt ENTRIES for a crashed exit. One-shot per position.

        One-shot BY CONSTRUCTION, not by a latch: the spend in `_spend_on_crash` makes
        `check_once` refuse this position's `_trigger` for the rest of the process, so
        the only route back here is the 15:30 flatten override — one further send,
        which is itself a genuinely new event. The SPENT episode's repeating page is
        what carries the news afterwards.

        A crashed PROFIT TAKE (`profit=True`, 2026-09-23) comes here too, and is
        one-shot by the same construction one allowance over: `_spend_profit_target`
        makes `_may_act_on` strip the trigger for good. It still halts entries — an
        exception in the exit task is a defect whichever trigger started it — but it
        says what is actually true: the TAKE is spent, the emergency exit is not.
        """
        detail = {
            # The halt seam's surviving assertion from the pre-2026-09-10 test
            # (`detail["where"] == "exit"`), carried on the record and the page too.
            "where": "exit",
            # Top level for `notify.alert_scope`'s per-position throttle (B1).
            "external_identifier": spread.external_identifier,
            "candidate_id": spread.candidate_id,
            "contracts": spread.contracts,
            **spread.credit_payload(),
            "outcome": str(ExitOutcome.NOT_RESOLVED),
            # Both None, and carried LITERALLY rather than omitted: the AST sweep
            # (tests/test_watchdog_exit.py) governs any alert naming an outcome, and
            # "no broker was involved" is the truthful answer, not a missing field.
            "reject_reason": None,
            "mutation_error": None,
            "crash_error": f"{type(exc).__name__}: {exc}"[:500],
            "error_type": type(exc).__name__,
            # Which allowance the crash spent (2026-09-23). On a profit take it is the
            # take's, and the emergency exit is intact — the difference between "this
            # position has no automated close left" and "it has all of them". On a
            # stub continuation (2026-09-24) it is the stub's ONE allowance, already
            # spent at the handoff, and `_attempted` keeps describing the original
            # partial — which is why both flags ride rather than one.
            "profit_target": profit,
            "stub_close": stub,
            "exit_attempt_intact": spread.external_identifier not in self._attempted,
        }
        logger.critical(
            "EXIT CRASHED: the exit task for %s raised %s: %s — %s, entries halt, and "
            "the watchdog keeps watching",
            spread.external_identifier, type(exc).__name__, exc,
            (
                "the PROFIT TARGET is retired (the emergency exit keeps its attempt)"
                if profit
                else "the stub's ONE further close is spent (the spent row keeps "
                "describing the original partial)"
                if stub
                else "the attempt is spent (NOT_RESOLVED)"
            ),
            exc_info=exc,
        )
        # Suppressed for the same reason `_halt_on_exception` suppresses its record:
        # the recorder may be exactly what failed, and the page must not depend on it.
        with contextlib.suppress(Exception):
            await self._audit.record("watchdog.exit_crashed", detail)
        self._notify("watchdog_exit_crashed", detail)
        self._record_failure("exit", exc, spread.external_identifier)
        await self._halt_entries("exit", detail)

    def _spend_send(self, key: str) -> int:
        """Charge one closing send against `key`'s budget; return the RUNNING TOTAL.

        Called once per send that came back `NOTHING_AT_BROKER`, with the send it is
        charging already made — so the total it returns INCLUDES that send, and the
        caller's `< _EXIT_SEND_ATTEMPTS` is asking whether another one is allowed.
        Returning the count from before the increment instead would authorize a
        fourth send against a budget of three, which is the version this was written
        as and the test caught.

        Counting happens HERE and nowhere else, and it counts sends rather than
        triggers on purpose: a position whose trigger fires every cadence tick for an
        hour must not exhaust a budget that only exists to bound how many orders the
        broker is asked to take.
        """
        total = self._sends.get(key, 0) + 1
        self._sends[key] = total
        return total

    async def _page_if_no_exit_yet(
        self, key: str, tag: str, payload: dict[str, Any]
    ) -> None:
        """The bound on SILENCE for a triggered position with no order. (2026-08-20)

        The no-order paths — `_UNATTEMPTED` (construction refused the quote or the
        price), `_UNPRICEABLE` (the legs cannot be priced at all), since 2026-08-28
        `_NOTHING_SENT` (the send failed and the broker confirmed nothing landed), and
        since 2026-08-31 `_CROSS_UNPRICEABLE` (the abandoned order was confirmed
        terminal and the crossing limit had no offer to build from) — retry on every
        cadence tick, and that is correct: abandoning would
        strand an open 0DTE position the watchdog has already decided must be closed,
        and consuming the position's one attempt on an order that was never sent
        would be worse still. **What was NOT bounded was how long that can go on
        without saying so.** The last two paths' retries are additionally bounded by
        COUNT (one shared `_EXIT_SEND_ATTEMPTS`) because each one costs a submission;
        the first two send nothing and repeat until the feed recovers.
        Rising-edge escalation pages once and then drops to Tier 2 telemetry, so a
        condition that never clears is one page and then silence — measured, not
        assumed: neither path had any second escalation before this.

        So this is a REPEATING escalation, not a second one-shot edge. Every
        `watchdog.exit_unattempted_max_s` the condition survives, one further Tier 1
        record and one further page fire, naming how long the position has been
        triggered with no order in the market.

        **Repeating rather than once-only is the deliberate part.** The first version
        fired once per episode, reusing the rising-edge convention on the reasoning
        that a bound on silence must not become an alert storm. That reasoning argues
        for a bounded RATE, and a single event is not the only way to get one: a
        lone page is one missed notification away from restoring exactly the silence
        this exists to prevent, and for "an open 0DTE position cannot be closed" that
        is the wrong side to err on. The rising-edge precedents here — `_DEGRADED`
        above all — are conditions that are RECORDED and never paged; they are not
        the analogue for the most severe state this module can be in.

        The rate is bounded twice over, neither time by suppressing the repeat:
        `overdue` fires per INTERVAL rather than per cadence tick (~1 Tier 1 write per
        bound, so the writer lock spec §6 protects is never contended), and
        `SlackNotifier` throttles at 900s, folding the rest into a `(+N suppressed)`
        count that says "continuously" rather than "again". **Both bounds are
        per-POSITION** — `overdue` because `_Escalation` keys on the identifier, and
        the notifier since 2026-08-21 (B1), which is why `payload` carries
        `candidate_id` at the top level. Before that the notifier's key was
        the kind alone, so a second position crossing its own bound inside the same
        15 minutes — the correlated case, not the tail — was folded into the first
        position's suppressed count.

        Deliberately NOT a halt. `_halt_process` sets the stop event, which ends the
        watchdog's own loop: halting here would stop the retries that are the only
        thing still trying to close the position. The escalation a human can act on
        is a page, not a shutdown.
        """
        elapsed = self._escalation.overdue(key, tag, self._wd.exit_unattempted_max_s)
        if elapsed is None:
            return
        detail = {
            **payload,
            "condition": tag,
            "unresolved_for_s": round(elapsed, 3),
            "bound_s": self._wd.exit_unattempted_max_s,
        }
        logger.error(
            "EXIT STILL NOT IN THE MARKET: %s has been triggered with no order for "
            "%.1fs (%s) — the position is open and the retry is getting nowhere",
            key, elapsed, tag,
        )
        await self._audit.record("watchdog.exit_still_unplaced", detail)
        self._notify("watchdog_exit_still_unplaced", detail)

    async def _page_if_spent_and_still_held(
        self, spread: OpenSpread, spent: _SpentAttempt
    ) -> None:
        """The bound on SILENCE for a position with no exit attempt left. (2026-08-21)

        `_page_if_no_exit_yet` bounds the conditions where the watchdog wants out and
        no order was ever sent. This is the worst member of that family: an order WAS
        sent, it did not close the position, and `_attempted` now makes `check_once`
        refuse to send another for the rest of the process lifetime. Nothing automated
        is still trying to CLOSE it. The stop-loss condition has already fired, the
        position is 0DTE, and the only remaining path to flat is a human.

        **Since 2026-08-28 the position is still PRICED on every tick**, and this page
        carries that mark. The two facts an operator needs are "nothing will close
        this" and "here is what it is worth right now", and until then only the first
        one existed — a triggered spread went 76 minutes with no cost-to-close on any
        surface. Refusing to act and refusing to look were the same branch; they are
        not the same decision.

        The one-attempt rule itself is NOT relaxed here, and must not be. Its worst
        case is measured, not theoretical: `NEVER_WORKED` — the `working_timeout_s`
        path — calls `_request_cancel`, which is best-effort and confirms nothing, so
        the abandoned order is at `Cancel Requested` at best and can still fill.
        Re-sending on top of that closes the spread TWICE, which does not flatten it:
        it re-opens it inverted on expiry day. `UNRESOLVED` is worse still — nobody
        read the order at all. Both are exactly the states `_await_settled` refuses to
        cross against, and a retry is the same second closing order under a different
        name.

        **The 2026-08-28 split does not weaken that; it stops the rule being applied
        to a case it was never argued for.** Every sentence above turns on an order
        that may exist and cannot be seen. `NO_ORDER_EXISTS` is the outcome where the
        broker was asked and named none — so it retries, under a budget
        (`_EXIT_SEND_ATTEMPTS`), through the duplicate guard, and lands here only when
        the budget is out. Nothing that reached the broker gained an extra attempt.

        **2026-08-31 admits one more, on the same test and not on a looser one.**
        `CROSS_UNPRICEABLE` is reachable only after `_await_settled` has POSITIVELY
        confirmed the abandoned order terminal — the strongest answer this module ever
        gets, and stronger than `NO_ORDER_EXISTS`'s absence. It shares this budget and
        this terminus. `CROSS_REFUSED`, which is the same code path with that
        confirmation missing, is untouched and still spends on the first try.

        So the fix for "permanently unwatched" is not a retry. It is refusing to let
        the position go quiet:

        - **Repeating, per `watchdog.exit_unattempted_max_s`.** Reused rather than
          given a key of its own, and honestly so: the key's own comment describes
          its class as "long enough that an ordinary few-second quote gap resolves
          inside it, short enough that a triggered position nobody can close is on a
          phone while the trading day is still running", which is this condition
          verbatim. Reusing it also keeps this an image-only deploy.
        - **A clean fill does not page**, and it does not need excluding by outcome
          to avoid it: `positions` loses the row on the closing fill and the next
          refresh drops the spread from `_scan`, taking `_escalation.forget` with it.
          The bound is what makes that safe — booking lag was 4-5s in live fire,
          well inside 30s. The corollary is deliberate: a FILLED exit whose row is
          STILL there past the bound pages, because a fill that never booked or a
          partial close that left a stub is a position with real risk and nothing
          acting on it, and no other rail says that at this granularity.
        - **Not a halt**, for the same reason `_page_if_no_exit_yet` is not: halting
          sets the stop event and ends the watchdog, which would stop the other
          position being watched. What a human can act on is a page.
        - **`/status` says the same thing**, since 2026-08-21 (A2b). This page was
          the ONLY surviving signal until then — the position had already been
          popped out of `_impaired`, so the phone view rendered it as an ordinary
          open position. `spent_exit_attempts()` is the live read that fixes that,
          and it reports the duration from the same `_SpentAttempt.since` used
          below, so the two channels cannot disagree about how long this has run.
          Since 2026-08-28 it also carries the mark, off the same `_SpentMark` this
          page reads — the position is evaluated again, so `_impaired` is live for it
          too, and the two rows are once more a genuine before-and-after rather than
          a handover.
        """
        key = spread.external_identifier
        # Defensive re-arm. `_run_exit` arms the episode, but `_refresh_if_due` calls
        # `_escalation.forget` for anything that leaves the scan, and `_attempted`
        # deliberately keeps its key across that. A spread that flickers out of the
        # ledger and back would otherwise return with the slot cleared, and `overdue`
        # answers None for a slot that is not active — silent forever, which is the
        # one outcome this method exists to make impossible.
        self._escalation.rising(key, _SPENT)
        if self._escalation.overdue(key, _SPENT, self._wd.exit_unattempted_max_s) is None:
            return
        # The episode decides WHEN to page; `_SpentAttempt.since` decides what
        # duration is reported. They are the same number until the defensive re-arm
        # above fires, and after it the episode's is a cadence artefact — the attempt
        # was spent once, and "45 minutes ago" is what the operator has to act on.
        held_for_s = max(0.0, self._clock() - spent.since)
        mark = self._spent_marks.get(key)
        working = self._working_close(spread)
        detail = {
            "external_identifier": key,
            "candidate_id": spread.candidate_id,
            "contracts": spread.contracts,
            **spread.credit_payload(),
            "exit_outcome": str(spent.outcome),
            # The broker's own words for WHY the attempt was spent (2026-09-02). This
            # page names an outcome, so it owes the reason — see `_alert_reject_reason`.
            "reject_reason": spent.reject_reason,
            "mutation_error": spent.mutation_error,
            "crash_error": spent.crash_error,
            "held_for_s": round(held_for_s, 3),
            "bound_s": self._wd.exit_unattempted_max_s,
            # The live mark (2026-08-28). The page used to say only that the position
            # could not be closed, which is the least actionable half of the news: a
            # human deciding whether to leg out by hand needs to know what it is worth
            # NOW, and before this the position was not being priced at all. None
            # throughout means this tick could not price it — itself worth seeing.
            "cost_to_close": None if mark is None else mark.cost_to_close,
            "credit_stop": None if mark is None else mark.credit_stop,
            "spot": None if mark is None else mark.spot,
            "still_triggered": None if mark is None else mark.still_triggered,
            # The stub-close allowance's state (2026-09-24), so an operator
            # reading this page knows whether Argus still counts itself as
            # holding one further close — "available" with no order in the
            # market means the next tick is still trying, "sent"/"exhausted"
            # means the page is the only thing left besides the flatten.
            "stub_close": self._stub_close_state(key, spent),
            # A close STILL WORKING at the broker (2026-09-25). On 2026-09-24 this page
            # said nothing would try again 43 times while a crossing limit was resting
            # — the sentence that invites a manual second close on top of it (C5).
            # None when the registry names no working close.
            "working_close": None if working is None else {
                "broker_order_id": working.broker_order_id,
                "limit_price": working.limit_price,
                "working_for_s": round(working.working_for_s, 3),
            },
        }
        # "vs stop" only while the credit trigger can fire (2026-09-27, `page_view`).
        mark_phrase = _mark_phrase(
            detail["cost_to_close"], detail["credit_stop"],
            credit_trigger_enabled=self._thresholds.credit_trigger_enabled,
        )
        if working is not None:
            logger.error(
                "EXIT ATTEMPT SPENT AND STILL HELD: %s ended %s %.1fs ago; a CLOSING "
                "order is still working at the broker (#%s at %s for %.0fs) — check the "
                "broker BEFORE sending anything by hand (%s, still triggered=%s)",
                key, spent.outcome, held_for_s, working.broker_order_id,
                working.limit_price, working.working_for_s,
                mark_phrase, detail["still_triggered"],
            )
        else:
            logger.error(
                "EXIT ATTEMPT SPENT AND STILL HELD: %s ended %s %.1fs ago and the ledger "
                "still shows it open — nothing automated will try again; this one needs "
                "a human (%s, still triggered=%s)",
                key, spent.outcome, held_for_s,
                mark_phrase, detail["still_triggered"],
            )
        await self._audit.record("watchdog.exit_spent_still_held", detail)
        self._notify("watchdog_exit_spent_still_held", detail)

    def _reap_exits(self) -> None:
        for key in [k for k, t in self._exiting.items() if t.done()]:
            task = self._exiting.pop(key)
            # `_run_exit` catches everything, so a non-CancelledError exception here
            # would be a bug in that catch. Retrieved either way so it is never an
            # un-retrieved task exception.
            with contextlib.suppress(asyncio.CancelledError):
                exc = task.exception()
                if exc is not None:  # pragma: no cover - defensive
                    logger.critical("watchdog exit task for %s raised: %s", key, exc)

    async def _drain_exits(self) -> None:
        if not self._exiting:
            return
        tasks = list(self._exiting.values())
        self._exiting.clear()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- failure -----------------------------------------------------------

    def _record_failure(self, where: str, exc: BaseException, key: str | None = None) -> None:
        """THE only writer of `_failure`/`_failures`/`_affected`. In-memory, cannot fail.

        `_record_failure` does no I/O and raises nothing by construction, so it is
        safe to call at the top of a crash path — which is exactly where every caller
        needs it, before anything that could itself fail.
        """
        self._failure = _WatchdogFailure(
            where=where,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
            at=self._clock(),
            external_identifier=key,
        )
        self._failures += 1
        if key is not None:
            self._affected.add(key)

    async def _degrade(self, where: str, exc: BaseException, *, spread: OpenSpread | None = None) -> None:
        """Record the failure and count it against THIS pass. The edges are
        `_settle_pass`'s: this method never records, pages or halts, because it can
        be reached several times in one pass (once per poisoned position) and the
        episode must fire once."""
        self._record_failure(
            where, exc, spread.external_identifier if spread is not None else None
        )
        self._pass_failures += 1
        logger.error(
            "position watchdog degraded in %s (%s: %s) — entries halt on the pass "
            "edge; watching continues",
            where, type(exc).__name__, exc, exc_info=exc,
        )

    async def _settle_pass(self) -> None:
        """The episode's edges, once per pass. The ONE place the degradation halts.

        Called by `run()` after every pass (and by tests driving `check_once`
        directly). The failure branch fires `watchdog.degraded` Tier 1, the page and
        the `ENTRIES` halt on the RISING edge only — a defect that fails every pass
        runs to the close instead of parking the process, and re-halting per tick
        would buy nothing (`_halt_process` is idempotent) while paging per tick would
        be the storm `_Escalation` exists to prevent. The silence in between is
        bounded by `overdue` on `_DEGRADED_PAGE_INTERVAL_S`.

        The clean branch closes the episode: Tier 1 `watchdog.recovered` and one
        page, `_affected` cleared, `_last_clean_pass` stamped. **Entries stay
        HALTED** — the halt is permanent by design and only `POST /resume` clears
        it, and the page says so because a recovered watchdog with a HALTED badge is
        otherwise unexplainable.
        """
        now = self._clock()
        if self._pass_failures:
            failure = self._failure
            assert failure is not None  # `_pass_failures` implies `_record_failure`
            detail = {
                "where": failure.where,
                "error_type": failure.error_type,
                "error": failure.error,
                "failures": self._failures,
                "pass_failures": self._pass_failures,
                "affected": sorted(self._affected),
                "last_clean_pass_s": (
                    None if self._last_clean_pass is None
                    else round(now - self._last_clean_pass, 3)
                ),
            }
            if self._escalation.rising(_WATCHDOG_KEY, _WATCHDOG_FAILING):
                # Suppressed because the recorder may be exactly what failed; the
                # page and the halt below must happen either way — the same rule
                # `_halt_on_exception` follows.
                with contextlib.suppress(Exception):
                    await self._audit.record("watchdog.degraded", detail)
                self._notify("watchdog_degraded", detail)
                await self._halt_entries("check", detail)
            else:
                self._audit.telemetry("watchdog.degraded_still", detail)
                if (
                    for_s := self._escalation.overdue(
                        _WATCHDOG_KEY, _WATCHDOG_FAILING, _DEGRADED_PAGE_INTERVAL_S
                    )
                ) is not None:
                    self._notify(
                        "watchdog_degraded", {**detail, "degraded_for_s": round(for_s, 1)}
                    )
            return
        self._last_clean_pass = now
        since = self._escalation.active(_WATCHDOG_KEY, _WATCHDOG_FAILING)
        if since is None:
            return
        self._escalation.falling(_WATCHDOG_KEY, _WATCHDOG_FAILING)
        failure = self._failure
        detail = {
            "failures": self._failures,
            "where": failure.where if failure is not None else None,
            "error_type": failure.error_type if failure is not None else None,
            "error": failure.error if failure is not None else None,
            "degraded_for_s": round(now - since, 3),
            "affected": sorted(self._affected),
        }
        self._affected.clear()
        with contextlib.suppress(Exception):
            await self._audit.record("watchdog.recovered", detail)
        # Entries stay HALTED — the halt is permanent by design. The page says so,
        # because a recovered watchdog with a HALTED badge is otherwise unexplainable.
        self._notify("watchdog_recovered", detail)

    async def _halt_entries(self, where: str, detail: dict) -> None:
        """The narrow halt: kill switch + `HaltGrade.ENTRIES`, no stop event, no
        `request_stop`. `_halt_process` is idempotent, so repeats cost nothing.

        The grade is passed HERE rather than left to the wiring seam, because P3's
        own rule is that the grade is a property of the failure and not of the call
        site. `HaltGrade.tighten` makes this safe for free: an `ENTRIES` halt
        arriving after an `ALL` halt cannot relax it.
        """
        failure = self._failure
        assert failure is not None  # every caller records the failure first
        await self._halt(
            f"position watchdog failed in {where} "
            f"({failure.error_type}: {failure.error})",
            source=HALT_SOURCE,
            grade=HaltGrade.ENTRIES,
            detail=detail,
        )

    def watchdog_health(self) -> WatchdogHealth:
        """The watchdog's own health, right now. Live read for `/status`.

        In-memory only — no try/except, deliberately: the same contract
        `impaired_positions()` and `unwatchable_positions()` hold, because a
        swallowing reader would render a genuine bug in this method as the calm
        "WATCHING", which is the reassuring-when-wrong failure the row exists to
        remove. Safe to call from another task on the same loop.
        """
        now = self._clock()
        since = self._escalation.active(_WATCHDOG_KEY, _WATCHDOG_FAILING)
        failure = self._failure
        return WatchdogHealth(
            degraded=since is not None,
            watching=len(self._scan.spreads),
            failures=self._failures,
            where=None if failure is None else failure.where,
            error_type=None if failure is None else failure.error_type,
            error=None if failure is None else failure.error,
            degraded_for_s=None if since is None else max(0.0, now - since),
            last_failure_for_s=(
                None if failure is None else max(0.0, now - failure.at)
            ),
            last_clean_pass_s=(
                None if self._last_clean_pass is None
                else max(0.0, now - self._last_clean_pass)
            ),
            affected=tuple(sorted(self._affected)),
        )

    def _scan_age(self) -> float | None:
        """Seconds since the watch list was last successfully rebuilt, or None.

        THE seam behind `watchdog.exit_started`'s `scan_age_s` (§3.7): recorded,
        never decided on. None before the first successful `open_spreads`.
        """
        if self._last_scan_ok is None:
            return None
        return round(self._clock() - self._last_scan_ok, 3)

    async def _halt_on_exception(self, exc: BaseException, *, where: str) -> None:
        """Record, alert, and HALT THE PROCESS — the old strict posture, now reached
        only by the `AuditWriteError` exclusions (§3.5).

        By the time this runs, `AuditLog._fail` has already fired `on_failure` ->
        `Application._halt_on_audit_failure` -> `_halt_process(grade=ALL)` ->
        `request_stop()`, so the shared stop event is already set and the process is
        already parking; this method keeps the record, the page and the halt that
        today's path produced, verbatim.

        Order matters: the alert goes first because it is the one step that works
        with a dead audit log, then the Tier 1 record (suppressed if the recorder is
        what failed), then the halt. The halt is the same engage-kill-switch +
        request-stop pairing `Application._halt_process` gives the audit-failure and
        reconcile-mismatch paths, so there is one implementation of "stop".

        **An exit already in flight is NOT cancelled by this.** Its task keeps running
        to completion, and that is deliberate: it is trying to close a losing
        position, and killing it mid-walk could leave a working order at the exchange
        with nobody reporting on it. `_halt_process` only sets an Event — no forced
        exit — so the process winds down and lets it finish.

        Note for chunk 5: if the SAME `stop` event is passed to this watchdog and to
        the exit walks (which is the right wiring — it is how a session close reaches
        an in-flight walk), then setting it here makes those walks return `ABORTED`
        after requesting a cancel. That is the intended behaviour, not a side effect:
        chunk 3's abort path exists precisely so a walk in flight does not leave a
        working order behind when everything is stopping.
        """
        self._record_failure(where, exc)
        self._stop.set()
        detail = {
            "where": where,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "watching": [s.external_identifier for s in self._scan.spreads],
        }
        logger.critical(
            "position watchdog failed in %s (%s: %s) — halting the process; open "
            "positions must not be left unwatched",
            where, type(exc).__name__, exc, exc_info=exc,
        )
        self._notify("watchdog_fatal", detail)
        # Suppressed because the recorder may be exactly what failed; the halt below
        # must happen either way.
        with contextlib.suppress(Exception):
            await self._audit.record("watchdog.fatal", detail)
        await self._halt(
            f"position watchdog failed in {where} ({type(exc).__name__}: {exc})",
            source=HALT_SOURCE,
            detail=detail,
        )

    # -- small helpers -----------------------------------------------------

    async def _interruptible_sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def _notify(self, kind: str, detail: dict[str, Any]) -> None:
        # THE sink every watchdog page passes through, so the credit-stop rule is
        # applied here once rather than at 28 call sites (2026-09-27, `page_view`).
        # The journal line gets the same view: it is read as the page is.
        detail = page_view(
            detail, credit_trigger_enabled=self._thresholds.credit_trigger_enabled
        )
        logger.warning("%s: %s", kind, detail)
        self._alert(kind, detail)
