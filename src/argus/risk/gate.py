"""Pre-trade risk gate (spec §4 component notes, §6 safety rails).

A one-time check run immediately before an order is submitted — distinct from the
continuous position watchdog. It answers a single question: may this proposed
entry be sent right now? It denies (fail closed) on any of:

- kill switch engaged
- daily-loss circuit breaker tripped
- concurrent-position limit reached — counted in SPREADS
  (`RiskSnapshot.open_spread_equivalent`), because that is the unit
  `max_concurrent_positions` is stated in, while the broker reports LEGS. Working
  OPENING orders count too, per trade idea — a sent-but-unfilled order (and a
  walk's abandoned one especially) is exposure the position counts cannot see
- position too large for ANY of the three size rails: defined risk >
  `position_size_pct` * NLV (relative), defined risk > `max_loss_per_position`
  (absolute USD), or contracts > `max_contracts_per_position` (absolute count)
- the day's LOSS BUDGET would be exceeded: the proposal's defined risk is more
  than `daily_max_loss` minus today's realized loss minus the worst case of
  everything already open (`daily_loss_budget_exceeded`, 2026-09-21). The fourth
  size bound, and the one that bounds the DAY rather than one position — never
  excused by an unreadable NLV, because the ledger estimate carries it without one
- market-data or account-stream data stale beyond threshold
- unusable inputs (non-positive NLV, malformed proposal)

The three size rails are checked independently and all failing ones are reported.
`orchestrator.size_contracts` already applies the same three when choosing a size,
and that duplication is the design: the sizer chooses, the gate REFUSES. A sizing
bug, a hand-built proposal, or a future caller that skips the sizer still cannot
get past a rail here.

Every evaluation — allow or deny — is written to the hash-chained audit log at
Tier 1 and awaited before the decision is returned, so the "gate decision written
before the next action proceeds" guarantee (spec §6 DoD) holds: no order can be
sent without its authorizing decision already durable.

All failing reasons are collected (not short-circuited) so the audit record shows
every reason an entry was blocked, not just the first.
"""

from __future__ import annotations

import math
from typing import Any

from argus.alerts import AlertFn, log_alert
from argus.audit.log import AuditLog
from argus.config import Config
from argus.risk.breaker import DailyLossBreaker
from argus.risk.killswitch import KillSwitch
from argus.risk.loss_budget import LossBudget, exceeds_budget
from argus.risk.models import EntryProposal, GateDecision, RiskSnapshot
from argus.risk.side_balance import REFUSAL as SIDE_REFUSAL
from argus.risk.side_balance import admissible as side_admissible
from argus.signal.models import SPX_MULTIPLIER
from argus.staleness import is_stale

#: The gate reason for a proposal the day's loss budget cannot hold.
BUDGET_REASON = "daily_loss_budget_exceeded"

#: Tier 1, at most ONCE per trading day (the gate is built per day): the first
#: refusal on a MEASURED budget where the day has spent enough that not even one
#: ORDINARY lot fits EVEN FLAT — see `_ends_entries_for_the_day`. Two kinds
#: of budget refusal are not this event, and each is already on its own
#: `gate.decision` with the numbers: an UNREADABLE budget (transient by nature — the
#: mid-fill window), and a budget merely COMMITTED to what is open (it comes back when
#: that closes). Paging on either would be the 2026-08-05 alert-storm shape.
BUDGET_EXHAUSTED_CATEGORY = "daily_loss.budget_exhausted"


