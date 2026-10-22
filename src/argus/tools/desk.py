"""The desk view — what traded today, what it made, and what it cost to get done.

READ-ONLY. Opens the database `mode=ro`, takes no lock, writes nothing — safe to
run mid-session against a live database (WAL readers never block the writer), and
safe alongside the always-on supervisor, which holds the writer lock for its whole
life.

    ./.venv/bin/python -m argus.tools.desk --config argus.sandbox.toml
    ./.venv/bin/python -m argus.tools.desk --config argus.sandbox.toml --date 2026-08-17

**Not a second scorer.** `tools.mark` answers the research question — was firing
right? — and is organized around it: one row per trade IDEA, ranked, with the
re-proposal history and a settlement-based terminal value. This answers the desk
question instead: what am I holding, what did it make, and how much edge did the
walk give up getting filled. The two overlap on positions closed intraday and MUST
agree there; they are computed from the same fill records by the same functions,
so a disagreement is a bug in one of them and worth chasing rather than averaging.

**Where the numbers come from, and why.**

- **Entry and exit prices come from FILLS** (`order.fill`, via
  `shadow.marker.read_executions`), never from the order's limit. A limit is what
  was asked; a fill is what happened. This is the same reconstruction `tools.mark`
  scores from, reused rather than reimplemented — attribution is by
  `external_identifier`, never a join to the mutable `orders` table.
- **Open positions are marked at the watchdog's own last look** (`cost_to_close`),
  because that is the number the virtual stop is actually being evaluated against.
  Marking them any other way would report a position as safe or breached differently
  from the rail that governs it. **That look comes from EITHER mark category** —
  `watchdog.check` or `watchdog.check_spent` — see `MARK_CATEGORIES`.
- **An exit attempt that did not close the position is reported** (2026-09-14). A
  held row names the spent state, and a section below the day names every attempt
  with the broker's own reason for it. Before that this tool read `watchdog.check`
  alone and consulted the exit outcome only on a CLOSED row, so a position whose
  stop fired and whose every send failed rendered as an ordinary hold, marked at the
  instant its exit died — see `read_marks` and `read_exits`.
- **`entry_credit` on a `watchdog.check` is now the REALIZED credit** (2026-09-02,
  migration 0005) and carries an `entry_credit_basis` saying so. It was the ORDER
  LIMIT until then — at-worst-slightly-low, which turned out to be a half-tick to a
  tick every time it was checked — so the mark's `cost_to_close` is used and its
  `entry_credit` is still discarded in favour of the fill-derived one. That is now
  a CROSS-CHECK rather than a correction: on a `basis: realized` record the two are
  the same arithmetic over the same numbers down two different pipes, so a
  disagreement is a bug in one of them and worth chasing — the posture this tool
  already takes toward `mark`. On `basis: limit` (the broker priced no fills) they
  legitimately differ, and mixing them would still bias every unrealized number.

**What this tool will not do: settlement.** A 0DTE position held to expiry produces
no fill event — cash settlement is not a trade — so its terminal value is unknowable
from the ledger alone and needs the settlement print as an INPUT. That is
`tools.mark --from-feed`/`--settlement`, and this tool says so rather than
extrapolating the last intraday mark into a number that looks like a result.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path

from argus.clock import ET, UTC_TS_FMT, et_day_bounds_utc_iso, et_today
from argus.config import Config
from argus.shadow.marker import read_executions
from argus.shadow.outcome import Execution
from argus.signal.models import SPX_MULTIPLIER


CHECK = "watchdog.check"
CHECK_SPENT = "watchdog.check_spent"
EXIT_FINISHED = "watchdog.exit_finished"
ENTRY_FINISHED = "entry.finished"
GATE = "gate.decision"
SUBMITTED = "order.submitted"

#: Every category that carries a mark. BOTH, because a position whose exit attempts
#: are spent is still evaluated on every tick — only the SUBMISSION is suppressed
#: (2026-08-28) — and its mark moves from `watchdog.check` to `watchdog.check_spent`
#: at that moment and never moves back (`_attempted` is never pruned).
#:
#: Reading `watchdog.check` alone is what froze the 2026-09-14 report at 11:34:10,
#: the last tick before that position's exit was spent, discarding 10,656 later
#: marks — so the one row a desk most needs to watch was the one row that stopped
#: updating, at the exact instant it started to matter. Same class as the 2026-09-02
#: `/status` under-copy: a reader correct when written, silently wrong once the
#: runtime grew a state. Ordered by `seq` across both, so the newest look wins
#: whichever category it came from.
MARK_CATEGORIES = (CHECK, CHECK_SPENT)


def _et(ts: str) -> str:
    """A UTC chain timestamp as an ET wall clock, which is how a desk reads it."""
    parsed = datetime.strptime(ts, UTC_TS_FMT).replace(tzinfo=timezone.utc)
    return parsed.astimezone(ET).strftime("%H:%M:%S")


# -- what the audit log says --------------------------------------------------


@dataclass(frozen=True)
class Mark:
    """The watchdog's last look at one position — the live exit basis."""

    candidate_id: str
    contracts: int
    cost_to_close: float
    credit_stop: float | None
    at_et: str
    #: Triggers that could not be evaluated on that tick. Non-empty means the mark
    #: is real but the stop was partly dark when it was taken.
    unknown_reasons: tuple[str, ...]
    #: The spent exit attempt's outcome, if this position has no exit attempt left.
    #: `None` is an ordinary held position. Non-None means the watchdog is still
    #: evaluating and marking it every tick and will not submit for it again — so
    #: the mark is live but nothing is acting on it.
    #:
    #: Read from `watchdog.check_spent` INDEPENDENTLY of whether that tick could
    #: price the spread: "has this position an exit attempt left?" is not a question
    #: about quotes, and hanging it off the mark would lose it on any day whose last
    #: PRICED look happened to be the one before the exit was spent.
    exit_outcome: str | None = None


