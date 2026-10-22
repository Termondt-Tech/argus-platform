"""The realized-volatility estimator.

Tested by RECOVERY of a known vol, never against golden numbers — the rule
`test_regime.py` states and for the same reason: a golden number pins whatever the
code did the day it was written, sign errors and all. `signal/realized.py` exists
to be divided into `signal/regime.py`'s `atm_iv`, so the properties that matter are
that it recovers the vol it was given, that it shares the annualization basis, and
that it refuses rather than guesses on a path it cannot measure.
"""

from __future__ import annotations

import ast
import math
import random
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from argus.clock import ET
from argus.signal import realized as realized_module
from argus.signal.realized import (
    GRID_SECONDS,
    MIN_COVERAGE,
    MIN_SPAN_FRACTION,
    WINDOW_START_ET,
    realized_vol,
)
from argus.signal.regime import MINUTES_PER_YEAR

TRADING_DATE = datetime(2026, 9, 8).date()
OPEN_ET = datetime.combine(TRADING_DATE, WINDOW_START_ET, tzinfo=ET)
CLOSE_ET = datetime.combine(TRADING_DATE, time(16, 0), tzinfo=ET)
HALF_DAY_CLOSE_ET = datetime.combine(TRADING_DATE, time(13, 0), tzinfo=ET)


def _path(
    *,
    sigma: float = 0.20,
    spot: float = 6800.0,
    seed: int = 1,
    step_s: float = 5.0,
    start: datetime = OPEN_ET,
    end: datetime = CLOSE_ET,
    drop: float = 0.0,
) -> list[tuple[datetime, float]]:
    """A geometric-Brownian path at a known annualized vol, on the same 365-day
    basis the estimator uses. `drop` randomly removes that fraction of ticks, which
    is how a Tier 2 queue-full burst presents in the recorded log."""
    rnd = random.Random(seed)
    per_step = sigma * math.sqrt(step_s / 60.0 / MINUTES_PER_YEAR)
    out: list[tuple[datetime, float]] = []
    at, price = start, spot
    while at <= end:
        if drop == 0.0 or rnd.random() >= drop:
            out.append((at, price))
        price *= math.exp(rnd.gauss(0.0, per_step))
        at += timedelta(seconds=step_s)
    return out


def _rv(path, **kw):
    return realized_vol(path, window_start=OPEN_ET, window_end=CLOSE_ET, **kw)


# -- recovery -----------------------------------------------------------------


@pytest.mark.parametrize("sigma", [0.08, 0.15, 0.30])
def test_recovers_a_known_vol(sigma):
    """Averaged over paths, because one path's realized vol is a draw and not the
    parameter — the estimator is unbiased, not clairvoyant."""
    got = [_rv(_path(sigma=sigma, seed=s)).annualized for s in range(24)]
    assert all(v is not None for v in got)
    assert sum(got) / len(got) == pytest.approx(sigma, rel=0.05)


def test_a_higher_true_vol_reads_higher():
    quiet = sum(_rv(_path(sigma=0.10, seed=s)).annualized for s in range(12))
    busy = sum(_rv(_path(sigma=0.25, seed=s)).annualized for s in range(12))
    assert busy > quiet


def test_the_grid_does_not_move_the_answer_on_a_clean_path():
    """The property the volatility signature plot relies on. SPX is a computed
    index, so there is no bid-ask bounce to inflate the high-frequency end."""
    path = _path(sigma=0.20, seed=7)
    got = [_rv(path, grid_seconds=g).annualized for g in (15.0, 30.0, 60.0, 120.0, 300.0)]
    assert all(v is not None for v in got)
    assert max(got) / min(got) < 1.15, got


def test_annualization_uses_the_sampled_span_not_the_requested_window():
    """A path covering only part of the window must not be annualized over the
    whole of it — that would understate every short session instead of refusing
    it. Checked by giving a HALF-DAY window a half-day path: the same true vol
    must come back, not one scaled by the missing hours."""
    path = _path(sigma=0.20, seed=3, end=HALF_DAY_CLOSE_ET)
    got = realized_vol(path, window_start=OPEN_ET, window_end=HALF_DAY_CLOSE_ET)
    assert got.ok and got.annualized == pytest.approx(0.20, rel=0.25)


# -- refusals -----------------------------------------------------------------


def test_a_truncated_session_refuses_as_span_too_short():
    """2026-08-17's shape: the path stops an hour in. The span is what is wrong,
    and the grid inside that hour may be perfectly dense."""
    path = _path(seed=5, end=OPEN_ET + timedelta(minutes=58))
    got = _rv(path)
    assert got.refusal == "span_too_short"
    assert got.annualized is None
    assert got.span_minutes < got.window_minutes * MIN_SPAN_FRACTION