class PreTradeGate:
    def __init__(
        self,
        *,
        config: Config,
        audit: AuditLog,
        killswitch: KillSwitch,
        breaker: DailyLossBreaker,
        alert: AlertFn | None = None,
    ) -> None:
        self._audit = audit
        # Defaults to `log_alert`, never to silence — `FillConsumer`'s rule.
        self._alert = alert or log_alert
        # One page per gate, and a gate per trading day, so this IS the day's
        # rising edge with no clock of its own. Never a latch on the decision: the
        # budget is recomputed on every candidate and can grow back when an exit
        # realizes less than its worst case.
        self._budget_exhausted_announced = False
        # The costliest ORDINARY lot: `width` wide at the least credit the strategy
        # accepts. The exhaustion page measures against this, never against a
        # `max_width` candidate a ladder gap produced (2026-09-24, 14:25 ET).
        self._ordinary_lot_max_loss = (
            config.signal.width - config.signal.min_credit
        ) * SPX_MULTIPLIER
        self._daily_max_loss = config.risk.daily_max_loss
        self._killswitch = killswitch
        self._breaker = breaker
        self._max_concurrent = config.risk.max_concurrent_positions
        self._position_size_pct = config.risk.position_size_pct
        self._max_loss_per_position = config.risk.max_loss_per_position
        self._max_contracts_per_position = config.risk.max_contracts_per_position
        self._side_balance = config.risk.side_balance
        self._max_side_imbalance = config.risk.max_side_imbalance
        self._market_max_age = config.staleness.market_data_max_age_s
        self._account_max_age = config.staleness.account_stream_max_age_s

    async def evaluate(
        self, proposal: EntryProposal, snapshot: RiskSnapshot
    ) -> GateDecision:
        reasons: list[str] = []

        if self._killswitch.engaged:
            reasons.append("kill_switch_engaged")

        # Feed the breaker the latest P&L first, so evaluating the gate is itself
        # an opportunity for the breaker to trip.
        if await self._breaker.evaluate(snapshot.daily_pnl):
            reasons.append("daily_loss_breaker_tripped")

        if proposal.contracts <= 0 or not math.isfinite(proposal.max_loss) or proposal.max_loss <= 0:
            reasons.append("invalid_proposal")

        # SPREADS, not legs. `open_position_count` is the broker's leg count and
        # `max_concurrent_positions` is a spread count, so comparing them directly
        # (as this did until 2026-08-13) makes a limit of 2 permit exactly ONE
        # vertical. The count stays BROKER-derived — a position held but never
        # booked into `positions` must still consume a slot — only its units change.
        # Since 2026-08-29 `open_spread_equivalent` also adds trade ideas with an
        # OPENING order still working at the broker (Finding 6): a walk's abandoned
        # order is uncounted exposure until it dies, and it can outlive the walk.
        if snapshot.open_spread_equivalent >= self._max_concurrent:
            reasons.append("concurrent_limit")

        # THE SIDE-BALANCE RULE, re-checked (2026-09-26). The orchestrator CHOSE the
        # side by the same pure function on the same snapshot; this is the rail that
        # refuses if the choice was bypassed or wrong — the size rails' own pattern.
        # A proposal with no side (a test or pre-2026-09-26 caller; never the
        # orchestrator) is not guessed at.
        if (
            self._side_balance
            and proposal.side is not None
            and not side_admissible(
                proposal.side,
                puts=snapshot.put_spread_count,
                calls=snapshot.call_spread_count,
                unknown=snapshot.unknown_side_spread_count,
                max_imbalance=self._max_side_imbalance,
            )
        ):
            reasons.append(SIDE_REFUSAL)

        # Size rail 1 (RELATIVE): defined risk must fit within pct-of-NLV.
        if not math.isfinite(snapshot.account_nlv) or snapshot.account_nlv <= 0:
            reasons.append("invalid_account_nlv")
        elif math.isfinite(proposal.max_loss) and proposal.max_loss > (
            self._position_size_pct * snapshot.account_nlv
        ):
            reasons.append("size_cap_exceeded")

        # Size rails 2 and 3 (ABSOLUTE): independent of NLV and of each other, so
        # they are checked separately and both can appear in the reasons. Not
        # `elif`-chained onto the NLV branch above — an unreadable NLV must not
        # excuse an oversized position, which is the whole reason these two rails
        # exist as absolute numbers rather than as a percentage.
        if math.isfinite(proposal.max_loss) and proposal.max_loss > self._max_loss_per_position:
            reasons.append("position_max_loss_exceeded")
        if proposal.contracts > self._max_contracts_per_position:
            reasons.append("contract_cap_exceeded")

        # The DAY's bound (2026-09-21). Beside the absolute rails and, like them,
        # not chained onto the NLV branch: the ledger estimate of the budget needs no
        # NLV, so an unreadable account cannot excuse it. An unreadable budget is
        # ZERO remaining, never "unconstrained".
        budget = snapshot.loss_budget
        if exceeds_budget(proposal.max_loss, budget.remaining):
            reasons.append(BUDGET_REASON)

        # Staleness — fail closed: anything not within [0, threshold] (including
        # inf/NaN for a never-connected or skewed stream) counts as stale.
        if not _within(snapshot.market_data_age_s, self._market_max_age):
            reasons.append("market_data_stale")
        if not _within(snapshot.account_stream_age_s, self._account_max_age):
            reasons.append("account_stream_stale")

        decision = GateDecision(allowed=not reasons, reasons=tuple(reasons), proposal=proposal)
        await self._audit.record("gate.decision", _payload(decision, snapshot, self._limits()))
        if BUDGET_REASON in reasons and _ends_entries_for_the_day(
            proposal, budget, self._ordinary_lot_max_loss
        ):
            await self._announce_budget_exhausted(proposal, budget)
        return decision

    async def _announce_budget_exhausted(
        self, proposal: EntryProposal, budget: LossBudget
    ) -> None:
        """Once per day: the budget no longer holds a lot of what is being proposed.

        Not a halt and not a kill-switch trip — a trip is durable since 2026-09-01
        and would turn a daily limit into a permanent one. It says why entries have
        stopped, which is otherwise visible only as entries not happening.
        """
        if self._budget_exhausted_announced:
            return
        self._budget_exhausted_announced = True
        payload = {
            "proposal": {
                "symbol": proposal.symbol,
                "contracts": proposal.contracts,
                "max_loss": _num(proposal.max_loss),
            },
            "loss_budget": _budget_payload(budget),
        }
        await self._audit.record(BUDGET_EXHAUSTED_CATEGORY, payload)
        self._alert("daily_loss_budget_exhausted", payload)

    def _limits(self) -> dict[str, Any]:
        return {
            "max_concurrent_positions": self._max_concurrent,
            "position_size_pct": self._position_size_pct,
            "max_loss_per_position": self._max_loss_per_position,
            "max_contracts_per_position": self._max_contracts_per_position,
            "daily_max_loss": self._daily_max_loss,
            "side_balance": self._side_balance,
            "max_side_imbalance": self._max_side_imbalance,
        }


