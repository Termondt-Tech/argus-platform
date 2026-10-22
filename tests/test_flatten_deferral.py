"""The flatten deferral's arithmetic, its config, and the structural pins. (2026-09-25)

`execution/flatten_deferral.py` is pure, so every rule of the design's §4.3 is tested
here directly — including against the RECORDED inputs of the live flattens, so the
numbers in the design and the config's provenance string are reproduced by code
rather than restated by hand.

→ docs/designs/2026-09-25-deferring-a-far-otm-flatten.md §4
"""

from __future__ import annotations

import ast
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from argus.config import (
    FLATTEN_DEFERRAL_MODES,
    MAX_FLATTEN_DEFER_TAIL_ALPHA,
    ConfigError,
)
from argus.execution import flatten_deferral as fd
from argus.execution.flatten_deferral import (
    DEFERRED,
    DeferralInputs,
    DeferralMode,
    DeferralParams,
    barrier_for,
    decide,
    deferral_boundary,
    standardized_distance,
    touch_probability,
)
from tests.support import make_config

_SRC = Path(__file__).resolve().parents[1] / "src" / "argus"

#: Production's calibrated values (2026-09-25), with the production buffer.
PARAMS = DeferralParams(
    insurance_multiple=10.0, tail_z99=2.33, tail_alpha=2.93, buffer_points=5.0
)


def _inputs(**overrides) -> DeferralInputs:
    base = dict(
        side="put", spot=7706.035, short_strike=7625.0, width=5.0,
        sigma_implied=0.218, sigma_realized=0.117, minutes_to_close=30.0,
    )
    base.update(overrides)
    return DeferralInputs(**base)


# -- the recorded live flattens ----------------------------------------------


def test_2026_09_24_defers():
    """The motivating day: 80 points out, IV ~0.22. z ≈ 6.02, P_touch ≈ 6e-4, and
    k·P·W ≈ 0.031 ≤ one tick."""
    d = decide(_inputs(), PARAMS)
    assert d.defer and d.reason == DEFERRED
    assert d.barrier == 7630.0
    assert d.z == pytest.approx(6.02, abs=0.02)
    assert d.p_touch == pytest.approx(6.2e-4, rel=0.05)
    assert d.lhs == pytest.approx(0.031, abs=0.002)
    assert d.lhs <= d.premium == 0.05


def test_2026_09_17_defers_and_2026_09_23_does_not():
    """The pair revision 2's point threshold could not tell apart: both ~0.70% out of
    the money, but 5.17σ against 4.55σ."""
    sept17 = decide(
        _inputs(spot=7637.48, short_strike=7585.0, sigma_implied=0.160, sigma_realized=0.073),
        PARAMS,
    )
    sept23 = decide(
        _inputs(side="call", spot=7706.2, short_strike=7760.0,
                sigma_implied=0.184, sigma_realized=0.100),
        PARAMS,
    )
    assert sept17.defer
    assert not sept23.defer and sept23.reason == fd.OVER_BUDGET
    assert sept17.z > 5.1 and 4.4 < sept23.z < 4.7


@pytest.mark.parametrize(
    "spot,strike,iv",
    [(7638.68, 7595.0, 0.187), (7774.02, 7740.0, 0.148)],
    ids=["2026-09-18", "2026-09-22"],
)
def test_the_nearer_live_flattens_still_flatten(spot, strike, iv):
    d = decide(_inputs(spot=spot, short_strike=strike, sigma_implied=iv, sigma_realized=0.07), PARAMS)
    assert not d.defer


