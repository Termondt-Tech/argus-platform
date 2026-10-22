"""The trading ledger: every fill, every trade idea and every trading day, as CSV.

READ-ONLY. Opens the database `mode=ro`, takes no lock, writes nothing to it.

    ./.venv/bin/python -m argus.tools.ledger --config argus.dev.toml --out-dir dev/ledger

It is the data under the Google Sheet (docs/decisions/2026-09-22-ledger-and-sheets.md):
three CSVs, rebuilt IN FULL on every run from the first record in the database to
the latest settled session, so a re-run or a catch-up produces the same files and
a correction (a settlement posting, a fee finalising) flows in on the next run
without any "update the old row" logic.

**Not a new scorer.** A trade's gross IS `desk.ClosedPosition.pnl`, from
`desk.summarize` over `desk.load` — the objects the EOD digest already reads — and
the fills come from `shadow.marker.read_fills`, the one decoder of `order.fill`.
This module adds columns (times, order ids, context), never a second way to price
a spread.

**A held-to-expiry trade is valued at SETTLEMENT, and says so** (`tools.settlement`,
2026-09-22). 0DTE expiry produces no fill, and an OTM expiry no broker transaction
either, so its terminal value comes from the day's last recorded spot (or an official
print in the settlements file) through `shadow.outcome`'s own settlement math, and
the row names its `gross_basis`. When no trustworthy price exists the gross is BLANK,
never 0: a blank reads as unknown, a zero as a scratch, which is the
reassuring-when-wrong failure.

**Three day-level grosses, and only one of them is compared with the broker.**
`gross` is the day's FILL CASH — what the fills themselves moved, which is exactly
what the broker's `value` sums — so it stays the reconciliation input and a
settlement estimate never reaches that comparison. On a day where everything closed
it is the realized P&L. `settled_gross` is the part of the day's result valued at
settlement, and `total_gross` is the day's result: every trade's gross, blank when
any trade is unvalued. `total_gross` is what the Dashboard's P&L sums.

**What ties the P&L to the broker, now that the recon compares cash.** Every trade's
gross must equal the cash its own fills moved less what its expired contracts owed at
settlement (`pnl_check`, per trade, to the cent). The broker vouches for the cash; this
vouches that the P&L was built from it. A trade that fails it names the numbers in
`trades.pnl_check` and makes its day `DIFF`.

**Money is written as fixed two-place strings, rounded in ONE place** (`_money`).
The shared scorer is float; every price is a 0.05 tick times 100, so rounding to
the cent at the boundary is exact in practice.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from argus.clock import ET, UTC_TS_FMT, et_day_bounds_utc_iso, et_now
from argus.config import Config
from argus.execution.build import candidate_key, parse_candidate_key
from argus.marketcalendar import MarketCalendar
from argus.occ import occ_strike
from argus.shadow.marker import read_fills
from argus.shadow.outcome import Execution, Fill
from argus.signal.models import SPX_MULTIPLIER
from argus.tools import desk
from argus.tools.broker_ledger import (
    BrokerTxnRow,
    ReconRow,
    apply_broker,
    reconcile,
    txn_rows,
)
from argus.tools.eod import G1Series, latest_settled_session, read_blackout_day, read_g1
from argus.tools.settlement import (
    FILLS,
    Settlement,
    read_settlement_overrides,
    resolve_settlement,
    settle_trade,
)

CANDIDATE = "signal.candidate"
PROCESS_START = "process.start"

FILLS_CSV = "argus_fills.csv"
TRADES_CSV = "trades.csv"
DAYS_CSV = "days.csv"
BROKER_CSV = "broker_txns.csv"
RECON_CSV = "recon.csv"

# Trade outcomes. Plain strings: they are cell values, and nothing branches on them
# but `_outcome` and the day's counts.
CLOSED = "closed"
HELD = "held_to_expiry"
PARTLY_CLOSED = "partly_closed"
UNPRICEABLE = "unpriceable"

#: No settlements file. Distinct from `None`, which is a file that was NAMED and
#: could not be read — then every date is refused rather than valued from the spot.
NO_OVERRIDES: Mapping[date, float | None] = MappingProxyType({})


# -- formatting: the one place a number becomes a cell ------------------------


def _money(x: float | None) -> str:
    """Dollars to the cent; blank for unknown. Never `-0.00`."""
    if x is None:
        return ""
    out = f"{round(x, 2):.2f}"
    return "0.00" if out == "-0.00" else out


def _price(x: float | None) -> str:
    """A per-contract price: at least two places, up to four.

    Four, not two, because a price averaged across partial fills at different
    ticks need not land on a tick, and rounding it here would hide that.
    """
    if x is None:
        return ""
    out = f"{x:.4f}".rstrip("0")
    whole, _, frac = out.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def _strike(x: float) -> str:
    return f"{x:g}"


def _et_time(dt: datetime | None) -> str:
    return "" if dt is None else dt.astimezone(ET).strftime("%H:%M:%S")


# -- the rows -----------------------------------------------------------------


@dataclass(frozen=True)
class FillRow:
    trading_date: str
    time_et: str
    candidate_id: str
    broker_order_id: str
    direction: str
    #: From the CASH SIGN, never the broker's leg action: tastytrade labels a leg
    #: relative to the account's position, so on a chained book an opening buy can
    #: come back `Buy to Close` (2026-08-25).
    buy_sell: str
    symbol: str
    side: str
    strike: str
    contracts: int
    price: str
    cash: str


@dataclass(frozen=True)
class TradeRow:
    trading_date: str
    candidate_id: str
    side: str
    short_strike: str
    long_strike: str
    width: str
    contracts: int
    entry_time_et: str
    entry_credit: str
    entry_steps: str
    entry_limit: str
    exit_time_et: str
    exit_trigger: str
    exit_outcome: str
    exit_debit: str
    outcome: str
    gross: str
    max_loss: str
    gross_pct_of_max_loss: str
    seconds_since_prev_exit: str
    delta_source: str
    version: str
    entry_order_ids: str
    exit_order_ids: str
    #: From the BROKER, filled in by `broker_ledger.apply_broker`. Blank until the
    #: broker has reported every one of this trade's orders — a partial answer is
    #: not a fee, and a confident zero would be worse than a blank.
    fees: str = ""
    net: str = ""
    recon: str = ""
    #: How `gross` was arrived at (`tools.settlement`): `fills`, `settlement_spot`,
    #: `settlement_official`, or a refusal — `spot_too_early`, `no_spot`,
    #: `settlement_file_error` — with a blank gross. Blank on an unpriceable
    #: execution. APPENDED, like every column after `exit_order_ids`, so no column
    #: the Dashboard addresses by letter moves.
    gross_basis: str = ""
    #: The settlement price used, on a settlement-valued row only.
    settlement_price: str = ""
    #: When the recorded spot was taken (ET), on any row that consulted one —
    #: including a `spot_too_early` refusal, where it is the diagnosis. Blank for an
    #: official print. The spot is a PROXY up to `SETTLEMENT_SPOT_MAX_AGE_MIN` old,
    #: so how old is part of the number.
    settlement_at_et: str = ""
    #: `full_credit` | `partial` | `max_loss`, on a settlement-valued row only.
    settlement_result: str = ""
    #: The part of `gross` valued at settlement: the EXPIRED contracts' term alone.
    #: Equal to `gross` on a trade held in full; on a partly closed one the rest is
    #: realized on fills and is not counted here. Blank on a fills-basis row.
    settled_gross: str = ""
    #: Blank when `gross` is exactly what the trade's own fills moved, less what its
    #: expired contracts owed at settlement (see `pnl_check`); otherwise the numbers
    #: that disagree. Any non-blank cell makes the day's recon `DIFF`.
    pnl_check: str = ""


@dataclass(frozen=True)
class DayRow:
    trading_date: str
    blackout: str
    entry_attempts: int
    trades: int
    closed: int
    held_to_expiry: int
    unpriceable: int
    gross: str
    argus_fees: str
    #: NLV on the day's first and last `gate.decision`, NOT at the open and close:
    #: the gate writes only while candidates are evaluated, so the last reading can
    #: predate the day's last exit. Blank on a day with no gate rows.
    nlv_at_first_gate: str
    nlv_at_last_gate: str
    gate_rows: int
    version: str
    #: From the BROKER (see `broker_ledger`). `net` is the day's gross plus the
    #: broker's fees; `broker_net` is the broker's own sum, and the two agreeing is
    #: the point of the `recon` tab.
    broker_fees: str = ""
    broker_net: str = ""
    net: str = ""
    recon: str = ""
    #: The part of the day's result valued at SETTLEMENT rather than on fills, and
    #: the day's result (every trade's gross; blank when any trade is unvalued). See
    #: the module docstring for why `gross` is neither.
    settled_gross: str = ""
    total_gross: str = ""


@dataclass(frozen=True)
class Ledger:
    fills: tuple[FillRow, ...]
    trades: tuple[TradeRow, ...]
    days: tuple[DayRow, ...]
    #: Empty until `--broker` runs: an EMPTY tuple means "not fetched", which the
    #: writer turns into "leave those tabs alone" rather than blanking them.
    broker: tuple[BrokerTxnRow, ...] = ()
    recon: tuple[ReconRow, ...] = ()


# -- reads this module adds (context columns only) ----------------------------


def read_delta_sources(
    conn: sqlite3.Connection, trading_date: date, start: str, end: str
) -> dict[str, str]:
    """candidate_id -> the `delta_source` on that spread's LAST `signal.candidate`.

    `signal.candidate` carries no `candidate_id`; it carries the side and both legs'
    strikes, which is the tuple `candidate_key` is built from, so the key is rebuilt
    with the constructor rather than re-spelled here.
    """
    out: dict[str, str] = {}
    for (raw,) in conn.execute(
        "SELECT payload FROM audit_log WHERE category = ? AND ts >= ? AND ts < ? "
        "ORDER BY seq",
        (CANDIDATE, start, end),
    ):
        try:
            p = json.loads(raw)
            source = p.get("delta_source")
            if source is None:
                continue
            key = candidate_key(
                trading_date,
                str(p["side"]),
                float(p["short"]["strike"]),
                float(p["long"]["strike"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        out[key] = str(source)
    return out


def read_version_at(conn: sqlite3.Connection, *, seq: int | None = None,
                    before_ts: str | None = None) -> str:
    """The `version` on the last `process.start` at or before a point in the chain.

    Which build wrote a record is the build of the process running at the time, and
    `process.start` is the durable answer (the journal rotates). Blank when no such
    row exists — a chain older than the key, or a fixture.
    """
    if seq is not None:
        row = conn.execute(
            "SELECT payload FROM audit_log WHERE category = ? AND seq <= ? "
            "ORDER BY seq DESC LIMIT 1",
            (PROCESS_START, seq),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload FROM audit_log WHERE category = ? AND ts < ? "
            "ORDER BY seq DESC LIMIT 1",
            (PROCESS_START, before_ts),
        ).fetchone()
    if row is None:
        return ""
    try:
        return str(json.loads(row[0]).get("version") or "")
    except (TypeError, ValueError):
        return ""


# -- assembly -----------------------------------------------------------------


def fill_rows(trading_date: date, fills: Iterable[Fill]) -> list[FillRow]:
    out: list[FillRow] = []
    for f in fills:
        parsed = parse_candidate_key(f.candidate_id)
        side = parsed[1] if parsed else ""
        strike = occ_strike(f.symbol)
        cash = None if f.price is None else f.cash_sign * f.price * f.contracts * SPX_MULTIPLIER
        out.append(
            FillRow(
                trading_date=trading_date.isoformat(),
                time_et=_et_time(f.at_utc),
                candidate_id=f.candidate_id,
                broker_order_id="" if f.broker_order_id is None else str(f.broker_order_id),
                direction=f.direction,
                buy_sell="SELL" if f.cash_sign > 0 else "BUY",
                symbol=f.symbol,
                side=side,
                strike="" if strike is None else _strike(strike),
                contracts=f.contracts,
                price=_price(f.price),
                cash=_money(cash),
            )
        )
    return out


def _outcome(ex: Execution) -> str:
    if not ex.priced:
        return UNPRICEABLE
    if ex.expired_contracts <= 0:
        return CLOSED
    return PARTLY_CLOSED if ex.closed_contracts > 0 else HELD


def _order_ids(fills: Iterable[Fill], candidate: str, direction: str) -> str:
    ids = sorted({
        f.broker_order_id for f in fills
        if f.candidate_id == candidate and f.direction == direction
        and f.broker_order_id is not None
    })
    return " ".join(str(i) for i in ids)


@dataclass(frozen=True)
class _Valuation:
    """How one trade's gross was reached. `gross` is `None` when it was not."""

    gross: float | None
    basis: str = ""
    settlement_price: str = ""
    settlement_at_et: str = ""
    settlement_result: str = ""
    #: The expired contracts' term alone — see `TradeRow.settled_gross`.
    at_settlement: float | None = None
    #: Dollars the expired contracts owed at settlement; 0.0 on a fills basis.
    liability: float = 0.0