@dataclass(frozen=True)
class Entry:
    """One entry attempt's execution quality — the repricing cost of getting done."""

    candidate_id: str
    outcome: str
    requested_contracts: int
    filled_contracts: int | None
    steps: int
    final_limit_price: float | None
    at_et: str
    #: The broker's own account of a refused submit, on `no_order_exists` (2026-09-04).
    #: This table is where the gap was SEEN — 2026-09-03 rendered five identical
    #: `no_order_exists` rows with nothing to tell a permanent instrument refusal from
    #: a transient 502 — so it is where the answer belongs.
    mutation_error: str | None = None


@dataclass(frozen=True)
class ExitAttempt:
    """One `watchdog.exit_finished` — an exit that REACHED the broker.

    A list per position, not one: a position gets one ordinary attempt and, past
    the flatten mark, one further allowance, and each of those can end without a
    fill. On 2026-09-14 one position produced four, and the desk view named none of
    them because it consulted the exit outcome only on a CLOSED row.

    `NOT_ATTEMPTED` never appears here — nothing was sent, so the watchdog escalates
    it edge-triggered instead of writing a Tier 1 record per tick. The absence of a
    row is therefore not the absence of an exit; the held row's `exit_outcome` is
    what says the attempts are spent.
    """

    candidate_id: str
    outcome: str
    at_et: str
    #: What fired: the trigger reasons the exit was acting on.
    trigger_reasons: tuple[str, ...]
    #: The exit's own sentence about the outcome, where it has one.
    reason: str | None
    #: The broker's VERBATIM refusal, off the walk. Two fields, never one, on the
    #: `mutation_error` precedent: an order the broker accepted then rejected is a
    #: different fact from one it would not accept at all.
    reject_reason: str | None


@dataclass
class DeskDay:
    trading_date: date
    executions: dict[str, Execution] = field(default_factory=dict)
    marks: dict[str, Mark] = field(default_factory=dict)
    entries: list[Entry] = field(default_factory=list)
    exits: dict[str, list[ExitAttempt]] = field(default_factory=dict)
    denials: Counter = field(default_factory=Counter)
    allowed: int = 0
    fees: float = 0.0
    session_span: tuple[str, str] | None = None


@dataclass(frozen=True)
class HeldPosition:
    """Contracts still open, valued at the watchdog's last look — never settlement."""

    candidate_id: str
    contracts: int
    entry_credit: float
    #: `None` when the position was never marked. It then contributes NOTHING to
    #: `unrealized` rather than a zero: an unmarked position has no value, which is
    #: a different fact from a value of nothing.
    mark: Mark | None

    @property
    def points(self) -> float | None:
        return None if self.mark is None else self.entry_credit - self.mark.cost_to_close

    @property
    def value(self) -> float:
        pts = self.points
        return 0.0 if pts is None else pts * self.contracts * SPX_MULTIPLIER

    @property
    def spent_exit(self) -> str | None:
        """The spent attempt's outcome, or `None` for an ordinary hold."""
        return None if self.mark is None else self.mark.exit_outcome


