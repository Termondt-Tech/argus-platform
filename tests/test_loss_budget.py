"""Loss-budget sizing: `daily_max_loss` bounds the whole day. (2026-09-21)

→ docs/decisions/2026-09-21-loss-budget-sizing.md

The invariant, W = realized day loss + Σ remaining worst case of everything open,
must satisfy `W ≤ daily_max_loss` after EVERY event of the day. It is tested
directly (a seeded randomized property over entries, overshooting stops, full-width
failures and a lagging scan), with a mutation check on each of its two enforcement
points, plus the 2026-09-21 replay, the restart, and every fail-closed path.
"""

from __future__ import annotations

import ast
import json
import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import argus.orchestrator as orchestrator_module
import argus.risk.gate as gate_module
from argus.audit.log import AuditLog
from argus.clock import et_today
from argus.execution.fills import FillConsumer
from argus.execution.models import (
    LegAction,
    LiveOrders,
    Order,
    OrderDirection,
    OrderLeg,
    UnmappableOrder,
)
from argus.execution.store import AppliedFill, InMemoryOrderStore, OpenSpread, OpenSpreadScan
from argus.execution.working_orders import open_working_worst_case
from argus.orchestrator import size_contracts
from argus.risk.breaker import DailyLossBreaker
from argus.risk.gate import BUDGET_EXHAUSTED_CATEGORY, BUDGET_REASON, PreTradeGate
from argus.risk.killswitch import KillSwitch
from argus.risk.loss_budget import (
    DayLedger,
    Exposure,
    LedgerFill,
    LossBudget,
    LossBudgetSource,
    compute_loss_budget,
    contracts_within_budget,
    exceeds_budget,
    trade_idea_key,
)
from argus.risk.models import EntryProposal, RiskSnapshot
from argus.risk.rehydrate import FILL_CATEGORY, load_day_risk_state
from support import make_config

CAP = 950.0


def occ(strike: float, right: str = "C") -> str:
    return f"SPXW  260921{right}{int(round(strike * 1000)):08d}"


def ident(idea: str, direction: str = "open") -> str:
    return f"argus-{direction}-{idea}"


def open_fills(idea: str, short: float, long_: float, contracts: int, credit: float,
               right: str = "C") -> list[LedgerFill]:
    """An opening vertical: short leg sold, long leg bought, netting `credit`."""
    long_price = 1.00
    return [
        LedgerFill(ident(idea), occ(short, right), -contracts, round(long_price + credit, 2)),
        LedgerFill(ident(idea), occ(long_, right), +contracts, long_price),
    ]


def close_fills(idea: str, short: float, long_: float, contracts: int, debit: float,
                right: str = "C") -> list[LedgerFill]:
    """A closing vertical: short leg bought back, long leg sold, paying `debit`."""
    long_price = 0.50
    return [
        LedgerFill(ident(idea, "close"), occ(short, right), +contracts,
                   round(long_price + debit, 2)),
        LedgerFill(ident(idea, "close"), occ(long_, right), -contracts, long_price),
    ]


def budget(ledger: DayLedger, *, scan: Exposure | None = None,
           working: Exposure | None = None, pnl: float = 0.0,
           anchored: bool | None = True, live: bool = False, cap: float = CAP) -> LossBudget:
    return compute_loss_budget(
        daily_max_loss=cap,
        cash_today=ledger.cash_today,
        ledger_complete=ledger.cash_complete,
        fill_exposure=ledger.exposure(),
        scan_exposure=scan if scan is not None else Exposure.none_open(),
        working_exposure=working if working is not None else Exposure.none_open(),
        daily_pnl=pnl,
        daily_pnl_anchored=anchored,
        broker_pnl_is_real=live,
    )


# -- the day ledger ------------------------------------------------------------


def test_cash_is_the_premium_the_fills_moved_across_every_rung():
    # 2026-09-01's two rungs: 1 contract at 2.05/1.55 and 1 at 2.15/1.65.
    idea = "20260901-put-7615-7610"
    ledger = DayLedger([
        LedgerFill(ident(idea), occ(7615, "P"), -1, 2.05),
        LedgerFill(ident(idea), occ(7610, "P"), +1, 1.55),
        LedgerFill(ident(idea), occ(7615, "P"), -1, 2.15),
        LedgerFill(ident(idea), occ(7610, "P"), +1, 1.65),
    ])
    assert ledger.cash_today == 100.00          # 0.50 × 2 × 100, exact
    assert ledger.cash_complete is True
    assert ledger.exposure().by_idea == {idea: 1000.0}   # 5 wide × 2 × 100


def test_a_closed_idea_holds_nothing_and_its_cash_is_the_realized_loss():
    idea = "call-7725-7730"
    ledger = DayLedger(open_fills(idea, 7725, 7730, 2, 0.65)
                       + close_fills(idea, 7725, 7730, 2, 1.50))
    assert ledger.cash_today == -170.00         # (0.65 − 1.50) × 200: the 2026-09-21 stop
    assert ledger.exposure().by_idea == {}
    b = budget(ledger)
    assert b.worst_case == 170.00 and b.remaining == CAP - 170.00


def test_chained_spreads_are_two_ideas_even_though_the_shared_strike_nets_to_zero():
    """The overlapping-verticals landmine: symbol-keyed, 7730 nets to 0 and the pair
    reads as ONE 10-wide. Per trade idea it is two 5-wides — 500 + 500."""
    ledger = DayLedger(open_fills("a", 7725, 7730, 1, 0.60)
                       + open_fills("b", 7730, 7735, 1, 0.40))
    assert ledger.exposure().by_idea == {"a": 500.0, "b": 500.0}


