"""The exit handoff — construction, the walk, and the market-crossing escalation.

Drives the REAL `ExitExecutor` over the real `PriceWalkingLoop`,
`AuditedOrderExecution` and `FakeOrderExecution`, on a fake clock that advances only
when the loop sleeps. Nothing here touches a network.

The properties worth pinning are the ones that decide whether a losing position
actually gets closed:

  * the exit is built by chunk 1's `build.py` (one multi-leg order, signed debit);
  * chunk 3's loop is used UNMODIFIED, with only a different `WalkPolicy`;
  * WALKED_AWAY is escalated by THIS chunk, once, and never by looping back;
  * the cross is REFUSED rather than stacked on an order that can still fill;
  * the crossing price is the natural (ask/bid) cost, not a floored mid, and is
    capped at the spread width.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from argus.audit.log import AuditWriteError
from argus.execution.audited import AuditedOrderExecution
from argus.execution.fake import FakeOrderExecution
from argus.execution.guard import DuplicateOrderGuard
from argus.execution.loop import PriceWalk, PriceWalkingLoop, WalkOutcome, WalkPolicy
from argus.execution.models import (
    LegAction,
    LiveOrders,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
)
from argus.execution.store import InMemoryOrderStore, OpenSpread
from argus.execution.watchdog import (
    ExitExecutor,
    ExitOutcome,
    LegQuote,
    SpreadMarket,
    TriggerReason,
)
from argus.signal.contracts import OptionContract
from tests.support import make_config
from tests.test_watchdog import FakeAudit, FakeClock

SHORT_OCC = "SPXW  260729P05000000"
LONG_OCC = "SPXW  260729P04995000"
CANDIDATE = "20260729-put-5000-4995"
TRADING_DATE = date(2026, 7, 29)


# -- harness ------------------------------------------------------------------


class Broker(FakeOrderExecution):
    """`FakeOrderExecution` plus the timing the real API has and it lacks.

    `live_after_polls` is how many status reads an order sits at `Received` before it
    goes `Live` — the measured pre-market case. `on_poll` scripts what happens
    between reads, so a fill can be timed to a specific cycle.
    """

    def __init__(self, *, live_after_polls: int | None = 0) -> None:
        super().__init__()
        self.live_after_polls = live_after_polls
        self.on_poll: list = []
        self._seen: dict[int, int] = {}

    async def get_live_orders(self) -> LiveOrders:
        if self.live_after_polls is not None:
            for order_id, order in list(self._orders.items()):
                if order.status is OrderStatus.RECEIVED:
                    seen = self._seen.get(order_id, 0)
                    self._seen[order_id] = seen + 1
                    if seen >= self.live_after_polls:
                        self.set_status(order_id, OrderStatus.LIVE)
        # `self.submitted`, not `self._orders`: the seeded opening order (below)
        # makes `_orders` non-empty before the walk ever submits, and a scripted
        # poll firing during the GUARD's pre-submit look would act on an order
        # that does not exist yet. `submitted` is the old `_orders` proxy's
        # intent — "the walk has placed something" — stated directly.
        if self.on_poll and self.submitted:
            action = self.on_poll.pop(0)
            if action is not None:
                action(self)
        return await super().get_live_orders()

    @property
    def working_ids(self) -> list[int]:
        return [i for i, o in self._orders.items() if o.is_working]


class Quotes:
    """A `LegQuoteSource` scripted directly in spread terms."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.legs: dict[str, tuple[float, float]] = {}
        self.spot_price: float | None = 5100.0
        self.strikes = {SHORT_OCC: (5000.0, "put"), LONG_OCC: (4995.0, "put")}

    def set(self, short: tuple[float, float], long_: tuple[float, float]) -> None:
        self.legs = {SHORT_OCC: short, LONG_OCC: long_}

    def resolve(self, occ_symbol: str):
        from argus.execution.watchdog import LegResolution

        market = self.legs.get(occ_symbol)
        if market is None:
            return LegResolution(problem=f"no quote for {occ_symbol}")
        strike, right = self.strikes[occ_symbol]
        return LegResolution(
            leg=LegQuote(
                contract=OptionContract(
                    occ_symbol, f".{occ_symbol}", strike, right, TRADING_DATE
                ),
                bid=market[0],
                ask=market[1],
                received_at=self._clock.now,
                age_s=0.0,
                delta=-0.15,
            )
        )

    def spot(self) -> float | None:
        return self.spot_price

    def market(self) -> SpreadMarket:
        return SpreadMarket(
            short=self.resolve(SHORT_OCC).leg, long=self.resolve(LONG_OCC).leg
        )


def held(credit: str = "0.85", contracts: int = 2) -> OpenSpread:
    return OpenSpread(
        external_identifier=f"argus-open-{CANDIDATE}",
        broker_order_id=900,
        candidate_id=CANDIDATE,
        underlying="SPXW",
        short_symbol=SHORT_OCC,
        long_symbol=LONG_OCC,
        short_quantity=contracts,
        long_quantity=contracts,
        entry_credit=Decimal(credit),
    )


def _seed_opening_order(broker: Broker, *, contracts: int = 2, order_id: int = 900) -> None:
    """The FILLED opening order every held spread implies. (2026-09-24)

    A held position exists because an opening order filled, and the broker's
    rolling 24h window always carries that order on a same-day 0DTE — it is the
    `opened_filled` side of the guard's quantity rule. A fixture broker holding
    a spread with NO opening order is not the ordinary first close; it is the
    "absence is not a verdict" refusal (§4.7 of the stub-close design), so the
    harness places the order `held()` says is behind the position. Injected
    directly at `held()`'s own `broker_order_id` so it consumes no id the walk's
    scripted `fill(1000)`s depend on.
    """
    broker._orders[order_id] = Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg(SHORT_OCC, LegAction.SELL_TO_OPEN, contracts, remaining_quantity=0),
            OrderLeg(LONG_OCC, LegAction.BUY_TO_OPEN, contracts, remaining_quantity=0),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=f"argus-open-{CANDIDATE}",
        candidate_id=CANDIDATE,
        broker_order_id=order_id,
        status=OrderStatus.FILLED,
    )


def build(
    *,
    broker: Broker | None = None,
    config=None,
    short: tuple[float, float] = (2.00, 2.20),
    long_: tuple[float, float] = (0.30, 0.50),
    open_contracts: int = 2,
    residuals=None,
):
    """A real ExitExecutor over the real audited/walk stack and a fake broker."""
    broker = broker or Broker()
    _seed_opening_order(broker, contracts=open_contracts)
    audit = FakeAudit()
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    wrapped = AuditedOrderExecution(broker, audit=audit, store=InMemoryOrderStore())
    quotes = Quotes(clock)
    quotes.set(short, long_)
    alerts: list[tuple[str, dict]] = []
    config = config or make_config()
    executor = ExitExecutor(
        execution=wrapped,
        audit=audit,
        config=config,
        # As the trading session builds it (`pipeline.build_execution_stack`): the
        # CLOSE quantity rule rides `watchdog.stub_close_enabled`.
        guard=DuplicateOrderGuard(
            wrapped,
            audit=audit,
            close_quantity_rule=config.watchdog.stub_close_enabled,
        ),
        quotes=quotes,
        alert=lambda k, d: alerts.append((k, d)),
        sleep=sleep,
        clock=clock,
        trading_date=lambda: TRADING_DATE,
        residuals=residuals,
    )
    return executor, broker, audit, quotes, clock, alerts, slept


CREDIT_TRIGGER = frozenset({TriggerReason.CREDIT_MULTIPLE})


# -- construction: chunk 1's builder, not a second path ------------------------


async def test_the_exit_is_one_multi_leg_order_at_a_signed_debit():
    executor, broker, audit, quotes, _, _, _ = build()
    broker.on_poll = [lambda b: b.fill(1000)]

    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.outcome is ExitOutcome.FILLED
    sent = broker.submitted[0]
    # ONE order, TWO legs — the single-legging rail, enforced in build.py.
    assert len(sent.legs) == 2
    assert sent.direction is OrderDirection.CLOSE
    # Cost to close is 2.10 - 0.40 = 1.70, and closing PAYS it: a negative limit.
    assert sent.limit_price == Decimal("-1.70")
    assert sent.external_identifier == f"argus-close-{CANDIDATE}"
    assert sent.candidate_id == CANDIDATE
    assert sent.contracts == 2


async def test_the_exit_closes_only_what_is_actually_held():
    executor, broker, _, quotes, _, _, _ = build()
    broker.on_poll = [lambda b: b.fill(1000)]
    await executor.close(held(contracts=1), quotes.market(), CREDIT_TRIGGER)
    assert broker.submitted[0].contracts == 1


