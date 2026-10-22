"""regime.py: the VIX-style 0DTE estimator, its guards, and its failure shape.

The estimator is validated by RECOVERY, not by golden numbers: strips are priced
with Black-Scholes at a known vol and the estimate is checked against it. A golden
number would pin whatever the code happened to produce the day it was written,
including any sign error, and would say nothing about whether the formula is right.
"""

from __future__ import annotations

import json
import math
from datetime import date, time

import pytest

from argus.signal.models import ChainSnapshot, OptionQuote
from argus.signal.regime import (
    MIN_MINUTES_TO_SETTLEMENT,
    MINUTES_PER_YEAR,
    compute_regime,
    implied_forward,
    strip_variance,
)

TODAY = date(2026, 8, 4)
CLOSE = time(16, 0)
SPOT = 5000.0


def _bs(s: float, k: float, t: float, sigma: float, is_call: bool) -> float:
    if t <= 0 or sigma <= 0:
        return max(0.0, (s - k) if is_call else (k - s))
    d1 = (math.log(s / k) + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    n = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2)))  # noqa: E731
    if is_call:
        return s * n(d1) - k * n(d2)
    return k * n(-d2) - s * n(-d1)


def _chain(
    *,
    sigma: float = 0.20,
    minutes: float = 360.0,
    spot: float = SPOT,
    span: int = 250,
    step: int = 5,
    half_spread: float = 0.05,
    put_vol_slope: float = 0.0,
) -> ChainSnapshot:
    """A Black-Scholes strip. `put_vol_slope` adds vol per 100 points BELOW spot,
    which is how a real equity skew is shaped."""
    t = minutes / MINUTES_PER_YEAR
    options: list[OptionQuote] = []
    for strike in range(int(spot - span), int(spot + span) + 1, step):
        vol = sigma + put_vol_slope * max(0.0, spot - strike) / 100.0
        for right, is_call in (("put", False), ("call", True)):
            px = _bs(spot, float(strike), t, vol, is_call)
            options.append(
                OptionQuote(
                    f"{right[0].upper()}{strike}",
                    float(strike),
                    right,
                    0.1,
                    max(px - half_spread, 0.0),
                    px + half_spread,
                )
            )
    return ChainSnapshot("SPXW", spot, tuple(options), 0.0)


def _at(hh: int, mm: int = 0) -> time:
    return time(hh, mm)


# --------------------------------------------------------------------------
# Recovery of a known vol
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sigma", [0.10, 0.20, 0.35, 0.60])
def test_recovers_known_vol_midsession(sigma):
    """Mid-session, the estimate is within ~15% of the true vol.

    The tolerance is loose ON PURPOSE and is not slop: the VIX integral assumes a
    continuum of strikes and SPX quotes a 5-point ladder, so a positive
    discretization bias is expected and documented. This asserts the estimator is
    measuring vol, not that it is unbiased — the bias is the subject of
    `test_bias_grows_toward_the_close`.
    """
    snap = _chain(sigma=sigma, minutes=360.0)
    got = compute_regime(snap, now_et=_at(10), today=TODAY, close_et=CLOSE)
    assert got.atm_iv == pytest.approx(sigma, rel=0.15)


def test_higher_true_vol_reads_higher():
    """Monotonicity is the property any threshold would actually rely on."""
    readings = [
        compute_regime(
            _chain(sigma=s), now_et=_at(10), today=TODAY, close_et=CLOSE
        ).atm_iv
        for s in (0.10, 0.20, 0.35, 0.60)
    ]
    assert readings == sorted(readings)


def test_bias_grows_toward_the_close():
    """The documented time-of-day bias is real and monotone.

    Pinned because it is the basis of a stated calibration constraint (do not pool
    raw intraday readings). If this stops holding, that guidance is wrong and the
    docstring in regime.py needs revisiting.
    """
    ratios = []
    for hh, mm in ((10, 0), (14, 0), (15, 30)):
        minutes = (16 - hh) * 60 - mm
        snap = _chain(sigma=0.20, minutes=minutes)
        got = compute_regime(snap, now_et=_at(hh, mm), today=TODAY, close_et=CLOSE)
        ratios.append(got.atm_iv / 0.20)
    assert ratios == sorted(ratios), ratios
    assert ratios[0] < 1.10 < ratios[-1]


def test_minutes_to_settlement_is_reported():
    got = compute_regime(_chain(), now_et=_at(10), today=TODAY, close_et=CLOSE)
    assert got.minutes_to_settlement == pytest.approx(360.0)


def test_half_day_close_shortens_the_horizon():
    """A half-day settles at 13:00, and the estimator must use that, not 16:00."""
    snap = _chain(minutes=60.0)
    early = compute_regime(snap, now_et=_at(12), today=TODAY, close_et=time(13, 0))
    assert early.minutes_to_settlement == pytest.approx(60.0)


# --------------------------------------------------------------------------
# Skew
# --------------------------------------------------------------------------


def test_skew_is_negative_when_puts_are_richer():
    """The documented sign convention: up-variance minus down-variance, so the
    usual equity shape (puts pricing more stress) reads NEGATIVE. A sign flip here
    would invert `_side_permitted`'s meaning, refusing exactly the wrong side."""
    got = compute_regime(
        _chain(put_vol_slope=0.05), now_et=_at(10), today=TODAY, close_et=CLOSE
    )
    assert got.skew < 0.0


def test_skew_becomes_more_negative_as_put_skew_steepens():
    readings = [
        compute_regime(
            _chain(put_vol_slope=slope), now_et=_at(10), today=TODAY, close_et=CLOSE
        ).skew
        for slope in (0.0, 0.02, 0.05)
    ]
    assert readings == sorted(readings, reverse=True), readings


