"""The broker side of the ledger (`argus.tools.broker_ledger`).

The transactions are built from the REAL 2026-09-17 trade confirmation: a put credit
spread, 2 contracts, sold 7585 at 3.00 and bought 7580 at 2.50 to open, closed at
0.08 and 0.03 — gross $90.00, fees $9.76, net $80.24, which is what `desk` reported
that evening and what the ledger's own `days.csv` says.

Fees are split across the broker's four buckets the way the confirmation shows them:
commission $2.00 on OPENING legs only, a $0.04 transaction fee, and $1.40 of clearing
and index-option fees per leg.

Nothing here reaches tastytrade: `Transaction` is the SDK's own model, built by hand.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from argus.tools import broker_ledger
from argus.tools.broker_ledger import (
    BROKER_UNREAD,
    DIFF,
    OK,
    PROVISIONAL,
    apply_broker,
    reconcile,
    txn_rows,
)
from argus.tools.ledger import DayRow, TradeRow

ENTRY_ORDER, EXIT_ORDER = 507158359, 507379992


class Txn:
    """The fields `broker_ledger` reads off an SDK `Transaction`."""

    def __init__(self, *, id, order_id, symbol, action, quantity, price, value,
                 commission="0.00", clearing="-0.20", regulatory="-0.04",
                 index_fees="-1.20", other="0.00", estimated=False,
                 at="2026-09-17T14:00:07Z", type="Trade", sub_type="Sell to Open",
                 description="Sold 2 SPXW"):
        self.id = id
        self.order_id = order_id
        self.symbol = symbol
        self.action = action
        self.quantity = Decimal(str(quantity))
        self.price = Decimal(str(price))
        self.value = Decimal(value)
        self.commission = Decimal(commission)
        self.clearing_fees = Decimal(clearing)
        self.regulatory_fees = Decimal(regulatory)
        self.proprietary_index_option_fees = Decimal(index_fees)
        self.other_charge = Decimal(other)
        self.is_estimated_fee = estimated
        self.executed_at = datetime.fromisoformat(at.replace("Z", "+00:00"))
        self.transaction_date = self.executed_at.date()
        self.transaction_type = type
        self.transaction_sub_type = sub_type
        self.description = description

    @property
    def fees(self) -> Decimal:
        # `or 0` like the broker: a bucket it does not charge comes back null.
        return sum((Decimal(str(f or 0)) for f in (
            self.commission, self.clearing_fees, self.regulatory_fees,
            self.proprietary_index_option_fees, self.other_charge,
        )), Decimal("0"))

    @property
    def net_value(self) -> Decimal:
        return self.value + self.fees


def confirmation_txns(**overrides) -> list[Txn]:
    """The four leg fills of 2026-09-17, as the broker reports them."""
    legs = [
        Txn(id=1, order_id=ENTRY_ORDER, symbol="SPXW  260917P07585000",
            action="Sell to Open", quantity=2, price="3.00", value="600.00",
            commission="-2.00"),
        Txn(id=2, order_id=ENTRY_ORDER, symbol="SPXW  260917P07580000",
            action="Buy to Open", quantity=2, price="2.50", value="-500.00",
            commission="-2.00", sub_type="Buy to Open"),
        Txn(id=3, order_id=EXIT_ORDER, symbol="SPXW  260917P07585000",
            action="Buy to Close", quantity=2, price="0.08", value="-16.00",
            at="2026-09-17T19:31:13Z", sub_type="Buy to Close"),
        Txn(id=4, order_id=EXIT_ORDER, symbol="SPXW  260917P07580000",
            action="Sell to Close", quantity=2, price="0.03", value="6.00",
            at="2026-09-17T19:31:14Z", sub_type="Sell to Close"),
    ]
    return [replace_attrs(leg, **overrides) for leg in legs]


def replace_attrs(txn: Txn, **overrides) -> Txn:
    for key, value in overrides.items():
        setattr(txn, key, value)
    return txn


def _day(**overrides) -> DayRow:
    base = dict(
        trading_date="2026-09-17", blackout="", entry_attempts=1, trades=1, closed=1,
        held_to_expiry=0, unpriceable=0, gross="90.00", argus_fees="-9.76",
        nlv_at_first_gate="40149.73", nlv_at_last_gate="40242.85", gate_rows=1624,
        version="v0.4.2", settled_gross="0.00", total_gross="90.00",
    )
    return DayRow(**{**base, **overrides})


def _trade(**overrides) -> TradeRow:
    base = dict(
        trading_date="2026-09-17", candidate_id="20260917-put-7585-7580", side="put",
        short_strike="7585", long_strike="7580", width="5", contracts=2,
        entry_time_et="10:00:07", entry_credit="0.50", entry_steps="0",
        entry_limit="0.50", exit_time_et="15:31:14",
        exit_trigger="flatten_before_close", exit_outcome="filled", exit_debit="0.05",
        outcome="closed", gross="90.00", max_loss="900.00",
        gross_pct_of_max_loss="10.0", seconds_since_prev_exit="", delta_source="",
        version="v0.4.2", entry_order_ids=str(ENTRY_ORDER),
        exit_order_ids=str(EXIT_ORDER),
    )
    return TradeRow(**{**base, **overrides})


# -- decoding -----------------------------------------------------------------


def test_the_brokers_rows_are_carried_through_with_their_fee_breakdown():
    rows = txn_rows(confirmation_txns())

    assert [r.trading_date for r in rows] == ["2026-09-17"] * 4
    assert rows[0].time_et == "10:00:07", "ET, from executed_at (UTC)"
    assert (rows[0].order_id, rows[0].value, rows[0].price) == (
        str(ENTRY_ORDER), "600.00", "3.00")
    assert rows[0].commission == "-2.00" and rows[2].commission == "0.00", \
        "commission is charged on opening legs only"
    assert rows[0].fees_total == "-3.44" and rows[2].fees_total == "-1.44"
    assert sum(Decimal(r.fees_total) for r in rows) == Decimal("-9.76"), \
        "the day's fees, as the confirmation prints them"
    assert sum(Decimal(r.value) for r in rows) == Decimal("90.00"), "gross"
    assert sum(Decimal(r.net_value) for r in rows) == Decimal("80.24"), "net"


def test_a_missing_fee_bucket_reads_as_zero_not_as_an_error():
    [txn] = confirmation_txns()[:1]
    txn.clearing_fees = None
    txn.other_charge = None

    [row] = txn_rows([txn])

    assert row.clearing_fees == "0.00" and row.fees_total == "-3.24"


# -- reconciliation -----------------------------------------------------------


def test_a_day_that_agrees_to_the_cent_is_OK():
    [recon] = reconcile([_day()], [_trade()], txn_rows(confirmation_txns()),
                        fetched=True)

    assert recon.status == OK
    assert (recon.argus_gross, recon.broker_gross, recon.gross_diff) == (
        "90.00", "90.00", "0.00")
    assert (recon.argus_fees, recon.broker_fees, recon.fee_diff) == (
        "-9.76", "-9.76", "0.00")
    assert recon.broker_net == "80.24"
    assert (recon.argus_orders, recon.broker_orders) == (2, 2)
    assert recon.unmatched_argus == "" and recon.unmatched_broker == ""


def test_a_fee_difference_of_one_cent_is_a_DIFF():
    """No tolerance: three days of confirmations matched exactly, and a tolerance
    absorbs the defect this exists to find."""
    txns = confirmation_txns()
    txns[0].commission = Decimal("-2.01")

    [recon] = reconcile([_day()], [_trade()], txn_rows(txns), fetched=True)

    assert recon.status == DIFF
    assert recon.fee_diff == "-0.01"


def test_a_broker_row_argus_never_placed_is_a_DIFF_and_is_named():
    """A cash settlement carries no order id, and the account is dedicated to Argus,
    so anything unmatched is worth looking at."""
    settlement = Txn(id=9, order_id=None, symbol="SPXW  260917P07585000",
                     action=None, quantity=2, price="0.00", value="-150.00",
                     commission="0.00", clearing="0.00", regulatory="0.00",
                     index_fees="0.00", type="Receive Deliver",
                     sub_type="Cash Settled Expiration",
                     description="Cash settlement SPXW 7585 put")

    [recon] = reconcile([_day()], [_trade()],
                        txn_rows(confirmation_txns() + [settlement]), fetched=True)

    assert recon.status == DIFF
    assert "Cash settlement" in recon.unmatched_broker


def test_an_order_the_broker_has_not_reported_is_a_DIFF():
    rows = [r for r in txn_rows(confirmation_txns()) if r.order_id != str(EXIT_ORDER)]

    [recon] = reconcile([_day()], [_trade()], rows, fetched=True)

    assert recon.status == DIFF
    assert recon.unmatched_argus == str(EXIT_ORDER)


def test_an_estimated_fee_is_counted_and_decides_nothing():
    """The first live read (2026-09-22) returned `is_estimated_fee` on EVERY row of
    three days whose fees match the clearing firm's confirmation exactly. Gating on
    it would make `OK` unreachable, so it is recorded and nothing branches on it."""
    txns = confirmation_txns()
    for txn in txns:
        txn.is_estimated_fee = True

    [recon] = reconcile([_day()], [_trade()], txn_rows(txns), fetched=True)

    assert recon.status == OK
    assert recon.estimated_fee_rows == 4


def test_a_trade_with_an_unknown_result_keeps_the_day_PROVISIONAL():
    """A spread held to expiry has no gross until the broker settles it."""
    [recon] = reconcile([_day(gross="", total_gross="")],
                        [_trade(gross="", outcome="held_to_expiry")],
                        txn_rows(confirmation_txns()), fetched=True)

    assert recon.status == PROVISIONAL
    assert recon.unknown_result_trades == 1
    assert recon.gross_diff == "", "unknown minus a number is not a difference"


def test_a_failed_fetch_is_BROKER_UNREAD_not_a_clean_day():
    [recon] = reconcile([_day()], [_trade()], [], fetched=False)

    assert recon.status == BROKER_UNREAD
    assert recon.broker_gross == "" and recon.broker_fees == ""


def test_a_day_the_broker_reports_nothing_for_is_not_silently_OK():
    """A blackout day places no orders; the broker legitimately has nothing. It is
    still not `OK`, because Argus's own gross is a structural zero there."""
    blackout = _day(blackout="yes", trades=0, closed=0, gross="0.00",
                    argus_fees="0.00")

    [recon] = reconcile([blackout], [], [], fetched=True)

    assert recon.status == OK, "nothing placed, nothing reported, nothing owed"
    assert (recon.argus_orders, recon.broker_orders) == (0, 0)


