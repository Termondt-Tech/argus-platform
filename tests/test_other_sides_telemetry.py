"""Every configured side is evaluated and recorded; the choice is unchanged (2026-09-26).

Until 2026-09-26 `CreditSpreadStrategy.evaluate` returned at the first qualifying
side, so on a put-candidate tick the call side was never priced or recorded, and every
call-side analysis since has had to impute it. The strategy now evaluates every side,
still CHOOSES the first qualifying one in `signal.sides` order, and hands the rest to
`signal.candidate` as `other_sides`. These tests pin all three halves: the choice, the
record, and that nothing decides on it.
"""

from __future__ import annotations

import json
import math
from datetime import time
from pathlib import Path

from argus.audit.log import AuditLog
from argus.config import SignalConfig
from argus.risk import DailyLossBreaker, KillSwitch, PreTradeGate
from argus.signal.chain import FakeChainSource
from argus.signal.models import ChainSnapshot, OptionQuote
from argus.signal.strategy import CreditSpreadStrategy
from argus.orchestrator import SignalLoop
from test_orchestrator import _StubStrategy, _fresh_builder, _config

MIDDAY = time(12, 0)


def _cfg(**over) -> SignalConfig:
    base = dict(
        sides=("put", "call"),
        target_short_delta=0.15,
        width=5.0,
        max_width=10.0,
        min_credit=0.75,
        entry_earliest=time(10, 0),
        entry_latest=time(15, 0),
        cadence_s=5.0,
        short_delta_source="feed",
    )
    base.update(over)
    return SignalConfig(**base)


def _opt(strike, right, delta, bid, ask):
    return OptionQuote(f"{right[0].upper()}{strike:g}", strike, right, delta, bid, ask)


PUTS = (
    _opt(4980, "put", -0.20, 1.80, 1.90),
    _opt(4975, "put", -0.15, 1.50, 1.60),   # short
    _opt(4970, "put", -0.10, 0.70, 0.80),   # long; credit 1.55 - 0.75 = 0.80
)
CALLS = (
    _opt(5020, "call", 0.20, 1.80, 1.90),
    _opt(5025, "call", 0.15, 1.50, 1.60),   # short
    _opt(5030, "call", 0.10, 0.70, 0.80),   # long; credit 0.80
)
THIN_PUTS = (
    _opt(4975, "put", -0.15, 0.90, 1.00),   # credit 0.95 - 0.75 = 0.20 < 0.75
    _opt(4970, "put", -0.10, 0.70, 0.80),
)
THIN_CALLS = (
    _opt(5025, "call", 0.15, 0.90, 1.00),
    _opt(5030, "call", 0.10, 0.70, 0.80),
)


def _chain(*legs):
    return ChainSnapshot("SPXW", 5000.0, tuple(legs), captured_at=0.0)


def _eval(chain, **cfg):
    return CreditSpreadStrategy(_cfg(**cfg)).evaluate(chain, MIDDAY)


# -- the choice is unchanged ---------------------------------------------------


def test_both_qualify_the_put_is_still_chosen_and_the_call_is_recorded():
    result = _eval(_chain(*PUTS, *CALLS))
    assert result.signal is not None and result.signal.side == "put"
    call = result.detail["other_sides"]["call"]
    assert call["qualified"] is True
    assert call["short"]["strike"] == 5025 and call["long"]["strike"] == 5030
    assert round(call["credit"], 2) == 0.80


def test_side_order_still_decides_which_qualifying_side_is_chosen():
    result = _eval(_chain(*PUTS, *CALLS), sides=("call", "put"))
    assert result.signal.side == "call"
    assert result.detail["other_sides"]["put"]["qualified"] is True


def test_a_rejected_other_side_records_why_it_was_rejected():
    result = _eval(_chain(*PUTS, *THIN_CALLS))
    assert result.signal.side == "put"
    call = result.detail["other_sides"]["call"]
    assert call["qualified"] is False
    assert call["reason"] == "credit_below_min"
    assert call["short_strike"] == 5025 and round(call["credit"], 2) == 0.20


def test_when_the_put_fails_the_call_is_chosen_and_the_put_rejection_rides_along():
    result = _eval(_chain(*THIN_PUTS, *CALLS))
    assert result.signal.side == "call"
    put = result.detail["other_sides"]["put"]
    assert put["qualified"] is False and put["reason"] == "credit_below_min"


def test_a_single_side_config_records_no_other_sides():
    result = _eval(_chain(*PUTS, *CALLS), sides=("put",))
    assert result.signal.side == "put"
    assert result.detail["other_sides"] == {}


def test_no_signal_keeps_the_per_side_shape_and_adds_nothing():
    result = _eval(_chain(*THIN_PUTS, *THIN_CALLS))
    assert result.signal is None
    assert set(result.detail) == {"per_side"}


# -- the record is Tier 1-safe --------------------------------------------------


def test_other_sides_is_json_safe_even_with_a_non_finite_delta_on_the_other_side():
    calls = (
        _opt(5025, "call", 0.15, 1.50, 1.60),
        _opt(5030, "call", math.nan, 0.70, 0.80),  # a NaN delta on the long leg
    )
    result = _eval(_chain(*PUTS, *calls))
    assert result.signal.side == "put"
    json.dumps(result.detail, allow_nan=False)   # the audit hash refuses NaN/inf


async def test_the_candidate_record_carries_other_sides_through_a_real_audit_write(audit: AuditLog):
    """End to end through the Tier 1 write: `signal.candidate` is written above the
    gate, so a refused record would suppress the entry — the chain must verify."""
    result = _eval(_chain(*PUTS, *THIN_CALLS))
    loop = _loop_with(audit, result)
    await loop.evaluate_once()
    row = audit._conn.execute(
        "SELECT payload FROM audit_log WHERE category = 'signal.candidate'"
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["side"] == "put"
    assert payload["other_sides"]["call"]["reason"] == "credit_below_min"
    audit.verify_chain()


async def test_a_strategy_without_other_sides_records_an_empty_dict(audit: AuditLog):
    result = _eval(_chain(*PUTS, *CALLS), sides=("put",))
    loop = _loop_with(audit, result)
    await loop.evaluate_once()
    row = audit._conn.execute(
        "SELECT payload FROM audit_log WHERE category = 'signal.candidate'"
    ).fetchone()
    assert json.loads(row["payload"])["other_sides"] == {}


def _loop_with(audit, result):
    cfg = _config()
    builder = _fresh_builder()[0]
    gate = PreTradeGate(
        config=cfg,
        audit=audit,
        killswitch=KillSwitch(audit),
        breaker=DailyLossBreaker(audit, daily_max_loss=cfg.risk.daily_max_loss),
    )
    return SignalLoop(
        strategy=_StubStrategy(result.signal, result.reason, result.detail),
        chain_source=FakeChainSource(_chain(*PUTS)),
        snapshot_builder=builder,
        gate=gate,
        audit=audit,
        config=cfg,
        clock_et=lambda: MIDDAY,
    )


# -- nothing decides on it ------------------------------------------------------


def test_nothing_outside_the_writer_and_the_recorder_names_other_sides():
    """Telemetry, never an input. Only the strategy (which builds it) and the
    orchestrator (which records it) may name the key; a reader anywhere else in
    `src/` is a decision being made on it, and needs its own design."""
    src = Path(__file__).resolve().parents[1] / "src" / "argus"
    allowed = {src / "signal" / "strategy.py", src / "orchestrator.py"}
    offenders = [
        str(p.relative_to(src)) for p in src.rglob("*.py")
        if p not in allowed and "other_sides" in p.read_text()
    ]
    assert offenders == []
