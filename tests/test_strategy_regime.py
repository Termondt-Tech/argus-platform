"""The regime gate and the event blackout on CreditSpreadStrategy.

Split from `test_strategy.py` deliberately: that file covers the BASELINE
selection logic, and the single most important property asserted here is that
none of this changes that baseline while `[regime]` is unconfigured.
"""

from __future__ import annotations

import math
from datetime import date, time

import pytest

from argus.config import RegimeConfig, SignalConfig
from argus.signal.models import ChainSnapshot, OptionQuote
from argus.signal.strategy import (
    EVENT_BLACKOUT,
    REGIME_SIDE_REFUSED,
    CreditSpreadStrategy,
    RegimeSnapshot,
    classify_iv_regime,
)

MIDDAY = time(12, 0)
TODAY = date(2026, 8, 4)
FOMC_DAY = date(2026, 9, 16)


def _cfg(**over) -> SignalConfig:
    base = dict(
        sides=("put",),
        target_short_delta=0.15,
        width=5.0,
        max_width=10.0,
        min_credit=0.75,
        entry_earliest=time(10, 0),
        entry_latest=time(15, 0),
        cadence_s=5.0,
        short_delta_source="feed",
    )
    base.update(over)
    return SignalConfig(**base)


def _regime_cfg(**over) -> RegimeConfig:
    base = dict(
        atm_iv_low_max=0.15,
        atm_iv_high_min=0.30,
        max_downside_skew_for_puts=0.002,
        max_upside_skew_for_calls=0.002,
        size_multiplier_low=1.0,
        size_multiplier_mid=0.75,
        size_multiplier_high=0.5,
    )
    base.update(over)
    return RegimeConfig(**base)


def _opt(strike, right, delta, bid, ask):
    return OptionQuote(f"{right[0].upper()}{strike:g}", strike, right, delta, bid, ask)


def _chain() -> ChainSnapshot:
    return ChainSnapshot(
        "SPXW",
        5000.0,
        (
            _opt(4975, "put", -0.15, 1.50, 1.60),  # short
            _opt(4970, "put", -0.10, 0.70, 0.80),  # long
            _opt(5025, "call", 0.15, 1.50, 1.60),
            _opt(5030, "call", 0.10, 0.70, 0.80),
        ),
        captured_at=0.0,
    )


def _regime(*, atm_iv=0.20, skew=0.0, today=TODAY) -> RegimeSnapshot:
    return RegimeSnapshot(atm_iv=atm_iv, skew=skew, today=today)


# --------------------------------------------------------------------------
# The inert-by-default guarantee
# --------------------------------------------------------------------------


def test_regime_argument_alone_changes_nothing():
    """THE headline property of this round. Passing a real regime with NO
    regime_config must produce byte-identical results to the pre-existing call —
    that is what makes the telemetry provably a no-op."""
    strategy = CreditSpreadStrategy(_cfg())
    without = strategy.evaluate(_chain(), MIDDAY)
    with_regime = strategy.evaluate(_chain(), MIDDAY, _regime(skew=-99.0, atm_iv=9.9))
    assert without.fired and with_regime.fired
    assert without.signal.to_payload() == with_regime.signal.to_payload()


def test_size_multiplier_is_one_without_regime_config():
    strategy = CreditSpreadStrategy(_cfg())
    assert strategy.size_multiplier(_regime(atm_iv=0.9)) == 1.0
    assert strategy.size_multiplier(None) == 1.0


def test_side_gate_permits_everything_without_regime_config():
    strategy = CreditSpreadStrategy(_cfg(sides=("put", "call")))
    # A skew far beyond any threshold, but no config to compare it against.
    assert strategy.evaluate(_chain(), MIDDAY, _regime(skew=-10.0)).fired


# --------------------------------------------------------------------------
# The event blackout — LIVE, and independent of regime_config
# --------------------------------------------------------------------------


def test_blackout_suppresses_entry_on_a_listed_date():
    strategy = CreditSpreadStrategy(_cfg(), blackout_dates=frozenset({FOMC_DAY}))
    result = strategy.evaluate(_chain(), MIDDAY, _regime(today=FOMC_DAY))
    assert not result.fired
    assert result.reason == EVENT_BLACKOUT
    assert result.detail["today"] == FOMC_DAY.isoformat()


def test_blackout_allows_an_unlisted_date():
    strategy = CreditSpreadStrategy(_cfg(), blackout_dates=frozenset({FOMC_DAY}))
    assert strategy.evaluate(_chain(), MIDDAY, _regime(today=TODAY)).fired


def test_blackout_does_not_require_regime_config():
    """The coupling that matters: the blackout is live with regime_config=None.
    It must not be hostage to a gate that needs months of calibration first."""
    strategy = CreditSpreadStrategy(
        _cfg(), regime_config=None, blackout_dates=frozenset({FOMC_DAY})
    )
    assert not strategy.evaluate(_chain(), MIDDAY, _regime(today=FOMC_DAY)).fired


def test_blackout_survives_an_unestimable_regime():
    """A NaN reading must NOT disarm the blackout. This is the whole reason
    `compute_regime` never returns None — a disordered chain on an FOMC day is
    exactly when the blackout matters most."""
    strategy = CreditSpreadStrategy(_cfg(), blackout_dates=frozenset({FOMC_DAY}))
    unestimable = RegimeSnapshot(atm_iv=math.nan, skew=math.nan, today=FOMC_DAY)
    result = strategy.evaluate(_chain(), MIDDAY, unestimable)
    assert not result.fired and result.reason == EVENT_BLACKOUT


