"""Measure the watchdog's evaluability trail, and what the audit log costs.

READ-ONLY. Opens the database `mode=ro`, takes no lock, writes nothing — safe to
run mid-session against a live database (WAL readers never block the writer).

    ./.venv/bin/python -m argus.tools.evaluability --config argus.sandbox.toml
    ./.venv/bin/python -m argus.tools.evaluability --config argus.sandbox.toml --date 2026-08-14

TWO QUESTIONS, ONE RUN, because they have the same answer shape.

**Is the Tier 1 evaluability flap gone?** On 2026-08-13 the watchdog wrote 1,517
Tier 1 `watchdog.unevaluable` records across 15,711 checks — 27.8% of ticks
unevaluable, entering and leaving the state ~1,517 times. `_Escalation` is a pure
rising/falling edge with no hysteresis, so a flapping condition defeats it and
every flap is a Tier 1 write: awaited, holding the writer lock, fsync'd under
`audit.synchronous = "FULL"`. Fix C should have largely removed it by re-levelling
`degraded` (one trigger dark) to Tier 2 and reserving the page for `blind` (no
trigger evaluable) — but "should have" is not a measurement, and the decision on
whether a settle allowance is still owed has to be made from episode DURATIONS,
never from a guess. Same rule `watchdog.book_settle_s` was sized under, and the
same rule that says re-tune it from `watchdog.book_settled` rather than from this
document.

**What does the log actually write per second?** The config's justification for
`synchronous = "FULL"` is a write rate. The conclusion (FULL is cheap) does not
turn on the number, but a stale basis is a comment nobody can check.

Neither answer is a decision. This prints the evidence and the decision RULE; a
human picks the branch, and if the branch is a settle allowance, sizes it from
the durations printed here.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from argus.clock import ET, UTC_TS_FMT, et_day_bounds_utc_iso, et_today
from argus.config import Config

# The Tier 1 pair that opens and closes a BLIND episode — no trigger evaluable,
# nothing watching the position. These are the pages, and the flap that matters.
BLIND_OPENED = "watchdog.unevaluable"
BLIND_CLOSED = "watchdog.evaluable_again"
# Tier 2: blind sustained, and the milder "one trigger dark, backstop still live".
BLIND_SUSTAINED = "watchdog.unevaluable_still"
DEGRADED = "watchdog.degraded"
CHECK = "watchdog.check"

_EVALUABILITY = (BLIND_OPENED, BLIND_CLOSED, BLIND_SUSTAINED, DEGRADED, CHECK)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, UTC_TS_FMT).replace(tzinfo=timezone.utc)


@dataclass
class Episode:
    """One continuous stretch of a position being BLIND."""

    key: str
    opened_at: datetime
    closed_at: datetime | None = None
    reasons: tuple[str, ...] = ()

    @property
    def open_ended(self) -> bool:
        return self.closed_at is None

    def duration_s(self, fallback_end: datetime) -> float:
        """Seconds blind. An episode still open at the last record is measured to
        THERE and flagged — reporting it as zero-length would hide the worst case,
        which is a position that never recovered."""
        return ((self.closed_at or fallback_end) - self.opened_at).total_seconds()


@dataclass
class Position:
    """Every blind episode for one position, in order."""

    key: str
    episodes: list[Episode] = field(default_factory=list)

    def gaps_s(self) -> list[float]:
        """Seconds between recovering and going blind again — the flap signature.

        A settle allowance only helps when these are SHORT: it suppresses the page
        for a condition that clears on its own, and a position that goes blind
        once an hour is not flapping, it is failing repeatedly.
        """
        out = []
        for previous, current in zip(self.episodes, self.episodes[1:]):
            if previous.closed_at is not None:
                out.append((current.opened_at - previous.closed_at).total_seconds())
        return out


def read_counts(conn: sqlite3.Connection, start: str, end: str) -> dict[str, tuple[int, int]]:
    """{category: (tier, count)} for the evaluability categories."""
    rows = conn.execute(
        "SELECT category, tier, COUNT(*) FROM audit_log "
        f"WHERE category IN ({', '.join('?' * len(_EVALUABILITY))}) "
        "AND ts >= ? AND ts < ? GROUP BY category, tier",
        (*_EVALUABILITY, start, end),
    ).fetchall()
    return {category: (tier, count) for category, tier, count in rows}


def read_episodes(conn: sqlite3.Connection, start: str, end: str) -> list[Position]:
    """Reconstruct blind episodes from the Tier 1 rising/falling pair.

    Keyed on `external_identifier`, which is what `_Escalation` itself keys on —
    so "distinct episodes" here means exactly what it means in the watchdog, and
    two positions blind at once are two episodes rather than one.
    """
    rows = conn.execute(
        "SELECT seq, ts, category, payload FROM audit_log "
        "WHERE category IN (?, ?) AND ts >= ? AND ts < ? ORDER BY seq",
        (BLIND_OPENED, BLIND_CLOSED, start, end),
    ).fetchall()

    positions: dict[str, Position] = {}
    open_now: dict[str, Episode] = {}
    for _seq, ts, category, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        key = str(payload.get("external_identifier") or payload.get("candidate_id") or "?")
        position = positions.setdefault(key, Position(key=key))
        if category == BLIND_OPENED:
            if key in open_now:
                # Two rising edges with no recovery between them. `_Escalation`
                # cannot produce this; if it appears, the run spans a restart.
                continue
            episode = Episode(
                key=key,
                opened_at=_parse(ts),
                reasons=tuple(payload.get("unknown_reasons") or ()),
            )
            open_now[key] = episode
            position.episodes.append(episode)
        else:
            episode = open_now.pop(key, None)
            if episode is not None:
                episode.closed_at = _parse(ts)
    return sorted(positions.values(), key=lambda p: -len(p.episodes))


def read_rate(conn: sqlite3.Connection, start: str, end: str) -> dict[int, int]:
    """{tier: count} over the window. Tier 1 is the number that costs an fsync."""
    return {
        int(tier): int(count)
        for tier, count in conn.execute(
            "SELECT tier, COUNT(*) FROM audit_log WHERE ts >= ? AND ts < ? GROUP BY tier",
            (start, end),
        )
    }


def read_span(conn: sqlite3.Connection, start: str, end: str) -> tuple[datetime, datetime] | None:
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM audit_log WHERE ts >= ? AND ts < ?", (start, end)
    ).fetchone()
    if not row or row[0] is None:
        return None
    return (_parse(row[0]), _parse(row[1]))


# -- reporting ----------------------------------------------------------------


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile: the smallest observed value at or above rank q.

    `ceil`, not `round`: Python rounds halves to even, so a two-element p50 would
    land on the LARGER value and quietly overstate a duration a settle allowance
    gets sized from. Nearest-rank never interpolates, so every number printed is
    an episode that actually happened.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _report(
    trading_date: date,
    db_path: str,
    counts: dict[str, tuple[int, int]],
    positions: list[Position],
    tiers: dict[int, int],
    span: tuple[datetime, datetime] | None,
    cadence_s: float,
) -> None:
    # The database is named on every run, deliberately. `db_path` is CWD-relative
    # and this reads whatever `--config` points at; seeing which file the numbers
    # came from is worth one line.
    print(f"\n{trading_date}  —  {db_path}  (read-only)")
    if span is None:
        print("  no audit records on this date")
        return
    first, last = span
    elapsed = max((last - first).total_seconds(), 1e-9)
    print(
        f"  records span {first.astimezone(ET):%H:%M:%S} to "
        f"{last.astimezone(ET):%H:%M:%S} ET  ({elapsed / 3600:.2f}h)"
    )

    print("\nEVALUABILITY")
    labels = {
        CHECK: "every tick, carries unknown_reasons",
        BLIND_OPENED: "BLIND, rising edge         -> PAGES",
        BLIND_SUSTAINED: "blind, sustained",
        BLIND_CLOSED: "recovery                   -> closes a page",
        DEGRADED: "one trigger dark, backstop live",
    }
    for category in (CHECK, BLIND_OPENED, BLIND_SUSTAINED, BLIND_CLOSED, DEGRADED):
        tier, count = counts.get(category, (0, 0))
        tier_label = f"T{tier}" if tier else "  "
        print(f"  {category:<28} {tier_label}  {count:>8,}   {labels[category]}")

    ticks = counts.get(CHECK, (0, 0))[1]
    blind_ticks = counts.get(BLIND_OPENED, (0, 0))[1] + counts.get(BLIND_SUSTAINED, (0, 0))[1]
    if ticks:
        print(f"  -> {blind_ticks / ticks:.1%} of ticks blind ({blind_ticks:,} of {ticks:,})")

    episodes = [e for p in positions for e in p.episodes]
    durations = [e.duration_s(last) for e in episodes]
    unterminated = [e for e in episodes if e.open_ended]

    print(f"\nBLIND EPISODES  ({len(episodes):,} distinct, over {len(positions)} position(s))")
    if not episodes:
        print("  none — no position was ever fully unevaluable on this date")
    else:
        print(
            f"  duration   p50 {_pct(durations, 0.50):.1f}s   "
            f"p90 {_pct(durations, 0.90):.1f}s   "
            f"p99 {_pct(durations, 0.99):.1f}s   max {max(durations):.1f}s"
        )
        if unterminated:
            print(
                f"  !! {len(unterminated)} episode(s) NEVER RECOVERED — measured to the "
                "last record, so their durations are lower bounds"
            )
        for position in positions:
            gaps = position.gaps_s()
            each = [e.duration_s(last) for e in position.episodes]
            gap_note = (
                f"   gap between p50 {_pct(gaps, 0.50):.1f}s" if gaps else "   (single episode)"
            )
            print(
                f"    {position.key:<40} {len(position.episodes):>6} episode(s)"
                f"   median {statistics.median(each):.1f}s   max {max(each):.1f}s{gap_note}"
            )
        print("\n  every episode, opened ET -> duration:")
        for episode in sorted(episodes, key=lambda e: e.opened_at)[:200]:
            mark = " (never recovered)" if episode.open_ended else ""
            print(
                f"    {episode.opened_at.astimezone(ET):%H:%M:%S}  "
                f"{episode.duration_s(last):>7.1f}s  {episode.key}"
                f"  {','.join(episode.reasons) or '-'}{mark}"
            )
        if len(episodes) > 200:
            print(f"    ... {len(episodes) - 200:,} more")

    tier1, tier2 = tiers.get(1, 0), tiers.get(2, 0)
    total = tier1 + tier2
    print("\nAUDIT WRITE RATE")
    print(f"  Tier 1  {tier1:>9,}   {tier1 / elapsed:>6.2f}/s   awaited, chain lock, fsync under FULL")
    print(f"  Tier 2  {tier2:>9,}   {tier2 / elapsed:>6.2f}/s   queued")
    print(f"  total   {total:>9,}   {total / elapsed:>6.2f}/s")
    print(
        f"  -> the config's [audit] synchronous comment should cite "
        f"{total / elapsed:.1f} records/sec (Tier 1 alone: {tier1 / elapsed:.1f}/s)"
    )

    # The decision rule, printed rather than applied: this tool measures, a human
    # decides, and a threshold buried in code would quietly become the decision.
    print("\nTHE DECISION (docs/decisions/2026-08-13-live-fire-sandbox-pass.md, Fix D)")
    if not ticks:
        # THE FALSE NEGATIVE THIS GUARD EXISTS FOR. The watchdog only evaluates
        # OPEN positions, so a day that never filled writes no `watchdog.check`
        # at all — and "no blind episodes" on such a day is not evidence the flap
        # is gone, it is evidence there was nothing to be blind about. Reading it
        # as DONE would close the finding on a day that never tested it.
        print(
            "  INCONCLUSIVE — no `watchdog.check` records on this date, so no position\n"
            "  was ever watched. Zero blind episodes here says nothing about the flap.\n"
            "  Re-run against a session that actually HELD a position."
        )
    elif not episodes:
        print(
            f"  No blind episodes across {ticks:,} watched tick(s). Nothing to\n"
            "  escalate, nothing to settle: DONE."
        )
    elif len(episodes) <= 2 * len(positions):
        print(
            f"  {len(episodes)} episode(s) over {len(positions)} position(s) is not a flap.\n"
            "  DONE — record the measurement in the decision doc, no code change."
        )
    else:
        p90 = _pct(durations, 0.90)
        # Sized the way `book_settle_s` was: comfortably above the observed
        # episodes AND above the interval that governs re-observation, which for
        # the trigger evaluation is `watchdog.cadence_s`.
        suggested = max(4.0 * p90, 4.0 * cadence_s)
        print(
            f"  {len(episodes)} episode(s) over {len(positions)} position(s), p90 "
            f"{p90:.1f}s — this still flaps.\n"
            "  A settle allowance is owed, mirroring watchdog.book_settle_s: a new\n"
            "  [watchdog] key, TIME-based on wall time since the condition was first\n"
            "  observed, suppressed observations still writing a Tier 2 trail, 0\n"
            "  disabling it. It must NOT reuse book_settle_s — different condition,\n"
            f"  different urgency.\n  Sized from THESE durations: ~{suggested:.1f}s "
            f"(4x p90 {p90:.1f}s, floored at 4x cadence_s {cadence_s:g}s)."
        )


def _open_readonly(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise SystemExit(f"database not found: {db_path}")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


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
    parser.add_argument("--date", help="ET trading date (YYYY-MM-DD); default today")
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    trading_date = date.fromisoformat(args.date) if args.date else et_today()
    start, end = et_day_bounds_utc_iso(trading_date)

    conn = _open_readonly(config.audit.db_path)
    try:
        _report(
            trading_date,
            config.audit.db_path,
            read_counts(conn, start, end),
            read_episodes(conn, start, end),
            read_rate(conn, start, end),
            read_span(conn, start, end),
            config.watchdog.cadence_s,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