async def test_the_exit_uses_the_exit_walk_policy_not_the_entry_one():
    """Entry and exit differ ONLY in the policy — the state machine is untouched."""
    config = make_config()
    entry = WalkPolicy.for_entry(config)
    exit_ = WalkPolicy.for_exit(config)
    assert exit_.cadence_s != entry.cadence_s
    assert exit_.walk_away_pct == config.watchdog.exit_walk_away_pct
    # The COA settle window is assembled from the SAME two [coa] keys, once.
    assert exit_.coa_settle_s == entry.coa_settle_s


async def test_a_stale_quote_is_not_attempted_and_keeps_the_position_retryable():
    """`build_order`'s freshness bound applies to the exit too. Nothing is sent, so
    the watchdog gets to try again rather than burning the one attempt."""
    executor, broker, audit, quotes, clock, _, _ = build()
    snapshot = quotes.market()      # taken now ...
    clock.advance(60.0)             # ... and acted on far past market_data_max_age_s

    report = await executor.close(held(), snapshot, CREDIT_TRIGGER)
    assert report.outcome is ExitOutcome.NOT_ATTEMPTED
    assert "StaleSignalError" in report.reason
    assert broker.submitted == []
    # Not recorded here: the watchdog escalates this edge-triggered, because it can
    # recur on every cadence tick.
    assert "watchdog.exit_finished" not in audit.events


# -- the exit's freshness bound is the TRIGGER's, not the entry's (2026-08-20) --
#
# `execution.signal_max_age_s` (1.0) exists to keep an ENTRY's submitted price equal
# to the price the pre-trade gate approved. No such invariant exists on the exit
# path — nothing gates an exit — but exit construction was passing that value anyway,
# while the watchdog fires triggers against `staleness.market_data_max_age_s` (2.0).
# The 1.0-2.0s band therefore produced triggers that could never become orders.
# Today's live case: a 1.313s-old snapshot, trigger fired, no order built.


async def test_an_exit_builds_from_a_quote_the_entry_tolerance_would_have_refused():
    """The exact 2026-08-20 case: a 1.313s-old snapshot, inside the trigger's own
    2.0s bound and outside the entry-only 1.0s one. It must become an order."""
    config = make_config()
    assert config.execution.signal_max_age_s == 1.0
    assert config.staleness.market_data_max_age_s == 2.0

    executor, broker, _, quotes, clock, _, _ = build(config=config)
    broker.on_poll = [lambda b: b.fill(1000)]
    snapshot = quotes.market()
    clock.advance(1.313)            # the measured age, in the dead band

    report = await executor.close(held(), snapshot, CREDIT_TRIGGER)
    assert report.outcome is ExitOutcome.FILLED
    assert broker.submitted[0].limit_price == Decimal("-1.70")


async def test_the_exit_still_refuses_a_quote_past_the_triggers_own_bound():
    """Relaxed to the trigger's bound, not removed: past 2.0s the quote is stale by
    the same rule that decided the trigger, so there is nothing honest to price."""
    executor, broker, _, quotes, clock, _, _ = build()
    snapshot = quotes.market()
    clock.advance(2.001)

    report = await executor.close(held(), snapshot, CREDIT_TRIGGER)
    assert report.outcome is ExitOutcome.NOT_ATTEMPTED
    assert "StaleSignalError" in report.reason
    assert broker.submitted == []


async def test_a_worthless_spread_is_not_closed_by_guessing_a_price():
    """`build.py` refuses a mid that rounds to zero and says the decision belongs to
    this chunk. The answer is "not by inventing a tick"."""
    executor, broker, _, quotes, _, _, _ = build(short=(0.0, 0.0), long_=(0.0, 0.0))
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert report.outcome is ExitOutcome.NOT_ATTEMPTED
    assert broker.submitted == []


async def test_the_three_reasons_no_order_was_built_read_differently(monkeypatch):
    """(2026-09-02) ONE outcome, THREE sentences.

    One outcome because the allowance downstream asks exactly one question — *was
    anything sent* — and the answer is "no" for all three; splitting it would put a
    second rule back into the thing `_run_exit`'s restore exists to reduce to one.

    Three sentences because the operator's next move differs. The zero mid is a
    DESIGNED REFUSAL and the routine case near the close; a stale quote is latency that
    clears on its own; anything else out of `build_order` is a defect signature that
    repeats every tick and never clears. Before this, all three paged as
    `f"{type(exc).__name__}: {exc}"`, so the modal one read as a crash.
    """
    from argus.execution.build import StaleSignalError, UnpriceableSpreadError
    from argus.execution.watchdog import _refusal_prose

    unpriceable = _refusal_prose(UnpriceableSpreadError(0.0, Decimal("0")))
    stale = _refusal_prose(StaleSignalError(4.0, 2.0))
    defect = _refusal_prose(ValueError("signal credit does not match its own legs"))

    # The type prefix survives on every branch: `error_type`-shaped readers and existing
    # log filters key on it, so the prose is added after it, never instead of it.
    assert unpriceable.startswith("UnpriceableSpreadError:")
    assert stale.startswith("StaleSignalError:")
    assert defect.startswith("ValueError:")

    # And each says the thing its own reader needs.
    assert "will invent one" in unpriceable and "looks again" in unpriceable
    assert "4.000s past the 2s bound" in stale and "looks again" in stale
    assert "refused to build the exit order" in defect
    assert "looks again" not in defect, (
        "the defect branch must not borrow the self-resolving wording of the other two"
    )


async def test_the_worthless_spreads_page_names_the_refusal_not_a_crash():
    """The end-to-end half of the test above, on the path that actually produces it."""
    executor, broker, _, quotes, _, _, _ = build(short=(0.0, 0.0), long_=(0.0, 0.0))
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert report.outcome is ExitOutcome.NOT_ATTEMPTED
    assert report.reason.startswith("UnpriceableSpreadError:")
    assert "neither the walk nor the flatten will invent one" in report.reason
    assert broker.submitted == []


async def test_an_identity_mismatch_refuses_to_send():
    """If the resolved strikes are not the held position's, the legs being priced are
    not the legs being held."""
    import dataclasses

    executor, broker, _, quotes, _, _, _ = build()
    wrong = dataclasses.replace(held(), candidate_id="20260729-put-9999-9994")
    report = await executor.close(wrong, quotes.market(), CREDIT_TRIGGER)
    assert report.outcome is ExitOutcome.NOT_ATTEMPTED
    assert "does not match" in report.reason
    assert broker.submitted == []


# -- the guard --------------------------------------------------------------


async def test_the_duplicate_guard_runs_before_the_first_exit_submission():
    """The exit carries its own brand-new external_identifier, so the guard's rule
    applies exactly. The scenario: a crash mid-exit, a restart, the position still
    open — without the guard the retry stacks a second closing order."""
    executor, broker, audit, quotes, _, _, _ = build()
    broker.on_poll = [lambda b: b.fill(1000)]
    await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert "tier2:order.duplicate_check" in audit.events


async def test_a_close_already_working_blocks_the_exit_and_is_never_crossed():
    """`BLOCKED` on an exit means a close is ALREADY in flight — the right answer, and
    the one that must never be crossed on top of. This is the crash-mid-exit shape:
    the process restarts, the position is still open, the watchdog triggers again."""

    class NeverFills(Broker):
        """Leaves the previous close working so the guard has something to find."""

        async def cancel(self, broker_order_id: int) -> None:
            self.cancelled.append(broker_order_id)
            self.set_status(broker_order_id, OrderStatus.CANCEL_REQUESTED)

    executor, broker, audit, quotes, _, alerts, _ = build(broker=NeverFills())
    first = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert first.outcome is ExitOutcome.CROSS_REFUSED   # its own order still working
    submitted_before = len(broker.submitted)

    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert report.outcome is ExitOutcome.NOT_RESOLVED
    assert report.walk.outcome is WalkOutcome.BLOCKED
    assert report.cross_broker_order_id is None
    assert len(broker.submitted) == submitted_before   # nothing new was routed
    assert "order.duplicate_blocked" in audit.events


async def test_a_send_that_failed_with_nothing_at_the_broker_is_its_own_outcome():
    """2026-08-28, P0. The walk's `NO_ORDER_EXISTS` — send failed, broker asked,
    nothing named — must NOT land in the `NOT_RESOLVED` bucket with the outcomes
    whose justification is "a second order would stack onto something we cannot see".
    There is nothing to see, and collapsing the two spent a triggered 0DTE spread's
    only exit attempt on a broker that had answered."""
    class SendFails(Broker):
        """The send never lands, so the broker has nothing to report under the
        identifier. Scripted on `submit` rather than with `fail_next`, which the
        duplicate guard's own read would consume first (and raising there is a
        process halt, not a walk outcome)."""

        async def submit(self, order, *, dry_run=False):
            raise ConnectionError("socket")

    executor, broker, _, quotes, _, _, _ = build(broker=SendFails())

    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.walk.outcome is WalkOutcome.NO_ORDER_EXISTS
    assert report.outcome is ExitOutcome.NOTHING_AT_BROKER
    # Not crossed: crossing pays the market to guarantee closure, and nothing here
    # says the ordinary walk has stopped working.
    assert report.cross_broker_order_id is None