# -- every refusal, in order ---------------------------------------------------


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"ineligible": "broker_poll_stale"}, "broker_poll_stale"),
        ({"spot": None}, fd.NO_SPOT),
        ({"spot": float("nan")}, fd.NO_SPOT),
        ({"spot": -1.0}, fd.NO_SPOT),
        ({"side": None}, fd.NO_STRIKE),
        ({"side": "straddle"}, fd.NO_STRIKE),
        ({"short_strike": None}, fd.NO_STRIKE),
        ({"width": None}, fd.NO_WIDTH),
        ({"width": 0.0}, fd.NO_WIDTH),
        ({"sigma_implied": None}, fd.NO_IMPLIED),
        ({"sigma_implied": float("inf")}, fd.NO_IMPLIED),
        ({"sigma_realized": None}, fd.NO_REALIZED),
        ({"sigma_implied": -0.1, "sigma_realized": -0.2}, fd.VOL_UNUSABLE),
        ({"spot": 7630.0}, fd.AT_BARRIER),          # exactly at the barrier
        ({"spot": 7600.0}, fd.AT_BARRIER),          # through it
        ({"spot": 7640.0}, fd.INSIDE_BODY),         # z < z99
        ({"spot": 7680.0}, fd.OVER_BUDGET),         # in the tail, not far enough
    ],
)
def test_every_unusable_input_refuses_with_its_own_reason(overrides, reason):
    d = decide(_inputs(**overrides), PARAMS)
    assert not d.defer
    assert d.reason == reason


def test_an_ineligible_decision_attempts_no_arithmetic():
    d = decide(_inputs(ineligible="watchdog_degraded"), PARAMS)
    assert (d.z, d.p_touch, d.lhs, d.barrier) == (None, None, None, None)


# -- the model's properties ----------------------------------------------------


def test_z_is_scale_free_in_the_index_level():
    """A log ratio: multiply spot, strike and buffer by c and nothing changes — the
    property a point or percent threshold lacks in the other two dimensions."""
    base = decide(_inputs(), PARAMS)
    scaled = decide(
        _inputs(spot=7706.035 * 1.3, short_strike=7625.0 * 1.3, width=5.0),
        DeferralParams(10.0, 2.33, 2.93, buffer_points=5.0 * 1.3),
    )
    assert scaled.z == pytest.approx(base.z, rel=1e-12)


def test_higher_volatility_or_more_time_means_closer_in_sigma():
    base = decide(_inputs(), PARAMS).z
    assert decide(_inputs(sigma_implied=0.40), PARAMS).z < base
    assert decide(_inputs(minutes_to_close=120.0), PARAMS).z < base
    # sigma is the MAX of the two: a realized spike above implied governs.
    assert decide(_inputs(sigma_realized=0.50), PARAMS).z < base


def test_the_call_side_mirrors_the_put_side():
    put = standardized_distance(side="put", spot=7700.0, barrier=7600.0, sigma=0.2, minutes_to_close=30.0)
    call = standardized_distance(side="call", spot=7600.0, barrier=7700.0, sigma=0.2, minutes_to_close=30.0)
    assert put == pytest.approx(call)
    assert barrier_for("put", 7625.0, 5.0) == 7630.0
    assert barrier_for("call", 7760.0, 5.0) == 7755.0


def test_no_time_left_is_an_infinite_distance_and_defers():
    d = decide(_inputs(minutes_to_close=0.0), PARAMS)
    assert d.defer and math.isinf(d.z) and d.p_touch == 0.0
    # And the payload survives it: `inf` is not a legal Tier 1 number.
    assert d.to_payload()["z"] is None


def test_touch_probability_is_the_anchored_pareto_tail():
    assert touch_probability(2.0, tail_z99=2.33, tail_alpha=2.93) is None
    assert touch_probability(2.33, tail_z99=2.33, tail_alpha=2.93) == pytest.approx(0.01)
    values = [touch_probability(z, tail_z99=2.33, tail_alpha=2.93) for z in (3.0, 4.0, 5.0, 6.0)]
    assert values == sorted(values, reverse=True)


def test_the_boundary_and_the_decision_agree():
    """Two independent implementations of one rule — the inequality and its solved
    form — pinned against each other."""
    for width in (5.0, 10.0):
        z_star = deferral_boundary(k=10.0, width=width, tail_z99=2.33, tail_alpha=2.93)
        p_at = touch_probability(z_star, tail_z99=2.33, tail_alpha=2.93)
        assert 10.0 * p_at * width == pytest.approx(0.05)
        for z, defer in ((z_star * 0.999, False), (z_star * 1.001, True)):
            spot = 7630.0 * math.exp(z * 0.20 * math.sqrt(30.0 / fd.MINUTES_PER_YEAR))
            d = decide(
                _inputs(spot=spot, width=width, sigma_implied=0.20, sigma_realized=0.10),
                PARAMS,
            )
            assert d.defer is defer, (width, z, d)


