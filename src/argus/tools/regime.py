"""Read the recorded regime telemetry — the instrument `[regime]` gets calibrated from.

READ-ONLY. Opens the database `mode=ro`, takes no lock, writes nothing — safe to
run mid-session against a live database (WAL readers never block the writer).

    ./.venv/bin/python -m argus.tools.regime --config argus.sandbox.toml
    ./.venv/bin/python -m argus.tools.regime --config argus.sandbox.toml --from 2026-08-12
    ./.venv/bin/python -m argus.tools.regime --config argus.sandbox.toml --at 13:00

`signal/regime.py` has computed `atm_iv` and `skew` on every tick with a chain
since 2026-08-12 and logged them on `signal.none` (Tier 2) and `signal.candidate`
(Tier 1), specifically so a threshold could one day be fit to the RECORDED
distribution rather than guessed. Nothing has ever read those records back. This
is that reader.

It answers three questions, in the order they have to be asked:

**Does the estimator work on a real chain at all?** The decision record
(`docs/decisions/regime-telemetry-and-event-blackout.md`) notes the estimator "has
only ever seen synthetic strips". Before any distribution means anything, the
failure rate has to be known and attributed: a NaN from the 10-minute arithmetic
guard, a NaN from a strip too thin to integrate, and a NaN from a total variance
that came out NEGATIVE are three different problems, and the payload distinguishes
them without any code change (see `_classify`).

**What does the distribution look like, per DAY?** Not per tick. See below.

**Is it armable?** No — and the tool says so, with the reason, rather than
printing something pasteable. This measures a PREDICTOR. Arming `[regime]` is a
claim about a RESPONSE (does a high-IV entry actually do worse), and no such
dataset exists: cert fills are not realistic, and the `credit_multiple`
prohibition narrows the admissible outcome set further still. Two different
studies; only one of them is possible today.

FOUR RULES ARE BUILT INTO WHAT THIS PRINTS, because each one is a way the
calibration goes quietly wrong.

1. **THE UNIT IS A DAY, NOT A TICK.** At `signal.cadence_s = 5.0` a session logs
   ~3,600 readings of a heavily autocorrelated series. Ticks make `n` look like
   thousands when the effective sample size is the number of SESSIONS. Every
   threshold this prints is computed from one reading per day, and the tick counts
   are reported only as coverage.

2. **THE CLOCK IS FIXED, NOT MODELLED.** The estimator is not scale-free in
   `minutes_to_settlement` — the VIX integral's discretization error grows as the
   remaining life shrinks toward SPX's 5-point ladder. `regime.py`'s docstring
   tabulates it and says "condition on `minutes_to_settlement`". Taking ONE
   reading per day at a fixed ET time is the strongest available form of that
   conditioning: it removes the bias exactly instead of estimating it. `--at`
   defaults to `signal.entry_earliest`, which is both the first moment an entry is
   possible and the decision point Vilkov (2026) measures.

3. **BLACKOUT DAYS ARE EXCLUDED BY DEFAULT.** The strategy refuses every entry on
   a listed FOMC/CPI/NFP/PCE date before any side is examined, so the gate never
   sees those chains. They are also the high-IV days. Leaving them in fits the top
   tercile to a population the gate cannot act on. `--include-blackout` puts them
   back for a separate look.

4. **`skew` IS A CLOCK READING BEFORE IT IS A MARKET READING, AND THAT BINDS
   INSIDE THE ENTRY WINDOW.** Measured on 22 real sessions (2026-08-05 .. 09-04),
   pooled by time remaining:

       band        300+min  240-300  180-240  120-180   60-120    10-60
       raw skew   -0.00124 -0.00076  0.00007  0.00033  0.00188  0.00503
       skew/iv^2    -0.054   -0.038    0.003    0.017    0.089    0.162

   It crosses ZERO around 200 minutes to settlement, and **21 of 21 sessions move
   the same way** — a real market effect does not go 21/21 across sessions with
   different vol levels and different directions, so this is overwhelmingly the
   estimator, not the surface. A symmetric synthetic strip drifts the same way
   (`-0.055` at 10:00 to `-0.020` at 15:30 on this window) but 3-8x less, which
   places the mechanism in the strip's composition — wings truncated by the
   `tradeable` filter and floored by `zerobid` as the distribution collapses
   toward the 5-point ladder — rather than in the arithmetic.

   The consequence is the sharp one: the entry window (10:00-15:00) spans 360 to
   60 minutes, so **a single scalar skew threshold applied across it would refuse
   puts in the morning and calls in the afternoon from the clock alone.** That is
   a stronger objection to arming `_side_permitted` than any threshold value, and
   it is why this tool takes ONE reading per day at a fixed time.

   NORMALIZING DOES NOT FIX IT, and does not fix the cross-day problem either.
   `skew / atm_iv**2` is dimensionless and, on synthetic strips holding the
   surface SHAPE fixed, invariant to the vol level where raw skew moves 20x. That
   mechanism is real but NEGLIGIBLE in practice: across 18 real sessions at a
   fixed 10:00 clock the two are equally variable (coefficient of variation 1.57
   raw vs 1.59 normalized) and neither correlates much with the level
   (`-0.24` raw, `-0.12` normalized). Day-to-day skew is dominated by genuine
   surface-shape changes that swamp the level effect, because the level barely
   moves (0.117-0.287) while the shape flips sign. Both are reported; neither is
   preferred, and `RegimeConfig`'s raw-valued keys need no redefinition on this
   evidence.

Neither the thresholds nor the ranking is a decision. This prints evidence and
the decision RULE; a human picks the branch — the same discipline
`tools.evaluability` follows, and for the same reason.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

from argus.clock import ET, UTC_TS_FMT, et_day_bounds_utc_iso
from argus.config import Config, ConfigError, load_blackout_dates
from argus.marketcalendar import MarketCalendar
from argus.signal.realized import GRID_SECONDS, WINDOW_START_ET, RealizedVol, realized_vol

# The two per-tick records that carry a regime block. `signal.skip` deliberately
# carries none (no chain, so nothing to estimate from), which is why it is absent
# here rather than counted as a failure — see `orchestrator.evaluate_once`.
CANDIDATE = "signal.candidate"
NONE = "signal.none"
_TICK_CATEGORIES = (CANDIDATE, NONE)

# The durable process/build boundary. There is no pid column on `audit_log`; a
# build change is visible only as a `process.start` carrying a different version,
# and a series that spans two of them is two code versions in one distribution.
PROCESS_START = "process.start"

# Mirrors `regime.MIN_MINUTES_TO_SETTLEMENT`. Imported as a literal rather than
# from the module so this tool can read a database written by an OLDER build
# without silently re-attributing its NaNs to the current guard value.
GUARD_MINUTES = 10.0


# Minutes-to-settlement bands for the pooled view. Bands, not clock hours: a
# half-day closes at 13:00, so an hour-of-day bucket mixes 3 hours of remaining
# life with 30 minutes of it — the exact conflation rule 2 exists to prevent.
_BANDS: tuple[tuple[str, float, float], ...] = (
    ("300+ min", 300.0, math.inf),
    ("240-300", 240.0, 300.0),
    ("180-240", 180.0, 240.0),
    ("120-180", 120.0, 180.0),
    ("60-120", 60.0, 120.0),
    ("10-60", GUARD_MINUTES, 60.0),
    ("<10 (guard)", -math.inf, GUARD_MINUTES),
)


def _utc_window(first: date, last: date) -> tuple[str, str]:
    """UTC ts bounds covering the ET trading dates `first..last` inclusive.

    The ET day is the unit and `audit_log.ts` is UTC, so the boundary is a
    conversion rather than a date comparison on a string — `clock`'s one rule for
    that (`et_day_bounds_utc_iso`), opened at `first`'s start and closed at `last`'s
    end, never a second spelling of it.
    """
    return et_day_bounds_utc_iso(first)[0], et_day_bounds_utc_iso(last)[1]


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, UTC_TS_FMT).replace(tzinfo=timezone.utc)


def _float(value: object) -> float | None:
    """A recorded number, or None. `RegimeSnapshot.to_payload` writes None for NaN
    (canonical audit hashing refuses what `json` cannot encode), so None here means
    "the estimator declined", never "the field is missing"."""
    return float(value) if isinstance(value, (int, float)) else None


@dataclass(frozen=True)
class Reading:
    """One tick's regime block, as recorded."""

    seq: int
    at_utc: datetime
    trading_date: date
    atm_iv: float | None
    skew: float | None
    minutes: float | None
    bucket: str | None
    category: str

    @property
    def at_et(self) -> datetime:
        return self.at_utc.astimezone(ET)

    @property
    def skew_ratio(self) -> float | None:
        """`skew / atm_iv**2` — the vol-level-independent asymmetry. See rule 4."""
        if self.skew is None or self.atm_iv is None or self.atm_iv <= 0.0:
            return None
        return self.skew / (self.atm_iv * self.atm_iv)


