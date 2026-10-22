"""Entry-time regime estimation from the 0DTE chain — TELEMETRY, not a gate.

`compute_regime(chain, ...)` turns a `ChainSnapshot` into a `RegimeSnapshot`:
an ATM implied-vol level and an implied-skew reading, both estimated from the
same OTM strip the signal engine already has subscribed. Pure and deterministic,
the same contract as `strategy.evaluate` — no I/O, no clock read, no network, so
it is fully testable against a constructed `ChainSnapshot`.

WHAT THIS IS FOR RIGHT NOW. Nothing acts on these numbers. `[regime]` is absent
from `argus.toml`, so `CreditSpreadStrategy`'s side gate and size scaler are
no-ops and the readings only land in the audit log. That is the entire point:
the thresholds in `RegimeConfig` cannot be chosen until there is a distribution
to choose them from, and nothing in Argus has ever recorded one.

WHY IT IS COMPUTED FROM PRICES AND NOT FROM AN IV FIELD. `OptionQuote` carries
`delta`, `bid` and `ask` — the DXLink Greeks join in `live_chain.py` maps delta
and nothing else, so there is no per-contract IV to average. That turns out to be
the right shape anyway: the VIX-style estimator is defined on option PRICES
directly (a strike-weighted integral of OTM mids), not on an average of per-strike
IVs, so no extra feed subscription, no models.py change, and no Black-Scholes
inversion is needed. `models.py` and `live_chain.py` are untouched by this module.

THE ESTIMATOR is CBOE's VIX integrated-variance formula evaluated on the 0DTE
strip rather than the 30-day interpolation:

    sigma^2 = (2/T) * SUM_i [ dK_i / K_i^2 * Q(K_i) ]  -  (1/T) * (F/K_0 - 1)^2

with `Q(K)` the OTM mid at strike K, `F` the implied forward, and `K_0` the
largest strike at or below `F`. `atm_iv` is `sqrt(sigma^2)`, annualized.

`skew` splits that same sum at `K_0` — call-side (up) variance minus put-side
(down) variance. Negative is the usual equity shape (puts pricing more stress
than calls); positive means the reverse. It is a VARIANCE DIFFERENCE, not a
normalized skew coefficient, so it carries units and is not comparable across
underlyings — fine here, where the only consumer compares SPX to SPX.

FAILURE IS A NaN READING, NEVER AN EXCEPTION AND NEVER `None`. A thin strip, a
missing forward, or an expiry that has already passed yields
`RegimeSnapshot(nan, nan, today)` — a snapshot that still carries `today`. That
is load-bearing, not tidiness: `CreditSpreadStrategy` reads the event-blackout
date off this object, so an estimator that could return `None` on a thin chain
would silently disarm the FOMC blackout on exactly the kind of disordered day it
exists for. The estimate is allowed to fail; the date is not.

THE READING IS BIASED, KNOWABLY, AND THAT IS WHY `minutes_to_settlement` RIDES
ALONG. The VIX integral assumes a continuum of strikes; SPX quotes a 5-point
ladder. The resulting discretization error is small when the distribution is wide
relative to that spacing and grows sharply as the remaining life shrinks.
Measured against synthetic Black-Scholes strips at a constant true vol of 0.20
(5-point strikes, +/-250-point window):

    09:45 -> 1.03x     13:00 -> 1.06x     15:00 -> 1.20x
    10:00 -> 1.03x     14:00 -> 1.10x     15:30 -> 1.38x
    12:00 -> 1.05x                        15:45 -> 1.68x

Two consequences, both for whoever eventually calibrates `[regime]`:

1. **Do not calibrate against published VIX values.** This estimator and the
   30-day index do not measure the same thing and do not agree in level. Fit the
   thresholds to THIS estimator's own recorded distribution.
2. **Do not pool raw readings across the session.** Condition on
   `minutes_to_settlement`, or compare like-for-like times of day. Pooling makes
   a threshold that mostly detects the clock.

Wide markets push the same direction (a +/-1.00 bid/ask reads 1.28x at 10:00
where a +/-0.05 reads 1.03x), which is worth knowing because 0DTE spreads widen
into the close at the same time the discretization bias bites.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time

from argus.signal.models import CALL, PUT, ChainSnapshot, OptionQuote, RegimeSnapshot

# Calendar-year annualization, matching CBOE's own convention (VIX divides by
# minutes-per-year). Trading-day annualization would inflate a 0DTE reading by
# ~sqrt(365/252) against every published SPX vol number, for no gain.
MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0

# Below this the (2/T) factor amplifies a one-tick pricing error into an
# arbitrarily large vol. At 0DTE that is not a hypothetical: the last minutes
# before a PM settlement price a strip that is nearly all intrinsic. 10 minutes
# of remaining life is where the reading stops meaning anything, so it is reported
# as unestimable rather than as a number nobody should use.
#
# This bounds the ARITHMETIC, not the bias. The measured 1.2-1.7x inflation over
# the last hour (module docstring) is well inside this guard and is deliberately
# still reported: it is smooth, monotone in the remaining time, and recorded
# alongside it, so it is correctable at analysis time. Discarding those readings
# would throw away the end-of-day sample entirely to avoid a bias that is already
# labelled.
MIN_MINUTES_TO_SETTLEMENT = 10.0

# The risk-free discount over a few remaining hours is ~1e-5 on a 5% rate and is
# swamped by the bid/ask on every leg in the strip. Carrying a rate would mean
# sourcing and configuring one to move the estimate by less than a tick.
DISCOUNT = 1.0


def compute_regime(
    chain: ChainSnapshot,
    *,
    now_et: time,
    today: date,
    close_et: time,
) -> RegimeSnapshot:
    """Estimate `(atm_iv, skew)` for `chain`, stamped with `today`.

    `now_et`/`close_et` are injected rather than read from a clock, keeping this
    pure — `close_et` comes from `MarketCalendar.close_time(today)`, so a half-day
    shortens `T` correctly instead of pretending the strip has until 16:00.

    Never raises, never returns None. See the module docstring.
    """
    minutes = _minutes_to_settlement(now_et, close_et)
    # Reported on EVERY path, including the unestimable ones — "no reading, and
    # here is how long was left when that happened" is itself the diagnostic for
    # whether the estimator is failing at the open, at the close, or at random.
    unestimable = RegimeSnapshot(
        atm_iv=math.nan, skew=math.nan, today=today, minutes_to_settlement=minutes
    )
    if minutes < MIN_MINUTES_TO_SETTLEMENT:
        return unestimable
    t_years = minutes / MINUTES_PER_YEAR

    puts = _usable(chain, PUT)
    calls = _usable(chain, CALL)
    forward = implied_forward(puts, calls, t_years)
    if forward is None:
        return unestimable

    k0 = _atm_strike(puts, calls, forward)
    if k0 is None:
        return unestimable

    # The OTM strip: puts strictly below K0, calls strictly above. At K0 itself
    # VIX uses the AVERAGE of the call and put mid, which is what keeps the two
    # wings from double-counting the ATM strike.
    down_strip = [(o.strike, o.floored_mid) for o in puts if o.strike < k0]
    up_strip = [(o.strike, o.floored_mid) for o in calls if o.strike > k0]
    atm_mid = _atm_contribution(puts, calls, k0)
    if atm_mid is not None:
        # Split the ATM contribution evenly so `up + down` still reconstructs the
        # full strip while neither wing is credited with all of it.
        down_strip.append((k0, atm_mid / 2.0))
        up_strip.append((k0, atm_mid / 2.0))

    down_var = strip_variance(down_strip, t_years)
    up_var = strip_variance(up_strip, t_years)
    if down_var is None or up_var is None:
        return unestimable

    # VIX's forward-offset correction, applied once to the combined strip.
    correction = (forward / k0 - 1.0) ** 2 / t_years
    variance = down_var + up_var - correction

    # A negative total variance means the strip and the forward disagree beyond
    # what the correction can absorb (a crossed or stale quote, typically). There
    # is no square root to take and no defensible number to report.
    atm_iv = math.sqrt(variance) if variance > 0.0 else math.nan
    skew = up_var - down_var
    return RegimeSnapshot(
        atm_iv=atm_iv if math.isfinite(atm_iv) else math.nan,
        skew=skew if math.isfinite(skew) else math.nan,
        today=today,
        minutes_to_settlement=minutes,
    )


def implied_forward(
    puts: list[OptionQuote], calls: list[OptionQuote], t_years: float
) -> float | None:
    """VIX's forward: `F = K* + e^(rT) * (C(K*) - P(K*))`, where `K*` is the strike
    with the smallest |call mid - put mid|.

    Deriving the forward from put-call parity rather than using spot is what makes
    the estimate consistent with the strip it is taken over: the options price the
    forward, and on an index with dividends and a nonzero rate that is not spot.
    Requires at least one strike quoting BOTH a call and a put; returns None
    otherwise rather than falling back to spot, since a silent substitution here
    would bias every reading in a way nothing downstream could detect.
    """
    put_by_strike = {o.strike: o for o in puts}
    pairs = [
        (o.strike, put_by_strike[o.strike].floored_mid, o.floored_mid)
        for o in calls
        if o.strike in put_by_strike
    ]
    if not pairs:
        return None
    strike, put_mid, call_mid = min(pairs, key=lambda p: abs(p[2] - p[1]))
    forward = strike + DISCOUNT * (call_mid - put_mid)
    return forward if math.isfinite(forward) and forward > 0.0 else None


def strip_variance(strip: list[tuple[float, float]], t_years: float) -> float | None:
    """`(2/T) * SUM dK_i / K_i^2 * Q(K_i)` over one wing.

    `dK_i` is the half-distance to the neighbouring strikes, and the outermost
    strike on each end uses its single neighbour's spacing — the standard VIX
    treatment, and the reason a wing needs at least two strikes to contribute at
    all. SPX ladders are not uniformly spaced (5-point near the money, 25-point in
    the wings), so `dK` is measured per strike rather than assumed.

    Returns None when the wing has fewer than two strikes: there is no spacing to
    measure, and defaulting one would invent the weight that dominates the sum.
    """
    if len(strip) < 2:
        return None
    ordered = sorted(strip)
    strikes = [k for k, _ in ordered]
    total = 0.0
    for i, (strike, mid) in enumerate(ordered):
        if strike <= 0.0 or not math.isfinite(mid) or mid < 0.0:
            return None
        if i == 0:
            d_k = strikes[1] - strikes[0]
        elif i == len(ordered) - 1:
            d_k = strikes[-1] - strikes[-2]
        else:
            d_k = (strikes[i + 1] - strikes[i - 1]) / 2.0
        total += (d_k / (strike * strike)) * mid
    variance = 2.0 * DISCOUNT * total / t_years
    return variance if math.isfinite(variance) else None


def _usable(chain: ChainSnapshot, right: str) -> list[OptionQuote]:
    """Tradeable legs only, sorted by strike.

    `tradeable` (ask > 0) is the same filter the strategy applies, so the regime
    is read off the same set of contracts the strategy could actually select from
    — a reading taken over a wider strip than the one being traded would be
    measuring a different chain than the one the decision is about.
    """
    return sorted(
        (o for o in chain.by_right(right) if o.tradeable), key=lambda o: o.strike
    )


def _atm_strike(
    puts: list[OptionQuote], calls: list[OptionQuote], forward: float
) -> float | None:
    """`K_0`: the largest strike at or below the forward that the strip actually
    quotes. Falls back to the smallest available strike when the forward sits
    below the whole ladder (a strike window that has drifted off spot)."""
    strikes = sorted({o.strike for o in puts} | {o.strike for o in calls})
    if not strikes:
        return None
    at_or_below = [k for k in strikes if k <= forward]
    return at_or_below[-1] if at_or_below else strikes[0]


def _atm_contribution(
    puts: list[OptionQuote], calls: list[OptionQuote], k0: float
) -> float | None:
    """The average of the call and put mid at `K_0`, or whichever single side is
    quoted there. None when `K_0` is quoted by neither."""
    mids = [o.floored_mid for o in puts + calls if o.strike == k0]
    usable = [m for m in mids if math.isfinite(m) and m >= 0.0]
    if not usable:
        return None
    return sum(usable) / len(usable)


def _minutes_to_settlement(now_et: time, close_et: time) -> float:
    """Minutes from `now_et` to `close_et`, same ET day. Negative past the close.

    SPXW is PM-settled at the close, so the close IS the expiry — the same fact
    `shadow/outcome.py` settles against. An arbitrary date is used for the
    subtraction because both operands are same-day wall-clock times.
    """
    anchor = date(2000, 1, 1)
    delta = datetime.combine(anchor, close_et) - datetime.combine(anchor, now_et)
    return delta.total_seconds() / 60.0
