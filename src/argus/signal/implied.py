"""Delta implied by a leg's own mid — the short strike's selection input.

→ docs/decisions/2026-09-21-short-strike-selection-on-mid-implied-delta.md

WHY THIS EXISTS. The short strike used to be chosen by the FEED delta while
`min_credit` tested a credit computed from the leg MIDS. Two sources, and the
credit floor then selects on the error between them: among strikes that all read
~0.15 on the feed, the ones whose credit clears 0.50 are disproportionately the
ones the feed UNDER-states — really closer to the money. On 2026-09-21 the five live
entries were ~0.19-delta calls, not 0.15. Inverting each leg's own mid puts
selection and credit on ONE source, so there is no estimation error left for the
floor to select on: a quote that ticks up raises the credit and the implied delta
together, which moves selection OUTWARD — the safe direction.

PURE AND STDLIB-ONLY: `math.erfc`, no I/O, no clock, no state. The same inputs give
the same answer on every call, which is what lets the recorded-quotes replay
(`tools.replay_selection`) re-run the runtime's arithmetic verbatim.

CONVENTIONS ARE IMPORTED FROM `regime.py`, NEVER RESTATED. Time basis, minimum
remaining life, the put-call-parity forward and the discount all come from the
module that already estimates the same market's vol from the same strip, so the two
estimates cannot drift apart. `tests/test_implied.py` pins that no constant here
shadows one of them.

THE MODEL is Black-76 on the forward — what makes a call and a put at one strike
imply one vol when priced off one forward:

    d1 = ln(F/K) / v + v/2,   v = sigma * sqrt(T),   d2 = d1 - v
    call = F N(d1) - K N(d2)          put = K N(-d2) - F N(-d1)
    delta: call N(d1), put N(d1) - 1   (forward delta; = spot delta at DISCOUNT 1)

`N` is spelled with `erfc`, not `erf`: every leg this module is asked about is out
of the money, where N(d) is small and `1 + erf(x)` would cancel away most of its
significant digits before the difference of two such terms is taken.

FAILURE IS `None`, NEVER AN EXCEPTION AND NEVER NaN. A NaN delta is the one value
`min()` cannot reject (see `OptionQuote.delta`): it would be selected, not skipped.
Every refusal also has a NAME (`Refusal`), because `no_short_leg_with_mid_delta`
records how many legs were refused and why — "the solver is failing" and "the
market is one-sided" are different mornings.

NO CACHE AND NO EARLY EXIT, deliberately. A cold solve costs a few microseconds and
a tick solves ~60 legs — about 0.2 ms against the heartbeat's 50 ms lag alarm. A
warm start or a search that stops at the first acceptable strike would add state
and failure modes to save microseconds, so every candidate leg is solved every tick.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass

from argus.signal.models import CALL, PUT, OptionQuote
from argus.signal.regime import DISCOUNT, MINUTES_PER_YEAR, MIN_MINUTES_TO_SETTLEMENT

__all__ = [
    "MidDelta",
    "Refusal",
    "implied_vol",
    "selection_delta",
    "solve_leg",
    "years_to_settlement",
]

# The solver's vol bracket, annualized. 0.5% is below any SPX 0DTE print on record
# and 500% is far above one; a mid that needs a vol outside it is not a market this
# selector should be choosing a strike from, and is refused as unconverged.
SIGMA_LO = 0.005
SIGMA_HI = 5.0
# Converged when the model price is within this many POINTS of the mid. A tick is
# 0.05, so this is six orders of magnitude inside anything a quote can express.
PRICE_TOL = 1e-7
MAX_ITERATIONS = 40
# Below this, a Newton step divides by (nearly) nothing; bisect instead.
MIN_VEGA = 1e-12

_SQRT2 = math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


class Refusal(enum.Enum):
    """Why a leg has no mid-implied delta. Values are the audit-log spelling."""

    #: `bid <= 0`, `ask < bid`, or a non-finite side. There is no two-sided mid to
    #: invert, and the zero-bid floor that prices the CREDIT is not a market.
    ONE_SIDED = "one_sided"
    #: The mid is at or outside the no-arbitrage bounds (at/below intrinsic, at/above
    #: the forward for a call or the strike for a put), or an input is not a usable
    #: number. No vol can produce this price.
    OUT_OF_BOUNDS = "out_of_bounds"
    #: Inside the bounds but no vol in `[SIGMA_LO, SIGMA_HI]` reproduced it within
    #: `MAX_ITERATIONS`.
    NO_CONVERGENCE = "no_convergence"


@dataclass(frozen=True, slots=True)
class MidDelta:
    """A leg's delta and vol as implied by its own raw mid. Both always finite."""

    delta: float    # signed: calls positive, puts negative (as `OptionQuote.delta`)
    iv: float       # annualized, calendar-time basis (`regime.MINUTES_PER_YEAR`)