def _settle(
    ex: Execution, side: str, short: float, long_: float,
    closed: desk.ClosedPosition | None, settlement: Settlement | None,
) -> _Valuation:
    """A trade with contracts still held at the close. Realized on what closed —
    `ClosedPosition.pnl`, never recomputed — plus `settle_trade` on what expired."""
    if settlement is None:
        return _Valuation(gross=None)
    at_et = _et_time(settlement.spot_at_et)
    if not settlement.valued:
        return _Valuation(gross=None, basis=settlement.basis, settlement_at_et=at_et)
    assert settlement.price is not None and ex.entry_credit is not None
    settled = settle_trade(
        side, short, long_, ex.expired_contracts, ex.entry_credit, settlement.price
    )
    realized = closed.pnl if closed is not None else 0.0
    return _Valuation(
        gross=realized + settled.gross,
        basis=settlement.basis,
        settlement_price=_price(settlement.price),
        settlement_at_et=at_et,
        settlement_result=settled.result,
        at_settlement=settled.gross,
        liability=settled.liability,
    )


#: Half a cent. Every cell is rounded to the cent in `_money`, so "agrees to the
#: cent" is a difference below this — and a real disagreement is at least a tick
#: times the multiplier, $5.
_HALF_CENT = 0.005


def pnl_check(gross: float | None, cash: float | None, liability: float) -> str:
    """Does a trade's P&L follow from its own fills? Blank when it does.

    **The recon compares FILL CASH with the broker, so this is what ties the P&L to
    that comparison.** Before settlement valuation the recon's Argus side WAS the
    P&L; once it became fill cash (a held trade's opening credit is cash the broker
    reports, its settlement value is not), a P&L that disagreed with its own fills
    would reconcile `OK`. The identity that closes that:

        gross == cash its fills moved - what its expired contracts owed at settlement

    which holds exactly for every well-formed vertical: realized
    `(credit - debit) x closed` plus settled `(credit - liability) x expired` is
    `credit x opened - debit x closed - liability x expired`, and the first two terms
    ARE the fills' cash. It compares Argus with ARGUS — the broker never sees the
    liability, so rule 5 (fills-only broker comparison) is untouched.

    Blank too when either side is unknown: an unvalued or unpriceable trade is
    already `PROVISIONAL`, and a check on nothing is not a pass.
    """
    if gross is None or cash is None:
        return ""
    expected = cash - liability
    if abs(gross - expected) < _HALF_CENT:
        return ""
    owed = f" - settlement liability {_money(liability)}" if liability else ""
    return f"gross {_money(gross)} != fill cash {_money(cash)}{owed}"


