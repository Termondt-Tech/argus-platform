"""Value types for the pre-trade risk gate.

The gate is deliberately decoupled from the (not-yet-built) broker layer: it
consumes a `RiskSnapshot` — a plain, already-fetched picture of current risk
state — rather than reaching out to fetch anything itself. That keeps it a pure,
synchronous-to-reason-about decision function and makes every branch testable
without a broker. When the broker layer lands, its job is to *produce* the
snapshot; the gate's logic doesn't change.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from argus.risk.loss_budget import LossBudget

#: Legs in one Argus position. Every order Argus can submit is a two-leg vertical —
#: `execution/build.py` has no code path that emits anything else (the
#: single-legging guardrail), so this is a structural property of the system rather
#: than an assumption about the current strategy. It would stop being one the day
#: Argus learns a structure with a different leg count, and this constant plus
#: `RiskSnapshot.open_spread_equivalent` is where that would have to be revisited.
LEGS_PER_VERTICAL = 2


@dataclass(frozen=True)
class EntryProposal:
    """A proposed credit-spread entry the gate must approve or reject.

    `max_loss` is the defined risk of the whole order in dollars (spreads are
    always defined-risk verticals — spec §3). It is what the position-size cap is
    checked against.
    """

    symbol: str
    contracts: int
    max_loss: float
    #: `"put"` or `"call"` (2026-09-26), for the side-balance re-check in
    #: `PreTradeGate`. The one production constructor (`orchestrator`) always sets
    #: it; None is only ever a test or pre-2026-09-26 caller, and the gate skips the
    #: side check for it rather than guessing a side.
    side: str | None = None


@dataclass(frozen=True)
class RiskSnapshot:
    """A point-in-time picture of risk state, supplied to the gate.

    Ages are seconds since the last good update for each stream. A stream that has
    never connected should be reported as `float("inf")` so the fail-closed
    staleness check blocks entries by default (spec §6).

    **`open_position_count` is BROKER LEGS, and that is deliberate on both counts.**
    Broker-derived, because a position held but never booked into `positions` must
    still count against the concurrent limit — the system degrades toward not
    trading (see `docs/decisions/2026-08-11-unwatched-position-gap.md`). Legs,
    because that is the unit the broker reports and the unit the watchdog's
    book-agreement check needs. `max_concurrent_positions` is a SPREAD count, so
    the gate compares `open_spread_equivalent`, never this field directly.

    **"Broker-derived" means a REST-seeded MIRROR of the broker, not the broker.**
    In the live pipeline it is read off `AccountStateCache`, which the account
    stream feeds and the liveness refresh re-seeds. That mirror can over-report: a
    close event missed on the stream used to leave an entry no re-seed could clear,
    because absence from a REST snapshot — not a zero row — is how a closed position
    is reported. `AccountStateCache.evict_absent` bounds the drift to
    `staleness.account_position_evict_after_s`, and the direction is the safe one
    anyway (a phantom leg inflates the count and BLOCKS entries), but a caller must
    not read this field as instantaneous broker truth.
    → docs/decisions/2026-08-19-account-cache-ghost-positions.md
    """

    open_position_count: int    # BROKER LEGS, not spreads — see open_spread_equivalent
    daily_pnl: float            # signed TOTAL day P&L (realized + unrealized); negative = loss
    account_nlv: float          # net liquidating value, for the size cap
    market_data_age_s: float
    account_stream_age_s: float
    #: How much of `daily_max_loss` a new entry may still put at risk (2026-09-21):
    #: `daily_max_loss − W`, W = realized day loss + the remaining worst case of
    #: everything open. REQUIRED, with no default, on the caps' own rule: a default
    #: here would be either fail-OPEN (the whole cap, whatever the day has lost) or a
    #: silent zero that refuses every entry for a reason nobody wrote down. The sizer
    #: sizes to it and the gate refuses past it — enforced twice, like every size rail.
    #: → docs/decisions/2026-09-21-loss-budget-sizing.md
    loss_budget: LossBudget
    #: Spreads Argus's own ledger says are open, per TRADE IDEA. Zero when nothing
    #: supplies it (shadow, and every caller that predates 2026-08-17), which leaves
    #: the broker-derived count governing exactly as before.
    ledger_spread_count: int = 0
    #: Trade ideas with an OPENING order still WORKING at the broker — exposure
    #: neither count above can see, because both count filled positions. Between
    #: submit and fill, and for as long as a walk's abandoned order sits with its
    #: death unconfirmed, that order is a spread that has not landed yet. Zero when
    #: nothing supplies it (shadow, and every caller predating 2026-08-29).
    #: Opening orders only — an exit's order closes a position already counted;
    #: `execution.working_orders.open_working_spread_equivalents` is the source.
    #: → docs/decisions/2026-08-28-exit-attempt-taxonomy-and-halt-scope.md (Finding 6)
    working_open_spread_count: int = 0
    #: Whether `daily_pnl` is a READING — an account NLV measured against a day
    #: ANCHOR (2026-09-18). `False` means it is a placeholder 0.0 no record could
    #: tell from a flat day: `DailyPnl.value` before an anchor exists, or the
    #: builder's no-account fallback. A structural zero rendered as a reading is
    #: exactly the G1 failure (the breaker reads it and cannot trip). RECORDED,
    #: never decided on: the gate and the breaker are unchanged. `None` = the caller
    #: did not say (shadow, tests, every construction site predating the field);
    #: only `RiskSnapshotBuilder` knows.
    daily_pnl_anchored: bool | None = None
    #: Open spreads PER SIDE (2026-09-26), by `open_spread_equivalent`'s own formula
    #: applied per side (`risk.side_balance.count_by_side`). Read by the side-balance
    #: rule in `orchestrator` and `PreTradeGate`, recorded on `gate.decision`. Zero
    #: when nothing supplies them — every caller predating the rule; production's
    #: `RiskSnapshotBuilder` always does, from the broker's own legs at least.
    put_spread_count: int = 0
    call_spread_count: int = 0
    #: Open spreads whose SIDE could not be read (2026-09-27 review): an unparseable
    #: broker leg, ledger idea or working order. Never folded into the two counts
    #: above — counting it on both sides is neutral for a rule on `puts − calls` —
    #: and the side rule assumes the worst side for it (`side_balance.admissible`).
    unknown_side_spread_count: int = 0

    @property
    def open_spread_equivalent(self) -> int:
        """Spreads open right now — the unit every risk cap is stated in.

        **The HIGHER of two position counts, PLUS working opening orders.**

        Broker legs / 2 catches a position the ledger never booked: held but
        unrecorded, it must still consume a slot, so the system degrades toward not
        trading (`docs/decisions/2026-08-11-unwatched-position-gap.md`).

        The ledger's per-idea count catches OVERLAPPING verticals, which the broker
        structurally cannot report. `signal.width = 5.0` against SPX's 5-point ladder
        makes consecutive candidates chain — 7755/7750 then 7750/7745 — and the
        shared strike nets to ZERO at the broker as much as in `positions`. Chained
        that way, k spreads present as 2 legs no matter how large k is, so a
        leg-derived count alone reads "1 spread" forever and the concurrent limit
        stops bounding anything. Measured 2026-08-17: three spreads open against a
        limit of two, ~2850 of max loss against a 2500 daily cap.
        → docs/decisions/2026-08-17-overlapping-verticals.md

        Fixed 2026-08-13: the gate compared a LEG count against a key meaning
        SPREADS, so `max_concurrent_positions = 2` permitted exactly one vertical
        and denied 402 of 404 gate decisions on `concurrent_limit`. The other two
        risk caps (`max_loss_per_position`, `max_contracts_per_position`) were
        already per-spread; one word, three keys, two meanings.

        **Rounds UP, and that direction is the point.** Leg counts go transiently
        odd — a real `broker_legs: 1` was observed during fill propagation on
        2026-08-13 — and a half-filled vertical is a position, not half a licence
        to open another. Ceiling counts it as one; flooring would hand out a free
        slot at exactly the moment the book is least well understood.

        **Working opening orders ADD, they do not `max()`.** A pending order is
        exposure DISTINCT from any filled position — one filled spread plus one
        working opening order is potentially two spreads, and must block a third
        against a cap of two (Finding 6, 2026-08-28). During fill propagation an
        order can briefly be counted both here (as legs) and as still-working; the
        transient over-count blocks an entry for a moment, the same safe direction
        the ceiling takes, and converges once the order goes terminal.
        """
        return (
            max(
                math.ceil(self.open_position_count / LEGS_PER_VERTICAL),
                self.ledger_spread_count,
            )
            + self.working_open_spread_count
        )


@dataclass(frozen=True)
class GateDecision:
    """Outcome of a single gate evaluation. `reasons` is empty iff allowed."""

    allowed: bool
    reasons: tuple[str, ...]
    proposal: EntryProposal
