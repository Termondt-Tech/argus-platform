"""Loss-budget sizing: `daily_max_loss` as a bound on the WHOLE DAY. (2026-09-21)

The welded invariant `max_concurrent_positions × max_loss_per_position ≤
daily_max_loss` bounds what is open AT ONCE, checked against a day on which nothing
has been realized. It says nothing about positions opened one AFTER another, and the
breaker cannot close the gap: it blocks entries at −`daily_max_loss` and cannot close
what is already open. So a day could realize −940, pass the breaker, open one more
full-size spread and take it to width — roughly TWICE the stated cap. This module is
the rule that makes the cap a bound on every path.

**The quantity.** At any moment of the trading day:

    W = realized day loss so far + Σ remaining worst-case loss of everything open

The day's final loss can never exceed W (realized loss is final; nothing open can
lose more than its worst case), and ONLY AN ENTRY raises W — an exit turns a worst
case into a realized loss no larger than it, a mark moves neither term, expiry
settles inside the worst case. So refusing every entry that would take W past
`daily_max_loss` keeps `W ≤ daily_max_loss` after every event of the day, by
induction, on every path: stops that overshoot, stops that fail, a broker outage, a
position held to full width. **It needs no model of the market and no working exit.**
Two tolerances are accepted, both ~$10 a contract: fees (the ledger is pre-fee), and
the last entry's walk concession — a proposal is priced at the MID credit and the
entry walk may fill below it. The second does not compound (the next gate reads the
real fill), and pricing the budget at the walk's floor is owed; see the decision's
As built, open question 1.

**Two estimates of W, and the WORSE one governs** — the concurrency gate's own
pattern (the higher of the broker- and ledger-derived counts):

  - `W_ledger = open_worst + working_worst − cash_today`. Mark-free and exact apart
    from fees. `cash_today` is the signed premium of today's own `order.fill` rows,
    `Σ −signed_delta × price × 100` — the sign the broker cannot relabel. The only
    estimate cert can use, since cert's day P&L is structurally zero.
  - `W_broker = −daily_pnl + open_worst + working_worst`. Carries fees and the
    broker's view; guards a ledger short a row. **Counted only when `daily_pnl` is an
    anchored reading AND the environment is live** — cert never marks NLV, so there
    it is a structural zero that would read as "nothing lost".

**`W_broker` takes every open spread's cost-to-close as ZERO, always** — a deliberate
narrowing of the design record's "the mark where there is one". `daily_pnl` already
carries each open spread's unrealized loss at the BROKER's mark, so the remaining
worst case must be measured from that same mark; a watchdog mark read at a different
instant can sit ABOVE it (a spread that has since recovered) and then understates W —
the one direction this rail may never err in. Zero overstates W by exactly the
broker's mark, which is the conservative direction, and it is moot at
`max_concurrent_positions = 1`, where entries only happen flat.

**Open exposure is the UNION of two sources, per trade idea, the larger winning.**
The watchdog's scan knows everything the ledger holds (including a prior day's) and
says when it cannot explain something; the fill ledger here is updated in the SAME
STEP as `cash_today`, so a spread that has just filled is never counted as cash
without its width — the window between a fill and the scan's next refresh is exactly
where a scan-only estimate would read the new credit as a gain.

**Not a latch, and never a kill-switch trip.** It is recomputed on every candidate
and clears with the trading date. Kill-switch trips are durable (2026-09-01), which
would turn a daily limit into a permanent halt — the first standing landmine.

**Fail closed.** Every input that cannot be read makes the remaining budget ZERO,
with the reason recorded: no entry until it can be read again.
→ docs/decisions/2026-09-21-loss-budget-sizing.md
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from argus.execution.models import parse_external_identifier
from argus.occ import occ_strike

#: One SPX option contract's multiplier: a price in points × this = dollars.
CONTRACT_MULTIPLIER = 100

#: Half a cent. The sizer and the gate compare dollar amounts built from float
#: arithmetic (`contracts × max_loss_per_contract` against `daily_max_loss − W`), and a
#: bare comparison would refuse a lot that fits EXACTLY on representation noise — the
#: 2026-09-02 credit stop fired on 4.4e-16. Both sides use this one constant, so what
#: the sizer chooses the gate accepts. Far below a cent, so it can never admit a lot a
#: human would call over budget.
BUDGET_EPSILON = 0.005

_CENT = Decimal("0.01")


def trade_idea_key(external_identifier: str) -> str | None:
    """The trade idea an Argus order belongs to — the candidate key, whichever side.

    `argus-open-<c>` and `argus-close-<c>` are one idea, which is what lets an exit's
    fills net against its entry's. None for anything that is not Argus's identifier
    shape; every caller fails closed on it.
    """
    parsed = parse_external_identifier(external_identifier)
    return None if parsed is None else parsed[1]


def vertical_width(symbol_a: str, symbol_b: str) -> Decimal | None:
    """Strike distance between two OCC symbols, in points; None = cannot say."""
    a, b = occ_strike(symbol_a), occ_strike(symbol_b)
    if a is None or b is None:
        return None
    width = abs(Decimal(str(a)) - Decimal(str(b)))
    return width if width > 0 else None


# -- exposure: what is open, per trade idea ------------------------------------


@dataclass(frozen=True)
class Exposure:
    """Worst-case dollars at risk, per trade idea — or a reason it cannot be said.

    The currency every source of "what is open" speaks, so the combining rule is one
    function. `by_idea` is None exactly when `unknown` names why; a caller never sees
    a partial map standing in for the whole.

    `closed` is the fill ledger's second answer: ideas it has seen OPEN AND CLOSE
    today (every leg nets to zero). Only `DayLedger.exposure` sets it, and
    `compute_loss_budget` lets it overrule the scan for exactly those ideas — see
    there for why that is safe and why it is needed.
    """

    by_idea: Mapping[str, float] | None
    unknown: str | None = None
    closed: frozenset[str] = frozenset()

    @classmethod
    def none_open(cls) -> Exposure:
        return cls(by_idea={})

    @classmethod
    def cannot_say(cls, reason: str) -> Exposure:
        return cls(by_idea=None, unknown=reason)

    @property
    def total(self) -> float | None:
        return None if self.by_idea is None else float(sum(self.by_idea.values()))


# -- the day's fill ledger -----------------------------------------------------


@dataclass(frozen=True)
class LedgerFill:
    """One `order.fill` record's worth of a leg movement — what this rail reads.

    Built from the live `AppliedFill` on the fill path and from the Tier 1 row's
    payload on a restart, and the two MUST agree: the payload is
    `AppliedFill.to_payload()` verbatim, so the same four fields are read both ways.
    """

    external_identifier: str
    symbol: str
    signed_delta: int           # + long, − short: the sign the broker cannot relabel
    price: float | None         # per-contract points; None = the broker priced nothing

    @classmethod
    def from_applied(cls, fill: Any) -> LedgerFill:
        """From an `execution.store.AppliedFill` (duck-typed: no import cycle)."""
        return cls(
            external_identifier=str(fill.external_identifier),
            symbol=str(fill.symbol),
            signed_delta=int(fill.signed_delta),
            price=None if fill.price is None else float(fill.price),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LedgerFill | None:
        """From a Tier 1 `order.fill` payload, or None if the row cannot be read.

        None is not an error to raise here: the caller records the ledger as unable
        to say what is open, which zeroes the budget for the day — fail closed at the
        RAIL, not at the process. A price that is present but unusable is treated as
        unpriced, the same known state the store already has for a slice the broker
        did not price.
        """
        try:
            identifier = payload["external_identifier"]
            symbol = payload["symbol"]
            delta = payload["signed_delta"]
            if not isinstance(identifier, str) or not isinstance(symbol, str):
                return None
            if isinstance(delta, bool) or not isinstance(delta, int):
                return None
            raw_price = payload.get("price")
        except (KeyError, TypeError):
            return None
        price: float | None
        if isinstance(raw_price, (int, float)) and not isinstance(raw_price, bool) and math.isfinite(raw_price):
            price = float(raw_price)
        else:
            price = None
        return cls(external_identifier=identifier, symbol=symbol, signed_delta=delta, price=price)


class DayLedger:
    """Today's premium cash and open legs, from Argus's OWN fills. Per trading date.

    Fed by `FillConsumer` immediately after each `order.fill` record is written, and
    rebuilt from those same rows at session start (`risk.rehydrate`), so a restart
    reproduces the in-memory state exactly. Built once per trading day inside
    `SessionState` — never carried across a rollover, which is the whole reset rule.

    Cash is kept in exact `Decimal` cents: the inputs are prices like 2.05 whose
    float products carry noise, and this number is compared against a dollar cap.
    """

    def __init__(self, fills: Iterable[LedgerFill | None] = ()) -> None:
        self._cash = Decimal(0)
        self._fills = 0
        self._unpriced = 0
        self._unreadable = 0
        #: trade idea -> OCC symbol -> signed net contracts from today's fills.
        self._net: dict[str, dict[str, int]] = {}
        #: Fills whose identifier names no trade idea — their legs cannot be paired.
        self._unattributed = 0
        for fill in fills:
            self.note(fill)

    def note(self, fill: LedgerFill | None) -> None:
        """Book one leg movement. `None` is an `order.fill` row that could not be read."""
        if fill is None:
            self._unreadable += 1
            return
        self._fills += 1
        if fill.price is None:
            self._unpriced += 1
        else:
            price = _decimal(fill.price)
            if price is None:
                self._unpriced += 1
            else:
                self._cash += -fill.signed_delta * price * CONTRACT_MULTIPLIER
        key = trade_idea_key(fill.external_identifier)
        if key is None:
            self._unattributed += 1
            return
        legs = self._net.setdefault(key, {})
        legs[fill.symbol] = legs.get(fill.symbol, 0) + fill.signed_delta

    @property
    def fills(self) -> int:
        return self._fills

    @property
    def cash_today(self) -> float:
        """Signed premium today, in dollars: + collected, − paid. Before fees."""
        return float(self._cash.quantize(_CENT))

    @property
    def cash_complete(self) -> bool:
        """False once any of today's fills was unpriced or unreadable: the cash is short."""
        return self._unpriced == 0 and self._unreadable == 0

    def exposure(self) -> Exposure:
        """Worst case of what today's fills still hold open, per trade idea.

        An idea whose legs net to zero is closed. One still holding legs must be ONE
        balanced vertical — a long and a short of equal size on two strikes — and is
        then worth `width × 100 × contracts`. Anything else (a half-booked multi-leg
        fill, a naked leg, an unparseable strike) cannot be bounded by a width, so the
        whole answer is "cannot say": transient in the mid-fill window, and the
        concurrency cap is refusing entries through that window too.

        Ideas whose legs all net to zero are returned in `closed`: the ledger has
        SEEN them close, which is a positive statement, not an absence.
        """
        if self._unreadable:
            return Exposure.cannot_say("fill_ledger_unreadable")
        if self._unattributed:
            return Exposure.cannot_say("fill_ledger_unattributed")
        out: dict[str, float] = {}
        closed: set[str] = set()
        for key, legs in self._net.items():
            held = {symbol: q for symbol, q in legs.items() if q != 0}
            if not held:
                closed.add(key)
                continue
            worst = _vertical_worst_case(held)
            if worst is None:
                return Exposure.cannot_say("fill_ledger_unpaired_legs")
            out[key] = worst
        return Exposure(by_idea=out, closed=frozenset(closed))


