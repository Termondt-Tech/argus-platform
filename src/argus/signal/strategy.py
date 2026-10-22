"""Strategy interface + baseline credit-spread strategy.

`Strategy.evaluate(chain, now_et)` returns a `StrategyResult`: either a
`SpreadSignal`, or a structured explanation of why none fired. Pure and
deterministic, so signal logic is fully testable without live data.

`CreditSpreadStrategy` is a TRANSPARENT BASELINE, not a validated edge (spec §10,
§11). It selects, per configured side:
  - a SHORT strike whose delta magnitude is closest to `target_short_delta`,
    where WHICH delta is `signal.short_delta_source` (2026-09-21):
      * "mid" — the delta implied by each out-of-the-money leg's OWN MID
        (`signal/implied.py`), the same quotes the credit is computed from, so
        `min_credit` has no estimation error left to select on. A tie goes to the
        farther out-of-the-money strike. There is NO fallback to the feed delta.
      * "feed" — the feed delta, among legs carrying one fresh enough to select by
        (`OptionQuote.has_delta` — where the greeks-freshness rail lives, scoped
        to the one leg it protects; see `live_chain`). The pre-2026-09-21
        behaviour, byte for byte, and the config-only rollback.
    Whichever source selects, the OTHER one's pick is recorded beside it
    (`alt_pick_strike`) and never acted on,
  - a LONG strike the nearest available distance in [`width`, `max_width`]
    further OTM (defined risk; economics use the ACTUAL distance), chosen from
    EVERY tradeable leg, because that leg contributes no delta to anything,
and emits the spread only if the zero-bid-floored net credit clears `min_credit`
and it's inside the entry time window. Every threshold is config, so you tune it
from shadow data rather than editing code.

Rejections carry the near-miss so `signal.none` telemetry is a tuning dataset,
not noise. When more than one side is configured, the FIRST qualifying side in
`signal.sides` order is the signal, and every other side is still evaluated and
rides the result as `detail["other_sides"]` (2026-09-26) — telemetry for
`signal.candidate`, never an input to any decision.

Two research-motivated extensions ride on the optional `regime` argument, and
they are at DIFFERENT maturities on purpose:

- The **event blackout** (`EVENT_BLACKOUT`) is LIVE. It suppresses every entry on
  a listed macro date (FOMC/CPI/NFP/PCE) and needs no calibration — it is a date
  list, not a fitted threshold.
- The **skew/IV regime gate** (`_side_permitted`, `size_multiplier`) is INERT: it
  no-ops entirely while `regime_config is None`, which is the shipped state.
  `[regime]` stays out of `argus.toml` until the per-tick regime telemetry the
  orchestrator now records has run long enough to calibrate a threshold from.

Both are no-ops when `regime is None`, so every pre-existing caller is unchanged.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import date, time
from typing import Any, Protocol

from argus.config import SHORT_DELTA_MID, RegimeConfig, SignalConfig
from argus.signal.implied import MidDelta, Refusal, solve_leg, years_to_settlement
from argus.signal.models import (
    CALL,
    PUT,
    ChainSnapshot,
    OptionQuote,
    RegimeSnapshot,
    SelectionContext,
    SpreadSignal,
    StrategyResult,
    payload_number,
)
from argus.signal.regime import implied_forward

_STRIKE_EPS = 1e-6

# Rejection codes, ranked by how far evaluation got. When several sides are
# configured and all decline, the headline reason is the furthest-advanced one —
# a credit near-miss is far more informative than "no tradeable legs".
OUTSIDE_ENTRY_WINDOW = "outside_entry_window"
EVENT_BLACKOUT = "event_blackout"
REGIME_SIDE_REFUSED = "regime_side_refused"
NO_TRADEABLE_LEGS = "no_tradeable_legs"
NO_SHORT_LEG_WITH_FRESH_DELTA = "no_short_leg_with_fresh_delta"
NO_SHORT_LEG_WITH_MID_DELTA = "no_short_leg_with_mid_delta"
NO_LONG_LEG_WITHIN_MAX_WIDTH = "no_long_leg_within_max_width"
CREDIT_EXCEEDS_WIDTH = "credit_exceeds_width"
CREDIT_BELOW_MIN = "credit_below_min"

_REASON_RANK = {
    OUTSIDE_ENTRY_WINDOW: 0,
    EVENT_BLACKOUT: 0,
    # Ranked BELOW "no tradeable legs" on purpose: a side that was refused before
    # its strikes were ever examined is the least informative reason of all, so it
    # must never win the headline over a side that actually got as far as pricing.
    REGIME_SIDE_REFUSED: 0,
    NO_TRADEABLE_LEGS: 1,
    # Above NO_TRADEABLE_LEGS: the ladder IS priceable, only the greeks feed is
    # behind. That is a lagging-feed diagnosis and a different day from an empty
    # chain, so it must not be reported as one.
    NO_SHORT_LEG_WITH_FRESH_DELTA: 2,
    # Its "mid" twin, and ranked with it: the ladder is priceable and no short
    # strike could be selected from it — here because no out-of-the-money quote
    # could be inverted (or there was no time left to invert it against), and the
    # detail says which by counting the refusals.
    NO_SHORT_LEG_WITH_MID_DELTA: 2,
    NO_LONG_LEG_WITHIN_MAX_WIDTH: 3,
    CREDIT_EXCEEDS_WIDTH: 4,
    CREDIT_BELOW_MIN: 5,
}


def classify_iv_regime(atm_iv: float, cfg: RegimeConfig) -> str:
    """Bucket an ATM-IV reading into "low" / "mid" / "high" per `RegimeConfig`'s
    static thresholds.

    Pure, so it is independently testable and so the orchestrator can log the
    regime tag on every tick without duplicating the threshold logic.

    A NaN reading buckets as "mid". That is the deliberate fail-SAFE direction
    here and it is worth being explicit about: "mid" carries `size_multiplier_mid`,
    and the alternative — treating an unestimable vol as "low" — would hand the
    largest size multiplier to precisely the case where the chain was too thin to
    read. `_side_permitted` separately refuses nothing on a NaN skew, so an
    unestimable regime never *expands* what is permitted either.
    """
    if math.isnan(atm_iv):
        return "mid"
    if atm_iv <= cfg.atm_iv_low_max:
        return "low"
    if atm_iv >= cfg.atm_iv_high_min:
        return "high"
    return "mid"


class Strategy(Protocol):
    def evaluate(
        self, chain: ChainSnapshot, now_et: time, regime: RegimeSnapshot | None = None
    ) -> StrategyResult: ...

    def size_multiplier(self, regime: RegimeSnapshot | None) -> float: ...


class CreditSpreadStrategy:
    def __init__(
        self,
        config: SignalConfig,
        regime_config: RegimeConfig | None = None,
        blackout_dates: frozenset[date] = frozenset(),
    ) -> None:
        self._cfg = config
        self._regime_cfg = regime_config
        self._blackout_dates = blackout_dates

    def evaluate(
        self, chain: ChainSnapshot, now_et: time, regime: RegimeSnapshot | None = None
    ) -> StrategyResult:
        if not (self._cfg.entry_earliest <= now_et <= self._cfg.entry_latest):
            return StrategyResult(
                None,
                OUTSIDE_ENTRY_WINDOW,
                {
                    "now_et": now_et.isoformat(),
                    "entry_earliest": self._cfg.entry_earliest.isoformat(),
                    "entry_latest": self._cfg.entry_latest.isoformat(),
                },
            )

        # THE EVENT BLACKOUT. Note what this does NOT consult: `self._regime_cfg`.
        # It is deliberately independent of the skew/IV gate — the blackout is a
        # categorical date list needing no calibration, while the gate needs months
        # of telemetry first, so tying them together would hold a rail that is ready
        # hostage to one that is not. The only coupling is that the DATE arrives on
        # the regime snapshot. See `regime.compute_regime`, which never returns None
        # and never leaves `today` unset, precisely so this check cannot be silently
        # skipped by an estimator failure.
        if regime is not None and regime.today in self._blackout_dates:
            return StrategyResult(
                None, EVENT_BLACKOUT, {"today": regime.today.isoformat()}
            )

        # ONE forward and ONE time-to-settlement per tick, shared by both sides —
        # and computed in BOTH modes, because the mode that does not select still
        # records its counterfactual pick.
        pricing = _mid_pricing(chain, regime)
        rejections: dict[str, dict[str, Any]] = {}
        signals: list[SpreadSignal] = []
        # EVERY configured side is evaluated on every tick (2026-09-26). Until then
        # the loop returned at the first qualifying side, so on a tick where the put
        # qualified the call side was never priced and never recorded — every
        # call-side analysis since has had to impute it. The CHOICE is unchanged:
        # the first qualifying side in `signal.sides` order, exactly as before.
        for side in self._cfg.sides:
            if not self._side_permitted(side, regime):
                rejections[side] = {
                    "reason": REGIME_SIDE_REFUSED,
                    "side": side,
                    "skew": payload_number(regime.skew) if regime is not None else None,
                }
                continue
            signal, rejection = self._select(chain, side, pricing)
            if signal is not None:
                signals.append(signal)
                continue
            assert rejection is not None
            rejections[side] = rejection

        if signals:
            chosen = signals[0]
            return StrategyResult(
                chosen,
                detail={"other_sides": _other_sides(chosen, signals, rejections)},
                alternatives=tuple(signals[1:]),
            )
        headline = max(rejections.values(), key=lambda r: _REASON_RANK[r["reason"]])
        return StrategyResult(None, headline["reason"], {"per_side": rejections})

    def _side_permitted(self, side: str, regime: RegimeSnapshot | None) -> bool:
        """Skew-based side gate — see `RegimeSnapshot` and `RegimeConfig` for the
        caveat that this specific mapping is an extrapolation from the research,
        not a directly cited finding.

        Returns True (permits everything) whenever regime gating isn't wired up,
        so it never changes baseline behaviour on its own. A NaN skew also permits:
        an unreadable chain is not evidence that a side is dangerous, and the
        fail-safe direction for a gate that only ever REMOVES opportunities is to
        keep the baseline rather than invent a refusal. The rails that must fail
        closed on bad data — staleness, the pre-trade gate — are elsewhere and are
        untouched by this.
        """
        if regime is None or self._regime_cfg is None:
            return True
        if math.isnan(regime.skew):
            return True
        cfg = self._regime_cfg
        if side == PUT and regime.skew < -cfg.max_downside_skew_for_puts:
            return False
        if side == CALL and regime.skew > cfg.max_upside_skew_for_calls:
            return False
        return True

    def size_multiplier(self, regime: RegimeSnapshot | None) -> float:
        """Regime-scaled position-size multiplier, applied by
        `orchestrator.size_contracts` on top of `risk.position_size_pct` — never in
        place of it, and never as a fourth independent rail. Returns 1.0 (no
        change) whenever regime gating isn't wired up, matching the same fail-safe
        default as `_side_permitted`. Config validation caps every multiplier at
        1.0, so this can only ever shrink a position."""
        if regime is None or self._regime_cfg is None:
            return 1.0
        cfg = self._regime_cfg
        bucket = classify_iv_regime(regime.atm_iv, cfg)
        return {
            "low": cfg.size_multiplier_low,
            "mid": cfg.size_multiplier_mid,
            "high": cfg.size_multiplier_high,
        }[bucket]

    def _select(
        self, chain: ChainSnapshot, side: str, pricing: _MidPricing
    ) -> tuple[SpreadSignal | None, dict[str, Any] | None]:
        legs = [o for o in chain.by_right(side) if o.tradeable]
        if not legs:
            return None, {"reason": NO_TRADEABLE_LEGS, "side": side, "candidates": 0}

        # Both selectors run on every tick; `short_delta_source` decides which one
        # SELECTS and the other becomes `alt_pick_strike`. Neither reads the
        # other's input: the mid pick never consults `delta`, the feed pick never
        # consults a mid delta.
        mid = _solve_side(legs, side, pricing)
        mid_pick = _mid_pick(legs, mid.solved, side, self._cfg.target_short_delta)
        feed_pick = self._feed_pick(legs)
        use_mid = self._cfg.short_delta_source == SHORT_DELTA_MID
        short_leg, alt_leg = (mid_pick, feed_pick) if use_mid else (feed_pick, mid_pick)
        context = SelectionContext(
            delta_source=self._cfg.short_delta_source,
            forward=pricing.forward,
            forward_source=pricing.forward_source,
            alt_pick_strike=alt_leg.strike if alt_leg is not None else None,
        )

        if short_leg is None and use_mid:
            return None, {
                "reason": NO_SHORT_LEG_WITH_MID_DELTA,
                "side": side,
                # The out-of-the-money legs that were ASKED, and why each refused.
                # "the solver is failing" and "the wing is one-sided" are
                # different mornings, and a bare count cannot tell them apart.
                "candidates": mid.asked,
                "refusals": dict(mid.refusals),
                # Set when nothing could be asked at all — no remaining life to
                # invert against (`regime` absent, unestimable, or inside the last
                # `regime.MIN_MINUTES_TO_SETTLEMENT`), or no forward and no spot.
                "cause": pricing.cause,
                **context.context_payload(),
            }
        if short_leg is None:
            return None, {
                "reason": NO_SHORT_LEG_WITH_FRESH_DELTA,
                "side": side,
                # A priceable ladder with no selectable short strike. Recorded as
                # its own reason so it reads as the lagging greeks feed it is,
                # rather than disappearing into a skipped tick.
                "candidates": len(legs),
                # BY HOW MUCH the feed missed, not just that it did. `has_delta`
                # collapses to a boolean and throws the distance away, and the
                # distance is the whole dataset: this is what answers "what would a
                # bound of B have bought?" from live traffic, and what a
                # drift-based bound is eventually calibrated against. None when no
                # leg on this side has ever carried a greeks reading — which is a
                # DIFFERENT condition (nothing to be late) and must not read as 0.
                "min_greeks_age_s": payload_number(min(o.greeks_age_s for o in legs)),
                **context.context_payload(),
            }

        # The long-leg search sees EVERY tradeable leg, delta or not — that is the
        # point of the split. Restricting it to fresh-greeks legs punched holes in
        # the ladder and pushed the search out to the next VISIBLE strike: measured
        # 2026-08-24, a 10-wide spread where 5 was available, then clamped to one
        # contract by `max_loss_per_position` and denied at the gate.
        long_leg = _find_long_leg(
            legs, short_leg, side, self._cfg.width, self._cfg.max_width
        )
        short_mid = mid.solved.get(short_leg.strike)
        if long_leg is None:
            return None, {
                "reason": NO_LONG_LEG_WITHIN_MAX_WIDTH,
                "side": side,
                "short_strike": payload_number(short_leg.strike),
                "short_delta": payload_number(short_leg.delta),
                "short_mid_delta": payload_number(short_mid.delta if short_mid else None),
                "width": self._cfg.width,
                "max_width": self._cfg.max_width,
                **context.context_payload(),
            }
        long_mid = mid.solved.get(long_leg.strike)
        context = dataclasses.replace(
            context,
            short_mid_delta=short_mid.delta if short_mid else None,
            short_mid_iv=short_mid.iv if short_mid else None,
            long_mid_delta=long_mid.delta if long_mid else None,
            long_mid_iv=long_mid.iv if long_mid else None,
        )

        # ACTUAL strike distance — never the configured target. A coarse ladder
        # can put the long leg further out than desired, and max loss must
        # reflect the risk actually taken.
        actual_width = abs(short_leg.strike - long_leg.strike)
        credit = short_leg.floored_mid - long_leg.floored_mid
        near_miss = {
            "side": side,
            "short_strike": payload_number(short_leg.strike),
            "short_delta": payload_number(short_leg.delta),
            # Carried on the near-miss too: a tick rejected on CREDIT still selected
            # a short strike, so it belongs in the same age distribution as one that
            # produced a candidate. Leaving it off would bias the dataset toward the
            # ticks that happened to clear `min_credit`.
            "short_greeks_age_s": payload_number(short_leg.greeks_age_s),
            "long_strike": payload_number(long_leg.strike),
            "long_delta": payload_number(long_leg.delta),
            # THE QUOTED SPREAD, on the REJECTED tick as well as the accepted one
            # (2026-09-15) — and this side is the half that makes the dataset usable
            # rather than merely present. `min_credit` rejects on CREDIT, credit is a
            # difference of floored mids, and a floored mid is a function of the very
            # spread being measured: recording the market only where the credit
            # cleared samples the tail of its own distribution. It is the same
            # argument `orchestrator.evaluate_once` already makes for the regime
            # block, with more force, because here the conditioning is MECHANICAL
            # rather than incidental.
            #
            # FLAT keys, not a nested `short`/`long` dict — match the record you are
            # joining, not the other record. Everything around them here is flat
            # (`short_strike`, `long_delta`), while `SpreadSignal.to_payload` nests,
            # so the two records differ on purpose.
            "short_bid": payload_number(short_leg.bid),
            "short_ask": payload_number(short_leg.ask),
            "long_bid": payload_number(long_leg.bid),
            "long_ask": payload_number(long_leg.ask),
            "credit": payload_number(credit),
            "width": payload_number(actual_width),
            # The selection behind this near miss — flat, like everything around
            # it. `short_delta` above stays the FEED delta in both modes.
            "short_mid_delta": payload_number(context.short_mid_delta),
            "short_mid_iv": payload_number(context.short_mid_iv),
            "long_mid_delta": payload_number(context.long_mid_delta),
            **context.context_payload(),
        }

        if credit < self._cfg.min_credit:
            return None, {
                **near_miss,
                "reason": CREDIT_BELOW_MIN,
                "min_credit": self._cfg.min_credit,
            }
        if credit >= actual_width:  # nonsensical (would imply negative max loss)
            return None, {**near_miss, "reason": CREDIT_EXCEEDS_WIDTH}

        return (
            SpreadSignal(
                underlying=chain.underlying,
                side=side,
                short_leg=short_leg,
                long_leg=long_leg,
                credit=credit,
                width=actual_width,
                # Freshness travels WITH the candidate: the execution layer bounds
                # how stale a signal may be at construction, and it can only do
                # that if the snapshot's timestamp survives selection.
                captured_at=chain.captured_at,
                # So does the underlying price. This is the arrival price the entry
                # walk's drift breaker measures against — the level the credit on
                # this candidate was worth. Same rule as `captured_at`: a chain-
                # snapshot fact that has to survive selection to be usable later.
                underlying_price=chain.underlying_price,
                selection=context,
            ),
            None,
        )

    def _feed_pick(self, legs: list[OptionQuote]) -> OptionQuote | None:
        """The pre-2026-09-21 selector, unchanged: closest FEED delta among legs
        with one fresh enough (`has_delta`). A tie falls to chain order, as it
        always has — "feed" mode is the rollback and must stay byte-identical."""
        candidates = [o for o in legs if o.has_delta]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda o: abs(abs(o.delta) - self._cfg.target_short_delta),
        )


@dataclass(frozen=True, slots=True)
class _MidPricing:
    """The per-tick inputs every mid delta is inverted against.

    `t_years` None means no mid delta is computable this tick, and `cause` says
    why. `forward_source` is "parity" when `regime.implied_forward` produced one
    and "spot" on the fallback — a decided fallback (2026-09-21, Richard): the
    spot-forward basis is ~0.1-0.5 points, a delta error of <=0.007, well inside
    the 5-point grid, so refusing the tick would cost more than it protects.
    """

    t_years: float | None
    forward: float | None
    forward_source: str | None
    cause: str | None


def _other_sides(
    chosen: SpreadSignal,
    signals: list[SpreadSignal],
    rejections: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """What the sides NOT chosen looked like on the tick a candidate fired.

    TELEMETRY. Nothing decides on it (a test pins that only this module and the
    orchestrator name the key). A side that also qualified carries its full
    `to_payload()` — the same shape `signal.candidate` already records, so already
    Tier 1-safe — and a side that did not carries its rejection, whose every number
    already went through `payload_number` for `signal.none`. That matters because
    `signal.candidate` is Tier 1 and written ABOVE the gate: a non-finite field here
    would suppress every entry (see `payload_number`).
    """
    out: dict[str, dict[str, Any]] = {
        s.side: {"qualified": True, **s.to_payload()} for s in signals if s is not chosen
    }
    out.update({side: {"qualified": False, **r} for side, r in rejections.items()})
    return out


def _mid_pricing(chain: ChainSnapshot, regime: RegimeSnapshot | None) -> _MidPricing:
    # No regime means no time to expiry, and NO FALLBACK to the feed delta: a
    # silent fallback re-introduces exactly the selection effect the mid selector
    # exists to remove. The orchestrator always passes a regime; tests and tools
    # that want a mid selection must supply one.
    t_years = years_to_settlement(regime.minutes_to_settlement) if regime else None
    if t_years is None:
        return _MidPricing(None, None, None, "no_time_to_settlement")
    # The same tradeable set `regime.compute_regime` reads its forward from, so
    # the two estimates of one market agree on what the forward is.
    forward = implied_forward(
        [o for o in chain.options if o.right == PUT and o.tradeable],
        [o for o in chain.options if o.right == CALL and o.tradeable],
        t_years,
    )
    if forward is not None:
        return _MidPricing(t_years, forward, "parity", None)
    spot = chain.underlying_price
    if math.isfinite(spot) and spot > 0.0:
        return _MidPricing(t_years, spot, "spot", None)
    return _MidPricing(None, None, None, "no_forward")


@dataclass(frozen=True, slots=True)
class _SideSolve:
    #: Mid delta per OTM strike that inverted. Keyed by strike: one side's ladder
    #: holds each strike once, and the long leg's telemetry is a lookup here.
    solved: dict[float, MidDelta]
    asked: int
    refusals: dict[str, int]


def _solve_side(legs: list[OptionQuote], side: str, pricing: _MidPricing) -> _SideSolve:
    """Invert every OUT-OF-THE-MONEY leg on one side, relative to the forward.

    Only OTM legs are asked, which also makes "an in-the-money leg is never a
    short" structural rather than a filter someone can drop. Every one is solved
    on every tick — no cache, no early exit (see `implied.py`).
    """
    # Every cause, always — a zero is a statement, an absent key is not.
    refusals = {r.value: 0 for r in Refusal}
    if pricing.t_years is None or pricing.forward is None:
        return _SideSolve({}, 0, refusals)
    forward, t_years = pricing.forward, pricing.t_years
    solved: dict[float, MidDelta] = {}
    asked = 0
    for leg in legs:
        if not (leg.strike > forward if side == CALL else leg.strike < forward):
            continue
        asked += 1
        result = solve_leg(leg, forward, t_years)
        if isinstance(result, Refusal):
            refusals[result.value] += 1
        else:
            solved[leg.strike] = result
    return _SideSolve(solved, asked, refusals)


def _mid_pick(
    legs: list[OptionQuote], solved: dict[float, MidDelta], side: str, target: float
) -> OptionQuote | None:
    """argmin |abs(mid delta) - target|, ties to the FARTHER out-of-the-money
    strike — the lower-risk one. Deterministic: the tie no longer falls to chain
    order, which is unspecified."""
    candidates = [o for o in legs if o.strike in solved]
    if not candidates:
        return None
    # Farther OTM is a HIGHER call strike and a LOWER put strike.
    sign = -1.0 if side == CALL else 1.0
    return min(
        candidates,
        key=lambda o: (abs(abs(solved[o.strike].delta) - target), sign * o.strike),
    )


def _find_long_leg(
    legs: list[OptionQuote],
    short_leg: OptionQuote,
    side: str,
    width: float,
    max_width: float,
) -> OptionQuote | None:
    """Nearest strike at least `width` further OTM, no further than `max_width`.

    Requiring an exact `width` match silently yields no signal whenever the
    ladder has gaps. Widening to a bounded range keeps the structure
    defined-risk while tolerating real chains; `max_width` stops a sparse ladder
    from quietly turning a $5 spread into a $50 one.
    """
    if side == PUT:
        low = short_leg.strike - max_width - _STRIKE_EPS
        high = short_leg.strike - width + _STRIKE_EPS
        candidates = [o for o in legs if low <= o.strike <= high]
        return max(candidates, key=lambda o: o.strike) if candidates else None
    if side == CALL:
        low = short_leg.strike + width - _STRIKE_EPS
        high = short_leg.strike + max_width + _STRIKE_EPS
        candidates = [o for o in legs if low <= o.strike <= high]
        return min(candidates, key=lambda o: o.strike) if candidates else None
    return None  # pragma: no cover - config validation prevents this


# `payload_number` — the ONE JSON-safety rule — is imported from `signal.models`
# rather than spelled out again here. It used to be this module's private `_num`,
# behaviour-identical to two other spellings of the same ternary; see that
# function's docstring for why there is now only one. (2026-09-15)