# -- the WALKED_AWAY escalation ------------------------------------------------


async def test_walking_away_submits_one_market_crossing_order():
    """Spec §7's exit variant, plus the guarantee this chunk adds: if the auction will
    not pay, pay the market rather than keep waiting or escalate to a human."""
    executor, broker, audit, quotes, _, alerts, _ = build()
    # Never fills, so the walk exhausts its ladder and abandons.
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.walk.outcome is WalkOutcome.WALKED_AWAY
    assert report.outcome is ExitOutcome.CROSSED
    assert report.cross_broker_order_id is not None
    # The natural is short ask - long bid = 2.20 - 0.30 = 1.90 — but the walk has
    # already bid 1.70 -> 2.05 and been refused all the way up (2026-09-25). Until then
    # this test pinned the cross at the natural, i.e. THREE ticks below a price the
    # market had just refused: Finding 1 of 2026-09-24, encoded as an expectation.
    # The long leg has a bid (0.30), so a leg-in is possible and the cross continues
    # the ladder by one rung: 2.05 + 0.05 = 2.10.
    assert report.walk.final_limit_price == Decimal("-2.05")
    assert report.cross_limit_price == Decimal("-2.10")
    assert "watchdog.crossed" in audit.events
    assert any(k == "watchdog_crossed_market" for k, _ in alerts)
    crossed = audit.payload("watchdog.crossed")
    assert crossed["limit_basis"] == "past_ladder"
    assert crossed["walked_away_at"] == -2.05
    assert crossed["opening_limit"] == -1.70


# -- the flatten's modal regime: a spread marked at one tick (2026-08-31) ------
#
# A winning 0DTE spread is nearly worthless by 15:30, which is the whole reason it is
# winning — so the flatten fires almost exclusively on exits whose opening limit is
# 0.05 or 0.10, and the multiplicative walk-away floor rounded back onto the initial
# for exactly those. Both flattens on 2026-08-31 walked away "after 0 step(s) at
# -0.05" and rode entirely on one instantaneous market cross; one found an offer and
# one did not, and the one that did not spent the position's only attempt.


def _near_tick(**kwargs):
    """Short 0.00/0.15, long 0.00/0.05 — a floored net mid of exactly one tick, and a
    crossing debit of 0.15 (the short's ask, the long's raw bid)."""
    return build(short=(0.0, 0.15), long_=(0.0, 0.05), **kwargs)


async def test_a_flatten_on_a_spread_marked_at_one_tick_still_has_a_ladder():
    executor, broker, _, quotes, _, _, _ = _near_tick()
    flatten = frozenset({TriggerReason.FLATTEN_BEFORE_CLOSE})

    report = await executor.close(held(), quotes.market(), flatten)

    assert broker.submitted[0].limit_price == Decimal("-0.05")
    # THE POINT: a reprice went out. Before the minimum this list was empty and the
    # walk abandoned after 0 steps.
    assert [str(o.limit_price) for _, o in broker.replaced] == ["-0.10"]
    assert report.walk.outcome is WalkOutcome.WALKED_AWAY
    assert report.walk.steps == 1
    # And the escalation is untouched: still one cross, still at the natural cost.
    assert report.outcome is ExitOutcome.CROSSED
    assert report.cross_limit_price == Decimal("-0.15")


async def test_the_near_tick_ladder_stays_far_inside_the_width_cap():
    """The extra range is bounded by arithmetic, not by trust: at
    `exit_walk_min_steps = 1` the floor is 0.10 against a 5.00 width, and the guard
    can only bind at all below a debit of 0.25.

    The cross keeps its own `min(debit, width)` rail regardless — that is the rail
    that turns a bad print into a bounded loss, and nothing here touches it. (The
    walk's OWN floor has no width cap; that is a separate, reported finding about
    large debits, where a purely multiplicative floor can already overshoot the
    width. It is unreachable from this minimum.)
    """
    executor, _, _, quotes, _, _, _ = _near_tick()
    policy = WalkPolicy.for_exit(make_config())
    walk = PriceWalk.from_policy(Decimal("-0.05"), policy)

    assert abs(walk.floor) == Decimal("0.10")
    assert abs(walk.floor) < Decimal(str(quotes.market().width))
    # Every rung, not just the floor.
    assert all(
        abs(price) < Decimal("5.00")
        for i in range(1, 10)
        if (price := walk.price_at(i)) is not None
    )


async def test_the_crossing_price_is_the_natural_not_a_floored_mid():
    """Flooring the long leg's zero bid UP would LOWER the debit and make the one
    order whose whole purpose is to fill less aggressive."""
    executor, broker, _, quotes, _, _, _ = build(short=(2.00, 2.20), long_=(0.0, 0.10))
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    # Natural: 2.20 ask - 0.00 bid = 2.20. A floored mid would have used 0.05.
    assert report.cross_limit_price == Decimal("-2.20")


async def test_the_crossing_price_is_capped_at_the_spread_width():
    """A vertical can never be worth more than the distance between its strikes, so a
    quote implying otherwise is a bad print. Paying it would turn a bounded loss into
    an unbounded one."""
    executor, _, _, quotes, _, _, _ = build(short=(9.00, 40.00), long_=(0.0, 0.10))
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert report.cross_limit_price == Decimal("-5.00")   # the 5000/4995 width


async def test_the_cross_is_refused_when_the_abandoned_order_can_still_fill():
    """`cancel()` leaves an order at `Cancel Requested`, not gone. A second closing
    order on one that can still fill closes the spread TWICE — which does not flatten
    it, it re-opens it inverted and unmanaged on expiry day."""

    class StubbornBroker(Broker):
        async def cancel(self, broker_order_id: int) -> None:
            # Accepts the request but the order stays working, forever.
            self.cancelled.append(broker_order_id)
            self.set_status(broker_order_id, OrderStatus.CANCEL_REQUESTED)

    executor, broker, audit, quotes, _, alerts, _ = build(broker=StubbornBroker())
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.walk.outcome is WalkOutcome.WALKED_AWAY
    assert report.outcome is ExitOutcome.CROSS_REFUSED
    assert report.cross_broker_order_id is None
    assert any(k == "watchdog_cross_refused" for k, _ in alerts)
    # Exactly one order was ever routed: the walk's. Nothing was stacked on it.
    assert len(broker.submitted) == 1


class VanishingBroker(Broker):
    """Cancels, then stops mentioning the trade idea at all — while it is still alive.

    The shape A3 is about (2026-08-21). `cancel()` leaves the order at
    `Cancel Requested`, where it can still fill; from that point the broker's answer
    carries nothing under the identifier, so `by_identifier` comes back EMPTY. The
    whole identifier rather than the one order because `get_live_orders()` reports
    today's orders including terminal ones, so the walk's replaced-and-cancelled
    rungs are in there too — an empty answer means the read lost the trade idea, not
    that one row went missing.

    `reappear_after_polls` closes the read gap: after that many blind reads the
    orders are back in the answer and terminal. Same scenario, differing only in
    whether a confirmation ever arrives.
    """

    def __init__(self, *, reappear_after_polls: int | None = None) -> None:
        super().__init__()
        self.hidden_identifier: str | None = None
        self.hidden_polls = 0
        self._reappear_after_polls = reappear_after_polls

    async def cancel(self, broker_order_id: int) -> None:
        self.cancelled.append(broker_order_id)
        # A cancel REQUEST: accepted, still working, still able to fill.
        cancelling = self.set_status(broker_order_id, OrderStatus.CANCEL_REQUESTED)
        self.hidden_identifier = cancelling.external_identifier

    async def get_live_orders(self) -> LiveOrders:
        if self.hidden_identifier is not None:
            self.hidden_polls += 1
            if (
                self._reappear_after_polls is not None
                and self.hidden_polls > self._reappear_after_polls
            ):
                for order_id, order in list(self._orders.items()):
                    if order.external_identifier == self.hidden_identifier and order.is_working:
                        self.set_status(order_id, OrderStatus.CANCELLED)
                self.hidden_identifier = None
        live = await super().get_live_orders()
        if self.hidden_identifier is None:
            return live
        return dataclasses.replace(
            live,
            orders=tuple(
                o for o in live.orders if o.external_identifier != self.hidden_identifier
            ),
        )


