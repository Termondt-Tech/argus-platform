"""A minimum interval between requests to tastytrade, shared by every caller.

Extracted from `execution/ratelimit.py` on 2026-10-08 when a second strategy needed
it (platform by extraction: shared code moves only when a second strategy uses
it). The SPX book's governor is this class, unchanged; its reasoning, the confirmed
**40 order requests per second, per IP, on `/orders`**, and the layering
(`AuditedOrderExecution(ThrottledOrderExecution(...))`) stay documented there.

Deliberately the dumbest thing that works: a minimum interval, enforced under a lock
held across the sleep, paced off the scheduled slot. Not a token bucket: a bucket
lets a runaway caller empty its burst before the brake engages.

Two uses, with opposite expectations:
- **The SPX book** runs three orders of magnitude inside the limit, so a throttled
  request means a runaway loop somewhere and is logged as a WARNING.
- **A slow poller** paces itself on purpose, well under any limit, because
  tastytrade publishes none for its other endpoints and answers too many requests
  with a bare 429 (cert did, 2026-10-08). There a throttled request is the design,
  so `warn_on_throttle=False`.

`paced_read` is the other half: a READ through the governor, retried with backoff.
The probe's account reads and the capture's instrument reads both use it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

# The SPX book's logger name, kept so its journal lines read exactly as before.
logger = logging.getLogger("argus.execution.ratelimit")
logger_reads = logging.getLogger("argus.broker.reads")

ClockFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


class OrderRateLimiter:
    """A shared minimum interval between requests. One per process.

    `max_requests_per_second` is a ceiling, not a target. The interval is enforced
    with a lock, so concurrent callers queue rather than interleave inside the
    window.
    """

    def __init__(
        self,
        max_requests_per_second: float,
        *,
        clock: ClockFn | None = None,
        sleep: SleepFn | None = None,
        warn_on_throttle: bool = True,
    ) -> None:
        if max_requests_per_second <= 0:
            raise ValueError(
                f"max_requests_per_second must be > 0, got {max_requests_per_second!r}"
            )
        self._min_interval = 1.0 / max_requests_per_second
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._warn = warn_on_throttle
        self._lock = asyncio.Lock()
        self._last: float | None = None
        #: How many requests had to wait. For the SPX book a non-zero value in a
        #: normal session is itself the signal: at 2% of the ceiling nothing
        #: should ever queue.
        self.throttled_count = 0

    @property
    def min_interval_s(self) -> float:
        return self._min_interval

    async def acquire(self) -> float:
        """Wait until a request may be sent. Returns the seconds spent waiting.

        The lock is held across the sleep on purpose: releasing it early would let
        every queued caller compute the same "next allowed" instant and fire
        together, which is the burst this exists to prevent.
        """
        async with self._lock:
            now = self._clock()
            if self._last is None:
                self._last = now
                return 0.0
            earliest = self._last + self._min_interval
            delay = earliest - now
            if delay <= 0:
                self._last = now
                return 0.0
            self.throttled_count += 1
            if self._warn:
                logger.warning(
                    "order request throttled for %.3fs — at %.0f req/s this should not "
                    "happen in normal operation; check for a runaway loop",
                    delay, 1.0 / self._min_interval,
                )
            await self._sleep(delay)
            # `earliest`, not "now again": pacing off the scheduled slot keeps the
            # spacing exact even when the sleep overshoots, so a slow event loop
            # cannot make the effective rate drift upward on the next call.
            self._last = earliest
            return delay


#: Waits before each retry of a failed READ; after the last, the failure is raised.
#: Longer than the SPX walk's (0.5, 1, 2): a bare 429 is the case this is for.
READ_BACKOFF_S = (1.0, 2.0, 4.0)

T = TypeVar("T")


def brief(exc: BaseException) -> str:
    """One line for a log: the SDK wraps a 429 in a page of HTML."""
    text = str(exc)
    if "429 Too Many Requests" in text:
        return "429 Too Many Requests"
    first = text.splitlines()[0] if text else ""
    return f"{type(exc).__name__}: {first[:160]}"


async def paced_read(
    limiter: OrderRateLimiter,
    what: str,
    fn: Callable[[], Awaitable[T]],
    *,
    backoff_s: tuple[float, ...] = READ_BACKOFF_S,
    sleep: SleepFn | None = None,
) -> T:
    """A READ through the governor, retried after each of `backoff_s`; the last
    failure raises. Only for reads: a read has no side effect to repeat, and a
    send or a cancel is never retried blind (the SPX walk's `_first_submit`)."""
    sleep = sleep or asyncio.sleep
    for attempt in range(len(backoff_s) + 1):
        await limiter.acquire()
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — retried, then raised
            if attempt == len(backoff_s):
                raise
            logger_reads.warning("%s failed (%s); retrying in %.0fs", what, brief(exc),
                                 backoff_s[attempt])
            await sleep(backoff_s[attempt])
    raise AssertionError("unreachable")