def test_a_half_booked_vertical_cannot_be_bounded_by_a_width():
    ledger = DayLedger(open_fills("a", 7725, 7730, 2, 0.65)[:1])     # short leg only
    exposure = ledger.exposure()
    assert exposure.by_idea is None and exposure.unknown == "fill_ledger_unpaired_legs"
    assert budget(ledger).remaining == 0.0


def test_an_unpriced_fill_leaves_the_cash_incomplete_but_the_legs_known():
    fills = open_fills("a", 7725, 7730, 1, 0.65)
    fills[0] = LedgerFill(fills[0].external_identifier, fills[0].symbol,
                          fills[0].signed_delta, None)
    ledger = DayLedger(fills)
    assert ledger.cash_complete is False
    assert ledger.exposure().by_idea == {"a": 500.0}


def test_an_unreadable_row_makes_the_ledger_say_it_cannot_say():
    ledger = DayLedger([None])
    assert ledger.cash_complete is False
    assert ledger.exposure().unknown == "fill_ledger_unreadable"


def test_a_fill_under_no_argus_identifier_cannot_be_paired():
    ledger = DayLedger([LedgerFill("hand-placed", occ(7725), -1, 1.0)])
    assert ledger.exposure().unknown == "fill_ledger_unattributed"


def test_the_payload_and_the_live_fill_decode_identically():
    applied = AppliedFill(
        broker_order_id=1, external_identifier=ident("a"), symbol=occ(7725),
        action=LegAction.SELL_TO_OPEN, contracts=2, signed_delta=-2, price=1.65,
        position_before=0, position_after=-2,
    )
    row = json.loads(json.dumps({**applied.to_payload(), "status": "Filled",
                                 "candidate_id": None}))
    assert LedgerFill.from_payload(row) == LedgerFill.from_applied(applied)


@pytest.mark.parametrize("payload", [
    {},
    {"external_identifier": "x", "symbol": "y", "signed_delta": "2", "price": 1.0},
    {"external_identifier": "x", "symbol": "y", "signed_delta": True, "price": 1.0},
    {"external_identifier": 3, "symbol": "y", "signed_delta": 1, "price": 1.0},
])
def test_a_malformed_payload_is_unreadable_never_guessed(payload):
    assert LedgerFill.from_payload(payload) is None


def test_trade_idea_key_joins_the_open_and_the_close():
    assert trade_idea_key("argus-open-x-1") == trade_idea_key("argus-close-x-1") == "x-1"
    assert trade_idea_key("not-ours") is None


# -- the combining rule ----------------------------------------------------------


def test_open_exposure_is_the_per_idea_union_with_the_larger_winning():
    """The fill ledger and the scan name overlapping ideas: counted once, at the
    larger worst case. An idea only one of them has seen still counts."""
    ledger = DayLedger(open_fills("a", 7725, 7730, 1, 0.50))     # a: 500, cash +50
    b = budget(ledger, scan=Exposure(by_idea={"a": 1000.0, "b": 500.0}))
    assert b.open_worst_case == 1500.0
    assert b.worst_case_ledger == 1500.0 - 50.0


def test_a_spread_that_just_filled_counts_before_the_scan_has_seen_it():
    """The race the union closes: cash already holds the new credit, the scan has
    not refreshed. Scan-only would read the credit as a GAIN and the budget as MORE
    than the cap."""
    ledger = DayLedger(open_fills("a", 7725, 7730, 2, 0.65))
    b = budget(ledger, scan=Exposure.none_open())
    assert b.worst_case == 1000.0 - 130.0
    assert b.remaining == CAP - 870.0


def test_a_scan_that_still_holds_a_closed_spread_does_not_reopen_it():
    """The union's lag in the OTHER direction (v0.4.6 review): right after an exit,
    the ledger already holds the closing debit while the scan still lists the spread
    for up to one watchdog pass. Counting both read W as realized loss PLUS the full
    width — a known budget of −230 on a day that had lost 180."""
    idea = "call-7725-7730"
    ledger = DayLedger(open_fills(idea, 7725, 7730, 2, 0.60)
                       + close_fills(idea, 7725, 7730, 2, 1.50))
    assert ledger.exposure().closed == frozenset({idea})
    stale = budget(ledger, scan=Exposure(by_idea={idea: 1000.0}))
    fresh = budget(ledger, scan=Exposure.none_open())
    assert stale == fresh
    assert stale.worst_case == 180.0 and stale.remaining == CAP - 180.0


def test_the_ledger_overrules_the_scan_only_for_ideas_it_watched_close():
    """An idea the ledger does not know (a prior day's, a lost row) still comes from
    the scan, and one it holds open still takes the larger of the two."""
    ledger = DayLedger(open_fills("a", 7725, 7730, 1, 0.50)
                       + open_fills("c", 7740, 7745, 1, 0.50)
                       + close_fills("c", 7740, 7745, 1, 0.20))
    b = budget(ledger, scan=Exposure(by_idea={"a": 1000.0, "b": 500.0, "c": 500.0}))
    assert b.open_worst_case == 1000.0 + 500.0      # a at the larger, b from the scan, c gone


def test_working_opening_orders_add_as_if_filled_at_zero_credit():
    b = budget(DayLedger(), working=Exposure(by_idea={"w": 500.0}))
    assert b.working_worst_case == 500.0 and b.remaining == CAP - 500.0


