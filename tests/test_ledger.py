"""The trading ledger (`argus.tools.ledger`) — columns around the desk's numbers.

Payloads are built BY HAND, matching what the production writers emit, on
`test_desk.py`'s rule: a fixture built by the production writer cannot catch the
production writer changing shape.

The numbers are the real 2026-09-21 ones, read off the broker's trade
confirmations: two call spreads, 2 contracts each.

- `call-7725-7730`: sold 7725 at 1.95, bought 7730 at 1.30 (0.65 credit), closed
  by buying 7725 at 4.73 and selling 7730 at 3.23 (1.50 debit) -> -170.00.
- `call-7735-7740`: sold 7735 at 2.30, bought 7740 at 1.65 (0.65 credit), closed
  by buying 7735 at 4.12 and selling 7740 at 2.72 (1.40 debit) -> -150.00.
"""

from __future__ import annotations

import csv
from datetime import date

from argus.marketcalendar import MarketCalendar
from argus.tools import desk
from argus.tools.ledger import (
    DayRow,
    FillRow,
    TradeRow,
    _money,
    _price,
    build,
    first_record_date,
    first_session_date,
    write_ledger,
)

from support import make_config

DAY = date(2026, 9, 21)
CAL = MarketCalendar.from_config(make_config().market)
T1 = "20260921-call-7725-7730"
T2 = "20260921-call-7735-7740"


def _ts(hms: str) -> str:
    """An ET wall-clock time on DAY as the chain's UTC timestamp (EDT = UTC-4)."""
    h, m, s = (int(x) for x in hms.split(":"))
    return f"2026-09-21T{h + 4:02d}:{m:02d}:{s:02d}.000000Z"


async def _fill(audit, candidate, *, direction, action, strike, price, at,
                order_id, contracts=2):
    audit._clock = lambda: _ts(at)
    await audit.record(
        "order.fill",
        {
            "broker_order_id": order_id,
            "external_identifier": f"argus-{direction}-{candidate}",
            "symbol": f"SPXW  260921C0{strike}000",
            "action": action,
            "contracts": contracts,
            "signed_delta": contracts if action.startswith("Buy") else -contracts,
            "price": price,
            "position_before": 0,
            "position_after": 0,
            "status": "Filled",
            "candidate_id": None,
        },
    )


async def _trade1(audit, *, close=True):
    await _fill(audit, T1, direction="open", action="Sell to Open", strike=7725,
                price=1.95, at="10:00:07", order_id=507889144)
    await _fill(audit, T1, direction="open", action="Buy to Open", strike=7730,
                price=1.30, at="10:00:07", order_id=507889144)
    if close:
        await _fill(audit, T1, direction="close", action="Buy to Close", strike=7725,
                    price=4.73, at="10:21:54", order_id=507922468)
        await _fill(audit, T1, direction="close", action="Sell to Close", strike=7730,
                    price=3.23, at="10:21:54", order_id=507922468)


async def _trade2(audit):
    await _fill(audit, T2, direction="open", action="Sell to Open", strike=7735,
                price=2.30, at="10:22:00", order_id=507922509)
    await _fill(audit, T2, direction="open", action="Buy to Open", strike=7740,
                price=1.65, at="10:22:00", order_id=507922509)
    await _fill(audit, T2, direction="close", action="Buy to Close", strike=7735,
                price=4.12, at="11:12:10", order_id=507979719)
    await _fill(audit, T2, direction="close", action="Sell to Close", strike=7740,
                price=2.72, at="11:12:10", order_id=507979719)


def _one_day(db):
    return build(db, [DAY], CAL)


# -- trades: the desk's number, with columns around it ------------------------


async def test_a_closed_trade_carries_the_desks_gross_and_its_order_ids(db, audit):
    await _trade1(audit)

    [t] = _one_day(db).trades

    assert t.outcome == "closed"
    assert t.entry_credit == "0.65" and t.exit_debit == "1.50"
    assert t.gross == "-170.00", "the real 2026-09-21 number"
    assert t.max_loss == "870.00", "(5 - 0.65) x 2 x 100"
    assert t.gross_pct_of_max_loss == "-19.5"
    assert t.entry_time_et == "10:00:07" and t.exit_time_et == "10:21:54"
    assert t.entry_order_ids == "507889144" and t.exit_order_ids == "507922468"
    assert (t.side, t.short_strike, t.long_strike, t.width) == ("call", "7725", "7730", "5")