# --------------------------------------------------------------------------
# Failure shape: NaN, never None, never an exception, and `today` always present
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, snap, now",
    [
        ("empty chain", ChainSnapshot("SPXW", SPOT, (), 0.0), _at(10)),
        ("past the close", _chain(), time(16, 30)),
        ("inside the min-time guard", _chain(), time(15, 55)),
        ("puts only", None, _at(10)),
        ("single strike", None, _at(10)),
    ],
)
def test_unestimable_inputs_yield_nan_but_keep_the_date(label, snap, now):
    """THE load-bearing failure property. `CreditSpreadStrategy` reads the event
    blackout date off this object, so an estimator that returned None on a thin
    chain would silently disarm the FOMC blackout on a disordered day."""
    if label == "puts only":
        full = _chain()
        snap = ChainSnapshot(
            "SPXW", SPOT, tuple(o for o in full.options if o.right == "put"), 0.0
        )
    elif label == "single strike":
        snap = ChainSnapshot(
            "SPXW",
            SPOT,
            (
                OptionQuote("P5000", 5000.0, "put", -0.5, 9.0, 9.1),
                OptionQuote("C5000", 5000.0, "call", 0.5, 9.0, 9.1),
            ),
            0.0,
        )
    got = compute_regime(snap, now_et=now, today=TODAY, close_et=CLOSE)
    assert got is not None
    assert math.isnan(got.atm_iv)
    assert math.isnan(got.skew)
    assert got.today == TODAY


def test_min_minutes_guard_boundary():
    """Just inside the guard is unestimable; just outside it is a real number."""
    inside = compute_regime(
        _chain(minutes=MIN_MINUTES_TO_SETTLEMENT - 1),
        now_et=time(15, 51),
        today=TODAY,
        close_et=CLOSE,
    )
    outside = compute_regime(
        _chain(minutes=MIN_MINUTES_TO_SETTLEMENT + 5),
        now_et=time(15, 45),
        today=TODAY,
        close_et=CLOSE,
    )
    assert math.isnan(inside.atm_iv)
    assert math.isfinite(outside.atm_iv)


def test_payload_is_json_encodable_on_both_paths():
    """The audit log refuses what `json` cannot encode, and a refused Tier 1 write
    halts trading — an unestimable regime must never be able to do that."""
    good = compute_regime(_chain(), now_et=_at(10), today=TODAY, close_et=CLOSE)
    bad = compute_regime(
        ChainSnapshot("SPXW", SPOT, (), 0.0), now_et=_at(10), today=TODAY, close_et=CLOSE
    )
    for snapshot in (good, bad):
        encoded = json.loads(json.dumps(snapshot.to_payload()))
        assert encoded["today"] == TODAY.isoformat()
    assert json.loads(json.dumps(bad.to_payload()))["atm_iv"] is None


def test_untradeable_legs_are_excluded():
    """`tradeable` (ask > 0) is the strategy's own filter. Reading the regime over
    a wider strip than the one being traded would measure a different chain than
    the decision is about."""
    base = _chain()
    dead = tuple(
        OptionQuote(o.symbol, o.strike, o.right, o.delta, 0.0, 0.0)
        if o.strike < SPOT - 100
        else o
        for o in base.options
    )
    got = compute_regime(
        ChainSnapshot("SPXW", SPOT, dead, 0.0), now_et=_at(10), today=TODAY, close_et=CLOSE
    )
    # Still estimable from the surviving strip, and not silently crediting the
    # dead wing with a zero mid.
    assert math.isfinite(got.atm_iv)


# --------------------------------------------------------------------------
# The pure helpers
# --------------------------------------------------------------------------


def test_implied_forward_uses_put_call_parity():
    """F = K* + (C - P) at the strike where call and put mids are closest. With a
    symmetric strip priced at spot, the forward should land near spot."""
    snap = _chain()
    puts = sorted((o for o in snap.options if o.right == "put"), key=lambda o: o.strike)
    calls = sorted((o for o in snap.options if o.right == "call"), key=lambda o: o.strike)
    forward = implied_forward(puts, calls, 360.0 / MINUTES_PER_YEAR)
    assert forward == pytest.approx(SPOT, rel=0.01)


def test_implied_forward_none_without_a_paired_strike():
    """No fallback to spot: a silent substitution would bias every reading in a
    way nothing downstream could detect."""
    snap = _chain()
    puts = [o for o in snap.options if o.right == "put" and o.strike < SPOT]
    calls = [o for o in snap.options if o.right == "call" and o.strike > SPOT]
    assert implied_forward(puts, calls, 0.001) is None


def test_strip_variance_needs_two_strikes():
    assert strip_variance([(5000.0, 1.0)], 0.001) is None
    assert strip_variance([], 0.001) is None
    assert strip_variance([(4995.0, 1.0), (5000.0, 1.0)], 0.001) is not None


def test_strip_variance_handles_nonuniform_spacing():
    """SPX ladders are 5-point near the money and 25-point in the wings, so dK is
    measured per strike rather than assumed."""
    uneven = [(4900.0, 0.5), (4950.0, 0.8), (4975.0, 1.2), (4980.0, 1.4)]
    got = strip_variance(uneven, 360.0 / MINUTES_PER_YEAR)
    assert got is not None and got > 0.0


def test_strip_variance_rejects_nonsense_inputs():
    assert strip_variance([(0.0, 1.0), (5000.0, 1.0)], 0.001) is None
    assert strip_variance([(4995.0, -1.0), (5000.0, 1.0)], 0.001) is None
    assert strip_variance([(4995.0, math.nan), (5000.0, 1.0)], 0.001) is None
