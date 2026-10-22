"""Valuing a held-to-expiry trade from settlement (`argus.tools.settlement`).

→ docs/decisions/2026-09-22-valuing-a-held-to-expiry-trade.md

The day is the real 2026-09-22 one: the first trade ever selected on mid-implied
delta, `20260922-put-7740-7735`, 2 contracts at a 0.50 credit, entered 10:00:00 and
never closed — the 15:30 flatten found no market and the spread expired. The leg
prices (1.20 / 0.70) are illustrative; only their 0.50 difference is the record's.
The settlement `7753.21` is the last recorded spot in the 2026-09-21 snapshot, used
as an illustrative value above the short strike — NOT that day's actual close, which
none of these tests needs or claims.

The broker fees are the confirmation schedule for a spread held to expiry: two
opening legs at $3.44 each (a $2.00 commission, $0.04 transaction fee and $1.40 of
clearing and index-option fees), $6.88 in all — what the live read showed.

Payloads are built BY HAND, on `test_desk.py`'s rule: a fixture built by the
production writer cannot catch the production writer changing shape.
"""

from __future__ import annotations

import ast
from dataclasses import fields, replace
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest

import argus.tools.settlement as settlement_module
from argus.marketcalendar import MarketCalendar
from argus.shadow.outcome import settlement_liability
from argus.tools import sheets_report
from argus.tools.broker_ledger import (
    DIFF,
    OK,
    PROVISIONAL,
    ReconRow,
    apply_broker,
    reconcile,
    txn_rows,
)
from argus.tools.ledger import NO_OVERRIDES, DayRow, TradeRow, build, load_overrides
from argus.tools.settlement import (
    NO_SPOT,
    SETTLEMENT_FILE_ERROR,
    SETTLEMENT_OFFICIAL,
    SETTLEMENT_SPOT,
    SPOT_TOO_EARLY,
    read_settlement_overrides,
    read_settlement_spots,
    resolve_settlement,
    settle_trade,
)

from support import make_config
from test_broker_ledger import Txn

DAY = date(2026, 9, 22)
CAL = MarketCalendar.from_config(make_config().market)
PUT = "20260922-put-7740-7735"
CALL = "20260922-call-7800-7805"
ENTRY_ORDER, CALL_ENTRY, CALL_EXIT = 508100001, 508100002, 508100003
SETTLE = 7753.21


def _ts(hms: str, day: date = DAY) -> str:
    """An ET wall-clock time as the chain's UTC timestamp (EDT = UTC-4)."""
    h, m, s = (int(x) for x in hms.split(":"))
    return f"{day.isoformat()}T{h + 4:02d}:{m:02d}:{s:02d}.000000Z"


async def _fill(audit, candidate, *, direction, action, right, strike, price, at,
                order_id, contracts=2, day=DAY):
    audit._clock = lambda: _ts(at, day)
    await audit.record("order.fill", {
        "broker_order_id": order_id,
        "external_identifier": f"argus-{direction}-{candidate}",
        "symbol": f"SPXW  {day:%y%m%d}{right}0{strike}000",
        "action": action,
        "contracts": contracts,
        "signed_delta": contracts if action.startswith("Buy") else -contracts,
        "price": price,
        "position_before": 0,
        "position_after": 0,
        "status": "Filled",
        "candidate_id": None,
    })


async def _open_put(audit, *, day=DAY, candidate=PUT, contracts=2):
    await _fill(audit, candidate, direction="open", action="Sell to Open", right="P",
                strike=7740, price=1.20, at="10:00:00", order_id=ENTRY_ORDER,
                contracts=contracts, day=day)
    await _fill(audit, candidate, direction="open", action="Buy to Open", right="P",
                strike=7735, price=0.70, at="10:00:00", order_id=ENTRY_ORDER,
                contracts=contracts, day=day)


async def _mark(audit, spot, at, *, category="watchdog.check", candidate=PUT, day=DAY):
    """A watchdog evaluation row: `spot` at the top level, as `to_payload` splats it."""
    audit._clock = lambda: _ts(at, day)
    await audit.record(category, {
        "candidate_id": candidate, "contracts": 2, "cost_to_close": 0.05,
        "credit_stop": 1.0, "spot": spot, "unknown_reasons": [],
        **({"exit_outcome": "not_resolved"} if category == "watchdog.check_spent" else {}),
    })


