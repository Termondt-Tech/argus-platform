"""Read proposal records back out and score them against settlement.

The audit log is append-only and hash-chained, so an outcome can never be
stitched onto the original record — it is written as its own Tier 1
`shadow.outcome` record referencing the candidate's `first_seq`. That keeps the
chain immutable and makes the scoring itself auditable: what settlement price
was used, and when it was applied.

Marking is Tier 1 rather than telemetry because it is the shadow-mode evidence
the whole phase exists to produce. Losing a day of it to a crash would mean
re-deriving it from a settlement price nobody wrote down.

**TWO CATEGORIES, ONE UNION, KEYED ON PRESENCE — NEVER ON ENVIRONMENT.**
`shadow.would_submit` is shadow's terminal record and `entry.dry_run` is the
dry-run entry path's; each environment writes one of them, so filtering on the
first alone silently stopped scoring anything at the 2026-08-11 sandbox flip.
The fix is a category union, and the thing that keeps it fixed is that NOTHING
here consults `environment`. This module never loads a `Config`, and the query
asks the RECORDS which categories a date holds rather than asking a config file
which category it should expect. That is deliberate and load-bearing in three
directions:

  - going back to shadow (for any reason, at any time) needs no change here;
  - a date is scored by what was actually recorded on it, not by what the
    config says today, months later — which is the only correct basis for
    backfilling an old date;
  - a day that somehow holds BOTH (a mid-day restart across an environment
    change) is not a special case: the union is ordered by the chain's global
    `seq`, and `trade_ideas` groups on (date, side, strikes) with no category in
    the key, so an overlapping spread collapses to one idea rather than two.

Adding a third terminal category later means adding it to `PROPOSAL_CATEGORIES`
and writing `markable_payload` into it — nothing else. `entry.finished` was the
third, added 2026-08-13 when live fire started.

**A PROPOSAL IS NOT THE WHOLE STORY ONCE ORDERS REALLY FILL.** Settlement scoring
answers "what would this have been worth held to expiry", which is the only
question shadow and dry-run can ask because nothing is ever held. A live position
the watchdog closes intraday has a REALIZED result instead, and the 16:00 print
says nothing about it. So this module reads a second thing — `order.fill`, the
Tier 1 record of every leg movement — and hands it to `shadow.outcome` as an
`Execution`, which decides per idea whether settlement or the fills govern.

That second read is keyed the same way as the first: on records, never on
environment. A day with no fills scores exactly as it always did, whether it had
none because it was a shadow day, a dry-run day, or a live day where every walk
walked away.

**Fills are attributed through `external_identifier`, not through a join.** The
broker echoes Argus's own identifier back verbatim, and it is
`argus-{direction}-{candidate}` by construction — so an `order.fill` record names
both the candidate and which half of the round trip it is, entirely within the
append-only log. `candidate_id` on those records is NULL (it does not round-trip
through the broker) and the `orders` table that does carry it is mutable
current-state, upserted in place; scoring a date months later has to read what
was RECORDED then, not what a live table says now.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone

from argus.audit.log import AuditLog
from argus.clock import UTC_TS_FMT, et_day_bounds_utc_iso
from argus.execution.build import candidate_key, parse_candidate_key
from argus.execution.models import LegAction, parse_external_identifier
from argus.shadow.outcome import (
    CASH_IN,
    CASH_OUT,
    Execution,
    Fill,
    Outcome,
    Proposal,
    TradeIdea,
    executions,
    mark,
    rerank,
    trade_ideas,
)

logger = logging.getLogger("argus.shadow.marker")

WOULD_SUBMIT = "shadow.would_submit"
DRY_RUN = "entry.dry_run"
FINISHED = "entry.finished"
FILL = "order.fill"
OUTCOME = "shadow.outcome"

#: Every audit category that can carry a scoreable proposal, read as a union.
#:
#: Membership is the whole environment-independence property: this tuple is a
#: statement about which RECORDS are markable, not about which environment is
#: configured. See the module docstring.
PROPOSAL_CATEGORIES = (WOULD_SUBMIT, DRY_RUN, FINISHED)

def _decode(seq: int, ts: str, category: str, payload_json: str) -> Proposal | None:
    """Decode one proposal row; None (with a log line) if it cannot be scored.

    `category` is carried only so the log line names the record that was skipped:
    with two sources in the union, "skipping a malformed record" without saying
    which category is a message that costs a query to act on.

    A record with no `signal`/`proposal` block is reported at INFO, not WARNING,
    and the distinction is not cosmetic. Every `entry.dry_run` written before
    2026-08-12 lacks the block by construction — it had not been added yet — so
    those rows are UNSCOREABLE BUT NOT BROKEN, and marking a pre-flip date would
    otherwise emit a warning per row for records that are exactly as they were
    designed to be. A record that HAS the block and still fails to decode is a
    different thing, and keeps the warning.
    """
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        logger.warning("skipping unparseable %s record at seq=%d", category, seq)
        return None
    if not isinstance(payload, dict) or "signal" not in payload or "proposal" not in payload:
        logger.info(
            "skipping %s at seq=%d: no signal/proposal block, so there is nothing "
            "to score (expected for entry.dry_run records written before 2026-08-12)",
            category, seq,
        )
        return None
    try:
        signal = payload["signal"]
        return Proposal(
            seq=seq,
            at_utc=datetime.strptime(ts, UTC_TS_FMT).replace(tzinfo=timezone.utc),
            side=signal["side"],
            short_strike=float(signal["short"]["strike"]),
            long_strike=float(signal["long"]["strike"]),
            width=float(signal["width"]),
            credit=float(signal["credit"]),
            contracts=int(payload["proposal"]["contracts"]),
        )
    except (KeyError, ValueError, TypeError):
        logger.warning("skipping malformed %s record at seq=%d", category, seq)
        return None


def read_proposals(conn: sqlite3.Connection, trading_date: date) -> list[Proposal]:
    """Every scoreable proposal on `trading_date`, from ALL proposal categories.

    ORDER BY seq, not by category then seq: the two sources are interleaved into
    one chain-ordered sequence, so "the first proposal for this spread" means the
    same thing whether the day held one category or both. The `ts` bound is the ET
    trading day (`clock.et_day_bounds_utc_iso`) and stays on the indexed column;
    both categories are Tier 1, written under the chain lock, so `ts` and `seq`
    agree on order.
    """
    start, end = et_day_bounds_utc_iso(trading_date)
    placeholders = ", ".join("?" * len(PROPOSAL_CATEGORIES))
    rows = conn.execute(
        f"SELECT seq, ts, category, payload FROM audit_log "  # noqa: S608 - fixed tuple
        f"WHERE category IN ({placeholders}) AND ts >= ? AND ts < ? ORDER BY seq",
        (*PROPOSAL_CATEGORIES, start, end),
    ).fetchall()
    decoded = [_decode(seq, ts, category, payload) for seq, ts, category, payload in rows]
    return [p for p in decoded if p is not None]


def _decode_fill(seq: int, ts: str, payload_json: str) -> Fill | None:
    """Decode one `order.fill` row; None (with a log line) if it cannot be used.

    Every rejection here is a real position whose cost this run will not know
    about, so all of them are WARNINGs — unlike a proposal record with no markable
    block, which is a known and harmless historical state.
    """
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        logger.warning("skipping unparseable %s record at seq=%d", FILL, seq)
        return None
    if not isinstance(payload, dict):
        logger.warning("skipping malformed %s record at seq=%d", FILL, seq)
        return None

    parsed = parse_external_identifier(str(payload.get("external_identifier") or ""))
    if parsed is None:
        # `fills.py` returns early on a non-Argus identifier, so this category
        # should only ever hold Argus's own. An unreadable one is a fill that
        # cannot be attributed to any trade idea.
        logger.warning(
            "skipping %s at seq=%d: external_identifier %r is not one Argus minted",
            FILL, seq, payload.get("external_identifier"),
        )
        return None
    direction, candidate = parsed

    try:
        action = LegAction(payload["action"])
        contracts = int(payload["contracts"])
        price = payload.get("price")
        order_id = payload.get("broker_order_id")
        fill = Fill(
            seq=seq,
            at_utc=datetime.strptime(ts, UTC_TS_FMT).replace(tzinfo=timezone.utc),
            candidate_id=candidate,
            direction=str(direction),
            symbol=str(payload["symbol"]),
            contracts=contracts,
            price=None if price is None else float(price),
            cash_sign=CASH_IN if action.sells else CASH_OUT,
            broker_order_id=None if order_id is None else int(order_id),
        )
    except (KeyError, ValueError, TypeError):
        logger.warning("skipping malformed %s record at seq=%d", FILL, seq)
        return None

    # NOTE: there is deliberately no check here that the broker's open/close
    # SUFFIX agrees with the identifier's direction (2026-08-25). tastytrade
    # labels a leg action relative to THE ACCOUNT'S POSITION, not to Argus's
    # intent: on a chained book an opening BUY lands on a strike another live
    # spread is already short and comes back `Buy to Close` on an `open` order.
    # Whether a fill is coherent is a question about the SPREAD, and it is asked
    # in `outcome.executions` where the answer can be recorded as a problem
    # instead of deleting the record. See `outcome._role_problems`.
    if fill.contracts <= 0:
        logger.warning("skipping %s at seq=%d: %d contracts", FILL, seq, fill.contracts)
        return None
    return fill


def read_fills(conn: sqlite3.Connection, trading_date: date) -> list[Fill]:
    """Every usable `order.fill` on `trading_date`, in chain order.

    A fill whose candidate belongs to a DIFFERENT trading date is dropped. Argus
    trades 0DTE only, so a position never spans a date boundary and such a record
    would mean the identifier or the clock is wrong — either way not something to
    attach to today's scoring.
    """
    start, end = et_day_bounds_utc_iso(trading_date)
    rows = conn.execute(
        "SELECT seq, ts, payload FROM audit_log "
        "WHERE category = ? AND ts >= ? AND ts < ? ORDER BY seq",
        (FILL, start, end),
    ).fetchall()
    out: list[Fill] = []
    for seq, ts, payload in rows:
        fill = _decode_fill(seq, ts, payload)
        if fill is None:
            continue
        parsed = parse_candidate_key(fill.candidate_id)
        if parsed is None or parsed[0] != trading_date:
            logger.warning(
                "skipping %s at seq=%d: candidate %r is not a %s spread",
                FILL, seq, fill.candidate_id, trading_date,
            )
            continue
        out.append(fill)
    return out


def read_executions(conn: sqlite3.Connection, trading_date: date) -> dict[str, Execution]:
    """What actually traded on `trading_date`, netted per candidate. Empty is normal."""
    return executions(read_fills(conn, trading_date))


def _synthesize(execution: Execution) -> TradeIdea | None:
    """A trade idea reconstructed from fills alone, for a position with no proposal.

    **This is a rail, not a backfill helper.** A missing proposal record costs one
    counterfactual data point; a missing FILLED position costs real P&L and leaves
    the account's own history disagreeing with the signal record. So a candidate
    that has fills is always scored, even when nothing markable was written for it
    — which is exactly the state 2026-08-13 is in, its live entries having written
    `entry.finished` before that record carried a markable block.

    It costs no second decoder: `candidate_id` IS (date, side, short, long) by
    construction (`execution.build.candidate_key`), the width is the strikes, the
    size is what filled, and the credit is what it filled AT — which is the number
    that governs a filled position anyway. The only thing genuinely lost is the
    re-proposal history, reported honestly as `proposals = 0`.
    """
    parsed = parse_candidate_key(execution.candidate_id)
    if parsed is None:
        logger.warning(
            "cannot score fills for %r: not a candidate id", execution.candidate_id
        )
        return None
    if execution.opened_contracts <= 0:
        # Closing fills with no opening fills behind them. There is no position to
        # reconstruct, and a zero-contract idea would score a confident $0 for
        # records that are plainly incomplete. Say so and leave it out.
        logger.warning(
            "cannot score fills for %s: %d contract(s) opened, %d closed%s",
            execution.candidate_id, execution.opened_contracts,
            execution.closed_contracts,
            f" ({'; '.join(execution.problems)})" if execution.problems else "",
        )
        return None
    trading_date, side, short_strike, long_strike = parsed
    # 0.0 only when the fills carry no price at all, which `Outcome.shape` reports
    # as `unpriceable` and refuses to turn into a P&L — the record exists to say a
    # position happened and could not be priced, not to assert it was free.
    credit = execution.entry_credit if execution.entry_credit is not None else 0.0
    return TradeIdea(
        trading_date=trading_date,
        side=side,
        short_strike=short_strike,
        long_strike=long_strike,
        width=abs(long_strike - short_strike),
        contracts=execution.opened_contracts,
        credit=credit,
        best_credit=credit,
        worst_credit=credit,
        proposals=0,
        first_seen_et=execution.first_fill_et,
        last_seen_et=execution.last_fill_et,
        first_seq=execution.first_seq,
        entry_rank=1,  # replaced by `rerank` once the day's whole set is known
    )


def pending_scores(
    conn: sqlite3.Connection, trading_date: date
) -> list[tuple[TradeIdea, Execution | None]]:
    """Every idea to score on `trading_date`, each with what it actually did.

    The two readers are joined on the candidate key, which both sides can produce:
    a decoded proposal has (date, side, strikes) and so does a fill's identifier.
    An idea with no fills gets `None` and is scored at settlement exactly as
    before; an execution with no idea is reconstructed by `_synthesize` so a real
    position can never go unscored.
    """
    ideas = trade_ideas(read_proposals(conn, trading_date))
    execs = read_executions(conn, trading_date)

    paired: list[tuple[TradeIdea, Execution | None]] = []
    matched: set[str] = set()
    for idea in ideas:
        key = candidate_key(
            idea.trading_date, idea.side, idea.short_strike, idea.long_strike
        )
        execution = execs.get(key)
        if execution is not None:
            matched.add(key)
        paired.append((idea, execution))

    for key, execution in execs.items():
        if key in matched:
            continue
        synthesized = _synthesize(execution)
        if synthesized is not None:
            paired.append((synthesized, execution))

    # Rank across the MERGED set: "the Nth idea of the day on this side" is a
    # property of the day, not of whichever reader produced the row.
    ranked = {idea.first_seq: idea for idea in rerank(i for i, _ in paired)}
    return [(ranked[idea.first_seq], execution) for idea, execution in paired]


def marked_dates(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT json_extract(payload, '$.trading_date') FROM audit_log "
        "WHERE category = ?",
        (OUTCOME,),
    ).fetchall()
    return {row[0] for row in rows if row[0]}


def pending_ideas(conn: sqlite3.Connection, trading_date: date) -> list[TradeIdea]:
    """The day's distinct trade ideas, merged and ranked. See `pending_scores`."""
    return [idea for idea, _ in pending_scores(conn, trading_date)]