# -- the columns the recon fills in -------------------------------------------


def test_the_broker_fee_lands_on_the_trade_and_the_day():
    broker = txn_rows(confirmation_txns())
    days, trades = [_day()], [_trade()]
    recon = reconcile(days, trades, broker, fetched=True)

    days, trades = apply_broker(days, trades, broker, recon)

    assert (trades[0].fees, trades[0].net, trades[0].recon) == ("-9.76", "80.24", OK)
    assert (days[0].broker_fees, days[0].broker_net) == ("-9.76", "80.24")
    assert days[0].net == "80.24" and days[0].recon == OK


def test_a_trade_whose_orders_the_broker_has_not_all_reported_keeps_a_BLANK_fee():
    """A partial answer is not a fee, and a confident zero is worse than a blank."""
    broker = [r for r in txn_rows(confirmation_txns()) if r.order_id != str(EXIT_ORDER)]
    days, trades = [_day()], [_trade()]
    recon = reconcile(days, trades, broker, fetched=True)

    _, trades = apply_broker(days, trades, broker, recon)

    assert trades[0].fees == "" and trades[0].net == ""
    assert trades[0].recon == DIFF


def test_an_unread_broker_leaves_the_broker_columns_blank():
    days, trades = [_day()], [_trade()]
    recon = reconcile(days, trades, [], fetched=False)

    days, trades = apply_broker(days, trades, [], recon)

    assert (days[0].broker_fees, days[0].broker_net, days[0].net) == ("", "", "")
    assert days[0].recon == BROKER_UNREAD
    assert days[0].gross == "90.00", "the Argus side is untouched"


# -- rails --------------------------------------------------------------------


def test_the_broker_call_is_history_and_nothing_that_can_move_money():
    source = Path(broker_ledger.__file__).read_text()
    tree = ast.parse(source)
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "get_history" in called
    for forbidden in ("place_order", "replace_order", "cancel_order", "delete_order"):
        assert forbidden not in source, f"{forbidden} has no business in a reporting tool"
    imported = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not any(m.startswith("argus.execution") for m in imported)


def test_the_pure_functions_need_no_tastytrade_import():
    """`tools.ledger` without `--broker` must not need credentials or the SDK, so the
    session import is inside `fetch_history`."""
    tree = ast.parse(Path(broker_ledger.__file__).read_text())
    top_level = {
        n.module for n in tree.body if isinstance(n, ast.ImportFrom) and n.module
    }
    assert not any(m.startswith("tastytrade") or m == "argus.broker" for m in top_level)
