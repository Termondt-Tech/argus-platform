"""Broker reconciliation (spec §6 non-negotiable).

The broker is the only source of truth. On every startup — and on demand — Argus
compares the broker's authoritative positions against what it *expected* to be
holding (its own persistent record). Any mismatch means Argus has lost track of
reality, so the process must refuse to resume automated logic and alert instead
of guessing (CLAUDE.md non-negotiable). The refusal itself lives in the app
(`_reconcile`); this module produces the verdict.

The comparison is a pure function over two `symbol -> signed_quantity` maps, so
every discrepancy class is exhaustively testable without a broker.

**Orders are reconciled too** (added in chunk 2). Positions alone are not the
whole of "has Argus lost track of reality": a WORKING order nobody knows about can
become a position at any moment, and the recovery story for a crash mid-submission
is explicitly "ask the broker what actually happened, and halt on anything
unexplained" — never resume, never replay. `reconcile_orders` is that question.
Both kinds of discrepancy land in the same `ReconciliationResult`, so the two
existing refusal sites (`app.Application._reconcile` at process start,
`session._reconcile_or_abort` per trading day) halt on an unexplained order through
exactly the code path they already use for a position mismatch — no second rail,
no second environment gate.

**Including orders that cannot be read at all** (2026-08-20). Until then the
adapter dropped any order it could not map before this module ever saw it — safe
under a comment written when nothing here called `get_live_orders()`, and false
from the day this module started. One unrecognized status string was enough to
hide an unknown WORKING or FILLED order from the rail that exists to halt on it.
`get_live_orders()` now returns `LiveOrders`, and `reconcile_orders` takes the
unreadable ones as a required argument.
→ docs/decisions/2026-08-20-unmappable-orders-reach-reconciliation.md

**The sandbox 24h reset is handled here too** (added in chunk 5). tastytrade wipes
a sandbox account's trades, positions and balances every 24 hours, so sandbox would
otherwise present a `missing_at_broker` mismatch — and therefore a hard halt —
every single morning. `sandbox_reset_decision` is the pure decision about whether a
non-empty ledger against an empty broker is that expected reset, and it says yes
only when Argus's own trading day (ET, `clock.py`) has rolled over. `reconcile_and_record`
is the one sequence both refusal sites now run, so the reset handling, the
`trading_date` stamp and the mismatch alert exist exactly once.

**Positions that expired away are resolved here too** (added 2026-08-12). 0DTE
expiry produces no fill event and therefore never removes the `positions` row it
left behind, so the next reconcile reads a stale row as `missing_at_broker` and
hard-halts on the strategy's most common outcome. `expiry_verdicts` is the pure
rule — a row the broker does not hold, whose OWN OCC expiry has passed, is an
expected resolution rather than a mismatch — and it can only ever REMOVE rows
from the discrepancy set, never add one. "Has passed" means strictly before
today, OR today with today's session already SETTLED (2026-08-25): SPXW is
PM-settled and cash-settled, so an evening restart on an expiry day was halting
on rows that definitively could not still be held.

**A halt earned here also carries a GRADE** (2026-08-29). `halt_grade` is the pure
classifier deciding whether a verdict stops the whole process (everything, and the
default) or only new ENTRIES, leaving the session — and therefore the watchdog and
its virtual stops — running. It sits beside `mismatch_halts_process` for the same
reason that function exists: one source of truth for a boundary both refusal sites
must never drift on. The two are orthogonal — that one asks "process or day?", this
one asks "given a halt, how wide?" — and shadow never reaches this one.
→ docs/decisions/2026-08-29-halt-scope-split.md

The `execution.store` import is `LedgerState` only — a plain dataclass, and since
2026-09-03 (`parse_external_identifier` moved from `execution.build` into
`execution.models`) this module imports no `execution.build` at all.
`execution.models` is SDK-free the same way `execution.store` is — duck-typed
mapping, no tastytrade import, and it stays that way. Neither pulls the SDK in
(that lives in `execution.broker`), so this module stays importable by anything,
which is the property the `Any`-typed ports below protect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, NoReturn

from argus.account.port import BrokerAccountPort
from argus.alerts import AlertFn
from argus.audit.log import AuditLog, AuditWriteError
from argus.execution.models import (
    DISPOSITION_UNMAPPABLE,
    Order,
    OrderDirection,
    OrderDisposition,
    UnmappableOrder,
    classify_order,
    parse_external_identifier,
)
from argus.execution.store import LedgerState
from argus.occ import occ_expiry

logger = logging.getLogger("argus.reconcile")

# Position discrepancy kinds:
UNEXPECTED_AT_BROKER = "unexpected_at_broker"  # broker holds it, Argus didn't expect it
MISSING_AT_BROKER = "missing_at_broker"        # Argus expected it, broker doesn't hold it
QUANTITY_MISMATCH = "quantity_mismatch"        # both hold it, different signed quantity

# Order discrepancy kinds. The first two mean "the broker has an order Argus has
# no record of placing"; they differ in what that order can still do to the
# account.
UNEXPECTED_WORKING_ORDER = "unexpected_working_order"  # unknown, and can still fill
UNEXPECTED_FILLED_ORDER = "unexpected_filled_order"    # unknown, and already a position
# The third is a different claim: the broker has an order Argus cannot READ, so
# whether it is working, filled or dead is not a question anyone answered.
UNMAPPABLE_BROKER_ORDER = "unmappable_broker_order"    # present, and uninterpretable


@dataclass(frozen=True)
class Discrepancy:
    symbol: str
    kind: str
    expected_qty: int
    broker_qty: int


@dataclass(frozen=True)
class OrderDiscrepancy:
    """A broker order Argus cannot account for.

    `external_identifier` is carried verbatim because it is the first thing an
    operator needs: an `argus-`-prefixed identifier means Argus placed this and
    lost the record (look for the pre-send `order.submit_attempt` in the audit
    trail); anything else means somebody else is trading the account.
    """

    broker_order_id: int | None
    external_identifier: str
    kind: str
    status: str | None
    disposition: str


@dataclass(frozen=True)
class ReconciliationResult:
    ok: bool
    discrepancies: tuple[Discrepancy, ...]
    order_discrepancies: tuple[OrderDiscrepancy, ...] = ()

    @property
    def total_discrepancies(self) -> int:
        """Positions + orders. What the refusal messages count, so an
        order-only mismatch never reports "0 discrepancies; refusing to resume"."""
        return len(self.discrepancies) + len(self.order_discrepancies)

    def as_payload(self) -> dict:
        return {
            "ok": self.ok,
            "discrepancies": [
                {
                    "symbol": d.symbol,
                    "kind": d.kind,
                    "expected_qty": d.expected_qty,
                    "broker_qty": d.broker_qty,
                }
                for d in self.discrepancies
            ],
            "order_discrepancies": [
                {
                    "broker_order_id": d.broker_order_id,
                    "external_identifier": d.external_identifier,
                    "kind": d.kind,
                    "status": d.status,
                    "disposition": d.disposition,
                }
                for d in self.order_discrepancies
            ],
        }


def reconcile_positions(
    expected: dict[str, int], broker: dict[str, int]
) -> ReconciliationResult:
    """Compare expected vs. broker positions (symbol -> signed qty, flats excluded)."""
    discrepancies: list[Discrepancy] = []
    for symbol in sorted(set(expected) | set(broker)):
        e = expected.get(symbol, 0)
        b = broker.get(symbol, 0)
        if e == b:
            continue
        if e == 0:
            kind = UNEXPECTED_AT_BROKER
        elif b == 0:
            kind = MISSING_AT_BROKER
        else:
            kind = QUANTITY_MISMATCH
        discrepancies.append(Discrepancy(symbol, kind, e, b))
    return ReconciliationResult(ok=not discrepancies, discrepancies=tuple(discrepancies))


def reconcile_orders(
    known_order_ids: Iterable[int],
    broker_orders: Sequence[Order],
    *,
    unmappable: Sequence[UnmappableOrder],
) -> tuple[OrderDiscrepancy, ...]:
    """Broker orders Argus has no record of placing, or cannot read. Pure; no I/O.

    `broker_orders` is `LiveOrders.orders` and `unmappable` is
    `LiveOrders.unmappable` — together, everything currently working plus anything
    cancelled or filled in the past 24 hours. `known_order_ids` is what Argus's
    own order ledger holds (`orders`, migration 0004) for those ids.

    **`unmappable` is keyword-only and has NO default, on purpose.** A default of
    `()` would let a caller reconstruct the exact bug this parameter was added for
    — reconciling against a set that silently excludes the orders it most needed
    to see — by simply not knowing the argument existed. Making it required means
    every call site states what it is doing about them.

    An unknown order is judged by the SHARED classification (`classify_order`) —
    the same function the duplicate-submission guard uses, which is the point of
    it existing:

        WORKING  -> discrepancy. It can still fill, so internal state is wrong
                    right now or about to be.
        FILLED   -> discrepancy. It already moved the account.
        DEAD      -> NOT a discrepancy. Terminal with nothing filled: it cannot
                    fill, and it holds nothing. It cannot make internal state
                    wrong, so halting the process over it would mean a rejected
                    order from yesterday afternoon bricks this morning — with no
                    risk being managed in exchange.

    An UNMAPPABLE order gets none of those readings, because none of them was
    derived — it is `UNMAPPABLE_BROKER_ORDER`, always a discrepancy, and it is
    **not** suppressed by `known_order_ids`. That last part is the one rule here
    worth stating twice: the other two kinds ask "did Argus place this?", and for
    them a ledger row is a complete answer. This kind asks "can Argus read this?",
    and a ledger row says nothing about that. An order of Argus's own that comes
    back wearing a status the mirrored enum does not have is *exactly* as
    unexplained as a stranger's, and resuming automated logic over it would be
    guessing — which spec §6 forbids in the same sentence it forbids resuming on a
    position mismatch.

    The cost of that is real and accepted: one benign foreign order with a status
    tastytrade added since the enum was last checked will hard-halt a sandbox/live
    startup. That is the correct direction (the alternative is trading blind past
    it), it is loud, and the audit record names the raw status string — which is
    normally a one-line fix to `OrderStatus`.

    Note this is asymmetric with the position check on purpose: there is no
    "missing at broker" for orders. Argus recording an order the broker has never
    heard of is not a hazard — nothing can execute — and `get_live_orders()`
    only looks back 24h, so every order older than that would read as missing.
    """
    known = {int(i) for i in known_order_ids}
    discrepancies: list[OrderDiscrepancy] = []
    for unreadable in unmappable:
        discrepancies.append(
            OrderDiscrepancy(
                broker_order_id=unreadable.broker_order_id,
                external_identifier=unreadable.external_identifier,
                kind=UNMAPPABLE_BROKER_ORDER,
                # The broker's RAW status text, not an `OrderStatus` — the enum is
                # what refused it. This field is the fix instruction.
                status=unreadable.raw_status,
                disposition=DISPOSITION_UNMAPPABLE,
            )
        )
    for order in broker_orders:
        if order.broker_order_id is not None and order.broker_order_id in known:
            continue
        disposition = classify_order(order)
        if disposition is OrderDisposition.DEAD:
            continue
        discrepancies.append(
            OrderDiscrepancy(
                broker_order_id=order.broker_order_id,
                external_identifier=order.external_identifier,
                kind=(
                    UNEXPECTED_WORKING_ORDER
                    if disposition is OrderDisposition.WORKING
                    else UNEXPECTED_FILLED_ORDER
                ),
                status=str(order.status) if order.status is not None else None,
                disposition=str(disposition),
            )
        )
    return tuple(discrepancies)


# -- the sandbox 24h account reset --------------------------------------------
#
# Confirmed against tastytrade's own documentation (2026-07-30): the SANDBOX account
# is reset every 24 hours — trades, transactions, positions and balances are all
# wiped, while the user/customer/account entities survive. So in sandbox the broker
# genuinely shows a flat account every morning regardless of what happened
# yesterday, and Argus's ledger, which is on a persistent volume, does not.
#
# Left alone that is a `missing_at_broker` discrepancy every single day, which in
# sandbox HARD-HALTS the process (`mismatch_halts_process`) — the rail firing
# correctly on a condition that is not a fault. The fix must not be to stop
# checking.
#
# MEASURED NARROWER THAN DOCUMENTED (2026-08-17, from the 2026-08-14 halt). The
# wipe reaches POSITIONS; it does not reach what `get_live_orders()` reports.
# Thursday's three filled orders were still returned at Friday 09:40 ET — 23h18m
# to 23h40m after the fills, inside that endpoint's rolling 24-HOUR lookback. So
# the reset clears HOLDINGS only — `positions` and `order_legs`, never the `orders`
# identity rows themselves (`clear_reset_holdings`). Clearing the order
# ledger inside that overlap is what emptied `known_orders()` against orders the
# broker still reported, turning Argus's own fills into `unexpected_filled_order`
# and hard-halting the day.
#
# The two windows are NOT the same and must not be conflated again: this decision
# keys on a trading-DAY rollover in ET, the order endpoint keys on a rolling 24
# HOURS. Any fill after ~09:40 ET sits inside both the next morning.
# → docs/decisions/2026-08-17-sandbox-reset-order-ledger.md

#: Why a reset was or was not assumed. Recorded verbatim, so the audit trail says
#: which branch ran rather than only what it did.
RESET_NOT_SANDBOX = "not_sandbox"
RESET_LEDGER_EMPTY = "ledger_already_empty"
RESET_NO_CLEAN_MARKER = "no_clean_reconciliation_on_record"
RESET_SAME_TRADING_DAY = "same_trading_day_as_last_clean_reconciliation"
RESET_LEDGER_WRITTEN_TODAY = "ledger_written_today"
RESET_DAY_ROLLED_OVER = "trading_day_rolled_over"


@dataclass(frozen=True)
class ResetDecision:
    """Whether to clear Argus's own ledger before reconciling, and why.

    Pure and separately testable, because this is the one place a safety rail is
    deliberately allowed to stand down and the reasoning has to be inspectable.
    """

    clear: bool
    reason: str
    today: date
    last_clean_trading_date: date | None
    ledger: LedgerState

    def to_payload(self) -> dict[str, Any]:
        return {
            "clear": self.clear,
            "reason": self.reason,
            "today": self.today.isoformat(),
            "last_clean_trading_date": (
                self.last_clean_trading_date.isoformat()
                if self.last_clean_trading_date is not None
                else None
            ),
            "ledger": self.ledger.to_payload(),
        }


def sandbox_reset_decision(
    *,
    environment: str,
    today: date,
    last_clean_trading_date: date | None,
    ledger: LedgerState,
) -> ResetDecision:
    """May the ledger be cleared as an expected consequence of the sandbox reset?

    Uses Argus's EXISTING day-rollover notion — a trading date in ET, compared
    against another trading date (`clock.py`) — and nothing else. Deliberately not
    an elapsed-time threshold: that would require guessing at a reset schedule
    tastytrade does not publish, and would drift out of agreement with the rest of
    the system's idea of a day.

    Every branch that is not a rollover returns `clear=False`, so the ONLY way the
    ledger is cleared is a positive answer to both questions:

      1. Has the trading day changed since the last reconciliation that was
         CONFIRMED CLEAN? (the operator's resolution, 2026-07-30)
      2. Is there nothing in the ledger written on today's trading date?

    (2) is not redundant with (1), and it is the reason a same-day crash-restart
    still halts. Consider a process that started this morning, reconciled clean
    YESTERDAY as its last clean run, submitted an order today, and then crashed
    before reconciling again: (1) alone says "the day rolled over, clear it", which
    would silently discard today's real order — exactly the masking that ruled out
    the unconditional-clear alternative. (2) refuses, and the mismatch halts.

    Both directions of being wrong were weighed, and they are not symmetric:
    clearing when it was not a reset loses Argus's record of a position the broker
    still holds — but that surfaces IMMEDIATELY as `unexpected_at_broker` on the very
    reconcile that follows, which halts. NOT clearing when it was a reset costs a
    refused day. So the residual risk of a false clear is a halt, not a silent
    resume, which is why this is safe to automate at all.

    Live is never cleared: a live account is not wiped nightly, so "broker empty,
    ledger non-empty" there is always real. That is why the gate is
    `environment == "sandbox"` and not `mismatch_halts_process`.
    """
    def decide(clear: bool, reason: str) -> ResetDecision:
        return ResetDecision(
            clear=clear,
            reason=reason,
            today=today,
            last_clean_trading_date=last_clean_trading_date,
            ledger=ledger,
        )

    if environment != "sandbox":
        return decide(False, RESET_NOT_SANDBOX)
    if ledger.empty:
        # Nothing to clear. Reported rather than skipped so the trail shows the
        # check ran on a morning when it happened to have nothing to do.
        return decide(False, RESET_LEDGER_EMPTY)
    if last_clean_trading_date is None:
        # No reconciliation has ever been confirmed clean (or the last one was not).
        # A non-empty ledger with no clean run behind it is unexplained, and an
        # unexplained ledger is not evidence of a reset. Fail closed.
        return decide(False, RESET_NO_CLEAN_MARKER)
    if last_clean_trading_date >= today:
        return decide(False, RESET_SAME_TRADING_DAY)
    if ledger.newest_trading_date is not None and ledger.newest_trading_date >= today:
        return decide(False, RESET_LEDGER_WRITTEN_TODAY)
    return decide(True, RESET_DAY_ROLLED_OVER)


# -- positions that expired away ----------------------------------------------
#
# A `positions` row leaves the table exactly two ways: a fill event nets its
# quantity to zero (`store._apply_position`), or the sandbox reset above clears
# the lot. **0DTE expiry is neither.** SPXW is a cash-settled European index
# option: at expiry the position simply ceases to exist at the broker — worthless
# or in the money, there is no assignment, no closing trade, and nothing that
# reaches `FillConsumer`, which only ever sees `PlacedOrder` events. Argus does
# receive `CurrentPosition` frames, but `account.stream._ingest_position` writes
# the in-memory cache, never the table.
#
# So the rows outlive the position they describe, and the next reconcile — the
# next process or session start — reads them as `missing_at_broker`, which in
# sandbox/live is a process-wide hard halt. On the strategy's MOST COMMON
# outcome. Sandbox never shows it, because the 24h reset above clears the ledger
# first and masks it completely.
#
# The rule is deliberately narrow, and each half is load-bearing:
#
#   - **Broker-confirmed, never clock-assumed.** Only a row the broker does not
#     hold AT ALL is eligible. "It is past 16:00" is not evidence on its own; a
#     position still open at the broker is still open, whatever the clock says.
#   - **PAST expiry, or today's expiry once today has SETTLED.** A future expiry
#     never resolves. A row expiring TODAY resolves only once the session is over
#     and settled (`MarketCalendar.is_settled`, close + 15 minutes, half-day
#     aware) — before that, the position can genuinely still be held and the cost
#     of being wrong in that direction is deleting Argus's record of a live one.
#   - **Unreadable expiry keeps the halt.** `occ_expiry` returns None rather than
#     raising, and None resolves nothing.
#
# The same-day half was added 2026-08-25, from a v0.3.8 evening deploy: restarting
# at 21:02 ET on a day that held two 0DTE spreads to expiry hard-halted on four
# `missing_at_broker` rows that nothing was ever going to remove, and stayed
# halted until the ET date rolled over. SPXW is PM-settled, cash-settled and
# European — after settlement a 0DTE position DEFINITIVELY cannot still be held,
# so the "might still be open" concern the strictly-past rule protects against
# simply does not apply once its own session has settled.
# → docs/decisions/2026-08-12-expired-position-resolution.md (2026-08-25 addendum)

#: Why a `missing_at_broker` row was or was not explained by its own expiry.
#: Recorded verbatim, so the trail says which branch ran and not only what it did.
EXPIRED_AT_BROKER = "expired_at_broker"
#: Today's expiry, resolved because today's session has settled. Deliberately its
#: OWN reason string rather than reusing `EXPIRED_AT_BROKER`: a same-day
#: resolution is the newer, time-dependent branch, and an operator reading the
#: audit trail after an evening restart must be able to see which one ran.
SETTLED_AT_BROKER = "expired_today_and_session_settled"
KEPT_NOT_YET_EXPIRED = "expiry_is_later_than_today"
#: Today's expiry, before today's session has settled — the original
#: strictly-past behaviour, now scoped to the part of the day where it is the
#: only safe answer.
KEPT_SESSION_NOT_SETTLED = "expiry_is_today_and_session_not_settled"
KEPT_EXPIRY_UNREADABLE = "expiry_not_derivable_from_symbol"


@dataclass(frozen=True)
class ExpiryVerdict:
    """One `missing_at_broker` row, judged against its own OCC expiry.

    Pure and separately testable for the same reason `ResetDecision` is: this is
    a safety rail standing down, and the reasoning has to be inspectable.
    """

    discrepancy: Discrepancy
    expiry: date | None
    resolved: bool
    reason: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.discrepancy.symbol,
            "expected_qty": self.discrepancy.expected_qty,
            "expiry": self.expiry.isoformat() if self.expiry is not None else None,
            "resolved": self.resolved,
            "reason": self.reason,
        }


def expiry_verdicts(
    discrepancies: Sequence[Discrepancy],
    today: date,
    *,
    session_settled: bool,
) -> tuple[ExpiryVerdict, ...]:
    """Judge every `missing_at_broker` row against its expiry. Pure; no I/O.

    Returns one verdict per eligible row and NOTHING for the other discrepancy
    kinds, which are untouched by this rule and reach the halt exactly as before:

      - `unexpected_at_broker` — the broker holds something Argus did not expect.
        Expiry says nothing about that; it is the opposite problem.
      - `quantity_mismatch` — the broker still holds SOME of it, so whatever else
        happened, it did not expire away.

    `today` is the caller's ET trading date (`clock.et_today`, threaded through
    `reconcile_and_record`), never a host-local date and never read here — the
    same discipline the reset decision follows, and for the same reason: a
    date-boundary disagreement between two parts of the system that both think
    they know "today" is the least visible way to get this wrong.

    **`session_settled` is keyword-only and has NO default**, for the same reason
    `reconcile_orders`' `unmappable` does: a default in either direction is a way
    to get this wrong without knowing the argument exists. `True` would let a
    caller resolve a live 0DTE row at 11:00; `False` would silently restore the
    evening-restart halt this parameter was added to end. It answers exactly one
    question — has TODAY's session closed and settled? — and it comes from
    `MarketCalendar.is_settled`, which knows about half-days. It is read only for
    a row expiring today; a past expiry resolves regardless and a future one
    never does.
    """
    verdicts: list[ExpiryVerdict] = []
    for d in discrepancies:
        if d.kind != MISSING_AT_BROKER:
            continue
        expiry = occ_expiry(d.symbol)
        if expiry is None:
            verdicts.append(ExpiryVerdict(d, None, False, KEPT_EXPIRY_UNREADABLE))
        elif expiry > today:
            verdicts.append(ExpiryVerdict(d, expiry, False, KEPT_NOT_YET_EXPIRED))
        elif expiry == today and session_settled:
            verdicts.append(ExpiryVerdict(d, expiry, True, SETTLED_AT_BROKER))
        elif expiry == today:
            verdicts.append(ExpiryVerdict(d, expiry, False, KEPT_SESSION_NOT_SETTLED))
        else:
            verdicts.append(ExpiryVerdict(d, expiry, True, EXPIRED_AT_BROKER))
    return tuple(verdicts)


def last_clean_reconciliation(conn: sqlite3.Connection) -> date | None:
    """The trading date of the most recent reconciliation, if it was CLEAN.

    Reads the single latest `reconcile.result` record and requires `ok` — it does
    not search backwards for the last clean one. That is the intended semantics, not
    a shortcut: after a non-ok result, sandbox/live HARD-HALT and refuse to start
    until the cause is fixed, so "the last thing on record is a mismatch" means an
    operator is mid-investigation. Standing the ledger-clearing rail down in that
    state would be the worst possible moment for it.

    Written for `AuditLog.read`, whose contract is bounded, index-backed queries
    only: this is one row, on migration 0003's `(category, ts)` index, from a
    category that gets a couple of records a day.
    """
    row = conn.execute(
        """
        SELECT payload FROM audit_log
        WHERE category = 'reconcile.result'
        ORDER BY seq DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
        if not payload.get("ok"):
            return None
        stamped = payload.get("trading_date")
        return date.fromisoformat(stamped) if stamped else None
    except (TypeError, ValueError, json.JSONDecodeError):
        # A record predating the `trading_date` field, or an unreadable one. Reads
        # as "no clean marker", which refuses the clear. Never raises: this feeds a
        # safety decision and must not be able to take reconciliation down.
        return None


