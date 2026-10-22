"""Signal domain types.

An `OptionQuote` is one contract's tradeable state (strike, right, delta, market).
A `ChainSnapshot` is the 0DTE chain at a point in time. A `SpreadSignal` is a
concrete defined-risk vertical the strategy proposes.

`OptionQuote.delta` is OPTIONAL, and that is a freshness statement rather than a
data-availability one: a leg whose greeks are older than
`staleness.market_data_max_age_s` still carries a live market and is admitted for
its PRICE, with `delta=None`. It is priceable, not selectable — see `has_delta`.

Economics are in points; the SPX multiplier ($100/point) converts to dollars.
Credit is computed from a **zero-bid-floored mid** (spec §7): a leg with `bid==0`
is floored to the min tick before its mid is taken, so an unfilled/deep-OTM leg
can't produce a nonsensical mid. **The record carries the raw `bid`/`ask` beside
that floored `mid`** (2026-09-15), because the floor makes the credit
systematically optimistic relative to a crossing exit and only the raw bid says
when it fired — see `_leg_payload`.

`payload_number` is the module's one public utility and is deliberately not
private to it: it is THE rule for a number on its way into the audit log, shared
by `signal/strategy.py` and `execution/watchdog.py`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from argus.execution.zerobid import floored_mid as _floored_mid
from argus.execution.zerobid import is_finite

SPX_MULTIPLIER = 100.0

PUT = "put"
CALL = "call"


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    strike: float
    right: str          # "put" | "call"
    #: Signed delta (puts negative); the MAGNITUDE is what short-strike selection
    #: uses. `None` means no delta fresh enough to select on — the leg still
    #: carries a tradeable market and is a perfectly good LONG leg, but it can
    #: never be the short one (`live_chain._admit`, 2026-08-24).
    #:
    #: `None` rather than NaN, deliberately, and against the NaN-means-unevaluable
    #: idiom `SpreadSignal.underlying_price` uses. That field is consumed by a
    #: comparison, where NaN fails CLOSED (every comparison is False -> abort).
    #: This one is consumed by `min()` over `abs(abs(delta) - target)`, where NaN
    #: fails OPEN: a NaN key never loses a comparison either, so `min` RETURNS it
    #: rather than skipping it — silently picking a strike off a delta nobody
    #: has. `None` cannot be compared at all, so a consumer that forgets to
    #: filter raises instead of mis-selecting.
    delta: float | None
    bid: float
    ask: float
    #: Age of the greeks reading behind `delta`, in seconds — whether or not it
    #: cleared `staleness.greeks_max_age_s`, and +inf when this leg has never had
    #: one. TELEMETRY ONLY: nothing selects, prices or refuses on it, and
    #: `has_delta` remains the single admission test. It exists because `delta is
    #: None` records the verdict and destroys the evidence, and the evidence is what
    #: sizes the next bound — a drift-based one, where the question is not "how old"
    #: but "how far did spot move since". Defaults to +inf so every hand-built quote
    #: (tests, tools, the fake) stays valid without asserting an age it cannot know.
    greeks_age_s: float = math.inf

    @property
    def tradeable(self) -> bool:
        # Needs a real offer; bid may legitimately be 0 (deep OTM) and gets floored.
        return self.ask > 0.0

    @property
    def has_delta(self) -> bool:
        """True when this leg may be selected as a SHORT strike.

        The finiteness half is not belt-and-braces: `OptionGreeks.delta` is a
        float straight off the feed, and a NaN there is the one value `min()`
        cannot reject for itself — it would be selected, not skipped.
        """
        return self.delta is not None and is_finite(self.delta)

    @property
    def floored_mid(self) -> float:
        # Delegated, not reimplemented: the execution layer reapplies this same
        # floor to fresh quotes when it walks a limit price, and two copies of a
        # safety rule is how one of them drifts (see `execution.zerobid`).
        return _floored_mid(self.bid, self.ask)


@dataclass(frozen=True)
class ChainSnapshot:
    underlying: str          # e.g. "SPXW"
    underlying_price: float
    options: tuple[OptionQuote, ...]
    captured_at: float

    def by_right(self, right: str) -> list[OptionQuote]:
        return [o for o in self.options if o.right == right]


@dataclass(frozen=True)
class SpreadSignal:
    underlying: str
    side: str                # "put" (bull put) | "call" (bear call)
    short_leg: OptionQuote
    long_leg: OptionQuote
    credit: float            # net credit in points (short mid - long mid)
    width: float             # strike distance in points
    # When the chain snapshot behind this candidate was taken (MONOTONIC seconds,
    # carried straight from `ChainSnapshot.captured_at`). Order construction bounds
    # this age instead of refetching a quote, so a stall between gate-pass and
    # submission is caught without a round trip — see `execution.build`.
    captured_at: float
    # The underlying's price when this candidate was evaluated, carried straight
    # from `ChainSnapshot.underlying_price` — the same reason `captured_at` is
    # here: a chain-snapshot fact the execution layer needs and cannot re-derive.
    #
    # This is the ARRIVAL PRICE the entry walk's drift breaker measures against
    # (`execution.entry.UnderlyingDriftCheck`). The ladder walks a limit derived
    # from the mid at THIS moment; if the underlying leaves this reference behind,
    # every rung below is priced off a market that no longer exists.
    #
    # NaN means "not recorded", and it is a real case, not a placeholder: chunk 4's
    # exit path synthesises a `SpreadSignal` purely to reach `build_order`, and no
    # underlying price was ever captured for it. Consumers must treat a non-finite
    # reference as UNEVALUABLE (which the drift check reads as "abort"), never as a
    # number — an unset reference must not silently become a comparison against 0.0.
    underlying_price: float = float("nan")
    # HOW the short strike was chosen, and what the other delta source would have
    # chosen instead (2026-09-21). TELEMETRY ONLY — nothing downstream of the
    # strategy reads it. None on a signal the strategy did not select (chunk 4's
    # exit path synthesises one), and the record then says so with nulls.
    selection: SelectionContext | None = None

    @property
    def max_loss_per_contract(self) -> float:
        return (self.width - self.credit) * SPX_MULTIPLIER

    @property
    def max_profit_per_contract(self) -> float:
        return self.credit * SPX_MULTIPLIER

    def describe(self) -> str:
        return (
            f"{self.underlying} {self.side} credit spread "
            f"{self.short_leg.strike:g}/{self.long_leg.strike:g}"
        )

    def to_payload(self) -> dict:
        sel = self.selection
        return {
            "underlying": self.underlying,
            "side": self.side,
            "short": {
                **_leg_payload(self.short_leg, greeks_age=True),
                "mid_delta": payload_number(sel.short_mid_delta if sel else None),
                "mid_iv": payload_number(sel.short_mid_iv if sel else None),
            },
            "long": {
                **_leg_payload(self.long_leg),
                "mid_delta": payload_number(sel.long_mid_delta if sel else None),
                "mid_iv": payload_number(sel.long_mid_iv if sel else None),
            },
            **(sel.context_payload() if sel else SelectionContext.EMPTY_CONTEXT),
            "credit": payload_number(self.credit),
            "width": payload_number(self.width),
            "underlying_price": payload_number(self.underlying_price),
            "max_loss_per_contract": payload_number(self.max_loss_per_contract),
            "max_profit_per_contract": payload_number(self.max_profit_per_contract),
        }


@dataclass(frozen=True)
class SelectionContext:
    """How a candidate's short strike was chosen — the record, never an input.

    → docs/decisions/2026-09-21-short-strike-selection-on-mid-implied-delta.md

    `delta_source` is the `signal.short_delta_source` in force ("mid" | "feed"), and
    it is the field that SPLITS THE DATASET: call trades selected on the feed delta
    were effectively ~0.19-delta trades, so call outcomes from before and after the
    mid selector must never be pooled.

    `forward` is the F the mid deltas were inverted against, and `forward_source`
    says where it came from: "parity" (`regime.implied_forward`) or "spot" (the
    documented fallback when no strike quotes both a call and a put). Both None
    when no mid delta could be computed at all (no remaining life).

    `alt_pick_strike` is the COUNTERFACTUAL — the short strike the OTHER source
    would have chosen on the same tick — so the two selectors' disagreement rate
    and direction are measured from the first session without a replay.

    The `*_mid_*` numbers are the legs' own mid-implied delta and vol; `None` when a
    leg's quote could not be inverted (a one-sided long wing is the common case).

    The feed delta is NOT here: it stays `OptionQuote.delta`, recorded as each
    leg's `delta` exactly as before, because `tools.marker` and `tools.desk` read
    that key.
    """

    delta_source: str
    forward: float | None
    forward_source: str | None
    alt_pick_strike: float | None
    short_mid_delta: float | None = None
    short_mid_iv: float | None = None
    long_mid_delta: float | None = None
    long_mid_iv: float | None = None

    #: The top-level keys with nothing selected — so a synthesised signal's record
    #: has the same shape as a selected one's, with nulls rather than absences.
    EMPTY_CONTEXT = {
        "delta_source": None,
        "forward": None,
        "forward_source": None,
        "alt_pick_strike": None,
    }

    def context_payload(self) -> dict[str, Any]:
        """The top-level keys, shared by `signal.candidate` and the flat
        `signal.none` near-miss. Every number through `payload_number`."""
        return {
            "delta_source": self.delta_source,
            "forward": payload_number(self.forward),
            "forward_source": self.forward_source,
            "alt_pick_strike": payload_number(self.alt_pick_strike),
        }


def _leg_payload(leg: OptionQuote, *, greeks_age: bool = False) -> dict:
    """One leg as the audit log records it: identity, selection inputs, market.

    **`bid` and `ask` are the RAW quote, and `mid` is the zero-bid-FLOORED one**
    (2026-09-15). Recording only the mid answers *what did Argus think this spread
    was worth* and cannot answer *what would it have cost to trade* — the
    difference between a mark and a price, and the one quantity this system had
    never recorded. Raw rather than a derived half-spread for two reasons that a
    width cannot carry: `bid == 0` is the only way `zerobid.floor_bid`'s firing
    becomes visible at all (the floor makes the recorded credit systematically
    optimistic relative to a crossing exit, by exactly the floor), and WHICH leg is
    the wide one is a selection question — it bears on `target_short_delta` and on
    the long-leg search. Every derived quantity follows from `(bid, ask)`; the
    reverse is false.

    `mid` stays despite being redundant: it is a wire contract `marker._decode` and
    `tools.desk` read back, and removing a key readers depend on to save 20 bytes
    is the trade nobody wants.

    Nothing in `src/` reads these two fields. They are an instrument.
    → docs/designs/2026-09-15-recording-the-quoted-spread.md
    """
    payload = {
        "symbol": leg.symbol,
        "strike": payload_number(leg.strike),
        "delta": payload_number(leg.delta),
        "bid": payload_number(leg.bid),
        "ask": payload_number(leg.ask),
        "mid": payload_number(leg.floored_mid),
    }
    if greeks_age:
        # How old the delta this strike was CHOSEN BY actually was — so only the
        # SHORT leg carries it. The paired half of `signal.none`'s
        # `min_greeks_age_s`: one says how stale the feed was when no strike was
        # selectable, this says how stale it was when one was. Together they are
        # the age distribution the next bound is sized from, and this side of it
        # also carries `underlying_price`, so the realized spot drift over that
        # age is recoverable at analysis time.
        #
        # +inf is the DEFAULT for a hand-built quote — chunk 4's exit path
        # synthesises one — so the guard below is load-bearing rather than
        # defensive: without it every such candidate would carry an unencodable
        # value straight into a Tier 1 write that halts trading on refusal.
        payload["greeks_age_s"] = payload_number(leg.greeks_age_s)
    return payload


def payload_number(value: float | None) -> float | None:
    """THE JSON-safety rule for a number on its way into the audit log.

    Non-finite becomes `None`. The canonical hash runs
    `json.dumps(..., allow_nan=False)` (`audit/models.py`), so a NaN or an inf in a
    payload is a REFUSED record — and the consequence is worse than "no record":

    - **Tier 1** raises `ValueError` from `_reject_unserializable`, which
      `SignalLoop.run` catches in its generic "a bad iteration must not kill the
      loop" handler. `signal.candidate` is written ABOVE the gate and the submit,
      so a single non-finite field would silently suppress every entry for as long
      as the condition held — no alert, no `/status` row, one `logger.exception`
      per tick. A telemetry field would have become a trading-behaviour change.
    - **Tier 2** drops the WHOLE record with a `tier2_unserializable` alert,
      taking the near-miss dataset or the mark trail `tools.desk` reads with it.

    **One definition, deliberately.** This rule had been decided FOUR times in four
    spellings — `_payload_delta`'s `has_delta` ternary, `_payload_age`'s and
    `underlying_price`'s inline `is_finite` ternaries, and `strategy._num`'s
    `isfinite` one — all behaviour-identical on every input, `None` and NaN
    included, which `tests/test_payload_safety.py` asserts rather than leaving to
    this sentence. `zerobid`'s own docstring is the precedent, and that module
    exists because the same mistake was anticipated for the tick floor: *"Two
    implementations of the same safety rule is exactly how one of them quietly
    drifts, so there is one."*

    **`None` in, `None` out, and that is a real case rather than a convenience:** a
    LONG leg legitimately has no delta (it is chosen by strike distance), and
    `OptionQuote.delta` is `None` rather than NaN for the reasons on that field.

    **Not `risk.gate._num`.** That one STRINGIFIES a non-finite value instead, and
    it is a different rule for a different purpose: a gate decision records the
    limit that was in force, where `inf` (a never-connected stream) is the
    information, not noise to be erased. Do not collapse the two.
    """
    return value if value is not None and is_finite(value) else None


@dataclass(frozen=True)
class StrategyResult:
    """Outcome of one strategy evaluation: a signal, or WHY none fired.

    The rejection detail is the shadow-mode tuning dataset. Recording only
    "nothing happened" makes weeks of telemetry useless for the one question
    shadow mode exists to answer. Capturing the near-miss — which strikes and
    deltas were selected, what credit they would have paid, and which threshold
    rejected them — lets you ask "what `min_credit` would have fired, and how
    often?" directly from the audit log, rather than re-running the day.
    """

    signal: SpreadSignal | None
    reason: str | None = None                       # rejection code when signal is None
    detail: dict[str, Any] = field(default_factory=dict)
    #: The OTHER sides that also qualified on this tick, in `signal.sides` order
    #: (2026-09-26). `signal` is still the strategy's own first choice; the
    #: side-balance rule in `orchestrator` may pick one of these instead.
    alternatives: tuple[SpreadSignal, ...] = ()

    @property
    def fired(self) -> bool:
        return self.signal is not None


@dataclass(frozen=True)
class RegimeSnapshot:
    """Entry-time market state, computed upstream and handed to the strategy.

    Lives HERE, not in `strategy.py` (where it is still re-exported and where every
    caller still imports it from), since 2026-09-21: `regime.py` builds it and
    `implied.py` imports `regime.py`'s conventions, and `strategy.py` imports
    `implied.py` — so defining it in `strategy.py` would close an import cycle.

    Deliberately NOT computed inside `CreditSpreadStrategy` — `evaluate()` stays a
    pure function of its inputs, the same reasoning as the existing chain/now_et
    split. `signal/regime.py` owns the computation from a `ChainSnapshot`.

    `atm_iv` — same-day ATM implied vol (annualized decimal), a VIX-style
    integrated-variance estimator over the SPXW 0DTE OTM strip, NOT the 30-day
    CBOE VIX index.
    `skew` — up-variance minus down-variance across that same strip: negative
    means puts are pricing more stress than calls (the usual equity skew, more so
    in stress regimes); positive is the reverse.
    `today` — ET calendar date, for the blackout-dates lookup.

    `atm_iv` and `skew` may be NaN when the strip is too thin to estimate from
    (see `regime.py`). `today` NEVER is — which is what lets the event blackout
    depend on this object without inheriting the estimator's failure modes.

    `minutes_to_settlement` is carried because the estimate is NOT scale-free in
    it. Measured against synthetic strips at a constant true vol of 0.20, the
    estimator reads 1.03x at 10:00 ET and 1.68x at 15:45 — the VIX integral's
    discretization error grows as the remaining life shrinks toward SPX's 5-point
    strike spacing. The reading is therefore comparable across days AT THE SAME
    TIME OF DAY and not otherwise, so any threshold calibrated by pooling raw
    intraday readings would be fitting the clock as much as the vol. Recording the
    remaining time is what keeps that conditionable at analysis time instead of
    baked irreversibly into the dataset. It defaults to NaN so a hand-built
    snapshot (tests, tools) stays valid without asserting a time it does not know.
    """

    atm_iv: float
    skew: float
    today: date
    minutes_to_settlement: float = float("nan")

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe telemetry. NaN becomes None: canonical audit hashing refuses
        what `json` cannot encode, and a refused Tier 1 write halts trading — an
        unestimable regime must not be able to take the process down on its way
        into the log (same rule as `SpreadSignal.underlying_price`)."""
        return {
            "atm_iv": payload_number(self.atm_iv),
            "skew": payload_number(self.skew),
            "today": self.today.isoformat(),
            "minutes_to_settlement": payload_number(self.minutes_to_settlement),
        }
