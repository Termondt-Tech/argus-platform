"""Calibrate the flatten deferral's tail — where `z99` and `alpha` come from.

READ-ONLY. Opens every database `mode=ro`, takes no lock, writes nothing — safe to
run against a NAS snapshot or mid-session against a live database.

    ./.venv/bin/python -m argus.tools.flatten_calibration --config argus.dev.toml \\
        --db dev/nas/argus-20260914T163001.db --db dev/nas/argus-20260924T163001.db

`watchdog.flatten_defer_tail_z99` and `watchdog.flatten_defer_tail_alpha` are
CALIBRATION OUTPUTS, never hand-tuned (`execution/flatten_deferral.py` states the
model). This tool produces them from Argus's own recorded SPX path and prints the
evidence beside them, so a change to either is a rerun, a read and a config-only
release — never an edit.

WHAT IT MEASURES. For every 30-minute window (and 5/10/15/20, for the envelope),
stepping one minute from 10:00 to the close, on every NON-EVENT session:

    z = ln(max or min over the window / spot at its start) / (sigma · √(L / year))
    sigma = max(atm_iv at the start, trailing-30-minute realized vol at the start)

— the SAME standardization the watchdog computes at runtime, from the SAME functions
(`signal.realized.realized_vol` for RV; the recorded `regime.atm_iv`, which is
`regime.compute_regime`, for IV; `regime.MINUTES_PER_YEAR` for the basis). That
equivalence is the whole claim the calibration makes, so it is structural here rather
than a matter of matching arithmetic by hand.

FIVE RULES ARE BUILT INTO WHAT THIS PRINTS.

1. **Event sessions are excluded: the log's `event_blackout` OR a FRED release date.**
   A session is out when `signal.none` records `reason = event_blackout` (FOMC, the
   discretionary dates, and every CPI/NFP/PCE day before 2026-09-30) or when FRED
   lists it as a CPI, NFP or PCE release date — `tools.blackout_dates`' own fetcher and
   ids. The release days are TRADED since v0.5.4 and log no blackout, but they stay
   out: they are known in advance and realize a much lighter standardized tail from
   10:00, so pooling them thins the tail the model applies on every other day, and
   the non-event fit is conservative on them. Needs `ARGUS_FRED_API_KEY`; no key, a
   failed request or an empty family refuses the run, because an empty answer would
   pool them in silently — the thin direction.
   -> docs/decisions/2026-09-29-the-flatten-calibration-excludes-release-days.md
2. **The whole day, not the last hour.** The deferral acts from 15:30, but the
   all-day population is a conservative SUPERSET: it carries the midday shock
   episodes that make the tail, and it is ten times the sample.
3. **`z99` is the ENVELOPE across window lengths** — the largest 99th percentile over
   5/10/15/20/30 minutes — because the runtime τ shrinks from 30 minutes to zero.
4. **`alpha` is the Hill estimate, capped at 3** — the inverse cubic law of index
   returns. Weeks of data cannot certify a thinner tail, and the loader refuses one.
   The recommendation is `min(3.0, the bootstrap lower bound)`.
5. **The tail must be modelled, and the tool says why.** A budget near 1e-3 is below
   what the sample can certify empirically (rule of three on the non-overlapping
   windows is printed), so extrapolation is unavoidable — and done with a stated tail.

→ docs/designs/2026-09-25-deferring-a-far-otm-flatten.md §4.4
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from argus.bootstrap import load_env
from argus.clock import ET
from argus.config import MAX_FLATTEN_DEFER_TAIL_ALPHA, Config
from argus.execution.flatten_deferral import (
    DeferralInputs,
    DeferralParams,
    decide,
    deferral_boundary,
)
from argus.occ import occ_strike
from argus.risk.loss_budget import vertical_width
from argus.signal.realized import realized_vol
from argus.signal.regime import MINUTES_PER_YEAR
from argus.tools.blackout_dates import (
    FRED_RELEASE_IDS,
    BlackoutError,
    _api_key,
    fetch_release_dates,
)
from argus.tools.regime import _utc_window, read_prices, read_readings, read_span

#: Window lengths, in minutes, for the envelope (rule 3).
WINDOW_MINUTES: tuple[int, ...] = (5, 10, 15, 20, 30)
#: The headline window, matching the 30-minute runway of the flatten.
PRIMARY_MINUTES = 30
#: First window start, ET. The strategy's own entry window opens here, and the regime
#: estimator's morning readings are unstable before it.
FIRST_START = time(10, 0)
#: Step between window starts. Overlapping windows are correlated; the tail counts are
#: also reported on non-overlapping windows for that reason.
STEP_S = 60
#: How far a start's spot or IV reading may be from the start instant and still
#: represent it — six signal ticks at the 5-second cadence.
MATCH_TOLERANCE_S = 30.0
#: A window whose path has a hole wider than this is dropped rather than measured: a
#: gap hides exactly the excursion being measured.
MAX_GAP_S = 60.0
#: Top fraction of pooled excursions the Hill estimator uses.
HILL_FRACTION = 0.02
#: Bootstrap resamples (by DAY — the unit that is independent) and the fixed seed that
#: makes the printed interval reproducible.
BOOTSTRAP_RESAMPLES = 200
BOOTSTRAP_SEED = 20260925
#: The trailing realized-vol window: the runtime watchdog's own.
RV_WINDOW = timedelta(minutes=30)
#: Rule 1's guard. CPI, NFP and PCE each publish monthly (gaps of five weeks at most),
#: so a family with no FRED date over a span longer than this is a broken answer,
#: never a quiet calendar. Shorter spans can legitimately hold none.
RELEASE_GUARD_DAYS = 45


@dataclass(frozen=True)
class Session:
    """One recorded session: the index path and the IV readings, both ET-stamped."""

    day: date
    path: tuple[tuple[datetime, float], ...]
    iv: tuple[tuple[datetime, float], ...]
    close: datetime


@dataclass(frozen=True)
class Excursion:
    """One window's standardized maximum moves, UP and DOWN, and where it started."""

    day: date
    start: datetime
    minutes: int
    up: float
    down: float


