"""`signal/implied.py` — the mid-implied delta the short strike is selected by.

Pinned BY PROPERTY rather than by a table of hand-computed values: price -> vol ->
price round-trips across the whole region the selector can reach, put/call parity
off one forward, monotonicity in distance, and the refusal contract — `None` or a
named `Refusal`, never an exception and never a non-finite number, because NaN is
the one value `min()` cannot reject (see `OptionQuote.delta`).

→ docs/decisions/2026-09-21-short-strike-selection-on-mid-implied-delta.md
"""

from __future__ import annotations

import ast
import math
import pathlib

import pytest

from argus.signal import implied, regime
from argus.signal.implied import (
    MidDelta,
    Refusal,
    implied_vol,
    selection_delta,
    solve_leg,
    years_to_settlement,
)
from argus.signal.models import CALL, PUT, OptionQuote

F = 6000.0
HOSTILE = [math.nan, math.inf, -math.inf]


def _t(minutes: float) -> float:
    return minutes / regime.MINUTES_PER_YEAR


def _price(sigma: float, forward: float, strike: float, t: float, right: str) -> float:
    price, _, _ = implied._price_and_vega(
        sigma, forward, strike, math.sqrt(t), math.log(forward / strike), right == CALL
    )
    return price


def _quote(strike, right, bid, ask, delta=None) -> OptionQuote:
    return OptionQuote(f"{right}{strike:g}", strike, right, delta, bid, ask)


# -- round trip ---------------------------------------------------------------


@pytest.mark.parametrize("minutes", [10.0, 30.0, 90.0, 180.0, 360.0])
@pytest.mark.parametrize("sigma", [0.05, 0.12, 0.30, 0.80, 2.00])
@pytest.mark.parametrize("right", [CALL, PUT])
def test_price_to_vol_to_price_round_trips(minutes, sigma, right):
    t = _t(minutes)
    for offset in range(5, 305, 5):
        strike = F + offset if right == CALL else F - offset
        price = _price(sigma, F, strike, t, right)
        if price < 1e-3:   # below any quotable price; the wing is out of reach
            continue
        vol = implied_vol(price, F, strike, t, right)
        assert vol is not None, (strike, price)
        assert abs(_price(vol, F, strike, t, right) - price) < implied.PRICE_TOL
        assert vol == pytest.approx(sigma, rel=1e-4)


def test_put_and_call_at_one_strike_imply_one_vol_off_one_forward():
    """What Black-76 on the forward buys: parity holds exactly off F, so a put and
    a call at the same strike cannot disagree about the market's vol."""
    t = _t(240.0)
    for strike in (5950.0, 5990.0, 6000.0, 6010.0, 6050.0):
        call = _price(0.15, F, strike, t, CALL)
        put = call - (F - strike)        # parity at DISCOUNT 1
        assert implied_vol(call, F, strike, t, CALL) == pytest.approx(
            implied_vol(put, F, strike, t, PUT), rel=1e-6
        )


def test_the_discount_is_regimes():
    """The parity above holds only because the discount is 1 — imported, never
    restated, so the two estimators cannot drift apart."""
    assert regime.DISCOUNT == 1.0


@pytest.mark.parametrize("right", [CALL, PUT])
def test_abs_delta_falls_with_distance_out_of_the_money(right):
    t = _t(300.0)
    deltas = []
    for offset in range(5, 50, 5):     # out to ~2.3 sigma, all quotable
        strike = F + offset if right == CALL else F - offset
        price = _price(0.14, F, strike, t, right)
        assert price > 0.05
        half = 0.025
        got = selection_delta(_quote(strike, right, price - half, price + half), F, t)
        assert got is not None
        deltas.append(abs(got.delta))
    assert deltas == sorted(deltas, reverse=True)
    assert all(0.0 < d < 0.5 for d in deltas)