def _classify(reading: Reading) -> str:
    """Which of the estimator's outcomes this reading is.

    The three failure modes are genuinely different problems and the payload
    separates them without needing a code change:

    `guard`      — under `MIN_MINUTES_TO_SETTLEMENT`. Working as designed; the
                   `2/T` factor turns a one-tick pricing error into an arbitrary
                   vol below it. Expected near the close, alarming at 10:00.
    `no_strip`   — an early return above the guard: no implied forward (no strike
                   quoting BOTH a call and a put within `market_data_max_age_s`),
                   no `K_0`, or a wing with fewer than two strikes. A FEED
                   coverage problem, not an arithmetic one.
    `negative`   — the strip integrated but total variance came out <= 0, so
                   there was no square root to take. `skew` survives this path
                   and `atm_iv` does not, which is exactly how it is identified.
                   Means the strip and the forward disagree beyond what the
                   correction absorbs: a crossed or stale quote.
    `estimated`  — a reading.
    """
    if reading.atm_iv is not None:
        return "estimated"
    if reading.skew is not None:
        return "negative"
    if reading.minutes is not None and reading.minutes < GUARD_MINUTES:
        return "guard"
    return "no_strip"


def read_readings(conn: sqlite3.Connection, start: str, end: str) -> list[Reading]:
    """Every recorded regime block in the window, in `seq` order.

    `trading_date` comes from the payload's own `today`, not from converting `ts`:
    the process stamped that date itself from `clock.et_today`, so it is
    authoritative about which session a reading belongs to and cannot disagree
    with the date the blackout check used.
    """
    rows = conn.execute(
        "SELECT seq, ts, category, payload FROM audit_log "
        f"WHERE category IN ({', '.join('?' * len(_TICK_CATEGORIES))}) "
        "AND ts >= ? AND ts < ? ORDER BY seq",
        (*_TICK_CATEGORIES, start, end),
    ).fetchall()

    readings: list[Reading] = []
    for seq, ts, category, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        block = payload.get("regime")
        if not isinstance(block, dict):
            # A record predating the regime work (before 2026-08-12). Skipped
            # rather than counted as a failure: the estimator did not decline,
            # it did not exist.
            continue
        try:
            trading_date = date.fromisoformat(str(block.get("today")))
        except ValueError:
            continue
        readings.append(
            Reading(
                seq=int(seq),
                at_utc=_parse(ts),
                trading_date=trading_date,
                atm_iv=_float(block.get("atm_iv")),
                skew=_float(block.get("skew")),
                minutes=_float(block.get("minutes_to_settlement")),
                bucket=block.get("regime_bucket"),
                category=str(category),
            )
        )
    return readings


