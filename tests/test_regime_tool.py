"""The read-only regime telemetry reader.

`signal/regime.py` has logged `atm_iv`/`skew` on every tick since 2026-08-12 so a
threshold could be fit to the recorded distribution; nothing read them back until
this tool. The tests below pin the four rules that make the reading correct rather
than merely present — the unit is a DAY, the clock is FIXED, blackout days are out,
and the comparable skew is the normalized one — plus the failure taxonomy, which
is the part that has to work before any distribution means anything.

Records are written through the REAL `RegimeSnapshot.to_payload` and the real
`SignalLoop._regime_payload`, never hand-built dicts. This tool reads a payload by
key, which is precisely the seam that went wrong in
`docs/decisions/2026-09-02-a-status-seam-drops-what-its-source-gained.md`: a
reader that names its source's fields cannot fail loudly when the source changes,
so it is pinned against the source instead of against a literal.
"""

from __future__ import annotations

import math
from datetime import date, time

import pytest

from argus.marketcalendar import MarketCalendar
from argus.signal.realized import RealizedVol
from argus.signal.strategy import RegimeSnapshot
from argus.tools.regime import (
    CANDIDATE,
    GUARD_MINUTES,
    NONE,
    Reading,
    _classify,
    _pct,
    _utc_window,
    daily_reading,
    iv_over_rv,
    read_builds,
    read_prices,
    read_readings,
    session_window,
)

TRADING_DATE = date(2026, 9, 8)
WINDOW = _utc_window(TRADING_DATE, TRADING_DATE)


def _ts(hh: int, mm: int, ss: float = 0.0) -> str:
    """UTC. 14:00 UTC is 10:00 ET — the fixed clock every series here uses."""
    return f"2026-09-08T{hh:02d}:{mm:02d}:{ss:06.3f}000Z"


def _snapshot(
    atm_iv: float = 0.14,
    skew: float = -0.0011,
    minutes: float = 360.0,
    today: date = TRADING_DATE,
) -> RegimeSnapshot:
    return RegimeSnapshot(atm_iv=atm_iv, skew=skew, today=today, minutes_to_settlement=minutes)


@pytest.fixture
async def running(audit):
    """`signal.none` is Tier 2 — a queued record that never lands is a reading this
    tool cannot see, so the consumer has to be alive."""
    audit.start()
    yield audit
    await audit.aclose()


async def _tick(
    audit,
    ts: str,
    snapshot: RegimeSnapshot,
    *,
    candidate: bool = False,
    price: float | None = None,
) -> None:
    """One per-tick record carrying a regime block, shaped as `orchestrator` shapes it.

    `regime_bucket` is None because `[regime]` is absent — the same None the
    orchestrator emits rather than a guessed tag. `price` mirrors
    `underlying_price`, which the orchestrator attaches to both categories.
    """
    audit._clock = lambda: ts
    payload: dict = {"regime": {**snapshot.to_payload(), "regime_bucket": None}}
    if price is not None:
        payload["underlying_price"] = price
    if candidate:
        await audit.record(CANDIDATE, {**payload, "contracts": 2})
    else:
        audit.telemetry(NONE, {**payload, "reason": "no_short_leg_with_fresh_delta"})
        await audit._queue.join()


# -- reading the log ----------------------------------------------------------


async def test_reads_both_tick_categories(db, running):
    """Tier 1 candidates and Tier 2 no-candidates both carry a regime block, and
    sampling only the candidate ones would condition the distribution on the
    conditions the current thresholds already like."""
    await _tick(running, _ts(14, 0), _snapshot(atm_iv=0.13))
    await _tick(running, _ts(14, 0, 5), _snapshot(atm_iv=0.15), candidate=True)

    readings = read_readings(db, *WINDOW)
    assert [r.category for r in readings] == [NONE, CANDIDATE]
    assert [r.atm_iv for r in readings] == [0.13, 0.15]


async def test_trading_date_comes_from_the_payload_not_the_timestamp(db, running):
    """The process stamped `today` itself from `clock.et_today`, so it is what the
    blackout check consulted. Deriving the session from `ts` instead would let the
    two disagree at exactly the UTC/ET boundary."""
    await _tick(running, _ts(14, 0), _snapshot(today=date(2026, 9, 8)))

    [reading] = read_readings(db, *WINDOW)
    assert reading.trading_date == date(2026, 9, 8)


async def test_records_without_a_regime_block_are_skipped_not_counted_as_failures(db, running):
    """A record predating 2026-08-12. The estimator did not decline — it did not
    exist — so counting it as a NaN would put a fabricated failure rate in the one
    table that decides whether the estimator works at all."""
    running._clock = lambda: _ts(14, 0)
    running.telemetry(NONE, {"reason": "no_tradeable_legs", "underlying_price": 6800.0})
    await running._queue.join()
    await _tick(running, _ts(14, 0, 5), _snapshot())

    assert len(read_readings(db, *WINDOW)) == 1


