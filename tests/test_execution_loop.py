"""The price-walking loop (spec §7) — timing, sequencing, and giving up.

No network anywhere: every test drives `FakeOrderExecution` through the same
`AuditedOrderExecution` wrapper production uses, on a fake clock that advances
only when the loop sleeps. A test that waited real seconds would be a test nobody
runs, and the whole subject here is waiting.

The assertions that matter most are the ones about ORDER of operations — that the
cadence clock does not start on the ack, that a cancel appears only on the
walk-away/abandon paths, that the duplicate guard runs exactly once — because
those are the properties a reader cannot check by looking at the code and the
ones a refactor would break silently.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from argus.execution.audited import AuditedOrderExecution
from argus.execution.build import build_order
from argus.execution.fake import FakeOrderExecution
from argus.execution.guard import DuplicateOrderGuard
from argus.execution.loop import (
    LiveOrderView,
    PriceWalk,
    PriceWalkingLoop,
    WalkOutcome,
    WalkPolicy,
    _Walk,
)
from argus.execution.models import (
    CONFIRMED_IN_MARKET,
    LegAction,
    LiveOrders,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
    UnmappableOrderError,
    is_in_market,
)
from argus.execution.store import InMemoryOrderStore
from argus.execution.zerobid import TICK
from argus.signal.models import OptionQuote, SpreadSignal
from tests.support import make_config

OURS = "argus-open-20260729-put-5000-4995"


# -- harness ------------------------------------------------------------------


class FakeAudit:
    """Records categories in order so sequencing can be asserted."""

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


class FakeTime:
    """A clock that only moves when the loop sleeps. Deterministic by construction."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Broker(FakeOrderExecution):
    """`FakeOrderExecution` plus the one behaviour the real API has and it lacks:
    an order that reaches the market some time AFTER it is acknowledged.

    `live_after_polls` is how many status reads an order sits at `Received` before
    it goes `Live` — the measured pre-market case at its limit (`None` = never).
    `on_poll` scripts what happens between reads, so a fill can be timed to a
    specific cycle.
    """

    def __init__(self, *, live_after_polls: int | None = 0) -> None:
        super().__init__()
        self.live_after_polls = live_after_polls
        self.polls = 0
        self.on_poll: list = []
        self._seen: dict[int, int] = {}
        #: 1-based replace call numbers that raise. `fail_next` cannot express this
        #: — it fires on whichever port call comes next, which is a status read.
        self.replace_fails_at: set[int] = set()
        #: Replace calls that perform the replace and THEN raise: the timeout that
        #: took effect, which is the case a replay would get wrong.
        self.replace_fails_after_landing_at: set[int] = set()
        self.replace_calls = 0
        #: 1-based replace call -> (status, reason, fill the original, contracts).
        #: See `refuse_replace`.
        self.replace_refusals: dict[int, tuple[OrderStatus, str | None, bool, int | None]] = {}

    def refuse_replace(
        self,
        call: int,
        *,
        reason: str | None = None,
        status: OrderStatus = OrderStatus.REJECTED,
        original_fills: bool = True,
        contracts: int | None = None,
    ) -> None:
        """The 2026-09-01 race: the ORIGINAL fills, so the replacement is refused.

        Observed in cert at 18:42:12 — `'Received a fill on the original order'` on
        broker order 1430433 while 1430426 filled 2 contracts. The fake's ordinary
        `replace` terminates the original, which is right for the ordinary case and
        is exactly what this race is not: the original is the order that survives.

        `original_fills=False` scripts a refusal that means what it says — the
        broker declined the replacement and nothing filled anywhere.
        """
        self.replace_refusals[call] = (status, reason, original_fills, contracts)

    async def replace(self, broker_order_id: int, order: Order) -> Order:
        self.replace_calls += 1
        if self.replace_calls in self.replace_fails_at:
            raise ConnectionError("replace never reached the broker")
        refusal = self.replace_refusals.get(self.replace_calls)
        if refusal is not None:
            status, reason, original_fills, contracts = refusal
            if original_fills:
                self.fill(broker_order_id, contracts)
            self.replaced.append((broker_order_id, order))
            new = self._place(
                dataclasses.replace(order, replaces_order_id=broker_order_id)
            )
            refused = dataclasses.replace(
                self._orders[new.broker_order_id], status=status, reject_reason=reason
            )
            self._orders[new.broker_order_id] = refused
            return refused
        replaced = await super().replace(broker_order_id, order)
        if self.replace_calls in self.replace_fails_after_landing_at:
            raise TimeoutError("replace landed but the response was lost")
        return replaced

    def plant_filled_sibling(
        self, contracts: int | None = None, limit: str = "0.85"
    ) -> Order:
        """A SECOND filled order under the same `external_identifier`.

        The broker does not enforce identifier uniqueness (measured, chunk 1), so
        this is a real state and not a contrivance — it is what `_recover`'s
        two-working-orders branch and the exit retry both produce.

        `limit` exists so a test can tell the two rungs apart by PRICE, which is the
        only way to catch a fix that moves `state.sent` along with `state.last`
        (2026-09-10). A limit is not a fill price, and the walk's report must keep
        saying which rung's limit it is quoting.
        """
        planted = self._place(spread(limit))
        return self.fill(planted.broker_order_id, contracts)

    async def get_live_orders(self) -> LiveOrders:
        self.polls += 1
        if self.live_after_polls is not None:
            for order_id, order in list(self._orders.items()):
                if order.status is OrderStatus.RECEIVED:
                    seen = self._seen.get(order_id, 0)
                    self._seen[order_id] = seen + 1
                    if seen >= self.live_after_polls:
                        self.set_status(order_id, OrderStatus.LIVE)
        # Scripted actions happen TO orders, so the duplicate guard's pre-submit
        # look at an empty account does not consume one.
        if self.on_poll and self._orders:
            action = self.on_poll.pop(0)
            if action is not None:
                action(self)
        return await super().get_live_orders()

    @property
    def live_ids(self) -> list[int]:
        return [i for i, o in self._orders.items() if o.is_working]


def spread(limit: str = "0.85", direction: OrderDirection = OrderDirection.OPEN) -> Order:
    short, long_ = (
        (LegAction.SELL_TO_OPEN, LegAction.BUY_TO_OPEN)
        if direction is OrderDirection.OPEN
        else (LegAction.BUY_TO_CLOSE, LegAction.SELL_TO_CLOSE)
    )
    return Order(
        direction=direction,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", short, 2),
            OrderLeg("SPXW  P4995", long_, 2),
        ),
        limit_price=Decimal(limit),
        external_identifier=OURS,
        candidate_id="20260729-put-5000-4995",
    )


def policy(**overrides) -> WalkPolicy:
    base = dict(
        cadence_s=3.0,
        coa_settle_s=1.0,
        step_ticks=1,
        walk_away_pct=0.20,
        # The ENTRY shape, so the shared helper leaves the multiplicative floor
        # exactly as it was. Tests that care about the minimum pass their own.
        min_concession_steps=0,
        working_timeout_s=10.0,
        status_poll_interval_s=0.5,
        max_recovery_attempts=3,
    )
    base.update(overrides)
    return WalkPolicy(**base)


def build_loop(
    broker: Broker, *, audit=None, guard=True, stop=None, view_factory=None, **kwargs
):
    audit = audit or FakeAudit()
    wrapped = AuditedOrderExecution(broker, audit=audit, store=InMemoryOrderStore())
    clock = FakeTime()
    if view_factory is not None:
        # The audited/throttled stack is built in here, so a test that needs to
        # instrument or break the VIEW gets it handed the same wrapper production
        # would give it, rather than reaching around the helper.
        kwargs["view"] = view_factory(wrapped)
    loop = PriceWalkingLoop(
        execution=wrapped,
        audit=audit,
        policy=kwargs.pop("policy", None) or policy(),
        guard=DuplicateOrderGuard(wrapped, audit=audit) if guard else None,
        stop=stop,
        sleep=clock.sleep,
        clock=clock.clock,
        **kwargs,
    )
    return loop, audit, clock


# -- the one state machine, two parameterizations -----------------------------


async def test_entry_and_exit_are_the_same_machine_with_different_policies():
    """Chunk 4's exit is a different `WalkPolicy`, not a second implementation.

    An exit walks a NEGATIVE limit (a debit) and concedes by paying more, while an
    entry walks a positive one and concedes by collecting less. Both are "subtract
    a tick" because of tastytrade's sign convention, which is why the machine has
    no direction branch — the same code produces both ladders.
    """
    entry_broker = Broker()
    entry, _, _ = build_loop(entry_broker, policy=policy(cadence_s=3.0))
    await entry.run(spread("0.85"), guard_first_submission=True)

    exit_broker = Broker()
    exit_loop, _, exit_clock = build_loop(
        exit_broker, policy=policy(cadence_s=1.0, walk_away_pct=0.20)
    )
    await exit_loop.run(spread("-0.85", OrderDirection.CLOSE), guard_first_submission=False)

    entry_prices = [str(o.limit_price) for _, o in entry_broker.replaced]
    exit_prices = [str(o.limit_price) for _, o in exit_broker.replaced]
    assert entry_prices == ["0.80", "0.75", "0.70"]     # collect less
    assert exit_prices == ["-0.90", "-0.95", "-1.00"]   # pay more
    # And the exit ran on its own cadence, clamped up to the auction floor.
    assert exit_clock.slept.count(1.0) >= 3


async def test_every_walked_price_is_tick_aligned_through_zerobid():
    """Repricing goes through `zerobid.round_to_tick`, the one implementation.

    Built from a real `SpreadSignal` via chunk 1's `build_order`, so the whole
    path — floored mid -> gated limit -> every walked price — is exercised.
    """
    short = OptionQuote("SPXW  P4975", 4975.0, "put", -0.15, 1.20, 1.40)
    long_ = OptionQuote("SPXW  P4970", 4970.0, "put", -0.10, 0.0, 0.30)  # zero bid
    signal = SpreadSignal(
        underlying="SPXW", side="put", short_leg=short, long_leg=long_,
        credit=short.floored_mid - long_.floored_mid, width=5.0, captured_at=100.0,
    )
    order = build_order(
        signal, direction=OrderDirection.OPEN, contracts=1,
        trading_date=date(2026, 7, 29), now=100.0, max_signal_age_s=1.0,
    )
    broker = Broker()
    loop, _, _ = build_loop(broker)
    await loop.run(order, guard_first_submission=True)

    prices = [order.limit_price] + [o.limit_price for _, o in broker.replaced]
    assert prices, "the walk should have stepped at least once"
    for price in prices:
        assert price % TICK == 0, f"{price} is not a whole tick"


# -- property 1: the clock starts in the market, not at the ack ---------------


def test_only_live_is_a_confirmed_in_market_status():
    """The set is deliberately one value, and `RECEIVED` is deliberately not in it.

    Chunk 1 measured a pre-market submission sitting at `Received` with
    `tif.next_valid_session` — accepted, and not in the market. Widening this set
    is a measurement, not an edit; this test is what makes that explicit.
    """
    assert CONFIRMED_IN_MARKET == frozenset({OrderStatus.LIVE})
    assert not is_in_market(OrderStatus.RECEIVED)
    assert not is_in_market(None)
    assert is_in_market(OrderStatus.LIVE)