def test_delta_signs_follow_the_feeds_convention():
    """Calls positive, puts negative — `OptionQuote.delta`'s convention, so a mid
    delta and a feed delta are directly comparable on the record."""
    t = _t(300.0)
    call = selection_delta(_quote(6030.0, CALL, 1.50, 1.60), F, t)
    put = selection_delta(_quote(5970.0, PUT, 1.50, 1.60), F, t)
    assert call is not None and 0.0 < call.delta < 0.5
    assert put is not None and -0.5 < put.delta < 0.0


def test_a_call_and_a_put_at_one_strike_have_deltas_one_apart():
    """N(d1) and N(d1) - 1: the forward-delta parity, at one vol."""
    t = _t(200.0)
    strike = 6020.0
    call_px = _price(0.18, F, strike, t, CALL)
    put_px = call_px - (F - strike)
    call = solve_leg(_quote(strike, CALL, call_px, call_px), F, t)
    put = solve_leg(_quote(strike, PUT, put_px, put_px), F, t)
    assert isinstance(call, MidDelta) and isinstance(put, MidDelta)
    assert call.delta - put.delta == pytest.approx(1.0, abs=1e-6)


def test_it_inverts_the_raw_mid_never_the_floored_one():
    """Two-sided legs only, so the floor never fires here — but the price asked
    about is `(bid + ask) / 2` by construction, and this pins it."""
    t = _t(300.0)
    got = solve_leg(_quote(6030.0, CALL, 1.40, 1.70), F, t)
    assert isinstance(got, MidDelta)
    assert _price(got.iv, F, 6030.0, t, CALL) == pytest.approx(1.55, abs=1e-6)


def test_a_half_spread_moves_the_delta_by_little():
    """The decision record's argument for no spread threshold: a 0.05 half-spread
    at these strikes moves delta by ~0.003, a 0.25 one by ~0.014."""
    t = _t(300.0)
    strike = 6030.0
    mid = _price(0.13, F, strike, t, CALL)
    base = selection_delta(_quote(strike, CALL, mid - 0.05, mid + 0.05), F, t)
    skewed = selection_delta(_quote(strike, CALL, mid, mid + 0.10), F, t)   # mid + 0.05
    assert base is not None and skewed is not None
    assert 0.0 < skewed.delta - base.delta < 0.01


# -- refusals -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("bid", "ask"),
    [(0.0, 0.10), (-0.05, 0.10), (1.60, 1.50), (math.nan, 1.0), (1.0, math.inf)],
)
def test_one_sided_or_crossed_quotes_are_refused(bid, ask):
    assert solve_leg(_quote(6030.0, CALL, bid, ask), F, _t(300.0)) is Refusal.ONE_SIDED
    assert selection_delta(_quote(6030.0, CALL, bid, ask), F, _t(300.0)) is None


@pytest.mark.parametrize(
    ("strike", "right", "price"),
    [
        (6030.0, CALL, F),          # at the upper bound
        (6030.0, CALL, F + 1.0),    # above it
        (5970.0, CALL, 30.0),       # AT intrinsic
        (5970.0, CALL, 29.0),       # below intrinsic
        (5970.0, PUT, 5970.0),      # at the put's upper bound (K)
        (6030.0, PUT, 20.0),        # below the put's intrinsic (30)
    ],
)
def test_prices_no_vol_can_produce_are_out_of_bounds(strike, right, price):
    got = solve_leg(_quote(strike, right, price, price), F, _t(300.0))
    assert got is Refusal.OUT_OF_BOUNDS
    assert implied_vol(price, F, strike, _t(300.0), right) is None


def test_a_price_needing_a_vol_outside_the_bracket_does_not_converge():
    t = _t(300.0)
    strike = 6100.0
    too_rich = _price(implied.SIGMA_HI * 1.5, F, strike, t, CALL)
    assert too_rich < F     # inside the arbitrage bounds, outside the bracket
    assert solve_leg(_quote(strike, CALL, too_rich, too_rich), F, t) is Refusal.NO_CONVERGENCE


@pytest.mark.parametrize("value", HOSTILE + [0.0, -1.0])
@pytest.mark.parametrize("arg", ["price", "forward", "strike", "t"])
def test_hostile_numbers_never_raise_and_never_escape(arg, value):
    args = {"price": 1.5, "forward": F, "strike": 6030.0, "t": _t(300.0)}
    args[arg] = value
    vol = implied_vol(args["price"], args["forward"], args["strike"], args["t"], CALL)
    assert vol is None or math.isfinite(vol)