async def test_build_boundaries_are_readable(db, running):
    """There is no pid column; a build change is visible only as `process.start`
    carrying a different version, and a series spanning two of them is two code
    versions in one distribution."""
    running._clock = lambda: _ts(13, 30)
    await running.record("process.start", {"environment": "sandbox", "version": "v0.3.17"})

    assert [v for _, v in read_builds(db, *WINDOW)] == ["v0.3.17"]


# -- the failure taxonomy -----------------------------------------------------


def _reading(atm_iv: float | None, skew: float | None, minutes: float | None) -> Reading:
    from datetime import datetime, timezone

    return Reading(
        seq=1,
        at_utc=datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc),
        trading_date=TRADING_DATE,
        atm_iv=atm_iv,
        skew=skew,
        minutes=minutes,
        bucket=None,
        category=NONE,
    )


@pytest.mark.parametrize(
    "atm_iv, skew, minutes, expected",
    [
        (0.14, -0.001, 360.0, "estimated"),
        # Both NaN above the guard: an early return — no forward, no K0, or a wing
        # under two strikes. A FEED problem.
        (None, None, 360.0, "no_strip"),
        # Both NaN below the guard: the 2/T arithmetic bound, working as designed.
        (None, None, GUARD_MINUTES - 1.0, "guard"),
        # `skew` survives a non-positive total variance and `atm_iv` does not —
        # which is the only way to tell a crossed/stale quote from a thin strip,
        # and it needs no code change to observe.
        (None, -0.001, 360.0, "negative"),
    ],
)
def test_failure_modes_are_distinguishable_from_the_payload_alone(atm_iv, skew, minutes, expected):
    assert _classify(_reading(atm_iv, skew, minutes)) == expected


def test_the_negative_variance_path_is_reachable_from_the_estimator():
    """Pins the premise the `negative` classification rests on: `compute_regime`
    really can return a NaN `atm_iv` alongside a finite `skew`. If it ever stops
    doing so, the taxonomy above has a branch that describes nothing."""
    snapshot = RegimeSnapshot(atm_iv=math.nan, skew=-0.001, today=TRADING_DATE)
    payload = snapshot.to_payload()

    assert payload["atm_iv"] is None
    assert payload["skew"] == -0.001


# -- rule 2: the clock is fixed ----------------------------------------------


async def test_the_daily_reading_is_the_one_nearest_the_fixed_clock(db, running):
    await _tick(running, _ts(13, 55), _snapshot(atm_iv=0.11))  # 09:55 ET
    await _tick(running, _ts(14, 1), _snapshot(atm_iv=0.12))  # 10:01 ET
    await _tick(running, _ts(14, 30), _snapshot(atm_iv=0.13))  # 10:30 ET

    reading = daily_reading(read_readings(db, *WINDOW), time(10, 0), 300.0)
    assert reading is not None and reading.atm_iv == 0.12


async def test_a_day_with_nothing_near_the_clock_contributes_nothing(db, running):
    """Silently taking the next available reading would put an 11:30 observation in
    a column labelled 10:00, which is the pooling this whole tool exists to avoid."""
    await _tick(running, _ts(15, 30), _snapshot(atm_iv=0.13))  # 11:30 ET

    assert daily_reading(read_readings(db, *WINDOW), time(10, 0), 300.0) is None


async def test_an_unestimable_reading_never_represents_the_day(db, running):
    """A NaN at 10:00 is a fact about the FEED at that instant, not a level. Taking
    it would put a hole in the series that a percentile cannot see."""
    await _tick(running, _ts(14, 0), _snapshot(atm_iv=math.nan, skew=math.nan))
    await _tick(running, _ts(14, 0, 30), _snapshot(atm_iv=0.13))

    reading = daily_reading(read_readings(db, *WINDOW), time(10, 0), 300.0)
    assert reading is not None and reading.atm_iv == 0.13


# -- rule 4: the comparable skew is the normalized one ------------------------


def test_skew_ratio_is_stable_across_vol_levels_where_raw_skew_is_not():
    """Pins the MECHANISM, which is real — and deliberately not a recommendation.

    Two days with the SAME surface asymmetry and different vol levels: the raw
    difference moves with the level, the ratio does not.

    Measured on 22 real sessions this effect turns out to be NEGLIGIBLE, because
    the level barely moves (0.117-0.287) while the surface shape flips sign — the
    two columns come out equally variable (cv 1.57 vs 1.59) and neither correlates
    much with `atm_iv`. So the tool reports both and prefers neither, and
    `RegimeConfig`'s raw-valued keys need no redefinition. Do not read this test as
    an argument for normalizing the config's thresholds; it is the calibration
    against which that real-data finding was judged surprising.
    """
    quiet = _reading(0.10, -0.055 * 0.10**2, 360.0)
    busy = _reading(0.30, -0.055 * 0.30**2, 360.0)

    assert busy.skew is not None and quiet.skew is not None
    assert abs(busy.skew) > 8 * abs(quiet.skew)
    assert quiet.skew_ratio == pytest.approx(busy.skew_ratio)