def _nearest(
    times: Sequence[datetime], series: Sequence[tuple[datetime, float]], at: datetime
) -> float | None:
    """The value nearest `at` within `MATCH_TOLERANCE_S`, or None. `times` is
    `series`' timestamps, precomputed once per series by the caller."""
    if not series:
        return None
    i = bisect.bisect_left(times, at)
    best: tuple[float, float] | None = None
    for j in (i - 1, i):
        if 0 <= j < len(series):
            gap = abs((series[j][0] - at).total_seconds())
            if gap <= MATCH_TOLERANCE_S and (best is None or gap < best[0]):
                best = (gap, series[j][1])
    return None if best is None else best[1]


def _trailing_rv(
    times: Sequence[datetime], path: Sequence[tuple[datetime, float]], at: datetime
) -> float | None:
    """`realized_vol` over the trailing `RV_WINDOW`, handed only that slice of the
    path — the SAME function the watchdog runs, and O(window) rather than O(day)."""
    i0 = bisect.bisect_left(times, at - RV_WINDOW)
    i1 = bisect.bisect_right(times, at)
    return realized_vol(
        list(path[i0:i1]), window_start=at - RV_WINDOW, window_end=at
    ).annualized


def session_starts(session: Session) -> list[tuple[datetime, float, float]]:
    """`(start, spot, sigma)` for every usable window start in one session. Pure.

    Computed ONCE per session and shared by every window length, because neither the
    spot at a start nor its sigma depends on how long the window is. A start is
    dropped when it has no spot or IV within tolerance or trailing RV is refused
    (`realized.py`'s own coverage rules).
    """
    times = [t for t, _ in session.path]
    iv_times = [t for t, _ in session.iv]
    out = []
    at = datetime.combine(session.day, FIRST_START, tzinfo=ET)
    while at < session.close:
        s0 = _nearest(times, session.path, at)
        iv = _nearest(iv_times, session.iv, at)
        if s0 is not None and iv is not None and iv > 0.0:
            rv = _trailing_rv(times, session.path, at)
            if rv is not None:
                out.append((at, s0, max(iv, rv)))
        at += timedelta(seconds=STEP_S)
    return out