async def test_an_empty_broker_answer_refuses_the_cross_instead_of_reading_as_dead():
    """A3 (2026-08-21). `any()` over an empty list is False, so an order the broker
    never mentioned used to satisfy "nothing is working" and authorize the cross —
    against an order still at `Cancel Requested` that can still fill. That closes the
    spread TWICE: it does not flatten the position, it re-opens it inverted.

    Absence is a read gap, not a verdict — the reading `PriceWalkingLoop` already
    gives the identical signal two frames away."""
    executor, broker, audit, quotes, _, alerts, _ = build(broker=VanishingBroker())
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.walk.outcome is WalkOutcome.WALKED_AWAY
    assert report.outcome is ExitOutcome.CROSS_REFUSED
    assert report.cross_broker_order_id is None
    # THE assertion: exactly one order was ever routed — the walk's. Nothing was
    # stacked on the one the broker stopped talking about.
    assert len(broker.submitted) == 1
    assert "watchdog.crossed" not in audit.events
    # The scenario really did fire: read blind more than once, and the order the
    # broker stopped mentioning is STILL working and can still fill.
    assert broker.hidden_identifier is not None and broker.hidden_polls > 1
    assert broker.working_ids == [broker.cancelled[-1]]

    # Visible, and described as what it was: absent, not "still working". The two
    # refuse for the same reason but send an operator to different places.
    _, detail = next((k, d) for k, d in alerts if k == "watchdog_cross_refused")
    assert "absent" in detail["reason"] and "still working" not in detail["reason"]
    assert detail["reason"] == report.reason


class PartialAnswerBroker(Broker):
    """Reports the walk's earlier, genuinely-terminal rungs and omits the CURRENT one.

    The shape A3b is about (2026-08-21), and the same failure as A3 reached a
    different way. A price-walk exit reprices, and every rung mints a NEW broker id
    under the SAME `external_identifier`, so `by_identifier` legitimately returns
    several orders for one exit. `cancel()` leaves the final rung at
    `Cancel Requested`, where it can still fill — and from that point this broker's
    answer names only the abandoned earlier rungs, all `Cancelled`.

    So the answer is NON-EMPTY and every entry in it is terminal, which is exactly
    what A3's fix accepts as a confirmation. No unmappable status is involved (that
    is C2's route); the answer is simply incomplete.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hidden_order_id: int | None = None
        self.hidden_polls = 0

    async def cancel(self, broker_order_id: int) -> None:
        self.cancelled.append(broker_order_id)
        # A cancel REQUEST: accepted, still working, still able to fill.
        self.set_status(broker_order_id, OrderStatus.CANCEL_REQUESTED)
        self.hidden_order_id = broker_order_id

    async def get_live_orders(self) -> LiveOrders:
        live = await super().get_live_orders()
        if self.hidden_order_id is None:
            return live
        self.hidden_polls += 1
        return dataclasses.replace(
            live,
            orders=tuple(
                o for o in live.orders if o.broker_order_id != self.hidden_order_id
            ),
        )


async def test_a_partial_broker_answer_refuses_the_cross_even_though_it_is_not_empty():
    """A3b (2026-08-21). The route A3's fix left open: the broker's answer NAMES the
    walk's abandoned earlier rungs — every one of them genuinely terminal — and omits
    the current one, still at `Cancel Requested`. `dispositions` is non-empty and
    entirely terminal, so a list-wide check reads "properly dead, safe to cross" and
    stacks a second closing order on one that can still fill. Same catastrophic
    outcome as A3: it does not flatten the position, it re-opens it inverted.

    The fix is A3's rule at the right granularity — only `walk.broker_order_id`'s own
    disposition can authorize the cross."""
    executor, broker, audit, quotes, _, alerts, _ = build(broker=PartialAnswerBroker())
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.walk.outcome is WalkOutcome.WALKED_AWAY
    assert report.outcome is ExitOutcome.CROSS_REFUSED
    assert report.cross_broker_order_id is None
    # THE assertion: exactly one order was ever routed — the walk's.
    assert len(broker.submitted) == 1
    assert "watchdog.crossed" not in audit.events

    # The scenario really was the PARTIAL one, not A3's empty answer: the walk built
    # several rungs, the answer still names some of them, every named one is
    # terminal, and the one it omits is the order that can still fill.
    assert broker.replaced                      # more than one rung existed
    assert broker.hidden_polls > 1              # read blind more than once
    visible = [
        o
        for o in (await broker.get_live_orders()).orders
        if o.external_identifier == f"argus-close-{CANDIDATE}"
    ]
    assert visible                              # NOT empty — this is not A3's case
    assert all(o.is_terminal for o in visible)  # ... and all of it reads "dead"
    assert broker.working_ids == [broker.hidden_order_id]

    # Described as absent, not "still working": the operator has to chase a read as
    # well as an order, and the two send them to different places.
    _, detail = next((k, d) for k, d in alerts if k == "watchdog_cross_refused")
    assert "absent" in detail["reason"] and "still working" not in detail["reason"]
    assert detail["reason"] == report.reason


class RecordingBroker(Broker):
    """Keeps every answer `get_live_orders()` gave, so a test can assert on the one
    the settle window actually read rather than on the account afterwards."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: list[tuple[Order, ...]] = []

    async def get_live_orders(self) -> LiveOrders:
        live = await super().get_live_orders()
        self.answers.append(live.orders)
        return live


async def test_every_rung_present_and_terminal_still_confirms_and_crosses():
    """The other side of A3b's boundary: narrowing the check must not make a genuinely
    settled exit unresolvable. The ordinary walk-away shape — several rungs under one
    identifier, the current one among them, all terminal — is a confirmation, and the
    cross proceeds."""
    executor, broker, audit, quotes, _, _, _ = build(broker=RecordingBroker())
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.walk.outcome is WalkOutcome.WALKED_AWAY
    assert report.outcome is ExitOutcome.CROSSED
    assert report.cross_broker_order_id is not None
    assert "watchdog.crossed" in audit.events

    # The settle read is the last broker answer before the crossing order goes out.
    # Pin the shape it confirmed on: MORE THAN ONE order under this identifier — the
    # multi-rung case A3b is about — every one terminal, and the walk's own order
    # among them.
    settled_on = [
        o
        for o in broker.answers[-1]
        if o.external_identifier == f"argus-close-{CANDIDATE}"
    ]
    assert len(settled_on) > 1
    assert all(o.is_terminal for o in settled_on)
    assert report.walk.broker_order_id in {o.broker_order_id for o in settled_on}


async def test_a_terminal_answer_after_a_read_gap_still_settles_and_crosses():
    """The other side of the same boundary: the fix must not turn every answer into an
    uncertainty. The read gap closes inside the settle window, the broker NAMES the
    order and reports it terminal, and that confirmation is what the cross needs.
    Identical to the test above but for whether a confirmation ever arrives."""
    broker = VanishingBroker(reappear_after_polls=1)
    executor, broker, audit, quotes, _, _, _ = build(broker=broker)

    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.walk.outcome is WalkOutcome.WALKED_AWAY
    assert report.outcome is ExitOutcome.CROSSED
    assert report.cross_broker_order_id is not None
    assert len(broker.submitted) == 2      # the walk's, then the crossing order
    assert "watchdog.crossed" in audit.events
    assert broker.hidden_polls >= 1        # it really did go missing first


async def test_a_fill_that_crosses_the_cancel_is_reported_and_not_crossed():
    """The cancel and a fill can cross at the broker. `WalkResult` is a report, not a
    guarantee, and this is the case that proves why the settle read is necessary.

    **The NAME of the answer changed on 2026-09-10, and nothing else did.** The walk's
    own `_finish` now runs the identifier-wide FILLED read before it concludes
    `WALKED_AWAY` — the same question `_await_settled` asks a frame later, asked
    sooner — so the cross is never reached and the report is `FILLED` rather than
    `FILLED_LATE`. The design predicted the exit-side read would be *redundant*; on
    this path it is PRE-EMPTIVE, which is a reporting change the design did not
    anticipate. It is recorded as such rather than designed around, because carving
    `WALKED_AWAY` out on the exit path was the option sign-off explicitly declined.

    What did NOT change is everything load-bearing: nothing is crossed, exactly one
    order is sent, and `ExitOutcome.closed` is true either way. The distinction the
    old name carried — *the walk gave up and the order filled anyway* — is in the
    trail at Tier 2 as `walk.filled_under_identifier` with
    `suppressed_outcome: walked_away`.
    """

    class LateFillBroker(Broker):
        def __init__(self) -> None:
            super().__init__()
            self._cancelled_once = False

        async def cancel(self, broker_order_id: int) -> None:
            self.cancelled.append(broker_order_id)
            # The fill was already in flight when the cancel arrived.
            self.fill(broker_order_id)

    executor, broker, audit, quotes, _, _, _ = build(broker=LateFillBroker())
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.outcome is ExitOutcome.FILLED
    assert report.outcome.closed
    assert report.cross_broker_order_id is None
    assert len(broker.submitted) == 1
    # The fact the old name carried is still countable, one tier down.
    assert audit.payload("walk.filled_under_identifier")["suppressed_outcome"] == (
        "walked_away"
    )