@dataclass(frozen=True)
class ClosedPosition:
    """Contracts bought back before expiry, realized from actual fill prices."""

    candidate_id: str
    contracts: int
    entry_credit: float
    exit_debit: float
    last_outcome: str

    @property
    def points(self) -> float:
        return self.entry_credit - self.exit_debit

    @property
    def pnl(self) -> float:
        return self.points * self.contracts * SPX_MULTIPLIER


@dataclass(frozen=True)
class UnpriceablePosition:
    """A position the broker reported no fill prices for — carried, never guessed."""

    candidate_id: str
    why: str


@dataclass(frozen=True)
class DeskSummary:
    """The day's arithmetic, ONCE.

    Extracted from `_report` when `tools.eod` needed the same numbers for its Slack
    digest. Re-deriving them there would have made the digest a SECOND scorer of the
    day — the exact hazard this module's own docstring warns about between `desk`
    and `mark`, one layer down and with no test able to see the two diverge, because
    a digest and a page are never compared by anything. One computation, two
    renderings.
    """

    trading_date: date
    held: tuple[HeldPosition, ...]
    closed: tuple[ClosedPosition, ...]
    unpriceable: tuple[UnpriceablePosition, ...]
    fees: float

    @property
    def realized(self) -> float:
        return sum(c.pnl for c in self.closed)

    @property
    def unrealized(self) -> float:
        return sum(h.value for h in self.held)

    @property
    def total(self) -> float:
        return self.realized + self.unrealized + self.fees

    @property
    def held_contracts(self) -> int:
        return sum(h.contracts for h in self.held)

    @property
    def spent_exits(self) -> tuple[str, ...]:
        return tuple(h.candidate_id for h in self.held if h.spent_exit is not None)

    @property
    def flat(self) -> bool:
        """Nothing is still open — and an UNPRICEABLE execution refuses the claim.

        Fail-closed on purpose. "No fill prices were reported" is not "no contracts
        are held"; it is the state in which the ledger cannot say either way, and a
        flat check that answers `True` from an unreadable position is the
        reassuring-when-wrong failure this whole surface exists to remove.
        """
        return not self.held and not self.unpriceable


def summarize(day: DeskDay) -> DeskSummary:
    """Classify the day's executions and total them. Pure; no I/O."""
    held: list[HeldPosition] = []
    closed: list[ClosedPosition] = []
    unpriceable: list[UnpriceablePosition] = []

    for candidate, ex in sorted(day.executions.items()):
        if not ex.priced:
            why = "; ".join(ex.problems) if ex.problems else "no fill prices reported"
            unpriceable.append(UnpriceablePosition(candidate_id=candidate, why=why))
            continue
        credit = ex.entry_credit or 0.0

        if ex.closed_contracts > 0 and ex.exit_debit is not None:
            attempts = day.exits.get(candidate)
            closed.append(
                ClosedPosition(
                    candidate_id=candidate,
                    contracts=ex.closed_contracts,
                    entry_credit=credit,
                    exit_debit=ex.exit_debit,
                    last_outcome=attempts[-1].outcome if attempts else "closed",
                )
            )

        # NOT `elif`: a partly-closed position is both, and both terms count.
        if ex.expired_contracts > 0:
            held.append(
                HeldPosition(
                    candidate_id=candidate,
                    contracts=ex.expired_contracts,
                    entry_credit=credit,
                    mark=day.marks.get(candidate),
                )
            )

    return DeskSummary(
        trading_date=day.trading_date,
        held=tuple(held),
        closed=tuple(closed),
        unpriceable=tuple(unpriceable),
        fees=day.fees,
    )


