"""Replay the mid-implied-delta solver over recorded live quotes — THE PRE-DEPLOY GATE.

→ docs/decisions/2026-09-21-short-strike-selection-on-mid-implied-delta.md (Rollout)

READ-ONLY against the database. Opens it `mode=ro`, takes no lock, writes nothing
to it — safe mid-session against a live database (WAL readers never block the
writer). The ONE file it can write is a fixture, and only when asked to.

    # Run the gate against any recorded day
    ./.venv/bin/python -m argus.tools.replay_selection --config argus.live.toml --date 2026-09-21

    # Extract the committed CI fixture from a SNAPSHOT of the live database
    ./.venv/bin/python -m argus.tools.replay_selection --db dev/nas/<snapshot>.db \\
        --date 2026-09-21 --expect-entries 0.182,0.197,0.182,0.151,0.193 \\
        --extract-fixture tests/fixtures/selection_replay_2026-09-21.json

    # Run the gate against a fixture (no config, no database)
    ./.venv/bin/python -m argus.tools.replay_selection --fixture tests/fixtures/...json

WHY A REPLAY AND NOT A RECORD-ONLY LIVE PHASE. The case for selecting on the mid is
made in the decision record; what remains is the ordinary risk of new arithmetic
that trades money — a wrong sign on puts, the wrong time on a half-day, a solver
that fails on odd quotes. A record-only phase catches those only by spending more
sessions trading on the selection the change removes. Since 2026-09-21 every
`signal.candidate` and every near-miss `signal.none` carries each selected leg's
raw bid and ask, several thousand legs a session — so the same check runs before
the image ships.

WHAT IT CANNOT DO, AND SAYS SO. A recorded row holds the two legs chosen for one
side, never the chain. So (a) no put-call-parity forward can be formed and every
leg is inverted against SPOT — the runtime's documented fallback path, and the
reason criterion 2's residual is allowed to be nonzero; and (b) the full strike
search cannot be re-run. The search is covered by the unit tests on synthetic
chains (`tests/test_strategy_mid_delta.py`).

THE ARITHMETIC IS THE RUNTIME'S, NOT A COPY. Every leg goes through
`signal.implied.solve_leg` and `years_to_settlement`; criterion 4 runs the real
`CreditSpreadStrategy` over each row's reconstructed legs and encodes what it
would have written exactly as the audit log would (`allow_nan=False`).
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Any, Iterable

from argus.clock import et_day_bounds_utc_iso
from argus.config import SHORT_DELTA_SOURCES, Config, SignalConfig
from argus.signal.implied import MidDelta, Refusal, solve_leg, years_to_settlement
from argus.signal.models import CALL, PUT, ChainSnapshot, OptionQuote, RegimeSnapshot
from argus.signal.strategy import CreditSpreadStrategy

CANDIDATE = "signal.candidate"
NONE = "signal.none"
GATE_DECISION = "gate.decision"
WALK_STARTED = "walk.started"

FIXTURE_FORMAT = 1
#: One row per recorded LEG. Public market quotes and the clock only — nothing
#: about the account, the order, or the position.
FIXTURE_COLUMNS = (
    "seq", "kind", "entered", "side", "role", "strike",
    "bid", "ask", "feed_delta", "greeks_age_s", "spot", "minutes",
)

# -- the gate's criteria (decision record, "The pre-deploy gate") ------------
#: 1. Share of recorded two-sided out-of-the-money legs the solver must invert.
MIN_SOLVE_RATE = 0.99
#: 2. Put short legs whose greeks were at most this old...
FRESH_GREEKS_S = 10.0
#: ...must show no systematic offset from the feed: |median(|mid| - |feed|)| at
#: most this. The CONVENTION check — on the side where the feed has agreed with
#: the mids. It is the SIGNED median, deliberately (amended 2026-09-21): a wrong
#: put sign, time basis or forward is a BIAS, and bias is what a signed median
#: measures. The median of the ABSOLUTE gap measures dispersion instead — half-
#: spread noise plus the feed's own vol model — which sat at ~0.005 on 2026-09-21
#: whatever the greeks' age or the forward, so it cannot tell a convention error
#: from an ordinary quote. It is still printed; it is not gated.
MAX_PUT_MEDIAN_GAP = 0.005
#: 3. Each entry's short mid delta must reproduce the recorded value to this.
ENTRY_TOLERANCE = 0.001

#: The signal parameters criterion 4 runs the real strategy with when no config
#: is given (fixture mode). Selection parameters are irrelevant to ENCODABILITY,
#: which is all that criterion asks; these are the live values for readability.
_REPLAY_SIGNAL = dict(
    sides=(PUT, CALL),
    target_short_delta=0.15,
    width=5.0,
    max_width=10.0,
    min_credit=0.50,
    entry_earliest=time(10, 0),
    entry_latest=time(15, 0),
    cadence_s=5.0,
)


@dataclass(frozen=True, slots=True)
class ReplayLeg:
    """One recorded leg, as the gate reads it."""

    seq: int
    kind: str                   # CANDIDATE | NONE
    #: True for a candidate the gate allowed AND whose opening walk started — an
    #: order was actually handed to the broker path (never in shadow).
    entered: bool
    side: str
    role: str                   # "short" | "long"
    strike: float
    bid: float
    ask: float
    feed_delta: float | None
    greeks_age_s: float | None  # the SHORT leg only carries one
    spot: float | None
    minutes: float | None

    def quote(self) -> OptionQuote:
        return OptionQuote(
            symbol=f"{self.side}-{self.strike:g}",
            strike=self.strike,
            right=self.side,
            delta=self.feed_delta,
            bid=self.bid,
            ask=self.ask,
            greeks_age_s=self.greeks_age_s if self.greeks_age_s is not None else math.inf,
        )


# -- reading ------------------------------------------------------------------


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _minutes(payload: dict) -> float | None:
    regime = payload.get("regime")
    return _num(regime.get("minutes_to_settlement")) if isinstance(regime, dict) else None


def _candidate_legs(seq: int, payload: dict, entered: bool) -> list[ReplayLeg]:
    side = payload.get("side")
    spot, minutes = _num(payload.get("underlying_price")), _minutes(payload)
    legs = []
    for role in ("short", "long"):
        leg = payload.get(role)
        if not isinstance(leg, dict):
            continue
        strike, bid, ask = _num(leg.get("strike")), _num(leg.get("bid")), _num(leg.get("ask"))
        if strike is None or bid is None or ask is None:
            continue    # recorded before 2026-09-15: no market on the row
        legs.append(ReplayLeg(
            seq, CANDIDATE, entered, side, role, strike, bid, ask,
            _num(leg.get("delta")),
            _num(leg.get("greeks_age_s")) if role == "short" else None,
            spot, minutes,
        ))
    return legs


def _near_miss_legs(seq: int, payload: dict) -> list[ReplayLeg]:
    """A `signal.none` row carries a market only on its NEAR-MISS sides — flat keys
    (`short_bid`, `long_ask`, ...), never the nested candidate shape."""
    per_side = payload.get("per_side")
    if not isinstance(per_side, dict):
        return []
    spot, minutes = _num(payload.get("underlying_price")), _minutes(payload)
    legs = []
    for side, detail in sorted(per_side.items()):
        if not isinstance(detail, dict):
            continue
        for role in ("short", "long"):
            strike = _num(detail.get(f"{role}_strike"))
            bid, ask = _num(detail.get(f"{role}_bid")), _num(detail.get(f"{role}_ask"))
            if strike is None or bid is None or ask is None:
                continue
            legs.append(ReplayLeg(
                seq, NONE, False, side, role, strike, bid, ask,
                _num(detail.get(f"{role}_delta")),
                _num(detail.get("short_greeks_age_s")) if role == "short" else None,
                spot, minutes,
            ))
    return legs


def read_legs(conn: sqlite3.Connection, trading_date: date) -> list[ReplayLeg]:
    """Every recorded leg with a market on `trading_date` (ET), in `seq` order.

    `entered` is decided in ONE pass over the day in `seq` order: a candidate's gate
    decision and its walk both follow it before the next candidate can exist,
    because `SignalLoop.evaluate_once` awaits the whole submit path inline.
    """
    start, end = et_day_bounds_utc_iso(trading_date)
    rows = conn.execute(
        "SELECT seq, category, payload FROM audit_log "
        "WHERE ts >= ? AND ts < ? AND category IN (?, ?, ?, ?) ORDER BY seq",
        (start, end, CANDIDATE, NONE, GATE_DECISION, WALK_STARTED),
    ).fetchall()

    legs: list[ReplayLeg] = []
    pending: tuple[int, dict] | None = None     # the candidate awaiting its verdict
    allowed = False

    def flush(entered: bool) -> None:
        nonlocal pending
        if pending is not None:
            legs.extend(_candidate_legs(pending[0], pending[1], entered))
            pending = None

    for seq, category, raw in rows:
        payload = json.loads(raw)
        if category == CANDIDATE:
            flush(False)
            pending, allowed = (seq, payload), False
        elif category == GATE_DECISION and pending is not None:
            allowed = bool(payload.get("allowed"))
            if not allowed:
                flush(False)
        elif category == WALK_STARTED and pending is not None and allowed:
            if payload.get("direction") == "open":
                flush(True)
        elif category == NONE:
            legs.extend(_near_miss_legs(seq, payload))
    flush(False)
    legs.sort(key=lambda leg: (leg.seq, leg.role != "short"))
    return legs


# -- the fixture ---------------------------------------------------------------


def to_fixture(trading_date: date, legs: Iterable[ReplayLeg]) -> dict[str, Any]:
    return {
        "format": FIXTURE_FORMAT,
        "trading_date": trading_date.isoformat(),
        "source": (
            "signal.candidate + signal.none near-miss legs, extracted read-only by "
            "argus.tools.replay_selection. Public market quotes only."
        ),
        "columns": list(FIXTURE_COLUMNS),
        "legs": [[getattr(leg, c) for c in FIXTURE_COLUMNS] for leg in legs],
    }


def from_fixture(data: dict[str, Any]) -> tuple[date, list[ReplayLeg]]:
    if data.get("format") != FIXTURE_FORMAT or tuple(data.get("columns", ())) != FIXTURE_COLUMNS:
        raise ValueError(
            f"unrecognised replay fixture (format {data.get('format')!r}); "
            f"re-extract it with --extract-fixture"
        )
    return (
        date.fromisoformat(data["trading_date"]),
        [ReplayLeg(*row) for row in data["legs"]],
    )


def load_fixture(path: Path) -> tuple[date, list[ReplayLeg]]:
    return from_fixture(json.loads(path.read_text()))


# -- replay --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegResult:
    leg: ReplayLeg
    #: The runtime's answer. None when the leg was not ASKED: no remaining life,
    #: no spot, or in the money relative to spot (the runtime asks OTM legs only).
    outcome: MidDelta | Refusal | None


@dataclass
class ReplayReport:
    trading_date: date
    results: list[LegResult] = field(default_factory=list)
    #: Rows whose reconstructed payload the audit log would have REFUSED.
    unencodable: list[str] = field(default_factory=list)
    payload_rows: int = 0

    # -- criterion 1 --------------------------------------------------------
    @property
    def two_sided_otm(self) -> list[LegResult]:
        return [
            r for r in self.results
            if r.outcome is not None and r.outcome is not Refusal.ONE_SIDED
        ]

    @property
    def solve_rate(self) -> float | None:
        asked = self.two_sided_otm
        if not asked:
            return None
        return sum(isinstance(r.outcome, MidDelta) for r in asked) / len(asked)

    def refusals(self) -> dict[str, int]:
        counts = {r.value: 0 for r in Refusal}
        for r in self.results:
            if isinstance(r.outcome, Refusal):
                counts[r.outcome.value] += 1
        return counts

    # -- criterion 2 --------------------------------------------------------
    def gaps(self, side: str, *, max_age_s: float | None = None) -> list[float]:
        """|mid delta| - |feed delta| on SHORT legs — positive means the feed
        UNDER-states the true delta (the strike is really closer to the money)."""
        out = []
        for r in self.results:
            leg = r.leg
            if (
                leg.role != "short" or leg.side != side
                or not isinstance(r.outcome, MidDelta) or leg.feed_delta is None
            ):
                continue
            if max_age_s is not None and not (
                leg.greeks_age_s is not None and leg.greeks_age_s <= max_age_s
            ):
                continue
            out.append(abs(r.outcome.delta) - abs(leg.feed_delta))
        return out

    @property
    def put_fresh_median_gap(self) -> float | None:
        """Criterion 2's statistic: the SIGNED median gap (see `MAX_PUT_MEDIAN_GAP`)."""
        gaps = self.gaps(PUT, max_age_s=FRESH_GREEKS_S)
        return statistics.median(gaps) if gaps else None

    @property
    def put_fresh_median_abs_gap(self) -> float | None:
        """The dispersion beside it — reported, never gated."""
        gaps = self.gaps(PUT, max_age_s=FRESH_GREEKS_S)
        return statistics.median(abs(g) for g in gaps) if gaps else None

    # -- criterion 3 --------------------------------------------------------
    @property
    def entries(self) -> list[LegResult]:
        return [r for r in self.results if r.leg.entered and r.leg.role == "short"]


