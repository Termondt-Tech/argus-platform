"""The zero-bid floor and tick rounding — ONE definition, shared by every caller.

Spec §7 hard rule: before computing an implied mid, floor any leg with `bid == 0`
to the SPX minimum tick ($0.05). An unfloored zero-bid leg produces a
mathematically unfillable mid — you compute a credit you can never actually
collect, size against it, and route a limit that sits forever.

This lives in its own module rather than as a private detail of `SpreadSignal`
because different callers need it for different questions:

- **The signal layer** (`OptionQuote.floored_mid`) asks *is this candidate worth
  proposing?* — one floored mid per evaluation, at candidate time.
- **`build.py`** asks *is this candidate's credit really its legs' floored mid?* —
  recomputing it to refuse a limit price that came from anywhere else.
- **The position watchdog** (chunk 4) asks *what would it cost to close this
  now?* — reapplying the floor to FRESH quotes, long after the candidate was
  gated.

Those are different needs with the same arithmetic. Two implementations of the
same safety rule is exactly how one of them quietly drifts, so there is one.

**The price-walking loop (chunk 3) is deliberately NOT in that list.** An earlier
draft of this docstring named it as the fresh-quote caller; it is not, and the
distinction is load-bearing. `loop.py` imports `TICK` and `round_to_tick` only —
the pure arithmetic — and never `floor_bid`/`floored_mid`/`spread_mid`. It walks a
deterministic tick ladder down from the gate-approved mid rather than re-quoting,
so the floor is applied exactly once, upstream, by the signal layer.

If fresh-quote repricing is ever wanted there, note what it costs: an order's legs
carry **OCC** symbols, while `MarketDataPort` is keyed by **`streamer_symbol`** —
different strings, and `market.latest(order_leg.symbol)` returns `None` forever
without failing. Go through `execution.watchdog.LegQuotes`, which is the one
OCC -> streamer resolution step and reports why it could not price a leg. Do not
write a second resolver, for the same reason there is only one copy of this floor.

**Money arithmetic returns `Decimal`.** `round_to_tick` is the boundary between
the float economics the signal layer works in and the exact, tick-aligned price
that goes on the wire (tastytrade's `price` field is a `Decimal`). Rounding to
the nearest tick with a binary float would put a limit like `1.2000000000000002`
in the payload.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

from argus.marketdata.models import SPX_MIN_TICK

# Exact decimal form of the tick. Decimal(str(0.05)) == Decimal("0.05"), whereas
# Decimal(0.05) would carry the float's binary error into every rounded price.
TICK = Decimal(str(SPX_MIN_TICK))


def floor_bid(bid: float) -> float:
    """A leg's bid for pricing purposes: a zero (or absent) bid becomes one tick.

    A genuine 0.0 bid on a deep-OTM leg is real market data, not an error — which
    is why the floor belongs here in pricing rather than in the market-data layer
    (see `marketdata.models.SPX_MIN_TICK`).
    """
    return bid if bid > 0.0 else SPX_MIN_TICK


def floored_mid(bid: float, ask: float) -> float:
    """One leg's mid, with the zero-bid floor applied first."""
    return (floor_bid(bid) + ask) / 2.0


def spread_mid(
    short_bid: float, short_ask: float, long_bid: float, long_ask: float
) -> float:
    """A vertical's net mid in points: short leg's floored mid minus the long's.

    Sign convention is the vertical's, not the order's: this is positive for a
    credit spread with any value left in it. `build.py` decides whether that
    number is money coming in (opening) or going out (closing).
    """
    return floored_mid(short_bid, short_ask) - floored_mid(long_bid, long_ask)


# Precision the incoming float is normalized to before it is snapped to a tick.
# A net mid accumulated in binary floats lands at e.g. 1.1249999999999998 when the
# arithmetic value is exactly 1.125 — which would round DOWN a tie that should
# round up, making the limit price depend on the order the mid was summed in
# rather than on the market. Six decimals is far finer than any real price
# increment (a tick is 0.05) and far coarser than the noise.
_INPUT_PRECISION = Decimal("0.000001")

#: Below this, two prices are the SAME price. Same quantity and same argument as
#: `_INPUT_PRECISION`, derived from it so the two can never drift apart.
#:
#: The unit is points, and the sizing is the price grid rather than anything read
#: off `sys.float_info`. SPX options quote on a 0.05 tick; a leg mid is
#: `(bid + ask) / 2`, so it lands on a 0.025 grid, and `spread_mid` differences two
#: of those. The smallest difference between a net mid and anything else that can
#: EXIST is therefore 0.025 points ($2.50 a contract) — 25,000x this, so nothing
#: representable as a price can hide inside it. The largest error the arithmetic
#: produces is a few ULPs of a number around 1, ten orders of magnitude below it.
#:
#: It exists because a THRESHOLD comparison on a net mid has no meaningful answer
#: at its own boundary: `2.0 x 0.8` is exactly 1.6, which is exactly an achievable
#: `spread_mid`, and the same market state summed from different quotes gives
#: 1.5999999999999996 or 1.6000000000000005. On 2026-09-01 the second of those
#: closed a position for -160.00 on a 4.4e-16 breach.
#: → docs/decisions/2026-09-02-the-credit-stop-basis-and-its-comparison.md
PRICE_EPSILON = float(_INPUT_PRECISION)


def round_to_tick(price: float | Decimal) -> Decimal:
    """Snap a price to the nearest whole tick, exactly.

    Ties round away from zero, symmetrically for credits and debits. Direction is
    deliberately NOT decided here: rounding *toward* the market is a price-walking
    policy (chunk 3), and baking a favourable-direction bias into the shared
    primitive would silently apply it to the first submission too.
    """
    d = price if isinstance(price, Decimal) else Decimal(str(price))
    if not d.is_finite():
        raise ValueError(f"cannot round a non-finite price to a tick: {price!r}")
    d = d.quantize(_INPUT_PRECISION, rounding=ROUND_HALF_UP)
    return (d / TICK).quantize(Decimal(1), rounding=ROUND_HALF_UP) * TICK


def is_finite(value: float) -> bool:
    """Guard for prices arriving from a feed. NaN/inf must never reach a limit."""
    return isinstance(value, (int, float)) and math.isfinite(value)