def session_excursions(
    session: Session,
    minutes: int,
    starts: Sequence[tuple[datetime, float, float]] | None = None,
) -> list[Excursion]:
    """Every standardized window of `minutes` in one session. Pure.

    A window is dropped — never measured badly — when its start is unusable (see
    `session_starts`), it runs past the close, or its path has a hole wider than
    `MAX_GAP_S`.
    """
    if starts is None:
        starts = session_starts(session)
    path = session.path
    times = [t for t, _ in path]
    length = timedelta(minutes=minutes)
    root = math.sqrt(minutes / MINUTES_PER_YEAR)
    out: list[Excursion] = []
    for start, s0, sigma in starts:
        if start + length > session.close:
            break
        i0 = bisect.bisect_left(times, start)
        i1 = bisect.bisect_right(times, start + length)
        segment = path[i0:i1]
        if not segment or not _covered(segment, start, start + length):
            continue
        scale = sigma * root
        hi = max(p for _, p in segment)
        lo = min(p for _, p in segment)
        out.append(
            Excursion(
                day=session.day,
                start=start,
                minutes=minutes,
                up=math.log(hi / s0) / scale,
                down=math.log(s0 / lo) / scale,
            )
        )
    return out


def _covered(segment: Sequence[tuple[datetime, float]], start: datetime, end: datetime) -> bool:
    edges = [start, *(t for t, _ in segment), end]
    return all(
        (b - a).total_seconds() <= MAX_GAP_S for a, b in zip(edges, edges[1:])
    )


def pooled(excursions: Iterable[Excursion]) -> list[float]:
    """Both directions of every window: each is the barrier distance for one side."""
    values: list[float] = []
    for e in excursions:
        values.append(e.up)
        values.append(e.down)
    return values


def quantile(values: Sequence[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p * len(ordered)))]


def exceedance(values: Sequence[float], z: float) -> float:
    return sum(1 for v in values if v >= z) / len(values) if values else float("nan")


def hill_alpha(values: Sequence[float], fraction: float = HILL_FRACTION) -> float | None:
    """The Hill tail-index estimate from the top `fraction` of `values`. Pure.

    `alpha = 1 / mean(ln(x_i / u))` over the k largest, with `u` the (k+1)-th largest.
    None when there are too few values to form a tail.
    """
    ordered = sorted((v for v in values if v > 0.0), reverse=True)
    k = int(fraction * len(ordered))
    if k < 5 or k >= len(ordered):
        return None
    u = ordered[k]
    if u <= 0.0:
        return None
    mean_log = sum(math.log(x / u) for x in ordered[:k]) / k
    return None if mean_log <= 0.0 else 1.0 / mean_log


