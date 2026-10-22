"""The process-wide order-request governor.

Insurance, not cadence: designed volume is under 1 req/s against a confirmed 40
req/s ceiling, so the interesting assertions are all about the ABNORMAL path — a
runaway caller — and about the normal path being untouched.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import pytest

from argus.execution.fake import FakeOrderExecution
from argus.execution.models import LegAction, Order, OrderDirection, OrderLeg
from argus.execution.ratelimit import OrderRateLimiter, ThrottledOrderExecution


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def limiter(rate: float = 40.0) -> tuple[OrderRateLimiter, FakeTime]:
    clock = FakeTime()
    return OrderRateLimiter(rate, clock=clock.clock, sleep=clock.sleep), clock


def spread() -> Order:
    return Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", LegAction.SELL_TO_OPEN, 1),
            OrderLeg("SPXW  P4995", LegAction.BUY_TO_OPEN, 1),
        ),
        limit_price=Decimal("0.85"),
        external_identifier="argus-open-20260729-put-5000-4995",
    )


def test_the_interval_is_the_reciprocal_of_the_confirmed_ceiling():
    """40 req/s per IP on /orders — tastytrade support, 2026-07-29."""
    gate, _ = limiter(40.0)
    assert gate.min_interval_s == pytest.approx(0.025)


async def test_the_normal_path_never_waits():
    """At 2% of the ceiling nothing should ever queue, and a governor that slowed
    the designed cadence would be shaping timing it has no business shaping."""
    gate, clock = limiter(40.0)
    for _ in range(5):
        assert await gate.acquire() == 0.0
        clock.now += 3.0            # a normal 3s walk cycle between requests
    assert clock.slept == []
    assert gate.throttled_count == 0


async def test_a_runaway_caller_is_spaced_out():
    """The case this exists for: a defect elsewhere firing requests back to back."""
    gate, clock = limiter(40.0)
    await gate.acquire()            # t=0, free
    for _ in range(4):
        assert await gate.acquire() == pytest.approx(0.025)
    assert gate.throttled_count == 4
    # Five requests took four full intervals — exactly the ceiling, not more.
    assert clock.now == pytest.approx(0.1)


async def test_pacing_is_off_the_scheduled_slot_not_the_wakeup():
    """A slow event loop must not let the effective rate drift upward. If the
    sleep overshoots, the NEXT slot is still measured from where it should have
    been, so an overshoot is absorbed rather than compounded."""
    gate, clock = limiter(40.0)

    async def overshooting_sleep(seconds: float) -> None:
        clock.slept.append(seconds)
        clock.now += seconds + 0.05      # the loop was busy

    gate._sleep = overshooting_sleep     # type: ignore[assignment]
    await gate.acquire()
    await gate.acquire()
    # The overshoot already carried us past the next slot, so no further wait.
    assert await gate.acquire() == 0.0


async def test_concurrent_callers_queue_instead_of_bursting():
    """Releasing the lock before the sleep would let every queued caller compute
    the same 'next allowed' instant and fire together — the burst this prevents."""
    gate = OrderRateLimiter(40.0)          # a real clock: this one measures elapsed time
    started = time.monotonic()
    await asyncio.gather(*(gate.acquire() for _ in range(4)))
    elapsed = time.monotonic() - started

    # Four concurrent callers are spread across three intervals, not released
    # together after one. 0.9x the nominal 75ms only guards against clock
    # resolution — `asyncio.sleep` never returns early.
    assert elapsed >= 3 * gate.min_interval_s * 0.9
    assert gate.throttled_count == 3


def test_a_nonsense_rate_is_refused():
    with pytest.raises(ValueError, match="must be > 0"):
        OrderRateLimiter(0)


async def test_the_decorator_passes_every_call_through_unchanged():
    """Pure pass-through: it changes WHEN a call happens, never what it does."""
    gate, _ = limiter()
    broker = FakeOrderExecution()
    throttled = ThrottledOrderExecution(broker, gate)

    ack = await throttled.submit(spread(), dry_run=False)
    order_id = ack.order.broker_order_id
    assert order_id is not None
    replaced = await throttled.replace(order_id, spread().with_limit_price(Decimal("0.80")))
    await throttled.cancel(replaced.broker_order_id or 0)
    live = await throttled.get_live_orders()

    assert len(broker.submitted) == 1
    assert len(broker.replaced) == 1
    assert broker.cancelled == [replaced.broker_order_id]
    assert live


async def test_reads_are_governed_too():
    """The guard's `get_live_orders()` and the recovery path's requery run through
    the same ceiling — a runaway that only reads is still a runaway."""
    gate, clock = limiter(40.0)
    throttled = ThrottledOrderExecution(FakeOrderExecution(), gate)
    for _ in range(3):
        await throttled.get_live_orders()
    assert gate.throttled_count == 2