# -- asking the broker, as distinct from agreeing with it ---------------------
#
# Inner attempts per broker call, and the backoff between them. NOT config, and
# deliberately: this mirrors `session._fetch_accounts`'s own `attempts: int = 3`
# and `min(2.0 * attempt, 5.0)`, which is the established cadence for a transient
# cert failure on a single REST call. The two numbers that ARE config
# (`[reconcile] retry_interval_s`/`retry_budget_s`) govern the OUTER loop, which
# is the one an operator has a reason to tune.
_INNER_ATTEMPTS = 3


def _inner_backoff_s(attempt: int) -> float:
    return min(2.0 * attempt, 5.0)


class BrokerUnreachable(RuntimeError):
    """The broker could not be ASKED. Structurally distinct from a mismatch.

    **This is the whole of how the "never retry past a real mismatch" invariant is
    held, and it is held by mechanism rather than by discipline.** A mismatch is a
    `ReconciliationResult` with `ok=False` — a VALUE, returned by a broker that
    answered. It never travels as an exception out of `Reconciler.reconcile()`;
    `ReconciliationError` is constructed by the CALLER, after the value comes
    back. So a retry loop that wraps a call returning broker data cannot reach a
    mismatch: a mismatch returns, which exits the loop on the first pass having
    spent no attempts at all.

    This, by contrast, is raised ONLY from `_ask_broker` — a wrapper that contains
    no comparison logic — once its bounded attempts are spent. The two cases
    cannot be confused because they do not travel by the same mechanism.

    `leg` says WHICH question went unanswered ("positions", "orders", "login",
    "accounts"). That matters more than it looks: it separates "one endpoint is
    sick" from "the API is down", which is the same reason
    `account.refresh_failed` grew its own `leg` field on 2026-08-26.
    """

    def __init__(self, leg: str, attempts: int, last: BaseException | None) -> None:
        detail = f"{type(last).__name__}: {last}" if last is not None else "unknown"
        super().__init__(
            f"could not reach the broker to ask for {leg} after {attempts} "
            f"attempt(s): {detail}"
        )
        self.leg = leg
        self.attempts = attempts
        self.last = last

    def as_payload(self) -> dict[str, Any]:
        return {
            "leg": self.leg,
            "attempts": self.attempts,
            "error_type": type(self.last).__name__ if self.last is not None else None,
            "error": str(self.last)[:500] if self.last is not None else None,
        }