async def _fees(audit, fees=-6.88):
    audit._clock = lambda: _ts("10:00:00")
    await audit.record("order.submitted", {"dry_run": False, "fees": fees, "order": {}})


def _opening_txns(**overrides) -> list[Txn]:
    """The broker's two opening legs — and NOTHING for the OTM expiry, which is the
    whole reason this module exists."""
    at = "2026-09-22T14:00:00Z"
    legs = [
        Txn(id=11, order_id=ENTRY_ORDER, symbol="SPXW  260922P07740000",
            action="Sell to Open", quantity=2, price="1.20", value="240.00",
            commission="-2.00", at=at),
        Txn(id=12, order_id=ENTRY_ORDER, symbol="SPXW  260922P07735000",
            action="Buy to Open", quantity=2, price="0.70", value="-140.00",
            commission="-2.00", at=at, sub_type="Buy to Open"),
    ]
    for leg in legs:
        for key, value in overrides.items():
            setattr(leg, key, value)
    return legs


def _with_broker(ledger, txns):
    broker = txn_rows(txns)
    recon = reconcile(ledger.days, ledger.trades, broker, fetched=True)
    days, trades = apply_broker(ledger.days, ledger.trades, broker, recon)
    return days, trades, recon


# -- 1. the real day, end to end ----------------------------------------------


async def test_the_real_2026_09_22_day_is_valued_at_settlement_and_reconciles(db, audit):
    await _open_put(audit)
    await _fees(audit)
    await _mark(audit, 7760.10, "15:30:00")
    await _mark(audit, SETTLE, "15:59:58")

    ledger = build(db, [DAY], CAL)
    [t] = ledger.trades

    assert t.outcome == "held_to_expiry"
    assert t.entry_credit == "0.50"
    assert (t.gross, t.gross_basis) == ("100.00", SETTLEMENT_SPOT)
    assert (t.settlement_price, t.settlement_result) == ("7753.21", "full_credit")
    assert t.settlement_at_et == "15:59:58", "how old the proxy is, on the row"
    assert t.settled_gross == "100.00", "held in full: all of it is at settlement"
    assert t.pnl_check == "", "100 cash - 0 owed == 100 gross"
    assert t.gross_pct_of_max_loss == "11.1", "100 / ((5 - 0.50) x 2 x 100)"

    days, trades, [recon] = _with_broker(ledger, _opening_txns())
    [d], [t] = days, trades

    assert (t.fees, t.net) == ("-6.88", "93.12")
    assert (d.gross, d.settled_gross, d.total_gross) == ("100.00", "100.00", "100.00")
    assert (d.net, d.recon) == ("93.12", OK)
    assert (recon.status, recon.gross_diff, recon.fee_diff) == (OK, "0.00", "0.00")
    assert recon.settled_gross == "100.00"
    assert recon.unknown_result_trades == 0, "PROVISIONAL regains its meaning"


# -- 2 and 3. the three results, both sides -----------------------------------


@pytest.mark.parametrize("settlement, liability, gross, result", [
    (7753.21, 0.0, 100.00, "full_credit"),
    (7737.50, 2.5, -400.00, "partial"),
    (7730.00, 5.0, -900.00, "max_loss"),
])
def test_a_put_spread_settles_through_the_shared_liability(
    settlement, liability, gross, result
):
    assert settlement_liability("put", 7740, 7735, settlement) == liability
    settled = settle_trade("put", 7740, 7735, 2, 0.50, settlement)
    assert (round(settled.gross, 2), settled.result) == (gross, result)
    assert round(settled.liability, 2) == liability * 2 * 100, "dollars owed, 2 contracts"


@pytest.mark.parametrize("settlement, gross, result", [
    (7790.00, 120.00, "full_credit"),
    (7802.00, -280.00, "partial"),      # liability 2.00 on a 0.60 credit
    (7810.00, -880.00, "max_loss"),
])
def test_a_call_spread_takes_the_other_branch(settlement, gross, result):
    """So the side branch is covered rather than assumed."""
    settled = settle_trade("call", 7800, 7805, 2, 0.60, settlement)
    assert (round(settled.gross, 2), settled.result) == (gross, result)


