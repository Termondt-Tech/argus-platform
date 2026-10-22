"""QuoteBook — the shared in-memory quote state + staleness accounting.

Both the real DXLink adapter and the fake share this. It is deliberately tiny and
pure (no I/O, injected clock) so the staleness logic — the safety-critical part —
is exhaustively testable without a stream.

Fail-closed is baked in: an unknown symbol reports age +inf and `is_stale` True.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from argus.marketdata.models import OptionGreeks, Quote
from argus.staleness import is_stale

Clock = Callable[[], float]  # monotonic seconds


class QuoteBook:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._quotes: dict[str, Quote] = {}
        self._last_event_at: float | None = None

    def update(
        self,
        symbol: str,
        bid: float,
        ask: float,
        bid_size: float,
        ask_size: float,
    ) -> Quote:
        now = self._clock()
        quote = Quote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            received_at=now,
        )
        self._quotes[symbol] = quote
        self._last_event_at = now
        return quote

    def latest(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    def age(self, symbol: str) -> float:
        quote = self._quotes.get(symbol)
        if quote is None:
            return math.inf
        # max(0, ...) guards against a monotonic clock reporting equal timestamps.
        return max(0.0, self._clock() - quote.received_at)

    def is_stale(self, symbol: str, max_age_s: float) -> bool:
        return is_stale(self.age(symbol), max_age_s)

    def last_event_age(self) -> float:
        if self._last_event_at is None:
            return math.inf
        return max(0.0, self._clock() - self._last_event_at)


class GreeksBook:
    """Latest greeks per option symbol, with the same staleness discipline."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._greeks: dict[str, OptionGreeks] = {}

    def update(
        self,
        symbol: str,
        delta: float,
        gamma: float,
        theta: float,
        vega: float,
        implied_volatility: float,
    ) -> OptionGreeks:
        greeks = OptionGreeks(
            symbol=symbol,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            implied_volatility=implied_volatility,
            received_at=self._clock(),
        )
        self._greeks[symbol] = greeks
        return greeks

    def latest(self, symbol: str) -> OptionGreeks | None:
        return self._greeks.get(symbol)

    def age(self, symbol: str) -> float:
        greeks = self._greeks.get(symbol)
        if greeks is None:
            return math.inf
        return max(0.0, self._clock() - greeks.received_at)

    def is_stale(self, symbol: str, max_age_s: float) -> bool:
        return is_stale(self.age(symbol), max_age_s)