def _ends_entries_for_the_day(
    proposal: EntryProposal, budget: LossBudget, ordinary_lot_max_loss: float
) -> bool:
    """True when not even one ordinary lot would fit with nothing open. (2026-09-22,
    narrowed 2026-09-25)

    What the exhaustion page means: the day's own losses have used the budget, so
    closing everything open would not bring back room for one lot. Adds the worst case
    of everything open and working back onto `remaining` and asks the gate's own
    question again, about ONE lot: the smaller of the proposal's own lot and
    `ordinary_lot_max_loss` (a `width`-wide spread at `min_credit`).

    **Why not the proposal as proposed (2026-09-24, 14:25 ET).** The page fired on
    a put 7680/7670: 10 wide, because a stale 7675 quote pushed the long leg out to
    `max_width` for one tick. Its lot cost 922.5 against 866.8 flat, so it paged
    "exhausted" while a 5-wide lot of about 450 still had plenty of room, and it used
    up the day's only page. A lot that is costly only because of the strike ladder
    says nothing about the day. For the same reason the proposal is measured per LOT,
    not at its full contract count.

    **Without this the page fired on every day Argus held a full-size position**
    (2026-09-22, 10:00 ET). A 2-lot 5-wide at a 0.50 credit commits 900 of a 950
    budget, so the next candidate five seconds later was refused on the budget and
    paged "exhausted" when the budget was only tied up in the open position.
    `concurrent_limit` was refusing that candidate too.

    An unreadable budget never pages (see `BUDGET_EXHAUSTED_CATEGORY`). A `None` term
    on a known budget adds nothing, which leans toward paging.
    """
    if not budget.known:
        return False
    flat = (
        budget.remaining
        + (budget.open_worst_case or 0.0)
        + (budget.working_worst_case or 0.0)
    )
    lot = ordinary_lot_max_loss
    if proposal.contracts > 0 and math.isfinite(proposal.max_loss) and proposal.max_loss > 0:
        lot = min(lot, proposal.max_loss / proposal.contracts)
    return exceeds_budget(lot, flat)