def test_the_broker_estimate_binds_when_it_is_worse_and_only_in_live():
    ledger = DayLedger(open_fills("a", 7725, 7730, 1, 0.65)
                       + close_fills("a", 7725, 7730, 1, 1.50))          # cash −85
    # Fees and a ledger short a row: the broker says −300 for the day.
    live = budget(ledger, pnl=-300.0, anchored=True, live=True)
    assert (live.worst_case_ledger, live.worst_case_broker) == (85.0, 300.0)
    assert live.binding == "broker" and live.remaining == CAP - 300.0
    # Cert: day P&L is structurally zero, so the broker estimate is never counted.
    cert = budget(ledger, pnl=-300.0, anchored=True, live=False)
    assert cert.worst_case_broker is None and cert.binding == "ledger"
    # Unanchored: a placeholder 0.0, never a reading.
    unanchored = budget(ledger, pnl=-300.0, anchored=False, live=True)
    assert unanchored.worst_case_broker is None and unanchored.remaining == CAP - 85.0


def test_the_ledger_binds_on_a_tie_and_when_it_is_worse():
    ledger = DayLedger(open_fills("a", 7725, 7730, 1, 0.65)
                       + close_fills("a", 7725, 7730, 1, 1.50))
    assert budget(ledger, pnl=-85.0, live=True).binding == "ledger"
    assert budget(ledger, pnl=-10.0, live=True).binding == "ledger"


def test_the_broker_estimate_counts_every_open_spread_at_full_width():
    """Deliberately conservative (see the module docstring): the NLV already carries
    each open spread's unrealized loss at the broker's mark, so the remaining worst
    case is measured at a zero mark rather than a watchdog mark from another instant."""
    ledger = DayLedger(open_fills("a", 7725, 7730, 1, 0.50))
    # Marked at 2.00: NLV says −150 unrealized.
    b = budget(ledger, pnl=-150.0, live=True)
    assert b.worst_case_broker == 150.0 + 500.0
    assert b.worst_case_ledger == 450.0 and b.binding == "broker"


# -- fail closed: the design record's table, row by row ----------------------------


def test_unanchored_uses_the_ledger_alone():
    ledger = DayLedger(close_fills("a", 7725, 7730, 1, 1.0))
    b = budget(ledger, pnl=0.0, anchored=False, live=True)
    assert b.binding == "ledger" and b.worst_case_broker is None


def test_an_unpriced_fill_falls_back_to_the_broker_when_anchored_in_live():
    fills = open_fills("a", 7725, 7730, 1, 0.65) + close_fills("a", 7725, 7730, 1, 1.50)
    fills[2] = LedgerFill(fills[2].external_identifier, fills[2].symbol,
                          fills[2].signed_delta, None)
    ledger = DayLedger(fills)
    b = budget(ledger, pnl=-90.0, anchored=True, live=True)
    assert b.ledger_complete is False and b.worst_case_ledger is None
    assert b.binding == "broker" and b.remaining == CAP - 90.0


@pytest.mark.parametrize("anchored,live", [(False, True), (True, False), (None, True)])
def test_an_unpriced_fill_with_no_broker_reading_is_zero(anchored, live):
    ledger = DayLedger([LedgerFill(ident("a", "close"), occ(7725), +1, None),
                        LedgerFill(ident("a", "close"), occ(7730), -1, 0.5)])
    ledger_fills_open = open_fills("a", 7725, 7730, 1, 0.65)
    for f in ledger_fills_open:
        ledger.note(f)
    b = budget(ledger, pnl=-90.0, anchored=anchored, live=live)
    assert b.remaining == 0.0 and b.unknown_reason == "no_complete_estimate"


@pytest.mark.parametrize("scan,reason", [
    (Exposure.cannot_say("unmanaged_position"), "unmanaged_position"),
    (Exposure.cannot_say("watchdog_not_scanned"), "watchdog_not_scanned"),
    (Exposure(by_idea={"a": float("nan")}), "exposure_not_finite"),
])
def test_an_unreadable_open_position_is_zero_budget(scan, reason):
    b = budget(DayLedger(), scan=scan)
    assert b.remaining == 0.0 and not b.known and b.unknown_reason == reason


def test_an_unreadable_working_order_is_zero_budget():
    b = budget(DayLedger(), working=Exposure.cannot_say("working_order_unmappable"))
    assert b.remaining == 0.0 and b.unknown_reason == "working_order_unmappable"


def test_the_fresh_budget_is_the_whole_cap():
    b = LossBudget.fresh(CAP)
    assert b.remaining == CAP and b.known


# -- sizer and gate: the two halves share one epsilon ------------------------------


def test_contracts_within_budget_fits_exactly_and_never_on_noise():
    assert contracts_within_budget(870.0, 435.0) == 2
    assert contracts_within_budget(869.99, 435.0) == 1
    # 0.1 + 0.2 style noise must not cost a lot that fits.
    assert contracts_within_budget(870.0, 435.00000000000006) == 2
    assert contracts_within_budget(float("nan"), 435.0) == 0
    assert contracts_within_budget(float("inf"), 435.0) == 0
    assert contracts_within_budget(-5.0, 435.0) == 0


def test_exceeds_budget_accepts_what_the_sizer_chose():
    for remaining in (870.0, 770.0, 435.0, 555.55):
        n = contracts_within_budget(remaining, 435.00000000000006)
        assert not exceeds_budget(n * 435.00000000000006, remaining)
    assert exceeds_budget(870.02, 870.0)
    assert exceeds_budget(1.0, float("nan"))
    assert exceeds_budget(1.0, 0.0)


def _sized(mlpc: float, loss_budget: float) -> int:
    return size_contracts(
        1_000_000.0, 1.0, mlpc,
        max_loss_per_position=CAP, max_contracts_per_position=10,
        loss_budget=loss_budget,
    )