def bootstrap_alpha(
    by_day: dict[date, list[float]],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """A 90% interval for `hill_alpha`, resampling whole DAYS (the independent unit)."""
    days = sorted(by_day)
    if len(days) < 5:
        return None
    rng = random.Random(seed)
    estimates = []
    for _ in range(resamples):
        sample: list[float] = []
        for _ in days:
            sample.extend(by_day[rng.choice(days)])
        alpha = hill_alpha(sample)
        if alpha is not None:
            estimates.append(alpha)
    if len(estimates) < resamples // 2:
        return None
    estimates.sort()
    return (quantile(estimates, 0.05), quantile(estimates, 0.95))


def recommended_alpha(interval: tuple[float, float] | None, point: float | None) -> float:
    """`min(3.0, lower bound)` — never thinner than the inverse cubic law (rule 4)."""
    candidates = [MAX_FLATTEN_DEFER_TAIL_ALPHA]
    if interval is not None:
        candidates.append(interval[0])
    elif point is not None:
        candidates.append(point)
    return max(1.01, min(candidates))


def last_half_hour(session: Session, flatten_before_close_s: float) -> float | None:
    """The standardized max excursion from the flatten mark to the close, either way.

    Standardized as the runtime would at the mark: IV at the mark, RV trailing it,
    and the full remaining time. The backtest column: how close did a real path come
    to the deferral boundary?
    """
    mark = session.close - timedelta(seconds=flatten_before_close_s)
    times = [t for t, _ in session.path]
    s0 = _nearest(times, session.path, mark)
    iv = _nearest([t for t, _ in session.iv], session.iv, mark)
    rv = _trailing_rv(times, session.path, mark)
    if s0 is None or iv is None or rv is None:
        return None
    segment = [(t, p) for t, p in session.path if mark <= t <= session.close]
    if not segment:
        return None
    scale = max(iv, rv) * math.sqrt(
        flatten_before_close_s / 60.0 / MINUTES_PER_YEAR
    )
    hi = max(p for _, p in segment)
    lo = min(p for _, p in segment)
    return max(math.log(hi / s0), math.log(s0 / lo)) / scale


# -- I/O ----------------------------------------------------------------------


def _open_readonly(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise SystemExit(f"database not found: {db_path}")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def read_event_days(conn: sqlite3.Connection, start: str, end: str) -> set[date]:
    """Sessions on which the strategy recorded `event_blackout` (rule 1)."""
    days: set[date] = set()
    for (ts,) in conn.execute(
        "SELECT ts FROM audit_log WHERE category = 'signal.none' AND ts >= ? AND ts < ? "
        "AND json_extract(payload, '$.reason') = 'event_blackout'",
        (start, end),
    ):
        days.add(datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET).date())
    return days


def fetch_release_days(start: date, end: date) -> dict[str, list[date]]:
    """CPI/NFP/PCE release dates in [start, end] by family (rule 1), from FRED through
    `tools.blackout_dates`' own fetcher, so this tool and the blackout file read one
    source. Raises `BlackoutError` with no key or on any failed request."""
    api_key = _api_key()
    return {
        name: fetch_release_dates(release_id, api_key=api_key, start=start, end=end)
        for name, release_id in FRED_RELEASE_IDS.items()
    }


def empty_release_families(
    releases: dict[str, list[date]], start: date, end: date
) -> list[str]:
    """The families with no date over a span long enough that each must have one."""
    if (end - start).days <= RELEASE_GUARD_DAYS:
        return []
    return sorted(name for name, days in releases.items() if not days)


def excluded_days(
    days: Iterable[date], log_events: set[date], releases: dict[str, list[date]]
) -> dict[date, tuple[str, ...]]:
    """Every session day rule 1 excludes, with its sources: `event_blackout` from the
    log, then each release family FRED lists it under. A day with no source is kept."""
    out: dict[date, tuple[str, ...]] = {}
    for day in days:
        sources = (("event_blackout",) if day in log_events else ()) + tuple(
            name for name in sorted(releases) if day in releases[name]
        )
        if sources:
            out[day] = sources
    return out


@dataclass(frozen=True)
class FlattenTrigger:
    """One live `FLATTEN_BEFORE_CLOSE` rising edge, for the replay."""

    at: datetime
    candidate_id: str
    side: str | None
    short_strike: float | None
    width: float | None
    spot: float | None


def read_flatten_triggers(conn: sqlite3.Connection) -> list[FlattenTrigger]:
    """Every Tier 1 `watchdog.triggered` whose fired set includes the flatten."""
    out = []
    for ts, payload in conn.execute(
        "SELECT ts, payload FROM audit_log WHERE category = 'watchdog.triggered' "
        "ORDER BY seq"
    ):
        try:
            p = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if "flatten_before_close" not in (p.get("fired") or []):
            continue
        position = p.get("position") or {}
        short, long_ = position.get("short_symbol"), position.get("long_symbol")
        candidate = str(position.get("candidate_id") or "")
        side = "put" if "-put-" in candidate else "call" if "-call-" in candidate else None
        width = None if not (short and long_) else vertical_width(short, long_)
        out.append(
            FlattenTrigger(
                at=datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET),
                candidate_id=candidate,
                side=side,
                short_strike=None if not short else occ_strike(short),
                width=None if width is None else float(width),
                spot=p.get("spot") if isinstance(p.get("spot"), (int, float)) else None,
            )
        )
    return out


def replay(
    trigger: FlattenTrigger,
    session: Session | None,
    params: DeferralParams,
    flatten_before_close_s: float,
):
    """The arithmetic decision `decide` would make at a recorded flatten, with the
    recorded spot and strike and the session's recorded IV and trailing RV. The
    system-state conditions are assumed to hold — this replays the NUMBERS."""
    iv = rv = None
    if session is not None:
        iv = _nearest([t for t, _ in session.iv], session.iv, trigger.at)
        rv = _trailing_rv([t for t, _ in session.path], session.path, trigger.at)
    minutes = (
        flatten_before_close_s / 60.0 if session is None
        else max(0.0, (session.close - trigger.at).total_seconds() / 60.0)
    )
    return decide(
        DeferralInputs(
            side=trigger.side,
            spot=trigger.spot,
            short_strike=trigger.short_strike,
            width=trigger.width,
            sigma_implied=iv,
            sigma_realized=rv,
            minutes_to_close=minutes,
        ),
        params,
    )


def read_deferral_outcomes(conn: sqlite3.Connection) -> list[dict]:
    """Every `watchdog.flatten_deferral_outcome` row — the reliability dataset."""
    rows = []
    for (payload,) in conn.execute(
        "SELECT payload FROM audit_log WHERE category = 'watchdog.flatten_deferral_outcome' "
        "ORDER BY seq"
    ):
        try:
            rows.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return rows


