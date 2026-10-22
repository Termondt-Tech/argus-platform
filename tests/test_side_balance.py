"""The side-balance rule (v0.5.2, 2026-09-26): which side a new spread may open on.

Pins: the pure rule; the per-side counts (the concurrency cap's own formula, per
side, fail-closed on anything unreadable); the gate's refusal; the orchestrator's
choice end to end through real audit records; the rule OFF being the pre-2026-09-26
path exactly; and the config. → docs/designs/2026-09-26-multi-spread-side-balance.md
"""

from __future__ import annotations

import json
from datetime import time
from types import SimpleNamespace

import pytest

from argus.account.fake import FakeAccountState
from argus.account.models import Position
from argus.audit.log import AuditLog
from argus.config import Config, ConfigError
from argus.execution.models import LiveOrders
from argus.execution.watchdog import PositionWatchdog
from argus.execution.working_orders import open_working_spreads_by_side, side_of_identifier
from argus.marketdata import FakeMarketData
from argus.orchestrator import SignalLoop
from argus.risk import DailyLossBreaker, KillSwitch, PreTradeGate
from argus.risk.models import EntryProposal
from argus.risk.side_balance import REFUSAL, admissible, count_by_side, rank
from argus.risk.snapshot import DailyPnl, RiskSnapshotBuilder, _broker_legs_by_side
from argus.signal.chain import FakeChainSource
from argus.signal.models import ChainSnapshot, OptionQuote, SpreadSignal, StrategyResult
from support import config_dict, loss_budget_source, make_config

# -- the pure rule ---------------------------------------------------------------


@pytest.mark.parametrize("puts, calls, side, ok", [
    (0, 0, "put", True), (0, 0, "call", True),
    (1, 0, "put", False), (1, 0, "call", True),      # a put held: only a call
    (0, 1, "put", True), (0, 1, "call", False),      # a call held: only a put
    (1, 1, "put", True), (1, 1, "call", True),       # balanced: either (the cap decides)
])
def test_admissible_at_an_imbalance_of_one_is_one_per_side(puts, calls, side, ok):
    assert admissible(side, puts=puts, calls=calls, unknown=0, max_imbalance=1) is ok


def test_a_wider_band_allows_a_lean():
    assert admissible("put", puts=1, calls=0, unknown=0, max_imbalance=2)
    assert not admissible("put", puts=2, calls=0, unknown=0, max_imbalance=2)


# -- an unreadable side is the WORST side, not both (2026-09-27 review, finding 2) --
#
# Counting an unknown idea on both sides leaves `puts - calls` unchanged, so it
# changed no verdict: an unknown that was really a put let a second put open.


@pytest.mark.parametrize("puts, calls, unknown, side, ok", [
    (0, 0, 1, "put", False),   # the unknown could be a put: a put might make two
    (0, 0, 1, "call", False),  # ... or a call: likewise
    (1, 0, 1, "call", True),   # a put held: 2-1 or 1-2 either way, within the band
    (1, 0, 1, "put", False),
    (1, 1, 1, "put", False),   # could leave 3-1
    (0, 0, 2, "put", False),
])
def test_an_unknown_side_is_admissible_only_if_every_reading_is(puts, calls, unknown, side, ok):
    assert admissible(side, puts=puts, calls=calls, unknown=unknown, max_imbalance=1) is ok
    # And the property the review found missing: for EVERY assignment of the unknowns
    # to a side, the true book after the entry stays within the band iff admissible.
    worst = max(
        abs((puts + k) - (calls + unknown - k) + (1 if side == "put" else -1))
        for k in range(unknown + 1)
    )
    assert (worst <= 1) is ok


def test_counting_on_both_sides_was_neutral():
    """The defect, stated as arithmetic: +1 to each side moves no verdict."""
    for puts, calls in ((0, 0), (1, 0), (0, 1), (1, 1)):
        for side in ("put", "call"):
            both = admissible(side, puts=puts + 1, calls=calls + 1, unknown=0, max_imbalance=1)
            neither = admissible(side, puts=puts, calls=calls, unknown=0, max_imbalance=1)
            assert both == neither


def _s(side):
    return SimpleNamespace(side=side)