def _vertical_worst_case(held: Mapping[str, int]) -> float | None:
    if len(held) != 2:
        return None
    (sym_a, qa), (sym_b, qb) = held.items()
    if qa + qb != 0:
        return None
    width = vertical_width(sym_a, sym_b)
    if width is None:
        return None
    return float(width * CONTRACT_MULTIPLIER * abs(qa))


def _decimal(x: float) -> Decimal | None:
    if not math.isfinite(x):
        return None
    try:
        return Decimal(str(x))
    except InvalidOperation:
        return None


# -- the budget ----------------------------------------------------------------


@dataclass(frozen=True)
class LossBudget:
    """How much of `daily_max_loss` a NEW entry may still put at risk, and why.

    `remaining = daily_max_loss − W`, W the worse of the two estimates. ZERO whenever
    W cannot be established (`binding == "unknown"`, `unknown_reason` says what), so
    an entry is refused until it can. Negative is possible and meaningful (the
    broker's estimate, with fees, past the cap) and is refused the same as zero.
    """

    daily_max_loss: float
    remaining: float
    worst_case: float | None
    worst_case_ledger: float | None
    worst_case_broker: float | None
    cash_today: float
    open_worst_case: float | None
    working_worst_case: float | None
    ledger_complete: bool
    #: "ledger" | "broker" | "unknown" — which estimate set `remaining`.
    binding: str
    unknown_reason: str | None = None

    @property
    def known(self) -> bool:
        return self.binding != "unknown"

    @classmethod
    def fresh(cls, daily_max_loss: float) -> LossBudget:
        """A day with nothing realized and nothing open: the whole cap is available.

        What shadow reports (it holds nothing) and what a test means by "a budget
        that is not the subject of this test".
        """
        return cls(
            daily_max_loss=daily_max_loss,
            remaining=daily_max_loss,
            worst_case=0.0,
            worst_case_ledger=0.0,
            worst_case_broker=None,
            cash_today=0.0,
            open_worst_case=0.0,
            working_worst_case=0.0,
            ledger_complete=True,
            binding="ledger",
        )


