"""Short-strike selection on mid-implied delta (`signal.short_delta_source`).

The replay gate (`tests/test_replay_selection.py`) checks the ARITHMETIC against
recorded quotes, but a recorded row holds only two legs, so it cannot re-run the
search. The search is pinned here on synthetic chains: out-of-the-money only,
closest to target, a tie to the farther strike, no fallback to the feed delta, and
"feed" mode unchanged.

The chains are priced off one Black-76 surface, so every leg's TRUE delta is
known, and the FEED deltas are then set independently — which is what lets a test
say "the feed was wrong here, and selection did not care".

→ docs/decisions/2026-09-21-short-strike-selection-on-mid-implied-delta.md
"""

from __future__ import annotations

import json
import math
from datetime import date, time

import pytest

from argus.config import SignalConfig
from argus.signal import implied, regime
from argus.signal.implied import MidDelta
from argus.signal.models import CALL, PUT, ChainSnapshot, OptionQuote, RegimeSnapshot
from argus.signal.strategy import (
    CREDIT_BELOW_MIN,
    NO_SHORT_LEG_WITH_FRESH_DELTA,
    NO_SHORT_LEG_WITH_MID_DELTA,
    CreditSpreadStrategy,
    _mid_pick,
)

MIDDAY = time(12, 0)
F = 6000.0
MINUTES = 240.0
T = MINUTES / regime.MINUTES_PER_YEAR
REGIME = RegimeSnapshot(
    atm_iv=0.15, skew=0.0, today=date(2026, 9, 22), minutes_to_settlement=MINUTES
)


def _cfg(**over) -> SignalConfig:
    base = dict(
        sides=(PUT, CALL),
        target_short_delta=0.15,
        width=5.0,
        max_width=10.0,
        min_credit=0.05,
        entry_earliest=time(10, 0),
        entry_latest=time(15, 0),
        cadence_s=5.0,
        short_delta_source="mid",
    )
    base.update(over)
    return SignalConfig(**base)


def _bs(sigma: float, strike: float, right: str, forward: float = F) -> float:
    price, _, _ = implied._price_and_vega(
        sigma, forward, strike, math.sqrt(T), math.log(forward / strike), right == CALL
    )
    return price


def _true_delta(sigma: float, strike: float, right: str, forward: float = F) -> float:
    v = sigma * math.sqrt(T)
    n = implied._ncdf(math.log(forward / strike) / v + v / 2)
    return n if right == CALL else n - 1.0


def _leg(
    strike: float,
    right: str,
    *,
    sigma: float = 0.15,
    half: float = 0.025,
    feed_delta: float | None | str = "true",
    forward: float = F,
) -> OptionQuote:
    mid = _bs(sigma, strike, right, forward)
    delta = _true_delta(sigma, strike, right, forward) if feed_delta == "true" else feed_delta
    return OptionQuote(
        f"{right[0].upper()}{strike:g}", strike, right, delta, max(mid - half, 0.0), mid + half
    )


def _chain(
    *, rights=(PUT, CALL), lo=-120, hi=125, spot: float = F, forward: float = F, **leg_kw
) -> ChainSnapshot:
    """A 5-point ladder of both rights, ITM legs included, priced off `forward`."""
    options = tuple(
        _leg(F + k, right, forward=forward, **leg_kw)
        for right in rights
        for k in range(lo, hi, 5)
    )
    return ChainSnapshot("SPXW", spot, options, captured_at=0.0)


def _closest_true(side: str, target: float = 0.15) -> float:
    """The OTM strike whose TRUE delta is closest to target."""
    strikes = [F + k for k in range(5, 125, 5)] if side == CALL else [F - k for k in range(5, 125, 5)]
    return min(strikes, key=lambda k: abs(abs(_true_delta(0.15, k, side)) - target))


def _pick(chain, cfg, regime_=REGIME):
    return CreditSpreadStrategy(cfg).evaluate(chain, MIDDAY, regime_)


# -- the mid selector ---------------------------------------------------------


@pytest.mark.parametrize("side", [PUT, CALL])
def test_mid_mode_selects_the_strike_whose_mid_delta_is_closest(side):
    sig = _pick(_chain(), _cfg(sides=(side,))).signal
    assert sig is not None
    assert sig.short_leg.strike == _closest_true(side)
    assert sig.selection.delta_source == "mid"
    assert abs(sig.selection.short_mid_delta - _true_delta(0.15, sig.short_leg.strike, side)) < 0.01