def test_rank_puts_the_balancing_side_first_and_ties_in_sides_order():
    put, call = _s("put"), _s("call")
    kw = dict(side_of=lambda c: c.side, max_imbalance=1, unknown=0)
    assert rank([put, call], puts=0, calls=0, sides_order=("put", "call"), **kw) == [put, call]
    assert rank([put, call], puts=0, calls=0, sides_order=("call", "put"), **kw) == [call, put]
    assert rank([put, call], puts=1, calls=0, sides_order=("put", "call"), **kw) == [call]
    assert rank([put, call], puts=0, calls=1, sides_order=("put", "call"), **kw) == [put]
    assert rank([put], puts=1, calls=0, sides_order=("put", "call"), **kw) == []
    # An unknown narrows what is admissible, never the order of what is left.
    kw["unknown"] = 1
    assert rank([put, call], puts=1, calls=0, sides_order=("put", "call"), **kw) == [call]
    assert rank([put, call], puts=0, calls=0, sides_order=("put", "call"), **kw) == []


def test_count_by_side_is_the_concurrency_formula_per_side():
    # A chained put pair nets its shared strike to zero at the broker: 2 legs, but
    # 2 ideas in the ledger. The ledger wins; a working opening call adds.
    counts = count_by_side(broker_legs={"put": 2, "call": 0},
                           ledger_ideas={"put": 2}, working_ideas={"call": 1})
    assert counts == {"put": 2, "call": 1, "unknown": 0}
    # A leg the ledger never booked still counts from the broker (ceil of half).
    assert count_by_side(broker_legs={"call": 1}, ledger_ideas={}, working_ideas={}) == {
        "put": 0, "call": 1, "unknown": 0}
    # Unknowns are their own count, an upper bound on their effect: ceil of half the
    # legs, plus every unknown ledger and working idea. Never on a side.
    assert count_by_side(broker_legs={"unknown": 3}, ledger_ideas={"unknown": 1},
                         working_ideas={"unknown": 1}) == {"put": 0, "call": 0, "unknown": 4}


# -- the per-side counts, fail closed ----------------------------------------------


def _pos(symbol, qty=-1):
    return Position(symbol, "SPXW", "Equity Option", qty, 0.0, None)


def _builder(account, **kw):
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    return RiskSnapshotBuilder(market, account, DailyPnl(anchor_nlv=100_000),
                               reference_symbols=("SPX",), loss_budget=loss_budget_source(), **kw)


def test_the_builder_counts_broker_legs_by_right_and_an_unreadable_leg_as_unknown():
    account = FakeAccountState()
    account.set_net_liq(100_000)
    account.set_position(_pos("SPXW  260925P07655000"))
    account.set_position(_pos("SPXW  260925P07650000", 1))
    account.set_position(_pos("NOT-AN-OCC-SYMBOL"))
    snap = _builder(account).build()
    # put legs: 2 -> 1 spread; the unreadable leg is its own count, on no side
    assert (snap.put_spread_count, snap.call_spread_count) == (1, 0)
    assert snap.unknown_side_spread_count == 1


def test_a_flat_broker_leg_is_not_counted_on_its_side():
    """REPORT mutation M11 survived: nothing pinned the flat-leg skip. A leg the broker
    reports at zero is not held; counting it would refuse an admissible entry.

    Called DIRECTLY, deliberately: both `AccountCache.update_position` and
    `FakeAccountState` drop a zero-quantity position on write, so no snapshot built
    through either ever reaches the skip. It is the same defensive filter
    `AccountSnapshot.open_position_count` applies, and this pins the function's own
    contract ("non-flat legs") rather than pretending the builder exercises it."""
    legs = _broker_legs_by_side([
        _pos("SPXW  260925P07655000", 0),
        _pos("SPXW  260925C07700000"),
        _pos("NOT-AN-OCC-SYMBOL", 0),
    ])
    assert legs == {"put": 0, "call": 1, "unknown": 0}


def test_the_builder_takes_the_ledger_and_working_counts_by_side():
    account = FakeAccountState()
    account.set_net_liq(100_000)
    snap = _builder(account, ledger_spreads_by_side=lambda: {"put": 1},
                    working_open_spreads_by_side=lambda: {"call": 1}).build()
    assert (snap.put_spread_count, snap.call_spread_count) == (1, 1)


