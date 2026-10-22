"""SessionScheduler — drives one session per trading day in an always-on process.

This is the piece that turns the timer-driven, one-shot-per-day model into an
always-on one. It owns no trading state; it decides *when* to run a session and
delegates the *what* to an injected `run_session` callback. The decision is a
pure function (`plan`) so every scheduling case — before the open, mid-session,
after the close, weekend, holiday, half-day early close — is unit-testable with a
constructed clock and no async.

Design invariants:

- **One session attempt per trading day.** Once a day's session has been run — or
  aborted (login gave up, reconciliation refused) — that day is done; the
  scheduler sleeps to the NEXT trading day's open rather than re-entering today.
  This is what keeps a fast-failing session (e.g. cert down at the open) from
  becoming a hot loop that hammers the broker's auth endpoint. The retry that
  DOES happen at the open lives inside the session (bounded, ban-aware); the
  scheduler never restarts a session the same day.

- **Everything is interruptible by the process stop event.** Idle waits and the
  in-session close timer both unblock the instant `stop` is set, so SIGTERM
  unwinds cleanly whether the process is idling overnight or mid-session.

- **A session runs until its `session_stop` event.** The scheduler sets that
  event at the ET close (or immediately on process stop). The session body is
  responsible only for stopping promptly when it fires — the scheduler owns the
  clock, so a half-day's early close is enforced here, not left to the body.

- **Whose session.** By default a day's session is the market's own,
  `calendar.session_window` (the SPX book's, unchanged). Another strategy passes
  its own `window(day)`. One scheduler, a window per strategy
  (docs/decisions/2026-10-22-one-platform-a-strategy-per-process.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from argus.clock import et_now
from argus.marketcalendar import MarketCalendar

logger = logging.getLogger("argus.scheduler")

# run_session(trading_date, session_close, session_stop) -> awaitable.
# Returns when the session ends (close reached, process stop, or an early abort).
RunSessionFn = Callable[[date, datetime, asyncio.Event], Awaitable[None]]

NowFn = Callable[[], datetime]
# window(day) -> (start, end) in ET, or None when there is no session that day.
WindowFn = Callable[[date], tuple[datetime, datetime] | None]
# wait(seconds) -> awaitable; may return early (e.g. on process stop). Injected in
# tests to advance a fake clock without real sleeping.
WaitFn = Callable[[float], Awaitable[None]]

# How far ahead to scan for the next trading day. The longest market-closed run
# (a holiday bridging a weekend) is a handful of days; 14 is comfortable margin.
_HORIZON_DAYS = 14


@dataclass(frozen=True)
class Decision:
    """What the scheduler should do next, given the current time."""

    run: bool
    trading_date: date | None = None
    session_close: datetime | None = None
    wake_at: datetime | None = None


class SessionScheduler:
    def __init__(
        self,
        *,
        calendar: MarketCalendar,
        run_session: RunSessionFn,
        stop: asyncio.Event,
        now_et: NowFn | None = None,
        wait_fn: WaitFn | None = None,
        window: WindowFn | None = None,
    ) -> None:
        self._calendar = calendar
        self._window = window or calendar.session_window
        self._run_session = run_session
        self._stop = stop
        self._now_et = now_et or et_now
        self._wait_fn = wait_fn

    # -- pure decision (fully testable without async) ----------------------

    def plan(self, now: datetime) -> Decision:
        """Given `now` (tz-aware ET), decide whether to run a session or sleep."""
        window = self._window(now.date())
        if window is not None and window[0] <= now < window[1]:
            return Decision(run=True, trading_date=now.date(), session_close=window[1])
        return Decision(run=False, wake_at=self._next_session_open(now))

    def next_session_open(self, now: datetime) -> datetime:
        """The next session open strictly after `now` (public, for the status view).

        Mid-session this returns TOMORROW's open — i.e. the next session, not the
        current one — which is exactly what "next session open" means on the
        monitoring page while a session is running.
        """
        return self._next_session_open(now)

    def _next_session_open(self, now: datetime) -> datetime:
        """The next session-open strictly after `now`, scanning forward by day."""
        for offset in range(_HORIZON_DAYS + 1):
            d = (now + timedelta(days=offset)).date()
            window = self._window(d)
            if window is not None and window[0] > now:
                return window[0]
        # No trading day within the horizon — should never happen with a sane
        # calendar. Fall back to a bounded re-check rather than sleeping forever.
        logger.warning("no trading day within %d days of %s; re-checking in 1h", _HORIZON_DAYS, now)
        return now + timedelta(hours=1)

    # -- async driver ------------------------------------------------------

    async def run(self) -> None:
        """Loop until the process stop event is set.

        Each iteration either runs one trading day's session (then sleeps to the
        next day) or sleeps until the next session open. Both waits are
        interruptible, so the loop exits promptly on stop.
        """
        while not self._stop.is_set():
            decision = self.plan(self._now_et())
            if decision.run:
                assert decision.trading_date is not None
                assert decision.session_close is not None
                await self._run_one_session(decision.trading_date, decision.session_close)
                if self._stop.is_set():
                    break
                # The day is done regardless of how the session ended. Advancing
                # from *now* (>= the open) skips the rest of today and lands on the
                # next trading day's open — never re-entering today.
                await self._sleep_until(self._next_session_open(self._now_et()))
            else:
                assert decision.wake_at is not None
                await self._sleep_until(decision.wake_at)

    async def _run_one_session(self, trading_date: date, session_close: datetime) -> None:
        """Run the session for `trading_date`, ending it at `session_close`.

        A background timer sets `session_stop` at the close; the process stop
        event also sets it, so SIGTERM mid-session ends the day cleanly. Both
        helper tasks are always torn down, even if the session raises.
        """
        session_stop = asyncio.Event()

        async def closer() -> None:
            seconds = max(0.0, (session_close - self._now_et()).total_seconds())
            await self._wait(seconds)
            session_stop.set()

        async def stop_bridge() -> None:
            await self._stop.wait()
            session_stop.set()

        closer_task = asyncio.create_task(closer(), name="session-closer")
        bridge_task = asyncio.create_task(stop_bridge(), name="session-stop-bridge")
        try:
            await self._run_session(trading_date, session_close, session_stop)
        finally:
            session_stop.set()
            for task in (closer_task, bridge_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _sleep_until(self, wake_at: datetime) -> None:
        delay = (wake_at - self._now_et()).total_seconds()
        await self._wait(max(0.0, delay))

    async def _wait(self, seconds: float) -> None:
        """Sleep up to `seconds`, returning early if the process stop is set."""
        if self._wait_fn is not None:
            await self._wait_fn(seconds)
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