def trade_rows(
    day: desk.DeskDay,
    summary: desk.DeskSummary,
    fills: list[Fill],
    delta_sources: dict[str, str],
    versions: dict[str, str],
    settlement: Settlement | None = None,
) -> list[TradeRow]:
    """One row per trade idea, in entry order. Pure; no I/O.

    `settlement` is the day's resolved settlement price (or refusal); `None` means
    nobody asked, which leaves a held trade unvalued with a blank basis."""
    closed = {c.candidate_id: c for c in summary.closed}
    entries = {e.candidate_id: e for e in day.entries}  # the LAST attempt per idea
    exit_times = sorted(
        ex.last_fill_et for ex in day.executions.values() if ex.closed_contracts > 0
    )
    fills_by_trade: dict[str, list[Fill]] = {}
    for f in fills:
        fills_by_trade.setdefault(f.candidate_id, []).append(f)

    out: list[TradeRow] = []
    for cid, ex in sorted(day.executions.items(), key=lambda kv: kv[1].first_seq):
        parsed = parse_candidate_key(cid)
        if parsed is None:
            continue
        _, side, short, long_ = parsed
        width = abs(long_ - short)
        outcome = _outcome(ex)

        # Gross only when the whole idea is known. Closed in full: `ClosedPosition.pnl`
        # IS the number, never recomputed here. Anything still held at the close is
        # valued at settlement or not at all.
        if outcome == CLOSED and cid in closed:
            valued = _Valuation(gross=closed[cid].pnl, basis=FILLS)
        elif outcome in (HELD, PARTLY_CLOSED):
            valued = _settle(ex, side, short, long_, closed.get(cid), settlement)
        else:
            valued = _Valuation(gross=None)
        gross = valued.gross
        credit = ex.entry_credit if ex.priced else None
        max_loss = (
            None if credit is None
            else (width - credit) * ex.opened_contracts * SPX_MULTIPLIER
        )
        pct = None if gross is None or not max_loss else gross / max_loss * 100

        before = [t for t in exit_times if t < ex.first_fill_et]
        since = (
            None if not before
            else (ex.first_fill_et - before[-1]).total_seconds()
        )

        entry = entries.get(cid)
        attempts = day.exits.get(cid) or []
        last_exit = attempts[-1] if attempts else None
        mark = day.marks.get(cid)
        exit_outcome = (
            last_exit.outcome if last_exit is not None
            else (mark.exit_outcome if mark is not None and mark.exit_outcome else "")
        )

        out.append(
            TradeRow(
                trading_date=day.trading_date.isoformat(),
                candidate_id=cid,
                side=side,
                short_strike=_strike(short),
                long_strike=_strike(long_),
                width=_strike(width),
                contracts=ex.opened_contracts,
                entry_time_et=_et_time(ex.first_fill_et),
                entry_credit=_price(credit),
                entry_steps="" if entry is None else str(entry.steps),
                entry_limit="" if entry is None else _price(entry.final_limit_price),
                exit_time_et=_et_time(ex.last_fill_et) if ex.closed_contracts > 0 else "",
                exit_trigger="" if last_exit is None else " ".join(last_exit.trigger_reasons),
                exit_outcome=exit_outcome,
                exit_debit=_price(ex.exit_debit) if ex.closed_contracts > 0 else "",
                outcome=outcome,
                gross=_money(gross),
                max_loss=_money(max_loss),
                gross_pct_of_max_loss="" if pct is None else f"{pct:.1f}",
                seconds_since_prev_exit="" if since is None else f"{since:.0f}",
                delta_source=delta_sources.get(cid, ""),
                version=versions.get(cid, ""),
                entry_order_ids=_order_ids(fills, cid, "open"),
                exit_order_ids=_order_ids(fills, cid, "close"),
                gross_basis=valued.basis,
                settlement_price=valued.settlement_price,
                settlement_at_et=valued.settlement_at_et,
                settlement_result=valued.settlement_result,
                settled_gross=_money(valued.at_settlement),
                pnl_check=pnl_check(
                    gross, fill_cash(fills_by_trade.get(cid, ())), valued.liability
                ),
            )
        )
    return out