def _outcome(leg: ReplayLeg) -> MidDelta | Refusal | None:
    t_years = years_to_settlement(leg.minutes) if leg.minutes is not None else None
    spot = leg.spot
    if t_years is None or spot is None or not (math.isfinite(spot) and spot > 0.0):
        return None
    if not (leg.strike > spot if leg.side == CALL else leg.strike < spot):
        return None
    return solve_leg(leg.quote(), spot, t_years)


def _encode_row(
    rows: list[ReplayLeg], signal_cfg: dict[str, Any], report: ReplayReport
) -> None:
    """Criterion 4: run the REAL strategy, in both modes, over this row's legs and
    encode what it would have written, exactly as the audit log encodes."""
    first = rows[0]
    if first.minutes is None or first.spot is None:
        return
    chain = ChainSnapshot(
        "SPXW", first.spot, tuple(leg.quote() for leg in rows), captured_at=0.0
    )
    regime = RegimeSnapshot(
        atm_iv=math.nan, skew=math.nan, today=report.trading_date,
        minutes_to_settlement=first.minutes,
    )
    for source in SHORT_DELTA_SOURCES:
        cfg = SignalConfig(**{**signal_cfg, "sides": (first.side,), "short_delta_source": source})
        result = CreditSpreadStrategy(cfg).evaluate(chain, cfg.entry_earliest, regime)
        payload = result.signal.to_payload() if result.signal else result.detail
        report.payload_rows += 1
        try:
            json.dumps(payload, allow_nan=False)
        except ValueError as exc:
            report.unencodable.append(f"seq {first.seq} {first.side} {source}: {exc}")