@dataclass(frozen=True)
class ReconcileRetry:
    """How hard to try to ASK the broker. Never how hard to try to agree with it.

    Defaults to NO retry and no extra bound, so a bare `Reconciler(...)` — the
    tools, and every test that is not about this — behaves exactly as it did
    before 2026-08-26. Production never uses the default: `reconcile_with_retry`
    builds this from config and is the only thing the two real call sites call.

    `request_timeout_s` is applied by the CALLER as an `asyncio.timeout` around
    the port await, rather than being pushed into the port. That is deliberate —
    the ports (`BrokerAccountPort`, the execution port) are narrow by design and
    a timeout argument on every method is the wrong seam. It is safe here in a way
    it explicitly is NOT for the account refresher: these are async httpx calls on
    the loop, so cancelling them actually cancels the request, where cancelling a
    worker thread's await abandons a thread that keeps running
    (→ docs/decisions/2026-08-26-account-refresh-off-the-event-loop.md).
    """

    attempts: int = 1
    request_timeout_s: float | None = None

    @staticmethod
    def from_config(config: Any) -> "ReconcileRetry":
        return ReconcileRetry(
            attempts=_INNER_ATTEMPTS,
            request_timeout_s=config.reconcile.request_timeout_s,
        )


#: What a bare `Reconciler` gets: one attempt, no added bound, failures raised as
#: `BrokerUnreachable` all the same so the type is uniform across call sites.
NO_RETRY = ReconcileRetry()