def test_an_unknown_right_is_refused_not_guessed():
    assert implied_vol(1.5, F, 6030.0, _t(300.0), "straddle") is None


def test_nothing_it_returns_is_ever_non_finite():
    """A sweep over quoted prices, including the thin wings, both rights, the whole
    session — every answer is either a refusal or two finite numbers."""
    for minutes in (10.0, 60.0, 390.0):
        t = _t(minutes)
        for offset in range(0, 400, 5):
            for right, strike in ((CALL, F + offset), (PUT, F - offset)):
                for bid in (0.05, 0.50, 5.0, 50.0):
                    got = solve_leg(_quote(strike, right, bid, bid + 0.10), F, t)
                    if isinstance(got, MidDelta):
                        assert math.isfinite(got.delta) and math.isfinite(got.iv)
                        assert -1.0 < got.delta < 1.0
                        assert implied.SIGMA_LO <= got.iv <= implied.SIGMA_HI


def test_convergence_is_fast_on_the_region_the_selector_reaches():
    """No cache and no early exit is justified by a cold solve being cheap; this
    pins the part of that claim that is not wall-clock — the iteration count."""
    calls = 0
    real = implied._price_and_vega

    def counting(*args):
        nonlocal calls
        calls += 1
        return real(*args)

    implied._price_and_vega = counting
    try:
        solved = 0
        for minutes in (30.0, 180.0, 360.0):
            for sigma in (0.08, 0.14, 0.25):
                for offset in range(5, 105, 5):
                    for right, strike in ((CALL, F + offset), (PUT, F - offset)):
                        price = real(sigma, F, strike, math.sqrt(_t(minutes)),
                                     math.log(F / strike), right == CALL)[0]
                        if price < 0.05:
                            continue
                        before = calls
                        assert implied_vol(price, F, strike, _t(minutes), right) is not None
                        assert calls - before <= 15
                        solved += 1
    finally:
        implied._price_and_vega = real
    assert solved > 100
    assert calls / solved < 8


# -- time --------------------------------------------------------------------


def test_time_uses_regimes_basis_and_floor():
    assert years_to_settlement(300.0) == 300.0 / regime.MINUTES_PER_YEAR
    assert years_to_settlement(regime.MIN_MINUTES_TO_SETTLEMENT) is not None
    assert years_to_settlement(regime.MIN_MINUTES_TO_SETTLEMENT - 0.01) is None


@pytest.mark.parametrize("minutes", HOSTILE + [-5.0, 0.0])
def test_unusable_time_is_refused_never_defaulted(minutes):
    """NaN is what a hand-built `RegimeSnapshot` carries when it never asserted a
    time; it must refuse, not pass through as a number."""
    assert years_to_settlement(minutes) is None


# -- structure ----------------------------------------------------------------


def test_the_conventions_are_imported_from_regime_never_restated():
    """The decision's rule: `implied.py` IMPORTS its time basis, floor and discount,
    so the two estimates of one market can never drift apart."""
    src = pathlib.Path(implied.__file__).read_text()
    tree = ast.parse(src)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "argus.signal.regime"
        for alias in node.names
    }
    assert {"MINUTES_PER_YEAR", "MIN_MINUTES_TO_SETTLEMENT", "DISCOUNT"} <= imported
    assigned = {
        target.id
        for node in ast.walk(tree) if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    }
    assert not assigned & {"MINUTES_PER_YEAR", "MIN_MINUTES_TO_SETTLEMENT", "DISCOUNT"}


def test_the_module_is_pure():
    """No I/O, no clock: the replay re-runs the runtime's arithmetic verbatim only
    because nothing here can differ between the two runs."""
    tree = ast.parse(pathlib.Path(implied.__file__).read_text())
    modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert modules <= {"__future__", "enum", "math", "dataclasses", "argus"}