async def test_trade_gross_is_exactly_the_desks_realized_number(db, audit):
    """Not a second scorer: the ledger's gross must be `ClosedPosition.pnl`."""
    await _trade1(audit)
    await _trade2(audit)

    summary = desk.summarize(desk.load(db, DAY))
    ledger = _one_day(db)

    assert {t.candidate_id: t.gross for t in ledger.trades} == {
        c.candidate_id: _money(c.pnl) for c in summary.closed
    }
    assert ledger.days[0].gross == _money(summary.realized) == "-320.00"
    assert ledger.days[0].total_gross == "-320.00"
    assert {t.gross_basis for t in ledger.trades} == {"fills"}


async def test_a_reentry_after_a_stop_reports_the_seconds_since_that_exit(db, audit):
    """2026-09-21 re-entered one strike higher within a minute of each stop."""
    await _trade1(audit)
    await _trade2(audit)

    first, second = _one_day(db).trades

    assert first.seconds_since_prev_exit == "", "nothing exited before the first entry"
    assert second.seconds_since_prev_exit == "6", "10:21:54 -> 10:22:00"


async def test_a_held_to_expiry_trade_with_no_spot_has_a_BLANK_gross_never_zero(
    db, audit
):
    await _trade1(audit, close=False)

    ledger = _one_day(db)
    [t] = ledger.trades

    assert t.outcome == "held_to_expiry"
    assert t.gross == "" and t.exit_time_et == "" and t.exit_debit == ""
    assert t.gross_basis == "no_spot", "the refusal names itself"
    assert t.max_loss == "870.00", "the risk taken is known even when the result is not"
    [d] = ledger.days
    assert d.total_gross == "", "a day with an unknown trade has an unknown result"
    assert d.gross == "130.00", "but the cash its fills moved is still known"
    assert d.held_to_expiry == 1


async def test_entry_steps_and_the_exit_trigger_come_from_their_own_records(db, audit):
    await _trade1(audit)
    audit._clock = lambda: _ts("10:00:08")
    await audit.record("entry.finished", {
        "candidate_id": T1, "outcome": "filled", "requested_contracts": 2,
        "filled_contracts": 2, "steps": 0, "final_limit_price": 0.65,
    })
    audit._clock = lambda: _ts("10:21:55")
    await audit.record("watchdog.exit_finished", {
        "outcome": "filled", "trigger_reasons": ["credit_multiple"],
        "position": {"candidate_id": T1}, "walk": {},
    })

    [t] = _one_day(db).trades

    assert (t.entry_steps, t.entry_limit) == ("0", "0.65")
    assert (t.exit_trigger, t.exit_outcome) == ("credit_multiple", "filled")


async def test_delta_source_and_version_are_joined_from_their_records(db, audit):
    audit._clock = lambda: "2026-09-21T13:25:00.000000Z"
    await audit.record("process.start", {"environment": "live", "version": "v0.4.6"})
    audit._clock = lambda: _ts("10:00:05")
    await audit.record("signal.candidate", {
        "side": "call", "delta_source": "mid",
        "short": {"strike": 7725.0}, "long": {"strike": 7730.0},
    })
    await _trade1(audit)

    [t] = _one_day(db).trades
    assert (t.delta_source, t.version) == ("mid", "v0.4.6")
    assert _one_day(db).days[0].version == "v0.4.6"


async def test_the_days_version_is_the_build_running_at_the_close(db, audit):
    """An evening deploy is the NEXT session's build (09-17 traded on v0.4.2 and
    deployed v0.4.3 at 17:12 ET)."""
    audit._clock = lambda: "2026-09-21T13:00:00.000000Z"
    await audit.record("process.start", {"environment": "live", "version": "v0.4.5"})
    await _trade1(audit)
    audit._clock = lambda: "2026-09-21T21:12:48.000000Z"   # 17:12 ET
    await audit.record("process.start", {"environment": "live", "version": "v0.4.6"})

    assert build(db, [DAY], CAL).days[0].version == "v0.4.5"


async def test_the_ledger_starts_at_the_first_session_the_process_was_up_for(db, audit):
    """The cutover was after 2026-09-15's close, so 09-15 never traded live."""
    audit._clock = lambda: "2026-09-15T20:33:18.000000Z"   # 16:33 ET, after the close
    await audit.record("process.start", {"environment": "live", "version": "v0.4.2"})
    assert first_session_date(db, CAL) == date(2026, 9, 16)