async def test_an_unpriceable_cross_alerts_rather_than_sending_nothing_quietly():
    """It still alerts — but from `_record_exit`, not from a second notify of its own.

    The dedicated `watchdog_cross_unpriceable` call inside `_cross` was removed on
    2026-08-31 when this outcome became RETRYABLE: the fact an operator needs with it
    is which attempt this was and how many are left, and only the watchdog knows that.
    Alerting here as well would page three times for one event. What must not happen
    is the outcome going out silently, so this test now pins the alert that survives
    and the reason it carries.
    """
    executor, broker, _, quotes, _, alerts, _ = build()
    # The walk abandons, and by then the market has gone away entirely.
    original_resolve = quotes.resolve
    calls = {"n": 0}

    def resolve(symbol):
        calls["n"] += 1
        if calls["n"] > 2:      # the two lookups the initial build needs
            from argus.execution.watchdog import LegResolution

            return LegResolution(problem="feed gone")
        return original_resolve(symbol)

    quotes.resolve = resolve
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert report.outcome is ExitOutcome.CROSS_UNPRICEABLE
    assert not broker.submitted[1:], "nothing may be sent when the cross cannot price"
    unpriceable = [d for k, d in alerts if k == "watchdog_exit_cross_unpriceable"]
    assert len(unpriceable) == 1
    assert "no usable crossing price" in unpriceable[0]["reason"]
    # The dedicated one is gone; the watchdog raises this kind now, with the budget.
    assert not any(k == "watchdog_cross_unpriceable" for k, _ in alerts)


# -- outcomes that must NOT be crossed ----------------------------------------


async def test_a_rejected_exit_is_not_crossed():
    """A refusal is not "the auction did not pay enough", so it is not answered by
    paying more — a second order would be refused the same way."""
    executor, broker, audit, quotes, _, alerts, _ = build()
    broker.reject_next()
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.walk.outcome is WalkOutcome.REJECTED
    assert report.outcome is ExitOutcome.NOT_RESOLVED
    assert report.cross_broker_order_id is None
    assert len(broker.submitted) == 1
    assert any(k == "watchdog_exit_not_resolved" for k, _ in alerts)


async def test_an_order_that_never_reaches_the_market_is_not_crossed():
    """The measured pre-market case: accepted, `tif.next_valid_session`, sitting at
    `Received`. A crossing order would not reach the market either."""
    executor, broker, audit, quotes, _, _, _ = build(broker=Broker(live_after_polls=None))
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.walk.outcome is WalkOutcome.NEVER_WORKED
    assert report.outcome is ExitOutcome.NOT_RESOLVED
    assert report.cross_broker_order_id is None


async def test_an_unreachable_broker_at_the_guard_sends_nothing_and_does_not_halt():
    """G1. The 2026-09-03 incident, inverted. (Was: `..._propagates_and_halts`.)

    The old posture — a guard that cannot reach the broker halts the process and
    pages — fired for real at 11:04:05 ET on 2026-09-03, on ONE cert 502 on a GET,
    and what it halted was the watchdog: the triggered 0DTE spread it was closing
    lost its virtual stop, its mark and its 15:30 flatten for the rest of expiry day.

    Now: a bounded burst, then `NOT_ATTEMPTED`. Nothing routed (the invariant the old
    test also asserted, and the only one that was ever load-bearing), no exception
    out of `close`, and the reason NAMES the broker rather than reading like a
    builder defect.
    """

    class Unreachable(Broker):
        async def get_live_orders(self):
            raise ConnectionError("broker unreachable")

    executor, broker, audit, quotes, _, _, _ = build(broker=Unreachable())
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.outcome is ExitOutcome.NOT_ATTEMPTED
    # Nothing was routed, and no outcome was invented to paper over the unknown.
    assert broker.submitted == []
    assert "duplicate guard could not be asked" in report.reason
    assert "ConnectionError" in report.reason
    # Every attempt of the burst is Tier 2; giving up is the caller's Tier 1.
    assert audit.events.count("tier2:walk.guard_unreachable_attempt") == 3
    attempts = [p for c, p in audit.payloads if c == "walk.guard_unreachable_attempt"]
    assert [p["attempt"] for p in attempts] == [1, 2, 3]
    assert {p["error_type"] for p in attempts} == {"ConnectionError"}


async def test_a_guard_that_answers_on_the_second_attempt_exits_normally():
    """G2. The burst is for the BLIP, and the blip is the common case.

    A cert 502 that clears inside a second must cost the exit nothing at all — not
    the attempt, not the flatten override, and certainly not the process. The verdict
    the retry produces is an ORDINARY verdict: the walk proceeds and fills.
    """

    class FlakyOnce(Broker):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        async def get_live_orders(self):
            self.reads += 1
            if self.reads == 1:
                raise ConnectionError("502 Bad Gateway")
            return await super().get_live_orders()

    executor, broker, audit, quotes, _, _, _ = build(broker=FlakyOnce())
    broker.on_poll = [lambda b: b.fill(1000)]
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.outcome is ExitOutcome.FILLED
    assert len(broker.submitted) == 1
    # One failed attempt, recorded; the burst then answered and the walk was ordinary.
    assert audit.events.count("tier2:walk.guard_unreachable_attempt") == 1


async def test_a_dead_recorder_is_never_retried_by_the_guard_burst():
    """G6. The one exception type that is load-bearing, and it is an EXCLUSION.

    `has_conflicting_order` writes Tier 1 `order.duplicate_blocked` from inside
    itself, so an `AuditWriteError` surfaces from what looks like a pure read. A dead
    recorder halts the process and must never be re-attempted (CLAUDE.md) — so it is
    re-raised untouched, ahead of everything else, and still reaches the watchdog's
    halt. The burst absorbs an unreachable BROKER, never a broken LOG.
    """

    class DeadLog(Broker):
        async def get_live_orders(self):
            raise AuditWriteError("chain write failed")

    executor, broker, audit, quotes, _, _, _ = build(broker=DeadLog())
    with pytest.raises(AuditWriteError):
        await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    # Not retried, and not converted into a refusal.
    assert audit.events.count("tier2:walk.guard_unreachable_attempt") == 0
    assert broker.submitted == []


async def test_a_guard_burst_never_reaches_the_submit_path():
    """G3. The invariant the burst must not weaken: retry the QUESTION, never send.

    `NOT_ATTEMPTED` from a spent burst is a refusal, not a licence. Nothing may reach
    the wire without an answered `ConflictVerdict`, however many times the question
    was asked — so a broker that refuses every read must produce zero submissions no
    matter how long the burst runs.
    """

    class Unreachable(Broker):
        async def get_live_orders(self):
            raise ConnectionError("broker unreachable")

    executor, broker, audit, quotes, _, _, _ = build(broker=Unreachable())
    for _ in range(5):
        report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
        assert report.outcome is ExitOutcome.NOT_ATTEMPTED
    assert broker.submitted == []


async def test_an_unaskable_guard_writes_no_walk_records_at_all():
    """G4. A question asked before anything was sent did not start a walk.

    On 2026-09-03 the trail read `walk.started: 11` against `walk.finished: 10`, and
    the missing one was the walk that never happened — the guard raised after
    `walk.started` had already been written. The record now sits BELOW the guard, so
    an unaskable guard leaves the pair balanced.
    """

    class Unreachable(Broker):
        async def get_live_orders(self):
            raise ConnectionError("broker unreachable")

    executor, broker, audit, quotes, _, _, _ = build(broker=Unreachable())
    await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert [e for e in audit.events if e.startswith("walk.")] == []
    # The exit episode is still visible — it just did not become a walk.
    assert "watchdog.exit_started" in audit.events


async def test_a_blocked_guard_still_pairs_its_walk_records():
    """G5. The other side of G4, and the regression the reordering could cause.

    `BLOCKED` is written by `_finish`, so moving `walk.started` below the guard could
    have produced a `walk.finished` with no `walk.started`. The record sits above the
    `blocked` return specifically so it does not.
    """

    class NeverFills(Broker):
        """Leaves the first close working, so the second one's guard finds it."""

        async def cancel(self, broker_order_id: int) -> None:
            self.cancelled.append(broker_order_id)
            self.set_status(broker_order_id, OrderStatus.CANCEL_REQUESTED)

    executor, broker, audit, quotes, _, _, _ = build(broker=NeverFills())
    await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    submitted_before = len(broker.submitted)
    audit.events.clear()

    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.walk.outcome is WalkOutcome.BLOCKED
    assert report.outcome is ExitOutcome.NOT_RESOLVED
    assert [e for e in audit.events if e.startswith("walk.")] == [
        "walk.started",
        "walk.finished",
    ]
    assert len(broker.submitted) == submitted_before