def test_the_side_comes_from_the_identifier_argus_minted():
    assert side_of_identifier("argus-open-20260925-put-7655-7650") == "put"
    assert side_of_identifier("argus-close-20260921-call-7725-7730") == "call"
    assert side_of_identifier("somebody-else") is None
    assert side_of_identifier(None) is None


def test_working_orders_by_side_count_an_unreadable_idea_as_unknown():
    order = SimpleNamespace(is_working=True, external_identifier="argus-open-20260925-put-7655-7650")
    from argus.execution.models import OrderDirection
    order.direction = OrderDirection.OPEN
    bad = SimpleNamespace(external_identifier="argus-open-garbled")
    live = LiveOrders(orders=(order,), unmappable=(bad,))
    assert open_working_spreads_by_side(live) == {"put": 1, "call": 0, "unknown": 1}


def test_the_watchdog_counts_its_scan_by_side_and_an_unreadable_one_as_unknown():
    wd = PositionWatchdog.__new__(PositionWatchdog)
    wd._scan = SimpleNamespace(spreads=(
        SimpleNamespace(external_identifier="argus-open-20260925-put-7655-7650"),
        SimpleNamespace(external_identifier="argus-open-20260925-call-7700-7705"),
        SimpleNamespace(external_identifier="argus-open-garbled"),
    ))
    assert wd.open_spreads_by_side == {"put": 1, "call": 1, "unknown": 1}


# -- the gate refuses --------------------------------------------------------------


def _gate(audit, cfg):
    return PreTradeGate(config=cfg, audit=audit, killswitch=KillSwitch(audit),
                        breaker=DailyLossBreaker(audit, daily_max_loss=cfg.risk.daily_max_loss))


def _snapshot(puts, calls):
    account = FakeAccountState()
    account.set_net_liq(100_000)
    return _builder(account, ledger_spread_count=lambda: puts + calls,
                    ledger_spreads_by_side=lambda: {"put": puts, "call": calls}).build()


@pytest.mark.parametrize("side_balance, side, refused", [
    (True, "put", True),     # a put held: a second put is refused
    (True, "call", False),   # the other side is fine
    (True, None, False),     # no side on the proposal: never guessed at
    (False, "put", False),   # the rule off: today's behaviour
])
async def test_the_gate_refuses_an_inadmissible_side(audit: AuditLog, side_balance, side, refused):
    cfg = make_config(risk={"side_balance": side_balance})
    decision = await _gate(audit, cfg).evaluate(
        EntryProposal("x", 1, 400.0, side=side), _snapshot(puts=1, calls=0))
    assert (REFUSAL in decision.reasons) is refused


@pytest.mark.parametrize("side", ["put", "call"])
async def test_the_gate_refuses_either_side_beside_an_unknown_one(audit: AuditLog, side):
    """Finding 2 (2026-09-27 review) at the rail: an empty book plus one working
    opening order whose side cannot be read. Counted on both sides, that was 1-1 and
    either side was admitted; the unknown could be either, so neither is."""
    account = FakeAccountState()
    account.set_net_liq(100_000)
    snap = _builder(account, working_open_spread_count=lambda: 1,
                    working_open_spreads_by_side=lambda: {"unknown": 1}).build()
    assert (snap.put_spread_count, snap.call_spread_count,
            snap.unknown_side_spread_count) == (0, 0, 1)
    cfg = make_config(risk={"side_balance": True})
    decision = await _gate(audit, cfg).evaluate(EntryProposal("x", 1, 400.0, side=side), snap)
    assert REFUSAL in decision.reasons
    rows = [json.loads(r["payload"]) for r in audit._conn.execute(
        "SELECT category, payload FROM audit_log ORDER BY seq") if r["category"] == "gate.decision"]
    assert rows[-1]["snapshot"]["unknown_side_spread_count"] == 1


# -- the orchestrator chooses ------------------------------------------------------


def _signal(side):
    if side == "put":
        short, long_ = OptionQuote("P4975", 4975, "put", -0.15, 1.50, 1.60), OptionQuote("P4970", 4970, "put", -0.10, 0.70, 0.80)
    else:
        short, long_ = OptionQuote("C5025", 5025, "call", 0.15, 1.50, 1.60), OptionQuote("C5030", 5030, "call", 0.10, 0.70, 0.80)
    return SpreadSignal(underlying="SPXW", side=side, short_leg=short, long_leg=long_,
                        credit=0.80, width=5.0, captured_at=0.0)