async def test_cadence_does_not_start_until_the_order_is_in_the_market():
    """The failure this prevents: walking an order nobody can see.

    The order sits at `Received` for three status reads. Not one reprice may
    happen in that time, and the first full cadence wait must come after it goes
    `Live` — otherwise the first step would cancel an order that never had a
    chance to fill.
    """
    broker = Broker(live_after_polls=3)
    loop, audit, clock = build_loop(broker)
    reprices_while_received = []
    broker.on_poll = [lambda b: reprices_while_received.append(len(b.replaced))] * 3

    await loop.run(spread(), guard_first_submission=True)

    assert reprices_while_received == [0, 0, 0]
    # Three poll intervals of waiting for the market, then the cadence.
    assert clock.slept[:4] == [0.5, 0.5, 0.5, 3.0]


async def test_a_replaced_order_must_reach_the_market_again():
    """A replace mints a NEW order (verified against sandbox, chunk 1), so it has
    to get to the exchange on its own before its cycle can be counted."""
    broker = Broker(live_after_polls=1)
    loop, _, clock = build_loop(broker)
    await loop.run(spread(), guard_first_submission=True)

    # Every price — the first and each replacement — paid one poll interval to
    # reach the market before its cadence started.
    assert clock.slept.count(0.5) == 1 + len(broker.replaced)


async def test_an_order_that_never_works_is_cancelled_not_left():
    """The measured pre-market case. `tif.next_valid_session` means the order goes
    live at the NEXT open, unattended, at a price nobody is walking — so timing
    out must cancel, never just walk away and forget it."""
    broker = Broker(live_after_polls=None)   # never reaches Live
    loop, audit, _ = build_loop(broker)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.NEVER_WORKED
    assert result.cancel_requested
    assert broker.cancelled == [1000]
    assert broker.replaced == []
    assert "order.cancel_requested" in audit.events


async def test_a_fill_counts_as_in_market_even_if_live_is_never_observed():
    """A fill is proof of exposure. Requiring `Live` first would hang on an order
    that filled inside the first auction."""
    broker = Broker(live_after_polls=None)
    broker.on_poll = [lambda b: b.fill(1000)]
    loop, _, _ = build_loop(broker)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2


# -- property 2: a cycle is at least one full COA settle window ---------------


def test_cycle_wait_combines_response_window_and_extended_processing():
    """Both figures, not the response interval alone (spec §7).

    Cancelling at the end of the response interval races a straggler auction
    response the exchange is still processing — the cancel and a fill cross.
    """
    config = make_config(coa={"response_window_ms": 100, "extended_processing_ms": 900})
    assert WalkPolicy.for_entry(config).coa_settle_s == pytest.approx(1.0)


def test_a_cadence_tighter_than_the_auction_is_raised_to_it():
    """A 1s exit cadence against a 1s settle window must wait the window."""
    fast = policy(cadence_s=0.25, coa_settle_s=1.0)
    assert fast.cycle_wait_s == 1.0
    assert fast.clamped_by_coa
    assert policy(cadence_s=3.0, coa_settle_s=1.0).cycle_wait_s == 3.0


async def test_the_loop_actually_holds_for_the_clamped_window():
    broker = Broker()
    loop, _, clock = build_loop(broker, policy=policy(cadence_s=0.25, coa_settle_s=1.0))
    await loop.run(spread(), guard_first_submission=True)
    assert 0.25 not in clock.slept
    assert clock.slept.count(1.0) >= 1


# -- wiring: the audited path, with a store ----------------------------------


def test_the_raw_adapter_cannot_be_handed_to_the_loop():
    """Chunk 1's adapter writes no pre-send audit record and no ledger row."""
    with pytest.raises(TypeError, match="AuditedOrderExecution"):
        PriceWalkingLoop(
            execution=FakeOrderExecution(),  # type: ignore[arg-type]
            audit=FakeAudit(), policy=policy(),
        )


def test_an_audited_execution_without_a_store_is_refused():
    """The silent, delayed failure: orders placed with no ledger row are
    unaccounted for at the next restart, and order reconciliation hard-halts
    sandbox/live on exactly that — the morning after."""
    audit = FakeAudit()
    without_store = AuditedOrderExecution(FakeOrderExecution(), audit=audit)
    assert not without_store.records_orders
    with pytest.raises(ValueError, match="store="):
        PriceWalkingLoop(execution=without_store, audit=audit, policy=policy())


async def test_every_order_the_walk_places_reaches_the_ledger():
    broker = Broker()
    audit = FakeAudit()
    store = InMemoryOrderStore()
    wrapped = AuditedOrderExecution(broker, audit=audit, store=store)
    clock = FakeTime()
    loop = PriceWalkingLoop(
        execution=wrapped, audit=audit, policy=policy(),
        guard=DuplicateOrderGuard(wrapped, audit=audit),
        sleep=clock.sleep, clock=clock.clock,
    )
    result = await loop.run(spread(), guard_first_submission=True)

    # The first submission and every replacement — the whole chain, because each
    # reprice mints a new broker id that reconciliation would otherwise not know.
    assert len(store.orders) == 1 + len(broker.replaced)
    assert result.broker_order_id in store.orders


# -- the duplicate guard: exactly once, before the first submit --------------


class CountingGuard(DuplicateOrderGuard):
    def __init__(self, execution, **kwargs):
        super().__init__(execution, **kwargs)
        self.calls = 0

    async def has_conflicting_order(self, external_identifier, *, quantity=None):
        # `quantity` rides every loop ask since 2026-09-24 (the CLOSE-side
        # quantity rule); the double keeps the real signature so the seam
        # cannot drift from what production passes.
        self.calls += 1
        return await super().has_conflicting_order(
            external_identifier, quantity=quantity
        )


async def test_the_guard_runs_once_and_never_before_a_reprice():
    """A replace re-sends the SAME identifier against its own still-working order.
    Guarding there would find that order, correctly call it a conflict, and
    deadlock the loop against itself."""
    broker = Broker()
    audit = FakeAudit()
    wrapped = AuditedOrderExecution(broker, audit=audit, store=InMemoryOrderStore())
    guard = CountingGuard(wrapped, audit=audit)
    clock = FakeTime()
    loop = PriceWalkingLoop(
        execution=wrapped, audit=audit, policy=policy(), guard=guard,
        sleep=clock.sleep, clock=clock.clock,
    )

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.steps == 3            # it did reprice, repeatedly
    assert guard.calls == 1             # and asked exactly once, up front
    assert "order.duplicate_blocked" not in audit.events


async def test_an_exit_style_walk_can_skip_the_guard():
    broker = Broker()
    audit = FakeAudit()
    wrapped = AuditedOrderExecution(broker, audit=audit, store=InMemoryOrderStore())
    guard = CountingGuard(wrapped, audit=audit)
    clock = FakeTime()
    loop = PriceWalkingLoop(
        execution=wrapped, audit=audit, policy=policy(), guard=guard,
        sleep=clock.sleep, clock=clock.clock,
    )
    await loop.run(spread(), guard_first_submission=False)
    assert guard.calls == 0


async def test_a_conflicting_order_blocks_the_walk_before_anything_is_sent():
    broker = Broker()
    await broker.submit(spread(), dry_run=False)   # a pre-existing working order
    loop, audit, _ = build_loop(broker)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.BLOCKED
    assert len(broker.submitted) == 1              # only the pre-existing one
    assert "order.duplicate_blocked" in audit.events


async def test_a_guard_that_cannot_reach_the_broker_stops_the_walk_loudly():
    """The guard is the only duplicate protection there is (the broker does not
    enforce `external_identifier` uniqueness). An unanswerable question must not
    become one more not-filled outcome a caller retries past."""
    broker = Broker()
    broker.fail_next(ConnectionError("cert down"))
    loop, _, _ = build_loop(broker)

    with pytest.raises(ConnectionError):
        await loop.run(spread(), guard_first_submission=True)
    assert broker.submitted == []


async def test_the_guard_is_required_when_asked_for():
    loop, _, _ = build_loop(Broker(), guard=False)
    with pytest.raises(ValueError, match="DuplicateOrderGuard"):
        await loop.run(spread(), guard_first_submission=True)


# -- replace for repricing, cancel for abandonment ---------------------------


async def test_repricing_uses_replace_and_never_cancel_then_submit():
    """`replace` is the atomic cancel-and-resubmit. Doing it by hand opens a window
    with nothing working and nothing recorded as intended."""
    broker = Broker()
    loop, _, _ = build_loop(broker)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.WALKED_AWAY
    assert len(broker.replaced) == 3
    assert len(broker.submitted) == 1              # exactly one submit, ever
    assert broker.cancelled == [result.broker_order_id]   # only the abandonment


async def test_the_walk_away_threshold_comes_from_the_policy():
    """20% of 0.85 stops at 0.70; 5% stops at 0.80. Nothing is hardcoded."""
    tight = Broker()
    loop, _, _ = build_loop(tight, policy=policy(walk_away_pct=0.05))
    await loop.run(spread("0.85"), guard_first_submission=True)
    assert [str(o.limit_price) for _, o in tight.replaced] == ["0.80"]


def test_a_credit_walk_never_crosses_into_a_debit():
    """Conceding 100% of a credit would take the limit to zero and past it. Paying
    to OPEN a credit spread is a different trade, not a more aggressive one."""
    walk = PriceWalk.from_policy(Decimal("0.10"), policy(walk_away_pct=1.0))
    assert walk.floor == TICK
    assert walk.price_at(1) == Decimal("0.05")
    assert walk.price_at(2) is None


# -- the near-tick floor collapse (2026-08-31) --------------------------------
#
# THE REGRESSION GUARD. A percentage of a price on a tick grid vanishes below half a
# tick, and the floor then rounds back onto the initial: at 0.20 an exit opening at a
# debit of 0.05 or 0.10 had NO rungs at all. That is the ordinary state of a winning
# 0DTE spread at the 15:30 flatten, not a tail — on 2026-08-31 both flattens walked
# away "after 0 step(s) at -0.05" and rode entirely on the market cross.
#
# This table fails at its first two rows against a purely multiplicative floor, and
# must keep failing if anyone reintroduces one.
# → docs/decisions/2026-08-31-exit-ladder-near-tick.md


def _rungs(walk: PriceWalk) -> list[str]:
    prices, i = [], 1
    while (price := walk.price_at(i)) is not None:
        prices.append(str(price))
        i += 1
        assert i < 200, "a walk with no floor"
    return prices


@pytest.mark.parametrize(
    "initial, floor, rungs",
    [
        # The two the percentage could not express. One step of range, no more.
        ("-0.05", "-0.10", 1),
        ("-0.10", "-0.15", 1),
        # Everything the percentage already expressed is BIT-IDENTICAL at min=1.
        ("-0.15", "-0.20", 1),
        ("-0.25", "-0.30", 1),
        ("-0.50", "-0.60", 2),
        ("-1.00", "-1.20", 4),
    ],
)
def test_the_exit_ladder_has_range_at_every_opening_debit(
    initial: str, floor: str, rungs: int
):
    walk = PriceWalk.from_policy(
        Decimal(initial), policy(walk_away_pct=0.20, min_concession_steps=1)
    )
    assert str(walk.floor) == floor
    assert len(_rungs(walk)) == rungs


