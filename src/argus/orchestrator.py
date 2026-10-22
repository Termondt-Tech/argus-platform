"""Shadow-mode orchestration loop: signal -> gate -> would-submit logging.

Ties the pieces together on a cadence. In shadow mode (the default; `submit` is
None) it NEVER places an order — it records the order it *would* have placed, so
weeks of candidate quality and gate outcomes accumulate in the audit log for
evaluation (spec §10 step 1). The loop body is identical for live: the only
difference is the terminal action (log vs. `submit`), so promoting out of shadow
is a one-line wiring change, not a rewrite.

Audit tiering (spec §6): a no-signal evaluation is high-volume telemetry (Tier 2).
Once a candidate fires, everything downstream — the candidate, the gate decision,
and the would-submit record — is Tier 1, because that is the shadow-mode evidence
we can least afford to lose.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, time
from typing import Any

from argus.audit.log import AuditLog, AuditWriteError
from argus.clock import et_time_of_day, et_today
from argus.config import Config
from argus.marketcalendar import MarketCalendar
from argus.risk.gate import PreTradeGate
from argus.risk.loss_budget import contracts_within_budget
from argus.risk.models import EntryProposal, RiskSnapshot
from argus.risk.side_balance import rank as side_rank
from argus.risk.snapshot import RiskSnapshotBuilder
from argus.shadow.outcome import markable_payload
from argus.signal.chain import ChainSource
from argus.signal.ladder import LadderRecorder
from argus.signal.models import SpreadSignal, StrategyResult, payload_number
from argus.signal.regime import compute_regime
from argus.signal.strategy import RegimeSnapshot, Strategy, classify_iv_regime

logger = logging.getLogger("argus.orchestrator")

# submit(signal, proposal, risk) -> awaitable. None => shadow mode.
SubmitFn = Callable[[SpreadSignal, EntryProposal, RiskSnapshot], Awaitable[None]]


def size_contracts(
    nlv: float,
    pct: float,
    max_loss_per_contract: float,
    *,
    max_loss_per_position: float,
    max_contracts_per_position: int,
    loss_budget: float,
    regime_multiplier: float = 1.0,
) -> int:
    """Contracts fitting inside ALL THREE size rails AND the day's loss budget —
    the tightest one governs.

    Four independent bounds, and the minimum wins:

      1. `pct` of NLV (relative; scales down with a drawdown)
      2. `max_loss_per_position` USD (absolute)
      3. `max_contracts_per_position` (absolute)
      4. `loss_budget` USD — what `daily_max_loss` has left once today's realized
         loss and the worst case of everything open are counted
         (`RiskSnapshot.loss_budget.remaining`, 2026-09-21). The first three bound
         ONE position; this one bounds the DAY, which is what makes 1-lot re-entries
         after a loss automatic and stops a second full-size position being opened
         on top of a spent day. A non-finite budget is zero lots.
         → docs/decisions/2026-09-21-loss-budget-sizing.md

    Bounds 2 and 3 are not derived from each other and must both be applied: a
    dollar cap and a contract cap disagree about which trade is too big the moment
    credit or strike distance moves, so which one binds depends on the trade in
    hand. Bound 1 stays as the additional sanity bound it always was — it is the
    only one that shrinks when the account does.

    The `max(1, ...)` floor is unchanged and deliberate: a candidate is always
    sized to at least one lot so it is still EVALUATED and recorded, even when no
    lot fits. `PreTradeGate` is the enforcer — it re-checks all three rails against
    the resulting proposal and denies. Sizing chooses; the gate refuses.

    The caps and the budget are keyword-only and have no defaults, so a caller
    cannot silently inherit an unlimited one. A `float('inf')` default here would be a fail-OPEN
    default on a risk rail.

    `regime_multiplier` scales bound 1 and NOTHING ELSE — it is deliberately NOT a
    fourth rail. It multiplies `pct` inside the relative bound, so the rail count
    stays three, the tightest-wins invariant is untouched, and `PreTradeGate` still
    re-checks exactly the same three things it always did. Config validation caps
    every regime multiplier at 1.0, so this can only ever move the relative bound
    DOWN; a value above 1 could at most restore `pct` to its unscaled level before
    bounds 2 and 3 clamp it anyway.

    It is the one keyword here that DOES default, and the asymmetry is the point:
    the caps default to nothing because their fail-open direction is unlimited
    risk, whereas this defaults to 1.0, which is "no scaling" — the fail-safe
    direction, and the same default `CreditSpreadStrategy.size_multiplier` returns
    when regime gating is unconfigured.
    """
    if max_loss_per_contract <= 0:
        return 0
    by_pct = int((pct * regime_multiplier * nlv) // max_loss_per_contract)
    by_dollars = int(max_loss_per_position // max_loss_per_contract)
    by_budget = contracts_within_budget(loss_budget, max_loss_per_contract)
    return max(1, min(by_pct, by_dollars, max_contracts_per_position, by_budget))


class SignalLoop:
    def __init__(
        self,
        *,
        strategy: Strategy,
        chain_source: ChainSource,
        snapshot_builder: RiskSnapshotBuilder,
        gate: PreTradeGate,
        audit: AuditLog,
        config: Config,
        clock_et: Callable[[], time] | None = None,
        today_et: Callable[[], date] | None = None,
        submit: SubmitFn | None = None,
        attempted: Callable[[SpreadSignal], bool] | None = None,
    ) -> None:
        self._strategy = strategy
        self._chain = chain_source
        self._builder = snapshot_builder
        self._gate = gate
        self._audit = audit
        self._pct = config.risk.position_size_pct
        self._max_loss_per_position = config.risk.max_loss_per_position
        self._max_contracts_per_position = config.risk.max_contracts_per_position
        self._cadence = config.signal.cadence_s
        self._clock_et = clock_et or et_time_of_day
        self._today_et = today_et or et_today
        # Regime estimation needs the settlement time, and a half-day settles at
        # 13:00. Built from `config.market` rather than injected because it is pure
        # scheduling data the loop already has — `MarketCalendar` reads nothing and
        # depends on nothing, so this adds no wiring for callers.
        self._calendar = MarketCalendar.from_config(config.market)
        # None until `[regime]` is configured. While it is None the regime is still
        # COMPUTED and LOGGED — only the bucket tag is unavailable, because a bucket
        # is a statement about thresholds that do not exist yet.
        self._regime_cfg = config.regime
        self._submit = submit
        # The side-balance rule (2026-09-26): which of a tick's qualifying sides to
        # propose. Off is the pre-2026-09-26 path exactly — the strategy's first side.
        self._side_balance = config.risk.side_balance
        self._max_side_imbalance = config.risk.max_side_imbalance
        self._sides_order = tuple(config.signal.sides)
        # The entry pipeline's own dedup, READ-ONLY; nothing is ever attempted in
        # shadow, where no pipeline exists.
        self._attempted = attempted or (lambda _signal: False)
        # The strike ladder, once a minute (2026-09-29, the objective audit's P9).
        # Telemetry only: it returns nothing, and a failure inside it is its own.
        self._ladder = LadderRecorder(audit)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def _regime_payload(self, regime: RegimeSnapshot) -> dict[str, object]:
        """The regime block attached to every per-tick record.

        `regime_bucket` is None until `[regime]` is configured — there is no
        threshold to bucket against, and emitting a guessed tag would put a number
        in the calibration dataset that no config ever produced.
        """
        bucket = (
            classify_iv_regime(regime.atm_iv, self._regime_cfg)
            if self._regime_cfg is not None
            else None
        )
        return {"regime": {**regime.to_payload(), "regime_bucket": bucket}}

    async def evaluate_once(self) -> None:
        chain = self._chain.snapshot()
        if chain is None:
            # No chain means no strip to estimate from, so this tick carries no
            # regime. It is the one per-tick record that cannot, and saying so
            # honestly beats emitting an all-null regime block that would read as
            # "estimated, and it came out empty".
            self._audit.telemetry("signal.skip", {"reason": "no_chain"})
            return

        now_et = self._clock_et()
        today = self._today_et()
        # Computed BEFORE the strategy is consulted and attached to whatever record
        # this tick produces — including the no-candidate ones. That is the whole
        # point of this telemetry: a distribution sampled only from ticks that
        # already produced a candidate is conditioned on the conditions the current
        # thresholds happen to like, which is precisely the wrong population to
        # calibrate a future gate from. `signal.none` (Tier 2) and `signal.candidate`
        # (Tier 1) between them cover every tick that has a chain.
        regime = compute_regime(
            chain,
            now_et=now_et,
            today=today,
            close_et=self._calendar.close_time(today),
        )
        regime_block = self._regime_payload(regime)
        # Before the strategy, so a minute's row is written whatever the tick then
        # decides — including on blackout days and outside the entry window, which
        # are exactly the rows the flatten and release-day questions need.
        self._ladder.maybe_record(chain, regime, today=today, now_et=now_et)

        result = self._strategy.evaluate(chain, now_et, regime)
        if result.signal is None:
            # High-volume no-op — Tier 2. Carries the near-miss (selected strikes,
            # deltas, the credit that would have been paid, and which threshold
            # rejected it) so a multi-week shadow run is a tuning dataset rather
            # than a record that nothing happened.
            self._audit.telemetry(
                "signal.none",
                {
                    # Through the shared guard like every other number on its way
                    # into the log (2026-09-18). This was the ONE raw numeric left
                    # on an entry record: the live adapter cannot produce a
                    # non-finite spot, but `Quote.mid` lets NaN/inf through (a NaN
                    # fails `ask <= 0.0`) from any other port, and a non-finite
                    # value here makes Tier 2 drop the WHOLE row — the near miss
                    # and its quoted spread with it. Record only; the one thing
                    # that decides on this spot, the entry drift breaker, already
                    # fails closed on a non-finite reference.
                    "underlying_price": payload_number(chain.underlying_price),
                    "reason": result.reason,
                    **regime_block,
                    **result.detail,
                },
            )
            return
        # A candidate fired. Everything below is Tier 1 shadow evidence.
        risk = self._builder.build()
        signal, choice = self._choose(result, risk)
        contracts = size_contracts(
            risk.account_nlv,
            self._pct,
            signal.max_loss_per_contract,
            max_loss_per_position=self._max_loss_per_position,
            max_contracts_per_position=self._max_contracts_per_position,
            loss_budget=risk.loss_budget.remaining,
            regime_multiplier=self._strategy.size_multiplier(regime),
        )
        proposal = EntryProposal(
            symbol=signal.describe(),
            contracts=contracts,
            max_loss=contracts * signal.max_loss_per_contract,
            side=signal.side,
        )
        await self._audit.record(
            "signal.candidate",
            {
                **signal.to_payload(),
                "contracts": contracts,
                # The sides NOT proposed on this tick, qualifying or not
                # (2026-09-26). Recorded, never decided on; `{}` for a single-side
                # config or a strategy that does not supply it.
                "other_sides": _other_sides_for(signal, result),
                # What the side-balance rule saw and did — recorded whether or not
                # the rule is on, so the same-side share of the book is measurable
                # either way (the 2026-09-26 forward note's D2).
                "book": {
                    "puts": risk.put_spread_count,
                    "calls": risk.call_spread_count,
                    "unknown": risk.unknown_side_spread_count,
                    "side_balance": self._side_balance,
                    "max_side_imbalance": self._max_side_imbalance,
                    "choice": choice,
                },
                **regime_block,
            },
        )

        decision = await self._gate.evaluate(proposal, risk)
        if not decision.allowed:
            # The gate already recorded gate.decision (Tier 1) with the reasons.
            return

        if self._submit is None:
            # SHADOW MODE: never route an order. Record what we would have sent.
            # The payload comes from `markable_payload` rather than being spelled
            # out here because `entry.dry_run` has to write the SAME shape for the
            # marker to read the two as one union — see that function.
            await self._audit.record("shadow.would_submit", markable_payload(signal, proposal))
        else:
            await self._submit(signal, proposal, risk)

    def _choose(
        self, result: StrategyResult, risk: RiskSnapshot
    ) -> tuple[SpreadSignal, str]:
        """Which qualifying side to propose, and why (the `book.choice` label).

        Rule off: the strategy's own first side, exactly as before (`"off"`).

        Rule on: the admissible sides, most balancing first and ties in
        `signal.sides` order (`risk.side_balance.rank`), skipping any the entry
        pipeline already attempted today — without that skip a repeated put hid a
        qualifying call for the whole tick. `"first"` when the strategy's own first
        side won, `"balance"` when another did. With no admissible side, the
        strategy's first side goes forward (`"refused"`) and `PreTradeGate` refuses
        it on `side_imbalance_exceeded` with the full record; with every admissible
        side already attempted, the best one goes forward (`"attempted"`) and the
        entry pipeline refuses it as today. Either way nothing new is sent.
        """
        first = result.signal
        assert first is not None
        if not self._side_balance:
            return first, "off"
        ranked = side_rank(
            (first, *result.alternatives),
            side_of=lambda s: s.side,
            puts=risk.put_spread_count,
            calls=risk.call_spread_count,
            unknown=risk.unknown_side_spread_count,
            max_imbalance=self._max_side_imbalance,
            sides_order=self._sides_order,
        )
        for candidate in ranked:
            if not self._attempted(candidate):
                return candidate, ("first" if candidate is first else "balance")
        if ranked:
            return ranked[0], "attempted"
        return first, "refused"

    async def run(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            try:
                await self.evaluate_once()
            except AuditWriteError:
                # NOT a bad iteration. The audit log is the precondition for
                # acting at all, so continuing would mean evaluating and
                # deciding with no record of either — the loop looking healthy
                # while the black box is dead. Stop; the kill switch has already
                # been engaged by AuditLog's failure latch.
                logger.critical(
                    "audit log unwritable — stopping the signal loop. No further "
                    "evaluation will occur until the process is restarted."
                )
                self._stop.set()
                break
            except Exception:  # a bad iteration must not kill the loop
                logger.exception("signal loop iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._cadence)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="signal-loop")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None


def _other_sides_for(chosen: SpreadSignal, result: StrategyResult) -> dict[str, Any]:
    """`other_sides` relative to the side actually PROPOSED (2026-09-26).

    The strategy records the other sides relative to ITS first choice; when the
    side-balance rule proposes a different side, that first choice becomes an other
    side and the proposed one stops being one.
    """
    other = dict(result.detail.get("other_sides", {}))
    other.pop(chosen.side, None)
    first = result.signal
    if first is not None and chosen is not first:
        other[first.side] = {"qualified": True, **first.to_payload()}
    return other