def fill_cash(fills: Iterable[Fill]) -> float | None:
    """The cash the day's fills moved, in dollars; `None` when any fill is unpriced.

    The reconciliation's Argus side. The broker's `value` column is the same sum over
    the same fills, so this is comparable on EVERY day — including one that held a
    spread to expiry, whose opening fills are real cash the broker reports — and it
    never contains a settlement estimate. On a day where everything closed it equals
    the realized P&L.
    """
    total = 0.0
    for f in fills:
        if f.price is None:
            return None
        total += f.cash_sign * f.price * f.contracts * SPX_MULTIPLIER
    return total


def day_row(
    day: desk.DeskDay,
    summary: desk.DeskSummary,
    trades: list[TradeRow],
    *,
    fills: Iterable[Fill],
    blackout: bool,
    g1: G1Series,
    version: str,
) -> DayRow:
    unknown = any(t.gross == "" for t in trades)
    # Summed off the trade CELLS, so the day's total is the column a person can add
    # up by eye. Every cell is a cent-rounded two-place string, so this is exact.
    total = None if unknown else sum(float(t.gross) for t in trades)
    # The expired contracts' terms only: a partly closed trade's realized half is
    # fill-derived and does not belong in "Of which valued at settlement".
    settled = None if unknown else sum(
        float(t.settled_gross) for t in trades if t.settled_gross
    )
    nlv = g1.nlv
    return DayRow(
        trading_date=day.trading_date.isoformat(),
        blackout="yes" if blackout else "",
        entry_attempts=len(day.entries),
        trades=len(trades),
        closed=sum(1 for t in trades if t.outcome == CLOSED),
        held_to_expiry=sum(1 for t in trades if t.outcome in (HELD, PARTLY_CLOSED)),
        unpriceable=sum(1 for t in trades if t.outcome == UNPRICEABLE),
        gross=_money(fill_cash(fills)),
        argus_fees=_money(summary.fees),
        nlv_at_first_gate=_money(nlv[0]) if nlv else "",
        nlv_at_last_gate=_money(nlv[-1]) if nlv else "",
        gate_rows=day.allowed + sum(day.denials.values()),
        version=version,
        settled_gross=_money(settled),
        total_gross=_money(total),
    )