def _within(age_s: float, threshold_s: float) -> bool:
    return not is_stale(age_s, threshold_s)


def _payload(
    decision: GateDecision, snapshot: RiskSnapshot, limits: dict[str, Any]
) -> dict[str, Any]:
    p = decision.proposal
    return {
        "allowed": decision.allowed,
        "reasons": list(decision.reasons),
        # The rail VALUES in force at decision time. A reason alone says a cap was
        # breached; this says by how much, without anyone having to reconstruct what
        # the config said that day.
        "limits": limits,
        "proposal": {
            "symbol": p.symbol,
            "contracts": p.contracts,
            "max_loss": _num(p.max_loss),
            "side": p.side,
        },
        "snapshot": {
            # Both units, because the reason string says "concurrent_limit" and the
            # limit is in spreads while the broker reports legs. Keeping the raw leg
            # count under its original name leaves existing audit analysis readable
            # across the 2026-08-13 change.
            "open_position_count": snapshot.open_position_count,   # broker LEGS
            "ledger_spread_count": snapshot.ledger_spread_count,
            "working_open_spread_count": snapshot.working_open_spread_count,
            "open_spread_equivalent": snapshot.open_spread_equivalent,
            # Per side (2026-09-26): what the side-balance rule decided on.
            "put_spread_count": snapshot.put_spread_count,
            "call_spread_count": snapshot.call_spread_count,
            "unknown_side_spread_count": snapshot.unknown_side_spread_count,
            "daily_pnl": _num(snapshot.daily_pnl),
            # False = `daily_pnl` above is a placeholder 0.0 (no day anchor, or no
            # account state), not a reading. Recorded for `tools.eod`'s G1 read;
            # decides nothing.
            "daily_pnl_anchored": snapshot.daily_pnl_anchored,
            "account_nlv": _num(snapshot.account_nlv),
            "market_data_age_s": _num(snapshot.market_data_age_s),
            "account_stream_age_s": _num(snapshot.account_stream_age_s),
            # Which estimate bound and by how much, so a refusal on
            # `daily_loss_budget_exceeded` can be re-derived from this row alone.
            "loss_budget": _budget_payload(snapshot.loss_budget),
        },
    }


def _budget_payload(b: LossBudget) -> dict[str, Any]:
    return {
        "remaining": _num(b.remaining),
        "worst_case": _opt_num(b.worst_case),
        "binding": b.binding,
        "unknown_reason": b.unknown_reason,
        "worst_case_ledger": _opt_num(b.worst_case_ledger),
        "worst_case_broker": _opt_num(b.worst_case_broker),
        "cash_today": _num(b.cash_today),
        "open_worst_case": _opt_num(b.open_worst_case),
        "working_worst_case": _opt_num(b.working_worst_case),
        "ledger_complete": b.ledger_complete,
    }


def _opt_num(x: float | None) -> float | str | None:
    return None if x is None else _num(x)


def _num(x: float) -> float | str:
    # Keep the audit payload JSON-serializable: canonical hashing forbids NaN/inf,
    # so non-finite values (a never-connected stream reports inf) are stringified.
    return x if isinstance(x, (int, float)) and math.isfinite(x) else str(x)