def replay(
    trading_date: date,
    legs: list[ReplayLeg],
    *,
    signal: SignalConfig | None = None,
) -> ReplayReport:
    report = ReplayReport(trading_date)
    report.results = [LegResult(leg, _outcome(leg)) for leg in legs]

    signal_cfg = dict(_REPLAY_SIGNAL)
    if signal is not None:
        signal_cfg.update(
            target_short_delta=signal.target_short_delta, width=signal.width,
            max_width=signal.max_width, min_credit=signal.min_credit,
            entry_earliest=signal.entry_earliest, entry_latest=signal.entry_latest,
        )
    by_row: dict[tuple[int, str], list[ReplayLeg]] = {}
    for leg in legs:
        by_row.setdefault((leg.seq, leg.side), []).append(leg)
    for rows in by_row.values():
        _encode_row(rows, signal_cfg, report)
    return report


# -- the gate ------------------------------------------------------------------


def gate_failures(
    report: ReplayReport, expected_entries: list[float] | None = None
) -> list[str]:
    """Every criterion that did not pass, as a sentence. Empty means PASS.

    `expected_entries` are the recorded short mid deltas of the day's entries, in
    order; None skips criterion 3 (a day with no documented entries)."""
    failures = []
    rate = report.solve_rate
    if rate is None:
        failures.append("criterion 1: no two-sided out-of-the-money legs to replay")
    elif rate < MIN_SOLVE_RATE:
        failures.append(
            f"criterion 1: solver inverted {rate:.2%} of two-sided OTM legs "
            f"(< {MIN_SOLVE_RATE:.0%}); refusals {report.refusals()}"
        )
    median = report.put_fresh_median_gap
    if median is None:
        failures.append(
            f"criterion 2: no put short legs with greeks <= {FRESH_GREEKS_S:g}s old"
        )
    elif abs(median) > MAX_PUT_MEDIAN_GAP:
        failures.append(
            f"criterion 2: put fresh-greeks signed median (|mid| - |feed|) "
            f"{median:+.4f} is outside +/-{MAX_PUT_MEDIAN_GAP}"
        )
    if expected_entries is not None:
        got = [
            abs(r.outcome.delta) if isinstance(r.outcome, MidDelta) else None
            for r in report.entries
        ]
        if len(got) != len(expected_entries) or any(
            g is None or abs(g - e) > ENTRY_TOLERANCE for g, e in zip(got, expected_entries)
        ):
            shown = [round(g, 4) if g is not None else None for g in got]
            failures.append(
                f"criterion 3: entry short mid deltas {shown} do not reproduce "
                f"{expected_entries} to +/-{ENTRY_TOLERANCE}"
            )
    if report.unencodable:
        failures.append(
            f"criterion 4: {len(report.unencodable)} of {report.payload_rows} "
            f"reconstructed payloads would be REFUSED: {report.unencodable[:3]}"
        )
    return failures


