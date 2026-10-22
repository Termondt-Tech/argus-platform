"""SignalLoop: shadow-mode signal -> gate -> would-submit, with correct audit tiers."""

from __future__ import annotations

import copy
from datetime import time

from argus.audit.log import AuditLog
from argus.config import Config
from support import make_config, loss_budget_source
from argus.marketdata import FakeMarketData
from argus.account.fake import FakeAccountState
from argus.orchestrator import SignalLoop, size_contracts
from argus.risk import DailyLossBreaker, KillSwitch, PreTradeGate
from argus.risk.snapshot import DailyPnl, RiskSnapshotBuilder
from argus.signal.chain import FakeChainSource
from argus.signal.models import ChainSnapshot, OptionQuote, SpreadSignal, StrategyResult



def _config():
    return make_config()


_SIGNAL = SpreadSignal(
    underlying="SPXW",
    side="put",
    short_leg=OptionQuote("P4975", 4975, "put", -0.15, 1.50, 1.60),
    long_leg=OptionQuote("P4970", 4970, "put", -0.10, 0.70, 0.80),
    credit=0.80,
    width=5.0,
    captured_at=0.0,
)
_CHAIN = ChainSnapshot("SPXW", 5000.0, (), 0.0)


class _StubStrategy:
    def __init__(self, signal, reason=None, detail=None, multiplier=1.0):
        self._result = StrategyResult(signal, reason, detail or {})
        self._multiplier = multiplier
        self.seen_regimes = []

    def evaluate(self, chain, now_et, regime=None):
        # Recorded so tests can assert the loop actually builds a regime and hands
        # it over, rather than only that the record it writes looks right.
        self.seen_regimes.append(regime)
        return self._result

    def size_multiplier(self, regime):
        return self._multiplier


def _fresh_builder():
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    account = FakeAccountState()
    account.set_net_liq(100_000)
    builder = RiskSnapshotBuilder(
        market, account, DailyPnl(anchor_nlv=100_000), reference_symbols=("SPX",), loss_budget=loss_budget_source()
    )
    return builder, market


def _loop(audit, *, signal, chain=_CHAIN, builder=None, submit=None, reason=None, detail=None):
    cfg = _config()
    builder = builder or _fresh_builder()[0]
    gate = PreTradeGate(
        config=cfg,
        audit=audit,
        killswitch=KillSwitch(audit),
        breaker=DailyLossBreaker(audit, daily_max_loss=cfg.risk.daily_max_loss),
    )
    return SignalLoop(
        strategy=_StubStrategy(signal, reason, detail),
        chain_source=FakeChainSource(chain),
        snapshot_builder=builder,
        gate=gate,
        audit=audit,
        config=cfg,
        clock_et=lambda: time(12, 0),
        submit=submit,
    )


def _categories(audit):
    return [r["category"] for r in audit._conn.execute("SELECT category FROM audit_log ORDER BY seq")]


# -- sizing ---------------------------------------------------------------


def _sized(nlv, pct, mlpc, *, dollars=1_000_000.0, contracts=1_000):
    """Sizing with both absolute caps parked out of the way unless a test moves them,
    so each assertion below isolates exactly one rail."""
    return size_contracts(
        nlv, pct, mlpc, max_loss_per_position=dollars, max_contracts_per_position=contracts,
        loss_budget=1_000_000.0,
    )


def test_size_contracts_pct_of_nlv_rail():
    assert _sized(100_000, 0.02, 420.0) == 4            # floor(2000/420)
    assert _sized(100_000, 0.02, 0.0) == 0              # invalid economics
    assert _sized(1_000, 0.02, 500.0) == 1              # budget < 1 lot -> still 1 (gate enforces)


def test_the_dollar_cap_binds_when_it_is_tighter_than_the_percentage():
    # 2% of 100k is 2000 (4 lots at 420), but the absolute cap allows only 1250.
    assert _sized(100_000, 0.02, 420.0, dollars=1250.0) == 2


def test_the_contract_cap_binds_when_it_is_tighter_than_either_dollar_bound():
    # Both dollar rails would allow 4; the contract cap is the whole reason it is 2.
    assert _sized(100_000, 0.02, 420.0, contracts=2) == 2


def test_the_tightest_rail_governs_whichever_it_is():
    # Same account, same caps, different trade: which rail binds moves with the
    # trade's own risk per contract, which is why both absolute rails exist.
    cheap, rich = 100.0, 900.0
    assert _sized(100_000, 0.02, cheap, dollars=1250.0, contracts=2) == 2   # contract cap
    assert _sized(100_000, 0.02, rich, dollars=1250.0, contracts=2) == 1    # dollar cap