# -- 4. a spot too far from the close ----------------------------------------


async def test_a_spot_45_minutes_before_the_close_is_refused_not_used(db, audit):
    await _open_put(audit)
    await _fees(audit)
    await _mark(audit, SETTLE, "15:15:00")

    ledger = build(db, [DAY], CAL)
    [t] = ledger.trades
    assert (t.gross, t.gross_basis, t.settlement_price) == ("", SPOT_TOO_EARLY, "")
    assert t.settlement_at_et == "15:15:00", "the refusal carries its own diagnosis"
    assert t.pnl_check == "", "no gross, nothing to check — and never a pass by default"

    [d], _, [recon] = _with_broker(ledger, _opening_txns())
    assert d.total_gross == "" and d.settled_gross == ""
    assert d.gross == "100.00", "the fills' own cash is still known and still compared"
    assert (recon.status, recon.gross_diff) == (PROVISIONAL, "0.00")


async def test_a_day_with_no_spot_at_all_names_that_rather_than_guessing(db, audit):
    await _open_put(audit)
    assert build(db, [DAY], CAL).trades[0].gross_basis == NO_SPOT


async def test_the_newest_spot_is_read_across_both_mark_categories(db, audit):
    """A spent exit moves the marks to `check_spent` and never back (2026-09-14): the
    held-to-expiry spread is exactly the position most likely to have one."""
    await _mark(audit, 7700.0, "15:20:00")
    await _mark(audit, SETTLE, "15:59:40", category="watchdog.check_spent")

    price, at = read_settlement_spots(db, DAY)
    assert price == SETTLE and at.strftime("%H:%M:%S") == "15:59:40"


async def test_a_tick_that_could_not_read_spot_is_skipped_not_taken(db, audit):
    await _mark(audit, SETTLE, "15:59:00")
    await _mark(audit, None, "15:59:59")

    assert read_settlement_spots(db, DAY)[0] == SETTLE


async def test_a_non_positive_spot_falls_through_to_the_last_usable_one(db, audit):
    """Filtered in SQL, so the newest row that HAS a price is found rather than the
    newest row deciding there is none."""
    await _mark(audit, SETTLE, "15:59:00")
    await _mark(audit, 0.0, "15:59:59")

    price, at = read_settlement_spots(db, DAY)
    assert (price, at.strftime("%H:%M:%S")) == (SETTLE, "15:59:00")


async def test_another_days_spot_is_never_read(db, audit):
    await _mark(audit, SETTLE, "15:59:00", day=date(2026, 9, 21))
    assert read_settlement_spots(db, DAY) is None


# -- 5 and 6. the persistent override file -----------------------------------


async def test_an_official_print_wins_over_the_recorded_spot(db, audit):
    await _open_put(audit)
    await _mark(audit, SETTLE, "15:59:58")

    [t] = build(db, [DAY], CAL, {DAY: 7737.50}).trades

    assert (t.gross_basis, t.settlement_price) == (SETTLEMENT_OFFICIAL, "7737.50")
    assert (t.gross, t.settlement_result) == ("-400.00", "partial")
    assert t.settlement_at_et == "", "an official print has no recorded-spot time"


def test_a_malformed_override_line_costs_its_own_date_and_nothing_else(tmp_path):
    path = tmp_path / "settlements.csv"
    path.write_text(
        "# official prints — Cboe SPX settlement\n"      # non-ASCII: read as UTF-8
        "\n"
        "2026-09-21,7702.55   # the 09-21 close\n"
        "2026-09-22,77x3.21\n"
        "2026-09-23,7710.00\n"
        "2026-09-23,7711.00\n"
        "20260924,7720\n"
        "2026-09-25,-1\n"
    )
    assert read_settlement_overrides(path) == {
        date(2026, 9, 21): 7702.55,
        date(2026, 9, 22): None,      # unreadable price
        date(2026, 9, 23): None,      # two answers are not an answer
        date(2026, 9, 25): None,      # not a price
    }, "an unreadable DATE cannot cost any date, so that line alone is dropped"


