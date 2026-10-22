"""The broker's own record of the same days, and the reconciliation against Argus.

READ-ONLY at the broker: one paginated `GET /accounts/{n}/transactions`
(`Account.get_history`). It sends no order and nothing that mutates, and an AST test
pins that this module cannot reach the submission path.

**The broker is the source of truth, so the broker's numbers are THE numbers.**
Argus's gross and its `order.submitted` fee estimate sit beside them as a check, and
a disagreement is a `DIFF` to chase — never something to average away.

**Matched per `order_id`, to the CENT.** Every `order.fill` Argus writes carries
`broker_order_id`, so the join is exact rather than strike-and-time guesswork, and
three days of confirmations already matched exactly (2026-09-17/18/21). A tolerance
would quietly absorb the very defects this exists to find, so `Decimal` is used end
to end and the comparison is `==`.

**Absence is never a verdict.** A failed fetch marks the day `BROKER_UNREAD` and the
Argus side is still written; it does not blank the broker columns and call them zero.
A broker row with no `order_id` — a cash settlement, or anything Argus did not place
— is reported as `unmatched_broker` and makes the day `DIFF`, which is the right
answer for both: the account is dedicated to Argus, and a spread that reached expiry
means the 15:30 flatten failed.

**`is_estimated_fee` is RECORDED and DECIDED ON BY NOTHING** (2026-09-22, from the
first live read). The design gated `PROVISIONAL` on it, on the assumption that it
marks a fee the broker has not finalised. The live answer: every row of 2026-09-17,
-18 and -21 carries it, including days whose fees match the CLEARING FIRM's own
confirmation to the cent. So it does not mean "not yet final", and gating on it would
leave every day permanently `PROVISIONAL` — a status that can never reach `OK` is a
status nobody reads. It stays as a column and a count.

**The comparison is FILLS ONLY, and that is load-bearing** (2026-09-22). A trade held
to expiry is valued at settlement (`tools.settlement`), and the broker has no row for
an OTM expiry — so a settlement number in `argus_gross` would make every such day
`DIFF` for a difference that is correct. `argus_gross` is `days.gross`, the day's fill
cash, never `days.total_gross`; the settlement value rides beside it in
`settled_gross`, compared with nothing. An ITM expiry DOES post a cash settlement,
with no `order_id`, and stays an `unmatched_broker` `DIFF` until its first real row
is captured as a fixture and paired with the settlement-valued trade — its type
strings have never been seen, and are NOT to be guessed, and neither is its DATE:
nothing yet says that row's `executed_at` falls on the expiry's own trading day
rather than the evening after or the next session, so a pairing must not assume it.

**Comparing cash rather than P&L would lose the check on the P&L — so a second,
Argus-only comparison rides here too.** `trades.pnl_check` (see `ledger.pnl_check`)
is non-blank when a trade's gross does not follow from its own fills; any such trade
makes the day `DIFF` and is named in `pnl_mismatch`. It needs no broker, so it wins
over `BROKER_UNREAD`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence

from argus.clock import ET
from argus.config import Config

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

#: Statuses, in the order they are decided. First match wins.
BROKER_UNREAD = "BROKER_UNREAD"
DIFF = "DIFF"
PROVISIONAL = "PROVISIONAL"
OK = "OK"


# -- rows ---------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerTxnRow:
    trading_date: str
    time_et: str
    id: str
    order_id: str
    type: str
    sub_type: str
    symbol: str
    action: str
    quantity: str
    price: str
    value: str
    commission: str
    clearing_fees: str
    regulatory_fees: str
    index_option_fees: str
    other_charge: str
    fees_total: str
    net_value: str
    is_estimated_fee: str
    description: str


@dataclass(frozen=True)
class ReconRow:
    trading_date: str
    status: str
    argus_gross: str
    broker_gross: str
    gross_diff: str
    argus_fees: str
    broker_fees: str
    fee_diff: str
    broker_net: str
    argus_orders: int
    broker_orders: int
    unmatched_argus: str
    unmatched_broker: str
    estimated_fee_rows: int
    unknown_result_trades: int
    #: The day's settlement-valued result, CARRIED, never compared: see the module
    #: docstring. Appended, so no column the Dashboard addresses moves.
    settled_gross: str = ""
    #: Trades whose P&L does not follow from their own fills (`ledger.pnl_check`),
    #: space-separated. Non-empty is `DIFF`, whether or not the broker was read.
    pnl_mismatch: str = ""


# -- decoding the SDK's Transaction -------------------------------------------


def _dec(value: Any) -> Decimal:
    """A signed money field as `Decimal`, `0.00` when the broker sent nothing.

    The SDK has already applied each field's price effect, so the sign is the
    broker's own: a credit is positive and a charge is negative.
    """
    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _money(value: Decimal | None) -> str:
    return "" if value is None else f"{value:.2f}"


def _et_parts(txn: Any) -> tuple[str, str]:
    """(ET trading date, ET time) for one transaction.

    `executed_at` is UTC and is what puts a fill on a trading DAY; the broker's own
    `transaction_date` is a settlement-side date and is not the same question.
    """
    executed = getattr(txn, "executed_at", None)
    if isinstance(executed, datetime):
        local = executed.astimezone(ET)
        return local.date().isoformat(), local.strftime("%H:%M:%S")
    fallback = getattr(txn, "transaction_date", None)
    return (fallback.isoformat() if isinstance(fallback, date) else ""), ""


def txn_rows(transactions: Iterable[Any]) -> list[BrokerTxnRow]:
    """The broker's rows, unchanged apart from types. Nothing is filtered out —
    a row Argus did not place is exactly what the reconciliation must see."""
    out: list[BrokerTxnRow] = []
    for txn in transactions:
        trading_date, time_et = _et_parts(txn)
        fees = (
            _dec(getattr(txn, "commission", None))
            + _dec(getattr(txn, "clearing_fees", None))
            + _dec(getattr(txn, "regulatory_fees", None))
            + _dec(getattr(txn, "proprietary_index_option_fees", None))
            + _dec(getattr(txn, "other_charge", None))
        )
        order_id = getattr(txn, "order_id", None)
        quantity = getattr(txn, "quantity", None)
        price = getattr(txn, "price", None)
        out.append(BrokerTxnRow(
            trading_date=trading_date,
            time_et=time_et,
            id=str(getattr(txn, "id", "")),
            order_id="" if order_id is None else str(order_id),
            type=str(getattr(txn, "transaction_type", "") or ""),
            sub_type=str(getattr(txn, "transaction_sub_type", "") or ""),
            symbol=str(getattr(txn, "symbol", "") or ""),
            action=str(getattr(txn, "action", "") or ""),
            quantity="" if quantity is None else f"{Decimal(str(quantity)):g}",
            price="" if price is None else f"{Decimal(str(price)):.2f}",
            value=_money(_dec(getattr(txn, "value", None))),
            commission=_money(_dec(getattr(txn, "commission", None))),
            clearing_fees=_money(_dec(getattr(txn, "clearing_fees", None))),
            regulatory_fees=_money(_dec(getattr(txn, "regulatory_fees", None))),
            index_option_fees=_money(
                _dec(getattr(txn, "proprietary_index_option_fees", None))
            ),
            other_charge=_money(_dec(getattr(txn, "other_charge", None))),
            fees_total=_money(fees),
            net_value=_money(_dec(getattr(txn, "net_value", None))),
            is_estimated_fee="yes" if getattr(txn, "is_estimated_fee", False) else "",
            description=str(getattr(txn, "description", "") or ""),
        ))
    return out


# -- reconciliation -----------------------------------------------------------


def _parse(cell: str) -> Decimal | None:
    """A ledger cell back to `Decimal`. Blank stays `None`: unknown is not zero."""
    if cell == "":
        return None
    try:
        return Decimal(cell).quantize(Decimal("0.01"))
    except ArithmeticError:
        return None


def _ids(cell: str) -> set[str]:
    return {part for part in cell.split() if part}


def reconcile(
    days: Sequence[Any],
    trades: Sequence[Any],
    broker: Sequence[BrokerTxnRow],
    *,
    fetched: bool,
) -> list[ReconRow]:
    """One row per trading date. Pure; no I/O.

    `fetched` says whether the broker answered AT ALL. Without it a failed fetch and
    a genuinely empty day are the same empty list, and the second is a real state:
    a blackout day places no orders.
    """
    by_date: dict[str, list[BrokerTxnRow]] = {}
    for row in broker:
        by_date.setdefault(row.trading_date, []).append(row)

    trades_by_date: dict[str, list[Any]] = {}
    for trade in trades:
        trades_by_date.setdefault(trade.trading_date, []).append(trade)

    out: list[ReconRow] = []
    for day in days:
        rows = by_date.get(day.trading_date, [])
        day_trades = trades_by_date.get(day.trading_date, [])

        argus_orders = {
            i for trade in day_trades
            for i in _ids(trade.entry_order_ids) | _ids(trade.exit_order_ids)
        }
        broker_orders = {r.order_id for r in rows if r.order_id}
        unmatched_argus = sorted(argus_orders - broker_orders)
        unmatched_broker = sorted(broker_orders - argus_orders)
        orphans = [r for r in rows if not r.order_id]

        argus_gross = _parse(day.gross)
        argus_fees = _parse(day.argus_fees)
        broker_gross = sum((_parse(r.value) or ZERO for r in rows), ZERO) if rows else None
        broker_fees = sum((_parse(r.fees_total) or ZERO for r in rows), ZERO) if rows else None
        broker_net = sum((_parse(r.net_value) or ZERO for r in rows), ZERO) if rows else None
        estimated = sum(1 for r in rows if r.is_estimated_fee)
        unknown = sum(1 for t in day_trades if t.gross == "")

        gross_diff = (
            None if argus_gross is None or broker_gross is None
            else broker_gross - argus_gross
        )
        fee_diff = (
            None if argus_fees is None or broker_fees is None
            else broker_fees - argus_fees
        )

        # Argus against its OWN fills: needs no broker, so it outranks "unread".
        mismatched = [t.candidate_id for t in day_trades if t.pnl_check]

        if mismatched:
            status = DIFF
        elif not fetched:
            status = BROKER_UNREAD
        elif (
            unmatched_argus or unmatched_broker or orphans
            or (gross_diff is not None and gross_diff != ZERO)
            or (fee_diff is not None and fee_diff != ZERO)
        ):
            status = DIFF
        elif unknown or argus_gross is None:
            status = PROVISIONAL
        else:
            status = OK

        out.append(ReconRow(
            trading_date=day.trading_date,
            status=status,
            argus_gross=day.gross,
            broker_gross=_money(broker_gross),
            gross_diff=_money(gross_diff),
            argus_fees=day.argus_fees,
            broker_fees=_money(broker_fees),
            fee_diff=_money(fee_diff),
            broker_net=_money(broker_net),
            argus_orders=len(argus_orders),
            broker_orders=len(broker_orders),
            unmatched_argus=" ".join(unmatched_argus),
            # An orphan is named by its description, which is where a cash
            # settlement says what it settled.
            unmatched_broker=" ".join(
                unmatched_broker + [f"({r.description[:40]})" for r in orphans]
            ),
            estimated_fee_rows=estimated,
            unknown_result_trades=unknown,
            settled_gross=day.settled_gross,
            pnl_mismatch=" ".join(mismatched),
        ))
    return out


def apply_broker(
    days: Sequence[Any], trades: Sequence[Any], broker: Sequence[BrokerTxnRow],
    recon: Sequence[ReconRow],
) -> tuple[list[Any], list[Any]]:
    """Fill the broker columns on the day and trade rows.

    A trade's fees are the broker's, summed over ITS order ids — so a trade whose
    orders the broker has not reported keeps a blank fee and a blank net rather than
    a confident zero.
    """
    fees_by_order: dict[str, Decimal] = {}
    for row in broker:
        if row.order_id:
            fees_by_order[row.order_id] = (
                fees_by_order.get(row.order_id, ZERO) + (_parse(row.fees_total) or ZERO)
            )
    recon_by_date = {r.trading_date: r for r in recon}

    new_trades = []
    for trade in trades:
        orders = _ids(trade.entry_order_ids) | _ids(trade.exit_order_ids)
        known = [fees_by_order[o] for o in orders if o in fees_by_order]
        fees = sum(known, ZERO) if len(known) == len(orders) and orders else None
        gross = _parse(trade.gross)
        net = None if fees is None or gross is None else gross + fees
        status = recon_by_date.get(trade.trading_date)
        new_trades.append(replace(
            trade,
            fees=_money(fees),
            net=_money(net),
            recon=status.status if status else "",
        ))

    new_days = []
    for day in days:
        status = recon_by_date.get(day.trading_date)
        # The day's RESULT, settlement-valued trades included — not `gross`, which is
        # fill cash and on a held-to-expiry day still carries the opening credit.
        gross = _parse(day.total_gross)
        broker_fees = _parse(status.broker_fees) if status else None
        net = None if gross is None or broker_fees is None else gross + broker_fees
        new_days.append(replace(
            day,
            broker_fees=status.broker_fees if status else "",
            broker_net=status.broker_net if status else "",
            net=_money(net),
            recon=status.status if status else "",
        ))
    return new_days, new_trades


# -- the one broker call ------------------------------------------------------


async def fetch_history(config: Config, start: date, end: date) -> list[Any]:
    """`GET /accounts/{n}/transactions` over `[start, end]`. Read-only.

    Imported lazily so the pure functions above — and the whole of `tools.ledger`
    without `--broker` — need no tastytrade session, no credentials and no network.
    The login is NOT retried: repeated logins are ban risk, which is why
    `_login_with_retry` spaces its own attempts ten minutes apart.
    """
    from tastytrade import Account

    from argus.broker import TastytradeSession, trading_credentials

    tt = TastytradeSession(config, trading_credentials(config))
    session = await tt.open()
    try:
        number = config.broker.account_number
        account = (
            await Account.get(session, number) if number
            else (await Account.get(session))[0]
        )
        return await account.get_history(
            session, start_date=start, end_date=end, page_offset=None, sort="Asc"
        )
    finally:
        await tt.aclose()