def test_the_sizer_sizes_to_the_budget_and_keeps_the_one_lot_floor():
    assert _sized(435.0, CAP) == 2           # the per-position cap binds
    assert _sized(435.0, 770.0) == 1         # the budget binds
    assert _sized(435.0, 100.0) == 1         # floor: recorded, and the gate refuses
    assert _sized(435.0, float("nan")) == 1


def _snapshot(b: LossBudget, *, nlv: float = 1_000_000.0) -> RiskSnapshot:
    return RiskSnapshot(
        open_position_count=0, daily_pnl=0.0, account_nlv=nlv,
        market_data_age_s=0.0, account_stream_age_s=0.0, loss_budget=b,
    )


class _Audit:
    """In-memory audit: records both tiers, reads nothing."""

    def __init__(self) -> None:
        self.tier1: list[tuple[str, dict]] = []

    async def record(self, category, payload):
        json.dumps(payload, allow_nan=False)        # canonical hashing forbids NaN/inf
        self.tier1.append((category, payload))

    def telemetry(self, category, payload):
        return True


def _gate(audit, *, alerts=None, signal=None, **risk) -> PreTradeGate:
    cfg = make_config(risk={
        "daily_max_loss": CAP, "max_loss_per_position": CAP,
        "max_concurrent_positions": 100, "position_size_pct": 1.0,
        "max_contracts_per_position": 10, **risk,
    }, signal=signal or {})
    return PreTradeGate(
        config=cfg, audit=audit, killswitch=KillSwitch(audit),
        breaker=DailyLossBreaker(audit, daily_max_loss=CAP),
        alert=(lambda k, d: alerts.append((k, d))) if alerts is not None else None,
    )


def _budget_with(remaining: float) -> LossBudget:
    worst = CAP - remaining
    return LossBudget(
        daily_max_loss=CAP, remaining=remaining, worst_case=worst,
        worst_case_ledger=worst, worst_case_broker=None, cash_today=-worst,
        open_worst_case=0.0, working_worst_case=0.0, ledger_complete=True,
        binding="ledger",
    )


async def test_the_gate_refuses_a_proposal_the_budget_cannot_hold():
    audit = _Audit()
    gate = _gate(audit)
    refused = await gate.evaluate(EntryProposal("x", 2, 870.0), _snapshot(_budget_with(770.0)))
    allowed = await gate.evaluate(EntryProposal("x", 1, 435.0), _snapshot(_budget_with(770.0)))
    assert refused.reasons == (BUDGET_REASON,)
    assert allowed.allowed is True


async def test_an_unreadable_nlv_does_not_excuse_the_budget():
    gate = _gate(_Audit())
    decision = await gate.evaluate(
        EntryProposal("x", 1, 435.0), _snapshot(_budget_with(100.0), nlv=0.0)
    )
    assert {"invalid_account_nlv", BUDGET_REASON} <= set(decision.reasons)


async def test_the_decision_records_which_estimate_bound_and_by_how_much():
    audit = _Audit()
    await _gate(audit).evaluate(EntryProposal("x", 1, 435.0), _snapshot(_budget_with(770.0)))
    (_, payload), = [r for r in audit.tier1 if r[0] == "gate.decision"]
    block = payload["snapshot"]["loss_budget"]
    assert block["remaining"] == 770.0 and block["binding"] == "ledger"
    assert block["worst_case_ledger"] == 180.0 and block["worst_case_broker"] is None
    assert payload["limits"]["daily_max_loss"] == CAP


async def test_exhaustion_is_announced_once_per_gate_and_never_on_an_unreadable_budget():
    audit, alerts = _Audit(), []
    gate = _gate(audit, alerts=alerts)
    unknown = budget(DayLedger(), scan=Exposure.cannot_say("watchdog_not_scanned"))
    await gate.evaluate(EntryProposal("x", 1, 435.0), _snapshot(unknown))
    assert alerts == []                     # transient by nature: recorded, not paged
    for _ in range(3):
        await gate.evaluate(EntryProposal("x", 1, 435.0), _snapshot(_budget_with(390.0)))
    announced = [r for r in audit.tier1 if r[0] == BUDGET_EXHAUSTED_CATEGORY]
    assert len(announced) == 1 and [k for k, _ in alerts] == ["daily_loss_budget_exhausted"]
    assert announced[0][1]["loss_budget"]["remaining"] == 390.0


async def test_a_stale_scan_after_an_exit_does_not_spend_the_days_page():
    """The v0.4.6 review's finding, end to end: a 1-lot re-entry proposed inside the
    watchdog's post-exit refresh window fits the day's real budget, so it is not
    refused on the budget and the one-per-day page is still there for the real
    exhaustion."""
    audit, alerts = _Audit(), []
    gate = _gate(audit, alerts=alerts)
    idea = "call-7725-7730"
    ledger = DayLedger(open_fills(idea, 7725, 7730, 2, 0.60)
                       + close_fills(idea, 7725, 7730, 2, 1.50))
    stale = budget(ledger, scan=Exposure(by_idea={idea: 1000.0}))
    decision = await gate.evaluate(EntryProposal("x", 1, 435.0), _snapshot(stale))
    assert BUDGET_REASON not in decision.reasons
    assert alerts == []
    assert not [r for r in audit.tier1 if r[0] == BUDGET_EXHAUSTED_CATEGORY]