@pytest.mark.parametrize("side", [PUT, CALL])
def test_mid_mode_is_blind_to_the_feed_delta(side):
    """The decision in one test: the feed delta is never consulted. Three feeds —
    absent, stale toward the money, stale away from it — one selection."""
    picks = set()
    for feed in (None, 0.30, 0.02):
        signed = None if feed is None else (feed if side == CALL else -feed)
        sig = _pick(_chain(feed_delta=signed), _cfg(sides=(side,))).signal
        assert sig is not None
        picks.add(sig.short_leg.strike)
    assert picks == {_closest_true(side)}


def test_a_lagging_greeks_feed_no_longer_stops_selection():
    """Under "feed", a chain with no fresh delta cannot select a short strike at
    all (the 2026-08-24 regression's shape). Under "mid" it selects normally."""
    chain = _chain(feed_delta=None)
    assert _pick(chain, _cfg(short_delta_source="feed")).reason == NO_SHORT_LEG_WITH_FRESH_DELTA
    assert _pick(chain, _cfg()).signal is not None


def test_the_credit_floor_no_longer_selects_the_feeds_error():
    """THE DEFECT, reproduced. Stale call greeks read ~0.15 one strike closer to
    the money; that strike clears `min_credit` and the true 0.15 strike does not.
    "feed" sells the closer strike; "mid" selects the true one and the floor
    refuses it cleanly — Consequence 1, the change working."""
    true_short = _closest_true(CALL)
    closer = true_short - 5.0
    options = []
    for k in range(-60, 125, 5):
        strike = F + k
        # the feed under-states: every call reads the delta of the strike 5 out
        feed = _true_delta(0.15, strike + 5.0, CALL)
        options.append(_leg(strike, CALL, feed_delta=feed))
    chain = ChainSnapshot("SPXW", F, tuple(options), captured_at=0.0)
    credit_at = {
        o.strike: o.floored_mid - next(x for x in options if x.strike == o.strike + 5).floored_mid
        for o in options if o.strike in (true_short, closer)
    }
    floor = (credit_at[true_short] + credit_at[closer]) / 2      # between the two
    cfg = dict(sides=(CALL,), min_credit=round(floor, 4))

    fed = _pick(chain, _cfg(short_delta_source="feed", **cfg))
    assert fed.signal is not None and fed.signal.short_leg.strike == closer
    assert fed.signal.selection.alt_pick_strike == true_short     # the mid's pick, recorded

    mid = _pick(chain, _cfg(**cfg))
    assert mid.signal is None and mid.reason == CREDIT_BELOW_MIN
    near = mid.detail["per_side"][CALL]
    assert near["short_strike"] == true_short
    assert near["alt_pick_strike"] == closer                       # the feed's pick
    assert near["delta_source"] == "mid"


def test_an_in_the_money_leg_is_never_a_short():
    """At a target no OTM leg can reach (0.55 > any OTM |delta|), the ITM leg that
    sits right on it must still not be chosen: only OTM legs are ASKED."""
    sig = _pick(_chain(), _cfg(sides=(CALL,), target_short_delta=0.55)).signal
    assert sig is not None
    assert sig.short_leg.strike > F
    assert sig.short_leg.strike == F + 5.0      # the OTM leg nearest 0.55


def test_the_otm_boundary_is_the_forward_not_spot():
    """OTM is judged against F. With the parity forward 10 points above spot, the
    call struck between them is IN the money relative to what the options price."""
    chain = _chain(spot=F - 10.0)
    result = _pick(chain, _cfg(sides=(CALL,), target_short_delta=0.49))
    assert result.signal.selection.forward_source == "parity"
    assert result.signal.selection.forward == pytest.approx(F, abs=0.05)
    assert result.signal.short_leg.strike > F


# -- ties ---------------------------------------------------------------------


def _solved(*pairs):
    return {k: MidDelta(delta=d, iv=0.15) for k, d in pairs}


def test_a_tie_goes_to_the_farther_call():
    # 0.375 and 0.125 are exact binary fractions, equidistant from 0.25
    legs = [OptionQuote("a", 6010.0, CALL, None, 1, 2), OptionQuote("b", 6030.0, CALL, None, 1, 2)]
    for order in (legs, legs[::-1]):
        pick = _mid_pick(order, _solved((6010.0, 0.375), (6030.0, 0.125)), CALL, 0.25)
        assert pick.strike == 6030.0