def render(report: ReplayReport, failures: list[str]) -> str:
    lines = [f"Mid-implied delta replay — {report.trading_date.isoformat()}"]
    asked = report.two_sided_otm
    one_sided = sum(r.outcome is Refusal.ONE_SIDED for r in report.results)
    not_asked = sum(r.outcome is None for r in report.results)
    lines.append(
        f"  legs {len(report.results)}: two-sided OTM {len(asked)}, one-sided {one_sided}, "
        f"not asked (ITM / no time / no spot) {not_asked}"
    )
    rate = report.solve_rate
    lines.append(
        f"  [1] solved {rate:.2%}" if rate is not None else "  [1] solved n/a"
    )
    lines.append(f"      refusals {report.refusals()}")
    for side in (PUT, CALL):
        all_gaps, fresh = report.gaps(side), report.gaps(side, max_age_s=FRESH_GREEKS_S)
        if all_gaps:
            lines.append(
                f"  {side:>4} short |mid|-|feed|: mean {statistics.fmean(all_gaps):+.4f} "
                f"(n={len(all_gaps)}); greeks <= {FRESH_GREEKS_S:g}s median "
                + (f"{statistics.median(fresh):+.4f}, median |gap| "
                   f"{statistics.median(abs(g) for g in fresh):.4f} (n={len(fresh)})"
                   if fresh else "n/a")
            )
    median, spread = report.put_fresh_median_gap, report.put_fresh_median_abs_gap
    lines.append(
        f"  [2] put fresh-greeks signed median gap "
        + (f"{median:+.4f}" if median is not None else "n/a")
        + f" (bound +/-{MAX_PUT_MEDIAN_GAP}); dispersion, not gated: median |gap| "
        + (f"{spread:.4f}" if spread is not None else "n/a")
    )
    lines.append(f"  [3] entries {len(report.entries)}:")
    for r in report.entries:
        mid = f"{abs(r.outcome.delta):.4f}" if isinstance(r.outcome, MidDelta) else "—"
        feed = f"{abs(r.leg.feed_delta):.4f}" if r.leg.feed_delta is not None else "—"
        lines.append(
            f"      seq {r.leg.seq} {r.leg.side} {r.leg.strike:g}: mid {mid}  feed {feed}"
        )
    lines.append(
        f"  [4] reconstructed payloads {report.payload_rows}, refused {len(report.unencodable)}"
    )
    lines.append("  GATE: " + ("PASS" if not failures else "FAIL"))
    lines.extend(f"    - {f}" for f in failures)
    return "\n".join(lines)