def compute_loss_budget(
    *,
    daily_max_loss: float,
    cash_today: float,
    ledger_complete: bool,
    fill_exposure: Exposure,
    scan_exposure: Exposure,
    working_exposure: Exposure,
    daily_pnl: float,
    daily_pnl_anchored: bool | None,
    broker_pnl_is_real: bool,
) -> LossBudget:
    """THE rule, pure. Every input is already in hand; nothing here reads anything.

    Open exposure is the per-idea UNION of the fill ledger and the watchdog's scan,
    the larger worst case winning where both name an idea. Working opening orders
    ADD on top (a working order is exposure distinct from any filled position — the
    concurrency cap's own rule), each counted as if filled at zero credit.

    **An idea the fill ledger has SEEN CLOSE today is not re-opened by the scan.**
    The union exists for the lag between a fill and the watchdog's next scan, and
    that lag runs in BOTH directions: right after an exit, the ledger already holds
    the closing debit while the scan still lists the spread at full width for up to
    one watchdog pass. Taking the larger there counts the realized loss AND the
    whole width — W overstated by the width, on a budget that reads as KNOWN — so a
    candidate evaluated in that window was refused as `daily_loss_budget_exceeded`
    and spent the day's one `daily_loss.budget_exhausted` page on an artifact,
    silencing the real one later (found in the v0.4.6 review, 2026-09-21). The
    override is safe because the scan reads the positions table the SAME
    `store.apply` writes before each `order.fill`, so outside that lag the two
    cannot disagree about an idea the ledger watched close. It never runs the other
    way: an idea the ledger does NOT know (a prior day's, or a row lost in a crash)
    is still taken from the scan, and one the ledger holds open still takes the max.
    """

    def unknown(reason: str, *, ledger: float | None = None) -> LossBudget:
        return LossBudget(
            daily_max_loss=daily_max_loss,
            remaining=0.0,
            worst_case=None,
            worst_case_ledger=ledger,
            worst_case_broker=None,
            cash_today=cash_today if math.isfinite(cash_today) else 0.0,
            open_worst_case=None,
            working_worst_case=working_exposure.total,
            ledger_complete=ledger_complete,
            binding="unknown",
            unknown_reason=reason,
        )

    if not math.isfinite(daily_max_loss) or daily_max_loss <= 0:
        return unknown("invalid_daily_max_loss")
    for source in (fill_exposure, scan_exposure, working_exposure):
        if source.by_idea is None:
            return unknown(source.unknown or "exposure_unknown")
        if not all(math.isfinite(v) and v >= 0 for v in source.by_idea.values()):
            return unknown("exposure_not_finite")

    assert fill_exposure.by_idea is not None and scan_exposure.by_idea is not None
    scan_open = {
        k: v for k, v in scan_exposure.by_idea.items() if k not in fill_exposure.closed
    }
    ideas = set(fill_exposure.by_idea) | set(scan_open)
    open_worst = float(sum(
        max(fill_exposure.by_idea.get(k, 0.0), scan_open.get(k, 0.0))
        for k in ideas
    ))
    working_worst = working_exposure.total or 0.0
    at_risk = open_worst + working_worst

    w_ledger = (
        _cents(at_risk - cash_today)
        if ledger_complete and math.isfinite(cash_today)
        else None
    )
    w_broker = (
        _cents(at_risk - daily_pnl)
        if broker_pnl_is_real and daily_pnl_anchored is True and math.isfinite(daily_pnl)
        else None
    )
    if w_ledger is None and w_broker is None:
        return unknown("no_complete_estimate")

    if w_broker is None or (w_ledger is not None and w_ledger >= w_broker):
        worst, binding = w_ledger, "ledger"
    else:
        worst, binding = w_broker, "broker"
    assert worst is not None
    return LossBudget(
        daily_max_loss=daily_max_loss,
        remaining=_cents(daily_max_loss - worst),
        worst_case=worst,
        worst_case_ledger=w_ledger,
        worst_case_broker=w_broker,
        cash_today=cash_today,
        open_worst_case=_cents(open_worst),
        working_worst_case=_cents(working_worst),
        ledger_complete=ledger_complete,
        binding=binding,
    )