def test_empty_blackout_set_suppresses_nothing():
    """The fallback that keeps the telemetry-only claim true if [events] is ever
    dropped while the regime wiring stays."""
    strategy = CreditSpreadStrategy(_cfg(), blackout_dates=frozenset())
    assert strategy.evaluate(_chain(), MIDDAY, _regime(today=FOMC_DAY)).fired


def test_blackout_inert_without_a_regime_argument():
    """No regime => no date => nothing to match. Documents the one path on which
    a blackout date does NOT suppress (e.g. tools calling evaluate/2)."""
    strategy = CreditSpreadStrategy(_cfg(), blackout_dates=frozenset({FOMC_DAY}))
    assert strategy.evaluate(_chain(), MIDDAY).fired


# --------------------------------------------------------------------------
# classify_iv_regime
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "atm_iv, expected",
    [
        (0.05, "low"),
        (0.15, "low"),      # boundary: at low_max is still low
        (0.1501, "mid"),
        (0.20, "mid"),
        (0.2999, "mid"),
        (0.30, "high"),     # boundary: at high_min is already high
        (0.90, "high"),
    ],
)
def test_classify_iv_regime_buckets(atm_iv, expected):
    assert classify_iv_regime(atm_iv, _regime_cfg()) == expected


def test_classify_iv_regime_nan_is_mid():
    """Fail-SAFE, and the choice matters: "low" carries the LARGEST size
    multiplier, so bucketing an unreadable chain as low would hand maximum size to
    exactly the case nobody could measure."""
    assert classify_iv_regime(math.nan, _regime_cfg()) == "mid"


# --------------------------------------------------------------------------
# _side_permitted / size_multiplier, once regime_config exists
# --------------------------------------------------------------------------


def test_put_side_refused_on_steep_downside_skew():
    strategy = CreditSpreadStrategy(_cfg(sides=("put",)), regime_config=_regime_cfg())
    result = strategy.evaluate(_chain(), MIDDAY, _regime(skew=-0.01))
    assert not result.fired
    assert result.reason == REGIME_SIDE_REFUSED
    assert result.detail["per_side"]["put"]["reason"] == REGIME_SIDE_REFUSED


def test_put_side_permitted_within_threshold():
    strategy = CreditSpreadStrategy(_cfg(sides=("put",)), regime_config=_regime_cfg())
    assert strategy.evaluate(_chain(), MIDDAY, _regime(skew=-0.001)).fired


def test_call_side_refused_on_steep_upside_skew():
    strategy = CreditSpreadStrategy(_cfg(sides=("call",)), regime_config=_regime_cfg())
    result = strategy.evaluate(_chain(), MIDDAY, _regime(skew=0.01))
    assert not result.fired and result.reason == REGIME_SIDE_REFUSED


def test_refused_side_falls_through_to_the_permitted_one():
    """A refused put must not stop the call side from being evaluated."""
    strategy = CreditSpreadStrategy(
        _cfg(sides=("put", "call")), regime_config=_regime_cfg()
    )
    result = strategy.evaluate(_chain(), MIDDAY, _regime(skew=-0.01))
    assert result.fired and result.signal.side == "call"


def test_nan_skew_permits_both_sides():
    """An unreadable chain is not evidence that a side is dangerous. The gate only
    ever REMOVES opportunities, so its fail-safe direction is the baseline."""
    strategy = CreditSpreadStrategy(
        _cfg(sides=("put",)), regime_config=_regime_cfg()
    )
    assert strategy.evaluate(_chain(), MIDDAY, _regime(skew=math.nan)).fired


def test_regime_side_refused_never_wins_the_headline_over_a_real_rejection():
    """A side refused before its strikes were examined is the least informative
    reason there is; a credit near-miss on the other side must outrank it."""
    strategy = CreditSpreadStrategy(
        _cfg(sides=("put", "call"), min_credit=99.0), regime_config=_regime_cfg()
    )
    result = strategy.evaluate(_chain(), MIDDAY, _regime(skew=-0.01))
    assert not result.fired
    assert result.reason == "credit_below_min"


@pytest.mark.parametrize(
    "atm_iv, expected", [(0.05, 1.0), (0.20, 0.75), (0.50, 0.5)]
)
def test_size_multiplier_tracks_the_bucket(atm_iv, expected):
    strategy = CreditSpreadStrategy(_cfg(), regime_config=_regime_cfg())
    assert strategy.size_multiplier(_regime(atm_iv=atm_iv)) == expected


def test_size_multiplier_never_exceeds_one_by_construction():
    """Config validation caps every multiplier at 1.0, so the sizer's relative rail
    can only ever be scaled DOWN. Asserted here so the guarantee is pinned at the
    consumer, not only at the loader."""
    strategy = CreditSpreadStrategy(_cfg(), regime_config=_regime_cfg())
    for atm_iv in (0.01, 0.15, 0.30, 5.0, math.nan):
        assert strategy.size_multiplier(_regime(atm_iv=atm_iv)) <= 1.0
