"""The market-crossing close: priced by what can fill, reported by the rung it stopped
at, and tracked while it rests. (2026-09-25, Parts B, C and D)

2026-09-24: a flatten walked 0.05 -> 0.20, went unfilled at and above a 0.10
natural, then crossed at 0.10 — below its own ladder — and paged
`walked_away_at: -0.05`. The 0.10 then rested 22 minutes against a 0.05 natural
while the spent-exit page said nothing would try again.

→ docs/designs/2026-09-25-deferring-a-far-otm-flatten.md §5-§7
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from argus.execution.loop import WalkOutcome, WalkResult
from argus.execution.models import LiveOrders, OrderDirection, OrderStatus
from argus.execution.residual_orders import ResidualOrderRecheck
from argus.execution.watchdog import ExitOutcome
from argus.execution.zerobid import TICK
from tests.support import make_config
from tests.test_watchdog import FakeAudit, FakeClock
from tests.test_watchdog_exit import CREDIT_TRIGGER, build, held


def _walked_away(order, final: str, steps: int = 3) -> WalkResult:
    return WalkResult(
        outcome=WalkOutcome.WALKED_AWAY,
        external_identifier=order.external_identifier,
        broker_order_id=1,
        steps=steps,
        requested_contracts=order.contracts,
        filled_contracts=0,
        final_limit_price=Decimal(final),
        cancel_requested=True,
        recovered_disconnects=0,
    )


def _order(executor, quotes):
    return executor._build_exit_order(held(), quotes.market())


# -- Part B: the cross ----------------------------------------------------------


def test_2026_09_24_no_long_bid_crosses_at_the_natural():
    """The long leg has no bid, so no leg-in exists and a higher price buys nothing
    (measured): the cross rests at the natural, the cheapest the evidence supports."""
    executor, _, _, quotes, *_ = build(short=(0.00, 0.10), long_=(0.00, 0.05))
    order = _order(executor, quotes)
    price = executor._cross_limit(order, _walked_away(order, "-0.20"))

    assert price.limit == Decimal("-0.10")
    assert price.basis == "natural_no_long_bid"
    assert price.long_bid == 0.0


def test_with_a_long_bid_the_cross_never_rebids_a_refused_price():
    """The same walk with a bid on the long leg: the natural is tradeable, and the
    cross continues the ladder by one rung."""
    executor, _, _, quotes, *_ = build(short=(0.05, 0.15), long_=(0.05, 0.10))
    order = _order(executor, quotes)
    price = executor._cross_limit(order, _walked_away(order, "-0.20"))

    assert price.natural == pytest.approx(0.10)
    assert price.limit == Decimal("-0.25")
    assert price.basis == "past_ladder"


def test_a_natural_above_the_ladder_is_paid():
    executor, _, _, quotes, *_ = build(short=(2.00, 2.40), long_=(0.10, 0.20))
    order = _order(executor, quotes)
    price = executor._cross_limit(order, _walked_away(order, "-2.05"))
    assert (price.limit, price.basis) == (Decimal("-2.30"), "natural")


@pytest.mark.parametrize(
    "last,basis", [("-4.95", "past_ladder"), ("-5.00", "width")]
)
def test_the_width_always_wins(last, basis):
    """One rung past -4.95 IS the width; one rung past -5.00 is not payable, so the
    cross is sent AT the width (D2) — never above it."""
    executor, _, _, quotes, *_ = build(short=(4.80, 4.95), long_=(0.05, 0.10))
    order = _order(executor, quotes)
    price = executor._cross_limit(order, _walked_away(order, last))
    assert price.limit == Decimal("-5.00") and price.basis == basis


def test_an_unpriceable_natural_is_still_cross_unpriceable():
    """Option C was NOT built: `|last| + step` is computable here, and the cross still
    refuses without a readable market — the retryable 2026-08-31 outcome, unchanged."""
    executor, _, _, quotes, *_ = build(short=(0.00, 0.10), long_=(0.00, 0.05))
    order = _order(executor, quotes)
    quotes.set((0.00, 0.00), (0.00, 0.05))          # no offer on the short leg
    assert executor._cross_limit(order, _walked_away(order, "-0.20")) is None


def test_the_cross_invariants_hold_for_every_book_and_ladder():
    """Invariants 1-4 of §5, over random books, ladders and float noise."""
    rng = random.Random(9)
    executor, _, _, quotes, *_ = build()
    step = executor._policy.step
    for _ in range(3000):
        short_ask = round(rng.choice([0.05, 0.10, 0.25, 1.0, 2.2, 4.9, 6.0, 40.0]) + rng.uniform(-1e-12, 1e-12), 12)
        long_bid = rng.choice([0.0, 0.0, 0.05, 0.30, 1.0])
        quotes.set((0.0, max(short_ask, 0.0)), (long_bid, long_bid + 0.05))
        try:
            order = executor._build_exit_order(held(), quotes.market())
        except ValueError:          # a mid that rounds to nothing: no order to cross
            continue
        last = -(TICK * rng.randint(1, 100))
        walk = _walked_away(order, str(last))
        price = executor._cross_limit(order, walk)
        if price is None:
            continue
        magnitude = -price.limit
        width = Decimal("5.00")
        assert price.limit < 0 and magnitude % TICK == 0
        assert magnitude <= width                                        # 1
        if long_bid > 0.0 and price.basis != "width":
            assert magnitude >= abs(last) + step                         # 2
        natural = Decimal(str(round(short_ask - long_bid, 2)))
        if natural <= width:
            assert magnitude >= natural - TICK / 2                       # 3


async def test_a_real_walk_away_on_the_2026_09_24_book_crosses_at_the_natural():
    """End to end through the real walk: the ladder is unchanged and so is the cross,
    because the long leg has no bid — so this release cannot make a 09-24 day dearer."""
    executor, _, audit, quotes, *_ = build(short=(0.00, 0.10), long_=(0.00, 0.05))
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert report.outcome is ExitOutcome.CROSSED
    assert report.cross_limit_price == Decimal("-0.10")
    assert audit.payload("watchdog.crossed")["limit_basis"] == "natural_no_long_bid"


# -- Part C: the rung it reports ---------------------------------------------------


async def test_the_row_and_the_page_report_where_the_walk_stopped():
    executor, _, audit, quotes, _, alerts, _ = build()
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    row = audit.payload("watchdog.crossed")
    page = next(d for k, d in alerts if k == "watchdog_crossed_market")
    assert row["walked_away_at"] == float(report.walk.final_limit_price) == -2.05
    assert row["opening_limit"] == -1.70 and row["walk_steps"] == report.walk.steps > 0
    # ONE builder: every shared key agrees.
    shared = set(row) & set(page)
    assert shared >= {"walked_away_at", "opening_limit", "walk_steps", "crossing_limit",
                      "limit_basis", "crossing_debit", "short_ask", "long_bid"}
    assert all(row[k] == page[k] for k in shared)


def test_a_zero_step_walk_away_reports_its_opening_limit():
    """Backward compatible with every row that was already right."""
    executor, _, _, quotes, *_ = build()
    order = _order(executor, quotes)
    walk = _walked_away(order, str(order.limit_price), steps=0)
    price = executor._cross_limit(order, walk)
    detail = executor._crossed_detail(order, walk, price)
    assert detail["walked_away_at"] == detail["opening_limit"] == float(order.limit_price)


def test_the_detail_reads_the_walk_not_the_order():
    """The mutation this exists for: read `order.limit_price` and this fails."""
    executor, _, _, quotes, *_ = build()
    order = _order(executor, quotes)
    walk = _walked_away(order, "-2.05")
    detail = executor._crossed_detail(order, walk, executor._cross_limit(order, walk))
    assert detail["walked_away_at"] == -2.05 != float(order.limit_price)


# -- Part D: the resting cross is tracked --------------------------------------------


def _registry(config=None, clock=None):
    audit = FakeAudit()
    cancelled: list[int] = []

    async def cancel(order_id: int) -> None:
        cancelled.append(order_id)

    registry = ResidualOrderRecheck(
        audit=audit,
        config=config or make_config(watchdog={"unmanaged_order_cancel": True}),
        cancel=cancel,
        alert=lambda k, d: None,
        clock=clock or FakeClock(),
    )
    return registry, audit, cancelled


async def test_a_crossed_exit_registers_its_cross_as_a_resting_close():
    registry, _, _ = _registry()
    executor, _, _, quotes, *_ = build(residuals=registry)
    report = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    crosses = [r for r in registry.outstanding() if r.walk_outcome == "crossed"]
    assert len(crosses) == 1
    (cross,) = crosses
    assert cross.broker_order_id == report.cross_broker_order_id
    assert cross.minted_direction is OrderDirection.CLOSE
    assert cross.working_expected and cross.limit_price == float(report.cross_limit_price)
    row = registry.working_close_for(report.external_identifier)
    assert row is not None and row.broker_order_id == report.cross_broker_order_id


async def test_a_resting_cross_pages_only_after_the_unmanaged_bound_and_is_never_cancelled():
    clock = FakeClock()
    registry, audit, cancelled = _registry(clock=clock)
    registry.register(
        broker_order_id=77, external_identifier="argus-close-20260729-put-x",
        minted_direction=OrderDirection.CLOSE, candidate_id="20260729-put-x",
        walk_outcome="crossed", cancel_requested=False, limit_price=-0.10,
        working_expected=True,
    )
    from tests.test_residual_orders import order as residual_order

    live = LiveOrders(orders=(residual_order(
        broker_order_id=77, identifier="argus-close-20260729-put-x",
        direction=OrderDirection.CLOSE, status=OrderStatus.LIVE,
    ),))
    clock.advance(5.0)                       # past the 2s cancel-settle allowance
    await registry.step(live)
    assert "order.residual_unmanaged" not in audit.events, "a cross resting is its job"

    clock.advance(30.0)                      # past unmanaged_order_max_s (30)
    await registry.step(live)
    assert "order.residual_unmanaged" in audit.events
    assert cancelled == [], "a CLOSE is never cancelled, even with cancel enabled"


async def test_a_resting_cross_that_fills_resolves_quietly_not_as_an_abandoned_fill():
    """Review fix (2026-09-25): `order.residual_filled` and its page mean "an ABANDONED
    order filled — the position is real and gets adopted". A cross filling is the cross
    doing its job — the position is CLOSED — so it resolves at Tier 2 like any other
    finished residual, with no page. The walk's own abandoned rung keeps the Tier 1
    record and the page, unchanged."""
    from tests.test_residual_orders import order as residual_order

    alerts: list[str] = []
    registry = ResidualOrderRecheck(
        audit=(audit := FakeAudit()),
        config=make_config(),
        cancel=lambda order_id: None,
        alert=lambda kind, detail: alerts.append(kind),
        clock=(clock := FakeClock()),
    )
    close_id = "argus-close-20260729-put-x"
    registry.register(
        broker_order_id=77, external_identifier=close_id,
        minted_direction=OrderDirection.CLOSE, candidate_id="20260729-put-x",
        walk_outcome="crossed", cancel_requested=False, limit_price=-0.10,
        working_expected=True,
    )
    registry.register(
        broker_order_id=76, external_identifier=close_id,
        minted_direction=OrderDirection.CLOSE, candidate_id="20260729-put-x",
        walk_outcome="walked_away", cancel_requested=True,
    )
    clock.advance(1.0)
    await registry.step(LiveOrders(orders=(
        residual_order(broker_order_id=77, identifier=close_id,
                       direction=OrderDirection.CLOSE, status=OrderStatus.FILLED,
                       remaining=0),
        residual_order(broker_order_id=76, identifier=close_id,
                       direction=OrderDirection.CLOSE, status=OrderStatus.FILLED,
                       remaining=0),
    )))

    assert registry.outstanding() == ()
    assert audit.events.count("order.residual_filled") == 1, "the abandoned rung only"
    assert audit.payload("order.residual_filled")["broker_order_id"] == 76
    assert alerts == ["residual_order_filled"]
    resolved = [p for c, p in audit.payloads if c == "order.residual_resolved"]
    assert [(p["broker_order_id"], p["sighting"]) for p in resolved] == [(77, "filled")]
    assert resolved[0]["working_expected"] is True


async def test_the_spent_page_and_status_name_a_close_still_working():
    """2026-09-24: 43 pages saying nothing would try again while a cross rested. With
    the cross registered, the page and the row say a close IS working, and to check
    the broker before sending anything by hand (C5's double close)."""
    from argus.execution.models import OrderDirection as _Dir
    from argus.execution.models import sibling_identifier
    from argus.execution.store import OpenSpreadScan
    from argus.session import _to_unclosable
    from argus.status import _spent_exit_cell
    from tests.test_watchdog import (
        RecordingExecutor,
        _spend_the_attempt,
        _triggered_and_priceable,
        build_quotes,
        build_watchdog,
        spread,
    )

    clock = FakeClock()
    registry, _, _ = _registry(clock=clock)
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.CROSSED), residuals=registry,
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.CROSSED)
    close_id = sibling_identifier(spread().external_identifier, _Dir.CLOSE)
    registry.register(
        broker_order_id=509260365, external_identifier=close_id,
        minted_direction=_Dir.CLOSE, candidate_id=spread().candidate_id,
        walk_outcome="crossed", cancel_requested=False, limit_price=-0.10,
        working_expected=True,
    )

    clock.advance(25.0)
    _triggered_and_priceable(clock, market)
    await watchdog.check_once()

    page = audit.payload("watchdog.exit_spent_still_held")
    assert page["working_close"]["broker_order_id"] == 509260365
    assert page["working_close"]["limit_price"] == -0.10
    (row,) = watchdog.spent_exit_attempts()
    assert (row.working_close_order_id, row.working_close_limit) == (509260365, -0.10)
    html = _spent_exit_cell(_to_unclosable([row]))
    assert "CLOSE WORKING at the broker: #509260365 at -0.10" in html
    assert "check the broker BEFORE sending anything by hand" in html


async def test_without_a_working_close_the_page_is_unchanged():
    from argus.execution.store import OpenSpreadScan
    from tests.test_watchdog import (
        RecordingExecutor,
        _spend_the_attempt,
        _triggered_and_priceable,
        build_quotes,
        build_watchdog,
        spread,
    )

    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, audit, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.NOT_RESOLVED)
    clock.advance(25.0)
    _triggered_and_priceable(clock, market)
    await watchdog.check_once()
    assert audit.payload("watchdog.exit_spent_still_held")["working_close"] is None
    (row,) = watchdog.spent_exit_attempts()
    assert row.working_close_order_id is None