def load_sessions(
    db_paths: Sequence[str], config: Config
) -> tuple[list[Session], set[date], list[dict], list[FlattenTrigger]]:
    """Every session across `db_paths` (the first database to hold a day wins),
    the event days, the deferral outcomes and the live flatten triggers."""
    from argus.marketcalendar import MarketCalendar

    calendar = MarketCalendar.from_config(config.market)
    sessions: dict[date, Session] = {}
    events: set[date] = set()
    outcomes: list[dict] = []
    triggers: list[FlattenTrigger] = []
    for db_path in db_paths:
        conn = _open_readonly(db_path)
        try:
            span = read_span(conn)
            if span is None:
                continue
            start, end = _utc_window(span[0].astimezone(ET).date(), span[1].astimezone(ET).date())
            prices = read_prices(conn, start, end)
            readings = read_readings(conn, start, end)
            events |= read_event_days(conn, start, end)
            outcomes.extend(read_deferral_outcomes(conn))
            triggers.extend(read_flatten_triggers(conn))
        finally:
            conn.close()
        iv_by_day: dict[date, list[tuple[datetime, float]]] = {}
        for r in readings:
            if r.atm_iv is not None and r.atm_iv > 0.0:
                iv_by_day.setdefault(r.trading_date, []).append((r.at_et, r.atm_iv))
        for day, path in prices.items():
            if day in sessions or day not in iv_by_day:
                continue
            ordered = tuple(sorted((t.astimezone(ET), p) for t, p in path))
            sessions[day] = Session(
                day=day,
                path=ordered,
                iv=tuple(sorted(iv_by_day[day])),
                close=calendar.close_datetime(day),
            )
    return [sessions[d] for d in sorted(sessions)], events, outcomes, triggers