def test_the_boundary_numbers_in_the_design():
    """§4.10: k=10 on a 5-wide is z >= 5.12; a 10-wide needs 2^(1/α) more."""
    b5 = deferral_boundary(k=10.0, width=5.0, tail_z99=2.33, tail_alpha=2.93)
    b10 = deferral_boundary(k=10.0, width=10.0, tail_z99=2.33, tail_alpha=2.93)
    assert b5 == pytest.approx(5.12, abs=0.01)
    assert b10 / b5 == pytest.approx(2 ** (1 / 2.93))
    assert deferral_boundary(k=5.0, width=5.0, tail_z99=2.33, tail_alpha=2.93) < b5
    assert deferral_boundary(k=20.0, width=5.0, tail_z99=2.33, tail_alpha=2.93) > b5


def test_raising_k_can_only_defer_less():
    """The safe-direction claim behind "raising k needs no analysis" (§4.9)."""
    rng = random.Random(7)
    for _ in range(500):
        inputs = _inputs(
            spot=7630.0 + rng.uniform(1.0, 200.0),
            sigma_implied=rng.uniform(0.05, 0.6),
            sigma_realized=rng.uniform(0.0, 0.6),
            minutes_to_close=rng.uniform(0.0, 30.0),
        )
        low = decide(inputs, DeferralParams(5.0, 2.33, 2.93, 5.0)).defer
        high = decide(inputs, DeferralParams(20.0, 2.33, 2.93, 5.0)).defer
        assert not (high and not low)


def test_decide_never_raises_on_hostile_numbers():
    hostile = [None, float("nan"), float("inf"), float("-inf"), 0.0, -1.0, 1e308]
    rng = random.Random(3)
    for _ in range(2000):
        pick = lambda: rng.choice(hostile + [rng.uniform(0.01, 9000.0)])  # noqa: E731
        decide(
            DeferralInputs(
                side=rng.choice(["put", "call", None]),
                spot=pick(), short_strike=pick(), width=pick(),
                sigma_implied=pick(), sigma_realized=pick(),
                minutes_to_close=rng.choice(
                    [-5.0, 0.0, 12.0, 30.0, float("nan"), float("inf"), float("-inf")]
                ),
            ),
            PARAMS,
        )


@pytest.mark.parametrize("minutes", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_time_to_close_refuses(minutes):
    """Review fix (2026-09-25): a NaN `minutes_to_close` used to DEFER — `z` came out
    NaN, and every refusal test was spelled so that NaN passed it. With spot one
    point from the barrier."""
    d = decide(_inputs(spot=7631.0, minutes_to_close=minutes), PARAMS)
    assert not d.defer and d.reason == fd.NO_TIME


def test_every_refusal_is_spelled_so_that_nan_refuses():
    """A NaN reaching any comparison in the arithmetic refuses rather than defers —
    checked by driving NaN through each numeric the arithmetic compares."""
    assert touch_probability(float("nan"), tail_z99=2.33, tail_alpha=2.93) is None
    nan_buffer = DeferralParams(10.0, 2.33, 2.93, float("nan"))
    assert not decide(_inputs(), nan_buffer).defer
    nan_k = DeferralParams(float("nan"), 2.33, 2.93, 5.0)
    assert not decide(_inputs(), nan_k).defer


def test_the_modes_agree_with_the_loader():
    assert {m.value for m in DeferralMode} == set(FLATTEN_DEFERRAL_MODES)


# -- config --------------------------------------------------------------------


_KEYS = (
    "flatten_deferral",
    "flatten_defer_insurance_multiple",
    "flatten_defer_tail_z99",
    "flatten_defer_tail_alpha",
    "flatten_defer_source",
)


@pytest.mark.parametrize("key", _KEYS)
def test_every_key_is_required(key):
    from argus.config import Config
    from tests.support import config_dict

    raw = config_dict()
    raw["watchdog"].pop(key)
    with pytest.raises(ConfigError, match=key):
        Config.from_dict(raw)
    # And the suite's own default is OFF, so no pre-existing test changed behaviour.
    assert make_config().watchdog.flatten_deferral == "off"


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"flatten_deferral": "maybe"}, "flatten_deferral"),
        ({"flatten_defer_insurance_multiple": 0.5}, ">= 1"),
        ({"flatten_defer_insurance_multiple": float("nan")}, "finite"),
        ({"flatten_defer_insurance_multiple": float("inf")}, "finite"),
        ({"flatten_defer_tail_z99": 0.0}, "> 0"),
        ({"flatten_defer_tail_alpha": 3.5}, "inverse cubic"),
        ({"flatten_defer_tail_alpha": 1.0}, "inverse cubic"),
        ({"flatten_defer_source": "   "}, "provenance"),
    ],
)
def test_every_bad_value_is_refused(overrides, match):
    with pytest.raises(ConfigError, match=match):
        make_config(watchdog=overrides)