def test_a_tie_goes_to_the_farther_put():
    legs = [OptionQuote("a", 5990.0, PUT, None, 1, 2), OptionQuote("b", 5970.0, PUT, None, 1, 2)]
    for order in (legs, legs[::-1]):
        pick = _mid_pick(order, _solved((5990.0, -0.375), (5970.0, -0.125)), PUT, 0.25)
        assert pick.strike == 5970.0


# -- refusals -----------------------------------------------------------------


@pytest.mark.parametrize(
    "regime_",
    [
        None,
        RegimeSnapshot(atm_iv=0.15, skew=0.0, today=date(2026, 9, 22)),   # NaN minutes
        RegimeSnapshot(atm_iv=0.15, skew=0.0, today=date(2026, 9, 22), minutes_to_settlement=9.0),
    ],
    ids=["no-regime", "nan-minutes", "inside-last-10-min"],
)
def test_no_time_to_settlement_refuses_with_no_feed_fallback(regime_):
    """A silent fallback to the feed delta would re-introduce exactly the selection
    effect the mid selector removes — so there is none, even with fresh feed deltas
    on every leg."""
    result = _pick(_chain(), _cfg(), regime_)
    assert result.signal is None
    assert result.reason == NO_SHORT_LEG_WITH_MID_DELTA
    for side in (PUT, CALL):
        detail = result.detail["per_side"][side]
        assert detail["cause"] == "no_time_to_settlement"
        assert detail["candidates"] == 0
        assert detail["refusals"] == {"one_sided": 0, "out_of_bounds": 0, "no_convergence": 0}
        assert detail["forward"] is None and detail["forward_source"] is None
        assert detail["alt_pick_strike"] is not None      # the feed's pick, recorded
    json.dumps(result.detail, allow_nan=False)


def test_feed_mode_is_unaffected_by_a_missing_regime():
    assert _pick(_chain(), _cfg(short_delta_source="feed"), None).signal is not None


def test_refusals_are_counted_by_cause_over_the_otm_legs_asked():
    """One-sided (bid 0) wings are the common refusal; the detail names it rather
    than folding it into a bare count."""
    options = tuple(
        OptionQuote(f"C{F + k:g}", F + k, CALL, 0.15, 0.0, 0.10) for k in range(5, 50, 5)
    ) + tuple(
        OptionQuote(f"C{F - k:g}", F - k, CALL, 0.60, 40.0, 41.0) for k in range(5, 20, 5)
    )
    chain = ChainSnapshot("SPXW", F, options, captured_at=0.0)
    result = _pick(chain, _cfg(sides=(CALL,)))
    assert result.reason == NO_SHORT_LEG_WITH_MID_DELTA
    detail = result.detail["per_side"][CALL]
    assert detail["candidates"] == 9                       # OTM legs only
    assert detail["refusals"] == {"one_sided": 9, "out_of_bounds": 0, "no_convergence": 0}
    assert detail["cause"] is None
    assert detail["forward_source"] == "spot"


# -- the forward --------------------------------------------------------------


def test_the_parity_forward_is_used_when_a_strike_quotes_both_rights():
    sig = _pick(_chain(forward=F + 3.0, spot=F), _cfg(sides=(PUT,))).signal
    assert sig.selection.forward_source == "parity"
    assert sig.selection.forward == pytest.approx(F + 3.0, abs=0.05)


def test_no_parity_pair_falls_back_to_spot_and_says_so():
    sig = _pick(_chain(rights=(PUT,)), _cfg(sides=(PUT,))).signal
    assert sig is not None
    assert sig.selection.forward_source == "spot"
    assert sig.selection.forward == F


def test_no_forward_and_no_spot_refuses():
    chain = _chain(rights=(PUT,), spot=math.nan)
    result = _pick(chain, _cfg(sides=(PUT,)))
    assert result.reason == NO_SHORT_LEG_WITH_MID_DELTA
    assert result.detail["per_side"][PUT]["cause"] == "no_forward"
    json.dumps(result.detail, allow_nan=False)


# -- "feed" mode: the rollback, byte for byte -----------------------------------


