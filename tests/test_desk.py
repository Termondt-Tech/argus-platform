"""The desk view (`argus.tools.desk`) — read-only reporting, no new arithmetic.

The point of this tool is that it reuses `shadow.marker.read_executions` rather
than recomputing P&L from the ledger a second way, so the tests that matter most
here are the ones pinning that: the desk's realized number and `tools.mark`'s
realized number come from the same fills and must agree exactly.

Payloads are built BY HAND, matching what the production writers emit, for the
same reason `test_shadow_live_outcomes.py` does: a fixture built by the production
writer cannot catch the production writer changing shape.

The numbers are the real 2026-08-13 ones — `20260813-call-7825-7830`, 2 contracts,
opened at 0.55 credit, closed at 1.20 debit: realized -$130.
"""

from __future__ import annotations

from datetime import date

import pytest

from argus.tools.desk import load

TRADING_DATE = date(2026, 8, 13)
OPENED_AT = "2026-08-13T14:00:08.900000Z"     # 10:00 ET
MARKED_AT = "2026-08-13T18:30:00.000000Z"     # 14:30 ET
CLOSED_AT = "2026-08-13T14:21:58.500000Z"     # 10:21 ET

CANDIDATE = "20260813-call-7825-7830"


async def _fill(audit, *, direction, action, symbol, contracts, price, ts):
    """One `order.fill` — one leg's movement.

    `candidate_id` is NULL exactly as in production: it does not round-trip through
    the broker, so attribution comes from `external_identifier`.
    """
    audit._clock = lambda: ts
    await audit.record(
        "order.fill",
        {
            "broker_order_id": 1311951 if direction == "open" else 1311952,
            "external_identifier": f"argus-{direction}-{CANDIDATE}",
            "symbol": symbol,
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


async def _open_position(audit, *, contracts=2):
    """Sell the 7825 at 2.00, buy the 7830 at 1.45 -> 0.55 net credit."""
    await _fill(audit, direction="open", action="Sell to Open",
                symbol="SPXW  260813C07825000", contracts=contracts, price=2.00,
                ts=OPENED_AT)
    await _fill(audit, direction="open", action="Buy to Open",
                symbol="SPXW  260813C07830000", contracts=contracts, price=1.45,
                ts=OPENED_AT)


async def _close_position(audit, *, contracts=2):
    """Buy the 7825 back at 4.20, sell the 7830 at 3.00 -> 1.20 net debit."""
    await _fill(audit, direction="close", action="Buy to Close",
                symbol="SPXW  260813C07825000", contracts=contracts, price=4.20,
                ts=CLOSED_AT)
    await _fill(audit, direction="close", action="Sell to Close",
                symbol="SPXW  260813C07830000", contracts=contracts, price=3.00,
                ts=CLOSED_AT)


async def _mark(audit, *, cost_to_close, ts=MARKED_AT, unknown=(), contracts=2):
    """One `watchdog.check`. Tier is irrelevant here — the tool keys on category."""
    audit._clock = lambda: ts
    await audit.record(
        "watchdog.check",
        {
            "candidate_id": CANDIDATE,
            "external_identifier": f"argus-open-{CANDIDATE}",
            "contracts": contracts,
            "entry_credit": 0.55,
            "cost_to_close": cost_to_close,
            "credit_stop": 1.10,
            "problems": [],
            "spot": 7799.63,
            "sustaining": {},
            "true_reasons": [],
            "unknown_reasons": list(unknown),
        },
    )


# -- realized: closed intraday, from actual fills -----------------------------


async def test_a_position_closed_intraday_reports_realized_pnl_from_FILLS(db, audit):
    await _open_position(audit)
    await _close_position(audit)

    day = load(db, TRADING_DATE)
    ex = day.executions[CANDIDATE]

    assert ex.entry_credit == pytest.approx(0.55), "net of both opening legs"
    assert ex.exit_debit == pytest.approx(1.20), "net of both closing legs"
    assert ex.closed_contracts == 2
    assert ex.expired_contracts == 0, "nothing left held"
    # (0.55 - 1.20) * 2 * 100 = -130.00, the real 2026-08-13 number.
    assert round((ex.entry_credit - ex.exit_debit) * ex.closed_contracts * 100, 2) == -130.00


async def test_the_desk_and_tools_mark_agree_on_a_closed_position(db, audit):
    """The cross-check that justifies the tool existing alongside `tools.mark`.

    Both read the SAME fills through the SAME reconstruction, so a disagreement
    would mean one of them grew its own arithmetic — which is the thing this
    tool was built specifically not to do.
    """
    from argus.shadow.marker import read_executions

    await _open_position(audit)
    await _close_position(audit)

    desk = load(db, TRADING_DATE).executions[CANDIDATE]
    marker = read_executions(db, TRADING_DATE)[CANDIDATE]
    assert desk == marker


# -- unrealized: still held, at the watchdog's own last look ------------------


async def test_a_held_position_is_marked_at_the_watchdogs_LAST_look(db, audit):
    await _open_position(audit)
    await _mark(audit, cost_to_close=0.90, ts="2026-08-13T18:00:00.000000Z")
    await _mark(audit, cost_to_close=0.30, ts=MARKED_AT)      # the latest one

    day = load(db, TRADING_DATE)
    assert day.executions[CANDIDATE].expired_contracts == 2, "still held"
    mark = day.marks[CANDIDATE]
    assert mark.cost_to_close == 0.30, "the last look, not the first"
    assert mark.at_et == "14:30:00", "reported in ET, as a desk reads it"
    # (0.55 - 0.30) * 2 * 100 = +50.00 unrealized
    assert round((0.55 - mark.cost_to_close) * 2 * 100, 2) == 50.00


async def test_a_tick_that_could_not_price_the_spread_does_not_clobber_the_last_mark(db, audit):
    """A stale price beats no price, and both beat rendering None as a number."""
    await _open_position(audit)
    await _mark(audit, cost_to_close=0.30, ts="2026-08-13T18:00:00.000000Z")
    audit._clock = lambda: MARKED_AT
    await audit.record(
        "watchdog.check",
        {"candidate_id": CANDIDATE, "contracts": 2, "cost_to_close": None,
         "unknown_reasons": ["credit_multiple"]},
    )

    mark = load(db, TRADING_DATE).marks[CANDIDATE]
    assert mark.cost_to_close == 0.30
    assert mark.at_et == "14:00:00", "and it still says WHEN that price was taken"


# -- a spent exit: marked, unacted-on, and it must not read as an ordinary hold ---
#
# The 2026-09-14 shape. One position, credit stop fired at 11:33:10 against a
# breached mark, every send lost at the transport layer, exit attempt spent at
# 11:34:56, then 10,656 `watchdog.check_spent` marks to 15:59:59 and a 15:30
# flatten that also failed. The report showed a quiet held row marked at 11:34:10
# and "nothing was closed before expiry".


async def _spent_mark(audit, *, cost_to_close, ts, outcome="nothing_at_broker", contracts=2):
    """One `watchdog.check_spent` — the mark of a position with no attempt left."""
    audit._clock = lambda: ts
    await audit.record(
        "watchdog.check_spent",
        {
            "candidate_id": CANDIDATE,
            "external_identifier": f"argus-open-{CANDIDATE}",
            "contracts": contracts,
            "entry_credit": 0.55,
            "cost_to_close": cost_to_close,
            "credit_stop": 1.10,
            "exit_outcome": outcome,
            "problems": [],
            "spot": 7799.63,
            "true_reasons": ["credit_multiple"],
            "unknown_reasons": [],
        },
    )


async def _exit_attempt(audit, *, outcome, ts, reason=None, reject_reason=None):
    """One `watchdog.exit_finished` — `ExitReport.to_payload()` plus the position."""
    audit._clock = lambda: ts
    await audit.record(
        "watchdog.exit_finished",
        {
            "outcome": outcome,
            "external_identifier": f"argus-close-{CANDIDATE}",
            "trigger_reasons": ["credit_multiple"],
            "walk": None if reject_reason is None else {"reject_reason": reject_reason},
            "cross_limit_price": None,
            "cross_broker_order_id": None,
            "reason": reason,
            "position": {"candidate_id": CANDIDATE, "contracts": 2},
        },
    )


async def test_a_spent_position_is_marked_at_its_LATEST_look_not_at_the_spend(db, audit):
    """The defect, in one test.

    `watchdog.check` stops the instant the exit is spent and `watchdog.check_spent`
    takes over, so reading the first alone freezes the mark at the exact moment the
    position started to matter — 4h25m and 10,656 marks stale on 2026-09-14.
    """
    await _open_position(audit)
    await _mark(audit, cost_to_close=1.60, ts="2026-08-13T15:34:10.000000Z")   # 11:34
    await _spent_mark(audit, cost_to_close=0.05, ts="2026-08-13T19:59:59.000000Z")  # 15:59

    mark = load(db, TRADING_DATE).marks[CANDIDATE]
    assert mark.cost_to_close == 0.05, "the latest look, from EITHER mark category"
    assert mark.at_et == "15:59:59"
    assert mark.exit_outcome == "nothing_at_broker", "and it says the attempt is used"


async def test_the_spent_state_survives_a_last_look_that_could_not_price(db, audit):
    """`exit_outcome` is read independently of the price guard.

    Whether a position has an exit attempt left is not a question about quotes, so
    a final run of unpriceable ticks must not take the answer down with it.
    """
    await _open_position(audit)
    await _spent_mark(audit, cost_to_close=1.60, ts="2026-08-13T15:34:10.000000Z")
    await _spent_mark(audit, cost_to_close=None, ts="2026-08-13T19:59:59.000000Z")

    mark = load(db, TRADING_DATE).marks[CANDIDATE]
    assert mark.cost_to_close == 1.60, "the stale price beats no price"
    assert mark.at_et == "11:34:10"
    assert mark.exit_outcome == "nothing_at_broker"


async def test_the_spent_state_survives_when_the_last_PRICED_look_was_an_ordinary_check(db, audit):
    """The asymmetric case: the last priced tick predates the spend.

    Hanging `exit_outcome` off the mark record would answer `None` here, which is
    the reassuring-when-wrong reading — an ordinary hold.
    """
    await _open_position(audit)
    await _mark(audit, cost_to_close=1.60, ts="2026-08-13T15:34:10.000000Z")
    await _spent_mark(audit, cost_to_close=None, ts="2026-08-13T19:59:59.000000Z")

    mark = load(db, TRADING_DATE).marks[CANDIDATE]
    assert mark.cost_to_close == 1.60
    assert mark.exit_outcome == "nothing_at_broker"


async def test_an_ordinary_held_position_carries_no_spent_state(db, audit):
    await _open_position(audit)
    await _mark(audit, cost_to_close=0.30)

    assert load(db, TRADING_DATE).marks[CANDIDATE].exit_outcome is None


async def test_every_exit_attempt_is_kept_not_just_the_last(db, audit):
    """A position gets an ordinary attempt and, past the flatten mark, one more."""
    await _open_position(audit)
    await _exit_attempt(audit, outcome="nothing_at_broker", ts="2026-08-13T15:34:56.000000Z",
                        reason="submit failed")
    await _exit_attempt(audit, outcome="not_resolved", ts="2026-08-13T19:30:31.000000Z",
                        reason="flatten: submit failed",
                        reject_reason="TastytradeError: timeout_error: Request Timed Out")

    attempts = load(db, TRADING_DATE).exits[CANDIDATE]
    assert [a.outcome for a in attempts] == ["nothing_at_broker", "not_resolved"]
    assert [a.at_et for a in attempts] == ["11:34:56", "15:30:31"]
    assert attempts[0].reject_reason is None, "no walk, no broker prose — not an empty string"
    assert "Request Timed Out" in (attempts[1].reject_reason or "")
    assert attempts[1].trigger_reasons == ("credit_multiple",)


async def test_a_held_position_whose_exits_all_FAILED_is_reported_as_such(db, audit, capsys):
    """End to end, the 2026-09-14 shape: the report must not read as a quiet hold."""
    from argus.tools.desk import _report

    await _open_position(audit)
    await _mark(audit, cost_to_close=1.60, ts="2026-08-13T15:34:10.000000Z")
    await _exit_attempt(audit, outcome="nothing_at_broker", ts="2026-08-13T15:34:56.000000Z",
                        reason="submit failed")
    await _spent_mark(audit, cost_to_close=0.05, ts="2026-08-13T19:59:59.000000Z")
    await _exit_attempt(audit, outcome="not_resolved", ts="2026-08-13T19:30:31.000000Z",
                        reason="flatten: submit failed")

    _report(load(db, TRADING_DATE), "test.db")
    out = capsys.readouterr().out

    assert "EXIT SPENT (nothing_at_broker)" in out, "on the row an operator scans"
    assert "MANUAL action" in out, "and what to do about it"
    assert "EXITS THAT DID NOT CLOSE THE POSITION" in out
    assert "nothing_at_broker" in out and "not_resolved" in out, "both attempts, not the last"
    assert "flatten: submit failed" in out, "with the reason, below the table"


async def test_a_closed_position_reports_its_LAST_exit_outcome_and_no_failure_section(db, audit, capsys):
    """The closed path keeps today's behaviour: the outcome on the row, no section."""
    from argus.tools.desk import _report

    await _open_position(audit)
    await _close_position(audit)
    await _exit_attempt(audit, outcome="filled", ts=CLOSED_AT)

    day = load(db, TRADING_DATE)
    _report(day, "test.db")
    out = capsys.readouterr().out

    assert day.exits[CANDIDATE][-1].outcome == "filled"
    assert "EXITS THAT DID NOT CLOSE THE POSITION" not in out
    assert "EXIT SPENT" not in out


async def test_a_partly_closed_position_reports_both_halves(db, audit):
    """One contract closed, one still held — realized and unrealized at once."""
    await _open_position(audit, contracts=2)
    await _close_position(audit, contracts=1)
    await _mark(audit, cost_to_close=0.30)

    day = load(db, TRADING_DATE)
    ex = day.executions[CANDIDATE]
    assert ex.closed_contracts == 1
    assert ex.expired_contracts == 1


async def test_an_unmarked_held_position_is_reported_not_silently_valued(db, audit):
    """No mark means no basis. It must appear, and contribute nothing."""
    await _open_position(audit)

    day = load(db, TRADING_DATE)
    assert day.executions[CANDIDATE].expired_contracts == 2
    assert CANDIDATE not in day.marks


# -- the rest of the day ------------------------------------------------------


async def test_fees_sum_across_orders_and_skip_dry_runs(db, audit):
    audit._clock = lambda: OPENED_AT
    await audit.record("order.submitted", {"dry_run": False, "fees": -7.09, "order": {}})
    await audit.record("order.submitted", {"dry_run": False, "fees": -3.09, "order": {}})
    await audit.record("order.submitted", {"dry_run": True, "fees": -99.00, "order": {}})

    assert round(load(db, TRADING_DATE).fees, 2) == -10.18


async def test_gate_decisions_split_into_allowed_and_denials_by_reason(db, audit):
    audit._clock = lambda: OPENED_AT
    await audit.record("gate.decision", {"allowed": True, "reasons": []})
    for _ in range(3):
        await audit.record("gate.decision", {"allowed": False, "reasons": ["concurrent_limit"]})
    await audit.record(
        "gate.decision", {"allowed": False, "reasons": ["concurrent_limit", "stale_market_data"]}
    )

    day = load(db, TRADING_DATE)
    assert day.allowed == 1
    assert day.denials["concurrent_limit"] == 4
    assert day.denials["stale_market_data"] == 1


async def test_entry_quality_carries_the_reprice_count(db, audit):
    """The closest thing this system has to an "adjustment": how much the walk had
    to chase to get filled."""
    audit._clock = lambda: OPENED_AT
    await audit.record(
        "entry.finished",
        {"candidate_id": CANDIDATE, "outcome": "filled", "requested_contracts": 2,
         "filled_contracts": 2, "steps": 3, "final_limit_price": 0.55},
    )

    (entry,) = load(db, TRADING_DATE).entries
    assert entry.steps == 3
    assert entry.filled_contracts == 2
    assert entry.at_et == "10:00:08"


async def test_desk_names_why_the_broker_refused(db, audit, capsys):
    """S5. The surface the gap was SEEN on. (2026-09-04)

    2026-09-03's table was five identical `no_order_exists` rows with nothing
    separating a permanent instrument refusal from a transient 502 — 71% of the
    day's entry attempts, unexplained. The reason now prints below the table.
    """
    from argus.tools.desk import _report

    audit._clock = lambda: OPENED_AT
    await audit.record(
        "entry.finished",
        {"candidate_id": CANDIDATE, "outcome": "no_order_exists",
         "requested_contracts": 2, "filled_contracts": None, "steps": 0,
         "final_limit_price": 0.55,
         "mutation_error": "TastytradeError: instrument_validation_failed: Trading of "
                         "SPXW 260903P05000000 is not supported"},
    )

    (entry,) = load(db, TRADING_DATE).entries
    assert entry.mutation_error and "instrument_validation_failed" in entry.mutation_error

    _report(load(db, TRADING_DATE), "test.db")
    out = capsys.readouterr().out
    assert "WHY THE BROKER REFUSED" in out
    assert "SPXW 260903P05000000 is not supported" in out


async def test_a_row_written_before_the_field_existed_still_reads(db, audit, capsys):
    """S6. Every `entry.finished` before 2026-09-04 lacks the key, and a day that
    spans the deploy must render — with the section absent, not with a `None` in it.
    """
    from argus.tools.desk import _report

    audit._clock = lambda: OPENED_AT
    await audit.record(
        "entry.finished",
        {"candidate_id": CANDIDATE, "outcome": "no_order_exists",
         "requested_contracts": 2, "filled_contracts": None, "steps": 0,
         "final_limit_price": 0.55},
    )

    (entry,) = load(db, TRADING_DATE).entries
    assert entry.mutation_error is None

    _report(load(db, TRADING_DATE), "test.db")
    out = capsys.readouterr().out
    assert "EXECUTION QUALITY" in out
    assert "WHY THE BROKER REFUSED" not in out


async def test_a_date_with_no_records_is_reported_not_crashed(db, audit):
    day = load(db, date(2026, 1, 2))
    assert day.session_span is None
    assert day.executions == {}


async def test_the_report_renders_without_raising(db, audit, capsys):
    """Smoke: every branch of the printer over a day holding both shapes."""
    from argus.tools.desk import _report

    await _open_position(audit, contracts=2)
    await _close_position(audit, contracts=1)
    await _mark(audit, cost_to_close=0.30, unknown=["credit_multiple"])
    audit._clock = lambda: OPENED_AT
    await audit.record(
        "entry.finished",
        {"candidate_id": CANDIDATE, "outcome": "filled", "requested_contracts": 2,
         "filled_contracts": 2, "steps": 1, "final_limit_price": 0.55},
    )
    await audit.record("gate.decision", {"allowed": False, "reasons": ["concurrent_limit"]})

    _report(load(db, TRADING_DATE), "test.db")
    out = capsys.readouterr().out
    assert "OPEN / HELD" in out
    assert "CLOSED INTRADAY" in out
    assert "EXECUTION QUALITY" in out
    # The settlement caveat must appear whenever anything is still held.
    assert "tools.mark" in out
