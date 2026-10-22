"""Risk controls: kill switch, daily-loss breaker, pre-trade gate, and (later)
the continuous position watchdog."""

from argus.risk.breaker import BREACH_CATEGORY, DailyLossBreaker
from argus.risk.gate import PreTradeGate
from argus.risk.killswitch import HaltedError, KillSwitch
from argus.risk.models import EntryProposal, GateDecision, RiskSnapshot
from argus.risk.rehydrate import (
    DayRiskRehydrationError,
    DayRiskState,
    load_day_risk_state,
    record_day_pnl_anchor,
)
from argus.risk.snapshot import DailyPnl, RiskSnapshotBuilder

__all__ = [
    "KillSwitch",
    "HaltedError",
    "DailyLossBreaker",
    "BREACH_CATEGORY",
    "DayRiskState",
    "DayRiskRehydrationError",
    "load_day_risk_state",
    "record_day_pnl_anchor",
    "PreTradeGate",
    "EntryProposal",
    "RiskSnapshot",
    "GateDecision",
    "RiskSnapshotBuilder",
    "DailyPnl",
]
