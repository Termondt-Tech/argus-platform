"""Argus's durable order record, and the fill -> `positions` path (spec §6/§7).

This is the module that turns reconciliation's expected-position set from
permanently-empty into something real. Everything else about the reconciliation
rail already existed; what was missing was anything that ever WROTE `positions`.

Three things it is deliberately not:

- **Not an order state machine.** Nothing here decides what to do next, and
  nothing replays a message that was already sent. It records what the broker
  said, so that after a crash Argus can ask the broker what actually happened and
  compare. The recovery story is "ask and halt on anything unexplained", never
  "resume where we left off".
- **Not a second writer.** All SQL runs through an injected `run` seam, which in
  production is `AuditLog.write` — the process's ONE writer connection, under the
  chain lock. See that method for why a private connection here would be a bug.
- **Not a fill-price ledger.** `positions.average_open_price` is best-effort (see
  `_apply_position`). Reconciliation compares symbols and signed quantities;
  nothing safety-critical reads the price.

**Idempotency is the whole design.** `PlacedOrder` events carry the CUMULATIVE
`remaining_quantity` per leg and are re-sent on every status change — several
times for one fill, and again for anything already terminal. So the store keeps
its own `applied_quantity` per leg and books only the difference. Apply the same
event twice and the second one is a no-op; restart mid-day and the ledger is on
disk, so nothing is double-booked. That property is what lets the fill consumer
be a dumb "here is another event" callback with no de-duplication of its own.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from argus.clock import ET
from argus.execution.models import (
    ORDER_ID_PREFIX,
    LegAction,
    Order,
    OrderDirection,
    OrderLeg,
)
from argus.occ import occ_expiry

# run(fn) -> awaitable of fn(conn). The database seam: `AuditLog.write` in the
# app, a trivial wrapper over a connection in tests. Injected rather than taking a
# connection directly so the store cannot accidentally become a second writer.
RunFn = Callable[[Callable[[sqlite3.Connection], Any]], Awaitable[Any]]
ClockFn = Callable[[], str]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class AppliedFill:
    """Contracts booked into `positions` by one event — the DELTA, not the total.

    Emitted so the caller can write one audit record per real movement. An event
    that tells us nothing new produces none of these at all, which is what keeps
    the Tier 1 trail free of the repeat notifications the stream sends.
    """

    broker_order_id: int
    external_identifier: str
    symbol: str
    action: LegAction
    contracts: int              # newly applied on this leg; always > 0
    signed_delta: int           # + long, - short — what moved the position
    price: float | None         # avg price of the newly-applied slice, if reported
    position_before: int
    position_after: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "broker_order_id": self.broker_order_id,
            "external_identifier": self.external_identifier,
            "symbol": self.symbol,
            "action": str(self.action),
            "contracts": self.contracts,
            "signed_delta": self.signed_delta,
            "price": self.price,
            "position_before": self.position_before,
            "position_after": self.position_after,
        }


class CreditBasis(StrEnum):
    """Where a held spread's `entry_credit` came from. Travels with the number.

    Only `REALIZED` may be used in an analysis of `credit_multiple`; both LIMIT
    bases are low by an unknown amount and are EXCLUDED from such a dataset rather
    than corrected after the fact.
    """

    #: The contracts-weighted average of what the opening fills actually collected,
    #: across every opening order the trade idea has.
    REALIZED = "realized"
    #: The filled opening order's limit. The broker priced no fills on at least one
    #: leg — a known state (`LegFill` is best-effort detail), and the pre-0005
    #: behaviour for every position. The basis is LOW, so the stop is TIGHT.
    LIMIT = "limit"
    #: The fills ARE priced and say a credit spread was opened for a debit. The
    #: spread is still WATCHED, on the limit — a price-arithmetic contradiction
    #: must not cost a live vertical its virtual stop, its flatten and its row on
    #: `/status`, which is what routing it to `malformed` would do. Loud rather
    #: than quiet: it pages once per position, because two ledger accounts of the
    #: same order disagreeing is worth knowing about.
    LIMIT_FILLS_INCOHERENT = "limit_fills_incoherent"


@dataclass(frozen=True)
class OpenSpread:
    """One open credit spread, reassembled from the ledger for the watchdog (chunk 4).

    **Why this comes from the ORDER ledger and not from `positions`.** The
    watchdog's dominant trigger is "cost to close has reached N x the credit
    received", and `positions` cannot answer it: that table is keyed by OCC LEG
    symbol, holds no spread-level row, and its only price column
    (`average_open_price`) is documented right here as best-effort and
    safety-irrelevant — it is 0.0 whenever the broker reported no per-fill prices,
    which is every case chunk 1 actually observed. `orders`/`order_legs` (migration
    0004) is the only place the two legs are grouped into a spread at all, and
    `orders.limit_price` is the only durable record of what the trade was priced at.

    **`entry_credit` is what the trade idea actually COLLECTED, since 2026-09-02.**
    It is the contracts-weighted average across every opening order the idea has,
    from `order_legs.priced_quantity`/`priced_notional` (migration 0005), and it
    falls back to the filled opening order's LIMIT when the broker priced no fills.
    `entry_credit_basis` says which of the two this is, and `entry_limit` carries
    the limit either way, so the stop and the operator never read different numbers.

    **What that corrects, and why it was left standing for two months.** Until
    migration 0005 this was the LIMIT, always. A limit order fills at the limit OR
    BETTER, so the basis was LOW and the 2x trigger fired EARLY — the safe
    direction, and the reason correcting it kept losing to other work. The gap was
    never a rounding error: five observations of it now exist and it is **one
    half-tick to one tick of price improvement** every time (0.55 -> 0.60 on
    2026-08-19, 18% on 2026-08-20, and on 2026-09-01 0.45 -> 0.50, 0.75 -> 0.775,
    0.55 -> 0.60). It carries straight into `credit_stop = credit_multiple x
    entry_credit`, so a basis low by a tick is a stop tight by two. On 2026-09-01
    that closed `put-7630-7625` at 0.925 against a stop of 0.90 computed from 0.45,
    where the realized 0.50 puts the stop at 1.00 and the position is never
    stopped out at all.

    **The premise that made the fix look expensive was stale.** This comment used
    to say `positions.average_open_price` is "0.0 whenever the broker reported no
    per-fill prices, which is every case chunk 1 actually observed", and conclude
    that capturing realized prices meant new machinery. Real per-fill prices have
    been arriving throughout: `_map_fills` builds `LegFill`, `OrderLeg.fill_price`
    averages the slice being booked, and `_apply_leg` has been computing it on
    every applied fill and writing it to the `order.fill` record all along. Only
    the DURABILITY at spread level was missing.

    `positions.average_open_price` is still not the source and cannot become one:
    it is keyed by SYMBOL, and chained verticals net to zero at a shared strike.
    Nor is the NEWEST filled opening order — a walk that reprices fills across
    several `broker_order_id`s under one `external_identifier` (proven 2026-09-01:
    1429812 filled 1 contract, 1429813 filled 1), so a per-order price misses rung
    one. The average is taken per LEG across every opening order in the trade idea,
    signed by `LegAction.sells`, which the broker does not relabel.

    **The `credit_multiple` prohibition survives in narrowed form.** Conclusions may
    be drawn only from positions whose basis is `REALIZED`; a `LIMIT` or
    `LIMIT_FILLS_INCOHERENT` one is EXCLUDED rather than corrected, its basis being
    low by an unknown amount. And a number measured in SANDBOX, where fills are not
    realistic, still cannot size a change on its own — fixing the basis removes a
    known bias, it does not make cert fills representative.

    `contracts` comes from what was APPLIED, not from what was ordered: the order
    says what was requested, `order_legs.applied_quantity` says what actually filled,
    net of any close. It stopped coming from `positions` on 2026-08-17 — that table
    is keyed by symbol and holds only the NET, so two concurrent verticals sharing a
    strike erased each other and left both unwatchable.
    → docs/decisions/2026-08-17-overlapping-verticals.md
    → docs/decisions/2026-09-02-the-credit-stop-basis-and-its-comparison.md
    """

    external_identifier: str
    broker_order_id: int
    candidate_id: str | None
    underlying: str
    short_symbol: str
    long_symbol: str
    #: Magnitudes currently held per leg, from this trade idea's own applied fills.
    short_quantity: int
    long_quantity: int
    #: THE number the stop is priced from. Points, positive (a credit).
    entry_credit: Decimal
    #: Where `entry_credit` came from. Never inferred by a reader — a stop computed
    #: from one number while an operator reads another is its own defect.
    entry_credit_basis: CreditBasis = CreditBasis.LIMIT
    #: The filled opening order's limit, always. Equal to `entry_credit` on the
    #: LIMIT bases; the number the realized one improved on otherwise.
    entry_limit: Decimal = Decimal(0)
    #: What the FILLS say, whether or not it was used. `None` when the broker priced
    #: nothing. Carried even when it was REJECTED — a page saying "the fills are
    #: incoherent" without the number is a page nobody can act on, and on the
    #: REALIZED path it makes the block readable without knowing the branch rules.
    fills_credit: Decimal | None = None
    #: Contracts `fills_credit` was computed from, summed over the SHORT leg's
    #: opening fills. 0 when there is no realized average at all. "Realized" from
    #: one of two contracts is a different claim from "realized" from both, and the
    #: page should say which.
    priced_contracts: int = 0

    @property
    def contracts(self) -> int:
        """Paired spread contracts — the size that can be closed as a vertical."""
        return min(self.short_quantity, self.long_quantity)

    @property
    def imbalanced(self) -> bool:
        """True if the legs are not paired, i.e. part of the position is naked.

        Should be impossible — a vertical is submitted and closed as ONE multi-leg
        order — but a partial cancel at the exchange can produce it, and the
        watchdog must say so loudly rather than quietly watch the paired part.
        """
        return self.short_quantity != self.long_quantity

    def credit_payload(self) -> dict[str, Any]:
        """The stop's basis and its provenance, as one inseparable block.

        **Every surface that names `entry_credit` splats this**, and an AST sweep
        (`test_open_spreads.py`) asserts no module outside this one writes the key
        by hand. `entry_credit` is read into SIX hand-built alert and record dicts
        in `watchdog.py`, and a hand-built dict is exactly what let an exit page
        lose the broker's `reject_reason` a week ago (2026-09-02). A number whose
        meaning changed on 2026-09-02 travelling without the field that says which
        meaning it has is the same defect one field over.
        """
        return {
            "entry_credit": float(self.entry_credit),
            "entry_credit_basis": str(self.entry_credit_basis),
            "entry_limit": float(self.entry_limit),
            "fills_credit": None if self.fills_credit is None else float(self.fills_credit),
            "priced_contracts": self.priced_contracts,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "external_identifier": self.external_identifier,
            "broker_order_id": self.broker_order_id,
            "candidate_id": self.candidate_id,
            "underlying": self.underlying,
            "short_symbol": self.short_symbol,
            "long_symbol": self.long_symbol,
            "short_quantity": self.short_quantity,
            "long_quantity": self.long_quantity,
            "contracts": self.contracts,
            **self.credit_payload(),
            "imbalanced": self.imbalanced,
        }


@dataclass(frozen=True)
class OpenSpreadScan:
    """What the ledger says is open, plus everything it could NOT explain.

    The second half is the point. A watchdog that silently returns the spreads it
    understood would leave an open position it could not parse completely
    unmonitored — the exact gap this chunk exists to close — so anything held but
    unexplained rides back in `unattributed`/`malformed` for the caller to record
    Tier 1 and alert on.
    """

    spreads: tuple[OpenSpread, ...] = ()
    #: `positions` symbols no filled opening order accounts for. Distinct from a
    #: reconciliation mismatch (broker vs. Argus); this is Argus's own two records
    #: disagreeing with each other.
    unattributed: tuple[str, ...] = ()
    #: Ledger orders that hold a position but are not a well-formed 2-leg vertical
    #: with a credit limit. Each entry says what was wrong.
    malformed: tuple[dict[str, Any], ...] = ()

    @property
    def fully_explained(self) -> bool:
        return not self.unattributed and not self.malformed


@runtime_checkable
class OpenSpreadSource(Protocol):
    """Where the watchdog gets its watch list. One method, so it fakes trivially.

    `today` is keyword-only and REQUIRED, following `reconcile_orders`' `unmappable`
    for the same reason: it decides which legs count as still open, and the
    dangerous direction is silent. A date read from the wrong clock — or defaulted
    while the caller had a different one — drops a LIVE spread off the watch list
    with no stop being evaluated. Every caller states which trading date it is
    reading the ledger as of.
    """

    async def open_spreads(self, *, today: date) -> OpenSpreadScan: ...


@dataclass(frozen=True)
class LedgerState:
    """What Argus's own order/position record currently holds, and from when.

    Read at reconciliation time (`argus.reconcile`) to answer one question: is this
    ledger content from a PREVIOUS trading day? The sandbox account is wiped every
    24 hours, so a ledger describing yesterday's positions against an empty broker
    is expected rather than a discrepancy — while the same shape from TODAY is a
    genuine mismatch that must halt. See `reconcile.sandbox_reset_decision`.

    `newest_trading_date` is the ET trading date of the most recent thing written,
    across BOTH tables. ET because that is what a trading day means everywhere else
    in Argus (`clock.py`); the columns themselves are ISO-8601 UTC.
    """

    orders: int
    positions: int
    newest_trading_date: date | None

    @property
    def empty(self) -> bool:
        return self.orders == 0 and self.positions == 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "orders": self.orders,
            "positions": self.positions,
            "newest_trading_date": (
                self.newest_trading_date.isoformat()
                if self.newest_trading_date is not None
                else None
            ),
        }


@runtime_checkable
class OrderStore(Protocol):
    """What the rest of the arc depends on. Implemented by the SQLite store and
    by `InMemoryOrderStore`, so callers are testable without a database."""

    async def record_order(self, order: Order) -> None:
        """Note that this broker order is Argus's, without booking any fill.

        Called on a submission ack (and on a replace, for the new id). What makes
        an order 'accounted for' at restart-time reconciliation.
        """

    async def apply(self, order: Order) -> tuple[AppliedFill, ...]:
        """Book whatever `order` reports as newly filled. Idempotent."""

    async def known_orders(self, broker_order_ids: Iterable[int]) -> set[int]:
        """The subset of `broker_order_ids` Argus has a record of."""

    async def ledger_state(self) -> LedgerState:
        """What the ledger holds and the ET trading date it was last written on."""

    async def clear_reset_holdings(self) -> LedgerState:
        """DELETE the `positions` the sandbox reset wiped. Returns prior state.

        Only ever called for the sandbox 24h account reset (see
        `reconcile.sandbox_reset_decision`), and only when the trading day has
        rolled over. Never on a live account — a live account is not wiped
        nightly, so "broker empty, ledger non-empty" there is always real.

        **Positions only; the order record is deliberately kept.** Until
        2026-08-17 this cleared `orders`/`order_legs` too, on the assumption that
        the reset wipes the whole account. It does not: cert wipes positions and
        balances, but `get_live_orders()` keeps reporting orders for a rolling 24
        HOURS, which is a different window from the trading-DAY rollover this
        reset keys on. Deleting the order rows inside that overlap left
        `known_orders()` empty against orders the broker still reported, and
        reconciliation halted on Argus's own fills as `unexpected_filled_order`.
        → docs/decisions/2026-08-17-sandbox-reset-order-ledger.md
        """

    async def remove_positions(self, expected: Mapping[str, int]) -> dict[str, int]:
        """Delete named `positions` rows that still hold exactly `expected`.

        The scoped counterpart to `clear_reset_holdings`, for expired-position resolution
        (`reconcile.expiry_verdicts`): those rows are explained individually, by
        their own symbol's expiry, so the removal is per row and the order ledger
        is left completely alone.

        Returns symbol -> the signed quantity actually removed, which is a SUBSET
        of `expected` whenever a row has moved or vanished since the caller looked.
        That is the point of passing the quantity at all — a row that no longer
        matches is not the row the caller reasoned about, and reconciliation reads
        the shortfall as "still a discrepancy" and halts.
        """


def signed_leg_delta(action: LegAction, contracts: int) -> int:
    """Contracts as a SIGNED position change: buying is long, selling is short.

    Open/close does not enter into it — `BUY_TO_CLOSE` on a short leg adds a
    positive quantity that takes the position back toward flat, which is the same
    arithmetic as `BUY_TO_OPEN`. Keeping it that way means closing a spread needs
    no special case; the position simply nets to zero and the row goes away.

    `action.sells` rather than a member list repeated here: post-close scoring
    reads the same property for the CASH sign, and two lists that must agree about
    which actions are sells is exactly the drift this codebase keeps paying for.
    """
    return -contracts if action.sells else contracts


class SqliteOrderStore:
    """The `orders`/`order_legs` ledger and the `positions` writes it drives."""

    def __init__(self, run: RunFn, *, clock: ClockFn | None = None) -> None:
        self._run = run
        self._clock = clock or _utc_now_iso

    async def record_order(self, order: Order) -> None:
        if order.broker_order_id is None:
            # A dry run mints no id (measured against sandbox, chunk 1), and there
            # is nothing at the broker to account for. Recording it would invent an
            # order that does not exist.
            return
        now = self._clock()
        await self._run(lambda conn: _record_order(conn, order, now))

    async def apply(self, order: Order) -> tuple[AppliedFill, ...]:
        if order.broker_order_id is None:
            return ()
        now = self._clock()
        return await self._run(lambda conn: _apply(conn, order, now))

    async def known_orders(self, broker_order_ids: Iterable[int]) -> set[int]:
        ids = [int(i) for i in broker_order_ids]
        if not ids:
            return set()
        return await self._run(lambda conn: _known_orders(conn, ids))

    async def ledger_state(self) -> LedgerState:
        return await self._run(_ledger_state)

    async def clear_reset_holdings(self) -> LedgerState:
        """Wipe `positions` in ONE transaction, keeping `orders`/`order_legs`.

        The retained direction is the safe one. `positions` without `orders` is a
        held position no order accounts for — the watchdog reports it unwatchable
        and reconciliation cannot explain it. The reverse, an order row whose
        position is gone, is simply what every closed position looks like, and
        `_pair_spreads` gates each reconstructed spread on a non-zero `positions`
        row, so a retained order reconstructs nothing.

        The `run` seam gives the transaction for free (`AuditLog.write` wraps `fn`),
        which is also why this cannot use a private connection.
        """
        return await self._run(_clear_reset_holdings)

    async def remove_positions(self, expected: Mapping[str, int]) -> dict[str, int]:
        """Scoped position delete (`OrderStore.remove_positions`). One transaction.

        Goes through the same `run` seam as every other write here, which is what
        keeps this module the ONLY writer of `positions` — expired-position
        resolution lives in `reconcile.py` but its delete does not.
        """
        if not expected:
            return {}
        rows = dict(expected)
        return await self._run(lambda conn: _remove_positions(conn, rows))

    async def open_spreads(self, *, today: date) -> OpenSpreadScan:
        """Reassemble currently-held spreads from the ledger (`OpenSpreadSource`).

        Read-only, and it runs through the same `run` seam as every write here — so
        in production it takes the audit log's chain lock. That lock's contract
        (`AuditLog.read`) is that callers keep queries BOUNDED and RARE, which is
        exactly why the watchdog calls this on its slow `refresh_interval_s` rather
        than on its evaluation cadence: the triggers themselves need no database.
        """
        return await self._run(lambda conn: _open_spreads(conn, today))


# -- the SQL (plain functions, so they run inside the injected seam) -----------


def _upsert_order(conn: sqlite3.Connection, order: Order, now: str) -> None:
    """Insert or refresh the order row. `recorded_at` survives an update."""
    conn.execute(
        """
        INSERT INTO orders (
            broker_order_id, external_identifier, candidate_id, direction,
            underlying, status, limit_price, recorded_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(broker_order_id) DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at,
            -- Only ever fill these IN; a later event that has lost Argus's own
            -- linkage (candidate_id never round-trips through the broker) must
            -- not erase what the submission recorded.
            candidate_id = COALESCE(orders.candidate_id, excluded.candidate_id),
            external_identifier = CASE
                WHEN excluded.external_identifier = '' THEN orders.external_identifier
                ELSE excluded.external_identifier END
        """,
        (
            order.broker_order_id,
            order.external_identifier,
            order.candidate_id,
            str(order.direction),
            order.underlying,
            str(order.status) if order.status is not None else None,
            float(order.limit_price),
            now,
            now,
        ),
    )
    for leg in order.legs:
        conn.execute(
            """
            INSERT INTO order_legs (broker_order_id, symbol, action, quantity)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(broker_order_id, symbol) DO UPDATE SET
                quantity = excluded.quantity
            """,
            (order.broker_order_id, leg.symbol, str(leg.action), leg.quantity),
        )


def _record_order(conn: sqlite3.Connection, order: Order, now: str) -> None:
    conn.execute("BEGIN")
    try:
        _upsert_order(conn, order, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _apply(conn: sqlite3.Connection, order: Order, now: str) -> tuple[AppliedFill, ...]:
    """Book the newly-filled contracts on `order`. One transaction, or nothing.

    The ledger update and the position update are the same transaction on purpose:
    a crash between them would leave `positions` disagreeing with the record of
    what produced it, and the next reconcile would refuse the day (or halt the
    process) over bookkeeping rather than a real broker discrepancy.
    """
    assert order.broker_order_id is not None
    conn.execute("BEGIN")
    try:
        _upsert_order(conn, order, now)
        applied = [
            fill
            for leg in order.legs
            if (fill := _apply_leg(conn, order, leg, now)) is not None
        ]
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return tuple(applied)


def _apply_leg(
    conn: sqlite3.Connection, order: Order, leg: OrderLeg, now: str
) -> AppliedFill | None:
    """Book one leg's delta, or None if this event adds nothing."""
    filled = leg.filled_quantity
    if filled is None:
        # The broker has not said. Not evidence of zero — just no news.
        return None
    row = conn.execute(
        "SELECT applied_quantity FROM order_legs WHERE broker_order_id = ? AND symbol = ?",
        (order.broker_order_id, leg.symbol),
    ).fetchone()
    already = int(row[0]) if row is not None else 0
    if filled <= already:
        # Either a repeat of an event already booked, or one that arrived out of
        # order reporting LESS than we have applied. Both are no-ops: fills do not
        # un-happen, and letting a stale event reduce the count would silently
        # unwind a position that really exists.
        return None
    contracts = filled - already
    delta = signed_leg_delta(leg.action, contracts)
    price = leg.fill_price(already, filled)
    if price is None:
        # The broker booked contracts without pricing them. `LegFill` is documented
        # as best-effort detail — it prices a fill, it does not count one — so this
        # is a known state, and the priced columns simply do not move. They are the
        # PRICED SUBSET of `applied_quantity`, never a parallel count of it, and a
        # zero blended in here would be the halved lie `_apply_position` refuses.
        conn.execute(
            "UPDATE order_legs SET applied_quantity = ? "
            "WHERE broker_order_id = ? AND symbol = ?",
            (filled, order.broker_order_id, leg.symbol),
        )
    else:
        # Same transaction, same statement, same idempotency: this is behind the
        # `filled <= already` return above, so a re-delivered or replayed event
        # contributes nothing here for the same reason it contributes nothing to
        # `applied_quantity`. (0005)
        #
        # The pair written is EXACTLY the pair this call already emits on the
        # `AppliedFill` below — `contracts` and `price` — so these columns are the
        # durable running total of the `order.fill` records, and the ledger's
        # realized credit and `shadow.outcome`'s reconstruction of it are the same
        # arithmetic over the same numbers rather than two ways of scoring one day.
        # That includes `fill_price`'s extrapolation where the broker priced only
        # part of a slice: both paths carry the covered average against the whole
        # applied count, which is what makes them agree.
        conn.execute(
            "UPDATE order_legs SET applied_quantity = ?, "
            "    priced_quantity = priced_quantity + ?, "
            "    priced_notional = priced_notional + ? "
            "WHERE broker_order_id = ? AND symbol = ?",
            (filled, contracts, price * contracts, order.broker_order_id, leg.symbol),
        )
    before, after = _apply_position(conn, order, leg, delta, contracts, price, now)
    return AppliedFill(
        broker_order_id=order.broker_order_id,
        external_identifier=order.external_identifier,
        symbol=leg.symbol,
        action=leg.action,
        contracts=contracts,
        signed_delta=delta,
        price=price,
        position_before=before,
        position_after=after,
    )


def _apply_position(
    conn: sqlite3.Connection,
    order: Order,
    leg: OrderLeg,
    delta: int,
    contracts: int,
    price: float | None,
    now: str,
) -> tuple[int, int]:
    """Move `positions` by `delta` and maintain the cost basis. Returns (before, after).

    `average_open_price` is maintained the ordinary way — reset when a position is
    opened from flat, quantity-weighted when it is added to, untouched when it is
    reduced — with one concession to reality: when the broker reports no per-fill
    price for the contracts being booked, the existing basis is carried rather than
    a zero being blended in. A slightly stale basis is a better lie than a
    halved one, and NOTHING safety-critical reads this column: reconciliation
    (spec §6) compares `symbol` and `signed_quantity` only. A brand-new position
    with no reported price lands at 0.0, which reads as "not reported".

    A position that nets to flat is DELETED rather than left at zero, which keeps
    the table equal to what `SqlitePositionStore.open_positions()` already
    filters for and stops closed 0DTE spreads accumulating forever.

    NB this is the only path that removes a row on a CLOSING TRADE, and a position
    held to expiry never takes it — expiry generates no fill event at all. Those
    rows are resolved at the next reconcile instead (`reconcile.expiry_verdicts`,
    `remove_positions`).
    """
    row = conn.execute(
        "SELECT signed_quantity, average_open_price FROM positions WHERE symbol = ?",
        (leg.symbol,),
    ).fetchone()
    before = int(row[0]) if row is not None else 0
    basis = float(row[1]) if row is not None else 0.0
    after = before + delta

    if after == 0:
        conn.execute("DELETE FROM positions WHERE symbol = ?", (leg.symbol,))
        return before, after

    if before == 0 or (before > 0) != (after > 0):
        new_basis = price if price is not None else 0.0
    elif abs(after) > abs(before):
        add_price = price if price is not None else basis
        new_basis = (basis * abs(before) + add_price * contracts) / abs(after)
    else:
        new_basis = basis

    conn.execute(
        """
        INSERT INTO positions (symbol, underlying, signed_quantity, average_open_price, opened_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            signed_quantity = excluded.signed_quantity,
            average_open_price = excluded.average_open_price
        """,
        (leg.symbol, order.underlying, after, new_basis, now),
    )
    return before, after


def _ledger_state(conn: sqlite3.Connection) -> LedgerState:
    """Counts plus the newest write instant, as an ET trading date.

    Bounded by construction: two `COUNT(*)` and two `MAX()` over tables that hold a
    handful of rows per day. Both timestamp columns are ISO-8601 UTC written by
    `_utc_now_iso`, and that format sorts lexicographically, so `MAX()` on the text
    is the true maximum — no parsing inside SQLite, no locale, no ambiguity.
    """
    orders = int(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
    positions = int(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0])
    stamps = [
        conn.execute("SELECT MAX(updated_at) FROM orders").fetchone()[0],
        conn.execute("SELECT MAX(opened_at) FROM positions").fetchone()[0],
    ]
    newest = max((s for s in stamps if s), default=None)
    return LedgerState(
        orders=orders, positions=positions, newest_trading_date=_trading_date_of(newest)
    )


def _trading_date_of(utc_iso: str | None) -> date | None:
    """ET trading date for an ISO-8601 UTC stamp. `None` for absent or unparseable.

    Unparseable returns None rather than raising: this feeds a SAFETY decision
    (may the ledger be cleared?), and `None` is read there as "cannot say the day
    rolled over", which refuses the clear. A raise would instead take down
    reconciliation itself. Fail closed, and stay up to say so.
    """
    if not utc_iso:
        return None
    try:
        # `%Y-%m-%dT%H:%M:%S.%fZ` — `fromisoformat` handles the trailing Z from 3.11.
        parsed = datetime.fromisoformat(utc_iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ET).date()


def _clear_reset_holdings(conn: sqlite3.Connection) -> LedgerState:
    """Delete what the reset wiped — `positions` and `order_legs` — keeping `orders`.

    The state is read BEFORE the deletes so the returned counts describe what was
    there — that is what lands in the Tier 1 audit record. It reports BOTH counts,
    orders included, because the record's job is to say what the ledger looked like
    when the reset was assumed, not only what was removed.

    **`orders` survives; the HOLDINGS do not.** The v0.3.2 requirement is precisely
    and only that `known_orders()` still recognises an order the broker reports
    inside its 24h lookback, and that query reads `orders` alone — no join to
    `order_legs`. So keeping the identity record satisfies it, while clearing the
    applied-leg record keeps the ledger internally consistent with a wiped account.

    `order_legs` has to go for the same reason `positions` does (added 2026-08-17):
    spread reassembly is now per trade idea and derives holdings from
    `applied_quantity`, so legs left behind after a wipe would rebuild yesterday's
    spreads as phantoms for the watchdog to chase.
    """
    before = _ledger_state(conn)
    conn.execute("DELETE FROM order_legs")
    conn.execute("DELETE FROM positions")
    return before


def _remove_positions(conn: sqlite3.Connection, expected: dict[str, int]) -> dict[str, int]:
    """Delete each named row IF it still holds the quantity the caller saw.

    The `signed_quantity = ?` clause is the whole safety of this: the caller
    reasoned about a row it read through a different connection, and a row that has
    moved since is not that row. `changes()` reports whether the delete actually
    matched, so the return value is what HAPPENED rather than what was asked for.

    One transaction, like every other write here — a half-applied resolution would
    leave one leg of a vertical in the ledger and the other gone, which
    `_open_spreads` reports as a malformed order for as long as it persists.
    """
    removed: dict[str, int] = {}
    conn.execute("BEGIN")
    try:
        for symbol, quantity in expected.items():
            cur = conn.execute(
                "DELETE FROM positions WHERE symbol = ? AND signed_quantity = ?",
                (symbol, int(quantity)),
            )
            if cur.rowcount:
                removed[symbol] = int(quantity)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return removed


def _leg_is_expired(symbol: str, today: date) -> bool:
    """Is this leg's OWN OCC expiry strictly before the ledger read's `today`?

    The SAME predicate `reconcile.expiry_verdicts` uses for a `positions` row, on
    the same source of truth (the OCC symbol) and in the same direction, because
    the two are answering the same question about the same instrument and must
    never disagree about it.

    **Strictly before, and it does NOT take the same-day settlement carve-out.**
    Reconciliation runs once, at a known moment, and can afford to ask what time
    it is; this runs on the watchdog's refresh cadence all session long, and the
    cost of being wrong here is a HELD 0DTE SPREAD SILENTLY LEAVING THE WATCH
    LIST — no stop evaluated, all afternoon. Nothing is gained by resolving it 15
    minutes after the close either: the session is over, and the next reconcile
    (which does apply the carve-out) will have cleared `positions` before the
    watchdog ever scans again.

    `None` — an unreadable symbol — is NOT expired, so it stays outstanding and
    reaches the divergence check exactly as before. Fail-closed, identically to
    the reconciliation rule: a leg Argus cannot identify is not a leg it may drop.
    """
    expiry = occ_expiry(symbol)
    return expiry is not None and expiry < today


def _open_spreads(conn: sqlite3.Connection, today: date) -> OpenSpreadScan:
    """Reassemble spreads from the ORDER ledger, per trade idea.

    BOTH directions are selected, unlike before 2026-08-17: a spread's remaining
    size is its opening order's applied contracts NET of its closing order's, and
    that subtraction is what replaces the old lookup into `positions`.

    Why it cannot come from `positions`: that table is keyed by SYMBOL and holds
    only the NET, so two concurrent verticals sharing a strike erase each other's
    record of it. `signal.width = 5.0` against SPX's 5-point ladder makes exactly
    that the COMMON case — 7755/7750 and 7750/7745 chain — and on 2026-08-17 it
    left two live spreads unwatchable. Per-idea attribution has no such collision:
    each candidate's legs are summed only against its own orders.

    `positions` is still read, for `unattributed` only — a held symbol no filled
    order explains is Argus's two records disagreeing, and remains worth saying.

    `today` is the ET trading date of the read, threaded from the watchdog's own
    `trading_date` seam. It exists because this arithmetic had NO notion of expiry
    (2026-08-25): a 0DTE spread held to expiry produces no closing fill, so its
    legs stayed "outstanding" forever while the `positions` rows were resolved
    away at the next reconcile — a permanent `attributed != held` divergence, and
    therefore a permanent false `watchdog.unwatchable` page every 15 seconds.
    → docs/decisions/2026-08-12-expired-position-resolution.md (2026-08-25 addendum)
    """
    rows = list(
        conn.execute(
            """
            SELECT o.broker_order_id AS oid, o.external_identifier, o.candidate_id,
                   o.underlying, o.limit_price, o.direction,
                   l.symbol, l.action, l.applied_quantity,
                   l.priced_quantity, l.priced_notional
            FROM orders o
            JOIN order_legs l ON l.broker_order_id = o.broker_order_id
            WHERE l.applied_quantity > 0
            ORDER BY o.broker_order_id
            """
        )
    )
    held = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT symbol, signed_quantity FROM positions WHERE signed_quantity != 0"
        )
    }
    return _pair_spreads(rows, held, today)