@pytest.mark.parametrize("initial", ["-0.05", "-0.10"])
def test_without_the_minimum_those_same_debits_have_no_walk_at_all(initial: str):
    """The defect itself, pinned. `min_concession_steps=0` is the entry shape and
    still reproduces it exactly — which is why `for_entry` passing 0 is a REPORTED
    condition (`signal.min_credit` keeps entries out of it) rather than a fix."""
    walk = PriceWalk.from_policy(
        Decimal(initial), policy(walk_away_pct=0.20, min_concession_steps=0)
    )
    assert walk.floor == Decimal(initial)
    assert _rungs(walk) == []


def test_the_minimum_only_ever_widens_a_walk_that_is_already_wider():
    """It is a FLOOR under the percentage, not a replacement for it. A 1.70 debit
    keeps all seven of its rungs at a minimum of three."""
    walk = PriceWalk.from_policy(
        Decimal("-1.70"), policy(walk_away_pct=0.20, min_concession_steps=3)
    )
    assert str(walk.floor) == "-2.05"
    assert len(_rungs(walk)) == 7


def test_the_minimum_is_counted_in_STEPS_not_in_TICKS():
    """The guarantee is "this many reprices", so it survives `step_ticks` > 1.
    Counting ticks instead would give a 2-tick walk half the rungs it was promised."""
    walk = PriceWalk.from_policy(
        Decimal("-0.05"),
        policy(walk_away_pct=0.20, step_ticks=2, min_concession_steps=2),
    )
    assert _rungs(walk) == ["-0.15", "-0.25"]


def test_the_minimum_cannot_push_a_CREDIT_walk_across_zero():
    """The two guards compose in the safe order: the minimum applies first and the
    credit clamp still binds after it. Reversing them would let a credit walk offer
    to pay to open — a different trade from the one that was sized and gated."""
    walk = PriceWalk.from_policy(
        Decimal("0.10"), policy(walk_away_pct=0.05, min_concession_steps=5)
    )
    assert walk.floor == TICK
    assert _rungs(walk) == ["0.05"]


def test_the_ENTRY_policy_opts_out_of_the_minimum_explicitly():
    """`for_entry` passes 0, so every entry ladder is exactly what it was. The entry
    side has the same collapse below a credit of 0.25 and is out of scope — it is
    unreachable only because `signal.min_credit` is 0.50, and that dependency is
    recorded rather than relied on silently."""
    config = make_config()
    assert WalkPolicy.for_entry(config).min_concession_steps == 0
    assert (
        WalkPolicy.for_exit(config).min_concession_steps
        == config.watchdog.exit_walk_min_steps
    )
    # At PRODUCTION's entry_walk_away_pct, the ladder the shadow record was measured
    # against — 0.85 stops at 0.75 — with the minimum inert, exactly as before.
    live = make_config(execution={"entry_walk_away_pct": 0.10})
    entry = PriceWalk.from_policy(Decimal("0.85"), WalkPolicy.for_entry(live))
    assert str(entry.floor) == "0.75"
    assert _rungs(entry) == ["0.80", "0.75"]
    # And the collapse is still there below 0.25, which is the reported finding:
    # only `signal.min_credit = 0.50` keeps an entry out of it.
    assert _rungs(PriceWalk.from_policy(Decimal("0.25"), WalkPolicy.for_entry(live))) == []


def test_a_walk_policy_refuses_a_negative_minimum():
    with pytest.raises(ValueError, match="min_concession_steps"):
        policy(min_concession_steps=-1)


# -- the width cap (2026-09-17) -----------------------------------------------


@pytest.mark.parametrize(
    "initial, uncapped, capped, rungs",
    [
        # THE PRODUCTION CASE, one tick inside it. 2026-09-03's exit opened at -5.05
        # on a 5.00-wide spread and floored at -6.05; `build._limit_price` now stops
        # that limit being built at all, so the deepest a walk can START is the width.
        ("-5.00", "-6.00", "-5.0", 0),
        # The case the 2026-08-31 record named and left: a -4.50 exit floored at
        # -5.40, 18 rungs, the last several offering more than the spread can be worth.
        ("-4.50", "-5.40", "-5.0", 10),
        # The boundary: 0.20 of 4.00 is 0.80, so -4.80 sits INSIDE the width and the
        # cap must not touch it.
        ("-4.00", "-4.80", "-4.80", 16),
        # The modal regimes are nowhere near the cap and must be bit-identical.
        ("-1.65", "-2.00", "-2.00", 7),
        ("-0.05", "-0.20", "-0.20", 3),
    ],
)
def test_the_walk_floor_is_capped_at_the_spread_width(
    initial: str, uncapped: str, capped: str, rungs: int
):
    """A vertical cannot be worth more than its strike distance, so every rung past
    the width offers to pay more than the position's maximum possible loss.

    `min_concession_steps=3` here is the deployed live value, so these rows are the
    production ladder rather than a synthetic one.
    """
    p = policy(walk_away_pct=0.20, min_concession_steps=3)
    assert str(PriceWalk.from_policy(Decimal(initial), p).floor) == uncapped
    walk = PriceWalk.from_policy(Decimal(initial), p, width=Decimal("5.0"))
    assert str(walk.floor) == capped
    assert len(_rungs(walk)) == rungs


@pytest.mark.parametrize("initial", ["-0.05", "-1.00", "-2.50", "-4.00", "-4.95", "-5.00"])
def test_no_rung_on_any_exit_walk_can_exceed_the_width(initial: str):
    """The invariant itself, swept rather than sampled: whatever the opening debit,
    no price the ladder will ever offer is above the width."""
    walk = PriceWalk.from_policy(
        Decimal(initial),
        policy(walk_away_pct=0.20, min_concession_steps=3),
        width=Decimal("5.0"),
    )
    for price in [walk.initial] + [Decimal(r) for r in _rungs(walk)]:
        assert abs(price) <= Decimal("5.0"), f"{price} exceeds the 5.00 width"


def test_the_width_cap_is_the_LAST_clause_so_nothing_can_push_past_it():
    """A cap another clause may override is not a cap. `min_concession_steps` is the
    only clause that WIDENS a walk, so it is the one that could breach the width —
    an absurd minimum on a near-width debit must still stop at the width."""
    walk = PriceWalk.from_policy(
        Decimal("-4.95"),
        policy(walk_away_pct=0.20, min_concession_steps=50),
        width=Decimal("5.0"),
    )
    assert str(walk.floor) == "-5.0"
    assert _rungs(walk) == ["-5.00"]


def test_the_width_cap_is_inert_for_ENTRIES_by_arithmetic():
    """An opening walk concedes DOWNWARD from a credit `build._limit_price` has
    already capped at the width, so `abs(floor) <= abs(initial) <= width` holds before
    the clamp is reached. Pinned because the entry ladder must stay bit-identical —
    the same standing `min_concession_steps` has."""
    entry = WalkPolicy.for_entry(make_config(execution={"entry_walk_away_pct": 0.10}))
    for credit in ("0.50", "0.55", "0.65", "0.75", "0.80", "1.00", "2.00", "4.95", "5.00"):
        assert PriceWalk.from_policy(Decimal(credit), entry) == PriceWalk.from_policy(
            Decimal(credit), entry, width=Decimal("5.0")
        )


def test_an_unreadable_width_leaves_the_floor_uncapped_rather_than_breaking_the_walk():
    """`Order.width` is `None` when the legs' OCC symbols cannot be read, and per
    `occ.py`'s rule a caller must fail closed on it. Here failing closed means the
    floor is simply not capped — the pre-2026-09-17 behaviour — because refusing to
    build a walk would strand a triggered position over a symbol parse. The OPENING
    limit is capped independently from `SpreadSignal.width`, so the two bounds do not
    share a failure mode."""
    p = policy(walk_away_pct=0.20, min_concession_steps=3)
    assert PriceWalk.from_policy(Decimal("-4.50"), p, width=None) == PriceWalk.from_policy(
        Decimal("-4.50"), p
    )


def test_step_of_recovers_the_ladder_position_from_a_price():
    walk = PriceWalk.from_policy(Decimal("0.85"), policy())
    assert walk.step_of(Decimal("0.75")) == 2
    assert walk.step_of(Decimal("0.85")) == 0
    assert walk.step_of(Decimal("0.77")) is None    # not on this ladder
    assert walk.step_of(Decimal("0.90")) is None    # better than we started


# -- outcomes ----------------------------------------------------------------


async def test_a_fill_ends_the_walk():
    broker = Broker()
    broker.on_poll = [None, lambda b: b.fill(1000)]
    loop, audit, _ = build_loop(broker)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2
    assert result.requested_contracts == 2
    assert not result.partially_filled
    assert broker.cancelled == []
    assert audit.payload("walk.finished")["outcome"] == "filled"


async def test_a_partial_fill_takes_what_filled_and_cancels_the_rest():
    """A vertical is ONE multi-leg order, so a partial is a SMALLER SPREAD — still
    defined-risk, never a naked leg. The remainder is cancelled rather than walked
    because tastytrade's replace semantics on a partially-filled order have not
    been measured, and the way to be wrong is to re-request the full size."""
    broker = Broker()
    broker.on_poll = [None, lambda b: b.fill(1000, contracts=1)]
    loop, _, _ = build_loop(broker)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.FILLED
    assert result.partially_filled
    assert result.filled_contracts == 1
    assert broker.cancelled == [1000]
    assert broker.replaced == []


async def test_a_rejection_ends_the_walk_immediately():
    """Not a "not filled yet" case: stepping a rejected order sends a second order
    the broker refuses the same way."""
    broker = Broker()
    broker.reject_next()
    loop, _, clock = build_loop(broker)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.REJECTED
    assert broker.replaced == []
    assert clock.slept == []            # not even one cycle was waited


async def test_a_rejection_carries_the_brokers_reason_onto_the_walk_result():
    """`WalkResult.reject_reason` is the SAME surface every other outcome's reason
    reaches — so an operator does not have to correlate against `order.event`."""
    broker = Broker()
    broker.reject_next("Account is not authorized to trade this product")
    loop, audit, _ = build_loop(broker)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.REJECTED
    assert result.reject_reason == "Account is not authorized to trade this product"
    assert audit.payload("walk.finished")["reject_reason"] == (
        "Account is not authorized to trade this product"
    )


async def test_a_non_rejected_outcome_carries_no_reject_reason():
    """`None`, never `""` — the same discipline `cost_to_close` uses for
    None-vs-0.0. A filled walk never touched a rejection."""
    broker = Broker()
    broker.on_poll = [None, lambda b: b.fill(1000)]
    loop, audit, _ = build_loop(broker)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.FILLED
    assert result.reject_reason is None
    assert audit.payload("walk.finished")["reject_reason"] is None


async def test_an_order_terminated_elsewhere_is_reported_as_such():
    """Chunk 5 has to tell "I gave up" apart from "something else killed it"."""
    broker = Broker()
    broker.on_poll = [None, lambda b: b.set_status(1000, OrderStatus.EXPIRED)]
    loop, _, _ = build_loop(broker)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.CANCELLED_ELSEWHERE
    assert result.last_status is OrderStatus.EXPIRED
    assert not result.cancel_requested


async def test_a_stop_request_cancels_the_working_order():
    """A walk in flight must not leave an order behind at session end."""
    import asyncio

    stop = asyncio.Event()
    broker = Broker()
    broker.on_poll = [lambda b: stop.set()]
    loop, _, _ = build_loop(broker, stop=stop)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.ABORTED
    assert result.cancel_requested
    assert broker.cancelled == [1000]


