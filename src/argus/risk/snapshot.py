"""RiskSnapshot assembly.

Combines the three live sources into the single `RiskSnapshot` the pre-trade gate
consumes (spec §4 — the gate is a pure decision function over an already-fetched
snapshot):

- account state (positions -> open count, NLV) from the AlertStreamer cache,
- market-data freshness from the DXLink feed,
- day P&L from a start-of-day NLV anchor.

**Day P&L is total (realized + unrealized)**, computed as the change in net
liquidating value since a start-of-day anchor. That is the correct input for the
daily-loss circuit breaker: an open 0DTE spread moving against you is a live loss,
and a realized-only measure would let unrealized drawdown balloon without tripping
the breaker.

`build()` is a pure read — it never mutates the anchor. Absent/stale inputs are
encoded as +inf ages and a 0.0 NLV so the gate's own fail-closed logic (invalid
NLV, stale data) makes the decision; the builder does not pre-judge.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping

from argus.account.port import AccountStatePort
from argus.marketdata.port import MarketDataPort
from argus.occ import occ_right
from argus.risk.loss_budget import LossBudgetSource
from argus.risk.models import RiskSnapshot
from argus.risk.side_balance import CALL, PUT, UNKNOWN, count_by_side


class DailyPnl:
    """Day P&L as NLV change since a start-of-day anchor.

    The anchor is set once at session start (`anchor_to`). On a same-day restart
    it is REHYDRATED through `anchor_nlv` from today's `day_pnl.anchored` audit
    record (`risk.rehydrate`), so the day's prior P&L isn't lost — the same
    rehydration concern as the daily-loss breaker's tripped state, and wired at
    the same seam (see `DailyLossBreaker.initially_tripped`). Constructing this
    bare on a same-day restart re-anchors to the CURRENT NLV, which after a loss
    reads the day's damage as ~0 and disarms the breaker.

    `reset()` clears it for a new day. Until anchored, day P&L reads 0.0 (no loss).
    """

    def __init__(self, anchor_nlv: float | None = None) -> None:
        self._anchor = anchor_nlv

    @property
    def anchored(self) -> bool:
        return self._anchor is not None

    @property
    def anchor(self) -> float | None:
        return self._anchor

    def anchor_to(self, nlv: float) -> None:
        self._anchor = nlv

    def value(self, current_nlv: float) -> float:
        if self._anchor is None:
            return 0.0
        return current_nlv - self._anchor

    def reset(self) -> None:
        self._anchor = None


class RiskSnapshotBuilder:
    def __init__(
        self,
        market: MarketDataPort,
        account: AccountStatePort,
        day_pnl: DailyPnl,
        *,
        loss_budget: LossBudgetSource,
        reference_symbols: tuple[str, ...] = (),
        ledger_spread_count: Callable[[], int] = lambda: 0,
        working_open_spread_count: Callable[[], int] = lambda: 0,
        ledger_spreads_by_side: Callable[[], Mapping[str, int]] = lambda: {},
        working_open_spreads_by_side: Callable[[], Mapping[str, int]] = lambda: {},
    ) -> None:
        self._market = market
        self._account = account
        self._day_pnl = day_pnl
        # REQUIRED, no default (2026-09-21): the day's loss budget is a size rail,
        # and a default source would be one holding an EMPTY ledger — the whole cap
        # available whatever the day has already lost.
        self._loss_budget = loss_budget
        # Read through a callable because the watchdog that owns the scan is built
        # AFTER this builder, and because the count must be current at each
        # `build()`. Defaults to 0, which leaves the broker-derived count governing
        # for every caller that supplies nothing — shadow included.
        self._ledger_spread_count = ledger_spread_count
        # Same late-bound shape and the same reason: the watchdog that polls the
        # broker for working orders is built after this, and the count must be
        # current at each `build()`. Defaults to 0 — shadow, and every caller that
        # predates this — which leaves the concurrency cap counting positions only,
        # exactly as before.
        self._working_open_spread_count = working_open_spread_count
        # The same two counts SPLIT BY SIDE (2026-09-26), for the side-balance rule.
        # Same late-bound shape, same owner (the watchdog), same `{}` in shadow. The
        # broker's half needs no source: it is read off the account snapshot below.
        self._ledger_spreads_by_side = ledger_spreads_by_side
        self._working_open_spreads_by_side = working_open_spreads_by_side
        # If given, market-data freshness is the WORST age among these symbols
        # (fail closed on the least-fresh leg we care about). Otherwise the feed
        # heartbeat (`last_event_age`) stands in as the coarse liveness check.
        self._reference_symbols = reference_symbols

    def build(self) -> RiskSnapshot:
        account_stream_age = self._account.age()
        acct = self._account.snapshot()
        broker_legs = _broker_legs_by_side(acct.positions if acct is not None else ())
        by_side = count_by_side(
            broker_legs=broker_legs,
            ledger_ideas=self._ledger_spreads_by_side(),
            working_ideas=self._working_open_spreads_by_side(),
        )
        if acct is not None and acct.net_liquidating_value is not None:
            nlv = acct.net_liquidating_value
            open_count = acct.open_position_count
            daily = self._day_pnl.value(nlv)
            daily_is_reading = self._day_pnl.anchored
        else:
            # No usable account state — surface it as unusable and let the gate
            # fail closed (invalid NLV + stale account stream).
            nlv = 0.0
            open_count = 0
            daily = 0.0
            # A placeholder, whatever the anchor says: there is no NLV to measure
            # against it. Reported as such so `/status` cannot render this 0.00 as a
            # reading beside an anchored flag (2026-09-18).
            daily_is_reading = False
        return RiskSnapshot(
            open_position_count=open_count,
            daily_pnl=daily,
            account_nlv=nlv,
            market_data_age_s=self._market_age(),
            account_stream_age_s=account_stream_age,
            # The broker estimate is counted only on an anchored READING — the
            # no-account branch above is a placeholder and passes False.
            loss_budget=self._loss_budget.evaluate(
                daily_pnl=daily, daily_pnl_anchored=daily_is_reading
            ),
            ledger_spread_count=self._ledger_spread_count(),
            working_open_spread_count=self._working_open_spread_count(),
            # Said out loud, because both placeholders answer 0.0: an unanchored
            # `DailyPnl.value`, and the no-account branch above.
            daily_pnl_anchored=daily_is_reading,
            put_spread_count=by_side[PUT],
            call_spread_count=by_side[CALL],
            unknown_side_spread_count=by_side[UNKNOWN],
        )

    def _market_age(self) -> float:
        if self._reference_symbols:
            return max(self._market.age(s) for s in self._reference_symbols)
        return self._market.last_event_age()

    def anchor_day_pnl_if_possible(self) -> bool:
        """Anchor day P&L to the current account NLV, if account state is available.

        Called once at session start by the orchestration loop. Returns True if an
        anchor was set. Does nothing if already anchored or no NLV is known yet.

        **The already-anchored guard is what makes a same-day restart correct**, and
        it needs no special case: the supervisor constructs `DailyPnl` with today's
        rehydrated anchor, so this arrives already anchored and returns False. The
        seam is the constructor. Never weaken this guard to "re-anchor if the
        anchor looks stale" — anchoring to a post-loss NLV is precisely the bug
        that let a restart hand back a spent loss budget (2026-08-20).
        """
        if self._day_pnl.anchored:
            return False
        acct = self._account.snapshot()
        if acct is None or acct.net_liquidating_value is None:
            return False
        self._day_pnl.anchor_to(acct.net_liquidating_value)
        return True


def _broker_legs_by_side(positions: Iterable) -> dict[str, int]:
    """Non-flat broker LEGS per right (symbols, not contracts), for the per-side count.

    A symbol whose right cannot be read is counted under `UNKNOWN`, never on a side
    (2026-09-27 review): it is held risk on an unknown side, and the side rule assumes
    the worst side for it. Counting it on BOTH sides, as the first cut did, is neutral
    for a rule on `puts − calls` — it changed no verdict.
    """
    legs = {PUT: 0, CALL: 0, UNKNOWN: 0}
    for p in positions:
        if p.signed_quantity == 0:
            continue
        right = occ_right(p.symbol)
        legs[UNKNOWN if right is None else right] += 1
    return legs