def build_day(
    conn: sqlite3.Connection,
    trading_date: date,
    calendar: MarketCalendar,
    overrides: Mapping[date, float | None] | None = NO_OVERRIDES,
) -> tuple[list[FillRow], list[TradeRow], DayRow]:
    """One day's rows.

    The day's `version` is the build running at the session's CLOSE, not the last
    deploy before midnight (an evening deploy is the next session's build).
    `overrides` is the settlements file: empty for none, `None` for one that was
    named and could not be read (see `settlement.resolve_settlement`)."""
    start, end = et_day_bounds_utc_iso(trading_date)
    day = desk.load(conn, trading_date)
    summary = desk.summarize(day)
    fills = read_fills(conn, trading_date)
    versions = {
        cid: read_version_at(conn, seq=ex.first_seq)
        for cid, ex in day.executions.items()
    }
    # Resolved only when something reached the close, so an ordinary day costs no
    # extra query; a refusal is logged by the resolver once per date, not per trade.
    held = any(ex.priced and ex.expired_contracts > 0 for ex in day.executions.values())
    settlement = (
        resolve_settlement(conn, trading_date, calendar, overrides) if held else None
    )
    trades = trade_rows(
        day, summary, fills, read_delta_sources(conn, trading_date, start, end),
        versions, settlement,
    )
    row = day_row(
        day,
        summary,
        trades,
        fills=fills,
        blackout=read_blackout_day(conn, start, end).blacked_out,
        g1=read_g1(conn, start, end),
        version=read_version_at(
            conn, before_ts=_chain_ts(calendar.close_datetime(trading_date))
        ),
    )
    return fill_rows(trading_date, fills), trades, row


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, UTC_TS_FMT).replace(tzinfo=timezone.utc)