# -- a terminal rung is not a verdict about the spread (2026-09-02) -----------
#
# THE reproduction, from cert on 2026-09-01: an exit that SUCCEEDED — 2 contracts,
# 1.50 debit, position flat — was recorded REJECTED because the walk concluded from
# the one rung it was holding while the fill sat on the rung it had just replaced.
# The rule broken is one the codebase already states as a non-negotiable and already
# implements one path over (`_await_settled`): the FILLED check is identifier-WIDE.


class CountingView(LiveOrderView):
    """`LiveOrderView` that counts identifier-wide reads, and can refuse them."""

    def __init__(self, execution, *, raises: Exception | None = None) -> None:
        super().__init__(execution)
        self.identifier_calls = 0
        self._raises = raises

    async def by_identifier(self, external_identifier: str):
        self.identifier_calls += 1
        if self._raises is not None:
            raise self._raises
        return await super().by_identifier(external_identifier)


async def test_a_replacement_refused_while_the_original_filled_concludes_filled():
    """THE incident. `put-7615-7610`, 2026-09-01: the walk repriced once, the broker
    refused the replacement *because the original had filled*, and the walk reported
    REJECTED -> NOT_RESOLVED with `filled_contracts: 0` on a spread that filled 2.

    The whole verdict has to move, not just the headline: the outcome, the contracts,
    the broker order id, and the last status all describe the rung that FILLED.
    """
    broker = Broker()
    broker.refuse_replace(1, reason="Received a fill on the original order")
    loop, audit, _ = build_loop(broker, policy=policy(cadence_s=1.0))

    result = await loop.run(
        spread("-0.85", OrderDirection.CLOSE), guard_first_submission=False
    )

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2
    assert result.broker_order_id == 1000        # the ORIGINAL, not the refused rung
    assert result.last_status is OrderStatus.FILLED
    # `reject_reason`'s contract is NOT widened: it stays present only on REJECTED.
    assert result.reject_reason is None
    # The rejection is not lost — it rides in the reason, which every surface reads.
    assert "1001" in result.reason
    assert "Received a fill on the original order" in result.reason

    finished = audit.payload("walk.finished")
    assert finished["outcome"] == "filled"
    assert finished["broker_order_id"] == 1000
    assert finished["filled_contracts"] == 2
    # And the correction is countable rather than only quotable: the incident's
    # headline was a RATE (1 of 3 replaces), and prose cannot be counted.
    telemetry = audit.payload("walk.filled_under_identifier")
    assert telemetry["terminal_broker_order_id"] == 1001
    assert telemetry["terminal_status"] == "Rejected"
    assert telemetry["terminal_reject_reason"] == "Received a fill on the original order"
    assert telemetry["filled_broker_order_id"] == 1000
    assert telemetry["steps"] == 1


async def test_the_entry_mirror_of_the_same_race_records_a_fill_not_a_rejection():
    """`_reprice` is shared, so an ENTRY walk whose original fills as the replace
    goes in recorded `outcome='rejected'` for a trade idea that OPENED a position.

    Never a position-safety hole — `positions` is written from the fill feed by
    chunk 2, independently of the walk — but it mis-scores the day, skips the
    watchdog's look-sooner nudge, and would feed a false count into the open
    consecutive-rejection back-off decision. **"Your order filled" is not a broker
    refusing to trade.**
    """
    broker = Broker()
    broker.refuse_replace(1, reason="Received a fill on the original order")
    loop, audit, _ = build_loop(broker)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2
    assert result.broker_order_id == 1000
    assert audit.payload("walk.finished")["outcome"] == "filled"


async def test_a_refusal_with_nothing_filled_anywhere_is_still_a_refusal():
    """The fix must not swallow real refusals. The broker declined the replacement,
    nothing under the identifier filled, and the honest answer is unchanged."""
    broker = Broker()
    broker.refuse_replace(
        1, reason="Account is not authorized to trade this product", original_fills=False
    )
    loop, audit, _ = build_loop(broker)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert result.outcome is WalkOutcome.REJECTED
    assert result.filled_contracts == 0
    assert result.broker_order_id == 1001
    assert result.reject_reason == "Account is not authorized to trade this product"
    assert "walk.filled_under_identifier" not in audit.events


async def test_a_cancelled_replacement_over_a_filled_original_concludes_filled_too():
    """`CANCELLED_ELSEWHERE` has the identical shape and a mechanism sitting right
    next to the observed one: `replace` cancels the ORIGINAL at the broker, so a
    broker resolving the same race by cancelling the REPLACEMENT instead of
    rejecting it produces exactly this — and lands in the same NOT_RESOLVED bucket
    on the exit path. The probe goes ABOVE the rejected/cancelled split for that
    reason."""
    broker = Broker()
    broker.refuse_replace(1, status=OrderStatus.CANCELLED)
    loop, _, _ = build_loop(broker)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2
    assert result.broker_order_id == 1000


async def test_a_terminated_replacement_with_nothing_filled_is_still_cancelled_elsewhere():
    broker = Broker()
    broker.refuse_replace(1, status=OrderStatus.EXPIRED, original_fills=False)
    loop, _, _ = build_loop(broker)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert result.outcome is WalkOutcome.CANCELLED_ELSEWHERE
    assert result.last_status is OrderStatus.EXPIRED


async def test_the_probe_is_not_gated_on_having_repriced():
    """Ungated on `state.steps`, deliberately. The broker does NOT enforce
    `external_identifier` uniqueness (measured, chunk 1), so a walk at step 0 is not
    a walk with one order under its key — `_recover` resuming against an order a
    "failed" submit actually landed, and the exit retry re-running under the same
    `argus-close-*` key, both produce exactly this. Gating an identifier-wide
    question on a rung-scoped counter would repeat the bug's own category error."""
    broker = Broker(live_after_polls=None)
    broker.on_poll = [
        lambda b: (b.plant_filled_sibling(), b.set_status(1000, OrderStatus.REJECTED))
    ]
    loop, _, _ = build_loop(broker)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert result.outcome is WalkOutcome.FILLED
    assert result.steps == 0
    assert result.filled_contracts == 2
    assert broker.replaced == []


async def test_a_probe_that_cannot_be_answered_falls_back_to_the_rungs_own_verdict():
    """Fail-closed by construction, and the fallback is not a soft option.

    The probe is strictly ADDITIVE over a positive terminal observation the broker
    gave us one call earlier, so a read that does not happen discovers nothing and
    licenses nothing new. Manufacturing UNRESOLVED out of it would DESTROY
    information the broker positively supplied. `_request_cancel`'s rule holds
    verbatim: a call that raises must not turn a decided outcome into an exception.
    """
    broker = Broker()
    broker.refuse_replace(1, reason="Received a fill on the original order")
    view = None

    def make_view(wrapped):
        nonlocal view
        view = CountingView(wrapped, raises=ConnectionError("gone"))
        return view

    loop, audit, _ = build_loop(broker, view_factory=make_view)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert result.outcome is WalkOutcome.REJECTED
    assert result.reject_reason == "Received a fill on the original order"
    # The verdict says what it is about, so an operator is not misled about its scope.
    assert "could not be answered" in result.reason
    assert "about the rung alone" in result.reason
    # Recorded, never swallowed: Tier 1, on the existing shape, with a call name that
    # makes the failure RATE a one-line query rather than this comment's guess.
    disconnect = audit.payload("walk.disconnected")
    assert disconnect["call"] == "terminal_probe"
    assert disconnect["error_type"] == "ConnectionError"
    assert result.recovered_disconnects == 1
    # One attempt. `_observe`'s retry ladder is for reads the loop cannot continue
    # without; this one's absence is a no-op, and retrying would put backoff sleep on
    # the EXIT path's terminating branch while a triggered position is open.
    assert view.identifier_calls == 1


async def test_an_unreadable_order_under_the_identifier_takes_the_same_fallback():
    """`UnmappableOrderError` is how "the broker holds something here and Argus
    cannot say what" arrives. It is a probe that could not be answered, not a
    verdict, and it falls back like any other."""
    broker = Broker()
    broker.refuse_replace(1, reason="Received a fill on the original order")
    broker.add_unmappable(broker_order_id=9999, external_identifier=OURS)
    loop, _, _ = build_loop(broker, guard=False)

    result = await loop.run(spread("0.85"), guard_first_submission=False)

    assert result.outcome is WalkOutcome.REJECTED
    assert "could not be answered" in result.reason


async def test_the_probe_costs_one_read_and_only_where_a_non_fill_is_claimed():
    """It cannot fire per poll: the gate is in `_finish`, which runs once per walk.

    **`WALKED_AWAY` now asks, and did not before 2026-09-10.** That is the cost the
    uniform rule was accepted at, stated exactly: one extra GET on the entry path's
    ordinary non-fill outcome, on top of the cancel round trip it already makes. It
    is paid rather than carved out because "this branch is different" is the
    reasoning that produced this defect three times — and a walked-away entry whose
    prior rung filled has nothing downstream to correct it, unlike the exit path,
    where `_await_settled` asks the same question a moment later.
    """
    filled = Broker()
    filled.on_poll = [None, lambda b: b.fill(1000)]
    views: list[CountingView] = []

    def make_view(wrapped):
        views.append(CountingView(wrapped))
        return views[-1]

    loop, _, _ = build_loop(filled, view_factory=make_view)
    assert (await loop.run(spread(), guard_first_submission=True)).outcome is (
        WalkOutcome.FILLED
    )
    assert views[-1].identifier_calls == 0, "FILLED is the answer; do not re-ask it"

    walked = Broker()
    loop, _, _ = build_loop(walked, view_factory=make_view)
    assert (await loop.run(spread(), guard_first_submission=True)).outcome is (
        WalkOutcome.WALKED_AWAY
    )
    assert views[-1].identifier_calls == 1

    raced = Broker()
    raced.refuse_replace(1, reason="Received a fill on the original order")
    loop, _, _ = build_loop(raced, view_factory=make_view)
    assert (await loop.run(spread(), guard_first_submission=True)).outcome is (
        WalkOutcome.FILLED
    )
    assert views[-1].identifier_calls == 1


def test_first_filled_is_one_implementation_shared_with_recovery():
    """"Which order counts as having left a position" is one question. Two
    implementations would be free to disagree about the same broker answer — and the
    disagreement would be between a path concluding FILLED and one concluding
    REJECTED about the same spread."""
    from argus.execution.loop import _first_filled

    dead = dataclasses.replace(spread(), status=OrderStatus.CANCELLED, legs=tuple(
        dataclasses.replace(leg, remaining_quantity=leg.quantity) for leg in spread().legs
    ))
    done = dataclasses.replace(spread(), status=OrderStatus.FILLED, legs=tuple(
        dataclasses.replace(leg, remaining_quantity=0) for leg in spread().legs
    ))
    assert _first_filled([]) is None
    assert _first_filled([dead]) is None
    assert _first_filled([dead, done]) is done
    # "Cannot tell" counts as filled — `classify_order`'s fail-closed posture is
    # consumed as-is here and must not be relaxed to make this tidier.
    cannot_tell = dataclasses.replace(spread(), status=OrderStatus.CANCELLED)
    assert _first_filled([cannot_tell]) is cannot_tell