async def test_a_budget_committed_to_an_open_position_does_not_page():
    """The 2026-09-22 10:00 ET page, verbatim: a 2-lot 5-wide at 0.50 was open
    (cash 100, worst case 1000), and the next candidate was refused on the budget —
    correctly — and paged "exhausted", wrongly. Flat, 950 would hold that lot."""
    audit, alerts = _Audit(), []
    gate = _gate(audit, alerts=alerts)
    committed = LossBudget(
        daily_max_loss=CAP, remaining=-50.0, worst_case=1000.0,
        worst_case_ledger=900.0, worst_case_broker=1000.0, cash_today=100.0,
        open_worst_case=1000.0, working_worst_case=0.0, ledger_complete=True,
        binding="broker",
    )
    decision = await gate.evaluate(
        EntryProposal("SPXW put credit spread 7740/7735", 1, 447.49999999999994),
        _snapshot(committed),
    )
    assert BUDGET_REASON in decision.reasons        # the refusal itself is right
    assert alerts == []
    assert not [r for r in audit.tier1 if r[0] == BUDGET_EXHAUSTED_CATEGORY]


async def test_a_day_whose_losses_leave_no_lot_even_flat_still_pages():
    """The page the alert exists for: realized −560, nothing open, 390 left, and a
    435 lot. Also holds with a position open, as long as closing it would not help."""
    for open_worst in (0.0, 500.0):
        audit, alerts = _Audit(), []
        gate = _gate(audit, alerts=alerts)
        b = LossBudget(
            daily_max_loss=CAP, remaining=390.0 - open_worst,
            worst_case=560.0 + open_worst, worst_case_ledger=560.0 + open_worst,
            worst_case_broker=None, cash_today=-560.0, open_worst_case=open_worst,
            working_worst_case=0.0, ledger_complete=True, binding="ledger",
        )
        await gate.evaluate(EntryProposal("x", 1, 435.0), _snapshot(b))
        assert [k for k, _ in alerts] == ["daily_loss_budget_exhausted"], open_worst


#: `argus.live.toml`'s `[signal]` on 2026-09-24: an ordinary lot is (5 − 0.50) × 100.
_LIVE_SIGNAL = {"width": 5.0, "max_width": 10.0, "min_credit": 0.50}


def _budget_2026_09_24(*, cash_today: float, open_worst: float, broker: float) -> LossBudget:
    worst = max(open_worst - cash_today, broker)
    return LossBudget(
        daily_max_loss=CAP, remaining=CAP - worst, worst_case=worst,
        worst_case_ledger=open_worst - cash_today, worst_case_broker=broker,
        cash_today=cash_today, open_worst_case=open_worst, working_worst_case=0.0,
        ledger_complete=True, binding="broker" if broker >= open_worst - cash_today else "ledger",
    )


async def test_2026_09_24_a_ladder_gap_candidate_does_not_spend_the_days_page():
    """seq 159828–159830, verbatim. The day had realized −120 and held put 7625/7620
    at 1 lot (credit 50, worst case 500), so 366.8 remained on the broker estimate.
    For one tick the long leg skipped 7675, and the candidate came out 10 wide at
    0.775: a 922.5 lot. Flat, 866.8 would not hold that lot, which is why it paged
    "exhausted" before this fix. But the same 866.8 holds an ordinary 5-wide lot
    (450) almost twice over, so entries had not ended. The refusal stays; the page
    goes, and is still there for the real exhaustion later."""
    audit, alerts = _Audit(), []
    gate = _gate(audit, alerts=alerts, signal=_LIVE_SIGNAL)
    held = _budget_2026_09_24(cash_today=-70.0, open_worst=500.0, broker=583.2)
    assert held.remaining == pytest.approx(366.8)
    flat = held.remaining + held.open_worst_case
    assert exceeds_budget(922.5, flat)          # the rule as it was would have paged

    decision = await gate.evaluate(
        EntryProposal("SPXW put credit spread 7680/7670", 1, 922.5), _snapshot(held)
    )
    assert BUDGET_REASON in decision.reasons    # the refusal itself is right
    assert alerts == []
    assert not [r for r in audit.tier1 if r[0] == BUDGET_EXHAUSTED_CATEGORY]

    # Later the held spread goes to full width: realized −570, nothing open, ~367
    # left. An ordinary lot no longer fits, and the day's page is still unspent.
    spent = _budget_2026_09_24(cash_today=-570.0, open_worst=0.0, broker=583.2)
    await gate.evaluate(
        EntryProposal("SPXW put credit spread 7685/7680", 1, 450.0), _snapshot(spent)
    )
    assert [k for k, _ in alerts] == ["daily_loss_budget_exhausted"]


async def test_a_wide_candidate_still_pages_when_no_ordinary_lot_fits_either():
    """The ordinary lot only ever NARROWS the page: once an ordinary lot does not fit
    flat either, a `max_width` candidate pages like any other."""
    audit, alerts = _Audit(), []
    gate = _gate(audit, alerts=alerts, signal=_LIVE_SIGNAL)
    await gate.evaluate(
        EntryProposal("SPXW put credit spread 7680/7670", 1, 922.5), _snapshot(_budget_with(400.0))
    )
    assert [k for k, _ in alerts] == ["daily_loss_budget_exhausted"]


async def test_the_page_asks_about_one_lot_not_the_whole_proposal():
    """A 2-lot proposal the budget refuses, where one of its lots fits flat, has not
    ended the day's entries: the sizer will offer the one lot."""
    audit, alerts = _Audit(), []
    gate = _gate(audit, alerts=alerts, signal=_LIVE_SIGNAL)
    decision = await gate.evaluate(EntryProposal("x", 2, 870.0), _snapshot(_budget_with(500.0)))
    assert BUDGET_REASON in decision.reasons
    assert alerts == []


