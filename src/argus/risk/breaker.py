"""Daily max-loss circuit breaker (spec §6, CLAUDE.md non-negotiable).

Halts new entries automatically the instant the daily loss threshold is breached
— no human step required to trigger it. It is a distinct rail from the kill
switch: the kill switch is manual/phone (and, optionally, the loop-stall
escalation), while this trips itself off P&L.

The trip is **latched**: once tripped it stays tripped until an explicit `reset()`
at the next trading day's session start. A recovering P&L must NOT silently
re-arm trading — the day's risk budget is spent.

`evaluate()` is the single place the trip decision lives, so it can be driven both
by the pre-trade gate (at proposal time) and, later, by the position watchdog
(continuously). Feeding it the latest P&L is what makes it "not wait to be
noticed."
"""

from __future__ import annotations

from argus.audit.log import AuditLog

#: The Tier 1 category a trip writes — and the ONLY durable record that the day's
#: loss budget is spent. `risk.rehydrate` reads it back to reconstruct
#: `initially_tripped` on a same-day restart, so the literal is defined here, at
#: the writer, and imported by that reader rather than re-spelled.
#:
#: `status.py` deliberately does NOT read it. A row outlives the process that
#: wrote it, so deriving "is the breaker tripped?" from one reported a halt that
#: was not in force; `/status` reads the live breaker object instead. Rehydration
#: is the one legitimate reader — reconstructing the latch is exactly what a
#: durable record is for.
BREACH_CATEGORY = "daily_loss.breach"


class DailyLossBreaker:
    def __init__(
        self,
        audit: AuditLog,
        *,
        daily_max_loss: float,
        initially_tripped: bool = False,
    ) -> None:
        # daily_max_loss is a positive dollar magnitude; the breaker trips when the
        # day's loss reaches it (i.e. daily_pnl <= -daily_max_loss).
        self._audit = audit
        self._threshold = daily_max_loss
        # `initially_tripped` is the rehydration seam, and it is WIRED: on a
        # same-day restart `Application._run_session_day` reads back today's
        # `daily_loss.breach` (`risk.rehydrate.load_day_risk_state`) and constructs
        # the breaker already tripped, so a crash, a deploy, or a manual restart
        # cannot re-arm a day whose budget is spent. It was an unwired seam until
        # 2026-08-20 — the trip was latched only in memory, and every same-day
        # restart silently reset the day's loss budget.
        # → docs/decisions/2026-08-20-daily-loss-breaker-restart-rehydration.md
        self._tripped = initially_tripped

    @property
    def tripped(self) -> bool:
        return self._tripped

    async def evaluate(self, daily_pnl: float) -> bool:
        """Feed the latest signed daily P&L; trip (once) if the loss breaches.

        Returns the current tripped state. Records a Tier 1 `daily_loss.breach`
        exactly once, on the transition into the tripped state.
        """
        if not self._tripped and daily_pnl <= -self._threshold:
            self._tripped = True
            await self._audit.record(
                BREACH_CATEGORY,
                {"daily_pnl": daily_pnl, "threshold": self._threshold},
            )
        return self._tripped

    def reset(self) -> None:
        """Re-arm for a new trading day. Call at session start, never intraday."""
        self._tripped = False