def test_the_probe_decides_on_state_and_never_on_the_brokers_prose():
    """NEVER match on `reject_reason`. Broker prose changes without notice and a
    rail built on it fails open the day it does — `Order.reject_reason`'s own
    docstring says "Nothing decides on it". The prose is quoted into the reason and
    tested nowhere.

    Scoped to `_finish` since 2026-09-10, which is where the probe now lives — and
    that is a WIDER scope than the method it used to name, not a looser one: it now
    covers the whole terminating frame, including the outcome gates on
    `reject_reason` and `mutation_error`.
    """
    fn = next(
        n for n in ast.walk(_parse_loop())
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_finish"
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare):
            assert "reject_reason" not in ast.unparse(node), ast.unparse(node)
        if isinstance(node, ast.Call):
            rendered = ast.unparse(node)
            assert not (
                "reject_reason" in rendered
                and any(m in rendered for m in ("startswith", "endswith", "match", "find"))
            ), rendered


# -- a rung that NEVER WORKED is not a verdict either (2026-09-10) ------------
#
# The same rule, one frame up. 2026-09-02 hung the identifier-wide FILLED read off a
# TERMINAL BROKER OBSERVATION and argued from exactly that — which left the three
# branches that conclude off a CLOCK asking nothing: `_await_in_market`'s two deadline
# exits and `_walk`'s blind-cycle exit. **The trigger for asking is that the WALK is
# about to conclude, not that the BROKER already did.**
#
# Reproduced in cert 2026-09-09: `20260909-call-7660-7670` reported
# `never_worked / filled_contracts 0 / steps 1`; the ledger holds 1 contract at 0.55,
# closed intraday for +40.00.
#
# The gate now lives in `_finish`, the sole constructor of every `WalkResult` here,
# which is the only place the rule can be an INVARIANT rather than a habit — and the
# habit has failed three times (`reconcile._ask_broker` and
# `entry._check_for_duplicate` on 2026-08-26, `_resolve`'s terminal branch on
# 2026-09-02).
#
# T11 is deliberately NOT re-written here: 2026-09-02's regression
# (`test_a_replacement_refused_while_the_original_filled_concludes_filled`, above) is
# UNEDITED and passes through the moved gate. That it needed no change is the
# assertion — the frame moved, the behaviour did not.


class InvisibleOrderView(LiveOrderView):
    """Acknowledged with an id, and then never present in any answer.

    The `not seen` deadline branch needs `by_id` to answer nothing while
    `by_identifier` answers normally — which is precisely the shape that makes the
    branch wrong: a fill on a PRIOR rung carries a different `broker_order_id`
    entirely, so every `by_id` read can legitimately fail while the identifier-wide
    answer is sitting there.
    """

    async def by_id(self, broker_order_id: int):
        return None


async def test_a_deadline_that_never_saw_the_market_asks_before_it_concludes():
    """T1. THE reported defect. The replacement sat unreadable for the full
    `working_timeout_s`, the walk concluded `NEVER_WORKED` from that one rung, and a
    prior rung under the same identifier had filled.

    The whole verdict moves, not the headline: outcome, contracts and broker id all
    describe the rung that FILLED. `final_limit_price` deliberately does NOT — it is
    read off `state.sent`, and relabelling it as the filled rung's limit would dress
    one limit up as more fill-like than another. A limit is not a fill price.
    """
    broker = Broker(live_after_polls=None)
    # 0.55 against the walked rung's 0.85 — the 2026-09-09 shape, where the report's
    # 0.50 was the REPLACEMENT's limit and the ledger's 0.55 was rung 0's.
    broker.on_poll = [lambda b: b.plant_filled_sibling(limit="0.55")]
    loop, audit, _ = build_loop(broker, guard=False)

    result = await loop.run(spread("0.85"), guard_first_submission=False)

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2
    assert result.broker_order_id == 1001        # the planted fill, not the dead rung
    # `state.sent` does NOT move with `state.last`: this is the walk's own last
    # limit, not the filled rung's 0.55. A limit is not a fill price.
    assert result.final_limit_price == Decimal("0.85")
    # A cancel WAS requested on the rung that never worked, and the record keeps
    # saying so — `state.cancel_requested` does not move with the outcome.
    assert result.cancel_requested
    assert audit.payload("walk.finished")["outcome"] == "filled"


async def test_a_deadline_that_never_saw_the_order_at_all_asks_too():
    """T2. `UNRESOLVED: order_not_visible_at_broker` — the LOUDER of the two deadline
    outcomes. `entry.py`'s `_ALERTING_OUTCOMES` names it the headline "a human should
    look at this now" verdict, and on the exit path it lands in `NOT_RESOLVED`, which
    spends a triggered position's one attempt. Fixing `NEVER_WORKED` alone would have
    left the noisier sibling wrong."""
    broker = Broker(live_after_polls=None)
    broker.plant_filled_sibling()               # id 1000; the walked order is 1001
    loop, audit, _ = build_loop(
        broker, guard=False, view_factory=InvisibleOrderView
    )

    result = await loop.run(spread("0.85"), guard_first_submission=False)

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2
    assert result.broker_order_id == 1000
    assert audit.payload("walk.finished")["outcome"] == "filled"


async def test_the_blind_cycle_exit_asks_a_different_question_than_the_one_that_failed():
    """T3. The exit that deserves the probe most obviously of the three: it concludes
    from repeated failure of `by_id`, and `by_identifier` is a DIFFERENT QUESTION
    AGAINST A DIFFERENT KEY. A fill on the original rung carries another
    `broker_order_id` entirely, so N unreadable `by_id` answers say nothing at all
    about it."""

    class Amnesiac(Broker):
        """The walked order is visible on the in-market check that opens each cycle
        and absent from the post-hold read that would decide the next price — the one
        pattern that never resolves and never reprices. Everything else under the
        identifier stays visible throughout."""

        async def get_live_orders(self):
            answer = await super().get_live_orders()
            if self.polls % 2:
                return answer
            return LiveOrders(
                orders=tuple(o for o in answer.orders if o.broker_order_id != 1000),
                unmappable=answer.unmappable,
            )

    broker = Amnesiac()
    broker.on_poll = [lambda b: b.plant_filled_sibling()]
    alerts: list[tuple[str, dict]] = []
    loop, audit, _ = build_loop(
        broker, guard=False, alert=lambda kind, detail: alerts.append((kind, detail))
    )

    result = await loop.run(spread("0.85"), guard_first_submission=False)

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2
    assert result.broker_order_id == 1001
    assert broker.replaced == []
    # The page that preceded the wrong verdict still fires — the walk really did lose
    # sight of its own order, and that is worth saying even when the spread is flat.
    assert any(kind == "walk_order_kept_disappearing" for kind, _ in alerts)
    assert audit.payload("walk.finished")["outcome"] == "filled"


async def test_walked_away_and_drifted_ask_as_well():
    """T4. The two arguable members, and they are IN. Both reach `_finish`
    immediately after a best-effort `_request_cancel` that confirms nothing —
    structurally identical to `NEVER_WORKED`. Excluding them would re-assert exactly
    the reasoning that produced this defect three times: *this branch is different*.
    """
    walked = Broker()
    walked.on_poll = [lambda b: b.plant_filled_sibling()]
    loop, _, _ = build_loop(walked, guard=False)

    result = await loop.run(spread("0.85"), guard_first_submission=False)

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2
    assert result.broker_order_id == 1001

    drifting = Broker()
    drifting.on_poll = [lambda b: b.plant_filled_sibling()]
    loop, _, _ = build_loop(
        drifting, guard=False, drift_check=DriftSpy(reasons=[None, "SPX moved 3.4"])
    )

    result = await loop.run(spread("0.85"), guard_first_submission=False)

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2
    assert result.broker_order_id == 1001


async def test_a_deadline_probe_that_cannot_be_answered_keeps_the_deadlines_verdict():
    """T5. Fail-closed, and the fallback is not a soft option. A read that does not
    happen discovers nothing and licenses nothing new; manufacturing a different
    outcome out of it would destroy the verdict the walk had honestly reached."""
    broker = Broker(live_after_polls=None)
    view = None

    def make_view(wrapped):
        nonlocal view
        view = CountingView(wrapped, raises=ConnectionError("gone"))
        return view

    loop, audit, _ = build_loop(broker, guard=False, view_factory=make_view)

    result = await loop.run(spread("0.85"), guard_first_submission=False)

    assert result.outcome is WalkOutcome.NEVER_WORKED
    # The verdict says what it is ABOUT, so an operator is not misled about its scope.
    assert "no confirmed in-market status" in result.reason
    assert "could not be answered" in result.reason
    assert "about the rung alone" in result.reason
    disconnect = audit.payload("walk.disconnected")
    assert disconnect["call"] == "terminal_probe"
    assert disconnect["error_type"] == "ConnectionError"
    assert result.recovered_disconnects == 1
    # One attempt: `_observe`'s retry ladder is for reads the loop cannot continue
    # without, and this one's absence is a no-op.
    assert view.identifier_calls == 1


async def test_an_aborted_walk_adds_no_broker_round_trip():
    """T6. **A halt must not add a broker round trip.** The phone button's stated
    bound is one `cycle_wait_s`; a GET inside the abort frame widens it for no
    benefit, since nothing downstream acts on the abort's fill count. This exclusion
    is a SAFETY RAIL, not an optimisation — asserted on the view's call count, never
    on the result."""
    import asyncio

    stop = asyncio.Event()
    broker = Broker()
    broker.on_poll = [lambda b: (b.plant_filled_sibling(), stop.set())]
    view = None

    def make_view(wrapped):
        nonlocal view
        view = CountingView(wrapped)
        return view

    loop, _, _ = build_loop(broker, guard=False, stop=stop, view_factory=make_view)

    result = await loop.run(spread("0.85"), guard_first_submission=False)

    assert result.outcome is WalkOutcome.ABORTED
    assert view.identifier_calls == 0


async def test_a_blocked_walk_adds_no_broker_round_trip():
    """T7. Two independent reasons, either sufficient: nothing was sent, so there is
    no claim about a spread to be wrong about; and `_ask_guard` asked the
    identifier-wide question one frame earlier and acted on the answer."""
    broker = Broker()
    broker.plant_filled_sibling()
    view = None

    def make_view(wrapped):
        nonlocal view
        view = CountingView(wrapped)
        return view

    loop, _, _ = build_loop(broker, view_factory=make_view)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert result.outcome is WalkOutcome.BLOCKED
    assert view.identifier_calls == 0


async def test_the_three_sites_that_already_asked_do_not_ask_again():
    """T8. `probe=False`, at the three call sites within a few statements of a
    `by_identifier` read — and on one of them the broker has just refused it
    repeatedly. Each assertion counts the reads the SITE ITSELF makes; the gate must
    add none.

    Default `True` and it must stay True: the default is the safe side, so a call
    site that forgets the keyword asks one unnecessary question rather than skipping
    a necessary one.
    """
    # `_recover`, >1 working: that read ANSWERED, and the answer IS the verdict.
    duplicates = Broker()
    duplicates.replace_fails_at = {1}
    duplicates.on_poll = [None, None, lambda b: b._place(spread())]
    view = None

    def make_view(wrapped):
        nonlocal view
        view = CountingView(wrapped)
        return view

    loop, _, _ = build_loop(duplicates, guard=False, view_factory=make_view)
    result = await loop.run(spread(), guard_first_submission=False)
    assert result.outcome is WalkOutcome.UNRESOLVED
    assert view.identifier_calls == 1, "`_recover`'s own read, and nothing added"

    # `_recover`, nothing found: the same read, positively empty, is the whole basis
    # of `NO_ORDER_EXISTS`. Asking again would derive the outcome from itself.
    empty = Broker()
    empty.fail_next(ConnectionError("socket"))
    loop, _, _ = build_loop(empty, guard=False, view_factory=make_view)
    result = await loop.run(spread(), guard_first_submission=False)
    assert result.outcome is WalkOutcome.NO_ORDER_EXISTS
    assert view.identifier_calls == 1

    # `run()`'s `BrokerUnavailable` handler: `_recover` just spent
    # `max_recovery_attempts` on this exact call and failed every one.
    unreachable = Broker()
    unreachable.replace_fails_at = {1}

    def refusing_view(wrapped):
        nonlocal view
        view = CountingView(wrapped, raises=ConnectionError("broker gone"))
        return view

    loop, _, _ = build_loop(unreachable, guard=False, view_factory=refusing_view)
    result = await loop.run(spread(), guard_first_submission=False)
    assert result.outcome is WalkOutcome.UNRESOLVED
    assert view.identifier_calls == 3, (
        "`_recover`'s three exhausted attempts, and no fourth from the gate"
    )