async def test_a_walk_that_loses_the_broker_after_submitting_is_not_crossed():
    """UNRESOLVED means "I do not know what exists" — the one outcome from which
    nothing may be inferred, so certainly not "send another order"."""

    class DropsAfterSubmit(Broker):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        async def get_live_orders(self):
            self.reads += 1
            if self.reads > 1:      # the guard's pre-submit look succeeds
                raise ConnectionError("broker went away mid-walk")
            return await super().get_live_orders()

    executor, broker, audit, quotes, _, alerts, _ = build(broker=DropsAfterSubmit())
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert report.walk.outcome is WalkOutcome.UNRESOLVED
    assert report.outcome is ExitOutcome.NOT_RESOLVED
    assert report.cross_broker_order_id is None
    assert any(k == "watchdog_exit_not_resolved" for k, _ in alerts)


# -- recording ----------------------------------------------------------------


async def test_every_attempted_exit_is_recorded_tier_1_and_alerted():
    executor, broker, audit, quotes, _, alerts, _ = build()
    broker.on_poll = [lambda b: b.fill(1000)]
    await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert "watchdog.exit_started" in audit.events
    assert "watchdog.exit_finished" in audit.events
    # A stopped-out position is what an operator wants on their phone.
    assert any(k == "watchdog_exit_filled" for k, _ in alerts)


async def test_the_recorded_walk_away_price_is_the_one_the_walk_will_use():
    executor, broker, audit, quotes, _, _, _ = build()
    broker.on_poll = [lambda b: b.fill(1000)]
    await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    started = audit.payload("watchdog.exit_started")
    # -1.70 conceding 20% is -2.04, which `PriceWalk` snaps to the tick: -2.05. The
    # recorded number is the walk's OWN floor, not a recomputation, so it cannot
    # disagree with where the walk actually stops.
    assert started["walk_away_price"] == pytest.approx(-2.05)
    assert started["trigger_reasons"] == ["credit_multiple"]


async def test_exit_started_carries_the_walk_range_AND_the_offer():
    """Where the walk opens, where it stops, and the offer it walks toward — all on
    this one Tier 1 row, the offer off the SAME `market` the walk was handed.
    (2026-09-18)

    The merge briefly replaced this copy with a join to the triggering row. That join
    is exact only on the trigger's rising edge (Tier 1 `watchdog.triggered`); every
    later exit on a still-triggered position is preceded by Tier 2
    `watchdog.triggered_still`, which is queued (its `seq` can land after this row)
    and droppable. So the fact lives here, durable and ordered.
    """
    executor, broker, audit, quotes, _, _, _ = build()
    market = quotes.market()
    broker.on_poll = [lambda b: b.fill(1000)]
    await executor.close(held(), market, CREDIT_TRIGGER)

    started = audit.payload("watchdog.exit_started")
    assert started["order"]["limit_price"] == pytest.approx(-1.70)
    assert started["walk_away_price"] == pytest.approx(-2.05)
    assert started["crossing_debit"] is not None
    assert started["crossing_debit"] == pytest.approx(market.crossing_debit)


async def test_a_nonfinite_quote_never_reaches_exit_started_at_all():
    """Why the `is_finite` guard on that field is defensive rather than load-bearing.

    `crossing_debit` and `cost_to_close` are derived from the SAME four numbers, so a
    non-finite bid or ask makes the mid non-finite too and `build_order` refuses before
    anything is recorded. The guard stays because the two derivations could diverge
    later; the reachable `None` is on `TriggerEvaluation.crossing_debit`, where the
    market can be absent entirely — which is what a degraded tick looks like.
    """
    executor, broker, audit, quotes, _, _, _ = build(
        short=(2.00, float("nan")), long_=(0.30, 0.50)
    )
    broker.on_poll = [lambda b: b.fill(1000)]
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert report.outcome is ExitOutcome.NOT_ATTEMPTED
    assert "watchdog.exit_started" not in audit.events


async def test_the_exit_refuses_an_unrecorded_execution_stack():
    """Chunk 3's structural refusal still applies to the exit: an order placed without
    a ledger row is unaccounted for at the next restart, which hard-halts
    sandbox/live."""
    broker = Broker()
    audit = FakeAudit()
    unrecorded = AuditedOrderExecution(broker, audit=audit)   # no store=
    with pytest.raises(ValueError, match="store="):
        PriceWalkingLoop(
            execution=unrecorded,
            audit=audit,
            policy=WalkPolicy.for_exit(make_config()),
        )


# -- what happens AFTER the one attempt is spent (2026-08-21, A2) --------------
#
# The two tests above pin the "do not cross" half of `NEVER_WORKED`, and that half
# is unchanged. What was never pinned is the half that follows: `NOT_RESOLVED` adds
# the position to `PositionWatchdog._attempted`, `check_once` skips `_attempted`
# keys for the rest of the process lifetime, and the only thing that had ever been
# said about it was one `watchdog_exit_not_resolved` alert at the moment it
# happened. A triggered 0DTE position, permanently unevaluated, silent.
#
# These drive the REAL executor over the REAL walk INTO a real `PositionWatchdog`,
# so the whole chain is exercised: trigger -> order sent -> status never confirms
# within `working_timeout_s` -> spent attempt -> repeating page.


def _wire(broker: Broker):
    """A real `ExitExecutor` (over the real walk) inside a real `PositionWatchdog`."""
    from argus.execution.store import OpenSpreadScan
    from tests.test_watchdog import build_watchdog

    executor, broker, audit, quotes, clock, alerts, _ = build(
        broker=broker,
        # Cost to close 2.30 - 0.40 = 1.90, clear of the 1.70 credit stop
        # (0.85 x credit_multiple 2.0) rather than sitting exactly on it.
        short=(2.20, 2.40),
    )
    watchdog, audit, alerts, halts = build_watchdog(
        clock=clock,
        quotes=quotes,
        scan=OpenSpreadScan(spreads=(held(),)),
        executor=executor,
        # ONE trail across both sides, so the ordering of the exit's own records and
        # the watchdog's escalation is expressible.
        audit=audit,
        alerts=alerts,
    )
    return watchdog, broker, audit, alerts, halts, clock


async def _tick(watchdog, clock, seconds: float) -> None:
    clock.advance(seconds)
    await watchdog.check_once()
    await watchdog._drain_exits()


async def test_an_exit_whose_status_never_confirms_does_not_go_silent():
    """THE reproduction. The order is accepted and then sits at `Received` forever,
    so `_await_in_market` burns `working_timeout_s`, requests a cancel it cannot
    confirm, and reports NEVER_WORKED -> NOT_RESOLVED. That spends the position's one
    exit attempt while the stop-loss condition is still true and the position is
    still held.

    The one-attempt rule is correct and stays: the cancel is best-effort, so the
    abandoned order is at `Cancel Requested` at best and can still fill, and a second
    closing order on top of it re-opens the spread inverted on expiry day. What must
    not stay is the silence — the escalation has to keep saying so."""
    watchdog, broker, audit, alerts, halts, clock = _wire(Broker(live_after_polls=None))

    for _ in range(3):                          # debounce (sustained_s 2.0), then fire
        await _tick(watchdog, clock, 1.5)

    # The walk really ran: one order out, never live, and the exit gave up on it.
    assert len(broker.submitted) == 1
    assert audit.payload("watchdog.exit_finished")["outcome"] == "not_resolved"
    assert [k for k, _ in alerts].count("watchdog_exit_not_resolved") == 1
    # And the position is now skipped by `check_once` — the state this test is about.
    assert list(watchdog._attempted) == [held().external_identifier]
    assert watchdog._attempted[held().external_identifier].outcome is ExitOutcome.NOT_RESOLVED
    assert "watchdog.exit_spent_still_held" not in audit.events

    await _tick(watchdog, clock, 25.0)          # past exit_unattempted_max_s (20.0)

    page = audit.payload("watchdog.exit_spent_still_held")
    assert page["external_identifier"] == held().external_identifier
    # The outcome is IN the page: "the walk timed out" and "the cross was refused"
    # are different next moves for whoever picks this up.
    assert page["exit_outcome"] == "not_resolved"
    assert page["held_for_s"] >= 20.0
    assert page["bound_s"] == 20.0
    assert [k for k, _ in alerts].count("watchdog_exit_spent_still_held") == 1
    # A page, never a halt: halting ends the watchdog, and the OTHER position in a
    # two-position book still needs watching.
    assert halts == []


