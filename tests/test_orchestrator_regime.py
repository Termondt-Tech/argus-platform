"""SignalLoop: per-tick regime telemetry and the size-multiplier wiring.

The central claim under test is the one this round was built to make true: the
regime is recorded on EVERY tick that has a chain, not only on ticks that
produced a candidate. A distribution sampled only from candidate-producing ticks
is conditioned on the conditions the current thresholds already like, which is
the wrong population to calibrate a future gate from.
"""

from __future__ import annotations

import json
import math
from datetime import date, time

import pytest
from support import config_dict, loss_budget_source

from argus.audit.log import AuditLog
from argus.config import Config
from argus.account.fake import FakeAccountState
from argus.marketdata import FakeMarketData
from argus.orchestrator import SignalLoop, size_contracts
from argus.risk import DailyLossBreaker, KillSwitch, PreTradeGate
from argus.risk.snapshot import DailyPnl, RiskSnapshotBuilder
from argus.signal.chain import FakeChainSource
from argus.signal.models import ChainSnapshot, OptionQuote, SpreadSignal, StrategyResult

TODAY = date(2026, 8, 4)

_SIGNAL = SpreadSignal(
    underlying="SPXW",
    side="put",
    short_leg=OptionQuote("P4975", 4975, "put", -0.15, 1.50, 1.60),
    long_leg=OptionQuote("P4970", 4970, "put", -0.10, 0.70, 0.80),
    credit=0.80,
    width=5.0,
    captured_at=0.0,
)


def _strip() -> ChainSnapshot:
    """A strip wide enough to be estimable — the loop must produce a real reading,
    not just a well-shaped null."""
    options = []
    for strike in range(4900, 5101, 5):
        for right in ("put", "call"):
            intrinsic = max(0.0, (5000.0 - strike) if right == "put" else (strike - 5000.0))
            px = intrinsic + 12.0 * math.exp(-((strike - 5000.0) ** 2) / (2 * 60.0**2))
            options.append(
                OptionQuote(f"{right[0].upper()}{strike}", float(strike), right, 0.1,
                            max(px - 0.05, 0.0), px + 0.05)
            )
    return ChainSnapshot("SPXW", 5000.0, tuple(options), 0.0)


class _StubStrategy:
    def __init__(self, signal, reason=None, detail=None, multiplier=1.0):
        self._result = StrategyResult(signal, reason, detail or {})
        self._multiplier = multiplier
        self.seen_regimes = []

    def evaluate(self, chain, now_et, regime=None):
        self.seen_regimes.append(regime)
        return self._result

    def size_multiplier(self, regime):
        return self._multiplier


def _builder():
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    account = FakeAccountState()
    account.set_net_liq(100_000)
    return RiskSnapshotBuilder(
        market, account, DailyPnl(anchor_nlv=100_000), reference_symbols=("SPX",), loss_budget=loss_budget_source()
    )


def _loop(audit, strategy, *, chain=None, cfg=None, submit=None, at=time(12, 0)):
    cfg = cfg or Config.from_dict(config_dict())
    gate = PreTradeGate(
        config=cfg,
        audit=audit,
        killswitch=KillSwitch(audit),
        breaker=DailyLossBreaker(audit, daily_max_loss=cfg.risk.daily_max_loss),
    )
    return SignalLoop(
        strategy=strategy,
        chain_source=FakeChainSource(_strip() if chain is None else chain),
        snapshot_builder=_builder(),
        gate=gate,
        audit=audit,
        config=cfg,
        clock_et=lambda: at,
        today_et=lambda: TODAY,
        submit=submit,
    )