def _spread_key(row: Any) -> str | None:
    """The TRADE IDEA an order belongs to — what groups an open with its close.

    `candidate_id` when the ledger has it. It is nullable by design: it never
    round-trips through the broker, so an order row first seen from a `PlacedOrder`
    alert can lack it. `external_identifier` is NOT NULL and deterministically
    `{prefix}{direction}-{candidate}` (`build.external_identifier`), so the same
    key is recoverable from it.

    `None` means the key cannot be established, and the caller reports the order
    rather than guessing — grouping unrelated orders under one key would merge two
    positions into one nonsense spread.
    """
    candidate = row["candidate_id"]
    if candidate:
        return str(candidate)
    external = str(row["external_identifier"])
    prefix = f"{ORDER_ID_PREFIX}{row['direction']}-"
    if external.startswith(prefix) and len(external) > len(prefix):
        return external[len(prefix) :]
    return None


#: The credit is normalized to this before it prices anything. An average over
#: rungs is a genuine ratio — 2 contracts at 0.50 and 1 at 0.44 is 0.48 — so the
#: float division carries representation noise that has no business reaching a
#: threshold. Six decimals is the quantity `zerobid._INPUT_PRECISION` and
#: `tools.mark._NOISE_FLOOR` already settled on, for the same reason: far finer
#: than any real price increment (a tick is 0.05, a mid moves on 0.025) and far
#: coarser than the noise. It is deliberately NOT a tick — the realized credit is
#: an average and is not required to land on the grid its inputs do.
_CREDIT_QUANTUM = Decimal("0.000001")