PUT, CALL = _signal("put"), _signal("call")


class _Strategy:
    def __init__(self, result):
        self._result = result

    def evaluate(self, chain, now_et, regime=None):
        return self._result

    def size_multiplier(self, regime):
        return 1.0


def _both_qualify():
    return StrategyResult(PUT, detail={"other_sides": {"call": {"qualified": True, **CALL.to_payload()}}},
                          alternatives=(CALL,))


async def _run(audit, *, result, puts=0, calls=0, unknown=0, side_balance=True, attempted=None):
    cfg = make_config(risk={"side_balance": side_balance}, signal={"sides": ["put", "call"]})
    account = FakeAccountState()
    account.set_net_liq(100_000)
    builder = _builder(account, ledger_spread_count=lambda: puts + calls + unknown,
                       ledger_spreads_by_side=lambda: {"put": puts, "call": calls,
                                                       "unknown": unknown})
    sent = []

    async def submit(signal, proposal, risk):
        sent.append((signal, proposal))

    loop = SignalLoop(strategy=_Strategy(result), chain_source=FakeChainSource(ChainSnapshot("SPXW", 5000.0, (), 0.0)),
                      snapshot_builder=builder, gate=_gate(audit, cfg), audit=audit, config=cfg,
                      clock_et=lambda: time(12, 0), submit=submit, attempted=attempted)
    await loop.evaluate_once()
    rows = {r["category"]: json.loads(r["payload"]) for r in audit._conn.execute(
        "SELECT category, payload FROM audit_log ORDER BY seq")}
    audit.verify_chain()
    return sent, rows


async def test_with_a_put_held_the_call_is_proposed_and_sent(audit: AuditLog):
    sent, rows = await _run(audit, result=_both_qualify(), puts=1)
    assert [s.side for s, _ in sent] == ["call"]
    assert sent[0][1].side == "call"
    cand = rows["signal.candidate"]
    assert cand["side"] == "call"
    assert cand["book"] == {"puts": 1, "calls": 0, "unknown": 0, "side_balance": True,
                            "max_side_imbalance": 1, "choice": "balance"}
    # other_sides is relative to what was PROPOSED: the put is now the other side.
    assert set(cand["other_sides"]) == {"put"} and cand["other_sides"]["put"]["qualified"] is True


async def test_an_empty_book_proposes_the_put_first(audit: AuditLog):
    sent, rows = await _run(audit, result=_both_qualify())
    assert [s.side for s, _ in sent] == ["put"]
    assert rows["signal.candidate"]["book"]["choice"] == "first"


async def test_a_repeated_put_no_longer_hides_a_qualifying_call(audit: AuditLog):
    sent, rows = await _run(audit, result=_both_qualify(),
                            attempted=lambda s: s.side == "put")
    assert [s.side for s, _ in sent] == ["call"]
    assert rows["signal.candidate"]["book"]["choice"] == "balance"


async def test_only_the_held_side_qualifies_nothing_is_sent_and_the_gate_says_why(audit: AuditLog):
    sent, rows = await _run(audit, result=StrategyResult(PUT, detail={"other_sides": {}}), puts=1)
    assert sent == []
    assert rows["signal.candidate"]["book"]["choice"] == "refused"
    assert REFUSAL in rows["gate.decision"]["reasons"]
    assert rows["gate.decision"]["snapshot"]["put_spread_count"] == 1


async def test_beside_an_unknown_side_the_selector_proposes_neither(audit: AuditLog):
    """Finding 2 (2026-09-27 review) at the selector: both sides qualify on an empty
    book holding one idea whose side cannot be read. Nothing is sent, the book says
    why, and the gate refuses the forwarded first side on the record."""
    sent, rows = await _run(audit, result=_both_qualify(), unknown=1)
    assert sent == []
    assert rows["signal.candidate"]["book"]["unknown"] == 1
    assert rows["signal.candidate"]["book"]["choice"] == "refused"
    assert REFUSAL in rows["gate.decision"]["reasons"]