def test_skew_ratio_declines_a_non_positive_iv():
    """No denominator, no ratio — and never a division that turns an unestimable
    reading into a very large number."""
    assert _reading(None, -0.001, 360.0).skew_ratio is None
    assert _reading(0.0, -0.001, 360.0).skew_ratio is None


# -- percentiles --------------------------------------------------------------


# -- the price path and realized vol ------------------------------------------


async def test_the_price_path_is_read_from_both_categories(db, running):
    await _tick(running, _ts(14, 0), _snapshot(), price=6800.0)
    await _tick(running, _ts(14, 0, 5), _snapshot(), candidate=True, price=6801.5)

    prices = read_prices(db, *WINDOW)
    assert [p for _at, p in prices[TRADING_DATE]] == [6800.0, 6801.5]


async def test_the_path_is_keyed_off_the_TIMESTAMP_not_the_payload_date(db, running):
    """The opposite of `read_readings`, deliberately. `underlying_price` predates
    the regime block, so a payload `today` is unavailable for most of the
    backfill — and a tick's ET date is unambiguous on its own, because a session
    crosses no midnight. Keying on `today` here would silently drop every
    pre-2026-08-12 session, which is most of the history this reads."""
    running._clock = lambda: _ts(14, 0)
    running.telemetry(NONE, {"reason": "no_tradeable_legs", "underlying_price": 6800.0})
    await running._queue.join()

    [(at, price)] = read_prices(db, *WINDOW)[TRADING_DATE]
    assert price == 6800.0
    assert at.date() == TRADING_DATE


async def test_unusable_prices_are_dropped_by_the_reader(db, running):
    running._clock = lambda: _ts(14, 0)
    running.telemetry(NONE, {"reason": "no_chain", "underlying_price": None})
    await running._queue.join()
    running._clock = lambda: _ts(14, 0, 5)
    running.telemetry(NONE, {"reason": "x", "underlying_price": -1.0})
    await running._queue.join()
    await _tick(running, _ts(14, 0, 10), _snapshot(), price=6800.0)

    assert len(read_prices(db, *WINDOW)[TRADING_DATE]) == 1


def test_the_session_window_shortens_on_a_half_day():
    """A half-day is a SHORT session, not a truncated one. The window comes from
    `MarketCalendar.close_time`, the same source `compute_regime`'s `close_et`
    does, so the estimator is never asked to explain missing afternoon hours that
    the exchange was closed for."""
    calendar = MarketCalendar(
        session_open=time(9, 30),
        session_close=time(16, 0),
        half_day_close=time(13, 0),
        holidays=frozenset(),
        half_days=frozenset({TRADING_DATE}),
    )
    start, end = session_window(calendar, TRADING_DATE)

    assert start.time() == time(10, 0)
    assert end.time() == time(13, 0)


@pytest.mark.parametrize(
    "atm_iv, rv, expected",
    [
        (0.20, RealizedVol(0.16, None, 355.0, 360.0, 355, 355, 1.0, 60.0), 1.25),
        # A refused RV yields no ratio — never a ratio against a number the
        # estimator declined to stand behind.
        (0.20, RealizedVol(None, "coverage_too_low", 355.0, 360.0, 80, 355, 0.23, 60.0), None),
        (None, RealizedVol(0.16, None, 355.0, 360.0, 355, 355, 1.0, 60.0), None),
    ],
)
def test_iv_over_rv_declines_whenever_either_side_is_missing(atm_iv, rv, expected):
    reading = _reading(atm_iv, -0.001, 360.0)
    got = iv_over_rv(reading, rv)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_iv_over_rv_declines_a_missing_reading():
    assert iv_over_rv(None, RealizedVol(0.16, None, 355.0, 360.0, 355, 355, 1.0, 60.0)) is None


def test_percentiles_are_nearest_rank_so_every_boundary_is_an_observed_day():
    """Same rule as `tools.evaluability`, and deliberately the same: a tercile
    boundary no session ever produced is not a threshold anyone can check against
    the trail."""
    values = [0.10, 0.12, 0.14, 0.16, 0.18, 0.20]

    assert _pct(values, 1 / 3) in values
    assert _pct(values, 2 / 3) in values
    assert _pct(values, 0.50) == 0.14
    assert _pct([], 0.5) == 0.0