def _open_readonly(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise SystemExit(f"database not found: {db_path}")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--config",
        help="path to the config file whose audit DB to read (there is no default; "
        "a default path is how the live database gets opened by accident)",
    )
    parser.add_argument(
        "--db",
        help="an audit database FILE to read instead of a config's (a pulled "
        "snapshot); opened read-only like any other",
    )
    parser.add_argument("--date", help="ET trading date to replay (YYYY-MM-DD)")
    parser.add_argument("--fixture", help="replay a committed fixture instead of a database")
    parser.add_argument(
        "--extract-fixture", metavar="PATH",
        help="also write the day's legs as a replay fixture (market quotes only)",
    )
    parser.add_argument(
        "--expect-entries",
        help="comma-separated short mid deltas the day's entries must reproduce "
        "(criterion 3), e.g. 0.182,0.197,0.182,0.151,0.193",
    )
    args = parser.parse_args(argv)

    signal = None
    if args.fixture:
        if args.config or args.db or args.date or args.extract_fixture:
            parser.error(
                "--fixture replays a fixture; it takes no --config/--db/--date/--extract-fixture"
            )
        trading_date, legs = load_fixture(Path(args.fixture))
    else:
        if bool(args.config) == bool(args.db) or not args.date:
            parser.error("give --date and exactly one of --config / --db (or --fixture)")
        if args.config:
            config = Config.load(args.config)
            signal, db_path = config.signal, config.audit.db_path
        else:
            db_path = args.db
        trading_date = date.fromisoformat(args.date)
        conn = _open_readonly(db_path)
        try:
            legs = read_legs(conn, trading_date)
        finally:
            conn.close()
        if args.extract_fixture:
            path = Path(args.extract_fixture)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(to_fixture(trading_date, legs), separators=(",", ":")) + "\n")
            print(f"wrote {len(legs)} legs to {path}")

    expected = (
        [float(x) for x in args.expect_entries.split(",")] if args.expect_entries else None
    )
    report = replay(trading_date, legs, signal=signal)
    failures = gate_failures(report, expected)
    print(render(report, failures))
    return 0 if not failures else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