async def test_exhaustion_never_trips_the_kill_switch():
    """A trip is durable since 2026-09-01: it would turn a daily limit permanent."""
    audit = _Audit()
    gate = _gate(audit)
    await gate.evaluate(EntryProposal("x", 1, 435.0), _snapshot(_budget_with(0.0)))
    assert gate._killswitch.engaged is False
    assert not any(c.startswith("kill_switch") for c, _ in audit.tier1)


# -- 2026-09-21, replayed ---------------------------------------------------------


async def test_2026_09_21_replay():
    """The design record's table: five call spreads in a rising market, each stopped
    out, sized through the real sizer and gated by the real gate. Per-contract max
    loss is (5 − 0.65) × 100 = 435 throughout."""
    audit = _Audit()
    gate = _gate(audit, max_contracts_per_position=2)
    ledger = DayLedger()
    source = LossBudgetSource(daily_max_loss=CAP, ledger=ledger, broker_pnl_is_real=False)
    mlpc = 435.0
    # (per-contract stop debit, expected W before entry ≈, expected contracts)
    steps = [
        (1.55, 0.0, 2),      # trade 1: 2 × 435 = 870 ≤ 950; stop realizes −180
        (1.45, 180.0, 1),    # 2 × 435 = 870 > 770: ONE lot; −80
        (1.35, 260.0, 1),    # −70
        (1.30, 330.0, 1),    # −65
        (1.50, 395.0, 1),    # −85
        (1.45, 480.0, 1),    # −80
    ]
    for n, (debit, w_before, expected) in enumerate(steps):
        b = source.evaluate(daily_pnl=0.0, daily_pnl_anchored=True)
        assert b.worst_case == pytest.approx(w_before, abs=0.01)
        contracts = size_contracts(
            1_000_000.0, 1.0, mlpc, max_loss_per_position=CAP,
            max_contracts_per_position=2, loss_budget=b.remaining,
        )
        decision = await gate.evaluate(
            EntryProposal("x", contracts, contracts * mlpc), _snapshot(b)
        )
        assert (contracts, decision.allowed) == (expected, True), n
        idea = f"call-{n}"
        for f in open_fills(idea, 7725 + 10 * n, 7730 + 10 * n, contracts, 0.65):
            ledger.note(f)
        for f in close_fills(idea, 7725 + 10 * n, 7730 + 10 * n, contracts, debit):
            ledger.note(f)
    # After the sixth stop: W = 560, 390 left, and not one lot fits. Entries end.
    b = source.evaluate(daily_pnl=0.0, daily_pnl_anchored=True)
    assert b.worst_case == pytest.approx(560.0, abs=0.01) and b.remaining == 390.0
    decision = await gate.evaluate(EntryProposal("x", 1, mlpc), _snapshot(b))
    assert decision.reasons == (BUDGET_REASON,)


# -- the invariant, directly ------------------------------------------------------


@dataclass
class _Held:
    idea: str
    short: float
    long_: float
    contracts: int
    credit: float
    mark: float

    @property
    def width(self) -> float:
        return abs(self.long_ - self.short)

    @property
    def worst(self) -> float:
        return (self.width - self.credit) * 100 * self.contracts


async def _run_day(rng: random.Random, *, hostile_sizer: bool, live: bool,
                   steps: int = 40) -> tuple[list[str], list[str]]:
    """One simulated day. Returns (invariant violations, sizer-fit violations).

    The model is independent of the code under test: it tracks the TRUE realized
    P&L and each open spread's true worst case, and asserts W ≤ cap after every
    event. The code under test sees only what production sees — its own fills, a
    scan that may lag the newest position, and (live) a broker P&L with fees.
    """
    audit = _Audit()
    gate = _gate(audit)
    ledger = DayLedger()
    held: list[_Held] = []
    scan_lag: set[str] = set()
    #: Spreads already closed that the scan still lists — the post-exit refresh
    #: window, the lag in the direction that OVERSTATES W (v0.4.6 review).
    scan_stale: dict[str, float] = {}
    realized = 0.0
    fees = 0.0
    source = LossBudgetSource(
        daily_max_loss=CAP, ledger=ledger, broker_pnl_is_real=live,
        scan_exposure=lambda: Exposure(by_idea={
            **scan_stale,
            **{h.idea: h.width * 100 * h.contracts for h in held if h.idea not in scan_lag},
        }),
    )
    violations: list[str] = []
    sizer_misfits: list[str] = []
    n_ideas = 0

    def w_true() -> float:
        return -realized + sum(h.worst for h in held)

    for step in range(steps):
        action = rng.random()
        if action < 0.45 or not held:
            width = rng.choice((5.0, 5.0, 10.0))
            credit = round(rng.uniform(0.30, min(2.50, width - 0.5)) / 0.05) * 0.05
            mlpc = (width - credit) * 100
            pnl = realized + sum((h.credit - h.mark) * 100 * h.contracts for h in held) - fees
            b = source.evaluate(daily_pnl=pnl, daily_pnl_anchored=True)
            if not live and b.worst_case != pytest.approx(w_true(), abs=0.01):
                # Cert has no fees and no broker estimate, so the ledger's W is the
                # TRUE W exactly — any stale scan, either direction, must not move it.
                # An overstated W here is a false refusal and a false page.
                violations.append(f"step {step}: ledger W {b.worst_case} != {w_true():.2f}")
            contracts = (
                rng.randint(1, 4) if hostile_sizer
                else size_contracts(
                    1_000_000.0, 1.0, mlpc, max_loss_per_position=CAP,
                    max_contracts_per_position=10, loss_budget=b.remaining,
                )
            )
            decision = await gate.evaluate(
                EntryProposal("x", contracts, contracts * mlpc), _snapshot(b)
            )
            fits_one = contracts_within_budget(b.remaining, mlpc) >= 1
            if not hostile_sizer and fits_one and BUDGET_REASON in decision.reasons:
                sizer_misfits.append(f"step {step}: {contracts} lots vs {b.remaining}")
            if decision.allowed:
                n_ideas += 1
                idea = f"i{n_ideas}"
                short = 7000.0 + 20 * n_ideas
                h = _Held(idea, short, short + width, contracts, credit, credit)
                for f in open_fills(idea, h.short, h.long_, contracts, credit):
                    ledger.note(f)
                held.append(h)
                fees += 1.5 * contracts
                if rng.random() < 0.5:
                    scan_lag.add(idea)          # the scan has not refreshed yet
        elif action < 0.80:
            # An exit: a stop that overshoots, a flatten near zero, or a full-width
            # failure — any debit in [0, width].
            h = held.pop(rng.randrange(len(held)))
            debit = rng.choice((h.width, round(rng.uniform(0, h.width) / 0.05) * 0.05))
            for f in close_fills(h.idea, h.short, h.long_, h.contracts, debit):
                ledger.note(f)
            realized += (h.credit - debit) * 100 * h.contracts
            fees += 1.5 * h.contracts
            if h.idea not in scan_lag and rng.random() < 0.5:
                scan_stale[h.idea] = h.width * 100 * h.contracts   # not refreshed yet
            scan_lag.discard(h.idea)
        else:
            # Marks move and the scan catches up. Neither may change W.
            for h in held:
                h.mark = round(rng.uniform(0, h.width) / 0.05) * 0.05
            scan_lag.clear()
            scan_stale.clear()
        if w_true() > CAP + 1e-6:
            violations.append(f"step {step}: W = {w_true():.2f}")
    return violations, sizer_misfits