def _realized_credit(
    open_rows: Sequence[Any], *, short_symbol: str, long_symbol: str
) -> tuple[Decimal | None, int]:
    """What the opening fills actually collected, per spread contract. (2026-09-02)

    `None` when it cannot be said — either leg unpriced, or a leg both bought and
    sold — and the caller falls back to the LIMIT, which is the behaviour every
    position had before migration 0005.

    **Aggregated across EVERY opening order in the trade idea, weighted by
    contracts.** That is the whole correction. A walk that reprices fills across
    several `broker_order_id`s under one `external_identifier` — proven 2026-09-01,
    where 1429812 filled 1 contract at 2.05/1.55 and 1429813 filled 1 at
    2.15/1.65 — so the newest rung's price is one of several and reading it alone
    is right only by luck. Both rungs happened to net 0.50 that day; rungs at 0.55
    and 0.45 give 0.50 here and 0.45 from the newest.

    The one case this makes WORSE, stated: an idea closed and re-entered the same
    day would blend the closed entry's credit into the live one, where newest-only
    got it right. `_attempted` claims a candidate before `DuplicateOrderGuard` even
    runs, so a re-proposed strike pair never reaches submission, and a re-entry
    would mint the same deterministic `external_identifier` in any case. It has
    never been observed. The walk-rung case is the normal result of any walk that
    reprices, and it was wrong.

    The cash sign is `LegAction.sells` — the SINGLE definition, the one
    `signed_leg_delta` reads for the position sign and `shadow.marker` reads for
    the fill sign. It is the half of a leg action the broker does NOT relabel: an
    opening BUY that comes back `Buy to Close` on a chained book is still a buy
    (2026-08-25), so nothing here can be fooled by the relabelling, and nothing
    here reads `opens`/`closes` or a direction.

    Returns `(credit, priced_contracts)`, the second being the SHORT leg's priced
    contracts — how much of the position the average actually covers.
    """
    totals: dict[str, list[Any]] = {}      # symbol -> [quantity, notional, {sells}]
    for row in open_rows:
        symbol = str(row["symbol"])
        if symbol not in (short_symbol, long_symbol):
            continue
        quantity = int(row["priced_quantity"] or 0)
        if quantity <= 0:
            continue
        notional = float(row["priced_notional"] or 0.0)
        if not math.isfinite(notional):
            return None, 0
        entry = totals.setdefault(symbol, [0, 0.0, set()])
        entry[0] += quantity
        entry[1] += notional
        entry[2].add(LegAction(row["action"]).sells)

    net = Decimal(0)
    for symbol in (short_symbol, long_symbol):
        entry = totals.get(symbol)
        if entry is None:
            return None, 0
        quantity, notional, signs = entry
        if len(signs) != 1:
            # One symbol both bought and sold across this idea's opening orders.
            # Whatever that is, it is not one leg of a vertical, and netting it
            # would quietly cancel two real movements against each other.
            return None, 0
        average = Decimal(str(notional / quantity))
        net += average if next(iter(signs)) else -average

    return net.quantize(_CREDIT_QUANTUM, rounding=ROUND_HALF_UP), int(
        totals[short_symbol][0]
    )