def read_marks(conn: sqlite3.Connection, start: str, end: str) -> dict[str, Mark]:
    """The LAST mark per position on the date, from either `MARK_CATEGORIES`.

    Last rather than every one: this is a point-in-time desk view, and the most
    recent look is the only one that describes what is held now. Intraday that is
    a live mark; after the close it is the final intraday mark, which is NOT
    settlement — the report labels it accordingly.
    """
    marks: dict[str, Mark] = {}
    spent: dict[str, str] = {}
    for ts, category, payload in conn.execute(
        "SELECT ts, category, payload FROM audit_log "
        f"WHERE category IN ({','.join('?' * len(MARK_CATEGORIES))}) "
        "AND ts >= ? AND ts < ? ORDER BY seq",
        (*MARK_CATEGORIES, start, end),
    ):
        p = json.loads(payload)
        candidate = p.get("candidate_id")
        if candidate is None:
            continue
        if category == CHECK_SPENT:
            # Taken BEFORE the price guard below, deliberately: see `Mark.exit_outcome`.
            spent[candidate] = str(p.get("exit_outcome"))
        cost = p.get("cost_to_close")
        if cost is None:
            # A tick that could not price the spread at all. It is a real record and
            # the watchdog handles it, but it is not a mark and must not overwrite
            # the last one that was — reporting a stale price is better than
            # reporting none, and far better than reporting None as a number.
            continue
        # Ordered by seq, so the last write for a candidate is its latest look.
        marks[candidate] = Mark(
            candidate_id=candidate,
            contracts=int(p.get("contracts") or 0),
            cost_to_close=float(cost),
            credit_stop=p.get("credit_stop"),
            at_et=_et(ts),
            unknown_reasons=tuple(p.get("unknown_reasons") or ()),
        )
    return {
        cid: replace(m, exit_outcome=spent[cid]) if cid in spent else m
        for cid, m in marks.items()
    }


def read_entries(conn: sqlite3.Connection, start: str, end: str) -> list[Entry]:
    out: list[Entry] = []
    for ts, payload in conn.execute(
        "SELECT ts, payload FROM audit_log WHERE category = ? AND ts >= ? AND ts < ? "
        "ORDER BY seq",
        (ENTRY_FINISHED, start, end),
    ):
        p = json.loads(payload)
        out.append(
            Entry(
                candidate_id=p.get("candidate_id") or "?",
                outcome=str(p.get("outcome")),
                requested_contracts=int(p.get("requested_contracts") or 0),
                filled_contracts=p.get("filled_contracts"),
                steps=int(p.get("steps") or 0),
                final_limit_price=p.get("final_limit_price"),
                at_et=_et(ts),
                # `.get` on purpose: every `entry.finished` written before 2026-09-04
                # lacks the key, and a day that spans the deploy must still read.
                mutation_error=p.get("mutation_error"),
            )
        )
    return out


def read_exits(
    conn: sqlite3.Connection, start: str, end: str
) -> dict[str, list[ExitAttempt]]:
    """candidate_id -> every exit attempt that reached the broker, in order.

    Every attempt, not the last one: this used to return one outcome per position
    and was read only where a position had actually CLOSED, which made four failed
    attempts on a held position indistinguishable from none.
    """
    out: dict[str, list[ExitAttempt]] = {}
    for ts, payload in conn.execute(
        "SELECT ts, payload FROM audit_log WHERE category = ? AND ts >= ? AND ts < ? "
        "ORDER BY seq",
        (EXIT_FINISHED, start, end),
    ):
        p = json.loads(payload)
        candidate = (p.get("position") or {}).get("candidate_id")
        if not candidate:
            continue
        # `.get` throughout: this payload is `ExitReport.to_payload()` plus the
        # position, and a day spanning a deploy must still read.
        walk = p.get("walk") or {}
        out.setdefault(candidate, []).append(
            ExitAttempt(
                candidate_id=candidate,
                outcome=str(p.get("outcome")),
                at_et=_et(ts),
                trigger_reasons=tuple(str(r) for r in (p.get("trigger_reasons") or ())),
                reason=p.get("reason"),
                reject_reason=walk.get("reject_reason"),
            )
        )
    return out


def read_gate(conn: sqlite3.Connection, start: str, end: str) -> tuple[Counter, int]:
    denials: Counter = Counter()
    allowed = 0
    for (payload,) in conn.execute(
        "SELECT payload FROM audit_log WHERE category = ? AND ts >= ? AND ts < ?",
        (GATE, start, end),
    ):
        p = json.loads(payload)
        if p.get("allowed"):
            allowed += 1
        else:
            denials.update(p.get("reasons") or ())
    return denials, allowed