@pytest.mark.parametrize("hostile_sizer", [False, True])
@pytest.mark.parametrize("live", [False, True])
async def test_the_day_can_never_lose_more_than_the_cap(hostile_sizer, live):
    rng = random.Random(20260921 + hostile_sizer * 7 + live * 13)
    for day in range(150):
        violations, misfits = await _run_day(rng, hostile_sizer=hostile_sizer, live=live)
        assert violations == [], f"day {day}"
        assert misfits == [], f"day {day}"


async def test_mutation_without_the_gate_reason_the_invariant_breaks(monkeypatch):
    """Enforced twice means each half is load-bearing: with the gate's budget check
    removed, the sizer's one-lot floor alone opens positions past the cap."""
    monkeypatch.setattr(gate_module, "exceeds_budget", lambda *_: False)
    rng = random.Random(1)
    broke = False
    for _ in range(60):
        violations, _ = await _run_day(rng, hostile_sizer=False, live=False)
        broke = broke or bool(violations)
    assert broke


async def test_mutation_without_the_sizer_bound_the_sizer_stops_fitting(monkeypatch):
    """With the sizer's budget bound removed, it proposes lots the budget cannot
    hold while a smaller size would fit — refused entries that should have been
    1-lot re-entries."""
    monkeypatch.setattr(orchestrator_module, "contracts_within_budget", lambda *_: 10**6)
    rng = random.Random(2)
    misfit = False
    for _ in range(60):
        _, misfits = await _run_day(rng, hostile_sizer=False, live=False)
        misfit = misfit or bool(misfits)
    assert misfit


# -- the restart: rehydration reproduces the in-memory ledger ---------------------


def _placed(identifier: str, legs: list[tuple[str, str, float]], *, order_id: int):
    return SimpleNamespace(
        id=order_id,
        legs=[
            SimpleNamespace(
                symbol=symbol, action=action, quantity=2, remaining_quantity=0,
                instrument_type="Equity Option",
                fills=[SimpleNamespace(quantity=2, fill_price=str(price),
                                       fill_id=f"{order_id}-{symbol}", filled_at=None)],
            )
            for symbol, action, price in legs
        ],
        price="0.65", underlying_symbol="SPXW", external_identifier=identifier,
        status="Filled", replaces_order_id=None, replacing_order_id=None,
        reject_reason=None,
    )


async def test_the_fill_consumer_feeds_the_ledger_and_a_restart_rebuilds_it(audit: AuditLog):
    audit.start()
    live = DayLedger()
    consumer = FillConsumer(store=InMemoryOrderStore(), audit=audit, ledger=live)
    short, long_ = occ(7725), occ(7730)
    await consumer.consume(_placed(ident("call-7725-7730"), [
        (short, "Sell to Open", 1.65), (long_, "Buy to Open", 1.00)], order_id=1))
    await consumer.consume(_placed(ident("call-7725-7730", "close"), [
        (short, "Buy to Close", 2.00), (long_, "Sell to Close", 0.50)], order_id=2))
    await consumer.consume(_placed(ident("call-7735-7740"), [
        (occ(7735), "Sell to Open", 1.65), (occ(7740), "Buy to Open", 1.00)], order_id=3))
    assert live.fills == 6
    assert live.cash_today == 130.0 - 300.0 + 130.0
    state = await load_day_risk_state(audit, trading_date=et_today())
    rebuilt = state.ledger()
    assert (rebuilt.cash_today, rebuilt.cash_complete) == (live.cash_today, live.cash_complete)
    # The CLOSED set is rebuilt too: after a restart the ledger still overrules a
    # scan that lists the stopped-out spread.
    assert rebuilt.exposure() == live.exposure() == Exposure(
        by_idea={"call-7735-7740": 1000.0}, closed=frozenset({"call-7725-7730"})
    )
    assert state.restored is True
    await audit.aclose()