# -- report -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        required=True,
        help="path to the config file (REQUIRED — there is no default). Its calendar, "
        "flatten mark, width and current k/z99/alpha are read; its database is used "
        "only when no --db is given",
    )
    parser.add_argument(
        "--db",
        action="append",
        help="a database to read (mode=ro); repeatable. Default: the config's db_path",
    )
    args = parser.parse_args(argv)
    config = Config.load(args.config)
    wd = config.watchdog
    db_paths = args.db or [config.audit.db_path]

    sessions, events, outcomes, triggers = load_sessions(db_paths, config)
    if not sessions:
        raise SystemExit("no sessions with a path and IV — no calibration is possible")
    first, last = sessions[0].day, sessions[-1].day
    try:
        releases = fetch_release_days(first, last)
    except BlackoutError as exc:
        raise SystemExit(f"cannot identify the release days (rule 1): {exc}") from None
    empty = empty_release_families(releases, first, last)
    if empty:
        raise SystemExit(
            f"FRED returned no {', '.join(empty)} release date over {first}..{last} — "
            "each publishes monthly, so the query is broken; refusing rather than "
            "pooling release days in (rule 1)"
        )
    excluded = excluded_days((s.day for s in sessions), events, releases)
    usable = [s for s in sessions if s.day not in excluded]
    print(f"flatten-deferral calibration — {len(db_paths)} database(s), read-only")
    print(
        f"  sessions with a path and IV: {len(sessions)}; event sessions excluded: "
        f"{len(sessions) - len(usable)}; used: {len(usable)}"
    )
    for day, sources in sorted(excluded.items()):
        print(f"    excluded {day}: {', '.join(sources)}")
    if len(usable) < 5:
        raise SystemExit("fewer than 5 usable sessions — no calibration is possible")
    print(f"  span: {usable[0].day} .. {usable[-1].day}")

    z99_by_length: dict[int, float] = {}
    primary: list[Excursion] = []
    print("\n  standardized max excursion, all-day windows (both directions pooled):")
    print(f"  {'L':>4} {'n':>7} {'p99':>6} {'p99.9':>6} {'max':>6}  P(z>=3)  P(z>=4)  gauss(4)")
    gauss4 = math.erfc(4.0 / math.sqrt(2.0))  # 2·Φ(−4)
    starts = {s.day: session_starts(s) for s in usable}
    for minutes in WINDOW_MINUTES:
        exc = [
            e for s in usable for e in session_excursions(s, minutes, starts[s.day])
        ]
        values = pooled(exc)
        if not values:
            continue
        z99_by_length[minutes] = quantile(values, 0.99)
        print(
            f"  {minutes:>4} {len(values):>7} {z99_by_length[minutes]:>6.2f} "
            f"{quantile(values, 0.999):>6.2f} {max(values):>6.2f}  "
            f"{exceedance(values, 3.0):.4f}   {exceedance(values, 4.0):.4f}   {gauss4:.5f}"
        )
        if minutes == PRIMARY_MINUTES:
            primary = exc
    if not z99_by_length or not primary:
        raise SystemExit("no measurable windows")
    z99 = max(z99_by_length.values())

    values = pooled(primary)
    by_day: dict[date, list[float]] = {}
    for e in primary:
        by_day.setdefault(e.day, []).extend((e.up, e.down))
    alpha_point = hill_alpha(values)
    interval = bootstrap_alpha(by_day)
    alpha = recommended_alpha(interval, alpha_point)
    non_overlap = pooled(e for e in primary if e.start.minute in (0, 30) and e.start.second == 0)
    print(f"\n  z99 (envelope across windows): {z99:.2f}")
    print(
        f"  Hill alpha ({PRIMARY_MINUTES}-min, top {HILL_FRACTION:.0%}): "
        + ("n/a" if alpha_point is None else f"{alpha_point:.2f}")
        + (
            "" if interval is None
            else f"; 90% day-bootstrap [{interval[0]:.2f}, {interval[1]:.2f}]"
        )
    )
    print(f"  recommended alpha = min({MAX_FLATTEN_DEFER_TAIL_ALPHA:g}, lower bound) = {alpha:.2f}")
    if non_overlap:
        print(
            f"  resolution: {len(non_overlap)} non-overlapping direction-windows; zero "
            f"exceedances certify a rate only down to ~{3.0 / len(non_overlap):.1e} (rule of "
            "three) — the tail beyond that is the model's"
        )

    width = config.signal.width
    k = wd.flatten_defer_insurance_multiple
    boundary = deferral_boundary(k=k, width=width, tail_z99=z99, tail_alpha=alpha)
    print(
        f"\n  deferral boundary at k={k:g}, width={width:g}: z >= {boundary:.2f} "
        f"(loaded config: z99={wd.flatten_defer_tail_z99:g}, alpha={wd.flatten_defer_tail_alpha:g})"
    )
    backtest = [
        (s.day, z) for s in usable
        if (z := last_half_hour(s, wd.flatten_before_close_s)) is not None
    ]
    if backtest:
        worst_day, worst = max(backtest, key=lambda r: r[1])
        print(
            f"  backtest: {len(backtest)} flatten windows; largest standardized move "
            f"mark->close {worst:.2f} ({worst_day}), {worst / boundary:.0%} of the boundary"
        )
    params = DeferralParams(
        insurance_multiple=k,
        tail_z99=z99,
        tail_alpha=alpha,
        buffer_points=wd.underlying_buffer_points,
    )
    by_day_session = {s.day: s for s in sessions}
    if triggers:
        print("\n  replay of every recorded flatten (the numbers; system state assumed OK):")
        for t in sorted(triggers, key=lambda t: t.at):
            d = replay(t, by_day_session.get(t.at.date()), params, wd.flatten_before_close_s)
            z = "?" if d.z is None else f"{d.z:.2f}"
            p = "?" if d.p_touch is None else f"{d.p_touch:.1e}"
            print(
                f"    {t.at:%Y-%m-%d %H:%M} {t.candidate_id:<26} z {z:>6}  P {p:>8}  "
                f"-> {'DEFER' if d.defer else 'flatten'} ({d.reason})"
            )
    touched = sum(1 for o in outcomes if o.get("touched"))
    print(
        f"  deferral outcomes on record: {len(outcomes)}; barrier touched after a "
        f"deferral: {touched}"
    )
    today = date.today().isoformat()
    print("\n  # TOML (paste into [watchdog] of every profile; a config-only release):")
    print(f"  flatten_defer_tail_z99 = {z99:.2f}")
    print(f"  flatten_defer_tail_alpha = {alpha:.2f}")
    print(
        f'  # flatten_defer_source: "tools.flatten_calibration {today}: {len(usable)} '
        f'non-event sessions {usable[0].day}..{usable[-1].day}; z99 envelope {z99:.2f}; '
        f'Hill alpha {"n/a" if alpha_point is None else f"{alpha_point:.2f}"}, capped '
        f'{alpha:.2f}; k={k:g} (policy)"'
    )


if __name__ == "__main__":
    main()