async def test_a_refused_override_date_is_not_valued_from_the_spot_instead(db, audit):
    """The operator said the spot was wrong for that day; falling back to it would
    silently overrule them."""
    await _open_put(audit)
    await _mark(audit, SETTLE, "15:59:58")

    [t] = build(db, [DAY], CAL, {DAY: None}).trades
    assert (t.gross, t.gross_basis) == ("", SETTLEMENT_FILE_ERROR)


async def test_a_named_but_unreadable_file_refuses_every_date(db, audit):
    await _open_put(audit)
    await _mark(audit, SETTLE, "15:59:58")

    assert resolve_settlement(db, DAY, CAL, None).basis == SETTLEMENT_FILE_ERROR
    assert build(db, [DAY], CAL, None).trades[0].gross_basis == SETTLEMENT_FILE_ERROR


async def test_an_override_for_another_date_leaves_this_one_on_the_spot(db, audit):
    await _open_put(audit)
    await _mark(audit, SETTLE, "15:59:58")
    [t] = build(db, [DAY], CAL, {date(2026, 9, 21): 7000.0}).trades
    assert t.gross_basis == SETTLEMENT_SPOT


def test_the_cli_turns_an_unreadable_file_into_a_refusal_not_a_failed_run(
    tmp_path, capsys
):
    """Refusing the run would cost every other day's CSVs for one bad file."""
    assert load_overrides(None) is NO_OVERRIDES, "no file named: every date on the spot"
    assert load_overrides(tmp_path / "absent.csv") is None
    assert "every settlement valuation refused" in capsys.readouterr().err

    good = tmp_path / "settlements.csv"
    good.write_text("2026-09-22,7753.21\n")
    assert load_overrides(good) == {DAY: SETTLE}


# -- 7. the close comes from the calendar -------------------------------------


@pytest.mark.parametrize("at, basis", [
    ("12:50:00", SETTLEMENT_SPOT),   # 10 min before a 13:00 half-day close
    ("12:40:00", SPOT_TOO_EARLY),    # 20 min before it
])
async def test_a_half_day_is_measured_against_its_own_close(db, audit, at, basis):
    half_day = replace(CAL, half_days=frozenset({DAY}), half_day_close=time(13, 0))
    await _mark(audit, SETTLE, at)

    assert resolve_settlement(db, DAY, half_day, NO_OVERRIDES).basis == basis
    # …and on the regular calendar the same 12:50 spot is three hours too early.
    assert resolve_settlement(db, DAY, CAL, NO_OVERRIDES).basis == SPOT_TOO_EARLY


# -- 8. the reconciliation is fills-only --------------------------------------


async def test_the_broker_comparison_never_sees_a_settlement_number(db, audit):
    """THE load-bearing rule. A partial settlement makes the three grosses differ, so
    the test can tell which one reached the comparison."""
    await _open_put(audit)
    await _fees(audit)
    await _mark(audit, 7737.50, "15:59:58")

    days, _, [recon] = _with_broker(build(db, [DAY], CAL), _opening_txns())
    [d] = days

    assert (d.gross, d.settled_gross, d.total_gross) == ("100.00", "-400.00", "-400.00")
    assert recon.argus_gross == d.gross, "recon compares FILL CASH"
    assert recon.argus_gross != d.total_gross
    assert (recon.status, recon.gross_diff) == (OK, "0.00")
    assert d.net == "-406.88", "the day's net is its RESULT, not its fill cash"


# -- the invariant that ties the P&L to the broker's cash ---------------------


async def _imbalanced_round_trip(audit):
    """A short leg that filled 2 against a long leg that filled 1, then a 1x1 close.
    `_net_legs` counts the MINIMUM (1 spread) and flags nothing, so the P&L is 30.00
    on one contract — while the fills moved 150.00, which is what the broker reports.
    Before settlement valuation the recon's Argus side was the P&L and this was a
    $120 DIFF; comparing CASH alone, it would reconcile OK."""
    await _fill(audit, PUT, direction="open", action="Sell to Open", right="P",
                strike=7740, price=1.20, at="10:00:00", order_id=ENTRY_ORDER)
    await _fill(audit, PUT, direction="open", action="Buy to Open", right="P",
                strike=7735, price=0.70, at="10:00:00", order_id=ENTRY_ORDER,
                contracts=1)
    await _fill(audit, PUT, direction="close", action="Buy to Close", right="P",
                strike=7740, price=0.25, at="11:00:00", order_id=508100009,
                contracts=1)
    await _fill(audit, PUT, direction="close", action="Sell to Close", right="P",
                strike=7735, price=0.05, at="11:00:00", order_id=508100009,
                contracts=1)