async def test_the_telemetry_tells_the_two_shapes_apart():
    """T9. `suppressed_outcome` is what turns this into a MEASUREMENT rather than a
    second guess: without it the two shapes collapse into one category and 2026-09-02's
    rate (1-of-3 on successful replaces) can no longer be read separately from this
    one. The headline number in both incidents is a RATE, and prose is not countable.

    `terminal_status: null` IS the signature of this defect versus 2026-09-02's —
    there is no terminal broker observation on a deadline branch, only a clock.
    """
    deadline = Broker(live_after_polls=None)
    deadline.on_poll = [lambda b: b.plant_filled_sibling()]
    loop, audit, _ = build_loop(deadline, guard=False)
    await loop.run(spread("0.85"), guard_first_submission=False)

    fired = audit.payload("walk.filled_under_identifier")
    assert fired["suppressed_outcome"] == "never_worked"
    assert fired["terminal_broker_order_id"] is None
    assert fired["terminal_status"] is None
    assert fired["terminal_reject_reason"] is None
    assert fired["filled_broker_order_id"] == 1001
    assert fired["filled_contracts"] == 2

    resolved = Broker()
    resolved.refuse_replace(1, reason="Received a fill on the original order")
    loop, audit, _ = build_loop(resolved)
    await loop.run(spread("0.85"), guard_first_submission=True)

    fired = audit.payload("walk.filled_under_identifier")
    assert fired["suppressed_outcome"] == "rejected"
    assert fired["terminal_broker_order_id"] == 1001
    assert fired["terminal_status"] == "Rejected"
    assert fired["terminal_reject_reason"] == "Received a fill on the original order"


def test_every_outcome_is_classified():
    """T10. The test that makes this design worth more than patching three branches
    — the whole difference between an INVARIANT and a habit.

    Both halves are asserted. The union alone lets a new member be silently
    double-classified; disjointness alone lets one be absent from both. A
    `WalkOutcome` added without a decision about whether it claims a non-fill fails
    here, which is the thing that stops a fifth instance of this class.
    """
    from argus.execution.loop import _ASK_BEFORE_CONCLUDING, _NEVER_ASKS

    assert _ASK_BEFORE_CONCLUDING | _NEVER_ASKS == set(WalkOutcome)
    assert not (_ASK_BEFORE_CONCLUDING & _NEVER_ASKS)
    # The exclusions are a SET of their own, never "everything else" — each member is
    # excluded for its own reason and those reasons are written beside it.
    assert _NEVER_ASKS == {
        WalkOutcome.FILLED, WalkOutcome.BLOCKED, WalkOutcome.ABORTED
    }


async def test_a_walk_with_no_observation_at_all_emits_the_terminal_fields_as_null():
    """T13. `state.last is None` — the shape 2026-09-02 never had to handle, because
    its probe hung off an observation that by definition existed. The three
    `terminal_*` fields must be EMITTED as `None` rather than omitted: their absence
    is what distinguishes this defect from that one in the trail, so a missing key
    would be a missing measurement."""
    broker = Broker()
    filled = broker.plant_filled_sibling()
    loop, audit, _ = build_loop(broker, guard=False)

    state = _Walk(sent=spread(), walk=PriceWalk.from_policy(Decimal("0.85"), policy()))
    assert state.last is None and state.broker_order_id is None

    result = await loop._finish(
        state, WalkOutcome.UNRESOLVED, reason="order_not_visible_at_broker"
    )

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2
    assert result.broker_order_id == filled.broker_order_id
    fired = audit.payload("walk.filled_under_identifier")
    assert set(fired) >= {
        "terminal_broker_order_id", "terminal_status", "terminal_reject_reason"
    }
    assert fired["terminal_broker_order_id"] is None
    assert fired["terminal_status"] is None
    assert fired["terminal_reject_reason"] is None
    # The walk's own reason for concluding is what the correction is composed over,
    # since there is no broker word to quote.
    assert result.reason == (
        f"order_not_visible_at_broker, but {filled.broker_order_id} "
        "under this identifier is filled"
    )


async def test_a_probe_that_finds_nothing_rewrites_no_reason():
    """T14. The moved `_unfilled_reason` must not touch the happy path. A read that
    was ANSWERED and found nothing discovers nothing, so the caller's base reason is
    returned BYTE-IDENTICAL — anything else would be a silent prose change on every
    ordinary refusal in the trail."""
    rejected = Broker()
    rejected.refuse_replace(
        1, reason="Account is not authorized to trade this product", original_fills=False
    )
    loop, _, _ = build_loop(rejected)
    result = await loop.run(spread("0.85"), guard_first_submission=True)
    assert result.outcome is WalkOutcome.REJECTED
    assert result.reason == "broker rejected the order"

    terminated = Broker()
    terminated.refuse_replace(1, status=OrderStatus.EXPIRED, original_fills=False)
    loop, _, _ = build_loop(terminated)
    result = await loop.run(spread("0.85"), guard_first_submission=True)
    assert result.outcome is WalkOutcome.CANCELLED_ELSEWHERE
    assert result.reason == "terminated at the broker as Expired"


# -- disconnects: recovered here, never escalated to a halt ------------------


async def test_a_replace_that_never_arrived_resumes_at_the_price_still_working():
    """The local step counter claims a rung the order may never have reached, so
    it is re-derived from the broker's own limit price. Otherwise every later
    price is computed from a fiction and the ladder skips a rung."""
    broker = Broker()
    broker.replace_fails_at = {1}
    reconnects: list[int] = []

    async def reconnect():
        reconnects.append(1)

    loop, audit, _ = build_loop(broker, reconnect=reconnect)
    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert reconnects, "the reconnect seam should have been used"
    assert "walk.disconnected" in audit.events
    # 0.80 is re-attempted because the broker still holds 0.85 — and no rung is
    # skipped on the way down.
    assert [str(o.limit_price) for _, o in broker.replaced] == ["0.80", "0.75", "0.70"]
    assert result.outcome is WalkOutcome.WALKED_AWAY
    assert result.recovered_disconnects == 1


async def test_a_replace_that_landed_but_timed_out_is_never_re_sent():
    """The case a retry gets wrong. The replace took effect and the response was
    lost; asking the broker finds the NEW order at the NEW price, and the walk
    continues from there. Re-sending would be the replay chunk 2 forbids."""
    broker = Broker()
    broker.replace_fails_after_landing_at = {1}
    loop, _, _ = build_loop(broker)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    # 0.80 was sent exactly ONCE, despite the caller never hearing back about it.
    prices = [str(o.limit_price) for _, o in broker.replaced]
    assert prices == ["0.80", "0.75", "0.70"]
    assert prices.count("0.80") == 1
    assert len(broker.submitted) == 1
    assert result.outcome is WalkOutcome.WALKED_AWAY


async def test_recovery_reports_a_fill_discovered_while_disconnected():
    """The order filled during the window we could not see. The walk must report
    the fill, not the disconnect that hid it."""
    broker = Broker()
    broker.replace_fails_at = {1}
    broker.on_poll = [None, None, lambda b: b.fill(1000)]
    loop, _, _ = build_loop(broker)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert result.outcome is WalkOutcome.FILLED
    assert result.filled_contracts == 2
    assert result.recovered_disconnects == 1


async def test_an_unreachable_broker_ends_the_walk_unresolved_not_halted():
    """A mid-session disconnect is this loop's problem. Reconciliation is a
    restart-time rail (chunk 2) and must not be dragged into it."""
    class Dead(Broker):
        async def get_live_orders(self):
            raise ConnectionError("gone")

    broker = Dead()
    loop, audit, _ = build_loop(broker, guard=False)

    result = await loop.run(spread(), guard_first_submission=False)

    assert result.outcome is WalkOutcome.UNRESOLVED
    assert result.recovered_disconnects >= 3
    assert "walk.finished" in audit.events


async def test_two_working_orders_under_one_identifier_are_all_cancelled():
    """The broker does NOT enforce `external_identifier` uniqueness (measured,
    chunk 1), so this is reachable — and it is spec §9's runaway case: both can
    fill and the position doubles. Missing an entry costs a trade."""
    broker = Broker()
    alerts: list[tuple[str, dict]] = []

    # Two orders already working under our identifier, then a failed submit sends
    # the loop down the recovery path that has to choose between them.
    await broker.submit(spread(), dry_run=False)
    await broker.submit(spread(), dry_run=False)
    broker.fail_next(ConnectionError("socket"))

    loop, _, _ = build_loop(
        broker, guard=False, alert=lambda kind, detail: alerts.append((kind, detail))
    )
    result = await loop.run(spread(), guard_first_submission=False)

    assert result.outcome is WalkOutcome.UNRESOLVED
    assert sorted(broker.cancelled) == [1000, 1001]
    assert alerts and alerts[0][0] == "walk_duplicate_working_orders"


async def test_an_order_that_keeps_disappearing_does_not_loop_forever():
    """A walk is bounded by its ladder — a fixed number of reprices, then walk
    away. A cycle that observes nothing never reprices, so it needs its own bound
    or a broker that intermittently omits the order loops until the session ends."""
    class Amnesiac(Broker):
        async def get_live_orders(self):
            answer = await super().get_live_orders()
            # Visible on the in-market check that opens each cycle, absent from the
            # post-hold read that would decide the next price. The one pattern that
            # never resolves and never reprices.
            return answer if self.polls % 2 else LiveOrders()

    broker = Amnesiac()
    alerts: list[tuple[str, dict]] = []
    loop, _, _ = build_loop(
        broker, guard=False, alert=lambda kind, detail: alerts.append((kind, detail))
    )

    result = await loop.run(spread(), guard_first_submission=False)

    assert result.outcome is WalkOutcome.UNRESOLVED
    assert broker.replaced == []
    assert any(kind == "walk_order_kept_disappearing" for kind, _ in alerts)


async def test_a_failed_submit_with_nothing_at_the_broker_says_so_specifically():
    """Whether the call never arrived or arrived and died is not decidable from
    here, and the two differ by an order. Say so; the caller can re-run the guard.

    It says so as `NO_ORDER_EXISTS` and NOT as `UNRESOLVED` (2026-08-28). The broker
    WAS reached and answered — that is the opposite of the one outcome meaning nobody
    read it, and the exit path acts on the difference: this one keeps a budgeted
    retry, `UNRESOLVED` spends the position's attempt outright."""
    broker = Broker()
    broker.fail_next(ConnectionError("socket"))
    loop, _, _ = build_loop(broker, guard=False)

    result = await loop.run(spread(), guard_first_submission=False)

    assert result.outcome is WalkOutcome.NO_ORDER_EXISTS
    assert result.reason and "no order exists" in result.reason