def _cents(x: float) -> float:
    return round(x, 2)


ExposureFn = Callable[[], Exposure]


@dataclass
class LossBudgetSource:
    """The per-day seam `RiskSnapshotBuilder` reads the budget through.

    Holds today's `DayLedger` plus two late-bound reads off the watchdog, which is
    built after the builder — the `ledger_spread_count` shape. The defaults answer
    "nothing open", which is correct exactly where they are used: shadow, which holds
    nothing and submits nothing, and tests whose subject is not this rail.
    """

    daily_max_loss: float
    ledger: DayLedger
    #: `environment == "live"`. Cert's `daily_pnl` is structurally zero, so there the
    #: broker estimate would read "nothing lost" and is never counted.
    broker_pnl_is_real: bool
    scan_exposure: ExposureFn = field(default=Exposure.none_open)
    working_exposure: ExposureFn = field(default=Exposure.none_open)

    def evaluate(self, *, daily_pnl: float, daily_pnl_anchored: bool | None) -> LossBudget:
        return compute_loss_budget(
            daily_max_loss=self.daily_max_loss,
            cash_today=self.ledger.cash_today,
            ledger_complete=self.ledger.cash_complete,
            fill_exposure=self.ledger.exposure(),
            scan_exposure=self.scan_exposure(),
            working_exposure=self.working_exposure(),
            daily_pnl=daily_pnl,
            daily_pnl_anchored=daily_pnl_anchored,
            broker_pnl_is_real=self.broker_pnl_is_real,
        )


def contracts_within_budget(remaining: float, max_loss_per_contract: float) -> int:
    """Whole lots of `max_loss_per_contract` that fit in `remaining`. The SIZER's half.

    Non-finite or non-positive `remaining` is zero lots: an unreadable budget is a
    spent one. The gate's half is `exceeds_budget`, on the same epsilon.
    """
    if not math.isfinite(remaining) or remaining <= 0:
        return 0
    if not math.isfinite(max_loss_per_contract) or max_loss_per_contract <= 0:
        return 0
    return int((remaining + BUDGET_EPSILON) // max_loss_per_contract)


def exceeds_budget(max_loss: float, remaining: float) -> bool:
    """True when a proposal's defined risk does not fit. The GATE's half.

    Non-finite `remaining` exceeds: the gate refuses what it cannot bound.
    """
    if not math.isfinite(remaining) or not math.isfinite(max_loss):
        return True
    return max_loss > remaining + BUDGET_EPSILON