def _imbalanced_txns() -> list[Txn]:
    """The broker's side of the same fills, fee-free so nothing ELSE can DIFF."""
    free = dict(commission="0.00", clearing="0.00", regulatory="0.00",
                index_fees="0.00", at="2026-09-22T15:00:00Z")
    return [
        Txn(id=21, order_id=ENTRY_ORDER, symbol="SPXW  260922P07740000",
            action="Sell to Open", quantity=2, price="1.20", value="240.00", **free),
        Txn(id=22, order_id=ENTRY_ORDER, symbol="SPXW  260922P07735000",
            action="Buy to Open", quantity=1, price="0.70", value="-70.00", **free),
        Txn(id=23, order_id=508100009, symbol="SPXW  260922P07740000",
            action="Buy to Close", quantity=1, price="0.25", value="-25.00", **free),
        Txn(id=24, order_id=508100009, symbol="SPXW  260922P07735000",
            action="Sell to Close", quantity=1, price="0.05", value="5.00", **free),
    ]


async def test_a_pnl_that_does_not_follow_from_its_fills_is_a_DIFF(db, audit):
    """THE invariant pinned: gross == the trade's fill cash - what it owed at
    settlement. The broker comparison agrees to the cent here — which is exactly why
    it cannot be the only check once it compares cash rather than P&L."""
    await _imbalanced_round_trip(audit)

    ledger = build(db, [DAY], CAL)
    [t], [d] = ledger.trades, ledger.days
    assert (t.outcome, t.gross, d.gross) == ("closed", "30.00", "150.00")
    assert t.pnl_check == "gross 30.00 != fill cash 150.00"

    _, _, [recon] = _with_broker(ledger, _imbalanced_txns())
    assert (recon.gross_diff, recon.fee_diff) == ("0.00", "0.00"), \
        "the broker agrees with the CASH — the old check would not have"
    assert (recon.unmatched_argus, recon.unmatched_broker) == ("", "")
    assert (recon.status, recon.pnl_mismatch) == (DIFF, PUT)


async def test_the_invariant_needs_no_broker_and_outranks_unread(db, audit):
    await _imbalanced_round_trip(audit)
    ledger = build(db, [DAY], CAL)

    [recon] = reconcile(ledger.days, ledger.trades, [], fetched=False)
    assert (recon.status, recon.pnl_mismatch) == (DIFF, PUT)


@pytest.mark.parametrize("settle_at, gross", [
    (SETTLE, "100.00"),       # full credit: owed 0
    (7737.50, "-400.00"),     # partial: owed 500
    (7730.00, "-900.00"),     # max loss: owed 1,000
])
async def test_a_settled_trade_satisfies_the_invariant_at_every_result(
    db, audit, settle_at, gross
):
    """The liability is the ONLY part of a settled gross that is not fill cash, so
    the identity holds whichever way the index finished — a check that fired on
    every partial would be a check nobody reads."""
    await _open_put(audit)
    await _mark(audit, settle_at, "15:59:58")

    [t] = build(db, [DAY], CAL).trades
    assert (t.gross, t.pnl_check) == (gross, "")


def test_the_check_is_to_the_cent_and_says_nothing_about_an_unknown():
    from argus.tools.ledger import pnl_check

    assert pnl_check(-400.0, 100.0, 500.0) == ""
    assert pnl_check(-400.0 + 0.004, 100.0, 500.0) == "", "float noise below a cent"
    assert pnl_check(-399.99, 100.0, 500.0) == \
        "gross -399.99 != fill cash 100.00 - settlement liability 500.00"
    assert pnl_check(None, 100.0, 0.0) == "" and pnl_check(30.0, None, 0.0) == ""


# -- 9. the ITM case stays loud -----------------------------------------------