async def test_a_refused_submit_carries_the_brokers_own_account_of_it():
    """S1. A refusal to accept an order must not report as an ABSENCE. (2026-09-04)

    `_recover` has the causing exception in scope and threw it away, so the two
    shapes cert actually produces arrived identically — a permanent
    `instrument_validation_failed` naming the unsupported symbol, and a transient
    `timeout_error`, both as `no_order_exists` with `reject_reason: null` and the same
    fixed template `reason`. 7 of 8 on 2026-09-02; 5 of 7 (71%) on 2026-09-03.
    """
    broker = Broker()
    broker.fail_next(
        RuntimeError("instrument_validation_failed: Trading of SPXW 260903P05000000 "
                     "is not supported")
    )
    loop, audit, _ = build_loop(broker, guard=False)

    result = await loop.run(spread(), guard_first_submission=False)

    assert result.outcome is WalkOutcome.NO_ORDER_EXISTS
    assert result.mutation_error is not None
    assert "instrument_validation_failed" in result.mutation_error
    assert "SPXW 260903P05000000" in result.mutation_error
    # The TYPE rides along, the way every error_type/error pair in this codebase does,
    # so a filter is possible without anyone matching prose.
    assert result.mutation_error.startswith("RuntimeError: ")
    # And it reaches the Tier 1 record every downstream reader (desk, the entry alert,
    # the exit report) actually reads.
    assert audit.payload("walk.finished")["mutation_error"] == result.mutation_error


async def test_a_refused_REPRICE_carries_it_too():
    """S2. `_recover` is shared, so the field is not "submit only" in reach.

    A failed replace cancels the original and lands the replacement nowhere; the
    branch and the field are the same ones. `reason` is what names which mutation
    failed — the field carries the broker's words, not the call's identity.
    """
    class ReplaceDiesAndNothingRemains(Broker):
        """The replace raises, and the broker then reports nothing under the key."""

        async def replace(self, broker_order_id: int, order: Order) -> Order:
            self.replace_calls += 1
            raise ConnectionError("502 Bad Gateway")

        async def get_live_orders(self) -> LiveOrders:
            if self.replace_calls:
                return LiveOrders()
            return await super().get_live_orders()

    broker = ReplaceDiesAndNothingRemains()
    loop, _, _ = build_loop(broker, guard=False)

    result = await loop.run(spread(), guard_first_submission=False)

    assert result.outcome is WalkOutcome.NO_ORDER_EXISTS
    assert result.mutation_error == "ConnectionError: 502 Bad Gateway"
    assert result.reason and result.reason.startswith("replace failed")


async def test_mutation_error_is_absent_on_every_other_outcome():
    """S3. The contract, held by the GATE in `_finish` rather than by call sites.

    "Present only on `NO_ORDER_EXISTS`" is the same discipline `reject_reason` keeps
    one line above it, and it is enforced where the value is assembled — so a caller
    that passes one on the wrong outcome cannot put it there.
    """
    broker = Broker()
    broker.on_poll = [lambda b: b.fill(1000)]
    loop, _, _ = build_loop(broker, guard=False)
    result = await loop.run(spread(), guard_first_submission=False)
    assert result.outcome is WalkOutcome.FILLED
    assert result.mutation_error is None

    broker = Broker()
    broker.reject_next()
    loop, _, _ = build_loop(broker, guard=False)
    result = await loop.run(spread(), guard_first_submission=False)
    assert result.outcome is WalkOutcome.REJECTED
    # A rejection has its OWN field; the two are different events and neither
    # implies the other.
    assert result.mutation_error is None

    # And the GATE itself, driven directly — no production call site passes a
    # `mutation_error` on another outcome today, so the two assertions above would
    # hold with the gate deleted. "Present only on NO_ORDER_EXISTS" is a property of
    # `_finish`, and a contract nothing exercises is a comment.
    loop, _, _ = build_loop(Broker(), guard=False)
    order = spread()
    state = _Walk(sent=order, walk=PriceWalk.from_policy(order.limit_price, policy()))
    for outcome in (
        WalkOutcome.FILLED,
        WalkOutcome.WALKED_AWAY,
        WalkOutcome.UNRESOLVED,
        WalkOutcome.BLOCKED,
        WalkOutcome.ABORTED,
    ):
        smuggled = await loop._finish(
            state, outcome, reason="x", mutation_error="ConnectionError: 502"
        )
        assert smuggled.mutation_error is None, (
            f"a mutation_error survived onto {outcome} — the gate in `_finish` is what "
            "makes the field's contract true regardless of what a caller passes"
        )
    kept = await loop._finish(
        state, WalkOutcome.NO_ORDER_EXISTS, reason="x", mutation_error="ConnectionError: 502"
    )
    assert kept.mutation_error == "ConnectionError: 502"