#: Reports one failed inner attempt: (leg, attempt, attempts, exception).
AttemptFailedFn = Callable[[str, int, int, BaseException], None]


async def _ask_broker(
    leg: str,
    call: Callable[[], Awaitable[Any]],
    *,
    retry: ReconcileRetry,
    on_attempt_failed: AttemptFailedFn | None = None,
) -> Any:
    """Await ONE broker call, retrying transient failures. Contains no comparison.

    **The retry's scope is drawn by position in the call graph, not by exception
    type**, and that is the point. A type list would have to name
    `TastytradeError`, `httpx2` transport errors AND `httpcore2.ReadTimeout` —
    which escaped unwrapped in the 2026-08-26 traceback — and would silently stop
    matching the day the SDK renames one, the fragility `account/port.py` already
    documents. Because only the port await is inside the `try`, a `KeyError` in
    the comparison or a sqlite failure in the expected-position read cannot be
    retried: they are not in scope.

    **One type IS load-bearing, and only one.** `AuditedExecution.get_live_orders`
    writes a Tier 1 `order.unmappable_live` record inside the port call, so an
    `AuditWriteError` can surface from what otherwise looks like a pure read. A
    dead recorder halts the process (CLAUDE.md, no auto-retry) and must never be
    re-attempted here — so it is re-raised untouched, before anything else.
    """
    last: BaseException | None = None
    for attempt in range(1, retry.attempts + 1):
        try:
            if retry.request_timeout_s is None:
                return await call()
            async with asyncio.timeout(retry.request_timeout_s):
                return await call()
        except AuditWriteError:
            # See the docstring. The recorder is dead process-wide; retrying it is
            # the one thing CLAUDE.md forbids outright.
            raise
        except Exception as exc:  # noqa: BLE001 — reported, then re-raised as BrokerUnreachable
            last = exc
            if on_attempt_failed is not None:
                on_attempt_failed(leg, attempt, retry.attempts, exc)
            if attempt < retry.attempts:
                await asyncio.sleep(_inner_backoff_s(attempt))
    raise BrokerUnreachable(leg, retry.attempts, last)


