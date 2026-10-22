"""Event-loop-lag heartbeat (spec §4, §9).

The single-event-loop design (§4) means one blocking call stalls market data, the
signal engine, the watchdog, and kill-switch responsiveness *all at once*. This
task is the early-warning detector for that condition: it schedules a wakeup every
`interval_s` and measures how much later than scheduled the wakeup actually fires.
On a healthy loop that lateness ("lag") is sub-millisecond; when something blocks
the loop, the lag equals the duration of the block.

It does not itself do I/O on the hot path — it only measures, updates in-memory
metrics, and fires an `AlertFn`. Whether a *sustained* stall should escalate to a
fail-closed halt is the caller's policy (`on_sustained`), which maps to the §11
open question. Alerts fire on the transition into a breach and once when a run of
breaches becomes sustained — not on every sample — so a stall doesn't spam.

**RECOVERY IS LOGGED, NEVER ALERTED** (2026-08-05). It used to alert, and that
made the per-episode alert count TWO, which only looks harmless if stalls are
long and rare. They are not: the droplet's account liveness refresh
(`[staleness] account_refresh_s`) puts ~60-170ms of blocking work on the loop
every ~10s, so at `interval_s = 0.1` / `lag_alert_threshold_s = 0.05` this
detector produced a breach/recovered PAIR every 10 seconds — 13,406 breaches in
eleven days, measured on the droplet's journal. That was invisible while
`log_alert` was the only sink and became a Slack storm the moment `notify.py`
was wired in. A recovery carries nothing to act on (the breach it closes already
alerted, and `after_breaches` is in the metrics), so it is a log line. The
transition-in and sustained alerts are unchanged; volume on those is the
notifier's per-kind throttle to solve, not this module's.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from argus.alerts import AlertFn, log_alert

logger = logging.getLogger("argus.heartbeat")

# on_sustained(lag_s) -> awaitable; called once per sustained-stall episode.
OnSustained = Callable[[float], Awaitable[None]]


class LoopHeartbeat:
    def __init__(
        self,
        *,
        interval_s: float,
        lag_threshold_s: float,
        sustained_breach_count: int,
        alert: AlertFn | None = None,
        on_sustained: OnSustained | None = None,
    ) -> None:
        self._interval = interval_s
        self._threshold = lag_threshold_s
        self._sustained_count = sustained_breach_count
        self._alert = alert or log_alert
        self._on_sustained = on_sustained

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._sustained_tasks: set[asyncio.Task[None]] = set()

        # Metrics, for the monitoring surface and tests.
        self._last_lag = 0.0
        self._max_lag = 0.0
        self._breach_count = 0
        self._consecutive = 0
        self._sustained_events = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="loop-heartbeat")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        # Let any in-flight sustained-stall handler finish.
        if self._sustained_tasks:
            await asyncio.gather(*self._sustained_tasks, return_exceptions=True)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        target = loop.time()
        while not self._stop.is_set():
            target += self._interval
            delay = target - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Already behind schedule (the loop was blocked). Stay cooperative
                # instead of hot-looping while we measure the catch-up lag.
                await asyncio.sleep(0)
            lag = loop.time() - target
            self._observe(lag)

    # -- measurement -------------------------------------------------------

    def _observe(self, lag: float) -> None:
        self._last_lag = lag
        if lag > self._max_lag:
            self._max_lag = lag

        if lag > self._threshold:
            self._breach_count += 1
            self._consecutive += 1
            if self._consecutive == 1:
                self._alert(
                    "loop_lag_breach",
                    {"lag_s": round(lag, 4), "threshold_s": self._threshold},
                )
            if self._consecutive == self._sustained_count:
                self._sustained_events += 1
                self._alert(
                    "loop_lag_sustained",
                    {"consecutive": self._consecutive, "lag_s": round(lag, 4)},
                )
                self._escalate(lag)
        else:
            if self._consecutive > 0:
                # Logged, not alerted — see the module docstring. Nothing here is
                # actionable: the breach that opened this episode already alerted,
                # and the run length is available in the metrics and on /status.
                logger.info(
                    "event-loop lag recovered after %d consecutive breach(es)",
                    self._consecutive,
                )
            self._consecutive = 0

    def _escalate(self, lag: float) -> None:
        if self._on_sustained is None:
            return
        task = asyncio.create_task(self._run_sustained(lag))
        self._sustained_tasks.add(task)
        task.add_done_callback(self._sustained_tasks.discard)

    async def _run_sustained(self, lag: float) -> None:
        try:
            assert self._on_sustained is not None
            await self._on_sustained(lag)
        except Exception:  # a failing escalation must not kill the heartbeat
            logger.exception("sustained-stall handler failed")

    # -- metrics -----------------------------------------------------------

    @property
    def last_lag(self) -> float:
        return self._last_lag

    @property
    def max_lag(self) -> float:
        return self._max_lag

    @property
    def breach_count(self) -> int:
        return self._breach_count

    @property
    def sustained_events(self) -> int:
        return self._sustained_events