async def mark_trading_date(
    conn: sqlite3.Connection,
    audit: AuditLog,
    trading_date: date,
    settlement: float,
    *,
    settlement_source: str,
    remark: bool = False,
) -> list[Outcome]:
    """Score every trade idea from `trading_date` and record the outcomes.

    Refuses to double-mark: the audit log is append-only, so re-running would
    leave two contradictory scores for the same idea with nothing to say which
    is current. `remark=True` is the deliberate override (e.g. the first run
    used a wrong settlement price).

    `settlement` is still required on a day where every position closed intraday
    and nothing rode to expiry. It costs nothing there — the settlement term is
    multiplied by zero held contracts — and requiring it unconditionally is what
    keeps this ONE command with no flag saying which kind of day it is.
    """
    if not remark and trading_date.isoformat() in marked_dates(conn):
        raise ValueError(
            f"{trading_date} is already marked; pass remark=True to record a second "
            "scoring (the first is NOT removed — the log is append-only)"
        )

    outcomes = [
        mark(idea, settlement, execution)
        for idea, execution in pending_scores(conn, trading_date)
    ]
    for outcome in outcomes:
        await audit.record(
            OUTCOME,
            {**outcome.to_payload(), "settlement_source": settlement_source,
             "remark": remark},
        )
    return outcomes