async def reconcile_and_record(
    reconciler: "Reconciler",
    *,
    audit: "AuditLog",
    environment: str,
    today: date,
    session_settled: bool,
    alert: AlertFn,
    order_store: Any = None,
    retry: ReconcileRetry = NO_RETRY,
) -> ReconciliationResult:
    """The WHOLE of reconciling once: reset-check, compare, record, alert.

    Both refusal sites call this and differ only in HOW they refuse — process start
    (`app.Application._reconcile`) refuses by not finishing startup, per-session
    (`session._reconcile_or_abort`) refuses by halting a process already up. Keeping
    the sandbox reset handling, the `trading_date` stamp and the mismatch alert in
    ONE function is what stops those two paths drifting; before chunk 5 they already
    had two copies of the record-and-alert sequence, and the reset logic would have
    made that three of everything.

    `order_store` enables the sandbox-reset check AND expired-position resolution,
    both of which write to Argus's own ledger. Absent (shadow, or any caller
    without an order ledger) both are skipped entirely — shadow submits nothing, so
    nothing ever writes `positions` there and there is nothing to clear or resolve.

    Expiry resolution sits BETWEEN the compare and the record, which is the only
    place it can be: it needs the compare's verdict to know which rows the broker
    does not hold (never a clock assumption), and it must finish before the result
    is recorded and alerted on, because a row expiry explains was never a mismatch.
    So `reconcile.result` carries the SURVIVING verdict, and the Tier 1
    `reconcile.expired_positions` record written just before it says what changed
    and why.

    The `trading_date` stamp on `reconcile.result` is what makes the next run's
    rollover comparison possible (`last_clean_reconciliation`); it is written on
    every result, clean or not, but only a clean one is ever read back.

    `session_settled` is the ONE new input the same-day expiry carve-out needs
    (2026-08-25), and it is threaded rather than computed here for the reason the
    whole function exists: every call site must reach the same answer from the
    same clock. All three build it identically —
    `MarketCalendar.from_config(config.market).is_settled(today, clock.et_now())`
    — never from host-local time, and never from a second notion of "today".
    """
    if order_store is not None:
        # OUTSIDE the retry, and that is load-bearing. This writes a Tier 1 record
        # and then DELETES position rows; its "record before delete" crash-ordering
        # is written for a single pass, and a second evaluation could record two
        # resets for one. Everything after the compare (`_resolve_expired_positions`,
        # the `reconcile.result` write) is outside for the same reason.
        await _clear_ledger_if_reset(
            audit=audit, environment=environment, today=today, order_store=order_store
        )

    def _attempt_failed(leg: str, attempt: int, attempts: int, exc: BaseException) -> None:
        # Tier 2, matching `session.login_attempt_failed`. A retry that is silent
        # for minutes is this task's own complaint in miniature.
        logger.warning(
            "reconcile: %s fetch attempt %d/%d failed: %s: %s",
            leg, attempt, attempts, type(exc).__name__, exc,
        )
        audit.telemetry(
            "reconcile.unreachable_attempt",
            {
                "leg": leg,
                "attempt": attempt,
                "attempts": attempts,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )

    # `BrokerUnreachable` propagates from here deliberately UNRECORDED and
    # UNALERTED. The Tier 1 `reconcile.unreachable` and its alert belong to
    # `reconcile_with_retry`, which calls this repeatedly: writing them here would
    # put ~30 Tier 1 records and an alert storm on the log for ONE condition that
    # has not been given up on yet — the 2026-08-14 shape (34 restarts, ~68 Slack
    # messages). Per-attempt visibility is `_attempt_failed`'s Tier 2 telemetry;
    # GIVING UP is the crash-relevant fact, and it happens once.
    result = await reconciler.reconcile(retry=retry, on_attempt_failed=_attempt_failed)
    if order_store is not None:
        result = await _resolve_expired_positions(
            audit=audit,
            alert=alert,
            result=result,
            today=today,
            session_settled=session_settled,
            order_store=order_store,
        )
    # The shared-account hint is computed once, here, and folded into BOTH the
    # durable record and the alert — one copy, so the two can never disagree about
    # what the likely cause was. The key is ABSENT rather than None when there is
    # no hint: a `"hint": null` on every clean row is noise on the durable chain,
    # and a reader testing `"hint" in payload` is clearer than one testing for
    # non-null (design §3.6, 2026-09-10).
    hint = shared_account_hint(result, environment=environment)
    payload = result.as_payload() if hint is None else {**result.as_payload(), "hint": hint}
    await audit.record(
        "reconcile.result", {**payload, "trading_date": today.isoformat()}
    )
    if not result.ok:
        alert("reconciliation_mismatch", payload)
    return result


#: What one outer attempt needs from the clock: the trading date, and whether that
#: date's session has settled. ONE reading builds BOTH — never two, which could
#: straddle midnight and judge one day's expiry against another day's close.
ReconcileInputsFn = Callable[[], tuple[date, bool]]


async def _sleep_or_stop(seconds: float, stop: asyncio.Event | None) -> None:
    """Sleep, returning early the moment `stop` is set.

    Same purpose as `session._interruptible_sleep`, a different mechanism: the wait
    is a RACE against `asyncio.sleep` rather than a `wait_for` timeout, so the sleep
    is the module's own `asyncio.sleep` and the suite can substitute it. That is not
    a test convenience — this interval is 60 seconds in production over a
    30-minute budget, and a test suite that could not fake it would either take
    half an hour or not cover the loop at all.
    """
    if stop is None:
        await asyncio.sleep(seconds)
        return
    stopped = asyncio.ensure_future(stop.wait())
    sleeper = asyncio.ensure_future(asyncio.sleep(seconds))
    try:
        await asyncio.wait({stopped, sleeper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
        sleeper.cancel()


async def _give_up(
    exc: BrokerUnreachable,
    *,
    audit: "AuditLog",
    alert: AlertFn,
    today: date,
    attempt: int,
    elapsed: float,
    reason: str,
) -> NoReturn:
    """Record the episode ONCE, alert ONCE, and re-raise. Never returns.

    Tier 1: giving up on reaching the broker is a crash-relevant boundary — it is
    the fact that explains a parked process or a missing session, and the one an
    operator goes looking for. Exactly one record and one alert per EPISODE, not
    per attempt: the 2026-08-14 storm was 34 restarts re-announcing one unchanged
    condition, and a 30-minute loop at 60s is the same shape waiting to happen.
    """
    payload = {
        **exc.as_payload(),
        "trading_date": today.isoformat(),
        "outer_attempts": attempt,
        "elapsed_s": round(elapsed, 1),
        "gave_up": reason,
    }
    await audit.record("reconcile.unreachable", payload)
    alert("reconcile_unreachable", payload)
    raise exc


async def reconcile_with_retry(
    reconciler: "Reconciler",
    *,
    config: Any,
    audit: "AuditLog",
    alert: AlertFn,
    inputs: ReconcileInputsFn,
    order_store: Any = None,
    stop: asyncio.Event | None = None,
    now: Callable[[], float] = time.monotonic,
) -> ReconciliationResult:
    """Reconcile, retrying an UNREACHABLE broker until the configured budget runs out.

    The ONE outer loop both call sites take, so the cadence cannot drift — the same
    discipline `mismatch_halts_process` and `reconcile_and_record` already carry for
    the verdict and the record. The sites differ only in what they do with the
    `BrokerUnreachable` this finally raises: process start PARKS, the per-day site
    ABORTS THE DAY. Neither is decided here.

    **While this loop runs the process is alive, bound, serving /status and /kill,
    and NOT halted.** That is the whole design (2026-08-26): the control surface
    binds before the reconciliation rail, so the retry can happen in a state that
    needs nothing undone when it succeeds — as opposed to halting and then clearing
    the halt, which would mean making `KillSwitch` disengageable and `_halted`
    clearable. Two one-way latches stay one-way because this loop sits in front of
    them rather than inside them.

    Three ways out:

    - **Success** — return the verdict. A MISMATCH is a success for this loop's
      purposes: the broker answered, so there is nothing to retry, and the verdict
      goes straight back to the caller to refuse on. A retry can never reach a
      mismatch (see `BrokerUnreachable`), and this is where that matters most.
    - **`stop` set** — an operator pressed `/kill`, or the session is ending. Give
      up now: "stop trading" reading as "stop trying to start trading" is the only
      sensible interpretation, and the caller's refusal follows immediately.
    - **Budget exhausted** — raise the last `BrokerUnreachable`, having written the
      single Tier 1 `reconcile.unreachable` record and fired the single alert for
      the whole episode.

    `SessionAborted` and anything else pass straight through UNRETRIED. That split
    is deliberate and it is what keeps a wrong `account_number` in `argus.toml` from
    spending the entire budget re-proving a permanent fact: "could not ask" is
    `BrokerUnreachable`, "asked, and the configuration is wrong" is not.

    `inputs` is re-read per attempt rather than captured once. Over a 30-minute
    budget the trading date can roll over and a session can settle — and an evening
    restart is exactly when a process-start reconcile happens (2026-08-25), so a
    date captured before the loop is a real hazard, not a theoretical one.
    """
    retry = ReconcileRetry.from_config(config)
    interval = config.reconcile.retry_interval_s
    budget = config.reconcile.retry_budget_s
    started = now()
    attempt = 0
    last_unreachable: BrokerUnreachable | None = None
    while True:
        if stop is not None and stop.is_set() and last_unreachable is not None:
            # Stopped during the wait. Give up HERE rather than making one more
            # broker call and discovering it in the handler below — asking again
            # after being told to stop is exactly the round trip nobody wants.
            await _give_up(
                last_unreachable, audit=audit, alert=alert, today=inputs()[0],
                attempt=attempt, elapsed=now() - started,
                reason="stop_requested",
            )
        attempt += 1
        today, settled = inputs()
        try:
            return await reconcile_and_record(
                reconciler,
                audit=audit,
                environment=config.environment,
                today=today,
                session_settled=settled,
                alert=alert,
                order_store=order_store,
                retry=retry,
            )
        except BrokerUnreachable as exc:
            last_unreachable = exc
            elapsed = now() - started
            stopping = stop is not None and stop.is_set()
            if stopping or elapsed + interval > budget:
                await _give_up(
                    exc, audit=audit, alert=alert, today=today, attempt=attempt,
                    elapsed=elapsed,
                    reason="stop_requested" if stopping else "budget_exhausted",
                )
            logger.warning(
                "reconcile unreachable (%s); retrying in %.0fs "
                "(attempt %d, %.0fs of %.0fs budget spent)",
                exc, interval, attempt, elapsed, budget,
            )
            # INTERRUPTIBLE, not a bare sleep: at a 60s cadence a plain sleep would
            # sit on an operator's `/kill` for up to a minute, which is precisely
            # what "reachable in one action" is not.
            await _sleep_or_stop(interval, stop)


async def _resolve_expired_positions(
    *,
    audit: "AuditLog",
    alert: AlertFn,
    result: ReconciliationResult,
    today: date,
    session_settled: bool,
    order_store: Any,
) -> ReconciliationResult:
    """Drop the missing-at-broker rows their own expiry explains. Never widens.

    Returns the result the caller should act on: identical when nothing was
    explained, and otherwise the same result minus the rows actually removed from
    the ledger. `ok` is recomputed from what survives, and order discrepancies ride
    through untouched — this rule is about positions and knows nothing about orders.

    Recording order mirrors `_clear_ledger_if_reset` and for the same reason: the
    Tier 1 record goes in BEFORE the delete, so a process that dies between the two
    leaves an explanation with the ledger still intact — the recoverable half. The
    reverse ordering loses the account of why the table shrank, which is exactly
    what a post-mortem would be looking for.

    A row that does NOT come back from `remove_positions` (it moved or vanished
    between the compare and the delete) keeps its discrepancy and therefore keeps
    the halt. That is fail-closed by construction rather than by a check, and it
    alerts, because the ledger disagreeing with itself mid-reconcile is worth
    knowing about even though the outcome is safe.
    """
    verdicts = expiry_verdicts(
        result.discrepancies, today, session_settled=session_settled
    )
    if not verdicts:
        # Nothing missing at the broker at all — the overwhelmingly common case,
        # including every clean reconcile. Silent by design: a record here would sit
        # in front of every session saying nothing happened.
        return result

    resolved = tuple(v for v in verdicts if v.resolved)
    payload = {
        "trading_date": today.isoformat(),
        # Recorded alongside the verdicts because it is the input a same-day
        # verdict turned on, and it is not derivable from the record afterwards:
        # the `ts` is UTC and the settlement mark moves on a half-day.
        "session_settled": session_settled,
        "verdicts": [v.to_payload() for v in verdicts],
    }
    if not resolved:
        # Missing rows, none of them explained by expiry. The halt stands and
        # `reconcile.result` already carries the discrepancies; this is Tier 2
        # because its only content is WHY each one was not excused — useful next to
        # a halt, not worth a crash-critical write.
        audit.telemetry("reconcile.expiry_check", payload)
        return result

    logger.warning(
        "resolving %d expired position row(s) absent at the broker (today is %s): %s",
        len(resolved),
        today,
        ", ".join(f"{v.discrepancy.symbol} exp {v.expiry}" for v in resolved),
    )
    await audit.record("reconcile.expired_positions", payload)
    removed = await order_store.remove_positions(
        {v.discrepancy.symbol: v.discrepancy.expected_qty for v in resolved}
    )
    if len(removed) != len(resolved):
        alert(
            "expired_position_removal_incomplete",
            {
                "expected": [v.discrepancy.symbol for v in resolved],
                "removed": sorted(removed),
            },
        )

    survivors = tuple(d for d in result.discrepancies if d.symbol not in removed)
    return ReconciliationResult(
        ok=not survivors and not result.order_discrepancies,
        discrepancies=survivors,
        order_discrepancies=result.order_discrepancies,
    )


async def _clear_ledger_if_reset(
    *, audit: "AuditLog", environment: str, today: date, order_store: Any
) -> ResetDecision:
    """Evaluate the reset decision and act on it. Always records the verdict.

    The Tier 1 write happens on the CLEAR branch only; the no-clear branches are
    Tier 2. A ledger that was deleted is a crash-relevant fact about expected state
    and must survive; "checked, nothing to do" is the every-morning case and would
    otherwise put a Tier 1 record in front of every session for no information.
    """
    ledger = await order_store.ledger_state()
    decision = sandbox_reset_decision(
        environment=environment,
        today=today,
        last_clean_trading_date=await audit.read(last_clean_reconciliation),
        ledger=ledger,
    )
    if not decision.clear:
        audit.telemetry("reconcile.reset_check", decision.to_payload())
        return decision

    # Deliberately BEFORE the delete: if the process dies between the two, the
    # record says a clear was about to happen and the ledger is still intact — the
    # recoverable half. The reverse ordering loses the explanation for an
    # already-empty ledger, which is exactly what a post-mortem would need.
    logger.warning(
        "sandbox 24h reset assumed: clearing %d position(s), KEEPING %d order(s), "
        "last written on %s (today is %s)",
        ledger.positions, ledger.orders, ledger.newest_trading_date, today,
    )
    await audit.record("reconcile.sandbox_reset", decision.to_payload())
    await order_store.clear_reset_holdings()
    return decision


class ReconciliationError(RuntimeError):
    """Raised to refuse resuming automated logic when broker/internal state differ."""

    def __init__(self, result: ReconciliationResult) -> None:
        n = result.total_discrepancies
        super().__init__(f"broker reconciliation failed with {n} discrepancy(ies); refusing to resume")
        self.result = result


def mismatch_halts_process(environment: str) -> bool:
    """Whether a reconcile mismatch must halt the whole PROCESS or only the day.

    The single source of truth for the environment boundary, used by BOTH reconcile
    call sites (`app.Application._reconcile` at process start and
    `session._reconcile_or_abort` per trading day) so they can never drift:

      - `True` (sandbox/live): a broker/expected mismatch is exactly the
        crash-recovery scenario continuous reconciliation exists to catch (spec
        §6). Refuse to resume PROCESS-WIDE — real order state is at stake, so
        "alert and try again tomorrow" is not enough.
      - `False` (shadow): abort just the day and keep the process alive to retry
        tomorrow. Shadow submits nothing, so there is no order state to protect,
        and a hard halt would let a stale sandbox position (wiped nightly) brick
        every following day.

    The boundary is shadow vs. everything else — deliberately NOT live vs.
    everything else. Sandbox's whole purpose (spec §10 step 2) is to prove out
    reconciliation and crash recovery under conditions close to live with no money
    on the line; if sandbox inherited shadow's lenient path, the hard-halt
    behaviour would never actually be exercised until it mattered on a live
    account.
    """
    return environment != "shadow"


class HaltGrade(StrEnum):
    """How WIDE a halt's refusal reaches. Orthogonal to `mismatch_halts_process`.

    That function answers "does this environment halt the PROCESS or just the
    day?"; this one answers "given that it halts, what does the halt stop?" Two
    questions, two functions, and shadow never reaches this one.

    - `ALL` — today's behaviour and the default everywhere. No further sessions,
      the current one ends, the process parks. Every halt source that is not a
      reconciliation verdict gets this without being edited.
    - `ENTRIES` — the kill switch is engaged (so nothing enters, ever, until a
      restart) but the process is not asked to stop, so the scheduler keeps
      running and the day's session comes up with its entry hook unwired. The
      watchdog therefore runs: virtual stops are evaluated, positions are marked,
      and the time-based flatten fires.

    **`ALL` is the strict value and the default in every signature that takes
    one.** A caller who forgets the argument, or a future halt source nobody
    graded, gets the answer that stops everything.
    """

    ALL = "all"
    ENTRIES = "entries"

    def tighten(self, other: "HaltGrade") -> "HaltGrade":
        """The stricter of the two. Grades escalate; they never relax.

        **The opposite update rule from `HaltRecord.reason`, which is
        first-writer-wins** (2026-08-17: a cascade's first cause is the useful
        one, and a downstream symptom must not overwrite it). A grade needs
        strictest-wins instead — an `ENTRIES` halt followed by an `ALL` halt is
        `ALL`, and never the reverse. Two fields on one record with opposite
        rules is exactly what a later tidy-up unifies by accident, so it is a
        method here rather than an inline `min()` at the one call site.
        """
        return HaltGrade.ALL if HaltGrade.ALL in (self, other) else HaltGrade.ENTRIES


def halt_grade(result: ReconciliationResult) -> HaltGrade:
    """How wide a halt this verdict earns. Pure; no I/O; no clock.

    → docs/decisions/2026-08-29-halt-scope-split.md

    `ENTRIES` if and only if ALL THREE hold. Anything else is `ALL`, including a
    `not ok` result carrying no discrepancies at all — unreachable today
    (`ok` is derived from the discrepancy sets) and therefore exactly the shape
    that must fail closed if it ever becomes reachable.

    1. **No position discrepancy of ANY kind.** Not one, not a benign-looking
       one. `missing_at_broker` means the ledger holds a leg the broker does not
       — and the expiry carve-out has already run and declined to explain it,
       since `expiry_verdicts` is applied inside `reconcile_and_record` between
       the compare and the record. A watchdog running against that row would send
       a *closing* order for a leg the account does not hold, which is an OPENING
       trade, unmanaged, on expiry day. `quantity_mismatch` mis-sizes a close and
       an over-close inverts the position. `unexpected_at_broker` says the broker
       holds a leg Argus did not expect, which is either a fill never booked or
       somebody else trading the account — neither is a state in which to start
       routing closes off a local table.

    2. **Every order discrepancy is `UNEXPECTED_WORKING_ORDER`.**
       `UNEXPECTED_FILLED_ORDER` is a position claim wearing an order's clothes:
       it usually shows in the position half too, but a fill that nets against an
       existing expectation, or a positions endpoint trailing the orders
       endpoint, hides it. `UNMAPPABLE_BROKER_ORDER` answers no question at all
       — it could be working, it could be filled — and an unanswerable question
       is never permission, the same rule `DuplicateOrderGuard` and
       `_await_settled` already hold.

    3. **Every one of them is one of Argus's OWN OPENING orders**, by the
       identifier Argus minted. A foreign identifier means somebody is trading the
       account by hand and there is no bound on what they do next. An
       `argus-close-<candidate>` means an exit Argus placed and lost track of —
       a position changing hands. An `argus-open-<candidate>` can only ever ADD a
       position, and if it fills, `FillConsumer` books it (the identifier passes
       `is_argus_identifier`, and `store._apply` upserts the order row and the
       position rows in one transaction) and the watchdog adopts it on the next
       refresh. The system's response to that order filling is correct by
       existing mechanism.

    **The direction comes from `parse_external_identifier`, NEVER from
    `infer_direction`.** tastytrade labels each leg relative to the ACCOUNT'S
    position, so a closing order on a chained book comes back wearing opening leg
    actions and `infer_direction` calls it OPEN (2026-08-25, still open). The
    identifier is a string Argus generated and the broker echoes back verbatim;
    it is immune to that. A future fix to `infer_direction` must not "unify"
    these — this call site wants the minted answer specifically.

    **There is deliberately NO symbol or strike comparison here**, and adding one
    would be a bug rather than a tightening. `signal.width = 5.0` against SPX's
    5-point ladder makes consecutive spreads share a strike, and a shared strike
    nets to ZERO in `positions` — `SqlitePositionStore.open_positions()` reads
    `WHERE signed_quantity != 0`, so the strike two live spreads are both standing
    on is ABSENT from the expected map. A "does this order touch a held position?"
    test would answer "no" for precisely that strike.
    → docs/decisions/2026-08-17-overlapping-verticals.md
    """
    if result.ok:
        # Not a halt at all. Callers gate on `ok` first; answering `ALL` here
        # keeps the function total without inventing a third value for it.
        return HaltGrade.ALL
    if result.discrepancies:
        return HaltGrade.ALL
    if not result.order_discrepancies:
        return HaltGrade.ALL
    for d in result.order_discrepancies:
        if d.kind != UNEXPECTED_WORKING_ORDER:
            return HaltGrade.ALL
        parsed = parse_external_identifier(d.external_identifier)
        if parsed is None or parsed[0] is not OrderDirection.OPEN:
            return HaltGrade.ALL
    return HaltGrade.ENTRIES


#: The one shape that means "somebody else is trading this account". Formatted
#: with the count, never with the symbols — the symbols are already in the payload
#: and repeating them in prose makes a long alert longer.
_SHARED_ACCOUNT_HINT = (
    "{n} broker position(s) Argus has no record of. If this account is shared, it "
    "is NOT SUPPORTED — Argus's account-derived rails all read whole-account "
    "numbers: see docs/decisions/2026-08-31-live-account-is-not-dedicated.md"
)


def shared_account_hint(
    result: "ReconciliationResult", *, environment: str
) -> str | None:
    """A sentence naming the LIKELY cause of a live position mismatch. Never a verdict.

    DIAGNOSTIC ONLY. It is read by no rail, it reaches `ok`, `halt_grade` and
    `mismatch_halts_process` through no path, and a wrong hint costs an operator a
    second's confusion where a wrong verdict costs a trading day. That asymmetry is
    why this is prose on a payload and not a discrepancy kind.

    **Live only**, and that is not timidity: cert produces `unexpected_at_broker`
    rows legitimately, inside the sandbox reset's 24-hour order-reporting window,
    and a hint that fires on the ordinary cert failure is a hint nobody reads by
    the time live needs it.

    **Positions only.** A foreign WORKING order is the same diagnosis, but the test
    for "this order is not Argus's" already exists inside `halt_grade`, and a second
    copy here is two places that must agree about the same question forever. Named
    as a known gap rather than duplicated: a shared account holding only a manual
    working order gets the ordinary alert with no hint.
    """
    if environment != "live":
        return None
    if not result.discrepancies:
        return None
    if any(d.kind != UNEXPECTED_AT_BROKER for d in result.discrepancies):
        # A mixed set is a DIFFERENT problem — a missing_at_broker or a quantity
        # mismatch is Argus's own book disagreeing, and pointing at the shared
        # account there sends the operator to the wrong document.
        return None
    return _SHARED_ACCOUNT_HINT.format(n=len(result.discrepancies))


class Reconciler:
    """Fetches authoritative broker state and compares it to what Argus expected.

    Depends only on narrow ports, so it's fully testable with fakes. It returns a
    verdict; it does not decide what to do about it (that's the app's
    refuse-and-alert responsibility) and does not write audit itself.

    `execution`/`order_store` are optional and travel together: with both, the
    verdict also covers ORDERS (see `reconcile_orders`); with neither, it is the
    position-only check it has always been. Optional because shadow has no
    execution port at all — it submits nothing, so there is nothing of its own to
    find at the broker — while sandbox/live get the full check. Passing one
    without the other is a wiring mistake and raises rather than silently
    reconciling half of what the caller asked for.
    """

    def __init__(
        self,
        broker: BrokerAccountPort,
        expected: "PositionStore",
        *,
        # Typed `Any` deliberately: annotating these as `OrderExecutionPort` /
        # `OrderStore` would import `execution.broker` here, dragging the
        # tastytrade SDK into what is otherwise a pure comparison module that
        # depends only on narrow ports. Duck-typed against the same protocols.
        execution: Any = None,
        order_store: Any = None,
    ) -> None:
        if (execution is None) != (order_store is None):
            raise ValueError(
                "Reconciler needs both `execution` and `order_store` to reconcile "
                "orders, or neither — one alone cannot answer whether a broker "
                "order is one of Argus's"
            )
        self._broker = broker
        self._expected = expected
        self._execution = execution
        self._order_store = order_store

    @property
    def order_store(self) -> Any:
        """The order ledger this reconciler compares against, or None.

        Public so a caller can hand the SAME ledger to the sandbox-reset check without
        reaching into a private attribute — the ledger that may be cleared must always
        be the ledger that was compared.
        """
        return self._order_store

    async def reconcile(
        self,
        *,
        retry: ReconcileRetry = NO_RETRY,
        on_attempt_failed: AttemptFailedFn | None = None,
    ) -> ReconciliationResult:
        """Ask the broker, then compare. The two halves stay strictly separated.

        Every broker await goes through `_ask_broker`; nothing else does. So the
        compare below can only ever run on data a broker actually returned, and a
        mismatch — which this method RETURNS — is unreachable from the retry. See
        `BrokerUnreachable` for why that is the whole invariant.

        `retry` defaults to none, so a bare `Reconciler` behaves exactly as it did
        before 2026-08-26. `reconcile_with_retry` is what production goes through.
        """
        positions = await _ask_broker(
            "positions", self._broker.fetch_positions,
            retry=retry, on_attempt_failed=on_attempt_failed,
        )
        broker_map = {p.symbol: p.signed_quantity for p in positions if p.signed_quantity != 0}
        expected_map = self._expected.open_positions()
        result = reconcile_positions(expected_map, broker_map)

        if self._execution is None:
            return result

        # A SECOND, independent budget, deliberately: these are two questions to
        # two endpoints behind one gateway, and a positions endpoint that hangs
        # must not spend the orders endpoint's attempts and turn a one-sided
        # outage into a refusal to reconcile at all.
        live = await _ask_broker(
            "orders", self._execution.get_live_orders,
            retry=retry, on_attempt_failed=on_attempt_failed,
        )
        known = await self._order_store.known_orders(
            o.broker_order_id for o in live.orders if o.broker_order_id is not None
        )
        # `known_orders` is only asked about the MAPPABLE ids: an unmappable order
        # is a discrepancy whether or not Argus placed it (see `reconcile_orders`),
        # so looking it up in the ledger would answer a question nobody asks.
        order_discrepancies = reconcile_orders(
            known, live.orders, unmappable=live.unmappable
        )
        return ReconciliationResult(
            ok=result.ok and not order_discrepancies,
            discrepancies=result.discrepancies,
            order_discrepancies=order_discrepancies,
        )


# -- expected-position stores -------------------------------------------------


class PositionStore:
    """Argus's record of the positions it believes it holds. The reconciliation
    target. Subclassed by the in-memory and SQLite-backed implementations."""

    def open_positions(self) -> dict[str, int]:  # pragma: no cover - interface
        raise NotImplementedError


class InMemoryPositionStore(PositionStore):
    def __init__(self, positions: dict[str, int] | None = None) -> None:
        self._positions = dict(positions or {})

    def set(self, symbol: str, signed_quantity: int) -> None:
        if signed_quantity == 0:
            self._positions.pop(symbol, None)
        else:
            self._positions[symbol] = signed_quantity

    def open_positions(self) -> dict[str, int]:
        return {s: q for s, q in self._positions.items() if q != 0}


class SqlitePositionStore(PositionStore):
    """Reads expected positions from the `positions` table (migration 0002).

    Empty until order-recording is built; that's a valid state — a fresh Argus
    against a flat account reconciles clean, and a broker holding anything Argus
    didn't record trips the mismatch.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def open_positions(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT symbol, signed_quantity FROM positions WHERE signed_quantity != 0"
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}