async def test_an_itm_expirys_broker_cash_settlement_is_a_DIFF_and_is_named(db, audit):
    """Its type strings have never been seen, so it is NOT paired with the trade —
    and deliberately not guessed. It stays loud until a real row is captured."""
    await _open_put(audit)
    await _fees(audit)
    await _mark(audit, 7737.50, "15:59:58")
    cash = Txn(id=13, order_id=None, symbol="SPXW  260922P07740000", action=None,
               quantity=2, price="0", value="-500.00", commission="0.00",
               clearing="0.00", regulatory="0.00", index_fees="0.00",
               at="2026-09-22T22:00:00Z", type="Receive Deliver",
               sub_type="Cash Settled Assignment",
               description="Removal of option due to cash settlement")

    _, _, [recon] = _with_broker(build(db, [DAY], CAL), [*_opening_txns(), cash])

    assert recon.status == DIFF
    assert "Removal of option due to cash settlement" in recon.unmatched_broker


# -- 10. unknown is not zero --------------------------------------------------


async def test_the_days_result_is_blank_when_any_trade_is_unvalued(db, audit):
    """A closed call spread (known) beside a held put with no usable spot."""
    await _fill(audit, CALL, direction="open", action="Sell to Open", right="C",
                strike=7800, price=1.00, at="10:05:00", order_id=CALL_ENTRY)
    await _fill(audit, CALL, direction="open", action="Buy to Open", right="C",
                strike=7805, price=0.40, at="10:05:00", order_id=CALL_ENTRY)
    await _fill(audit, CALL, direction="close", action="Buy to Close", right="C",
                strike=7800, price=0.30, at="11:00:00", order_id=CALL_EXIT)
    await _fill(audit, CALL, direction="close", action="Sell to Close", right="C",
                strike=7805, price=0.10, at="11:00:00", order_id=CALL_EXIT)
    await _open_put(audit)
    await _mark(audit, SETTLE, "14:00:00")          # too early to settle the put

    ledger = build(db, [DAY], CAL)
    by_id = {t.candidate_id: t for t in ledger.trades}
    [d] = ledger.days

    assert (by_id[CALL].gross, by_id[CALL].gross_basis) == ("80.00", "fills")
    assert by_id[PUT].gross == ""
    assert d.total_gross == "" and d.settled_gross == ""
    assert d.gross == "180.00", "fill cash: 80 realized + 100 opening credit"


async def test_a_partly_closed_trade_is_realized_plus_settled(db, audit):
    """1 of 2 contracts bought back at 0.20; the other expires full-credit. Nothing
    special-cased: `ClosedPosition.pnl` plus `settle_trade` on what expired."""
    await _open_put(audit)
    await _fill(audit, PUT, direction="close", action="Buy to Close", right="P",
                strike=7740, price=0.25, at="15:30:05", order_id=508100009,
                contracts=1)
    await _fill(audit, PUT, direction="close", action="Sell to Close", right="P",
                strike=7735, price=0.05, at="15:30:05", order_id=508100009,
                contracts=1)
    await _mark(audit, SETTLE, "15:59:58")

    ledger = build(db, [DAY], CAL)
    [t], [d] = ledger.trades, ledger.days
    assert t.outcome == "partly_closed"
    assert (t.gross, t.gross_basis) == ("80.00", SETTLEMENT_SPOT), "30 + 50"
    assert t.settled_gross == "50.00", "the expired contract's term only"
    assert (d.total_gross, d.settled_gross) == ("80.00", "50.00"), \
        "the 30.00 realized on fills is not 'valued at settlement'"
    assert t.pnl_check == "", "cash 100 open - 20 close = 80, less 0 owed == 80"


# -- 11. the dashboard counts it, and says so ---------------------------------


def _kpi(label: str) -> list[str]:
    dashboard = next(t for t in sheets_report.tabs() if t.title == "Dashboard")
    return next(row for row in dashboard.rows if row and row[0] == label)


def test_the_dashboard_net_pnl_sums_the_days_result_not_its_fill_cash():
    total = sheets_report.span("days", "total_gross")
    fills = sheets_report.span("days", "gross")
    fees = sheets_report.span("days", "argus_fees")

    net = _kpi("Net P&L")[1]
    assert f"SUMIFS({total}," in net and f"SUMIFS({fees}," in net
    assert f"SUMIFS({fills}," not in net, "fill cash double-counts a held credit"
    assert _kpi("Gross")[1].startswith(f"=SUMIFS({total},")
    assert _kpi("Days with an unknown result")[1].startswith(f'=COUNTIFS({total},"",')