def test_feed_mode_selects_by_the_feed_delta_and_records_the_mid_pick():
    options = []
    for k in range(-60, 125, 5):
        strike = F + k
        feed = _true_delta(0.15, strike + 10.0, CALL)     # the feed is two strikes off
        options.append(_leg(strike, CALL, feed_delta=feed))
    chain = ChainSnapshot("SPXW", F, tuple(options), captured_at=0.0)
    sig = _pick(chain, _cfg(sides=(CALL,), short_delta_source="feed")).signal
    assert sig.short_leg.strike == _closest_true(CALL) - 10.0
    assert sig.selection.delta_source == "feed"
    assert sig.selection.alt_pick_strike == _closest_true(CALL)


def test_feed_mode_tie_still_falls_to_chain_order():
    """The rollback must be the OLD behaviour, including its unspecified tie."""
    # 0.375 and 0.125 are exact binary fractions, equidistant from 0.25
    a = OptionQuote("a", 6010.0, CALL, 0.375, 3.0, 3.1)
    b = OptionQuote("b", 6030.0, CALL, 0.125, 1.0, 1.1)
    c = OptionQuote("c", 6040.0, CALL, 0.05, 0.5, 0.6)
    chain = ChainSnapshot("SPXW", F, (a, b, c), captured_at=0.0)
    cfg = _cfg(
        sides=(CALL,), short_delta_source="feed", max_width=40.0, target_short_delta=0.25
    )
    assert _pick(chain, cfg).signal.short_leg.strike == 6010.0
    chain = ChainSnapshot("SPXW", F, (b, a, c), captured_at=0.0)
    assert _pick(chain, cfg).signal.short_leg.strike == 6030.0


# -- the record -----------------------------------------------------------------


def test_the_candidate_payload_carries_both_deltas_and_the_counterfactual():
    sig = _pick(_chain(feed_delta=None), _cfg(sides=(PUT,))).signal
    payload = sig.to_payload()
    for leg in ("short", "long"):
        assert {"delta", "mid_delta", "mid_iv"} <= set(payload[leg])
    assert payload["short"]["delta"] is None                 # the FEED delta, unchanged key
    assert payload["short"]["mid_delta"] == pytest.approx(sig.selection.short_mid_delta)
    assert payload["short"]["mid_delta"] < 0                 # a put, signed
    assert payload["delta_source"] == "mid"
    assert payload["forward_source"] == "parity"
    assert payload["alt_pick_strike"] is None                # no feed delta to pick by
    json.dumps(payload, allow_nan=False)


def test_the_near_miss_carries_the_selection_flat():
    result = _pick(_chain(), _cfg(sides=(PUT,), min_credit=4.0))
    assert result.reason == CREDIT_BELOW_MIN
    near = result.detail["per_side"][PUT]
    assert {
        "short_mid_delta", "short_mid_iv", "long_mid_delta", "alt_pick_strike",
        "delta_source", "forward", "forward_source",
    } <= set(near)
    assert "short" not in near and "long" not in near
    json.dumps(result.detail, allow_nan=False)


def test_a_synthesised_signal_records_nulls_not_absences():
    """Chunk 4's exit path builds a `SpreadSignal` no selector chose; its record
    keeps the shape with nulls, never a missing key."""
    from argus.signal.models import SpreadSignal

    leg = OptionQuote("x", 5990.0, PUT, None, 1.0, 1.1)
    payload = SpreadSignal("SPXW", PUT, leg, leg, 0.5, 5.0, captured_at=0.0).to_payload()
    assert payload["delta_source"] is None and payload["alt_pick_strike"] is None
    assert payload["short"]["mid_delta"] is None
    json.dumps(payload, allow_nan=False)


# -- performance (never asserted in CI) -----------------------------------------


@pytest.mark.skipif(
    "not config.getoption('-k') or 'benchmark' not in config.getoption('-k')",
    reason="benchmark: run with -k benchmark",
)
def test_benchmark_evaluate_on_a_60_leg_chain():
    """Reports per-tick cost; wall-clock is NEVER asserted. The decision's budget
    is ~0.2 ms against the heartbeat's 50 ms lag alarm."""
    import time as _time

    chain = _chain(lo=-75, hi=80)                    # 31 strikes x 2 rights, ~60 OTM
    strategy = CreditSpreadStrategy(_cfg(min_credit=99.0))   # never fires: both sides run
    n = 2000
    start = _time.perf_counter()
    for _ in range(n):
        strategy.evaluate(chain, MIDDAY, REGIME)
    per_tick = (_time.perf_counter() - start) / n
    print(f"\nevaluate(): {per_tick * 1e3:.3f} ms/tick over {len(chain.options)} legs")
