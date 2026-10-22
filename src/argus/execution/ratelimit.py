"""A process-wide floor under how fast Argus can hit tastytrade's `/orders`.

**This is insurance, not cadence.** tastytrade support confirmed the limit on
2026-07-29: **40 order requests per second, per IP, on `/orders`**. This strategy
runs one or two concurrent trade ideas walking at 1-5s per step, so real steady-
state volume is under one request per second — roughly 2% of the ceiling. Nothing
about the price-walking loop's design is shaped by this limit, and nothing should
be: if a cadence ever needs to be slowed to fit inside 40/s, the bug is the thing
generating the requests.

Which is exactly why the governor exists. The volume this guards against is not
the designed volume — it is a defect somewhere else: a walk that fails to notice
it is finished and reprices in a tight loop, two loops started for the same
candidate, a recovery path that retries without backing off. Spec §9 lists
"duplicate/runaway order submission" as a named risk, and the duplicate half has a
guard (`guard.py`); this is the runaway half. An IP-level block from the broker
would take out the kill switch's ability to cancel working orders at the same
moment a runaway loop is the reason you need it.

So it is deliberately the dumbest thing that works — a minimum interval between
requests, shared by every caller in the process. Not a token bucket, not a
priority queue, no burst allowance: a bucket would let a runaway defect empty its
burst before the brake engages, which is the one case this is for. Smoothing
everything to an even spacing costs the normal path nothing (it is already three
orders of magnitude inside the limit) and caps the abnormal path hard.

**Applied as a decorator, not by convention.** `ThrottledOrderExecution` implements
`OrderExecutionPort` and wraps another implementation, so the governor covers every
order request the process makes — including ones made by code that has never heard
of it (the duplicate guard's `get_live_orders()`, the recovery path's requery,
anything chunk 4/5 adds later). A helper the loop had to remember to call would
protect only the paths that remembered.

**Layering: audit OUTSIDE, throttle INSIDE.**

    AuditedOrderExecution(ThrottledOrderExecution(TastytradeOrderExecution(...)))

The Tier 1 "I am about to send this" write must be durable before the wire
regardless of how long the wire has to wait for its slot (spec §6). Throttling
outside the audit layer would delay the record too, so a process killed while
queued would have sent nothing AND recorded nothing — recoverable, but it throws
away the ordering guarantee for no reason. Inside, the record is always written
first and the wait happens between the record and the send, which is precisely
where a delay is harmless.
"""

from __future__ import annotations

# The governor itself is platform code since 2026-10-08 (a second strategy uses
# it too). Same class, same defaults: re-exported here so the SPX book's
# wiring and tests are unchanged.
from argus.broker.ratelimit import OrderRateLimiter
from argus.execution.broker import OrderExecutionPort
from argus.execution.models import LiveOrders, Order, OrderAck

__all__ = ["OrderRateLimiter", "ThrottledOrderExecution"]


class ThrottledOrderExecution:
    """`OrderExecutionPort` + the process-wide request governor. Pure pass-through
    otherwise: it changes nothing about any call except when it happens."""

    def __init__(self, execution: OrderExecutionPort, limiter: OrderRateLimiter) -> None:
        self._execution = execution
        self._limiter = limiter

    async def submit(self, order: Order, *, dry_run: bool) -> OrderAck:
        await self._limiter.acquire()
        return await self._execution.submit(order, dry_run=dry_run)

    async def cancel(self, broker_order_id: int) -> None:
        await self._limiter.acquire()
        await self._execution.cancel(broker_order_id)

    async def replace(self, broker_order_id: int, order: Order) -> Order:
        await self._limiter.acquire()
        return await self._execution.replace(broker_order_id, order)

    async def get_live_orders(self) -> LiveOrders:
        await self._limiter.acquire()
        return await self._execution.get_live_orders()