def test_the_floor_of_one_survives_every_cap_so_the_gate_still_sees_it():
    # Nothing fits any rail, but the candidate is still sized to 1 and EVALUATED —
    # the gate is what denies it, and its record is the shadow-mode evidence.
    assert _sized(1_000, 0.02, 5_000.0, dollars=100.0, contracts=1) == 1


# -- loop behavior --------------------------------------------------------


async def test_no_chain_records_skip_telemetry(audit: AuditLog):
    audit.start()
    loop = _loop(audit, signal=_SIGNAL, chain=None)
    await loop.evaluate_once()
    await audit.aclose()
    assert _categories(audit) == ["signal.skip"]


async def test_no_signal_records_none_telemetry(audit: AuditLog):
    audit.start()
    loop = _loop(audit, signal=None)
    await loop.evaluate_once()
    await audit.aclose()
    # Plus the minute's strike ladder (2026-09-29), written before the strategy.
    assert _categories(audit) == ["signal.ladder", "signal.none"]


async def test_no_signal_telemetry_carries_rejection_diagnostics(audit: AuditLog):
    import json

    # The near-miss must survive into the audit log — that's what makes weeks of
    # shadow telemetry tunable rather than just "nothing fired".
    detail = {
        "per_side": {
            "put": {
                "reason": "credit_below_min",
                "short_strike": 7440.0,
                "short_delta": -0.1541,
                "long_strike": 7435.0,
                "credit": 0.575,
                "min_credit": 0.75,
                "width": 5.0,
            }
        }
    }
    audit.start()
    loop = _loop(audit, signal=None, reason="credit_below_min", detail=detail)
    await loop.evaluate_once()
    await audit.aclose()

    row = audit._conn.execute(
        "SELECT payload FROM audit_log WHERE category = 'signal.none'"
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["reason"] == "credit_below_min"
    assert payload["per_side"]["put"]["credit"] == 0.575
    assert payload["per_side"]["put"]["min_credit"] == 0.75
    audit.verify_chain()


async def test_clean_candidate_logs_would_submit_and_never_submits(audit: AuditLog):
    submitted = []
    loop = _loop(audit, signal=_SIGNAL, submit=None)  # shadow
    await loop.evaluate_once()
    cats = _categories(audit)
    assert cats == ["signal.candidate", "gate.decision", "shadow.would_submit"]
    audit.verify_chain()
    assert submitted == []  # nothing routed


async def test_gate_denial_blocks_would_submit(audit: AuditLog):
    builder, market = _fresh_builder()
    market.advance(10.0)  # market data now stale -> gate denies
    loop = _loop(audit, signal=_SIGNAL, builder=builder)
    await loop.evaluate_once()
    cats = _categories(audit)
    assert "signal.candidate" in cats
    assert "gate.decision" in cats
    assert "shadow.would_submit" not in cats  # denied -> no would-submit


async def test_submit_hook_used_when_not_shadow(audit: AuditLog):
    submitted = []

    async def submit(signal, proposal, risk):
        submitted.append((signal, proposal, risk))

    loop = _loop(audit, signal=_SIGNAL, submit=submit)
    await loop.evaluate_once()
    cats = _categories(audit)
    assert "shadow.would_submit" not in cats  # live path doesn't log the shadow record
    assert len(submitted) == 1
    # Sized by the TIGHTEST rail: 2% of 100k NLV would allow 4 lots at 420 risk each,
    # but the absolute `max_loss_per_position` (1000 in the test config) allows 2.
    assert submitted[0][1].contracts == 2


# -- audit failure is terminal, not "a bad iteration" ---------------------


async def test_loop_stops_when_audit_log_fails(audit: AuditLog):
    """Regression (2026-07-21): the loop must NOT keep evaluating unrecorded.

    Previously an AuditWriteError was caught by the generic "a bad iteration must
    not kill the loop" handler, so the loop went on deciding while the black-box
    recorder was dead.
    """
    import asyncio

    audit.start()
    loop = _loop(audit, signal=_SIGNAL)
    await audit.record("seed", {})
    # A second writer steals the next seq — exactly the incident's shape.
    audit._conn.execute(
        "INSERT INTO audit_log (seq, ts, tier, category, payload, prev_hash, hash) "
        "VALUES (?, '2026-07-21T16:40:01Z', 1, 'process.start', '{}', 'x', 'y')",
        (audit.last_seq + 1,),
    )

    await asyncio.wait_for(loop.run(), timeout=5)   # returns instead of spinning

    assert audit.failed is not None