async def test_a_never_worked_exit_over_a_prior_fill_is_a_fill_end_to_end():
    """T12 (2026-09-10). The test above, with the one fact that changes everything:
    a rung under the SAME `argus-close-*` key has already filled.

    Identical inputs to `test_an_exit_whose_status_never_confirms_does_not_go_silent`
    otherwise — the order is accepted and sits at `Received` forever, so
    `_await_in_market` burns `working_timeout_s` and concludes `NEVER_WORKED`. Until
    2026-09-10 that concluded about the SPREAD from a fact about one RUNG, and the
    position paid for it twice: recorded as an exit that failed, and skipped by
    `check_once` for the rest of the process lifetime with a repeating page, while
    being flat the whole time.

    **What this pins is the whole chain, not the walk's own field.** `_after_walk`
    handles `FILLED` first, so the corrected outcome never reaches `NOT_RESOLVED`,
    never crosses, and never raises `watchdog_exit_not_resolved`.

    On the bookkeeping below: a FILLED exit still writes `_attempted` and still arms
    the `_SPENT` episode — that branch means "an order reached the broker", which is
    true here and true of every terminal outcome. What it does NOT do is page,
    because the closing fill books and the row leaves the ledger (the same property
    `test_a_clean_fill_that_books_never_pages` pins). The change is which outcome is
    recorded there, not whether the slot is written.
    """
    from argus.execution.store import OpenSpreadScan

    broker = Broker(live_after_polls=None)

    def plant_prior_fill(b: Broker) -> None:
        """The rung this walk had already replaced, sitting filled at the broker."""
        prior = dataclasses.replace(b.submitted[0], broker_order_id=None)
        b.fill(b._place(prior).broker_order_id)

    broker.on_poll = [plant_prior_fill]
    watchdog, broker, audit, alerts, halts, clock = _wire(broker)

    for _ in range(3):                          # debounce (sustained_s 2.0), then fire
        await _tick(watchdog, clock, 1.5)

    # One closing order out — the correction is a READ, never a second send.
    assert len(broker.submitted) == 1
    assert audit.payload("watchdog.exit_finished")["outcome"] == "filled"
    assert audit.payload("walk.finished")["outcome"] == "filled"
    assert audit.payload("walk.filled_under_identifier")["suppressed_outcome"] == (
        "never_worked"
    )
    # Not an exit failure, so none of the failure surfaces fire.
    assert not [k for k, _ in alerts if k == "watchdog_exit_not_resolved"]
    assert watchdog._attempted[held().external_identifier].outcome is ExitOutcome.FILLED

    watchdog._spreads.scan = OpenSpreadScan()   # the closing fill books; the row goes
    for _ in range(10):                         # 10 x 30s, far past the 20s bound
        await _tick(watchdog, clock, 30.0)

    assert "watchdog.exit_spent_still_held" not in audit.events
    assert not [k for k, _ in alerts if k == "watchdog_exit_spent_still_held"]
    assert len(broker.submitted) == 1
    assert halts == []


async def test_the_spent_attempt_page_repeats_and_never_re_sends_an_order():
    """Matching the `overdue()` design: it repeats while the condition holds, it is
    bounded to roughly one per interval rather than one per tick, and it does NOT
    turn into an unbounded retry — the broker sees exactly the one order the single
    walk sent, however long the escalation runs."""
    watchdog, broker, audit, alerts, _, clock = _wire(Broker(live_after_polls=None))

    for _ in range(3):
        await _tick(watchdog, clock, 1.5)
    assert len(broker.submitted) == 1

    for _ in range(60):                         # 60 x 10s = 10 minutes held
        await _tick(watchdog, clock, 10.0)

    pages = [k for k, _ in alerts].count("watchdog_exit_spent_still_held")
    assert audit.events.count("watchdog.exit_spent_still_held") == pages
    # Bounded by the 20s interval, not by the 0.5s cadence: ~30 in ten minutes, and
    # emphatically not the 60 ticks — nor the once-only version.
    assert 25 <= pages <= 35, f"expected ~one page per 20s bound, got {pages}"
    # The whole point of not retrying. One order, forever.
    assert len(broker.submitted) == 1
    # Each page says how long, so the trail reads as a duration rather than a repeat.
    durations = [d["held_for_s"] for k, d in alerts
                 if k == "watchdog_exit_spent_still_held"]
    assert durations == sorted(durations) and durations[-1] >= 570.0


# -- the page must carry the broker's own explanation (2026-09-02) -------------
#
# On 2026-09-01 the first exit REJECTION in the system's history paged with
# `loop.py`'s hardcoded fallback string while the broker's own words —
# `'Received a fill on the original order'`, the sentence saying the exit had
# already SUCCEEDED — sat in the Tier 1 record. P6 (2026-08-29) added
# `WalkResult.reject_reason` for exactly this, landed it on the entry path (which
# alerts `**result.to_payload()`, so it arrived for free) and silently missed the
# exit path, whose alert dicts are hand-built.


async def test_a_rejected_exit_pages_with_the_brokers_own_words():
    executor, broker, audit, quotes, _, alerts, _ = build()
    broker.reject_next("Received a fill on the original order")

    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert report.outcome is ExitOutcome.NOT_RESOLVED
    _, detail = next((k, d) for k, d in alerts if k == "watchdog_exit_not_resolved")
    assert detail["reject_reason"] == "Received a fill on the original order"
    # B1's throttle scope is unaffected: `notify.alert_scope` reads `candidate_id` at
    # the TOP level, and two correlated stop-outs must not fold into one page.
    assert detail["candidate_id"] == CANDIDATE
    # Not the nested walk payload — this goes to a phone.
    assert "walk" not in detail


async def test_an_exit_with_no_rejection_carries_none_not_an_empty_string():
    """The discipline `WalkResult.reject_reason` itself keeps. "" and None must not
    read as two different states on a field whose whole job is "was there a reason"."""
    executor, broker, audit, quotes, _, alerts, _ = build()
    broker.on_poll = [None, lambda b: b.fill(1000)]

    await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    _, detail = next((k, d) for k, d in alerts if k == "watchdog_exit_filled")
    assert detail["reject_reason"] is None


async def test_the_repeating_spent_page_carries_the_reason_too():
    """The repeating page is the one an operator is most likely to actually read: it
    survives a missed or throttled notification, where the one-shot at `_record_exit`
    does not. It names an outcome, so it owes the reason for it."""
    watchdog, broker, audit, alerts, _, clock = _wire(Broker())
    broker.reject_next("Received a fill on the original order")

    for _ in range(3):                          # debounce, then fire
        await _tick(watchdog, clock, 1.5)
    assert audit.payload("watchdog.exit_finished")["outcome"] == "not_resolved"

    await _tick(watchdog, clock, 25.0)          # past exit_unattempted_max_s (20.0)

    page = audit.payload("watchdog.exit_spent_still_held")
    assert page["exit_outcome"] == "not_resolved"
    assert page["reject_reason"] == "Received a fill on the original order"
    _, detail = next((k, d) for k, d in alerts if k == "watchdog_exit_spent_still_held")
    assert detail["reject_reason"] == "Received a fill on the original order"


def test_the_unpacked_report_payload_really_carries_the_reject_reason():
    """The escape hatch the AST test below allows must not rot.

    The sibling alerts (`watchdog_exit_unattempted`, the retry pages,
    `watchdog_exit_still_unplaced`) satisfy the rule by spreading
    `**report.to_payload()`, which carries the nested `walk`. If `to_payload` ever
    stops carrying it, those surfaces go quiet again and the structural test would
    still pass — so this is the assertion that closes it.
    """
    from argus.execution.loop import WalkResult
    from argus.execution.watchdog import ExitReport

    walk = WalkResult(
        outcome=WalkOutcome.REJECTED,
        external_identifier="argus-close-x",
        broker_order_id=1,
        steps=0,
        requested_contracts=2,
        filled_contracts=0,
        final_limit_price=Decimal("-1.50"),
        cancel_requested=False,
        recovered_disconnects=0,
        reject_reason="Received a fill on the original order",
    )
    payload = ExitReport(
        outcome=ExitOutcome.NOT_RESOLVED,
        external_identifier="argus-close-x",
        reasons=(TriggerReason.CREDIT_MULTIPLE,),
        walk=walk,
    ).to_payload()
    assert payload["walk"]["reject_reason"] == "Received a fill on the original order"


async def test_exit_started_carries_the_scan_age():
    """T13 (2026-09-10). §3.7's residual risk, recorded rather than silenced: if
    `open_spreads` keeps failing while the audit log keeps writing, the watchdog
    could act on a stale watch list. `scan_age_s` is the one number that would
    evidence it, and nothing decides on it."""
    executor, broker, audit, quotes, clock, _, _ = build()
    broker.on_poll = [lambda b: b.fill(1000)]

    # Before any successful scan — the unbound, standalone-executor case — the
    # field is PRESENT and None, never omitted.
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert report.outcome is ExitOutcome.FILLED
    broker.on_poll = [lambda b: b.fill(1000)]

    # Bound to a live reading (what `PositionWatchdog.__init__` binds beside
    # `bind_quotes`), it is carried through to the record.
    executor.bind_scan_age(lambda: 3.25)
    await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    records = [p for c, p in audit.payloads if c == "watchdog.exit_started"]
    assert [r["scan_age_s"] for r in records] == [None, 3.25]