def _pair_spreads(
    rows: Sequence[Any], held: dict[str, int], today: date
) -> OpenSpreadScan:
    """Pure pairing logic, so every branch is testable without a database.

    Grouped by TRADE IDEA (`_spread_key`), never by symbol. Each group's legs are
    summed — opening orders positive to the position, closing orders negative —
    and what is left over is what is still held for that idea. Two ideas sharing a
    strike therefore cannot cancel each other out, which is the whole reason this
    stopped reading `positions`.

    Re-entry of the same idea after a close still collapses correctly: opens and
    closes sum inside one group, so 2 opened + 2 closed + 2 opened is one open
    spread of 2, priced from the NEWEST opening order.

    **Expiry is the third way a leg stops being outstanding** (2026-08-25),
    alongside a closing fill and never having filled at all. It has to be, because
    the other two are both ORDERS and 0DTE expiry produces no order: a spread held
    to expiry is closed by the exchange, and until this predicate existed the
    ledger insisted its legs were still open forever. See `_leg_is_expired` for
    why the boundary is strictly-before-today with no settlement carve-out.
    """
    groups: dict[str, list[Any]] = {}
    spreads: list[OpenSpread] = []
    malformed: list[dict[str, Any]] = []
    claimed: set[str] = set()

    for row in rows:
        key = _spread_key(row)
        if key is None:
            malformed.append(
                {
                    "broker_order_id": int(row["oid"]),
                    "external_identifier": str(row["external_identifier"]),
                    "candidate_id": row["candidate_id"],
                    "problem": "cannot determine which trade idea this order belongs to",
                }
            )
            continue
        groups.setdefault(key, []).append(row)

    for key in sorted(groups):
        legs = groups[key]
        # The newest OPENING order prices the stop and identifies the spread: on a
        # re-entry the older one describes a position that is already closed.
        opens = [r for r in legs if str(r["direction"]) == str(OrderDirection.OPEN)]
        if not opens:
            # Closing fills with no opening order in the ledger. Nothing to watch,
            # but it is Argus's records disagreeing with themselves, so it is said.
            malformed.append(
                {
                    "candidate_id": key,
                    "problem": "closing fills with no opening order on record",
                }
            )
            continue
        head = max(opens, key=lambda r: int(r["oid"]))
        detail = {
            "broker_order_id": int(head["oid"]),
            "external_identifier": str(head["external_identifier"]),
            "candidate_id": head["candidate_id"],
        }

        net: dict[str, int] = {}
        for row in legs:
            delta = signed_leg_delta(LegAction(row["action"]), int(row["applied_quantity"]))
            net[str(row["symbol"])] = net.get(str(row["symbol"]), 0) + delta
        # An EXPIRED leg is not outstanding, however the arithmetic came out. The
        # filter is applied to the net rather than to the rows so it narrows only
        # this one thing: the legs still net exactly as before, and a group is
        # dropped only when every leg it has left has expired.
        outstanding = {
            symbol: qty
            for symbol, qty in net.items()
            if qty != 0 and not _leg_is_expired(symbol, today)
        }

        if not outstanding:
            # Fully closed — by a closing fill, by expiry, or by both. Nothing to
            # watch, and the symbols are deliberately NOT claimed: if `positions`
            # still carries a row for one of them, "a held position no open spread
            # explains" is precisely what `unattributed` is for, and claiming it
            # here would drop it silently (no spread would carry it into the
            # divergence check). For an expired leg that row is itself stale and
            # the next reconcile resolves it; saying so meanwhile is correct.
            continue

        shorts = [s for s, q in outstanding.items() if q < 0]
        longs = [s for s, q in outstanding.items() if q > 0]
        claimed.update(outstanding)
        if len(shorts) != 1 or len(longs) != 1:
            # Argus only ever routes two-leg defined-risk verticals (spec §3), so a
            # partially-closed or otherwise lopsided remainder is not something to
            # guess the risk of.
            malformed.append(
                {
                    **detail,
                    "problem": "outstanding legs are not one short and one long",
                    "outstanding": dict(sorted(outstanding.items())),
                }
            )
            continue

        short_symbol, long_symbol = shorts[0], longs[0]
        entry_limit = Decimal(str(head["limit_price"]))
        if entry_limit <= 0:
            # An opening order at a debit is not a credit spread. `build_order`
            # cannot produce one (`_limit_price` signs OPEN positive), so this is a
            # record that cannot be trusted to price a stop. UNCONDITIONAL, and
            # asked before the fills are: what the legs filled at says nothing
            # useful about an order whose own limit is corrupt.
            malformed.append(
                {**detail, "problem": f"opening order limit {entry_limit} is not a credit"}
            )
            continue

        realized, priced_contracts = _realized_credit(
            opens, short_symbol=short_symbol, long_symbol=long_symbol
        )
        if realized is None:
            entry_credit, basis = entry_limit, CreditBasis.LIMIT
        elif realized <= 0:
            # The fills are priced and they say a credit spread was opened for a
            # debit. Watched anyway, on the limit: the symbols, quantities, actions
            # and limit are all coherent and the spread is perfectly watchable on
            # the basis it has had all along — while `malformed` would cost it its
            # credit trigger, its underlying trigger, its FLATTEN_BEFORE_CLOSE, its
            # row on `/status` and its concurrency slot, over a price anomaly. The
            # limit basis is LOW, so the stop is TIGHT: the fail-closed direction.
            # The watchdog pages on this basis; it must never be a quiet fallback.
            entry_credit, basis = entry_limit, CreditBasis.LIMIT_FILLS_INCOHERENT
        else:
            entry_credit, basis = realized, CreditBasis.REALIZED

        spreads.append(
            OpenSpread(
                external_identifier=str(head["external_identifier"]),
                broker_order_id=int(head["oid"]),
                candidate_id=head["candidate_id"],
                underlying=str(head["underlying"]),
                short_symbol=short_symbol,
                long_symbol=long_symbol,
                short_quantity=abs(outstanding[short_symbol]),
                long_quantity=abs(outstanding[long_symbol]),
                entry_credit=entry_credit,
                entry_credit_basis=basis,
                entry_limit=entry_limit,
                fills_credit=realized,
                priced_contracts=priced_contracts,
            )
        )

    # THE INVARIANT that makes per-idea attribution safe to trust.
    #
    # Attribution decides WHICH idea holds what; it must never invent holdings. So
    # the sum of every spread's outstanding legs has to equal `positions` symbol by
    # symbol — the one table written from the same transaction as the fills. Where
    # it does not, the ledger is claiming a position that is not there (or missing
    # one that is), and the spreads touching that symbol are reported instead of
    # watched.
    #
    # This is what stops the change from reintroducing the hazard v0.3.2 was
    # careful about: an order ledger retained across a sandbox reset describes
    # spreads whose `positions` rows are gone, and without this they would be
    # watched as phantoms. Note the shared-strike case passes cleanly — 7755/7750
    # and 7750/7745 sum to zero on 7750, which is exactly what `positions` holds.
    #
    # Expiry (above) narrows what "outstanding" means; it does NOT relax this. An
    # UNEXPIRED leg missing from `positions` still diverges and its spread is still
    # reported, which is the whole reason the fix went there rather than here —
    # weakening this check would have excused every cause of disagreement at once,
    # instead of the one cause (a settled 0DTE leg) that is not a disagreement.
    attributed: dict[str, int] = {}
    for spread in spreads:
        attributed[spread.short_symbol] = (
            attributed.get(spread.short_symbol, 0) - spread.short_quantity
        )
        attributed[spread.long_symbol] = (
            attributed.get(spread.long_symbol, 0) + spread.long_quantity
        )
    divergent = {
        symbol
        for symbol in set(attributed) | set(held)
        if attributed.get(symbol, 0) != held.get(symbol, 0)
    }
    if divergent:
        kept: list[OpenSpread] = []
        for spread in spreads:
            touched = divergent & {spread.short_symbol, spread.long_symbol}
            if not touched:
                kept.append(spread)
                continue
            malformed.append(
                {
                    "broker_order_id": spread.broker_order_id,
                    "external_identifier": spread.external_identifier,
                    "candidate_id": spread.candidate_id,
                    "problem": "ledger holdings disagree with the positions table",
                    "divergent": {
                        symbol: {
                            "attributed": attributed.get(symbol, 0),
                            "positions": held.get(symbol, 0),
                        }
                        for symbol in sorted(touched)
                    },
                }
            )
        spreads = kept

    unattributed = tuple(sorted(symbol for symbol in held if symbol not in claimed))
    return OpenSpreadScan(
        spreads=tuple(spreads),
        unattributed=unattributed,
        malformed=tuple(malformed),
    )