async def test_a_start_before_the_close_counts_that_session(db, audit):
    audit._clock = lambda: "2026-09-17T13:00:00.000000Z"   # 09:00 ET
    await audit.record("process.start", {"environment": "live", "version": "v0.4.2"})
    assert first_session_date(db, CAL) == date(2026, 9, 17)


# -- fills --------------------------------------------------------------------


async def test_fill_rows_carry_signed_cash_and_the_broker_order_id(db, audit):
    await _trade1(audit)

    fills = _one_day(db).fills

    assert [(f.buy_sell, f.strike, f.price, f.cash) for f in fills] == [
        ("SELL", "7725", "1.95", "390.00"),
        ("BUY", "7730", "1.30", "-260.00"),
        ("BUY", "7725", "4.73", "-946.00"),
        ("SELL", "7730", "3.23", "646.00"),
    ]
    assert {f.broker_order_id for f in fills} == {"507889144", "507922468"}
    assert sum(float(f.cash) for f in fills) == -170.00, "the fills add up to the gross"


async def test_buy_sell_follows_the_cash_not_the_brokers_relabelled_action(db, audit):
    """On a chained book an opening BUY can come back `Buy to Close` (2026-08-25)."""
    await _fill(audit, T1, direction="open", action="Sell to Open", strike=7725,
                price=1.95, at="10:00:07", order_id=1)
    await _fill(audit, T1, direction="open", action="Buy to Close", strike=7730,
                price=1.30, at="10:00:07", order_id=1)

    [short, long_] = _one_day(db).fills
    assert (long_.direction, long_.buy_sell) == ("open", "BUY")


# -- days ---------------------------------------------------------------------


async def test_a_day_with_no_trades_still_has_a_row_and_a_known_zero(db, audit):
    audit._clock = lambda: _ts("09:30:00")
    await audit.record("session.event_blackout", {})

    [d] = _one_day(db).days
    assert (d.trades, d.gross, d.blackout) == (0, "0.00", "yes")
    assert (d.total_gross, d.settled_gross) == ("0.00", "0.00")


async def test_argus_fees_are_carried_on_the_day_as_the_desk_reads_them(db, audit):
    await _trade1(audit)
    audit._clock = lambda: _ts("10:00:07")
    await audit.record("order.submitted", {"dry_run": False, "fees": -6.88, "order": {}})
    await audit.record("order.submitted", {"dry_run": False, "fees": -2.88, "order": {}})

    assert _one_day(db).days[0].argus_fees == "-9.76"


async def test_the_whole_range_builds_one_day_row_per_date_given(db, audit):
    await _trade1(audit)
    ledger = build(db, [date(2026, 9, 18), DAY], CAL)

    assert [d.trading_date for d in ledger.days] == ["2026-09-18", "2026-09-21"]
    assert first_record_date(db) == DAY


# -- output -------------------------------------------------------------------


async def test_the_csvs_have_a_header_from_the_row_type_and_one_line_per_row(
    db, audit, tmp_path
):
    await _trade1(audit)
    write_ledger(tmp_path, _one_day(db))

    for name, row_type, rows in (
        ("argus_fills.csv", FillRow, 4),
        ("trades.csv", TradeRow, 1),
        ("days.csv", DayRow, 1),
    ):
        with (tmp_path / name).open() as fh:
            lines = list(csv.reader(fh))
        assert lines[0] == [f for f in row_type.__dataclass_fields__]
        assert len(lines) == 1 + rows
    assert not list(tmp_path.glob("*.tmp")), "written atomically, no temp file left"


def test_money_and_price_formatting():
    assert _money(None) == "" and _money(-0.001) == "0.00" and _money(-170) == "-170.00"
    assert _price(1.5) == "1.50" and _price(0.525) == "0.525" and _price(None) == ""


# -- rails --------------------------------------------------------------------


def test_the_ledger_imports_nothing_that_can_reach_an_order():
    """A reader of the ledger must never be one import away from a submission."""
    import ast
    from pathlib import Path

    import argus.tools.ledger as ledger

    source = Path(ledger.__file__).read_text()
    tree = ast.parse(source)
    imported = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    } | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    forbidden = {
        "argus.execution.pipeline", "argus.execution.entry", "argus.execution.loop",
        "argus.execution.watchdog", "argus.execution.broker", "argus.pipeline",
    }
    assert not imported & forbidden
    assert "place_order" not in source