def test_alpha_may_equal_the_cap():
    make_config(watchdog={"flatten_defer_tail_alpha": MAX_FLATTEN_DEFER_TAIL_ALPHA})


def test_on_requires_the_welded_size_invariant_not_a_single_position():
    """§4.5 condition 6, as amended 2026-09-25. What a second position changes is how
    many DOLLARS one barrier event can cost across the deferred positions, so the
    loader bounds the dollars — `max_concurrent_positions × max_loss_per_position ≤
    daily_max_loss` — and never the count. 2 spreads × 475 is the same worst event as
    1 × 950; the first cut's `== 1` refused exactly that move."""
    two_at_475 = {"daily_max_loss": 950.0, "max_concurrent_positions": 2,
                  "max_loss_per_position": 475.0}
    config = make_config(watchdog={"flatten_deferral": "on"}, risk=two_at_475)
    assert config.risk.open_risk_is_welded
    make_config(watchdog={"flatten_deferral": "on"},
                risk={**two_at_475, "max_concurrent_positions": 1,
                      "max_loss_per_position": 950.0})
    # One dollar past the budget refuses, at any count.
    for over in ({**two_at_475, "max_loss_per_position": 476.0},
                 {**two_at_475, "max_concurrent_positions": 3}):
        with pytest.raises(ConfigError, match="welded size invariant"):
            make_config(watchdog={"flatten_deferral": "on"}, risk=over)
        # `record` and `off` act on nothing, so they are not refused.
        make_config(watchdog={"flatten_deferral": "record"}, risk=over)
        make_config(watchdog={"flatten_deferral": "off"}, risk=over)


def test_the_watchdog_reads_the_loaders_own_predicate():
    """Condition 6 at runtime is the SAME `RiskConfig.open_risk_is_welded`, not a copy
    that could drift from the loader's."""
    source = (_SRC / "execution" / "watchdog.py").read_text()
    assert "self._config.risk.open_risk_is_welded" in source
    assert "max_concurrent_positions > 1" not in source


def test_the_three_profiles_ship_on_with_the_calibrated_values():
    from argus.config import Config

    root = _SRC.parents[1]
    for name in ("argus.live.toml", "argus.sandbox.toml", "argus.dev.toml"):
        wd = Config.load(root / name).watchdog
        assert wd.flatten_deferral == "on", name
        # k = 2.0 (was 10.0) and the 2026-09-29 recalibration (2.33 / 2.93, then
        # 2.38 / 2.80): docs/decisions/2026-09-28-flatten-deferral-k-2.md
        assert wd.flatten_defer_insurance_multiple == 2.0, name
        assert (wd.flatten_defer_tail_z99, wd.flatten_defer_tail_alpha) == (2.60, 2.72), name
        assert "flatten_calibration" in wd.flatten_defer_source, name



# -- structural pins -----------------------------------------------------------


def test_the_pure_core_imports_no_io_and_reads_no_config():
    tree = ast.parse((_SRC / "execution" / "flatten_deferral.py").read_text())
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    for banned in ("argus.config", "sqlite3", "asyncio", "argus.audit", "time", "os"):
        assert banned not in imported, banned
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "Config" not in names


