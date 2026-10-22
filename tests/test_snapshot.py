"""RiskSnapshot assembly: day-P&L anchoring, staleness propagation, gate wiring."""

from __future__ import annotations

import math

from argus.account.fake import FakeAccountState
from argus.account.models import Position
from argus.audit.log import AuditLog
from support import make_config, loss_budget_source
from argus.marketdata import FakeMarketData
from argus.risk import DailyLossBreaker, EntryProposal, KillSwitch, PreTradeGate
from argus.risk.snapshot import DailyPnl, RiskSnapshotBuilder


def _pos(symbol, qty):
    return Position(symbol, "SPX", "Equity Option", qty, 1.2, 0.9)


# -- DailyPnl -------------------------------------------------------------


def test_daily_pnl_zero_until_anchored():
    d = DailyPnl()
    assert d.anchored is False
    assert d.value(100_000) == 0.0


def test_daily_pnl_is_nlv_change_since_anchor():
    d = DailyPnl()
    d.anchor_to(100_000)
    assert d.value(99_000) == -1000.0
    assert d.value(101_500) == 1500.0
    d.reset()
    assert d.anchored is False
    assert d.value(99_000) == 0.0


# -- builder assembly -----------------------------------------------------


def test_builder_assembles_fields():
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    account = FakeAccountState()
    account.set_net_liq(100_000)
    account.set_position(_pos("SPXW1", -1))
    builder = RiskSnapshotBuilder(
        market, account, DailyPnl(anchor_nlv=100_000), reference_symbols=("SPX",), loss_budget=loss_budget_source()
    )
    snap = builder.build()
    assert snap.account_nlv == 100_000
    assert snap.open_position_count == 1
    assert snap.daily_pnl == 0.0  # nlv == anchor
    assert snap.market_data_age_s == 0.0
    assert snap.account_stream_age_s == 0.0


def test_the_builder_reports_legs_and_the_snapshot_derives_spreads():
    """The two units meet here. The account cache counts one entry per OCC symbol,
    so a single vertical arrives as 2 — and every risk cap is stated in spreads."""
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    account = FakeAccountState()
    account.set_net_liq(100_000)
    account.set_position(_pos("SPXW_7825C", -1))   # short leg
    account.set_position(_pos("SPXW_7830C", 1))    # long leg
    builder = RiskSnapshotBuilder(
        market, account, DailyPnl(anchor_nlv=100_000), reference_symbols=("SPX",), loss_budget=loss_budget_source()
    )
    snap = builder.build()
    assert snap.open_position_count == 2, "the broker reports LEGS"
    assert snap.open_spread_equivalent == 1, "one vertical is one position"


def test_an_unusable_account_snapshot_reports_no_spreads():
    """No account state means 0 legs and 0 spreads — the gate then fails closed on
    the invalid NLV and the stale stream, which is where that decision belongs."""
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    snap = RiskSnapshotBuilder(
        market, FakeAccountState(), DailyPnl(), reference_symbols=("SPX",), loss_budget=loss_budget_source()
    ).build()
    assert snap.open_position_count == 0 and snap.open_spread_equivalent == 0


def test_builder_daily_pnl_reflects_loss():
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    account = FakeAccountState()
    account.set_net_liq(98_500)
    builder = RiskSnapshotBuilder(market, account, DailyPnl(anchor_nlv=100_000), loss_budget=loss_budget_source())
    assert builder.build().daily_pnl == -1500.0


def test_builder_fail_closed_without_account_state():
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    account = FakeAccountState()  # no events -> no snapshot
    snap = RiskSnapshotBuilder(market, account, DailyPnl(), loss_budget=loss_budget_source()).build()
    assert snap.account_nlv == 0.0
    assert snap.open_position_count == 0
    assert snap.daily_pnl == 0.0
    assert snap.account_stream_age_s == math.inf  # gate flags stale + invalid nlv


def test_builder_market_age_uses_feed_heartbeat_by_default():
    market = FakeMarketData()
    market.push("A", 1.0, 1.2)
    account = FakeAccountState()
    account.set_net_liq(100_000)
    builder = RiskSnapshotBuilder(market, account, DailyPnl(anchor_nlv=100_000), loss_budget=loss_budget_source())
    market.advance(2.0)
    assert builder.build().market_data_age_s == 2.0


def test_builder_market_age_is_worst_of_reference_symbols():
    market = FakeMarketData()
    market.push("A", 1.0, 1.2)          # t=0
    market.advance(1.0)
    market.push("B", 1.0, 1.2)          # t=1
    market.advance(0.5)                 # now=1.5 -> A age 1.5, B age 0.5
    account = FakeAccountState()
    account.set_net_liq(100_000)
    builder = RiskSnapshotBuilder(
        market, account, DailyPnl(anchor_nlv=100_000), reference_symbols=("A", "B"), loss_budget=loss_budget_source()
    )
    assert builder.build().market_data_age_s == 1.5  # worst (fail closed)


def test_anchor_day_pnl_if_possible():
    market = FakeMarketData()
    account = FakeAccountState()
    day = DailyPnl()
    builder = RiskSnapshotBuilder(market, account, day, loss_budget=loss_budget_source())
    assert builder.anchor_day_pnl_if_possible() is False  # no account data yet
    account.set_net_liq(100_000)
    assert builder.anchor_day_pnl_if_possible() is True
    assert day.anchor == 100_000
    assert builder.anchor_day_pnl_if_possible() is False  # already anchored


# -- end-to-end: built snapshot flows through the real gate ---------------



