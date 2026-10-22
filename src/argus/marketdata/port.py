"""The market-data port — the seam every consumer depends on.

`DXLinkMarketData` and `FakeMarketData` both satisfy this Protocol structurally.
The staleness surface (`age`, `is_stale`, `last_event_age`) is first-class because
the fail-closed gate (spec §6) is only as good as the freshness signal it reads.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from argus.marketdata.models import OptionGreeks, Quote


@runtime_checkable
class MarketDataPort(Protocol):
    def latest(self, symbol: str) -> Quote | None:
        """Last known quote for a symbol, or None if never seen."""

    def greeks(self, symbol: str) -> OptionGreeks | None:
        """Last known greeks for an option symbol, or None if never seen."""

    def greeks_age(self, symbol: str) -> float:
        """Seconds since the last greeks update for `symbol`; +inf if never seen."""

    def age(self, symbol: str) -> float:
        """Seconds since the last quote for `symbol`; +inf if never seen."""

    def is_stale(self, symbol: str, max_age_s: float) -> bool:
        """True if `symbol` is missing or older than `max_age_s` (fail closed)."""

    def last_event_age(self) -> float:
        """Seconds since ANY quote arrived; +inf if the feed has produced nothing.

        A dead-but-connected stream shows up here as a growing age even while
        individual symbols look 'recent' from before the stall.
        """

    async def subscribe(self, symbols: Iterable[str]) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
