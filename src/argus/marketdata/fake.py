"""FakeMarketData — deterministic, clock-controlled MarketDataPort.

Two jobs:
1. Back the tests for everything that consumes market data, without a network.
2. Be the seed for the future deterministic tastytrade simulator (see the
   simulator design note). Its controllable clock is exactly what lets a test —
   or later a scenario script — reproduce a stream stall, a spread blowout, or a
   quote gap on demand, which sandbox cannot do.

Time is virtual: it does not advance on its own. `push()` records a quote at the
current virtual time; `advance()` moves the clock so staleness can be exercised
deterministically.
"""

from __future__ import annotations

from collections.abc import Iterable

from argus.marketdata.book import GreeksBook, QuoteBook
from argus.marketdata.models import OptionGreeks, Quote


class FakeMarketData:
    def __init__(self, start_time: float = 0.0) -> None:
        self._now = start_time
        self._book = QuoteBook(self._clock)
        self._greeks = GreeksBook(self._clock)
        self._subscribed: set[str] = set()
        self._running = False

    def _clock(self) -> float:
        return self._now

    # -- virtual-time controls (test / scenario harness) -------------------

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move virtual time backwards")
        self._now += seconds

    def push(
        self,
        symbol: str,
        bid: float,
        ask: float,
        *,
        bid_size: float = 1.0,
        ask_size: float = 1.0,
    ) -> Quote:
        return self._book.update(symbol, bid, ask, bid_size, ask_size)

    def push_greeks(
        self,
        symbol: str,
        delta: float,
        *,
        gamma: float = 0.0,
        theta: float = 0.0,
        vega: float = 0.0,
        implied_volatility: float = 0.0,
    ) -> OptionGreeks:
        return self._greeks.update(symbol, delta, gamma, theta, vega, implied_volatility)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def subscribed(self) -> frozenset[str]:
        return frozenset(self._subscribed)

    # -- MarketDataPort ----------------------------------------------------

    def latest(self, symbol: str) -> Quote | None:
        return self._book.latest(symbol)

    def greeks(self, symbol: str) -> OptionGreeks | None:
        return self._greeks.latest(symbol)

    def greeks_age(self, symbol: str) -> float:
        return self._greeks.age(symbol)

    def age(self, symbol: str) -> float:
        return self._book.age(symbol)

    def is_stale(self, symbol: str, max_age_s: float) -> bool:
        return self._book.is_stale(symbol, max_age_s)

    def last_event_age(self) -> float:
        return self._book.last_event_age()

    async def subscribe(self, symbols: Iterable[str]) -> None:
        self._subscribed.update(symbols)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