async def test_built_snapshot_allows_clean_entry_through_gate(audit: AuditLog):
    cfg = make_config()
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    account = FakeAccountState()
    account.set_net_liq(100_000)
    builder = RiskSnapshotBuilder(
        market, account, DailyPnl(anchor_nlv=100_000), reference_symbols=("SPX",), loss_budget=loss_budget_source()
    )
    gate = PreTradeGate(
        config=cfg,
        audit=audit,
        killswitch=KillSwitch(audit),
        breaker=DailyLossBreaker(audit, daily_max_loss=cfg.risk.daily_max_loss),
    )
    decision = await gate.evaluate(EntryProposal("SPXW1", 1, 425.0), builder.build())
    assert decision.allowed is True


async def test_built_snapshot_blocks_when_market_data_stale(audit: AuditLog):
    cfg = make_config()
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    market.advance(10.0)  # far past the 2.0s staleness tolerance
    account = FakeAccountState()
    account.set_net_liq(100_000)
    builder = RiskSnapshotBuilder(
        market, account, DailyPnl(anchor_nlv=100_000), reference_symbols=("SPX",), loss_budget=loss_budget_source()
    )
    gate = PreTradeGate(
        config=cfg,
        audit=audit,
        killswitch=KillSwitch(audit),
        breaker=DailyLossBreaker(audit, daily_max_loss=cfg.risk.daily_max_loss),
    )
    decision = await gate.evaluate(EntryProposal("SPXW1", 1, 425.0), builder.build())
    assert decision.allowed is False
    assert "market_data_stale" in decision.reasons


def _builder(*, positions: int, ledger_spread_count=None):
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    account = FakeAccountState()
    account.set_net_liq(100_000)
    for i in range(positions):
        account.set_position(_pos(f"SPXW_LEG{i}", -1 if i % 2 else 1))
    kwargs = {} if ledger_spread_count is None else {"ledger_spread_count": ledger_spread_count}
    return RiskSnapshotBuilder(
        market, account, DailyPnl(anchor_nlv=100_000), reference_symbols=("SPX",), **kwargs, loss_budget=loss_budget_source()
    )


def test_the_builder_reads_the_ledger_spread_count_at_every_build():
    """Late-bound, because the watchdog that owns the scan is built after this and
    the count has to be current at each `build()` rather than frozen at wiring."""
    count = {"n": 0}
    builder = _builder(positions=2, ledger_spread_count=lambda: count["n"])

    assert builder.build().open_spread_equivalent == 1, "broker-derived alone"
    # Two more spreads opened, chained onto the first: the shared strikes net to
    # zero so the BROKER still reports 2 legs. Only the ledger can see them.
    count["n"] = 3
    assert builder.build().open_spread_equivalent == 3


def test_a_builder_given_no_ledger_source_behaves_exactly_as_before():
    """Shadow, and every caller predating 2026-08-17."""
    snap = _builder(positions=4).build()
    assert snap.ledger_spread_count == 0
    assert snap.working_open_spread_count == 0
    assert snap.open_spread_equivalent == 2


def test_the_builder_reads_the_working_order_count_at_every_build():
    """Late-bound, same reason as the ledger count: the watchdog that polls the
    broker for working orders is built after this. Working orders ADD to the
    position-derived equivalent (Finding 6, 2026-08-28)."""
    working = {"n": 0}
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    account = FakeAccountState()
    account.set_net_liq(100_000)
    account.set_position(_pos("SPXW_A", 1))
    account.set_position(_pos("SPXW_B", -1))
    builder = RiskSnapshotBuilder(
        market,
        account,
        DailyPnl(anchor_nlv=100_000),
        reference_symbols=("SPX",),
        working_open_spread_count=lambda: working["n"], loss_budget=loss_budget_source(),
    )
    assert builder.build().open_spread_equivalent == 1
    working["n"] = 1
    assert builder.build().open_spread_equivalent == 2


# -- the snapshot SAYS whether day P&L is anchored (2026-09-18) ------------


def _anchor_builder(day_pnl: DailyPnl) -> RiskSnapshotBuilder:
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    account = FakeAccountState()
    account.set_net_liq(99_000)
    return RiskSnapshotBuilder(market, account, day_pnl, reference_symbols=("SPX",), loss_budget=loss_budget_source())


def test_an_unanchored_day_pnl_is_flagged_not_just_zero():
    """`DailyPnl.value` answers 0.0 before an anchor exists — indistinguishable from a
    flat day in every record, and the breaker reads it and cannot trip. The builder
    now says which it is."""
    snap = _anchor_builder(DailyPnl()).build()
    assert snap.daily_pnl == 0.0                 # unchanged: the gate is untouched
    assert snap.daily_pnl_anchored is False


def test_an_anchored_day_pnl_is_flagged_anchored():
    snap = _anchor_builder(DailyPnl(anchor_nlv=100_000)).build()
    assert snap.daily_pnl == -1000.0
    assert snap.daily_pnl_anchored is True


def test_the_no_account_placeholder_is_never_flagged_as_a_reading():
    """No account state: the builder writes nlv 0.0 and day P&L 0.0 as placeholders.
    With the day ANCHORED, the flag used to read True beside that placeholder, and
    `/status` rendered a green $0.00 day P&L as if it were measured."""
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    snap = RiskSnapshotBuilder(
        market, FakeAccountState(), DailyPnl(anchor_nlv=100_000), reference_symbols=("SPX",), loss_budget=loss_budget_source()
    ).build()
    assert snap.account_nlv == 0.0 and snap.daily_pnl == 0.0      # unchanged
    assert snap.daily_pnl_anchored is False
