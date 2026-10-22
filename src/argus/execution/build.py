"""Turn an approved candidate into an unsent multi-leg order. Pure — no I/O.

Three rules from spec §7 are enforced structurally here, not by convention:

1. **Single-legging guardrail.** A vertical is built as ONE order with two legs.
   There is no code path in this module that produces a per-leg order, so
   "always submit the spread atomically" is not something a future caller has to
   remember — routing the legs separately would mean writing a different builder.

2. **Reuse the gated mid; never refetch.** The limit comes from the
   `SpreadSignal`'s already zero-bid-floored credit — the exact number that sized
   the position and passed the pre-trade gate. Pulling a fresh quote here would
   submit a *different* trade than the one that was risk-evaluated, with the size
   and the gate verdict computed against a price that no longer applies. (It
   would also add a round trip on the latency-critical path for no benefit.)
   `_assert_credit_matches_legs` keeps that honest: it recomputes the net mid from
   the signal's own legs through the shared `zerobid` floor and refuses to build
   if it disagrees, so a signal whose credit came from somewhere else can't
   quietly become a limit price.

3. **Freshness is a timestamp check, not a refetch.** The candidate carries when
   its chain snapshot was taken; if more than `max_signal_age_s` has elapsed
   between gate-pass and construction, the pipeline stalled somewhere and the
   price is no longer one anybody evaluated. That is a bounds check on a number
   already in hand — no network, no added latency in the normal case — and it
   uses `argus.staleness.is_stale`, the same fail-closed rule the gate uses, so a
   NaN or a clock going backwards counts as stale too.

`direction` is a parameter rather than two near-identical builders: an exit is
the same two strikes with the actions and the price sign flipped, and the version
of this bug that matters is an exit path that drifted from the entry path.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal

from argus.execution.models import (
    ORDER_ID_PREFIX,
    Order,
    OrderDirection,
    OrderLeg,
    LegAction,
)
from argus.execution.zerobid import TICK, is_finite, round_to_tick, spread_mid
from argus.signal.models import CALL, PUT, SpreadSignal
from argus.staleness import is_stale

# How far the recomputed net mid may differ from the signal's own credit before
# the mismatch is treated as a bug rather than float noise. Half a tick: anything
# larger is a genuinely different price, not a rounding artifact.
_CREDIT_TOLERANCE = float(TICK) / 2.0

# Leg actions per direction: (short leg, long leg). Opening a credit spread sells
# the short strike and buys the long one; closing reverses both.
_ACTIONS: dict[OrderDirection, tuple[LegAction, LegAction]] = {
    OrderDirection.OPEN: (LegAction.SELL_TO_OPEN, LegAction.BUY_TO_OPEN),
    OrderDirection.CLOSE: (LegAction.BUY_TO_CLOSE, LegAction.SELL_TO_CLOSE),
}


class StaleSignalError(RuntimeError):
    """The candidate aged past tolerance between gate-pass and construction.

    Not a bad-input error — the inputs were fine when they were evaluated. It
    means something upstream stalled, so the caller should abandon this candidate
    and re-evaluate rather than submit a price nobody approved.
    """

    def __init__(self, age_s: float, max_age_s: float) -> None:
        super().__init__(
            f"signal is {age_s:.3f}s old, past the {max_age_s:g}s construction "
            "tolerance; refusing to build an order from a price the gate did not see"
        )
        self.age_s = age_s
        self.max_age_s = max_age_s


class UnpriceableSpreadError(ValueError):
    """The spread's net mid rounds to zero at the tick, so it has no limit price.

    **A `ValueError` SUBCLASS on purpose** (2026-09-02): every caller already catches
    `(StaleSignalError, ValueError)` around `build_order`, so nothing moves and no
    catch site widens. What it buys is that a caller can tell this apart from the other
    `ValueError`s this module raises — a credit that does not match its own legs, a
    non-positive contract count — **by TYPE rather than by matching the message text**,
    which is a rule this codebase holds elsewhere (`Order.reject_reason`: broker prose
    changes wording without notice, and a rail that matches on it fails open the day it
    does). Our own prose is more stable than a broker's, and it is not stable enough to
    build a branch on.

    The distinction is worth having because the two mean opposite things to whoever
    reads the page. This one is a DESIGNED REFUSAL and the routine case on the exit
    path: a winning 0DTE spread is nearly worthless near the close, so its mid rounding
    below half a tick is expected, not broken. The others are defect signatures. Before
    this existed the flatten's modal refusal paged as a bare `ValueError: ...`, which
    reads as a crash.

    Whether it is worth paying a tick to close a near-worthless spread stays an
    exit-policy question for chunk 4, and chunk 4's answer is still "not by guessing" —
    naming the refusal does not soften it.
    """

    def __init__(self, credit: float, magnitude: Decimal) -> None:
        super().__init__(
            f"net mid {credit!r} rounds to {magnitude} at a {TICK} tick — "
            "there is no valid limit price for this spread"
        )
        self.credit = credit
        self.magnitude = magnitude


def candidate_key(
    trading_date: date, side: str, short_strike: float, long_strike: float
) -> str:
    """Stable identity for one trade idea, from the four fields that define it.

    Deliberately the SAME tuple `shadow.outcome.trade_ideas` dedups on —
    (trading date, side, short strike, long strike) — so a live order joins
    directly to whatever shadow mode already recorded about the same spread.

    Split out from `candidate_id` so a caller holding the primitives (a decoded
    `TradeIdea`, a parsed identifier) can build the key without inventing a
    `SpreadSignal` to pass in. Same string, one format.
    """
    return f"{trading_date:%Y%m%d}-{side}-{short_strike:g}-{long_strike:g}"


def candidate_id(signal: SpreadSignal, trading_date: date) -> str:
    """`candidate_key` for a signal. THE constructor the order path calls."""
    return candidate_key(
        trading_date, signal.side, signal.short_leg.strike, signal.long_leg.strike
    )


def parse_candidate_key(key: str) -> tuple[date, str, float, float] | None:
    """`candidate_key` in reverse: (trading date, side, short, long), or None.

    **The inverse lives next to the constructor on purpose.** Post-close scoring
    reads `candidate_id` back out of the audit log to find out WHICH spread a fill
    belongs to, and a parser sitting in the scoring module would be free to drift
    from the format the order path writes. Here, a change to one is a change to a
    neighbour, and `test_candidate_key_round_trips` fails if they disagree.

    Returns None rather than raising for anything that is not this format —
    callers are reading records that may predate it, and "I cannot say" has to be
    a value they can branch on. Same fail-closed rule as `occ.occ_expiry`.
    """
    parts = key.split("-")
    if len(parts) != 4:
        return None
    day, side, short, long_ = parts
    if side not in (PUT, CALL):
        return None
    try:
        trading_date = datetime.strptime(day, "%Y%m%d").date()
        strikes = (float(short), float(long_))
    except ValueError:
        return None
    if not all(math.isfinite(s) for s in strikes):
        # `float("nan")` and `float("inf")` both parse. Neither is a strike.
        return None
    return (trading_date, side, *strikes)


def external_identifier(candidate: str, direction: OrderDirection) -> str:
    """Argus's own order key: deterministic, and that is the point.

    tastytrade documents `external_identifier` as the field for tracking orders
    across systems, and deriving it from the trade idea makes it usable for
    recovery rather than just a label: after a crash, the same candidate rebuilds
    to the same identifier, so `get_live_orders()` can answer "did I already send
    this?" from the BROKER — the only source of truth (spec §6) — instead of from
    local state that may be exactly what was lost.

    **The broker does NOT enforce uniqueness on this field.** Measured against
    sandbox on 2026-07-29 (`tools.order_smoke`): submitting the same
    `external_identifier` a second time was ACCEPTED both while the first order
    was still working and after it had been cancelled — two live orders, same
    identifier, different ids. So this is a MATCHING key, not a broker-side
    duplicate guard, and the distinction is load-bearing:

      - Chunk 2 must CHECK `get_live_orders()` before submitting. A duplicate
        submission will not bounce off the API — spec §9's "duplicate/runaway
        order submission" risk has no broker-side backstop, only ours.
      - The flip side is that re-entering the identical spread after a cancelled
        attempt is NOT blocked, which is the behaviour crash recovery needs.

    The `argus-` prefix is `models.ORDER_ID_PREFIX`, not a literal, because the
    fill consumer reads it back the other way round to tell an order Argus placed
    from one placed by hand in the same account.
    """
    return f"{ORDER_ID_PREFIX}{direction}-{candidate}"


def build_order(
    signal: SpreadSignal,
    *,
    direction: OrderDirection,
    contracts: int,
    trading_date: date,
    now: float,
    max_signal_age_s: float,
) -> Order:
    """Build the unsent multi-leg order for `signal`. Pure; raises, never logs.

    `now` is monotonic seconds on the same clock that stamped
    `signal.captured_at` (`time.monotonic` in the live wiring) — wall-clock time
    is not usable for an age bound this tight.
    """
    if direction not in _ACTIONS:  # pragma: no cover - enum-guarded
        raise ValueError(f"unknown order direction {direction!r}")
    if contracts <= 0:
        raise ValueError(f"contracts must be >= 1, got {contracts}")

    age_s = now - signal.captured_at
    if is_stale(age_s, max_signal_age_s):
        raise StaleSignalError(age_s, max_signal_age_s)

    _assert_credit_matches_legs(signal)

    limit_price = _limit_price(signal.credit, direction, width=signal.width)
    short_action, long_action = _ACTIONS[direction]
    return Order(
        direction=direction,
        underlying=signal.underlying,
        # ONE order, BOTH legs. Never two orders (spec §7 single-legging rule).
        legs=(
            OrderLeg(signal.short_leg.symbol, short_action, contracts),
            OrderLeg(signal.long_leg.symbol, long_action, contracts),
        ),
        limit_price=limit_price,
        external_identifier=external_identifier(
            candidate_id(signal, trading_date), direction
        ),
        candidate_id=candidate_id(signal, trading_date),
    )


def _limit_price(credit: float, direction: OrderDirection, *, width: float) -> Decimal:
    """Signed, tick-aligned limit: opening collects the mid, closing pays it.

    tastytrade reads a positive price as a credit and a negative one as a debit,
    so the sign IS the difference between selling the spread and buying it back.

    `width` is REQUIRED and keyword-only (2026-09-17). It is the spread's own strike
    distance, not a quote, so it costs this function nothing and cannot be omitted by
    a caller that happens not to have market data.
    """
    if not is_finite(credit):
        raise ValueError(f"signal credit is not a usable price: {credit!r}")
    if not is_finite(width) or width <= 0:
        # A bug, not a market condition: `SpreadSignal.width` is the ACTUAL strike
        # distance and every construction path computes it from two real strikes.
        raise ValueError(f"spread width is not a usable bound: {width!r}")
    magnitude = round_to_tick(credit)
    if magnitude <= 0:
        # A mid that rounds to zero cannot be expressed as a limit. Refuse rather
        # than quietly substituting one tick: whether it is worth paying a tick to
        # close a near-worthless spread is an exit-policy decision (chunk 4), not
        # something a builder should decide on the caller's behalf.
        #
        # Its OWN type since 2026-09-02, so the caller can say which refusal this was
        # without reading the message. Still a `ValueError`, so no catch site changes.
        raise UnpriceableSpreadError(credit, magnitude)
    magnitude = cap_at_width(magnitude, width)
    return magnitude if direction is OrderDirection.OPEN else -magnitude


def cap_at_width(magnitude: Decimal, width: float) -> Decimal:
    """Clamp a spread's price magnitude to its own strike distance. (2026-09-17)

    **A vertical cannot be worth more than the distance between its strikes.** The
    long leg caps it, which is what "defined risk" means, so a price above the width
    is arithmetically impossible for the instrument — never an aggressive price, always
    a bad number.

    **This fired in production.** On 2026-09-03 at 15:37:59 ET, after the manual
    restart that recovered the halted position, `argus-close-20260903-call-7725-7730`
    (width 5.00) was marked at `cost_to_close = 5.05` — a wide print on a deep-ITM
    spread whose mid landed above intrinsic — and nothing compared it to the width. The
    exit went out at **-5.05** and filled at step 0, realizing **-870.00** against an
    entry credit of 0.70. Holding to settlement pays the width: `(0.70 - 5.00) x 2 x
    100 = -860.00`. **The flatten paid $870 to escape an $860 loss**, and the $10 is
    exactly the 0.05 it went over the ceiling.

    **CAP, not refuse, and the distinction matters.** `UnpriceableSpreadError` above
    refuses because there is no price to express; here there IS one, it is merely above
    a ceiling that the instrument itself defines, so the honest answer is the ceiling.
    Refusing would strand a deep-ITM 0DTE spread with no automated path to close, which
    is strictly worse than paying the most it can possibly cost. This is also exactly
    what `watchdog._cross_limit` has always done (`min(debit, market.width)`) — the
    market cross was the only place in the system holding this invariant, and the
    opening limit and the walk's floor both skipped it. **Public since 2026-09-25**,
    because the cross now uses THIS ceiling rather than its own `min`: one
    floor-quantized width cap in the codebase, not two that could round differently.

    **DIRECTION-FREE**, and inert on the entry path for a reason worth stating rather
    than relying on: a credit above the width is free money, so `strategy.py` already
    refuses it as `CREDIT_EXCEEDS_WIDTH` before an order is built. Keeping the clamp
    unconditional costs nothing, needs no branch on direction, and means a caller that
    bypasses the strategy (`tools.order_smoke` sets `min_credit = 0.0`) is bounded too.

    The ceiling is quantized DOWN to the tick, never with `round_to_tick`. That helper
    is `ROUND_HALF_UP`, which on a width that did not sit on the grid would round the
    cap UP and put the bound back above the thing it is bounding — a cap that rounds
    outward is not a cap. Every SPX strike distance is already a multiple of 0.05 so
    this is a no-op today; it is written this way so a ladder with odd spacing cannot
    silently reopen the hole.
    """
    ceiling = (Decimal(str(width)) / TICK).to_integral_value(rounding=ROUND_FLOOR) * TICK
    return ceiling if magnitude > ceiling else magnitude


def _assert_credit_matches_legs(signal: SpreadSignal) -> None:
    """The signal's credit must be its legs' zero-bid-floored net mid.

    Cheap guard against the failure this whole module is arranged to avoid:
    submitting a limit derived from something other than the floored mid that was
    sized and gated. Recomputing through `zerobid` — the one implementation —
    means a strategy that ever computes credit differently fails here instead of
    on the wire.
    """
    recomputed = spread_mid(
        signal.short_leg.bid,
        signal.short_leg.ask,
        signal.long_leg.bid,
        signal.long_leg.ask,
    )
    if not is_finite(recomputed) or abs(recomputed - signal.credit) > _CREDIT_TOLERANCE:
        raise ValueError(
            f"signal credit {signal.credit!r} does not match the zero-bid-floored "
            f"net mid of its own legs ({recomputed!r}) — refusing to submit a limit "
            "the pre-trade gate did not evaluate"
        )