def read_fees(conn: sqlite3.Connection, start: str, end: str) -> float:
    """Fees the broker reported on every order it accepted today.

    Signed as the broker reports them (negative = charged), and summed across
    opens AND closes, so the net figure is the day's true frictional cost.
    """
    total = 0.0
    for (payload,) in conn.execute(
        "SELECT payload FROM audit_log WHERE category = ? AND ts >= ? AND ts < ?",
        (SUBMITTED, start, end),
    ):
        p = json.loads(payload)
        if p.get("dry_run"):
            continue
        total += float(p.get("fees") or 0.0)
    return total


def read_span(conn: sqlite3.Connection, start: str, end: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM audit_log WHERE ts >= ? AND ts < ?", (start, end)
    ).fetchone()
    if not row or row[0] is None:
        return None
    return (_et(row[0]), _et(row[1]))


def load(conn: sqlite3.Connection, trading_date: date) -> DeskDay:
    start, end = et_day_bounds_utc_iso(trading_date)
    denials, allowed = read_gate(conn, start, end)
    return DeskDay(
        trading_date=trading_date,
        executions=read_executions(conn, trading_date),
        marks=read_marks(conn, start, end),
        entries=read_entries(conn, start, end),
        exits=read_exits(conn, start, end),
        denials=denials,
        allowed=allowed,
        fees=read_fees(conn, start, end),
        session_span=read_span(conn, start, end),
    )


# -- the report ---------------------------------------------------------------


def _money(x: float) -> str:
    return f"{x:>12,.2f}"