def test_the_dashboard_reports_how_much_was_valued_at_settlement():
    row = _kpi("Of which valued at settlement")
    assert row[1].startswith(f"=SUMIFS({sheets_report.span('days', 'settled_gross')},")
    basis = sheets_report.span("trades", "gross_basis")
    for b in (SETTLEMENT_SPOT, SETTLEMENT_OFFICIAL):
        assert f'COUNTIFS({basis},"{b}",' in row[4]


def test_the_trade_list_shows_the_basis():
    dashboard = next(t for t in sheets_report.tabs() if t.title == "Dashboard")
    [trade_list] = [c for r in dashboard.rows for c in r if "'exit trigger'" in c]
    assert f"{sheets_report.col('trades', 'gross_basis')} 'basis'" in trade_list


def test_the_rollups_and_the_chart_read_the_days_result():
    total = sheets_report.span("days", "total_gross")
    tabs = {t.title: t for t in sheets_report.tabs()}
    assert total in tabs["calc"].rows[1][1]
    for title in ("Weekly", "Monthly"):
        assert sheets_report.span("days", "total_gross", absolute=True) in \
            tabs[title].rows[1][3]


# -- the columns --------------------------------------------------------------


def test_new_columns_are_appended_so_no_existing_letter_moves():
    """The formulas address the data tabs by letter; the letters as of the record
    (days A–Q, trades A–AA) must still hold, and the new columns come after them."""
    col = sheets_report.col
    assert [f.name for f in fields(DayRow)][-2:] == ["settled_gross", "total_gross"]
    assert [f.name for f in fields(TradeRow)][-6:] == [
        "gross_basis", "settlement_price", "settlement_at_et", "settlement_result",
        "settled_gross", "pnl_check"]
    assert [f.name for f in fields(ReconRow)][-2:] == ["settled_gross", "pnl_mismatch"]
    assert (col("days", "gross"), col("days", "argus_fees"), col("days", "recon")) == \
        ("H", "I", "Q")
    assert (col("trades", "gross"), col("trades", "max_loss"),
            col("trades", "gross_pct_of_max_loss"),
            col("trades", "seconds_since_prev_exit"), col("trades", "delta_source"),
            col("trades", "recon")) == ("Q", "R", "S", "T", "U", "AA")
    assert col("trades", "gross_basis") == "AB"
    assert sheets_report.whole("trades") == "trades!A2:AG"
    assert sheets_report.whole("recon") == "recon!A2:Q"


def test_a_column_name_that_is_not_a_field_fails_rather_than_reads_a_neighbour():
    with pytest.raises(ValueError):
        sheets_report.col("days", "gros")


# -- rails --------------------------------------------------------------------


def test_the_settlement_math_is_the_shared_scorers_and_nothing_else():
    """`settlement_liability` and `classify` are the only settlement math: no
    clamping of strikes here, and both are imported from `shadow.outcome`."""
    source = Path(settlement_module.__file__).read_text()
    tree = ast.parse(source)
    calls = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert not calls & {"min", "max", "clamp"}
    imported = {
        (n.module, a.name) for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) for a in n.names
    }
    assert ("argus.shadow.outcome", "settlement_liability") in imported
    assert ("argus.shadow.outcome", "classify") in imported
    assert {"settlement_liability", "classify"} <= calls


def test_the_settlement_module_opens_no_market_data_and_reaches_no_order():
    source = Path(settlement_module.__file__).read_text()
    tree = ast.parse(source)
    imported = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    } | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in imported if m.startswith(("argus.marketdata", "argus.broker",
                                                     "argus.execution", "tastytrade"))}
    assert "place_order" not in source


def test_money_on_the_settled_row_is_the_cent_the_broker_would_print():
    """Fixed two-place strings, rounded in ONE place, so a Decimal re-read is exact."""
    gross = settle_trade("put", 7740, 7735, 2, 0.50, 7737.35).gross
    assert Decimal(f"{round(gross, 2):.2f}") == Decimal("-430.00")
