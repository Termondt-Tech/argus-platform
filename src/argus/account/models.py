"""Domain account types + SDK mapping, decoupled from tastytrade.

Positions are keyed by their broker `symbol` (OCC option symbol) and carry a
**signed** contract quantity (+long / -short) so reconciliation is a plain
integer comparison. Mapping functions are pure so they can be unit-tested against
real SDK objects without a broker.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Position:
    symbol: str
    underlying: str
    instrument_type: str
    signed_quantity: int          # + long, - short, 0 flat
    average_open_price: float
    mark_price: float | None


@dataclass(frozen=True)
class Balances:
    net_liquidating_value: float


@dataclass(frozen=True)
class AccountSnapshot:
    positions: tuple[Position, ...]
    net_liquidating_value: float | None
    captured_at: float

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self.positions if p.signed_quantity != 0)

    def position_map(self) -> dict[str, int]:
        """symbol -> signed quantity, excluding flats. The reconciliation input."""
        return {p.symbol: p.signed_quantity for p in self.positions if p.signed_quantity != 0}


def signed_quantity(quantity: Decimal | float | int, direction: str | None) -> int:
    """Combine a positive magnitude and a direction string into a signed int."""
    q = int(quantity)
    d = (direction or "").strip().lower()
    if d == "short":
        return -abs(q)
    if d == "zero":
        return 0
    # Default (incl. "long") is a long position.
    return abs(q)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def map_position(cp: Any) -> Position:
    """Map a tastytrade `CurrentPosition` (REST or stream) to a domain `Position`."""
    open_price = _to_float(getattr(cp, "average_open_price", None)) or 0.0
    return Position(
        symbol=cp.symbol,
        underlying=getattr(cp, "underlying_symbol", "") or "",
        instrument_type=str(getattr(cp, "instrument_type", "") or ""),
        signed_quantity=signed_quantity(cp.quantity, getattr(cp, "quantity_direction", None)),
        average_open_price=open_price,
        mark_price=_to_float(getattr(cp, "mark_price", None)),
    )


def map_balances(bal: Any) -> Balances:
    """Map a tastytrade `AccountBalance` to domain `Balances`."""
    nlv = _to_float(getattr(bal, "net_liquidating_value", None))
    return Balances(net_liquidating_value=nlv if nlv is not None else 0.0)
