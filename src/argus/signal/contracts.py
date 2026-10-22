"""Option-chain metadata: SDK mapping and selection, all pure.

An `OptionContract` pairs the broker's OCC `symbol` (what an order references)
with the `streamer_symbol` (what DXLink subscribes to) — both are needed, and
conflating them is an easy way to end up subscribed to nothing.

Selection is deliberately pure so the 0DTE-expiry pick and the strike-window
filter are testable without a broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from argus.signal.models import CALL, PUT


@dataclass(frozen=True)
class OptionContract:
    symbol: str            # OCC symbol — what an order references
    streamer_symbol: str   # dxfeed symbol — what DXLink subscribes to
    strike: float
    right: str             # "put" | "call"
    expiration: date


def map_option(option: Any) -> OptionContract | None:
    """Map a tastytrade `Option` to a domain contract; None if unusable.

    An option without a streamer symbol can't be quoted, so it is dropped rather
    than silently producing a contract we'd never receive data for.
    """
    streamer_symbol = getattr(option, "streamer_symbol", None)
    if not streamer_symbol:
        return None
    raw_type = getattr(option, "option_type", None)
    value = getattr(raw_type, "value", raw_type)
    if value == "P":
        right = PUT
    elif value == "C":
        right = CALL
    else:
        return None
    return OptionContract(
        symbol=option.symbol,
        streamer_symbol=streamer_symbol,
        strike=float(option.strike_price),
        right=right,
        expiration=option.expiration_date,
    )


def select_expiration(
    chain_by_date: dict[date, list[Any]], target: date
) -> list[Any]:
    """Options expiring on `target` (for 0DTE, today). Empty if none — e.g. a
    non-trading day or a day with no expiry, which must NOT be treated as an
    error the caller silently ignores."""
    return list(chain_by_date.get(target, []))


def filter_strike_window(
    contracts: list[OptionContract], spot: float, window_points: float
) -> list[OptionContract]:
    """Keep strikes within +/- window_points of spot.

    SPX's 0DTE chain runs to thousands of strikes; subscribing to all of them
    wastes feed bandwidth and slows every snapshot join. The credit-spread
    strategy only ever looks near the money, so bound the subscription (spec §5 —
    this feed is one underlying at high frequency, not many symbols slowly).
    """
    return [c for c in contracts if abs(c.strike - spot) <= window_points]