def read_prices(
    conn: sqlite3.Connection, start: str, end: str
) -> dict[date, list[tuple[datetime, float]]]:
    """The recorded index path, `{ET date: [(at, price)]}`, in `seq` order.

    `underlying_price` rides on every `signal.none` / `signal.candidate` with a
    chain, at `signal.cadence_s`. Measured on the 2026-09-07 snapshot: 100% of
    those records carry one, back to 2026-07-24.

    **The session is keyed off the TIMESTAMP here, not off the payload's `today`**
    — the opposite of `read_readings`, deliberately. The price predates the regime
    block (`signal.none` carried it before 2026-08-12), so a payload date is not
    available for the whole path; and unlike a regime reading, a tick's ET date is
    unambiguous on its own, because a session runs 09:30-16:00 ET and crosses no
    midnight. Using `today` here would silently drop every pre-regime session,
    which is most of the backfill.
    """
    out: dict[date, list[tuple[datetime, float]]] = {}
    for payload_json, ts in conn.execute(
        "SELECT payload, ts FROM audit_log "
        f"WHERE category IN ({', '.join('?' * len(_TICK_CATEGORIES))}) "
        "AND ts >= ? AND ts < ? ORDER BY seq",
        (*_TICK_CATEGORIES, start, end),
    ):
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        price = payload.get("underlying_price")
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0.0:
            continue
        at = _parse(ts).astimezone(ET)
        out.setdefault(at.date(), []).append((at, float(price)))
    return out


def session_window(
    calendar: MarketCalendar, trading_date: date
) -> tuple[datetime, datetime]:
    """`(window_start, window_end)` in ET for one session.

    The close comes from `MarketCalendar.close_time`, so a half-day shortens the
    window instead of the estimator pretending the session ran to 16:00 — the same
    reason `compute_regime` takes an injected `close_et`.
    """
    return (
        datetime.combine(trading_date, WINDOW_START_ET, tzinfo=ET),
        datetime.combine(trading_date, calendar.close_time(trading_date), tzinfo=ET),
    )


