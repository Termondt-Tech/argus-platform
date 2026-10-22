"""AccountStateCache — live account state from the stream, with staleness.

Mirrors the market-data `QuoteBook`: a small, pure, injected-clock state holder so
the safety-relevant staleness logic is fully testable. Fed by the AlertStreamer
adapter; read by the RiskSnapshot builder.

**A position leaves this cache two ways, and for a long time only one worked.**

A `CurrentPosition` event carrying a flat/zero quantity means the position closed
— it is removed rather than lingering as a stale open position. That is the stream
path, and it depends on receiving one specific frame.

`evict_absent` is the other: a symbol an authoritative REST snapshot does not
report is not held. Absence, not a zero row, is how the REST positions endpoint
says "closed" — so before 2026-08-19 a single missed close frame left an entry
that NOTHING could remove, because `seed()` merged the snapshot in and never
subtracted. Measured that day: the cache read 3 legs against REST's 2 for over an
hour, pinning `open_spread_equivalent` at the concurrent limit and paging the one
watchdog condition that has no settle allowance.
→ docs/decisions/2026-08-19-account-cache-ghost-positions.md
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection
from dataclasses import dataclass

from argus.account.models import AccountSnapshot, Position
from argus.staleness import is_stale as _is_stale

Clock = Callable[[], float]  # monotonic seconds


@dataclass(frozen=True)
class EvictedPosition:
    """One entry dropped by `evict_absent`, with how stale it had gone.

    `age_s` is the measurement the eviction threshold gets re-tuned from — the same
    discipline `watchdog.book_settle_s` is sized under — so it is carried out to the
    caller rather than logged and discarded.
    """

    position: Position
    age_s: float


class AccountStateCache:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._positions: dict[str, Position] = {}
        #: symbol -> when it was last written by a stream event or a REST seed.
        #: Per-symbol, and deliberately NOT `_last_event_at`: that one is the link's
        #: liveness (any event refreshes it), this one is per-position evidence.
        self._last_seen: dict[str, float] = {}
        self._net_liq: float | None = None
        self._last_event_at: float | None = None

    def update_position(self, position: Position) -> None:
        self._touch()
        if position.signed_quantity == 0:
            self._positions.pop(position.symbol, None)
            self._last_seen.pop(position.symbol, None)
        else:
            self._positions[position.symbol] = position
            self._last_seen[position.symbol] = self._clock()

    def evict_absent(
        self, present: Collection[str], *, older_than_s: float
    ) -> tuple[EvictedPosition, ...]:
        """Drop entries an authoritative snapshot did not report. (2026-08-19)

        `present` is the symbol set from a SUCCESSFUL, COMPLETE REST fetch. A failed
        or partial fetch must never reach here — the caller leaves the cache
        untouched instead, so a broken link keeps looking broken.

        **The age gate is the whole safety argument, and it guards the direction
        that fails OPEN.** The broker's position book trails a fill by ~4-5s
        (measured 2026-08-13, the lag `watchdog.book_settle_s` exists to absorb), so
        a REST snapshot taken inside that window legitimately omits a position that
        really is held. Evicting on absence alone would drop a just-opened leg,
        under-count `open_spread_equivalent`, and hand out a concurrent slot that is
        already spoken for.

        The two cases separate cleanly on age. A real position is in EVERY snapshot,
        so each seed re-touches it and its age never exceeds the refresh interval. A
        ghost is in none of them, so its age climbs without bound. Requiring the age
        to clear a threshold set well above both the refresh interval and the
        propagation lag means only the second kind is ever eligible.

        Rounds toward keeping: an entry that has not aged out yet survives this pass
        and is reconsidered on the next one. Over-counting blocks entries, which is
        the recoverable direction; under-counting authorizes one.
        """
        now = self._clock()
        dropped: list[EvictedPosition] = []
        for symbol in list(self._positions):
            if symbol in present:
                continue
            last_seen = self._last_seen.get(symbol)
            if last_seen is None:
                # Defensive: an entry with no timestamp is treated as seen NOW so it
                # ages into eligibility, rather than becoming immortal for want of a
                # measurement.
                self._last_seen[symbol] = now
                continue
            age = now - last_seen
            if age < older_than_s:
                continue
            self._last_seen.pop(symbol, None)
            dropped.append(EvictedPosition(self._positions.pop(symbol), round(age, 3)))
        return tuple(dropped)

    def update_net_liq(self, net_liquidating_value: float) -> None:
        self._touch()
        self._net_liq = net_liquidating_value

    def _touch(self) -> None:
        self._last_event_at = self._clock()

    def snapshot(self) -> AccountSnapshot | None:
        if self._last_event_at is None:
            return None
        return AccountSnapshot(
            positions=tuple(self._positions.values()),
            net_liquidating_value=self._net_liq,
            captured_at=self._clock(),
        )

    def age(self) -> float:
        if self._last_event_at is None:
            return math.inf
        return max(0.0, self._clock() - self._last_event_at)

    def is_stale(self, max_age_s: float) -> bool:
        return _is_stale(self.age(), max_age_s)