def test_the_basis_is_imported_from_regime_never_redefined():
    source = (_SRC / "execution" / "flatten_deferral.py").read_text()
    assert "from argus.signal.regime import MINUTES_PER_YEAR" in source
    tree = ast.parse(source)
    assigned = {
        t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    assert "MINUTES_PER_YEAR" not in assigned


def _method(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def test_the_deferral_reads_no_leg_price_and_no_signal_loop():
    """Quiet legs cannot influence it (§4.2): the decision is spot + strike + clock +
    vol. And it reads the chain SOURCE, never `SignalLoop` state (spec §4)."""
    wd_path = _SRC / "execution" / "watchdog.py"
    for name in ("_deferral_ineligible", "_decide_deferral", "_apply_flatten_deferral",
                 "_deferral_vols"):
        node = _method(wd_path, name)
        attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        assert not attrs & {"bid", "ask", "cost_to_close", "crossing_debit", "market"}, name
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        assert "SignalLoop" not in names and "orchestrator" not in names, name


def test_the_per_tick_decision_field_has_one_builder():
    """(2026-09-28, P11) `watchdog.check` and `watchdog.triggered*` carry the decision
    under one key, spelled in one place: the literal appears once in the watchdog,
    inside `_deferral_field`, and both record sites build it there."""
    wd_path = _SRC / "execution" / "watchdog.py"

    def literals(node: ast.AST) -> int:
        return sum(
            1 for n in ast.walk(node)
            if isinstance(n, ast.Constant) and n.value == "flatten_deferral"
        )

    assert literals(ast.parse(wd_path.read_text())) == 1
    assert literals(_method(wd_path, "_deferral_field")) == 1
    for name in ("_check_one", "_trigger"):
        calls = [
            n for n in ast.walk(_method(wd_path, name))
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_deferral_field"
        ]
        assert calls, f"{name} must build the field through _deferral_field"


def test_the_deferral_only_ever_removes_the_flatten():
    """The one place the fired set is changed returns `fired - {FLATTEN}` or `fired`."""
    node = _method(_SRC / "execution" / "watchdog.py", "_apply_flatten_deferral")
    returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
    rendered = {ast.unparse(r.value) for r in returns}
    assert rendered == {
        "(fired, None)",
        "(fired - {TriggerReason.FLATTEN_BEFORE_CLOSE}, decision)",
        "(fired, decision)",
    }, rendered


def test_the_deferral_is_never_applied_to_a_spent_position():
    """The flatten's one-shot override and the stub continuation are out of scope by
    construction (hard constraint 1)."""
    node = _method(_SRC / "execution" / "watchdog.py", "_check_one")
    calls = [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_apply_flatten_deferral"
    ]
    assert len(calls) == 1, "the deferral has exactly one call site"
    guards = [
        n for n in ast.walk(node)
        if isinstance(n, ast.If) and ast.unparse(n.test) == "key not in self._attempted"
        and any(c in ast.walk(n) for c in calls)
    ]
    assert guards, "the one call must sit under `if key not in self._attempted:`"


# -- the calibration tool's core ------------------------------------------------


def test_hill_recovers_a_known_pareto_tail():
    from argus.tools.flatten_calibration import hill_alpha

    rng = random.Random(11)
    sample = [rng.paretovariate(3.0) for _ in range(40_000)]
    assert hill_alpha(sample, fraction=0.05) == pytest.approx(3.0, rel=0.1)


def test_a_brownian_path_reproduces_the_gaussian_touch_probability():
    """The standardization is right: driftless GBM sampled at the recorded 5-second
    cadence gives P(max excursion ≥ z) ≈ 2Φ(−z) (a little under, from discrete
    sampling), so the tool is measuring the quantity the model is about."""
    from argus.clock import ET
    from argus.tools.flatten_calibration import Session, pooled, session_excursions

    rng = random.Random(5)
    sigma = 0.20
    dt_min = 5.0 / 60.0
    step_sd = sigma * math.sqrt(dt_min / fd.MINUTES_PER_YEAR)
    sessions = []
    for d in range(12):
        day = date(2026, 8, 3) + timedelta(days=d)
        at = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
        close = datetime(day.year, day.month, day.day, 16, 0, tzinfo=ET)
        price, path = 7000.0, []
        while at <= close:
            path.append((at, price))
            price *= math.exp(rng.gauss(0.0, step_sd))
            at += timedelta(seconds=5)
        iv = tuple((t, sigma) for t, _ in path[::12])
        sessions.append(Session(day=day, path=tuple(path), iv=iv, close=close))
    values = pooled(e for s in sessions for e in session_excursions(s, 30))
    for z, target in ((1.0, 2 * 0.1587), (2.0, 2 * 0.0228)):
        observed = sum(v >= z for v in values) / len(values)
        assert 0.6 * target < observed < 1.1 * target, (z, observed, target)


def test_the_recommended_alpha_is_never_thinner_than_three():
    from argus.tools.flatten_calibration import recommended_alpha

    assert recommended_alpha((3.4, 6.0), 4.0) == 3.0
    assert recommended_alpha((2.93, 6.3), 3.9) == 2.93
    assert recommended_alpha(None, 3.5) == 3.0
    assert recommended_alpha(None, None) == 3.0


def test_event_days_are_read_from_the_log_itself(tmp_path):
    import json as _json
    import sqlite3

    from argus.tools.flatten_calibration import read_event_days

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE audit_log (seq INTEGER, ts TEXT, category TEXT, payload TEXT)")
    rows = [
        (1, "2026-09-16T14:00:00.000000Z", "signal.none", {"reason": "event_blackout"}),
        (2, "2026-09-17T14:00:00.000000Z", "signal.none", {"reason": "no_candidate"}),
    ]
    for seq, ts, cat, payload in rows:
        conn.execute("INSERT INTO audit_log VALUES (?,?,?,?)", (seq, ts, cat, _json.dumps(payload)))
    conn.commit()
    conn.close()
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert read_event_days(ro, "2026-09-15T00:00:00Z", "2026-09-19T00:00:00Z") == {
            date(2026, 9, 16)
        }
    finally:
        ro.close()


def test_release_days_are_excluded_whether_or_not_the_log_blacked_them_out():
    """Rule 1 (2026-09-29): from 2026-09-30 a CPI/NFP/PCE day is traded and logs no
    `event_blackout`, and it must still be out; a day with neither source stays in."""
    from argus.tools.flatten_calibration import excluded_days

    log = {date(2026, 9, 11), date(2026, 9, 16)}  # CPI while blacked out; FOMC
    releases = {
        "CPI": [date(2026, 9, 11), date(2026, 10, 14)],
        "NFP": [date(2026, 10, 2)],
        "PCE": [date(2026, 9, 30)],
    }
    days = [date(2026, 9, 11), date(2026, 9, 16), date(2026, 9, 29), date(2026, 9, 30),
            date(2026, 10, 2)]
    assert excluded_days(days, log, releases) == {
        date(2026, 9, 11): ("event_blackout", "CPI"),
        date(2026, 9, 16): ("event_blackout",),
        date(2026, 9, 30): ("PCE",),
        date(2026, 10, 2): ("NFP",),
    }


def test_an_empty_release_family_over_a_real_span_is_reported():
    from argus.tools.flatten_calibration import empty_release_families

    start, end = date(2026, 8, 5), date(2026, 9, 28)
    full = {"CPI": [date(2026, 8, 12)], "NFP": [date(2026, 8, 7)], "PCE": [date(2026, 8, 26)]}
    assert empty_release_families(full, start, end) == []
    assert empty_release_families({**full, "NFP": [], "CPI": []}, start, end) == ["CPI", "NFP"]
    # Twelve days can legitimately hold no NFP.
    assert empty_release_families({**full, "NFP": []}, date(2026, 9, 14), date(2026, 9, 25)) == []


def test_release_days_come_from_the_blackout_generators_fetcher(monkeypatch):
    """One source for the calibration and the blackout file: the same fetcher and ids,
    and no key is a `BlackoutError` (which `main` turns into a refusal), never a guess."""
    from argus.tools import blackout_dates as bd
    from argus.tools import flatten_calibration as fc

    seen: list[int] = []

    def fake(release_id, *, api_key, start, end):
        seen.append(release_id)
        return []

    monkeypatch.setattr(fc, "fetch_release_dates", fake)
    monkeypatch.setenv("ARGUS_FRED_API_KEY", "test-key")
    assert set(fc.fetch_release_days(date(2026, 8, 5), date(2026, 9, 28))) == set(
        bd.FRED_RELEASE_IDS
    )
    assert sorted(seen) == sorted(bd.FRED_RELEASE_IDS.values())
    monkeypatch.delenv("ARGUS_FRED_API_KEY")
    with pytest.raises(bd.BlackoutError):
        fc.fetch_release_days(date(2026, 8, 5), date(2026, 9, 28))