async def test_an_unparseable_fill_row_is_unreadable_not_a_halt(audit: AuditLog):
    audit.start()
    await audit.record(FILL_CATEGORY, {"garbage": True})
    state = await load_day_risk_state(audit, trading_date=et_today())
    assert state.fills == (None,)
    assert state.ledger().exposure().unknown == "fill_ledger_unreadable"
    await audit.aclose()


# -- the watchdog's reads -----------------------------------------------------------


def _order(identifier: str, *, contracts: int = 2, width: float = 5.0,
           direction: OrderDirection = OrderDirection.OPEN, symbols=None) -> Order:
    short, long_ = symbols or (occ(7725), occ(7725 + width))
    return Order(
        direction=direction, underlying="SPXW",
        legs=(OrderLeg(short, LegAction.SELL_TO_OPEN, contracts),
              OrderLeg(long_, LegAction.BUY_TO_OPEN, contracts)),
        limit_price=Decimal("0.65"), external_identifier=identifier,
    )


def test_working_opening_orders_are_worth_their_width_at_the_largest_rung():
    live = LiveOrders(orders=(
        _order(ident("a"), contracts=2), _order(ident("a"), contracts=1),     # two rungs
        _order(ident("b"), width=10.0, contracts=1),
        _order(ident("c", "close"), direction=OrderDirection.CLOSE),          # an exit
        _order("hand-placed"),                                                # foreign
    ))
    assert open_working_worst_case(live).by_idea == {"a": 1000.0, "b": 1000.0}


def test_an_unmappable_argus_order_or_unreadable_width_cannot_be_bounded():
    unmappable = LiveOrders(unmappable=(UnmappableOrder(
        broker_order_id=9, external_identifier=ident("a"), underlying="SPXW",
        raw_status="Live", reason="x",
    ),))
    assert open_working_worst_case(unmappable).unknown == "working_order_unmappable"
    garbled = LiveOrders(orders=(_order(ident("a"), symbols=("SPXW  P1", "SPXW  P2")),))
    assert open_working_worst_case(garbled).unknown == "working_order_unreadable"


def _spread(idea: str, *, short_q: int = 2, long_q: int = 2) -> OpenSpread:
    return OpenSpread(
        external_identifier=ident(idea), broker_order_id=1, candidate_id=idea,
        underlying="SPXW", short_symbol=occ(7725), long_symbol=occ(7730),
        short_quantity=short_q, long_quantity=long_q, entry_credit=Decimal("0.65"),
    )


async def test_the_watchdog_exposure_follows_its_scan():
    from test_watchdog import FakeClock, build_quotes, build_watchdog

    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    wd, *_ = build_watchdog(clock=clock, quotes=quotes,
                            scan=OpenSpreadScan(spreads=(_spread("a"),)))
    assert wd.open_exposure().unknown == "watchdog_not_scanned"
    await wd.check_once()
    assert wd.open_exposure().by_idea == {"a": 1000.0}

    for scan, reason in [
        (OpenSpreadScan(unattributed=(occ(7725),)), "unmanaged_position"),
        (OpenSpreadScan(spreads=(_spread("a", long_q=1),)), "imbalanced_position"),
    ]:
        wd, *_ = build_watchdog(clock=clock, quotes=quotes, scan=scan)
        await wd.check_once()
        assert wd.open_exposure().unknown == reason


# -- wiring --------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[1] / "src" / "argus"


def _calls(path: Path, name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)
    ]


def test_the_session_feeds_its_fill_consumer_the_days_ledger():
    """The ledger is fail-OPEN if the fill path stops feeding it: cash would read
    zero all day. So the one production construction must pass it."""
    (call,) = _calls(_SRC / "session.py", "FillConsumer")
    kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert kw.get("ledger") == "state.day_ledger"


def test_the_day_ledger_is_rehydrated_never_built_empty_in_the_supervisor():
    (call,) = _calls(_SRC / "app.py", "SessionState")
    kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert kw["day_ledger"] == "day_risk.ledger()"


def test_the_budget_never_reaches_the_kill_switch():
    """Not a latch: nothing in the budget's modules may engage the durable switch."""
    for module in ("risk/loss_budget.py",):
        assert not _calls(_SRC / module, "engage")
    gate_tree = ast.parse((_SRC / "risk" / "gate.py").read_text())
    assert not [n for n in ast.walk(gate_tree)
                if isinstance(n, ast.Attribute) and n.attr == "engage"]


# -- /status ---------------------------------------------------------------------------


def test_status_row_says_what_is_left_and_which_estimate_binds():
    from argus.status import _loss_budget_cell, _loss_budget_json

    assert "no session" in _loss_budget_cell(None) and _loss_budget_json(None) is None
    healthy = _loss_budget_cell(_budget_with(770.0))
    assert "$770.00" in healthy and "$950.00" in healthy and "binds: ledger" in healthy
    assert "class='ok'" in healthy
    assert "class='bad'" in _loss_budget_cell(_budget_with(0.0))
    unknown = budget(DayLedger(), scan=Exposure.cannot_say("unmanaged_position"))
    cell = _loss_budget_cell(unknown)
    assert "UNREADABLE" in cell and "unmanaged_position" in cell
    doc = _loss_budget_json(unknown)
    assert doc["remaining"] == 0.0 and doc["binding"] == "unknown"
    assert doc["unknown_reason"] == "unmanaged_position"
    json.dumps(doc, allow_nan=False)