def _chain_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(UTC_TS_FMT)


def first_record_date(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT MIN(ts) FROM audit_log").fetchone()
    if not row or row[0] is None:
        return None
    return _parse_ts(row[0]).astimezone(ET).date()


def first_session_date(conn: sqlite3.Connection, calendar: MarketCalendar) -> date | None:
    """The first trading day this database's process was up for at the close.

    The live database starts at the cutover, which was performed after 2026-09-15's
    close: that day's session ran in cert, so a row for it would be a zero day that
    never traded, counted as a day Argus sat out. So the ledger starts at the first
    session whose close came AFTER the first `process.start`. Falls back to the first
    record's date on a chain with no `process.start`.
    """
    row = conn.execute(
        "SELECT MIN(ts) FROM audit_log WHERE category = ?", (PROCESS_START,)
    ).fetchone()
    if not row or row[0] is None:
        return first_record_date(conn)
    started = _parse_ts(row[0]).astimezone(ET)
    d = started.date()
    for _ in range(16):
        if calendar.is_trading_day(d) and calendar.close_datetime(d) > started:
            return d
        d += timedelta(days=1)
    return None


def trading_dates(calendar: MarketCalendar, first: date, last: date) -> list[date]:
    out: list[date] = []
    d = first
    while d <= last:
        if calendar.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def build(
    conn: sqlite3.Connection,
    dates: Iterable[date],
    calendar: MarketCalendar,
    overrides: Mapping[date, float | None] | None = NO_OVERRIDES,
) -> Ledger:
    """The whole ledger over `dates`, read inside ONE transaction.

    One snapshot for the whole build: a read-only connection still sees commits
    between statements, and a ledger whose fills and days came from two different
    moments would not add up.

    The calendar is REQUIRED: the day's close anchors both the version stamp and the
    settlement spot's age bound, and a half-day closes at 13:00.
    """
    fills: list[FillRow] = []
    trades: list[TradeRow] = []
    days: list[DayRow] = []
    conn.execute("BEGIN")
    try:
        for d in dates:
            f, t, row = build_day(conn, d, calendar, overrides)
            fills.extend(f)
            trades.extend(t)
            days.append(row)
    finally:
        conn.execute("COMMIT")
    return Ledger(fills=tuple(fills), trades=tuple(trades), days=tuple(days))


# -- output -------------------------------------------------------------------


def write_csv(path: Path, rows: Iterable[object], row_type: type) -> None:
    """Write atomically: a temp file renamed into place, so the NAS pull (or a
    reader mid-run) never sees half a file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([f.name for f in fields(row_type)])
        for row in rows:
            writer.writerow(list(asdict(row).values()))  # type: ignore[call-overload]
    os.replace(tmp, path)


def write_ledger(out_dir: Path, ledger: Ledger) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / FILLS_CSV, ledger.fills, FillRow)
    write_csv(out_dir / TRADES_CSV, ledger.trades, TradeRow)
    write_csv(out_dir / DAYS_CSV, ledger.days, DayRow)
    # Written only when the broker was asked. A file of headers alone would push an
    # empty `broker_txns` tab over the top of yesterday's, which reads as "the broker
    # reported nothing" — a different claim from "nobody asked".
    if ledger.recon:
        write_csv(out_dir / BROKER_CSV, ledger.broker, BrokerTxnRow)
        write_csv(out_dir / RECON_CSV, ledger.recon, ReconRow)


def with_broker(ledger: Ledger, config: Config, first: date, last: date) -> Ledger:
    """The same ledger with the broker's side filled in.

    A failed fetch is NOT fatal and does not blank anything: every day is marked
    `BROKER_UNREAD`, the Argus side is written as usual, and tomorrow's rebuild picks
    the broker up again. The alternative — no recon rows at all — would leave
    yesterday's statuses on the tab looking current.
    """
    import asyncio

    from argus.bootstrap import load_env
    from argus.tools.broker_ledger import fetch_history

    # Only this path needs credentials, so `.env` is loaded here rather than at
    # startup: a run without `--broker` reads the database and nothing else.
    load_env()
    fetched, transactions = True, []
    try:
        transactions = asyncio.run(fetch_history(config, first, last))
    except Exception as exc:  # noqa: BLE001 — a reporting job never raises at the broker
        fetched = False
        print(f"ledger: broker history unavailable ({exc})", file=sys.stderr)

    broker = txn_rows(transactions)
    recon = reconcile(ledger.days, ledger.trades, broker, fetched=fetched)
    days, trades = apply_broker(ledger.days, ledger.trades, broker, recon)
    return replace(
        ledger, days=tuple(days), trades=tuple(trades),
        broker=tuple(broker), recon=tuple(recon),
    )


def load_overrides(path: Path | None) -> Mapping[date, float | None] | None:
    """The settlements file for `build`: `NO_OVERRIDES` when none is named, `None`
    when one is named and cannot be read.

    Not fatal: refusing the run would cost every other day's CSVs for one bad file.
    Every held-to-expiry trade is refused instead, because no date can be trusted to
    be absent from a file nobody could read.
    """
    if path is None:
        return NO_OVERRIDES
    try:
        return read_settlement_overrides(path)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ledger: settlements file unreadable ({exc}); every settlement "
              "valuation refused", file=sys.stderr)
        return None


def _open_readonly(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise SystemExit(f"database not found: {db_path}")
    # `isolation_level=None`: `build` manages its own transaction explicitly.
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, isolation_level=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        required=True,
        help="path to the config file (REQUIRED — there is no default; a default "
        "path is how the live database gets opened by accident)",
    )
    parser.add_argument("--out-dir", required=True, help="where the CSVs are written")
    parser.add_argument(
        "--through",
        help="last ET trading date to include (YYYY-MM-DD); default the latest "
        "SETTLED session",
    )
    parser.add_argument(
        "--settlements",
        type=Path,
        help="the persistent settlements file (`date,price` per line) whose official "
        "prints override the recorded spot. Optional: absent, every held-to-expiry "
        "trade is valued from the day's last recorded spot. Named but unreadable, "
        "every such trade is refused rather than valued from the spot",
    )
    parser.add_argument(
        "--broker",
        action="store_true",
        help="also fetch the broker's transaction history (one read-only GET) and "
        "reconcile against it. OFF by default, so a local run needs no credentials "
        "and touches no network",
    )
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    calendar = MarketCalendar.from_config(config.market)
    last = (
        date.fromisoformat(args.through) if args.through
        else latest_settled_session(calendar, et_now())
    )

    overrides = load_overrides(args.settlements)

    conn = _open_readonly(config.audit.db_path)
    try:
        first = first_session_date(conn, calendar)
        if first is None:
            print(f"ledger: no session in {config.audit.db_path}", file=sys.stderr)
            return 1
        ledger = build(conn, trading_dates(calendar, first, last), calendar, overrides)
    finally:
        conn.close()

    if args.broker:
        ledger = with_broker(ledger, config, first, last)

    write_ledger(Path(args.out_dir), ledger)
    print(
        f"ledger: {len(ledger.days)} day(s) {first}..{last}, "
        f"{len(ledger.trades)} trade(s), {len(ledger.fills)} fill(s) -> {args.out_dir}"
    )
    for trade in ledger.trades:
        spot_at = f", spot at {trade.settlement_at_et} ET" if trade.settlement_at_et else ""
        if trade.outcome in (HELD, PARTLY_CLOSED):
            print(f"  settlement {trade.candidate_id}: "
                  + (f"{trade.gross} at {trade.settlement_price} ({trade.gross_basis}, "
                     f"{trade.settlement_result}{spot_at})" if trade.gross
                     else f"UNVALUED ({trade.gross_basis or 'no basis'}{spot_at})"))
        if trade.pnl_check:
            # Argus against its own fills: printed whether or not the broker was read.
            print(f"  P&L DOES NOT FOLLOW FROM FILLS {trade.candidate_id}: "
                  f"{trade.pnl_check}")
    if ledger.recon:
        tally = Counter(r.status for r in ledger.recon)
        print("  broker: " + ", ".join(
            f"{count} {status}" for status, count in sorted(tally.items())
        ))
        for row in ledger.recon:
            if row.status == "DIFF":
                print(
                    f"  DIFF {row.trading_date}: gross {row.argus_gross} vs "
                    f"{row.broker_gross}, fees {row.argus_fees} vs {row.broker_fees}"
                    + (f", unmatched at broker: {row.unmatched_broker}"
                       if row.unmatched_broker else "")
                    + (f", unmatched in Argus: {row.unmatched_argus}"
                       if row.unmatched_argus else "")
                    + (f", P&L not from fills: {row.pnl_mismatch}"
                       if row.pnl_mismatch else "")
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