async def test_reporting_the_reason_changes_no_outcome_and_no_retry():
    """S4. The non-goal, pinned. This is a REPORTING change and nothing else.

    Nothing may branch on `mutation_error` — `Order.reject_reason`'s rule, inherited
    whole. Distinguishing a permanent refusal from a transient one is a separate and
    larger decision, and building it by matching this text is the tempting wrong fix.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "argus" / "execution"
    matchers = {"startswith", "endswith", "find", "index", "count", "__contains__"}
    for module in ("loop.py", "watchdog.py", "entry.py"):
        tree = ast.parse((src / module).read_text())
        for node in ast.walk(tree):
            # It may be ASSIGNED, PASSED and RENDERED. It may not be an operand of a
            # comparison, nor the receiver of a string matcher. `_finish`'s gate
            # (`mutation_error if outcome is NO_ORDER_EXISTS else None`) is deliberately
            # permitted: it branches on the OUTCOME and merely selects this value —
            # the field's own contract, enforced where the value is assembled.
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                assert not any("mutation_error" in ast.unparse(o) for o in operands), (
                    f"{module} compares `mutation_error` ({ast.unparse(node)[:120]}) — "
                    "it is what an operator reads, never what the code decides on"
                )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in matchers
            ):
                assert "mutation_error" not in ast.unparse(node.func.value), (
                    f"{module} matches on `mutation_error`'s TEXT "
                    f"({ast.unparse(node)[:120]}) — broker prose changes wording "
                    "without notice and a rail built on it fails open the day it does"
                )


async def test_an_unreadable_broker_stays_unresolved():
    """The other side of the split: a re-query that never answers is still the
    outcome that means nothing may be inferred. Asserted alongside its sibling so a
    later tidy cannot collapse them back into one."""
    class Mute(Broker):
        async def get_live_orders(self):
            raise ConnectionError("no answer")

    broker = Mute()
    broker.fail_next(ConnectionError("socket"))
    loop, _, _ = build_loop(broker, guard=False)

    result = await loop.run(spread(), guard_first_submission=False)

    assert result.outcome is WalkOutcome.UNRESOLVED


async def test_a_failed_cancel_does_not_overturn_a_decided_outcome():
    """The decision stands; the working order it failed to cancel is what
    restart-time order reconciliation exists to catch."""
    class NoCancel(Broker):
        async def cancel(self, broker_order_id):
            raise ConnectionError("cancel failed")

    broker = NoCancel()
    alerts: list[tuple[str, dict]] = []
    loop, _, _ = build_loop(
        broker, alert=lambda kind, detail: alerts.append((kind, detail))
    )

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.WALKED_AWAY
    assert not result.cancel_requested      # we asked; it did not take
    assert any(kind == "walk_cancel_failed" for kind, _ in alerts)


# -- LiveOrderView: an order present but unreadable ---------------------------
#
# The third caller of `get_live_orders()`, and the one whose handling of an
# unmappable order is a judgement call rather than an obvious answer. Omitting it
# reads as "absent", and "absent" is a verdict this loop acts on: `by_id` absent
# feeds the blind-cycle counter, `by_identifier` absent tells `_recover` nothing
# was sent. Neither may be concluded from an order the broker DOES hold.


async def test_by_id_raises_rather_than_reporting_our_own_order_absent():
    broker = Broker()
    broker.add_unmappable(broker_order_id=1000)
    view = LiveOrderView(broker)

    with pytest.raises(UnmappableOrderError, match="1000"):
        await view.by_id(1000)


async def test_by_identifier_raises_rather_than_reporting_nothing_was_sent():
    """The dangerous direction. `_recover` reads an empty answer as "the submit
    never landed" — which, against an order that is actually working, is how the
    only duplicate protection in the system gets bypassed on the recovery path."""
    broker = Broker()
    broker.add_unmappable(external_identifier=spread().external_identifier)
    view = LiveOrderView(broker)

    with pytest.raises(UnmappableOrderError):
        await view.by_identifier(spread().external_identifier)


async def test_an_unmappable_order_belonging_to_someone_else_is_not_this_views_problem():
    """Scoped to the query, like every other answer this view gives.

    A stranger's unreadable order is real and is dealt with elsewhere — an alert
    from the audited layer, a halt at the next reconcile. Raising here would abort
    a walk over an order that has nothing to do with it.
    """
    broker = Broker()
    ack = await broker.submit(spread(), dry_run=False)
    broker.add_unmappable(broker_order_id=9999, external_identifier="placed-by-hand")
    view = LiveOrderView(broker)

    assert (await view.by_id(ack.order.broker_order_id)) is not None
    assert len(await view.by_identifier(spread().external_identifier)) == 1


async def test_a_walk_whose_own_order_goes_unreadable_ends_unresolved_not_filled():
    """End to end: the raise lands in machinery that already exists.

    `_observe` treats it as a broker that cannot answer — retried, then escalated
    to `BrokerUnavailable`, which ends the walk UNRESOLVED. Same handling an
    unreachable broker already gets, and for the same reason: UNRESOLVED is the
    outcome from which nothing may be inferred, so no cancel is attempted and no
    fill is assumed. Bounded and loud, and it leaves the order to restart-time
    reconciliation — which now halts on it.
    """
    class GoesUnreadable(Broker):
        """The order is placed and working, then its status stops parsing."""

        async def get_live_orders(self):
            answer = await super().get_live_orders()
            if not answer.unmappable and self._orders:
                for order_id in list(self._orders):
                    self.add_unmappable(broker_order_id=order_id)
                return LiveOrders(unmappable=tuple(self._unmappable))
            return answer

    broker = GoesUnreadable()
    loop, _, _ = build_loop(broker, guard=False)

    result = await loop.run(spread(), guard_first_submission=False)

    assert result.outcome is WalkOutcome.UNRESOLVED
    assert result.filled_contracts == 0, "nothing may be inferred from an unread order"
    # It went down the broker-cannot-answer path rather than the blind-cycle one:
    # "unreadable" must never be recorded as "absent".
    assert result.recovered_disconnects >= 1
    assert "unreadable" in (result.reason or "")


# -- audit trail --------------------------------------------------------------


async def test_the_walk_records_how_it_started_and_how_it_ended():
    broker = Broker()
    broker.on_poll = [None, lambda b: b.fill(1000)]
    loop, audit, _ = build_loop(broker)

    await loop.run(spread(), guard_first_submission=True)

    # The guard's own Tier 2 `order.duplicate_check` precedes it (2026-09-03): the
    # question "is it safe to send?" is asked BEFORE `walk.started` is written, so
    # that record means a walk actually began. What is pinned is the pairing — every
    # `walk.started` has a `walk.finished` and the first walk record is the start.
    walk_records = [e for e in audit.events if e.startswith("walk.")]
    assert walk_records[0] == "walk.started"
    assert audit.events[-1] == "walk.finished"
    started = audit.payload("walk.started")
    assert started["walk_away_price"] == 0.70
    assert started["policy"]["coa_settle_s"] == 1.0
    finished = audit.payload("walk.finished")
    assert finished["candidate_id"] == "20260729-put-5000-4995"
    # The pre-send Tier 1 write still beats the wire — the decorator's job, not
    # this loop's, and it is not bypassed by going through the loop.
    assert audit.events.index("order.submit_attempt") < audit.events.index("order.submitted")


async def test_reprices_are_tier_2_and_the_outcome_is_tier_1():
    """A step is high-volume and survivable; how a trade idea ended is not."""
    broker = Broker()
    loop, audit, _ = build_loop(broker)
    await loop.run(spread(), guard_first_submission=True)

    assert audit.events.count("tier2:walk.step") == 3
    assert audit.events.count("walk.finished") == 1


# -- property 4: the underlying-drift circuit breaker -------------------------
#
# The ladder is pure arithmetic on a reference fixed at gate time, and in a fast
# market that reference goes stale. The breaker is a separate check at the cycle
# boundary, injected as a callable — the loop still has no quote path of its own,
# which the structural tests below keep true.


class DriftSpy:
    """A scripted `DriftCheckFn` that counts how often it was consulted."""

    def __init__(self, *, reasons: list[str | None] | None = None, raises=None) -> None:
        self.reasons = reasons if reasons is not None else []
        self.raises = raises
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        if not self.reasons:
            return None
        return self.reasons.pop(0)


async def test_no_drift_check_is_exactly_the_behaviour_that_shipped():
    """The bolt-on is opt-in. With no check the walk is bounded only by its ladder,
    which is what every caller written before this seam existed relies on."""
    broker = Broker()
    loop, audit, _ = build_loop(broker)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert result.outcome is WalkOutcome.WALKED_AWAY
    assert len(broker.replaced) == 3
    assert audit.payload("walk.started")["drift_check_armed"] is False


async def test_underlying_drift_abandons_the_walk_and_cancels_the_order():
    """The order does not get left working at a price the loop has stopped standing
    behind — same posture as the walk-away path, which is the only other way a walk
    gives up with something live at the broker."""
    broker = Broker()
    drift = DriftSpy(reasons=["underlying moved 3.40 points"])
    loop, audit, _ = build_loop(broker, drift_check=drift)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert result.outcome is WalkOutcome.DRIFTED
    assert result.reason == "underlying moved 3.40 points"
    assert result.cancel_requested
    assert broker.cancelled == [result.broker_order_id]
    assert broker.replaced == [], "the price must not step on the cycle it abandons"
    assert audit.payload("walk.started")["drift_check_armed"] is True
    assert audit.payload("walk.finished")["outcome"] == "drifted"


async def test_drifted_is_not_walked_away_and_keeps_its_budget_visible():
    """The two outcomes end the same way and mean opposite things.

    `WALKED_AWAY` says the concession budget was spent and the auction still would
    not pay — evidence about `entry_walk_away_pct`. `DRIFTED` says the walk was
    pulled with budget left — evidence about nothing but the tape. Collapsing them
    would quietly corrupt the statistic the walk-away threshold gets tuned from.
    """
    broker = Broker()
    # Continue for one cycle, then drift: the ladder still has rungs below.
    drift = DriftSpy(reasons=[None, "moved"])
    loop, _, _ = build_loop(broker, drift_check=drift)

    result = await loop.run(spread("0.85"), guard_first_submission=True)

    assert result.outcome is WalkOutcome.DRIFTED
    assert result.steps == 1
    assert PriceWalk.from_policy(Decimal("0.85"), policy()).price_at(result.steps + 1) \
        is not None, "the walk must still have had somewhere to go"


async def test_a_fill_in_the_same_cycle_beats_a_drift_abort():
    """Ordering, and it is the one that costs money to get wrong: cancelling an
    order that has already filled is far worse than walking a price one cycle too
    long. The check runs AFTER the observation is resolved, so a fill wins."""
    broker = Broker()
    broker.on_poll = [None, lambda b: b.fill(1000)]
    drift = DriftSpy(reasons=["moved"])
    loop, _, _ = build_loop(broker, drift_check=drift)

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.FILLED
    assert drift.calls == 0, "a resolved cycle must not reach the drift check at all"
    assert broker.cancelled == []


async def test_an_unreadable_cycle_gets_no_drift_verdict_either():
    """"I could not read the order" never reprices (existing property) and must not
    acquire a verdict of any other kind either. A read gap is not an observation."""
    class Amnesiac(Broker):
        async def get_live_orders(self):
            answer = await super().get_live_orders()
            return answer if self.polls % 2 else LiveOrders()

    drift = DriftSpy(reasons=["moved"])
    loop, _, _ = build_loop(Amnesiac(), guard=False, drift_check=drift)

    result = await loop.run(spread(), guard_first_submission=False)

    assert result.outcome is WalkOutcome.UNRESOLVED
    assert drift.calls == 0


async def test_a_drift_check_that_raises_abandons_the_walk_instead_of_propagating():
    """The check is in-memory arithmetic, so an exception is a defect in it — not a
    broker problem. Propagating would hand it to chunk 5's hard-halt region and take
    the process down; this path cancels the order, so the condition that justifies a
    halt (a live order nobody is walking) never arises."""
    broker = Broker()
    alerts: list[tuple[str, dict]] = []
    loop, _, _ = build_loop(
        broker,
        drift_check=DriftSpy(raises=RuntimeError("quote book exploded")),
        alert=lambda kind, detail: alerts.append((kind, detail)),
    )

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.outcome is WalkOutcome.DRIFTED
    assert "RuntimeError" in (result.reason or "")
    assert broker.cancelled == [result.broker_order_id]
    assert [kind for kind, _ in alerts] == ["walk_drift_check_failed"]


async def test_the_breaker_is_a_cycle_check_not_a_pre_flight_one():
    """It guards the ladder, which does not exist until an order is working. The
    order is submitted and reaches the market first; the gate already decided
    whether the candidate was worth sending at all."""
    broker = Broker()
    drift = DriftSpy(reasons=["moved"])
    loop, _, _ = build_loop(broker, drift_check=drift)

    result = await loop.run(spread(), guard_first_submission=True)

    assert len(broker.submitted) == 1
    assert result.steps == 0
    assert drift.calls == 1


# -- structural ---------------------------------------------------------------


def _parse_loop() -> ast.Module:
    path = Path(__file__).resolve().parent.parent / "src" / "argus" / "execution" / "loop.py"
    return ast.parse(path.read_text())


def test_every_production_from_policy_call_passes_a_width():
    """The cap is only as good as the call sites that arm it. (2026-09-17)

    `width` is keyword-only with a `None` default, so that the many unit tests which
    build a walk from a bare price keep working — which means a production caller that
    forgets it gets an UNCAPPED floor and no error. That is the shape of the defect
    this fix closes, so the two real call sites are pinned structurally instead:
    `loop.run` (the walk the state machine actually follows) and
    `watchdog.close` (the `walk_away_price` it records, which must be the number the
    walk will really stop at).

    A third production caller is fine — it just has to pass `width` and join this list.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "argus" / "execution"
    offenders = []
    for module in ("loop.py", "watchdog.py", "entry.py", "pipeline.py"):
        tree = ast.parse((src / module).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "from_policy"
                and isinstance(func.value, ast.Name)
                and func.value.id == "PriceWalk"
            ):
                continue
            if not any(kw.arg == "width" for kw in node.keywords):
                offenders.append(f"{module}:{node.lineno}")
    assert offenders == [], (
        "PriceWalk.from_policy called without `width=` in production — the floor "
        "would not be capped at the spread's own width:\n  " + "\n  ".join(offenders)
    )


def test_the_loop_never_imports_the_raw_broker_adapter():
    """Chunk 3 talks to `AuditedOrderExecution` and the port, never to chunk 1's
    tastytrade adapter — importing it is how the audited path gets bypassed."""
    offenders = []
    for node in ast.walk(_parse_loop()):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "TastytradeOrderExecution":
                    offenders.append(f"{node.module}.{alias.name}")
    assert offenders == []


def test_the_loop_never_takes_a_market_data_dependency():
    """The reprice ladder is derived from the gate-approved mid, never re-quoted.

    This is pinned structurally because the failure it prevents is silent and
    expensive. An order's legs carry OCC symbols while `MarketDataPort` is keyed by
    `streamer_symbol` — different strings — so `market.latest(order_leg.symbol)`
    returns `None` forever without raising. Chunk 4 hit exactly that and fixed it
    with `LegQuotes`. This loop is immune only because it has no quote path at all,
    and this test is what keeps that true: adding one has to break CI first, at
    which point the answer is `LegQuotes`, never a second resolver.
    """
    forbidden_modules = ("marketdata", "signal.models", "signal.chain")
    # The quote-CONSUMING half of zerobid. `TICK`/`round_to_tick` are pure
    # arithmetic and are exactly what this module should be using.
    forbidden_names = ("floor_bid", "floored_mid", "spread_mid")

    offenders = []
    for node in ast.walk(_parse_loop()):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(f"argus.{m}" in module for m in forbidden_modules):
                offenders.append(f"line {node.lineno}: imports {module}")
            for alias in node.names:
                if alias.name in forbidden_names:
                    offenders.append(f"line {node.lineno}: imports {alias.name}")
    assert offenders == []


def test_the_reprice_price_comes_only_from_the_pure_ladder():
    """`PriceWalk` does arithmetic and nothing else — no I/O, no clock, no feed.

    Asserted on the call graph rather than trusted: if `price_at` ever calls
    anything but the shared tick rounder, the ladder has acquired an input that
    can fail or go stale, and the walk-away threshold stops being measurable
    against a fixed reference.
    """
    calls: set[str] = set()
    for node in ast.walk(_parse_loop()):
        if isinstance(node, ast.ClassDef) and node.name == "PriceWalk":
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "price_at":
                    calls = {
                        ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)
                    }
    assert calls == {"round_to_tick"}


def test_the_loop_sleeps_only_through_its_injected_seam():
    """Every wait goes through `self._sleep`, so the whole suite runs on a fake
    clock. A direct `asyncio.sleep` would make one test take real seconds and the
    next contributor copy it."""
    offenders = []
    for node in ast.walk(_parse_loop()):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "sleep":
            continue
        target = node.func.value
        if isinstance(target, ast.Name) and target.id == "asyncio":
            offenders.append(f"line {node.lineno}: asyncio.sleep")
    assert offenders == []


def test_the_walk_failure_alert_is_never_silently_omitted():
    """`walk_unresolved` — the `max_recovery_attempts`-exhausted outcome that means "a
    human must go look" — was documented as alerting while `alert` defaulted to None.
    Chunk 4 built the real sink; the default is now `log_alert`, so an omitted
    argument degrades to a log line rather than to silence."""
    from argus.alerts import log_alert

    loop, _, _ = build_loop(Broker())
    assert loop._alert is log_alert
