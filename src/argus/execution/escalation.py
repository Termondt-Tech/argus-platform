"""Edge tracking for conditions that persist across cadence ticks. ONE implementation.

Lifted out of `watchdog.py` on 2026-08-30, unchanged, when the residual-order
recheck (P5) needed the same mechanism. It could not simply import it from there:
`watchdog.py` imports `loop.py`, `loop.py` registers residuals with
`residual_orders.py`, and `residual_orders.py` importing `watchdog.py` would close
that ring. A second copy of rising/falling/overdue was the alternative, and the one
thing this class is FOR is that every condition in the system gets the same bound on
noise and the same bound on silence — two copies is how one of them quietly loses
the `overdue` repeat and goes back to paging once.

The name keeps its underscore: `watchdog.py` re-exports it under the same name, so
every existing reference (including `tests/test_watchdog.py`) still reads
`from argus.execution.watchdog import _Escalation`.
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: Monotonic seconds. Declared here rather than imported from `loop.py`, which is
#: the module this one exists to stay upstream of.
ClockFn = Callable[[], float]


class _Escalation:
    """Edge tracking for conditions that persist across cadence ticks.

    Several of the watchdog's worst states are also its most repetitive: an options
    feed gap makes a position unevaluable on every tick, and a triggered position
    whose legs cannot be priced fails to build an order on every tick. Each is a
    Tier 1 event the first time and pure noise the two-hundredth — and a Tier 1
    record per tick is not more safety, it is a trail nobody can read (and, at a
    sub-second cadence, sustained contention on the writer lock spec §6 protects).

    So each (position, condition) pair escalates on the RISING edge only, and
    re-arms when the condition clears. The same shape `account/stream.py` uses for
    its `refresh_failed`/`refresh_recovered` pair.

    **Edge-triggering bounds the noise, so something else has to bound the
    silence** (2026-08-20, extended 2026-08-21). THREE of these tags —
    `_UNATTEMPTED`, `_UNPRICEABLE` and `_SPENT` — mean "the watchdog has decided this
    position must be closed and it is not closed". That is not a state that becomes
    less serious by lasting longer, but under rising-edge escalation alone it pages
    once and then goes quiet forever: for the first two while the retry loop spins,
    and for `_SPENT` while nothing runs at all.

    `overdue` is the answer, and it REPEATS rather than firing once. The distinction
    matters and was got wrong on the first pass: what the 2026-08-05 storm argues for
    is a bounded RATE, not a single event, and those are not the same rule. A single
    page is one missed notification away from the silence this exists to prevent —
    for a condition whose meaning is "an open 0DTE position cannot be closed", that
    trade is unacceptable. So it re-fires every `after_s` for as long as the condition
    holds, which is ~1 record per bound rather than one per cadence tick.

    **The phone is protected one layer down, not here.** `SlackNotifier` throttles
    at 900s and coalesces the rest into a `(+N suppressed)` count on the next delivery
    — so repeats cost one message per 15 minutes, carrying the count that
    distinguishes "again" from "continuously". Re-implementing that defence here, more
    strictly, is what produced the once-only version.

    **That throttle is per (kind, POSITION), and it had to be** (2026-08-21, B1). The
    reasoning above is a rate argument about ONE condition on ONE position, and it was
    written while the throttle keyed on `kind` alone. Two positions is the norm here,
    not the tail — they are same-underlying, same-day, and a move that makes one
    unclosable routinely does the same to the other — so per-kind coalescing meant the
    second position's page arriving as a bare `(+1 suppressed)` up to 15 minutes late.
    The escalation payloads therefore carry `candidate_id` at the TOP level, which is
    what `notify.alert_scope` reads; burying it inside `position` silently reverts
    this.
    → docs/decisions/2026-08-20-exit-order-freshness-bound.md,
      docs/decisions/2026-08-21-alert-throttle-is-per-position.md
    """

    def __init__(self, clock: ClockFn | None = None) -> None:
        #: (key, tag) -> when it went active. A dict rather than a set purely so
        #: `overdue` can measure the episode; `rising`/`falling` are unchanged.
        self._on: dict[tuple[str, str], float] = {}
        #: (key, tag) -> how many `after_s` bounds have already been reported for the
        #: CURRENT episode. Cleared by `falling`/`forget`, so a new episode restarts
        #: the count rather than inheriting a spent one.
        self._loud: dict[tuple[str, str], int] = {}
        self._clock = clock or time.monotonic

    def rising(self, key: str, tag: str) -> bool:
        """Mark (key, tag) as active; True only on the transition into it."""
        slot = (key, tag)
        if slot in self._on:
            return False
        self._on[slot] = self._clock()
        return True

    def falling(self, key: str, tag: str) -> bool:
        """Mark (key, tag) as clear; True only on the transition out of it."""
        slot = (key, tag)
        if slot not in self._on:
            return False
        del self._on[slot]
        self._loud.pop(slot, None)
        return True

    def active(self, key: str, tag: str) -> float | None:
        """When this (key, tag) episode started, or None if it is not active.

        Read-only, added 2026-09-10 so a caller that needs BOTH the edges and the live
        state does not have to keep a second copy of `_on` in sync with `rising`/
        `falling` — which is the drift `/status` has already been fixed for twice.
        """
        return self._on.get((key, tag))

    def overdue(self, key: str, tag: str, after_s: float) -> float | None:
        """Elapsed seconds each time this episode crosses another `after_s`; else None.

        Fires at 1x, 2x, 3x ... the bound — once per interval, never once per tick,
        and never only once. Returns None for a slot that is not active at all: a
        condition that is not up cannot be overdue.

        Counted in whole multiples of `after_s` rather than "seconds since the last
        page" so the cadence cannot drift with an irregular tick rate, and so a long
        gap between evaluations reports the interval it actually landed in.
        """
        slot = (key, tag)
        since = self._on.get(slot)
        if since is None or after_s <= 0:
            return None
        elapsed = self._clock() - since
        crossed = int(elapsed // after_s)
        if crossed <= self._loud.get(slot, 0):
            return None
        self._loud[slot] = crossed
        return elapsed

    def forget(self, key: str) -> None:
        for slot in [s for s in self._on if s[0] == key]:
            del self._on[slot]
            self._loud.pop(slot, None)