def _known_orders(conn: sqlite3.Connection, ids: Sequence[int]) -> set[int]:
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT broker_order_id FROM orders WHERE broker_order_id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    return {int(row[0]) for row in rows}


# -- in-memory equivalent (tests, and the seed for the planned simulator) ------


class InMemoryOrderStore:
    """Same contract, no database. Mirrors the SQLite store's idempotency."""

    def __init__(self, *, trading_date: date | None = None) -> None:
        self.orders: dict[int, Order] = {}
        self.positions: dict[str, int] = {}
        self._applied: dict[tuple[int, str], int] = {}
        #: `order_legs.priced_quantity` / `priced_notional` (migration 0005), which
        #: the fake must accumulate the same way or `_pair_spreads` — literally the
        #: same function for both stores — would see a different world here.
        self._priced: dict[tuple[int, str], tuple[int, float]] = {}
        #: The trading date `ledger_state` reports for whatever is held. A single
        #: settable value rather than a per-row stamp: the SQLite store derives one
        #: ET date from `MAX()` over both tables, so one value is the whole of what
        #: callers can observe, and a test that needs "the ledger is from yesterday"
        #: sets exactly that.
        self.trading_date = trading_date

    async def record_order(self, order: Order) -> None:
        if order.broker_order_id is None:
            return
        self.orders[order.broker_order_id] = order

    async def apply(self, order: Order) -> tuple[AppliedFill, ...]:
        if order.broker_order_id is None:
            return ()
        self.orders[order.broker_order_id] = order
        applied: list[AppliedFill] = []
        for leg in order.legs:
            filled = leg.filled_quantity
            if filled is None:
                continue
            key = (order.broker_order_id, leg.symbol)
            already = self._applied.get(key, 0)
            if filled <= already:
                continue
            self._applied[key] = filled
            contracts = filled - already
            delta = signed_leg_delta(leg.action, contracts)
            price = leg.fill_price(already, filled)
            if price is not None:
                quantity, notional = self._priced.get(key, (0, 0.0))
                self._priced[key] = (quantity + contracts, notional + price * contracts)
            before = self.positions.get(leg.symbol, 0)
            after = before + delta
            if after == 0:
                self.positions.pop(leg.symbol, None)
            else:
                self.positions[leg.symbol] = after
            applied.append(
                AppliedFill(
                    broker_order_id=order.broker_order_id,
                    external_identifier=order.external_identifier,
                    symbol=leg.symbol,
                    action=leg.action,
                    contracts=contracts,
                    signed_delta=delta,
                    price=price,
                    position_before=before,
                    position_after=after,
                )
            )
        return tuple(applied)

    async def known_orders(self, broker_order_ids: Iterable[int]) -> set[int]:
        return {int(i) for i in broker_order_ids if int(i) in self.orders}

    async def ledger_state(self) -> LedgerState:
        held = sum(1 for q in self.positions.values() if q != 0)
        return LedgerState(
            orders=len(self.orders),
            positions=held,
            # An empty ledger has no date, matching `MAX()` over empty tables.
            newest_trading_date=(
                self.trading_date if (self.orders or held) else None
            ),
        )

    async def clear_reset_holdings(self) -> LedgerState:
        """Positions only, matching the SQLite store.

        `orders`, `_applied` and `trading_date` all survive because the order rows
        do: `_applied` is the fill-idempotency set that keeps a re-delivered fill
        from recreating a cleared position, and `trading_date` is what
        `ledger_state()` reports for the orders still held.
        """
        before = await self.ledger_state()
        self.positions.clear()
        return before

    async def remove_positions(self, expected: Mapping[str, int]) -> dict[str, int]:
        """Same guarded semantics as the SQLite store: matching rows only."""
        removed: dict[str, int] = {}
        for symbol, quantity in expected.items():
            if self.positions.get(symbol) == int(quantity):
                del self.positions[symbol]
                removed[symbol] = int(quantity)
        return removed

    async def open_spreads(self, *, today: date) -> OpenSpreadScan:
        """Same pairing as the SQLite store — literally the same function.

        The rows are built as plain dicts, which `_pair_spreads` accepts because
        `sqlite3.Row` and `dict` share the `row["column"]` interface. That keeps the
        fake honest: a pairing bug cannot show up in only one of the two.
        """
        rows: list[dict[str, Any]] = []
        for order_id, order in sorted(self.orders.items()):
            # BOTH directions, matching the SQL: a spread's remaining size is its
            # opening fills net of its closing fills, grouped by trade idea.
            for leg in order.legs:
                applied = self._applied.get((order_id, leg.symbol), 0)
                if applied <= 0:
                    continue
                priced_quantity, priced_notional = self._priced.get(
                    (order_id, leg.symbol), (0, 0.0)
                )
                rows.append(
                    {
                        "oid": order_id,
                        "external_identifier": order.external_identifier,
                        "candidate_id": order.candidate_id,
                        "underlying": order.underlying,
                        "limit_price": float(order.limit_price),
                        "direction": str(order.direction),
                        "symbol": leg.symbol,
                        "action": str(leg.action),
                        "applied_quantity": applied,
                        "priced_quantity": priced_quantity,
                        "priced_notional": priced_notional,
                    }
                )
        held = {s: q for s, q in self.positions.items() if q != 0}
        return _pair_spreads(rows, held, today)