def read_builds(conn: sqlite3.Connection, start: str, end: str) -> list[tuple[datetime, str]]:
    """`(started_at, version)` for every process start in the window."""
    out: list[tuple[datetime, str]] = []
    for ts, payload_json in conn.execute(
        "SELECT ts, payload FROM audit_log WHERE category = ? AND ts >= ? AND ts < ? ORDER BY seq",
        (PROCESS_START, start, end),
    ):
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        out.append((_parse(ts), str(payload.get("version") or "unknown")))
    return out


def read_span(conn: sqlite3.Connection) -> tuple[datetime, datetime] | None:
    """The full extent of the database, for defaulting `--from`/`--to`."""
    row = conn.execute("SELECT MIN(ts), MAX(ts) FROM audit_log").fetchone()
    if not row or row[0] is None:
        return None
    return (_parse(row[0]), _parse(row[1]))


def daily_reading(readings: list[Reading], at_et: time, tolerance_s: float) -> Reading | None:
    """The reading nearest `at_et` on one day, or None if nothing is within tolerance.

    Nearest rather than first-at-or-after: a session that came up late, or a tick
    the loop skipped, should contribute the closest thing it has and be REPORTED
    as offset — silently taking the next available reading would put a 10:47
    observation in a column labelled 10:00.

    Estimated readings only. An unestimable one is a fact about the FEED at that
    instant, not a level, and admitting it here would put a hole in the series
    that a percentile cannot see.
    """
    candidates = [r for r in readings if r.atm_iv is not None]
    if not candidates:
        return None
    target = datetime.combine(candidates[0].at_et.date(), at_et, tzinfo=ET)
    nearest = min(candidates, key=lambda r: abs((r.at_et - target).total_seconds()))
    if abs((nearest.at_et - target).total_seconds()) > tolerance_s:
        return None
    return nearest