# -- the structural half: a convention is exactly what failed here ------------


def _resolve_detail(node: ast.expr, scope: ast.AST) -> tuple[set[str], bool] | None:
    """What an alert's detail contains: (literal keys, carries a whole payload).

    `None` means the shape could not be read at all, which the caller treats as a
    hole in the rule rather than as a pass.

    A bare `x.to_payload()` counts as carrying a payload: every `to_payload` in this
    package is the object's full view, and
    `test_the_unpacked_report_payload_really_carries_the_reject_reason` is what stops
    that escape hatch rotting.

    **`carries_payload` requires the `**` to BE a `to_payload()`, and until 2026-09-04
    it did not** — any `**` at all satisfied it. `_record_exit`'s hand-built alert
    unpacks `**spread.credit_payload()`, which is four price fields and can never
    carry a broker reason, so the one surface this whole rule was written for was
    passing on an unrelated splat. It carried `reject_reason` literally and was
    correct in fact; the SWEEP was not enforcing it, and deleting that key left the
    test green. Found by extending the rule to a second field, which is the argument
    for extending rules by sweep rather than by hand.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return (set(), True) if node.func.attr == "to_payload" else None
    if isinstance(node, ast.Name):
        found = None
        for stmt in ast.walk(scope):
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Dict):
                if any(
                    isinstance(t, ast.Name) and t.id == node.id for t in stmt.targets
                ):
                    found = stmt.value
        node = found
    if not isinstance(node, ast.Dict):
        return None
    return (
        {k.value for k in node.keys if isinstance(k, ast.Constant)},
        any(
            key is None
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "to_payload"
            for key, value in zip(node.keys, node.values)
        ),
    )


def _alert_sites(path: Path):
    """(kind source, resolved detail, function name) for every self._notify/_alert."""
    tree = ast.parse(path.read_text())
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr in ("_notify", "_alert")
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            ):
                continue
            if len(call.args) < 2:
                continue
            detail = call.args[1]
            # `_notify(kind, detail)` inside the generic sink itself: both are
            # parameters, and the real sites are its callers.
            if isinstance(detail, ast.Name) and detail.id in params:
                continue
            yield ast.unparse(call.args[0]), _resolve_detail(detail, fn), fn.name


def test_not_attempted_can_never_describe_an_order_that_reached_the_broker():
    """T5. The premise the flatten-allowance restore rests on. (2026-09-02)

    `_run_exit` gives a position its flatten override back on
    `ExitOutcome.NOT_ATTEMPTED` and on nothing else, because that value means *no order
    was built, so none was sent*. Two things make that safe and only one of them is
    visible at the restore:

    - a crash is an EXCEPTION and never reaches the restore at all — the spend stays at
      the handoff precisely so a task that dies leaves the allowance spent;
    - and `NOT_ATTEMPTED` is a VALUE that only construction-refusal sites can produce.

    The second is a property of `ExitExecutor.close`'s SHAPE, not of this module, so it
    is pinned here rather than trusted to a comment: every `ExitOutcome.NOT_ATTEMPTED`
    in `close` must be lexically above the walk it would otherwise be describing. Add
    one below `self._loop_factory().run(...)` and the restore silently starts handing
    back overrides for orders that reached the exchange.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "argus" / "execution"
    tree = ast.parse((src / "watchdog.py").read_text())
    close = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "close"
    )
    runs = [
        n.lineno
        for n in ast.walk(close)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run"
    ]
    assert len(runs) == 1, "the walk is started in exactly one place in `close`"
    refusals = [
        n.lineno
        for n in ast.walk(close)
        if isinstance(n, ast.Attribute)
        and n.attr == "NOT_ATTEMPTED"
        and isinstance(n.value, ast.Name)
        and n.value.id == "ExitOutcome"
    ]
    assert refusals, "an empty sweep would pass vacuously"
    assert all(line < runs[0] for line in refusals), (
        f"a NOT_ATTEMPTED at {[n for n in refusals if n > runs[0]]} sits BELOW the walk "
        f"at {runs[0]} — it could describe an order that reached the broker, and "
        "`_run_exit` restores the flatten override on exactly that value"
    )

    # The ONE route that cannot satisfy the lexical rule, because an `except` clause
    # cannot sit above what it wraps (2026-09-03). Its proof is the same in substance
    # — the helper has no submit path — so it is asserted rather than exempted, and
    # the sweep is closed: `close` may reach `NOT_ATTEMPTED` through exactly this one
    # helper and no other call below the walk.
    helper = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_guard_unreachable_report"
    )
    assert not [
        n
        for n in ast.walk(helper)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"run", "submit", "replace_order", "close"}
    ], "_guard_unreachable_report must contain no path that can reach the broker"
    assert not [n for n in ast.walk(helper) if isinstance(n, ast.Await)], (
        "_guard_unreachable_report must stay synchronous — an await is how a submit "
        "path gets back into a site the flatten restore trusts to have sent nothing"
    )
    below = [
        ast.unparse(n.func)
        for n in ast.walk(close)
        if isinstance(n, ast.Call) and n.lineno > runs[0]
        and isinstance(n.func, ast.Attribute)
        and n.func.attr.startswith("_")
    ]
    assert set(below) <= {
        "self._guard_unreachable_report",
        "self._after_walk",
        "self._record_exit",
    }, f"an unreviewed helper is called below the walk in `close`: {sorted(set(below))}"


def test_every_alert_that_names_an_outcome_carries_the_brokers_reason():
    """**What a per-position alert owes, pinned rather than remembered.**

    P6 put `reject_reason` on `WalkResult` and on the entry path, and the exit path
    missed it silently for three days until an exit was rejected for the first time.
    A convention is what failed; this is the mechanism that replaces it.

    The rule: any `self._notify` / `self._alert` whose alert KIND or whose detail
    keys name an outcome must also carry `reject_reason` — literally, or by carrying
    a whole payload that does (which
    `test_the_unpacked_report_payload_really_carries_the_reject_reason` pins).

    **The residual gap, stated rather than hidden:** an alert whose detail cannot be
    read at all is only failed when its KIND names an outcome. One whose kind is
    silent AND whose detail is unreadable would slip — so the rule is enforced where
    the reading is possible, and the unreadable shapes are the ones to add here.
    It caught a surface the design pass had missed (`watchdog_flatten_override`,
    which names `spent_outcome`) on its first run, which is the point.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "argus" / "execution"
    checked = []
    for module in ("watchdog.py", "entry.py"):
        for kind, detail, fn_name in _alert_sites(src / module):
            kind_names_outcome = "outcome" in kind.lower()
            assert detail is not None or not kind_names_outcome, (
                f"{module}:{fn_name} pages an outcome with a detail this test cannot "
                "read — add the shape to `_resolve_detail` rather than leaving the "
                "rule unenforced on it"
            )
            if detail is None:
                continue
            keys, carries_payload = detail
            if not (kind_names_outcome or any("outcome" in k for k in keys)):
                continue
            checked.append(f"{module}:{fn_name}")
            # TWO fields, not one (2026-09-04). The broker refuses an order in two
            # different ways and only one of them was ever surfaced: `reject_reason`
            # for an order it ACCEPTED and then rejected, `mutation_error` for one it
            # would not accept at all. The second reported as an ABSENCE
            # (`no_order_exists`, `reject_reason: null`) on 5 of 7 entry attempts on
            # 2026-09-03, which is why it is swept here rather than added quietly:
            # this rule exists precisely because the last field added on a convention
            # missed three surfaces.
            for field in ("reject_reason", "mutation_error"):
                assert field in keys or carries_payload, (
                    f"{module}:{fn_name} pages an outcome without `{field}` — the "
                    f"broker's account of it (keys: {sorted(keys)})"
                )
    # The rule is only worth having if it is actually reaching the surfaces the
    # incident was about — an empty sweep would pass vacuously.
    assert "watchdog.py:_record_exit" in checked
    assert "watchdog.py:_page_if_spent_and_still_held" in checked
    assert "watchdog.py:_flatten_may_act" in checked
    # T15 (2026-09-10): the crash page names an outcome, so it owes the broker's
    # two sentences — carried literally as None, because no broker was involved
    # is the truthful answer, not a missing field.
    assert "watchdog.py:_note_exit_crash" in checked
    assert any(name.startswith("entry.py:") for name in checked)