def _report(day: DeskDay, db_path: str) -> None:
    print(f"\n{day.trading_date}  —  {db_path}  (read-only)")
    if day.session_span is None:
        print("  no records on this date\n")
        return
    print(f"  records span {day.session_span[0]} to {day.session_span[1]} ET\n")

    summary = summarize(day)
    realized = summary.realized
    unrealized = summary.unrealized
    closed_candidates = {c.candidate_id for c in summary.closed}
    any_spent = bool(summary.spent_exits)

    unpriced = [f"  {p.candidate_id:32} {p.why}" for p in summary.unpriceable]
    closed_rows = [
        f"  {c.candidate_id:32} {c.contracts:>5} {c.entry_credit:>8.2f} "
        f"{c.exit_debit:>8.2f} {c.points:>8.2f} {_money(c.pnl)}  {c.last_outcome}"
        for c in summary.closed
    ]

    held_rows: list[str] = []
    for h in summary.held:
        if h.mark is None:
            held_rows.append(
                f"  {h.candidate_id:32} {h.contracts:>5} {h.entry_credit:>8.2f} "
                f"{'—':>8} {'—':>8} {'(never marked)':>12}"
            )
            continue
        flag = " *" if h.mark.unknown_reasons else ""
        # The spent state rides on the ROW, not only in a section below it: a
        # position nothing will submit for again must not read as an ordinary
        # hold on the one line an operator actually scans.
        state = "" if h.spent_exit is None else f"  EXIT SPENT ({h.spent_exit})"
        held_rows.append(
            f"  {h.candidate_id:32} {h.contracts:>5} {h.entry_credit:>8.2f} "
            f"{h.mark.cost_to_close:>8.2f} {h.points:>8.2f} {_money(h.value)}  "
            f"@{h.mark.at_et}{flag}{state}"
        )

    print("OPEN / HELD  (marked at the watchdog's last look — NOT settlement)")
    if held_rows:
        print(f"  {'CANDIDATE':32} {'CONTR':>5} {'CREDIT':>8} {'COST':>8} "
              f"{'PTS':>8} {'VALUE $':>12}")
        for row in held_rows:
            print(row)
        print(f"  {'':32} {'':5} {'':8} {'':8} {'unrealized':>8} {_money(unrealized)}")
        if any_spent:
            print(
                "\n  EXIT SPENT means that position's exit attempt is used. It is still\n"
                "  evaluated and marked every tick — the mark above is LIVE — but a\n"
                "  further send happens only under a retry budget (nothing_at_broker,\n"
                "  cross_unpriceable) or the flatten's one-shot allowance. Where none of\n"
                "  those applies, closing it is a MANUAL action: a retry IS the\n"
                "  double-close, which is why the watchdog will not do it."
            )
    else:
        print("  none")

    print("\nCLOSED INTRADAY  (realized from actual fill prices)")
    if closed_rows:
        print(f"  {'CANDIDATE':32} {'CONTR':>5} {'CREDIT':>8} {'DEBIT':>8} "
              f"{'PTS':>8} {'P&L $':>12}")
        for row in closed_rows:
            print(row)
        print(f"  {'':32} {'':5} {'':8} {'':8} {'realized':>8} {_money(realized)}")
    else:
        print("  none — nothing was closed before expiry")

    if unpriced:
        print("\nNOT PRICEABLE  (carried, never guessed)")
        for row in unpriced:
            print(row)

    # Attempts on positions that did NOT end up closed. On 2026-09-14 there were
    # four of them on one position — the credit stop at 11:33:10 and the 15:30
    # flatten among them, every send lost at the transport layer — and this report
    # showed a quiet held row and "nothing was closed before expiry".
    unclosed = [
        a
        for cid, attempts in sorted(day.exits.items())
        if cid not in closed_candidates
        for a in attempts
    ]
    if unclosed:
        print("\nEXITS THAT DID NOT CLOSE THE POSITION")
        print(f"  {'CANDIDATE':32} {'OUTCOME':18} TIME      TRIGGER")
        for a in unclosed:
            print(f"  {a.candidate_id:32} {a.outcome:18} {a.at_et}  "
                  f"{','.join(a.trigger_reasons) or '—'}")
        # Below the table, on the `WHY THE BROKER REFUSED` precedent: broker prose
        # is long and varies, and the operative question is which of these share a
        # cause — which reads as a list, not as a truncated column.
        explained = [a for a in unclosed if a.reason or a.reject_reason]
        if explained:
            print("\n  WHY")
            for a in explained:
                why = " / ".join(x for x in (a.reason, a.reject_reason) if x)
                print(f"  {a.candidate_id:32} {a.at_et}  {why}")

    print("\nDAY")
    print(f"  realized (closed intraday)        {_money(realized)}")
    print(f"  unrealized (held, last mark)      {_money(unrealized)}")
    print(f"  fees on filled orders             {_money(day.fees)}")
    print(f"  {'-' * 46}")
    print(f"  total                             {_money(summary.total)}")
    if summary.held:
        print(
            "\n  NB positions still held are marked at the last intraday quote, which is\n"
            "  not settlement. 0DTE expiry produces no fill event, so their terminal\n"
            "  value needs the settlement print:\n"
            "      python -m argus.tools.mark --dry-run --from-feed"
        )

    print("\nEXECUTION QUALITY  (steps = reprices needed to get filled)")
    if day.entries:
        print(f"  {'CANDIDATE':32} {'OUTCOME':10} {'REQ':>4} {'FILL':>5} "
              f"{'STEPS':>6} {'LIMIT':>8}  TIME")
        for e in day.entries:
            filled = "—" if e.filled_contracts is None else str(e.filled_contracts)
            limit = "—" if e.final_limit_price is None else f"{e.final_limit_price:.2f}"
            print(f"  {e.candidate_id:32} {e.outcome:10} {e.requested_contracts:>4} "
                  f"{filled:>5} {e.steps:>6} {limit:>8}  {e.at_et}")
        total_steps = sum(e.steps for e in day.entries)
        print(f"  {len(day.entries)} entry attempt(s), {total_steps} reprice(s) total")
        # Printed BELOW the table rather than as a column: the broker's prose is
        # long and varies, and the operative question is "which of these are the
        # same cause?" — which reads better as a list than as a truncated column.
        refused = [e for e in day.entries if e.mutation_error]
        if refused:
            print("\n  WHY THE BROKER REFUSED  (no order was created)")
            for e in refused:
                print(f"  {e.candidate_id:32} {e.mutation_error}")
    else:
        print("  no entry attempts")

    print("\nGATE")
    print(f"  allowed                           {day.allowed:>12,}")
    for reason, n in day.denials.most_common():
        print(f"  denied: {reason:26} {n:>12,}")
    if not day.denials:
        print("  no denials")
    print()


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

    conn = _open_readonly(config.audit.db_path)
    try:
        _report(load(conn, trading_date), config.audit.db_path)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
