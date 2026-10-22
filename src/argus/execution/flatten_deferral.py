"""Whether a far-OTM flatten may be DEFERRED — the arithmetic, and nothing else.

PURE. No I/O, no clock read, no config read, no quote read. The watchdog gathers the
inputs and owns every side effect; this module turns them into one
`DeferralDecision`. Same contract as `signal/regime.py` and `signal/realized.py`,
and for the same reason: the decision is the part worth testing exhaustively, and it
needs no watchdog to test.

WHAT IT DECIDES. The 15:30 `FLATTEN_BEFORE_CLOSE` is INSURANCE — against the price
triggers and the broker failing together inside the last half hour. On a spread far
out of the money that insurance is overpriced: on 2026-09-24 it walked a 0.05 → 0.20
ladder and left an unfillable 0.10 cross working for 22 minutes on a spread that was
80 points out of the money and expired worthless. This module answers one question:

    is the insurance at least `k` times overpriced?

    k · P_touch(z) · W  ≤  premium

- `z` is the log distance from spot to the UNDERLYING-TRIGGER BARRIER (the short
  strike ± `underlying_buffer_points` — the level at which the watchdog's own
  backstop fires and an emergency exit into a stressed book begins), in units of the
  expected move over the time left: `max(implied, realized) · √τ`. Scale-free in the
  index level, the volatility regime and the time of day — which a distance in points
  or percent is not (09-17 and 09-23 were both 0.70% out of the money and 5.16σ
  against 4.55σ).
- `P_touch(z) = 0.01 · (z / z99) ^ (−alpha)` is a Pareto tail anchored at the 99th
  percentile of the standardized excursion, calibrated by `tools.flatten_calibration`
  on Argus's own recorded SPX path. A Gaussian is 35× too thin at 4σ on that path
  and is deliberately not used.
- `W` is the spread's own width — the most a touch can cost, a conservative bound.
- `premium` is ONE TICK, the least any close can cost, BEFORE fees: adding fees or
  the natural would only make deferral easier, so leaving them out is the
  conservative side.
- `k` is the one policy choice (`watchdog.flatten_defer_insurance_multiple`).

**It can only ever say "defer" or "do not"; it never says "flatten harder".** Every
missing, stale or unusable input is a refusal, and a refusal means the flatten runs
exactly as it did before this module existed.

→ docs/designs/2026-09-25-deferring-a-far-otm-flatten.md §4
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from argus.execution.zerobid import TICK
from argus.signal.models import payload_number
# THE annualization basis, imported and never redefined — `realized.py`'s rule. The
# calibration standardized every window with `regime.py`'s `atm_iv` and
# `realized.py`'s RV on this basis, so a second copy here would silently make the
# runtime z a different quantity from the one the tail was fitted on.
from argus.signal.regime import MINUTES_PER_YEAR


class DeferralMode(StrEnum):
    """`watchdog.flatten_deferral`. Validated by `config.py` against these values."""

    #: Nothing is computed; the flatten is exactly the pre-2026-09-25 behaviour.
    OFF = "off"
    #: The decision is computed and RECORDED with `acted: false`; the flatten proceeds
    #: exactly as `off`. Not the shipped value — kept because it costs nothing.
    RECORD = "record"
    #: A decision to defer removes `FLATTEN_BEFORE_CLOSE` from this tick's fired set.
    ON = "on"


#: The anchor of the Pareto tail: `z99` is where the calibrated exceedance is 1%.
_ANCHOR_P = 0.01

# Refusal tags. Plain strings so every record and page carries the same words; a
# decision that defers carries `DEFERRED`.
DEFERRED = "deferred"
NO_TIME = "time_to_close_unknown"
NO_SPOT = "no_fresh_spot"
NO_STRIKE = "short_strike_unknown"
NO_WIDTH = "width_unknown"
NO_IMPLIED = "no_implied_vol_since_mark"
NO_REALIZED = "no_realized_vol"
VOL_UNUSABLE = "vol_unusable"
AT_BARRIER = "at_or_inside_the_barrier"
INSIDE_BODY = "inside_the_body"
OVER_BUDGET = "insurance_not_overpriced"


@dataclass(frozen=True)
class DeferralParams:
    """The three numbers the decision needs from config, plus the barrier's buffer.

    Built by the watchdog from `config.watchdog` and nothing else — this module never
    reads config, and carries no default for any of them (an AST test pins that).
    """

    #: `k` — decline the insurance only when it is at least this many times
    #: overpriced. Policy.
    insurance_multiple: float
    #: `z99` — the calibrated 99th percentile of the standardized excursion.
    tail_z99: float
    #: `alpha` — the calibrated tail index, capped at 3 by the loader.
    tail_alpha: float
    #: `watchdog.underlying_buffer_points` — where the barrier sits.
    buffer_points: float


@dataclass(frozen=True)
class DeferralInputs:
    """Everything the watchdog read for one position on one tick.

    `ineligible` carries the watchdog's own refusal (a non-clock trigger fired, an
    order may be working, the broker poll is stale, ...) — conditions that are about
    SYSTEM STATE rather than arithmetic, and so are decided there. When it is set the
    arithmetic is not attempted and the decision refuses with that reason.
    """

    side: str | None
    spot: float | None
    short_strike: float | None
    width: float | None
    sigma_implied: float | None
    sigma_realized: float | None
    minutes_to_close: float
    ineligible: str | None = None


@dataclass(frozen=True)
class DeferralDecision:
    """One decision, and every number behind it — for the record, the page and `/status`.

    `defer` is True only when every input was usable and the insurance test passed.
    Every other field is `None` where the arithmetic stopped before reaching it, never
    a placeholder number.
    """

    defer: bool
    #: `DEFERRED`, or the tag of the first refusal.
    reason: str
    k: float
    tail_z99: float
    tail_alpha: float
    premium: float
    minutes_to_close: float
    side: str | None = None
    spot: float | None = None
    short_strike: float | None = None
    barrier: float | None = None
    distance_points: float | None = None
    width: float | None = None
    sigma_implied: float | None = None
    sigma_realized: float | None = None
    sigma: float | None = None
    z: float | None = None
    p_touch: float | None = None
    #: `k · P_touch · W` — the left-hand side of the test, in points.
    lhs: float | None = None

    def to_payload(self) -> dict[str, Any]:
        """Every number through `payload_number`: a non-finite field on a Tier 1 row
        is a REFUSED record (2026-09-15), and `z` is legitimately `inf` at the close."""
        return {
            "defer": self.defer,
            "reason": self.reason,
            "k": payload_number(self.k),
            "tail_z99": payload_number(self.tail_z99),
            "tail_alpha": payload_number(self.tail_alpha),
            "premium": payload_number(self.premium),
            "minutes_to_close": payload_number(self.minutes_to_close),
            "side": self.side,
            "spot": payload_number(self.spot),
            "short_strike": payload_number(self.short_strike),
            "barrier": payload_number(self.barrier),
            "distance_points": payload_number(self.distance_points),
            "width": payload_number(self.width),
            "sigma_implied": payload_number(self.sigma_implied),
            "sigma_realized": payload_number(self.sigma_realized),
            "sigma": payload_number(self.sigma),
            "z": payload_number(self.z),
            "p_touch": payload_number(self.p_touch),
            "lhs": payload_number(self.lhs),
        }


def _usable(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def barrier_for(side: str, short_strike: float, buffer_points: float) -> float:
    """The underlying trigger's level: where the watchdog's backstop fires.

    The SAME arithmetic as `TriggerThresholds.underlying_breached` (spot at or past
    this level fires it), so the event being priced is exactly the event that forces
    an emergency exit.
    """
    return short_strike + buffer_points if side == "put" else short_strike - buffer_points


def standardized_distance(
    *, side: str, spot: float, barrier: float, sigma: float, minutes_to_close: float
) -> float:
    """`ln(spot / barrier) / (sigma · √τ)` for a put, the mirror for a call.

    `+inf` once no time is left (τ ≤ 0) and the barrier is still on the safe side —
    no path can reach it — and `≤ 0` at or inside the barrier. The caller has already
    refused a non-positive distance, a non-positive sigma and a non-positive spot.
    """
    log_distance = math.log(spot / barrier) if side == "put" else math.log(barrier / spot)
    if minutes_to_close <= 0.0:
        return math.inf if log_distance > 0.0 else 0.0
    tau = minutes_to_close / MINUTES_PER_YEAR
    return log_distance / (sigma * math.sqrt(tau))


def touch_probability(z: float, *, tail_z99: float, tail_alpha: float) -> float | None:
    """`0.01 · (z / z99) ^ (−alpha)`, or None inside the body (`z < z99`).

    None rather than a number because the Pareto form is only claimed for the tail;
    below the 99th percentile the probability is at least 1%, and no budget this
    design contemplates is anywhere near that — the refusal says so directly rather
    than extrapolating a tail model into the body.

    Written as `not z >= z99` rather than `z < z99` so a NaN `z` is None — the
    refusal — and never a NaN probability that every later comparison would pass.
    """
    if not z >= tail_z99:
        return None
    if math.isinf(z):
        return 0.0
    return _ANCHOR_P * (z / tail_z99) ** (-tail_alpha)


def decide(inputs: DeferralInputs, params: DeferralParams) -> DeferralDecision:
    """The decision. Refuses on the first unusable input, in a fixed order.

    Never raises on any input it can be handed — a `None`, a NaN, a zero width, a
    negative sigma are all refusals. The watchdog still wraps the call, because the
    one direction this path must never fail in is "flatten skipped by accident".

    **Every comparison is written so that a NaN REFUSES** (`not x > 0`, never
    `x <= 0`): a comparison with NaN is False, so the natural spelling of a refusal
    test passes a NaN straight through to `defer=True`. The review of 2026-09-25 found
    exactly that for a NaN `minutes_to_close`.
    """
    premium = float(TICK)
    base = {
        "k": params.insurance_multiple,
        "tail_z99": params.tail_z99,
        "tail_alpha": params.tail_alpha,
        "premium": premium,
        "minutes_to_close": inputs.minutes_to_close,
        "side": inputs.side,
        "spot": inputs.spot,
        "short_strike": inputs.short_strike,
        "width": inputs.width,
        "sigma_implied": inputs.sigma_implied,
        "sigma_realized": inputs.sigma_realized,
    }

    def refuse(reason: str, **extra: Any) -> DeferralDecision:
        return DeferralDecision(defer=False, reason=reason, **{**base, **extra})

    if inputs.ineligible is not None:
        return refuse(inputs.ineligible)
    if not _usable(inputs.minutes_to_close):
        return refuse(NO_TIME)
    if not _usable(inputs.spot) or not inputs.spot > 0.0:
        return refuse(NO_SPOT)
    if inputs.side not in ("put", "call") or not _usable(inputs.short_strike):
        return refuse(NO_STRIKE)
    if not _usable(inputs.width) or not inputs.width > 0.0:
        return refuse(NO_WIDTH)
    if not _usable(inputs.sigma_implied):
        return refuse(NO_IMPLIED)
    if not _usable(inputs.sigma_realized):
        return refuse(NO_REALIZED)
    sigma = max(inputs.sigma_implied, inputs.sigma_realized)
    if not sigma > 0.0:
        return refuse(VOL_UNUSABLE, sigma=sigma)

    barrier = barrier_for(inputs.side, inputs.short_strike, params.buffer_points)
    distance = inputs.spot - barrier if inputs.side == "put" else barrier - inputs.spot
    if not (barrier > 0.0 and distance > 0.0):
        return refuse(AT_BARRIER, barrier=barrier, distance_points=distance, sigma=sigma)

    z = standardized_distance(
        side=inputs.side,
        spot=inputs.spot,
        barrier=barrier,
        sigma=sigma,
        minutes_to_close=inputs.minutes_to_close,
    )
    numbers = {"barrier": barrier, "distance_points": distance, "sigma": sigma, "z": z}
    p_touch = touch_probability(
        z, tail_z99=params.tail_z99, tail_alpha=params.tail_alpha
    )
    if p_touch is None:
        return refuse(INSIDE_BODY, **numbers)
    lhs = params.insurance_multiple * p_touch * inputs.width
    if not lhs <= premium:
        return refuse(OVER_BUDGET, **numbers, p_touch=p_touch, lhs=lhs)
    return DeferralDecision(
        defer=True, reason=DEFERRED, **{**base, **numbers}, p_touch=p_touch, lhs=lhs
    )


def deferral_boundary(*, k: float, width: float, tail_z99: float, tail_alpha: float) -> float:
    """The smallest z that defers, for a given width: `z99 · (0.01·k·W / TICK)^(1/α)`.

    For the page and the tests (§4.10's sensitivity table). The decision itself never
    calls this — it evaluates the inequality directly — so the two are independent
    implementations of one rule and the tests pin that they agree.
    """
    return tail_z99 * (_ANCHOR_P * k * width / float(TICK)) ** (1.0 / tail_alpha)


__all__ = [
    "DEFERRED",
    "DeferralDecision",
    "DeferralInputs",
    "DeferralMode",
    "DeferralParams",
    "barrier_for",
    "decide",
    "deferral_boundary",
    "standardized_distance",
    "touch_probability",
]
