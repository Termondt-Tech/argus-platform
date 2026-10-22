"""Realized volatility over a recorded price path — the thing `atm_iv` is a level AGAINST.

PURE. No I/O, no clock read, no network, no config — the same contract
`signal/regime.py` holds, and for the same reason: it is fully testable against a
constructed path, and the seam that reads the path owns the reading.

WHAT THIS IS FOR. `regime.compute_regime` estimates IMPLIED vol on every tick.
Nothing computed REALIZED vol, so `atm_iv` was a level with nothing to be a level
against — which is the difference between knowing how far the market will move
(measured 2026-09-08: r = 0.84 against same-day realized) and knowing whether the
premium is RICH. That gap is `docs/research/gate-signal-candidates.md`'s Tier 1
candidate #2. This closes the arithmetic half of it.

**Nothing acts on this number.** It is not a gate input, not a size input, and no
config key selects it — the `[regime]` pattern exactly: compute it, make it
readable, and let a decision record cite a measured distribution before anything
conditions on it.

THE ESTIMATOR is close-to-close quadratic variation over a fixed grid:

    RV = sqrt( SUM ln(S_i / S_i-1)^2  /  (span_minutes / MINUTES_PER_YEAR) )

Parkinson's high/low estimator is more efficient per observation and is the usual
choice when only OHLC is available. We have the whole path, so quadratic variation
uses strictly more information; Parkinson is worth revisiting only if the recorded
path ever becomes sparse.

`MINUTES_PER_YEAR` IS IMPORTED FROM `regime.py` AND MUST NEVER BE REDEFINED HERE.
The one number anybody will actually want is IV/RV, and two copies of an
annualization basis is how a ratio silently stops meaning anything — the same rule
`execution/zerobid.py` exists to enforce for the tick floor. A test asserts the
import and the absence of a second definition.

THE WINDOW AND THE GRID ARE MODULE CONSTANTS, NOT CONFIG. Two separate reasons,
and neither is "config is inconvenient":

- `WINDOW_START_ET` deliberately does NOT read `signal.entry_earliest`, even
  though it currently holds the same value. That key is config and can move; if it
  did, the window would silently redefine itself and every historical comparison
  would break with nothing saying so. A series has to be comparable across days
  before it is convenient to retune.
- A new REQUIRED config key forces a coupled image+config deploy for a number
  nothing trades on. Same call, and the same reasoning, as
  `status.BLACKOUT_WARN_DAYS`.

`GRID_SECONDS` was chosen by DIAGNOSTIC rather than assumed. 5-second sampling
normally overstates realized vol through microstructure noise; measured over 21
sessions the volatility signature is FLAT from 5s to 900s (0.118-0.128), because
SPX is a computed index over 500 constituents rather than a traded price — no
bid-ask bounce, and the mild dip at 5s is constituent staleness. So the choice
moves nothing today. It is still a constant rather than a literal, and the tool
still prints the signature across grids, because that flatness is a property of
the FEED and a future change to it should be visible rather than silently biasing
the series.

A SHORT OR GAPPY SESSION REPORTS NO NUMBER, WITH THE REASON. Two failures are
distinguished because they are different problems: `span_too_short` is a session
whose path does not cover the window (a late start, or the 2026-09-03 halt at
11:04), and `coverage_too_low` is a path with HOLES in it (Tier 2 telemetry is
droppable under burst — `AuditLog.telemetry` returns False when the queue is
full). Reporting a number computed over whatever happened to be there would put a
figure in a comparable series that is not comparable to it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time

from argus.signal.regime import MINUTES_PER_YEAR

# 10:00 ET — the first moment an entry is possible, and the decision point the
# 2026-09-08 measurement and Vilkov (2026)'s conditional-entry rule both use.
# NOT read from `signal.entry_earliest`; see the module docstring.
WINDOW_START_ET = time(10, 0)

# Chosen by signature plot, not assumed. See the module docstring.
GRID_SECONDS = 60.0

# Minimum sampled span, in minutes, before a figure is reported. The 10:00->close
# window is 360 minutes on a full day and 180 on a half-day, so this is expressed
# as a FRACTION of the requested window rather than an absolute — a half-day is a
# short session, not a broken one.
MIN_SPAN_FRACTION = 0.80

# Minimum grid occupancy WITHIN the sampled span. Bounds holes, where
# MIN_SPAN_FRACTION bounds truncation; the two catch different failures and a
# session can fail either independently.
MIN_COVERAGE = 0.80

# Below this there is no quadratic variation worth the name, whatever the span.
MIN_SAMPLES = 3


@dataclass(frozen=True)
class RealizedVol:
    """One session's realized volatility, or the reason there isn't one.

    `annualized` is None exactly when `refusal` is not None — the pair is the same
    "no reading, and here is why" contract `tools.regime` reports estimator health
    under, and the reason a caller can distinguish a quiet feed from a halted
    process without re-reading the path.
    """

    annualized: float | None
    refusal: str | None
    span_minutes: float
    window_minutes: float
    samples: int
    expected_samples: int
    coverage: float
    grid_seconds: float

    @property
    def ok(self) -> bool:
        return self.annualized is not None


def _refused(
    reason: str,
    *,
    window_minutes: float,
    grid_seconds: float,
    span_minutes: float = 0.0,
    samples: int = 0,
    expected: int = 0,
    coverage: float = 0.0,
) -> RealizedVol:
    return RealizedVol(
        annualized=None,
        refusal=reason,
        span_minutes=span_minutes,
        window_minutes=window_minutes,
        samples=samples,
        expected_samples=expected,
        coverage=coverage,
        grid_seconds=grid_seconds,
    )


def realized_vol(
    path: list[tuple[datetime, float]],
    *,
    window_start: datetime,
    window_end: datetime,
    grid_seconds: float = GRID_SECONDS,
) -> RealizedVol:
    """Annualized realized volatility over `[window_start, window_end]`.

    `path` is `(timestamp, price)` pairs in any order; timestamps must share the
    tzinfo of the window bounds. `window_start`/`window_end` are INJECTED rather
    than derived, keeping this pure — the caller builds them from the trading date
    and `MarketCalendar.close_time`, so a half-day shortens the window correctly
    instead of pretending the session ran to 16:00. Same split, and the same
    reason, as `compute_regime`'s `now_et`/`close_et`.

    Never raises. See the module docstring for the refusal taxonomy.
    """
    window_minutes = (window_end - window_start).total_seconds() / 60.0
    if window_minutes <= 0.0 or grid_seconds <= 0.0:
        return _refused("empty_window", window_minutes=window_minutes, grid_seconds=grid_seconds)

    # One sample per grid bucket, the LAST price in each — a bucket is a period
    # and its close is what a close-to-close return is defined on. Buckets are
    # indexed from `window_start` so the grid is identical across sessions
    # regardless of when the first tick happened to land.
    buckets: dict[int, tuple[datetime, float]] = {}
    for at, price in path:
        if at < window_start or at > window_end or not math.isfinite(price) or price <= 0.0:
            continue
        index = int((at - window_start).total_seconds() // grid_seconds)
        current = buckets.get(index)
        if current is None or at >= current[0]:
            buckets[index] = (at, price)

    expected = int(window_minutes * 60.0 // grid_seconds) + 1
    if len(buckets) < MIN_SAMPLES:
        return _refused(
            "too_few_samples",
            window_minutes=window_minutes,
            grid_seconds=grid_seconds,
            samples=len(buckets),
            expected=expected,
        )

    ordered = [buckets[i] for i in sorted(buckets)]
    span_minutes = (ordered[-1][0] - ordered[0][0]).total_seconds() / 60.0

    # Coverage is measured over the SPAN, not the window: it answers "are there
    # holes in what was recorded", which is a different question from "did the
    # recording cover the session", and a truncated day must not also be reported
    # as a gappy one.
    span_expected = int(span_minutes * 60.0 // grid_seconds) + 1
    coverage = len(ordered) / span_expected if span_expected > 0 else 0.0

    if span_minutes < window_minutes * MIN_SPAN_FRACTION:
        return _refused(
            "span_too_short",
            window_minutes=window_minutes,
            grid_seconds=grid_seconds,
            span_minutes=span_minutes,
            samples=len(ordered),
            expected=span_expected,
            coverage=coverage,
        )
    if coverage < MIN_COVERAGE:
        return _refused(
            "coverage_too_low",
            window_minutes=window_minutes,
            grid_seconds=grid_seconds,
            span_minutes=span_minutes,
            samples=len(ordered),
            expected=span_expected,
            coverage=coverage,
        )

    variance = sum(
        math.log(later / earlier) ** 2
        for (_, earlier), (_, later) in zip(ordered, ordered[1:])
    )
    # Annualized over the SAMPLED span, never the requested window: quadratic
    # variation covers the period actually observed, and dividing by a window the
    # path did not span would understate every truncated day rather than refusing
    # it. The refusal above is what keeps the series comparable; this keeps the
    # arithmetic honest for the days that pass it.
    annualized = math.sqrt(variance / (span_minutes / MINUTES_PER_YEAR))
    if not math.isfinite(annualized):
        return _refused(
            "not_finite",
            window_minutes=window_minutes,
            grid_seconds=grid_seconds,
            span_minutes=span_minutes,
            samples=len(ordered),
            expected=span_expected,
            coverage=coverage,
        )
    return RealizedVol(
        annualized=annualized,
        refusal=None,
        span_minutes=span_minutes,
        window_minutes=window_minutes,
        samples=len(ordered),
        expected_samples=span_expected,
        coverage=coverage,
        grid_seconds=grid_seconds,
    )