def years_to_settlement(minutes: float) -> float | None:
    """`minutes_to_settlement` on `regime`'s annualization basis, or None when the
    remaining life is below `regime.MIN_MINUTES_TO_SETTLEMENT` (or not a number).

    The same floor the regime estimator refuses under, for the same reason: in the
    last minutes a PM-settled strip is nearly all intrinsic, and a one-tick error
    becomes an arbitrarily large vol. NaN — a hand-built `RegimeSnapshot` that
    never asserted a time — fails the `>=` and is refused, never defaulted.
    """
    if not minutes >= MIN_MINUTES_TO_SETTLEMENT or not math.isfinite(minutes):
        return None
    return minutes / MINUTES_PER_YEAR


def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT2)


def _price_and_vega(
    sigma: float, forward: float, strike: float, sqrt_t: float, log_fk: float, is_call: bool
) -> tuple[float, float, float]:
    """Black-76 `(price, vega, d1)` at `sigma`, undiscounted (`regime.DISCOUNT`)."""
    v = sigma * sqrt_t
    d1 = log_fk / v + 0.5 * v
    d2 = d1 - v
    if is_call:
        price = forward * _ncdf(d1) - strike * _ncdf(d2)
    else:
        price = strike * _ncdf(-d2) - forward * _ncdf(-d1)
    vega = forward * math.exp(-0.5 * d1 * d1) * _INV_SQRT_2PI * sqrt_t
    return DISCOUNT * price, DISCOUNT * vega, d1


def _solve(
    price: float, forward: float, strike: float, t_years: float, right: str
) -> tuple[float, float] | Refusal:
    """`(sigma, d1)` reproducing `price`, or the reason there is none."""
    if right not in (CALL, PUT):
        return Refusal.OUT_OF_BOUNDS
    if not (
        math.isfinite(price) and math.isfinite(forward) and math.isfinite(strike)
        and math.isfinite(t_years)
        and forward > 0.0 and strike > 0.0 and t_years > 0.0
    ):
        return Refusal.OUT_OF_BOUNDS
    is_call = right == CALL
    intrinsic = max(forward - strike, 0.0) if is_call else max(strike - forward, 0.0)
    upper = forward if is_call else strike
    if not intrinsic < price < upper:
        return Refusal.OUT_OF_BOUNDS

    sqrt_t = math.sqrt(t_years)
    log_fk = math.log(forward / strike)
    lo, hi = SIGMA_LO, SIGMA_HI
    # Manaster-Koehler start: the vol at which vega peaks, so Newton moves
    # monotonically toward the root from either side instead of overshooting off a
    # flat wing. Clamped into the bracket; at the money it is 0 and the clamp wins.
    sigma = min(max(math.sqrt(2.0 * abs(log_fk) / t_years), 0.2), hi)
    for _ in range(MAX_ITERATIONS):
        model, vega, d1 = _price_and_vega(sigma, forward, strike, sqrt_t, log_fk, is_call)
        error = model - price
        if abs(error) < PRICE_TOL:
            return sigma, d1
        # Price is increasing in sigma, so the sign of the error says which half of
        # the bracket the root is in. Narrowing on EVERY iteration is what makes
        # the bisection fallback a guarantee rather than a hope.
        if error > 0.0:
            hi = sigma
        else:
            lo = sigma
        step = sigma - error / vega if vega > MIN_VEGA else math.nan
        sigma = step if lo < step < hi else 0.5 * (lo + hi)
    return Refusal.NO_CONVERGENCE


def implied_vol(
    price: float, forward: float, strike: float, t_years: float, right: str
) -> float | None:
    """Black-76 implied vol of `price`, or None. Never raises, never NaN/inf."""
    solved = _solve(price, forward, strike, t_years, right)
    return None if isinstance(solved, Refusal) else solved[0]


def solve_leg(quote: OptionQuote, forward: float, t_years: float) -> MidDelta | Refusal:
    """`quote`'s mid-implied delta, or the named reason it has none.

    The price inverted is the RAW mid `(bid + ask) / 2`, never `floored_mid`: the
    floor fires only at `bid == 0`, which is refused here as one-sided, so on every
    leg this answers for the two are equal — and on the ones they differ, the floor
    is a pricing convention for a CREDIT, not a market to back a delta out of.
    """
    bid, ask = quote.bid, quote.ask
    if not (math.isfinite(bid) and math.isfinite(ask) and bid > 0.0 and ask >= bid):
        return Refusal.ONE_SIDED
    solved = _solve(0.5 * (bid + ask), forward, quote.strike, t_years, quote.right)
    if isinstance(solved, Refusal):
        return solved
    sigma, d1 = solved
    n_d1 = _ncdf(d1)
    return MidDelta(delta=n_d1 if quote.right == CALL else n_d1 - 1.0, iv=sigma)


def selection_delta(quote: OptionQuote, forward: float, t_years: float) -> MidDelta | None:
    """`solve_leg` without the reason — None whenever there is no delta."""
    solved = solve_leg(quote, forward, t_years)
    return None if isinstance(solved, Refusal) else solved