def _records(audit, category):
    rows = audit._conn.execute(
        "SELECT payload FROM audit_log WHERE category = ? ORDER BY seq", (category,)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


# --------------------------------------------------------------------------
# Every tick, not just the ones that fire
# --------------------------------------------------------------------------


async def test_no_candidate_tick_still_records_the_regime(audit: AuditLog):
    """The whole point of this round: an unbiased sample needs the ticks that
    produced nothing."""
    audit.start()
    loop = _loop(audit, _StubStrategy(None, reason="credit_below_min"))
    await loop.evaluate_once()
    await audit.aclose()

    (record,) = _records(audit, "signal.none")
    regime = record["regime"]
    assert regime["today"] == TODAY.isoformat()
    assert regime["atm_iv"] is not None
    assert regime["minutes_to_settlement"] == pytest.approx(240.0)


async def test_candidate_tick_records_the_regime_too(audit: AuditLog):
    audit.start()
    loop = _loop(audit, _StubStrategy(_SIGNAL))
    await loop.evaluate_once()
    await audit.aclose()

    (record,) = _records(audit, "signal.candidate")
    assert record["regime"]["today"] == TODAY.isoformat()
    assert record["regime"]["atm_iv"] is not None


async def test_regime_is_actually_handed_to_the_strategy(audit: AuditLog):
    """Asserted at the strategy, not only in the record — a loop that logged a
    regime but evaluated without one would pass a record-only test while leaving
    the blackout permanently disarmed."""
    strategy = _StubStrategy(None, reason="credit_below_min")
    audit.start()
    loop = _loop(audit, strategy)
    await loop.evaluate_once()

    assert len(strategy.seen_regimes) == 1
    assert strategy.seen_regimes[0] is not None
    assert strategy.seen_regimes[0].today == TODAY


async def test_no_chain_tick_carries_no_regime_block(audit: AuditLog):
    """The one per-tick record that cannot carry a reading. An all-null regime
    block here would read as "estimated, came out empty" rather than "not
    estimated"."""
    audit.start()
    loop = _loop(audit, _StubStrategy(None), chain=None)
    loop._chain = FakeChainSource(None)
    await loop.evaluate_once()
    await audit.aclose()

    (record,) = _records(audit, "signal.skip")
    assert "regime" not in record
    assert record["reason"] == "no_chain"


async def test_bucket_is_null_while_regime_config_is_absent(audit: AuditLog):
    """No thresholds => no bucket. Emitting a guessed tag would put a value in the
    calibration dataset that no config ever produced."""
    audit.start()
    loop = _loop(audit, _StubStrategy(None, reason="credit_below_min"))
    await loop.evaluate_once()
    await audit.aclose()

    (record,) = _records(audit, "signal.none")
    assert record["regime"]["regime_bucket"] is None


async def test_bucket_is_populated_once_regime_config_exists(audit: AuditLog):
    cfg = Config.from_dict(
        config_dict(
            regime={
                "atm_iv_low_max": 0.15,
                "atm_iv_high_min": 0.30,
                "max_downside_skew_for_puts": 0.002,
                "max_upside_skew_for_calls": 0.002,
                "size_multiplier_low": 1.0,
                "size_multiplier_mid": 0.75,
                "size_multiplier_high": 0.5,
            }
        )
    )
    audit.start()
    loop = _loop(audit, _StubStrategy(None, reason="credit_below_min"), cfg=cfg)
    await loop.evaluate_once()
    await audit.aclose()

    (record,) = _records(audit, "signal.none")
    assert record["regime"]["regime_bucket"] in ("low", "mid", "high")


async def test_unestimable_regime_still_writes_a_tier1_candidate(audit: AuditLog):
    """A NaN reading serializes as null. If it leaked as NaN the Tier 1 write would
    be refused and that halts trading — an unreadable chain must never do that."""
    thin = ChainSnapshot("SPXW", 5000.0, (), 0.0)
    audit.start()
    loop = _loop(audit, _StubStrategy(_SIGNAL), chain=thin)
    await loop.evaluate_once()
    await audit.aclose()

    (record,) = _records(audit, "signal.candidate")
    assert record["regime"]["atm_iv"] is None
    assert record["regime"]["today"] == TODAY.isoformat()


async def test_would_submit_payload_is_untouched(audit: AuditLog):
    """`shadow.would_submit` is what `tools.mark` reads and dedups. Adding a regime
    block there would change the shape of weeks of accumulated shadow evidence, so
    the regime is deliberately kept OUT of this one record."""
    audit.start()
    loop = _loop(audit, _StubStrategy(_SIGNAL))
    await loop.evaluate_once()
    await audit.aclose()

    (record,) = _records(audit, "shadow.would_submit")
    assert set(record) == {"proposal", "signal"}
    assert "regime" not in record


# --------------------------------------------------------------------------
# The size multiplier: scales rail 1, never becomes rail 4
# --------------------------------------------------------------------------


def test_multiplier_scales_the_percentage_rail():
    base = size_contracts(
        100_000, 0.02, 420.0,
        max_loss_per_position=1_000_000.0, max_contracts_per_position=1_000, loss_budget=1_000_000.0,
    )
    halved = size_contracts(
        100_000, 0.02, 420.0,
        max_loss_per_position=1_000_000.0, max_contracts_per_position=1_000, loss_budget=1_000_000.0,
        regime_multiplier=0.5,
    )
    assert base == 4 and halved == 2


def test_multiplier_defaults_to_no_scaling():
    """The one keyword here that defaults, because 1.0 is the fail-SAFE direction —
    unlike the caps, whose fail-open direction is unlimited risk."""
    assert size_contracts(
        100_000, 0.02, 420.0,
        max_loss_per_position=1_000_000.0, max_contracts_per_position=1_000, loss_budget=1_000_000.0,
    ) == 4


def test_multiplier_cannot_loosen_the_absolute_caps():
    """The rail count stays THREE. Even a nonsensical >1 multiplier cannot push the
    size past the dollar or contract cap — `PreTradeGate` re-checks the same three
    rails it always did."""
    assert size_contracts(
        100_000, 0.02, 420.0,
        max_loss_per_position=1250.0, max_contracts_per_position=1_000, loss_budget=1_000_000.0,
        regime_multiplier=10.0,
    ) == 2
    assert size_contracts(
        100_000, 0.02, 420.0,
        max_loss_per_position=1_000_000.0, max_contracts_per_position=2, loss_budget=1_000_000.0,
        regime_multiplier=10.0,
    ) == 2


def test_multiplier_keeps_the_one_lot_floor():
    """A candidate is still EVALUATED and recorded even when no lot fits; the gate
    is what denies it."""
    assert size_contracts(
        100_000, 0.02, 420.0,
        max_loss_per_position=1_000_000.0, max_contracts_per_position=1_000, loss_budget=1_000_000.0,
        regime_multiplier=0.0001,
    ) == 1


async def test_loop_applies_the_strategy_multiplier(audit: AuditLog):
    """End-to-end: the loop asks the strategy and the answer reaches the proposal."""
    strategy = _StubStrategy(_SIGNAL, multiplier=0.25)
    audit.start()
    loop = _loop(audit, strategy)
    await loop.evaluate_once()
    await audit.aclose()

    full = _StubStrategy(_SIGNAL, multiplier=1.0)
    (scaled_record,) = _records(audit, "signal.candidate")
    assert scaled_record["contracts"] < 100
    assert full.size_multiplier(None) == 1.0