async def test_with_a_put_held_and_an_unknown_the_call_still_goes(audit: AuditLog):
    """The worst case is exact, not a blanket refusal: 2-1 or 1-2 either way. (At two
    slots the CAP then refuses it — put + unknown fill both — which is the cap's
    call, not the side rule's; the side rule's verdict is what is pinned here.)"""
    sent, rows = await _run(audit, result=_both_qualify(), puts=1, unknown=1)
    assert rows["signal.candidate"]["side"] == "call"
    assert rows["signal.candidate"]["book"]["choice"] == "balance"
    assert REFUSAL not in rows["gate.decision"]["reasons"]
    assert "concurrent_limit" in rows["gate.decision"]["reasons"]
    assert sent == []


async def test_the_rule_off_is_the_old_path_exactly(audit: AuditLog):
    """Off: the strategy's first side goes forward even with that side held — the
    pre-2026-09-26 behaviour, and the config-only rollback."""
    sent, rows = await _run(audit, result=_both_qualify(), puts=1, side_balance=False)
    assert [s.side for s, _ in sent] == ["put"]
    assert rows["signal.candidate"]["book"]["choice"] == "off"
    assert REFUSAL not in rows["gate.decision"]["reasons"]


# -- config -----------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, 1, "true", None])
def test_side_balance_must_be_a_real_boolean(bad):
    raw = config_dict()
    raw["risk"]["side_balance"] = bad
    with pytest.raises(ConfigError, match="side_balance"):
        Config.from_dict(raw)


@pytest.mark.parametrize("bad", [0, -1, 1.0, True])
def test_max_side_imbalance_must_be_a_positive_int(bad):
    raw = config_dict()
    raw["risk"]["max_side_imbalance"] = bad
    with pytest.raises(ConfigError, match="max_side_imbalance"):
        Config.from_dict(raw)


@pytest.mark.parametrize("profile", ["argus.live.toml", "argus.sandbox.toml", "argus.dev.toml"])
def test_every_profile_ships_one_per_side(profile):
    risk = Config.load(profile).risk
    assert risk.side_balance is True and risk.max_side_imbalance == 1


def test_the_session_wires_the_per_side_counts_and_the_dedup_read():
    """The rule is only as good as its inputs: the session must hand the builder the
    watchdog's per-side counts and the loop the pipeline's dedup read."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "argus" / "session.py").read_text()
    assert "ledger_spreads_by_side=lambda" in src
    assert "working_open_spreads_by_side=lambda" in src
    assert "attempted=trading.entry.was_attempted" in src


# -- keyed on TRADE IDEAS, never symbols (design §7.1; 2026-09-27 review, finding 3c) --
#
# Spreads chain: a shared strike nets to zero at the broker and in `positions`, so a
# symbol-keyed count of the ledger half undercounts exactly the chained same-side
# pair the side rule exists to stop (CLAUDE.md's fourth standing landmine). The
# design promised this pin; the build shipped without it.

_SYMBOL_ATTRIBUTES = {"symbol", "short_symbol", "long_symbol", "legs", "short_leg", "long_leg"}


def _function_node(path, qualname):
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(path).read_text())
    parts = qualname.split(".")
    nodes = tree.body
    for i, part in enumerate(parts):
        wanted = ast.ClassDef if i < len(parts) - 1 else (ast.FunctionDef, ast.AsyncFunctionDef)
        (node,) = [n for n in nodes if isinstance(n, wanted) and n.name == part]
        nodes = node.body
    return node


@pytest.mark.parametrize("path, qualname", [
    ("src/argus/execution/watchdog.py", "PositionWatchdog.open_spreads_by_side"),
    ("src/argus/execution/working_orders.py", "open_working_spreads_by_side"),
])
def test_the_per_side_idea_counts_are_keyed_on_the_identifier_never_a_symbol(path, qualname):
    import ast

    node = _function_node(path, qualname)
    attributes = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
    called = {
        (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None))
        for n in ast.walk(node) if isinstance(n, ast.Call)
    }
    assert "external_identifier" in attributes
    assert not attributes & _SYMBOL_ATTRIBUTES, attributes & _SYMBOL_ATTRIBUTES
    assert not {c for c in called if c and c.startswith("occ_")}
    # The side comes from the identifier Argus minted, through the one parser.
    assert "side_of_identifier" in called
