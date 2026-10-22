"""Domain market-data types, decoupled from the tastytrade/dxfeed wire format.

Keeping our own `Quote` (rather than passing `dxfeed.Quote` around) means the
signal engine and pricing code never depend on the SDK's shape — and the
simulator can produce the same domain type without pretending to be dxfeed.

Prices are floats in this domain. `received_at` is stamped from a **monotonic**
clock at ingestion, and every staleness/age query uses that same clock, so age is
robust to wall-clock adjustments (NTP steps, DST) — which matters for a
fail-closed staleness gate that must not be fooled by a clock jump.
"""

from __future__ import annotations

from dataclasses import dataclass

# SPX minimum tick. The zero-bid floor (spec §7) is applied by the pricing layer,
# not here — market data reports raw bids, including a genuine 0.0 for a deep-OTM
# leg, and pricing decides how to treat it.
SPX_MIN_TICK = 0.05


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    received_at: float  # monotonic seconds, same clock as age queries

    @property
    def has_bid(self) -> bool:
        return self.bid > 0.0

    @property
    def two_sided(self) -> bool:
        return self.bid > 0.0 and self.ask > 0.0

    @property
    def mid(self) -> float | None:
        """Plain mid, or None if there is no offer. Callers needing the spec §7
        zero-bid floor use the signal layer's floored mid instead."""
        if self.ask <= 0.0:
            return None
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class OptionGreeks:
    """Per-contract greeks from the DXLink `Greeks` feed. `delta` drives strike
    selection; the rest are carried for the future position watchdog (gamma)."""

    symbol: str
    delta: float
    gamma: float
    theta: float
    vega: float
    implied_volatility: float
    received_at: float