# -- reporting ----------------------------------------------------------------


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile — the same rule `tools.evaluability` uses, and
    deliberately the same: it never interpolates, so every number printed is a
    reading that actually happened. A tercile boundary that no day ever produced
    is not a threshold anyone can check against the trail."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _corr(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation. NaN on a degenerate input rather than a ZeroDivisionError:
    this is a diagnostic line, and a constant column is a fact to print, not a crash."""
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return num / den if den else math.nan


def _cv(values: list[float]) -> float:
    """Coefficient of variation, |sd / mean|. Comparable across the raw and
    normalized columns, which have different units — which is the whole point."""
    mean = statistics.mean(values)
    return abs(statistics.stdev(values) / mean) if len(values) > 1 and mean else math.nan


def _fmt(value: float | None, spec: str = "7.4f") -> str:
    """A number, or a right-aligned dash occupying the same column width."""
    if value is not None:
        return format(value, spec)
    try:
        width = int(spec.split(".")[0])
    except ValueError:
        width = 7
    return "-".rjust(width)


def _report_coverage(
    db_path: str,
    first: date,
    last: date,
    readings: list[Reading],
    builds: list[tuple[datetime, str]],
) -> None:
    # The database is named on every run, deliberately: `db_path` is CWD-relative
    # and this reads whatever `--config` points at.
    print(f"\n{first} .. {last}  —  {db_path}  (read-only)")
    if not readings:
        print(
            "  NO REGIME TELEMETRY IN THIS WINDOW.\n"
            "  Either the window predates 2026-08-12 (the estimator did not exist) or\n"
            "  no session logged a tick with a chain. Nothing below can be computed."
        )
        return
    days = sorted({r.trading_date for r in readings})
    by_category = Counter(r.category for r in readings)
    print(
        f"  {len(readings):,} reading(s) over {len(days)} session(s)   "
        f"{by_category[NONE]:,} on {NONE} (T2), {by_category[CANDIDATE]:,} on {CANDIDATE} (T1)"
    )

    versions = sorted({v for _, v in builds})
    if len(versions) > 1:
        print(
            f"  !! {len(versions)} BUILDS in this window: {', '.join(versions)}\n"
            "     The series spans code versions. Split the window at the deploy before\n"
            "     reading any distribution — a whole-window aggregate mixes them."
        )
    elif versions:
        print(f"  build {versions[0]}  ({len(builds)} process start(s))")
    else:
        print("  no process.start in window — build unknown (records may predate the stamp)")

    if any(r.bucket is not None for r in readings):
        print(
            "  !! some readings carry a non-null regime_bucket, so [regime] was CONFIGURED\n"
            "     for part of this window. Those ticks were gated. Not a calibration sample."
        )


def _report_health(readings: list[Reading]) -> None:
    """The sanity read that has to come before any distribution."""
    print("\nESTIMATOR HEALTH")
    kinds = Counter(_classify(r) for r in readings)
    total = len(readings)
    labels = {
        "estimated": "a reading",
        "guard": f"under {GUARD_MINUTES:g} min to settlement — by design",
        "no_strip": "no forward / no K0 / a wing under 2 strikes  -> FEED COVERAGE",
        "negative": "total variance <= 0 — crossed or stale quote",
    }
    for kind in ("estimated", "guard", "no_strip", "negative"):
        count = kinds.get(kind, 0)
        print(f"  {kind:<12} {count:>9,}  {count / total:>6.1%}   {labels[kind]}")

    print("\n  by minutes to settlement:")
    print(f"    {'band':<14} {'ticks':>8} {'estimated':>10}   {'median atm_iv':>13}  {'median skew/iv^2':>17}")
    for label, low, high in _BANDS:
        band = [r for r in readings if r.minutes is not None and low <= r.minutes < high]
        if not band:
            continue
        ivs = [r.atm_iv for r in band if r.atm_iv is not None]
        ratios = [r.skew_ratio for r in band if r.skew_ratio is not None]
        # A band with no estimated reading prints "-", never "nan": the guard band
        # is EXPECTED to be empty here, and a nan in a column of levels reads as a
        # broken estimator rather than as one declining exactly where it should.
        print(
            f"    {label:<14} {len(band):>8,} {len(ivs) / len(band):>10.1%}   "
            f"{_fmt(statistics.median(ivs) if ivs else None, '13.4f')}  "
            f"{_fmt(statistics.median(ratios) if ratios else None, '17.3f')}"
        )
    print(
        "    ^ BOTH columns are clock readings before they are market readings.\n"
        "      atm_iv carries the documented discretization bias into the close (regime.py:\n"
        "      ~1.01x at 10:00 to ~1.46x at 15:45 on a 150-point window) on top of whatever\n"
        "      the intraday vol curve is really doing — measured, the two combine into a U.\n"
        "      skew is worse: on 22 real sessions it CROSSES ZERO around 200 minutes left,\n"
        "      21 of 21 of them in the same direction, which is the strip's composition and\n"
        "      not the surface. The entry window spans 360 to 60 minutes, so one scalar skew\n"
        "      threshold across it refuses puts in the morning and calls in the afternoon\n"
        "      from the clock alone. This is why the series below takes ONE fixed-clock\n"
        "      reading per session, and why pooling these bands would fit the clock."
    )


def iv_over_rv(reading: Reading | None, rv: RealizedVol | None) -> float | None:
    """`atm_iv / realized` for one session, or None if either side is missing.

    The whole reason realized vol is computed at all: `atm_iv` alone is a LEVEL,
    and the premium-seller's question is whether it is RICH, which is a level
    against something. Both sides are annualized on `MINUTES_PER_YEAR`, which is
    why the ratio means anything.
    """
    if reading is None or reading.atm_iv is None or rv is None or not rv.ok:
        return None
    assert rv.annualized is not None  # narrowed by rv.ok
    return reading.atm_iv / rv.annualized if rv.annualized > 0.0 else None


def _report_series(
    rows: list[tuple[date, Reading | None, RealizedVol | None, bool]],
    at_et: time,
    tolerance_s: float,
) -> None:
    print(f"\nTHE DAILY SERIES  (one reading per session, nearest {at_et:%H:%M} ET, +/-{tolerance_s:g}s)")
    print(
        f"  {'date':<12} {'ET':>9} {'offset':>8} {'atm_iv':>8} {'skew':>10} {'skew/iv^2':>10}"
        f" {'RV':>8} {'IV/RV':>7}  note"
    )
    for trading_date, reading, rv, is_blackout in rows:
        notes = ["BLACKOUT"] if is_blackout else []
        # A refused RV is named on the row rather than left blank: `span_too_short`
        # (the session's path did not cover the window) and `coverage_too_low`
        # (holes in what was recorded) are different problems, and a blank cell
        # reads as neither.
        if rv is not None and rv.refusal is not None:
            notes.append(f"RV {rv.refusal}")
        note = "  ".join(notes)
        if reading is None:
            print(
                f"  {trading_date.isoformat():<12} {'-':>9} {'-':>8} {'-':>8} {'-':>10} {'-':>10}"
                f" {_fmt(rv.annualized if rv else None, '8.4f')} {'-':>7}  "
                f"no estimated reading in window  {note}"
            )
            continue
        target = datetime.combine(reading.at_et.date(), at_et, tzinfo=ET)
        offset = (reading.at_et - target).total_seconds()
        print(
            f"  {trading_date.isoformat():<12} {reading.at_et:%H:%M:%S} {offset:>+7.0f}s "
            f"{_fmt(reading.atm_iv, '8.4f')} {_fmt(reading.skew, '10.5f')} "
            f"{_fmt(reading.skew_ratio, '10.3f')} "
            f"{_fmt(rv.annualized if rv else None, '8.4f')} "
            f"{_fmt(iv_over_rv(reading, rv), '7.2f')}  {note}"
        )


def _report_realized(
    pairs: list[tuple[Reading, RealizedVol]], label: str, refused: dict[str, int]
) -> None:
    """The IV/RV aggregate — is the premium rich, and is it rich CONSISTENTLY?"""
    ratios = [r for r in (iv_over_rv(rd, rv) for rd, rv in pairs) if r is not None]
    print(f"\nIMPLIED vs REALIZED  ({label}, n = {len(ratios)} session(s))")
    if refused:
        print(
            "  sessions with no RV: "
            + ", ".join(f"{reason} {count}" for reason, count in sorted(refused.items()))
        )
    if len(ratios) < 2:
        print("  too few sessions to describe.")
        return
    ivs = [rd.atm_iv for rd, rv in pairs if iv_over_rv(rd, rv) is not None]
    rvs = [rv.annualized for rd, rv in pairs if iv_over_rv(rd, rv) is not None]
    above = sum(1 for r in ratios if r > 1.0)
    print(
        f"  IV/RV     min {min(ratios):.2f}   p33 {_pct(ratios, 1/3):.2f}   "
        f"median {_pct(ratios, 0.50):.2f}   p67 {_pct(ratios, 2/3):.2f}   max {max(ratios):.2f}"
    )
    print(f"  mean {statistics.mean(ratios):.3f}   IV > RV on {above} of {len(ratios)} session(s)")
    if len(ivs) >= 3:
        # The forecast-skill check. This is the claim that `atm_iv` measures vol
        # at all, as opposed to measuring its own discretization bias — and it is
        # not mechanical: implied comes from the option chain, realized from the
        # index path.
        print(f"  corr(atm_iv, RV) = {_corr(ivs, rvs):+.3f}   <- forecast skill, not mechanical")
        print(f"  corr(atm_iv, IV/RV) = {_corr(ivs, ratios):+.3f}   <- does the LEVEL predict richness?")
    print(
        "     A ratio above 1 is the variance risk premium: implied over realized.\n"
        "     It is an UPPER BOUND — the estimator's biases (discretization, quote\n"
        "     width) run upward and are inside it. Sign is safe, magnitude is soft."
    )


def _report_signature(
    prices: dict[date, list[tuple[datetime, float]]], calendar: MarketCalendar, days: list[date]
) -> None:
    """The volatility signature plot — the diagnostic that chose `GRID_SECONDS`.

    Printed on every run rather than kept as a one-off, because the flatness it
    shows is a property of the FEED (SPX is a computed index, so no bid-ask
    bounce), and a change to that should be visible rather than silently biasing
    the series. A rising left edge means microstructure noise has appeared and the
    grid needs re-choosing.
    """
    print("\nVOLATILITY SIGNATURE  (median RV by sampling grid; flat = grid choice is not binding)")
    print(f"  {'grid':>8}  {'median RV':>10}  {'sessions':>8}")
    for grid in (5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0):
        values = []
        for day in days:
            start, end = session_window(calendar, day)
            got = realized_vol(prices.get(day, []), window_start=start, window_end=end, grid_seconds=grid)
            if got.ok:
                assert got.annualized is not None
                values.append(got.annualized)
        if not values:
            continue
        mark = "  <- GRID_SECONDS" if grid == GRID_SECONDS else ""
        print(f"  {grid:>7.0f}s  {statistics.median(values):>10.4f}  {len(values):>8}{mark}")


def _report_distribution(sample: list[Reading], label: str) -> None:
    print(f"\nDISTRIBUTION  ({label}, n = {len(sample)} session(s))")
    if len(sample) < 2:
        print("  too few sessions to describe.")
        return
    ivs = [r.atm_iv for r in sample if r.atm_iv is not None]
    ratios = [r.skew_ratio for r in sample if r.skew_ratio is not None]
    raws = [r.skew for r in sample if r.skew is not None]
    for name, values, spec in (
        ("atm_iv", ivs, "8.4f"),
        ("skew (raw)", raws, "8.5f"),
        ("skew/iv^2", ratios, "8.3f"),
    ):
        if not values:
            continue
        print(
            f"  {name:<12} min {format(min(values), spec)}   p33 {format(_pct(values, 1 / 3), spec)}"
            f"   p50 {format(_pct(values, 0.50), spec)}   p67 {format(_pct(values, 2 / 3), spec)}"
            f"   max {format(max(values), spec)}"
        )
    if len(ivs) >= 3 and len(raws) == len(ivs) and len(ratios) == len(ivs):
        # Does a threshold on skew secretly fire on the vol LEVEL? A ratio of
        # spreads answers this badly — real skew crosses zero, so dividing by its
        # median divides by ~0 and reports noise. Correlation against `atm_iv` asks
        # the question directly, and the coefficient of variation says whether
        # normalizing bought any stability at all.
        print(
            f"  vs atm_iv    corr(raw) {_corr(raws, ivs):+.3f}   "
            f"corr(skew/iv^2) {_corr(ratios, ivs):+.3f}"
        )
        print(f"  variability  cv(raw) {_cv(raws):.2f}   cv(skew/iv^2) {_cv(ratios):.2f}")
        print(
            "     Similar cv and both correlations weak => the two carry the same\n"
            "     information and normalizing buys nothing; day-to-day skew is surface\n"
            "     SHAPE, not vol level. A strongly negative corr(raw) with a much weaker\n"
            "     corr(skew/iv^2) would be the opposite finding: a raw threshold acting as\n"
            "     an atm_iv threshold in disguise, and a reason to redefine the key."
        )


def _report_decision(sample: list[Reading], excluded_blackout: int, missing_days: int) -> None:
    print("\nTHE DECISION (docs/decisions/regime-telemetry-and-event-blackout.md)")
    n = len(sample)
    if n == 0:
        print(
            "  INCONCLUSIVE — no estimated reading at the fixed clock on any session.\n"
            "  Read ESTIMATOR HEALTH above: if `no_strip` dominates, this is a feed\n"
            "  coverage problem (implied_forward needs one strike quoting BOTH a call and\n"
            "  a put inside staleness.market_data_max_age_s), not a calibration problem,\n"
            "  and no amount of further collection fixes it."
        )
        return

    ivs = sorted(r.atm_iv for r in sample if r.atm_iv is not None)
    notes = []
    if excluded_blackout:
        notes.append(f"{excluded_blackout} blackout session(s) excluded")
    if missing_days:
        notes.append(f"{missing_days} session(s) had no reading in tolerance")
    suffix = f"  ({'; '.join(notes)}.)" if notes else ""
    print(f"  n = {n} session(s) at the fixed clock.{suffix}")

    if n < 30:
        print(
            f"  NOT ENOUGH DAYS. {n} session(s) is a tercile boundary fit to a handful of\n"
            "  observations; it will move materially with the next week of collection.\n"
            "  Keep collecting. Nothing below is a threshold — it is a progress report."
        )
    else:
        print(
            f"  {n} sessions is enough to DESCRIBE the predictor. Candidate tercile\n"
            f"  boundaries from this sample: atm_iv_low_max = {_pct(ivs, 1/3):.3f}, "
            f"atm_iv_high_min = {_pct(ivs, 2/3):.3f}."
        )

    # The blocker that no amount of collection removes, stated on every run so it
    # cannot be lost behind a suddenly-sufficient n.
    print(
        "\n  ARMING IS STILL BLOCKED, AND MORE DAYS DO NOT UNBLOCK IT:\n"
        "  - This measures a PREDICTOR. Arming claims a RESPONSE (a high-IV entry does\n"
        "    worse), which needs realized outcomes. Cert fills are not realistic, and the\n"
        "    credit_multiple prohibition admits only entry_credit_basis = realized rows.\n"
        "  - The side gate compares RAW skew and has NO debounce, unlike every watchdog\n"
        "    trigger (sustained_s). A reading near threshold flickers the side on and off\n"
        "    every cadence_s, which randomizes entry timing under exactly the disordered\n"
        "    conditions the gate exists for. Hysteresis or a once-per-day latched read is\n"
        "    owed before it can gate anything.\n"
        "  - size_multiplier_* is inert at the current rails: bound 1 only binds when\n"
        "    position_size_pct * multiplier * NLV < 2 * max_loss_per_contract. Above that\n"
        "    max_contracts_per_position governs and the multiplier changes nothing.\n"
        "  Adding a [regime] section is a LIVE behaviour change. It is not a config tidy."
    )


def _open_readonly(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise SystemExit(f"database not found: {db_path}")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _blackout_dates(config: Config) -> tuple[frozenset[date], str | None]:
    """The blackout set, plus a warning when it could not be read.

    Deliberately NON-fatal, unlike `load_blackout_dates`' contract for the running
    process: a missing file there means an operator believes entries are suppressed
    when they are not, which is worth a session. Here it means one filter on a
    read-only report, and refusing to run would leave the operator with nothing.
    The warning is loud because blackout days are the high-IV days.
    """
    if config.events is None:
        return frozenset(), "[events] absent from this config — no blackout days excluded"
    try:
        return load_blackout_dates(config.events.blackout_dates_path), None
    except ConfigError as exc:
        return frozenset(), f"blackout dates unreadable ({exc}) — no blackout days excluded"


def main(argv: list[str] | None = None) -> None:
    """CLI entry point. `argv` is injectable so `tools.eod` can render this
    section in-process rather than shelling out to a second container."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        required=True,
        help="path to the config file (REQUIRED — there is no default; a default "
        "path is how the live database gets opened by accident)",
    )
    parser.add_argument("--from", dest="date_from", help="first ET trading date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="date_to", help="last ET trading date (YYYY-MM-DD)")
    parser.add_argument(
        "--at",
        help="ET wall-clock time for the one-reading-per-day series (HH:MM); "
        "default signal.entry_earliest",
    )
    parser.add_argument(
        "--tolerance-s",
        type=float,
        default=300.0,
        help="how far from --at a reading may be and still represent the day (default 300)",
    )
    parser.add_argument(
        "--include-blackout",
        action="store_true",
        help="keep FOMC/CPI/NFP/PCE sessions in the distribution (default: excluded, "
        "because the strategy refuses every entry on them)",
    )
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    at_et = (
        datetime.strptime(args.at, "%H:%M").time() if args.at else config.signal.entry_earliest
    )

    conn = _open_readonly(config.audit.db_path)
    try:
        span = read_span(conn)
        if span is None:
            raise SystemExit(f"no audit records at all in {config.audit.db_path}")
        first = (
            date.fromisoformat(args.date_from)
            if args.date_from
            else span[0].astimezone(ET).date()
        )
        last = (
            date.fromisoformat(args.date_to) if args.date_to else span[1].astimezone(ET).date()
        )
        start, end = _utc_window(first, last)
        readings = read_readings(conn, start, end)
        builds = read_builds(conn, start, end)
        prices = read_prices(conn, start, end)
    finally:
        conn.close()

    _report_coverage(config.audit.db_path, first, last, readings, builds)
    if not readings:
        return
    _report_health(readings)

    blackout, warning = _blackout_dates(config)
    if warning:
        print(f"\n  !! {warning}")

    by_day: dict[date, list[Reading]] = {}
    for reading in readings:
        by_day.setdefault(reading.trading_date, []).append(reading)

    # Realized vol is computed per session over the SAME calendar the process
    # trades on, so a half-day shortens the window rather than being reported as a
    # truncated full day.
    calendar = MarketCalendar.from_config(config.market)
    rows = []
    for day in sorted(by_day):
        window_start, window_end = session_window(calendar, day)
        rows.append(
            (
                day,
                daily_reading(by_day[day], at_et, args.tolerance_s),
                realized_vol(
                    prices.get(day, []), window_start=window_start, window_end=window_end
                ),
                day in blackout,
            )
        )
    _report_series(rows, at_et, args.tolerance_s)

    kept = [
        r for _, r, _rv, is_blackout in rows
        if r is not None and (args.include_blackout or not is_blackout)
    ]
    excluded = sum(
        1 for _, r, _rv, is_blackout in rows
        if r is not None and is_blackout and not args.include_blackout
    )
    missing = sum(1 for _, r, _rv, _b in rows if r is None)
    # The label must describe what actually happened, not what was asked for. An
    # unreadable list excludes NOTHING, and a sample headed "blackout sessions
    # excluded" that still contains them is the reassuring-when-wrong reading —
    # the high-IV days are exactly the ones this filter exists to drop.
    if args.include_blackout:
        label = "all sessions, blackout included"
    elif warning:
        label = "blackout sessions NOT excluded — list unavailable"
    else:
        label = "blackout sessions excluded"
    _report_distribution(kept, label)

    in_sample = [
        (day, reading, rv)
        for day, reading, rv, is_blackout in rows
        if reading is not None and (args.include_blackout or not is_blackout)
    ]
    _report_realized(
        [(reading, rv) for _day, reading, rv in in_sample if rv.ok],
        label,
        Counter(rv.refusal for _d, _r, rv in in_sample if rv.refusal is not None),
    )
    _report_signature(prices, calendar, [day for day, _r, _rv in in_sample])
    _report_decision(kept, excluded, missing)


if __name__ == "__main__":
    main()