def test_a_holey_session_refuses_as_coverage_too_low():
    """2026-09-03's shape, and the reason coverage is a SEPARATE test from span:
    the process halted at 11:04 and was restarted by hand at 15:37, so the path
    spans the whole window and is missing three quarters of it. A span test alone
    passes this, which is exactly the silently-wrong number the gate exists for."""
    morning = _path(seed=5, end=OPEN_ET + timedelta(minutes=64))
    evening = _path(seed=6, start=CLOSE_ET - timedelta(minutes=22), end=CLOSE_ET)
    got = _rv(morning + evening)
    assert got.refusal == "coverage_too_low"
    assert got.span_minutes > got.window_minutes * MIN_SPAN_FRACTION
    assert got.coverage < MIN_COVERAGE


def test_scattered_drops_are_tolerated_and_do_not_move_the_answer():
    """Tier 2 telemetry is droppable under burst (`AuditLog.telemetry` returns
    False on a full queue), so the recorded path is expected to be sparser than
    the cadence. Quadratic variation tolerates that; it is the truncation above
    that it cannot."""
    got = _rv(_path(sigma=0.20, seed=9, drop=0.5))
    assert got.ok and got.coverage >= MIN_COVERAGE
    assert got.annualized == pytest.approx(0.20, rel=0.35)


@pytest.mark.parametrize(
    "path, expected",
    [
        ([], "too_few_samples"),
        ([(OPEN_ET, 6800.0)], "too_few_samples"),
        # Every price is unusable, so nothing survives the filter.
        ([(OPEN_ET + timedelta(minutes=i), -1.0) for i in range(40)], "too_few_samples"),
        ([(OPEN_ET + timedelta(minutes=i), math.nan) for i in range(40)], "too_few_samples"),
    ],
)
def test_unusable_paths_refuse_rather_than_raise(path, expected):
    assert _rv(path).refusal == expected


def test_an_empty_window_refuses():
    assert realized_vol(
        _path(), window_start=CLOSE_ET, window_end=OPEN_ET
    ).refusal == "empty_window"


def test_ticks_outside_the_window_are_ignored():
    """The window is the comparability contract — a session that happened to log
    from 09:40 must not contribute pre-10:00 returns that other sessions lack."""
    inside = _path(sigma=0.20, seed=11)
    outside = _path(sigma=2.0, seed=12, start=OPEN_ET - timedelta(minutes=20), end=OPEN_ET - timedelta(seconds=5))
    assert _rv(inside + outside).annualized == pytest.approx(_rv(inside).annualized)


def test_a_half_day_window_is_not_a_truncated_full_day():
    """`MIN_SPAN_FRACTION` is a fraction of the REQUESTED window, not an absolute
    number of minutes, so an early-closing session is short rather than broken.
    A literal minimum would refuse every half-day."""
    path = _path(seed=13, end=HALF_DAY_CLOSE_ET)
    assert realized_vol(path, window_start=OPEN_ET, window_end=HALF_DAY_CLOSE_ET).ok


# -- the shared basis ---------------------------------------------------------


def test_the_annualization_basis_is_imported_and_never_redefined():
    """Two copies of an annualization basis is how IV/RV silently stops meaning
    anything. The one number anybody wants out of this module is a ratio against
    `regime.atm_iv`, so the basis must be THE basis — the same rule
    `execution/zerobid.py` enforces for the tick floor, held structurally rather
    than by comment."""
    source = Path(realized_module.__file__).read_text()
    tree = ast.parse(source)

    imported = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "argus.signal.regime"
        and any(a.name == "MINUTES_PER_YEAR" for a in node.names)
    ]
    assert imported, "realized.py must import MINUTES_PER_YEAR from regime.py"

    assigned = [
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "MINUTES_PER_YEAR"
    ]
    assert not assigned, "realized.py must not define its own MINUTES_PER_YEAR"


def test_the_window_start_is_not_read_from_config():
    """`WINDOW_START_ET` deliberately does not read `signal.entry_earliest`, even
    though the two currently hold the same value: that key can move, and the
    series would silently redefine itself with nothing saying so.

    Asserted over the AST rather than the source text, so the comment that
    EXPLAINS the rule does not trip the test enforcing it — the module is
    config-free by construction, and `entry_earliest` may appear in prose.
    """
    tree = ast.parse(Path(realized_module.__file__).read_text())

    config_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("argus.config")
    ]
    assert not config_imports, "realized.py must stay pure — no config import"

    referenced = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert "entry_earliest" not in referenced

    assert WINDOW_START_ET == time(10, 0)
    assert GRID_SECONDS == 60.0
