"""Argus's own order domain types (spec §7).

Deliberate choices, each of which has a failure mode behind it:

- **Status vocabulary is tastytrade's, verbatim.** `OrderStatus` mirrors the
  broker's enum value-for-value — no "pending"/"working"/"done" of our own. A
  parallel vocabulary means every reader has to hold a translation table in their
  head, and the translation is where an order that is actually still live gets
  filed as finished. `tests/test_execution_models.py` asserts our value set equals
  the SDK's exactly, so an SDK that gains a status breaks CI instead of silently
  falling through a mapping. Same for `LegAction`.

- **Partial fills are modelled by QUANTITY, not by a boolean.** Each leg carries
  `remaining_quantity`, because a 3-lot order that fills 1 lot is neither "filled"
  nor "unfilled" — and treating it as either is how you end up cancelling a
  working order you are already partly in, or re-submitting a position you already
  hold. What to DO about a partial is chunk 2/3's problem; representing it
  honestly is this layer's.

- **`replaces_order_id` / `replacing_order_id` are read off the broker's
  response, never reconstructed.** The cancel/replace chain is the broker's own
  bookkeeping; inferring it locally from what we *think* we sent is precisely the
  guess that goes wrong during the crash-recovery scenario it would be needed for.

- **SDK-free.** Like `account.models`, the mapping from the SDK is duck-typed
  (`getattr`), so these types stay independent of tastytrade's shapes and the
  mapping is unit-testable against plain stubs as well as real SDK objects.

Chunk 2 added ONE thing here: `classify_order` — the three-way disposition every
caller that has to ask "may I act on this?" shares. See its docstring for why a
binary terminal/non-terminal read of `OrderStatus` is the wrong question.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any

# A pure OCC symbol parser — no market data, no SDK, no cycle. `Order.width` needs it
# so the walk can bound itself by the order's own strikes rather than by a quote.
from argus.occ import occ_strike

# tastytrade classifies SPX/SPXW index options as "Equity Option" — the same
# instrument type as a single-name option, despite being cash-settled index
# options. This is an assumption about the broker's taxonomy, so it is named once
# here and VALIDATED against the real API by `argus.tools.order_smoke`: a wrong
# instrument type fails the dry run loudly rather than at submission time.
EQUITY_OPTION = "Equity Option"

# Every `external_identifier` Argus mints starts with this (see `build.py`). It is
# how an order read back off the broker can be told apart from one placed by hand
# in the same account — used by the fill consumer, which must not adopt somebody
# else's fill into Argus's own position record.
ORDER_ID_PREFIX = "argus-"


def is_argus_identifier(external_identifier: str | None) -> bool:
    """True if this `external_identifier` was minted by Argus."""
    return bool(external_identifier) and external_identifier.startswith(ORDER_ID_PREFIX)


class OrderDirection(StrEnum):
    """Whether this order opens a position or closes one.

    Carried explicitly rather than inferred at every call site, because it drives
    both the leg actions (`*_TO_OPEN` vs `*_TO_CLOSE`) and the sign of the limit
    price, and getting either backwards routes the opposite trade.
    """

    OPEN = "open"
    CLOSE = "close"


def parse_external_identifier(identifier: str) -> tuple[OrderDirection, str] | None:
    """`external_identifier` in reverse: (direction, candidate key), or None.

    The identifier is the ONLY place an order's own record carries its candidate:
    `candidate_id` never round-trips through the broker, so an `order.fill` event
    reconstructed from a `PlacedOrder` alert has it as NULL (observed on every
    fill of the 2026-08-13 live-fire pass). The identifier survives because the
    broker echoes it back verbatim — which is what makes a fill attributable to a
    trade idea from the audit log alone, with no join to a mutable table.

    Lives here since 2026-09-03 (moved from `execution.build`, unchanged), beside
    the prefix it parses: `build` imports `models`, so the reverse placement would
    cycle. Its new caller is `map_placed_order`, which takes an order's direction
    from the identifier Argus minted rather than from the leg actions the broker
    computed — the same principle `residual_orders.cancel_licence` already applies.
    """
    if not is_argus_identifier(identifier):
        return None
    body = identifier[len(ORDER_ID_PREFIX):]
    head, _, candidate = body.partition("-")
    if not candidate:
        return None
    try:
        direction = OrderDirection(head)
    except ValueError:
        return None
    return (direction, candidate)


def sibling_identifier(
    external_identifier: str, direction: OrderDirection
) -> str | None:
    """The identifier for the SAME candidate under the OTHER direction. (2026-09-24)

    The quantity-aware close rule needs the sibling OPEN identifier of a close it
    is asked about — the opened quantity is an input to `closed_filled + q <=
    opened_filled` — and building it by string surgery at the call site would be
    a SECOND minting path free to drift from `build.external_identifier`. So it
    is derived here, beside the parser, off the SAME `ORDER_ID_PREFIX` and the
    SAME parsed candidate: prefix, direction, candidate, exactly what `build`
    mints for that direction.

    `None` when the identifier does not parse (not Argus's, or a direction this
    grammar does not know): the caller's rule is binary there, per the standing
    fail-closed convention that "cannot say" is never quietly treated as a
    value.
    """
    parsed = parse_external_identifier(external_identifier)
    if parsed is None:
        return None
    _direction, candidate = parsed
    return f"{ORDER_ID_PREFIX}{direction.value}-{candidate}"


class LegAction(StrEnum):
    """Mirrors tastytrade's `OrderAction`, value-for-value."""

    BUY_TO_OPEN = "Buy to Open"
    BUY_TO_CLOSE = "Buy to Close"
    SELL_TO_OPEN = "Sell to Open"
    SELL_TO_CLOSE = "Sell to Close"
    #: futures only — never produced by this app, present so the mapping is total
    BUY = "Buy"
    #: futures only — never produced by this app, present so the mapping is total
    SELL = "Sell"

    @property
    def opens(self) -> bool:
        return self in (LegAction.BUY_TO_OPEN, LegAction.SELL_TO_OPEN)

    @property
    def closes(self) -> bool:
        return self in (LegAction.BUY_TO_CLOSE, LegAction.SELL_TO_CLOSE)

    @property
    def sells(self) -> bool:
        """Premium collected rather than paid — the CASH direction of this leg.

        Orthogonal to `opens`/`closes`, and keeping the two apart is the point: a
        closing BUY and an opening BUY both pay premium, so anything that derives
        cash flow from open-vs-close prices an exit exactly backwards. This is the
        single definition — `store.signed_leg_delta` reads it for the position
        sign and post-close scoring reads it for the cash sign.
        """
        return self in (LegAction.SELL_TO_OPEN, LegAction.SELL_TO_CLOSE, LegAction.SELL)


class OrderStatus(StrEnum):
    """Mirrors tastytrade's `OrderStatus`, value-for-value.

    The happy path is RECEIVED -> LIVE -> FILLED; everything else is a state the
    price-walking loop (chunk 3) and the watchdog (chunk 4) have to reason about.
    """

    RECEIVED = "Received"
    ROUTED = "Routed"
    LIVE = "Live"
    IN_FLIGHT = "In Flight"
    CONTINGENT = "Contingent"
    CANCEL_REQUESTED = "Cancel Requested"
    CANCELLED = "Cancelled"
    REPLACE_REQUESTED = "Replace Requested"
    PARTIALLY_REMOVED = "Partially Removed"
    REMOVED = "Removed"
    FILLED = "Filled"
    REJECTED = "Rejected"
    EXPIRED = "Expired"

    @property
    def is_terminal(self) -> bool:
        """True once the broker will do nothing further with this order.

        `PARTIALLY_REMOVED` is deliberately NOT terminal: part of the order was
        removed and the rest may still be working. Reading it as finished would
        let a caller walk away from a live order — so it fails toward "still
        out there", which is the direction that cannot leave an unmanaged
        position at the exchange.
        """
        return self in _TERMINAL

    @property
    def is_working(self) -> bool:
        return not self.is_terminal


_TERMINAL = frozenset(
    {
        OrderStatus.CANCELLED,
        OrderStatus.REMOVED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)

# Statuses that are CONFIRMED to mean "this order is in the market, and the COA
# window is running on it". Deliberately a single value, and deliberately not
# derived from `is_working`.
#
# This exists because chunk 3's price-walking loop must start its wait-cycle clock
# on the order being genuinely working, NOT on the submission being acknowledged —
# and chunk 1 measured the difference. Pre-market submissions against sandbox on
# 2026-07-29 came back accepted, carried
# `tif.next_valid_session: Your order will begin working during next valid session`,
# and sat at `Received`. So `RECEIVED` is confirmed to be reachable while the order
# is NOT in the market. A loop that started its clock on the ack would walk the
# price of an order no market maker can see.
#
# What each excluded status actually means at tastytrade has not been measured, so
# none of them are in here:
#
#   RECEIVED     measured NOT to imply in-the-market (above).
#   ROUTED       "on its way to the exchange" by name — an in-transit state.
#   IN_FLIGHT    in transit by name.
#   CONTINGENT   waiting on a condition; by definition not yet exposed.
#   *_REQUESTED  a cancel/replace is pending against it.
#
# `LIVE` is the only value whose name asserts the order is working at the
# exchange, and the asymmetry of being wrong settles it. Too STRICT (a status that
# does mean in-the-market is missing here) costs latency: the clock starts late,
# the loop waits longer, and the walk is slower than it needed to be. Too LOOSE
# (a status that does not mean in-the-market is included) is the measured failure:
# the cadence clock runs while the order sits outside the auction, and the first
# reprice cancels an order that never had a chance to fill. Being slow is
# recoverable; racing the auction on every step is not.
#
# Widening this is a MEASUREMENT, not a judgement call: watch a real order's status
# transitions through `AlertStreamer` during regular hours and add what is observed
# between the ack and the fill. Until then, one value.
CONFIRMED_IN_MARKET = frozenset({OrderStatus.LIVE})


def is_in_market(status: OrderStatus | None) -> bool:
    """True only for a status CONFIRMED to mean the order is working at the
    exchange. An unknown/absent status is not confirmation — see
    `CONFIRMED_IN_MARKET`."""
    return status is not None and status in CONFIRMED_IN_MARKET


class UnmappableOrderError(ValueError):
    """A broker order could not be represented as an Argus `Order`.

    Raised for a status or leg action outside the mirrored enums, or a leg set
    whose direction cannot be determined. Fails loud rather than defaulting: a
    guessed status on a live order is worse than no order at all.
    """


@dataclass(frozen=True)
class LegFill:
    """One execution against one leg, as the broker reported it.

    Carried so a recorded position can have a real cost basis instead of a
    placeholder. NOT the idempotency key for fill application: the store keys off
    the cumulative `remaining_quantity` chunk 1 measured against sandbox, and
    treats this list as best-effort price detail that may be absent. `fill_id` is
    kept for the audit trail, not relied on.
    """

    quantity: int
    price: float
    fill_id: str = ""
    filled_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "quantity": self.quantity,
            "price": self.price,
            "fill_id": self.fill_id,
            "filled_at": self.filled_at,
        }


@dataclass(frozen=True)
class OrderLeg:
    """One leg of a multi-leg order.

    `remaining_quantity` is what the BROKER reports is still unfilled. `None`
    means "not reported yet" (an unsent order, or a dry run) — which is
    deliberately distinct from `0` ("fully filled"), because conflating unknown
    with filled is the partial-fill bug this field exists to prevent.

    `fills` is the broker's per-execution detail when it sends any. It is
    deliberately NOT the source of truth for *how much* filled — that is
    `remaining_quantity`, which chunk 1 confirmed the API reports per leg. Fills
    were never observed during chunk 1's sandbox run (nothing was allowed to
    fill), so building quantity accounting on them would be building on an
    unverified assumption. They price the fill; they do not count it.
    """

    symbol: str                       # OCC symbol — what the order references
    action: LegAction
    quantity: int
    remaining_quantity: int | None = None
    instrument_type: str = EQUITY_OPTION
    fills: tuple[LegFill, ...] = ()

    @property
    def filled_quantity(self) -> int | None:
        """Contracts filled on this leg, or None if the broker hasn't said."""
        if self.remaining_quantity is None:
            return None
        return self.quantity - self.remaining_quantity

    def fill_price(self, start: int, end: int) -> float | None:
        """Quantity-weighted average price over the fill slice `[start, end)`.

        `start`/`end` are cumulative contract counts on this leg, so the caller
        can ask "what did the contracts I have not yet booked cost?" without
        needing fills to line up with events. Returns None when the broker
        reported no fill detail covering that slice — the honest answer, which
        the store turns into "keep the existing basis" rather than a made-up zero.
        """
        if end <= start:
            return None
        cursor = 0
        notional = 0.0
        covered = 0
        for fill in self.fills:
            lo, hi = cursor, cursor + fill.quantity
            cursor = hi
            overlap = min(hi, end) - max(lo, start)
            if overlap > 0:
                notional += fill.price * overlap
                covered += overlap
        return notional / covered if covered else None

    def to_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": str(self.action),
            "quantity": self.quantity,
            "remaining_quantity": self.remaining_quantity,
            "instrument_type": self.instrument_type,
            "fills": [f.to_payload() for f in self.fills],
        }


@dataclass(frozen=True)
class Order:
    """A multi-leg order — unsent, working, or terminal.

    ONE object across the whole lifecycle rather than separate unsent/placed
    types: the broker's own response is just this same order with more fields
    populated, and a second type would need the same fields plus a conversion
    that could drift.

    `limit_price` is SIGNED with tastytrade's convention: **positive = credit**
    (money in, opening a credit spread), **negative = debit** (money out, closing
    one). The API itself has no negative numbers — it splits the sign into a
    separate `price-effect` field — so keeping the sign here, in one place, is
    what stops a debit from being submitted as a credit.

    `external_identifier` is Argus's own key, set once per trade idea at
    construction (see `build.py`). It is the handle that makes crash recovery
    possible: after a restart, `get_live_orders()` can be matched against it to
    answer "did I already send this?" without trusting local state.

    `candidate_id` points back at the signal this came from — the same
    (date, side, short strike, long strike) identity `shadow.outcome.trade_ideas`
    dedups on, so a live order joins to its shadow-mode history for free.
    """

    direction: OrderDirection
    underlying: str                       # e.g. "SPXW"
    legs: tuple[OrderLeg, ...]
    limit_price: Decimal                  # signed: + credit, - debit
    external_identifier: str
    candidate_id: str | None = None
    broker_order_id: int | None = None
    status: OrderStatus | None = None
    replaces_order_id: int | None = None   # this order replaced that one
    replacing_order_id: int | None = None  # that order replaced this one
    #: The broker's own explanation for a REJECTED order, verbatim and untouched.
    #:
    #: Added 2026-08-27, the day two orders were routed and rejected inside a
    #: second (1422817, 1422841) and Argus recorded no reason for either — the field
    #: existed on the SDK's `PlacedOrder` the whole time and the mapping never read
    #: it. `Rejected` is on the NOT-YET-EXERCISED list precisely because it had
    #: never fired, so the first time it did was also the first time the gap showed.
    #:
    #: Nothing DECIDES on this and nothing should: it is not parsed, matched or
    #: classified, because a rejection string is broker prose that changes without
    #: notice and a rail built on `startswith` would fail open the day it does.
    #: `classify_order` reads `status` alone, exactly as before. This is evidence
    #: for a human, and its whole job is to be present.
    reject_reason: str | None = None

    @property
    def disposition(self) -> "OrderDisposition":
        """WORKING / FILLED / DEAD — see `classify_order`."""
        return classify_order(self)

    @property
    def is_working(self) -> bool:
        """True if the broker may still act on this order.

        An unknown status (`None`) counts as working — the same fail-closed
        posture as `OrderStatus.PARTIALLY_REMOVED`. Delegates to `classify_order`
        so there is exactly one implementation of "still out there".
        """
        return classify_order(self) is OrderDisposition.WORKING

    @property
    def is_terminal(self) -> bool:
        return not self.is_working

    @property
    def reported_fill_quantity(self) -> int | None:
        """Largest per-leg contract count the broker reports as FILLED.

        `None` means "cannot tell": at least one leg has no `remaining_quantity`
        and no other leg shows a fill, so there is no evidence either way. That is
        deliberately distinct from `0` ("the broker says nothing filled"), because
        the only safe reading of "cannot tell" is that a position may exist.

        `max` rather than `min` across legs on purpose. The question this answers
        is "did this order leave anything behind?", and a single leg filling alone
        — the single-legged outcome the whole atomic-submission rail exists to
        avoid, but which a partial cancel can still produce — leaves a naked
        position that very much counts.
        """
        best = 0
        unknown = False
        for leg in self.legs:
            filled = leg.filled_quantity
            if filled is None:
                unknown = True
            elif filled > best:
                best = filled
        if best > 0:
            return best
        return None if unknown else 0

    @property
    def contracts(self) -> int:
        """Spread contract count. Every leg of a vertical carries the same
        quantity, so this is the smallest — never assume they agree."""
        return min((leg.quantity for leg in self.legs), default=0)

    @property
    def width(self) -> Decimal | None:
        """Strike distance between this order's two legs, in points. (2026-09-17)

        The bound a vertical's price can never exceed, read off the order's OWN OCC
        symbols — **not from a quote**. That is the whole reason this lives here: it
        lets `PriceWalk` cap its floor at the width while importing no market data, so
        a rung stays computable in a completely dead book, which is the property the
        2026-08-31 ladder fix turns on.

        `None` means "cannot say" — not two legs, or a symbol `occ_strike` cannot parse
        — and per `occ.py`'s standing rule every caller must fail closed on it. Here
        that means the floor simply is not capped, which is the pre-2026-09-17
        behaviour; the OPENING limit is capped independently in `build._limit_price`
        from `SpreadSignal.width`, so the two bounds do not share a failure. For an
        order Argus built this cannot be `None`, because `build_order` mints both
        symbols itself.
        """
        if len(self.legs) != 2:
            return None
        strikes = [occ_strike(leg.symbol) for leg in self.legs]
        if any(k is None for k in strikes):
            return None
        return abs(Decimal(str(strikes[0])) - Decimal(str(strikes[1])))

    def with_limit_price(self, price: Decimal) -> "Order":
        """A copy at a new limit, same legs and same `external_identifier`.

        This is what chunk 3's cancel/replace step re-submits: tastytrade's
        `replace_order` sends everything EXCEPT the legs, so a repriced order is
        the same order at a different number, not a new trade idea. Deciding
        *which* number, and when, is the price-walking loop's job — not this
        model's.
        """
        return replace(self, limit_price=price)

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe view for the audit log (canonical hashing forbids NaN/inf)."""
        return {
            "direction": str(self.direction),
            "underlying": self.underlying,
            "legs": [leg.to_payload() for leg in self.legs],
            "limit_price": float(self.limit_price),
            "external_identifier": self.external_identifier,
            "candidate_id": self.candidate_id,
            "broker_order_id": self.broker_order_id,
            "status": str(self.status) if self.status is not None else None,
            "replaces_order_id": self.replaces_order_id,
            "replacing_order_id": self.replacing_order_id,
            "reject_reason": self.reject_reason,
        }


@dataclass(frozen=True)
class UnmappableOrder:
    """A broker order Argus could not represent — KEPT, never dropped.

    The broker reported this order. `map_placed_order` could not turn it into an
    `Order` (an enum value outside the mirrored set, a leg set with no inferable
    direction, no legs at all), so every field an `Order` needs is either absent
    or unreadable. Inventing them — a defaulted status, a guessed direction — is
    the one thing `UnmappableOrderError` exists to prevent, so this type carries
    only what the broker actually said, verbatim.

    **It is deliberately NOT an `Order`.** Nothing can submit it, reprice it,
    cancel-and-replace it, or classify it, because none of those questions have an
    answer for an order whose state we cannot read. What it IS good for is the one
    thing that matters: saying "the broker holds an order here, and Argus cannot
    say what it is doing" to a rail whose whole premise is halting on exactly that.

    `raw_status` is the broker's status string as sent. It is the single most
    useful field in a post-mortem: the fix for the common case (SDK enum drift) is
    one line in `OrderStatus`, and this is what names it.
    """

    broker_order_id: int | None
    external_identifier: str
    underlying: str
    raw_status: str | None
    reason: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "broker_order_id": self.broker_order_id,
            "external_identifier": self.external_identifier,
            "underlying": self.underlying,
            "raw_status": self.raw_status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LiveOrders:
    """Everything the broker reported, split by whether Argus can read it.

    The return type of `OrderExecutionPort.get_live_orders()`, and a result OBJECT
    rather than a list on purpose.

    Until 2026-08-20 the adapter returned `list[Order]` and silently dropped what
    it could not map, on a premise — "the reconciliation rail does not read this"
    — that was true when written and went false the day `reconcile.Reconciler`
    started calling it. A plain list has nowhere to put "and there were three more
    I could not read", so the drop had to be silent; that is the shape of the bug,
    not an implementation detail of it. This type has somewhere to put it, and
    deliberately does NOT define `__iter__`: a caller cannot write
    `for o in live_orders` and receive a quietly filtered view. It has to name
    `.orders` or `.unmappable`, which is the moment it has to think about both.

    → docs/decisions/2026-08-20-unmappable-orders-reach-reconciliation.md
    """

    #: Orders Argus can fully represent. The common case, and the ONLY ones any
    #: caller may act on.
    orders: tuple[Order, ...] = ()
    #: Orders the broker reported that could not be mapped. Never empty silently:
    #: every consumer decides what this means for it, and none may ignore it.
    unmappable: tuple[UnmappableOrder, ...] = ()

    @property
    def has_unmappable(self) -> bool:
        return bool(self.unmappable)

    def unmappable_by_identifier(self, external_identifier: str) -> tuple["UnmappableOrder", ...]:
        """Unmappable orders carrying this key.

        `external_identifier` survives a mapping failure — it is read straight off
        the broker's response, not through any enum — so an order Argus cannot
        classify can still be attributed to the trade idea that placed it. That is
        what lets the duplicate guard block on one.
        """
        return tuple(
            o for o in self.unmappable if o.external_identifier == external_identifier
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "orders": len(self.orders),
            "unmappable": [o.to_payload() for o in self.unmappable],
        }


@dataclass(frozen=True)
class OrderAck:
    """What the broker said about a submission.

    `warnings` and `errors` are carried because tastytrade returns them inside a
    200 response — an order can come back "placed" with an error attached, and
    dropping that leaves the caller believing something the broker didn't say.
    `dry_run` is on the ack, not just the request, so a caller can never mistake
    a validation echo for a routed order.
    """

    order: Order
    dry_run: bool
    buying_power_effect: float | None = None
    fees: float | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "order": self.order.to_payload(),
            "dry_run": self.dry_run,
            "buying_power_effect": self.buying_power_effect,
            "fees": self.fees,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


# -- the shared three-way classification --------------------------------------


class OrderDisposition(StrEnum):
    """What an order MEANS to a caller deciding whether it may act.

    Deliberately three values, not two. The tempting shortcut is
    `OrderStatus.is_terminal` — "terminal means done, done means safe" — and it is
    wrong in one specific, expensive way: `FILLED` is terminal in the
    order-lifecycle sense while being the single least safe thing to resubmit,
    because it means a real position exists. A binary read files it next to
    `CANCELLED` and doubles the position.
    """

    #: The broker may still act on it. Non-terminal, or status unknown.
    WORKING = "working"
    #: It left a position — filled, partially filled, or possibly-filled.
    FILLED = "filled"
    #: Terminal with nothing filled. Nothing can happen and nothing is held.
    DEAD = "dead"


#: The `disposition` string used for an `UnmappableOrder` in payloads that carry
#: one. Deliberately NOT a fourth `OrderDisposition` member: this enum answers
#: "what does this order MEAN to a caller deciding whether it may act", and every
#: member is a conclusion `classify_order` derived. An unmappable order has no
#: derived conclusion — adding it here would invite `classify_order` to return it,
#: and from there some caller to compare against it as if it were a verdict.
#: It is a label for a record, not a disposition.
DISPOSITION_UNMAPPABLE = "unmappable"


def classify_order(order: Order) -> OrderDisposition:
    """The ONE order-status classification in Argus. Both consumers use this.

    Used identically by the duplicate-submission guard (`execution/guard.py`) and
    by order reconciliation (`argus.reconcile.reconcile_orders`) precisely so the
    two can never drift into slightly different readings of the same broker
    status — which is the failure mode that ends with a duplicate spread on one
    side and a "clean" reconcile on the other.

        WORKING  the broker may still act on it (any non-terminal status,
                 `PARTIALLY_REMOVED` included, plus an unknown/None status).
        FILLED   it represents a real position, so acting again would double it.
        DEAD     terminal, and the broker reports nothing filled.

    Two rules inside FILLED are worth stating, because neither falls out of the
    status enum on its own:

    - **A terminal-but-not-FILLED order that partially filled is FILLED here.**
      A 3-lot that fills 1 and is then cancelled reports `CANCELLED`, and it left
      a 1-lot position. It is the same "a real position exists" case as `FILLED`,
      so it gets the same answer. Classifying it DEAD is how a partial fill turns
      into an unmanaged position nobody blocks a resubmission against.
    - **"Cannot tell" counts as FILLED.** If the broker terminated an order
      without reporting per-leg quantities, there is no evidence it left nothing.
      Blocking costs a missed trade, visible in the audit log and clear by the
      next day (`get_live_orders()` only reaches back 24h). Allowing costs a
      doubled position. Fail closed.

    `REJECTED`/`EXPIRED`/`CANCELLED`/`REMOVED` with all legs reporting zero filled
    are DEAD, and DEAD is the crash-recovery/re-entry case that must stay
    unblocked: re-entering the same spread after a cancelled attempt is exactly
    what recovery needs, and chunk 1's sandbox run depended on it working.
    """
    if order.status is None or order.status.is_working:
        return OrderDisposition.WORKING
    if order.status is OrderStatus.FILLED:
        return OrderDisposition.FILLED
    filled = order.reported_fill_quantity
    if filled is None or filled > 0:
        return OrderDisposition.FILLED
    return OrderDisposition.DEAD


# -- SDK mapping (duck-typed; no tastytrade import) ---------------------------


def parse_status(value: Any) -> OrderStatus:
    raw = getattr(value, "value", value)
    try:
        return OrderStatus(raw)
    except ValueError as exc:
        raise UnmappableOrderError(
            f"unrecognized broker order status {raw!r} — the mirrored OrderStatus "
            "enum is out of date with the tastytrade SDK"
        ) from exc


def parse_action(value: Any) -> LegAction:
    raw = getattr(value, "value", value)
    try:
        return LegAction(raw)
    except ValueError as exc:
        raise UnmappableOrderError(
            f"unrecognized broker leg action {raw!r} — the mirrored LegAction "
            "enum is out of date with the tastytrade SDK"
        ) from exc


def infer_direction(legs: tuple[OrderLeg, ...]) -> OrderDirection:
    """Open vs close, from the leg actions the broker reported back.

    Argus never mixes opening and closing legs in one order (a vertical is
    entered as one and exited as one), so any opening leg makes the order an
    open. A leg set with neither suffix — a futures BUY/SELL, which this
    SPX-only app never routes — is unmappable rather than guessed at.
    """
    if any(leg.action.opens for leg in legs):
        return OrderDirection.OPEN
    if any(leg.action.closes for leg in legs):
        return OrderDirection.CLOSE
    raise UnmappableOrderError(
        "cannot infer order direction: no leg carries an open/close action"
    )


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: Longest broker prose kept on an order. The reason is diagnostic text, not a
#: field anything matches on, and it lands in a hash-chained audit payload — an
#: unbounded string from the broker is not something to hand the canonical
#: serializer unmeasured. Generous enough that no realistic reason is clipped.
MAX_REJECT_REASON_CHARS = 500


def _text_or_none(value: Any) -> str | None:
    """Broker prose as a bounded string, or None when there is nothing to say.

    An EMPTY string becomes None deliberately: the API sends `""` for an order it
    did not reject, and "" and None must not read as two different states in a
    payload whose entire purpose is "was there a reason, and what was it".
    """
    if value is None:
        return None
    text = str(value).strip()
    return text[:MAX_REJECT_REASON_CHARS] if text else None


def _map_fills(leg: Any) -> tuple[LegFill, ...]:
    """Map a leg's `fills` when the broker sent any. Best-effort by design.

    A fill whose quantity or price won't parse is DROPPED rather than defaulted:
    these only price a position, and a made-up number in a cost basis is worse
    than an absent one (`fill_price` returns None and the store keeps the basis
    it already had). Quantity accounting never reads this.
    """
    mapped: list[LegFill] = []
    for fill in getattr(leg, "fills", None) or ():
        quantity = _int_or_none(getattr(fill, "quantity", None))
        price = getattr(fill, "fill_price", None)
        if quantity is None or quantity <= 0 or price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price):
            continue
        filled_at = getattr(fill, "filled_at", None)
        mapped.append(
            LegFill(
                quantity=quantity,
                price=price,
                fill_id=str(getattr(fill, "fill_id", "") or ""),
                filled_at=filled_at.isoformat() if hasattr(filled_at, "isoformat")
                else str(filled_at or ""),
            )
        )
    return tuple(mapped)


def map_placed_order(placed: Any) -> Order:
    """Map a tastytrade `PlacedOrder`/`UnplacedOrder` to a domain `Order`.

    Duck-typed so it works for a dry-run echo (no `id`), a live order, and a test
    stub alike. `candidate_id` is not recoverable from the broker — it is Argus's
    own linkage — so an order read back from `get_live_orders()` carries None
    there; `external_identifier`, which the API DOES round-trip, is the field to
    match on.

    `direction` comes from the identifier when the order is Argus's own, and
    falls back to `infer_direction` only for a foreign one: the identifier is a
    field Argus MINTED and the broker echoes verbatim, while the leg actions are
    COMPUTED against the account's position and relabel on a chained book
    (2026-09-03 → docs/decisions/, "relabelled closing legs"). Nothing else about
    the broker's answer is second-guessed — status, price and legs are its facts.
    """
    legs = tuple(
        OrderLeg(
            symbol=leg.symbol,
            action=parse_action(getattr(leg, "action", None)),
            quantity=int(getattr(leg, "quantity", 0) or 0),
            remaining_quantity=_int_or_none(getattr(leg, "remaining_quantity", None)),
            instrument_type=str(getattr(leg, "instrument_type", EQUITY_OPTION)),
            fills=_map_fills(leg),
        )
        for leg in getattr(placed, "legs", ())
    )
    if not legs:
        raise UnmappableOrderError("broker order has no legs")
    price = getattr(placed, "price", None)
    # ONE local, computed ONCE, used for both the parse and the field: computing
    # the identifier twice under different code paths is the seam a future edit
    # silently desyncs. Parsed BEFORE `infer_direction` runs — the minted answer
    # outranks the broker's, and only a foreign order falls through to inference.
    external_identifier = str(getattr(placed, "external_identifier", "") or "")
    parsed = parse_external_identifier(external_identifier)
    direction = parsed[0] if parsed is not None else infer_direction(legs)
    return Order(
        direction=direction,
        underlying=str(getattr(placed, "underlying_symbol", "") or ""),
        legs=legs,
        # The SDK's validator has already applied `price-effect`, so this is
        # signed the same way we send it.
        limit_price=Decimal(str(price)) if price is not None else Decimal(0),
        external_identifier=external_identifier,
        broker_order_id=_int_or_none(getattr(placed, "id", None)),
        status=parse_status(getattr(placed, "status", None)),
        replaces_order_id=_int_or_none(getattr(placed, "replaces_order_id", None)),
        replacing_order_id=_int_or_none(getattr(placed, "replacing_order_id", None)),
        # `getattr` with a default, like every field here: this mapper is duck-typed
        # so it works for a dry-run echo, a live order and a test stub alike, and a
        # source that has no such field must yield None rather than raising.
        reject_reason=_text_or_none(getattr(placed, "reject_reason", None)),
    )


def _raw_status(placed: Any) -> str | None:
    """The broker's status string as sent, without going through `OrderStatus`.

    Read by hand rather than via `parse_status` because this is called precisely
    when `parse_status` has already refused. A `StrEnum` value and a bare string
    both come out as the same text.
    """
    value = getattr(placed, "status", None)
    if value is None:
        return None
    return str(getattr(value, "value", value))


def map_live_order(placed: Any) -> Order | UnmappableOrder:
    """`map_placed_order`, but an unmappable order is RETURNED, not raised.

    This is the mapping `get_live_orders()` uses, and the difference from
    `map_placed_order` is the whole point: reading a list of the account's orders
    must not be all-or-nothing. One order from another instrument with a status
    the mirrored enum lags on would otherwise take out the read that the
    price-walking loop, the duplicate guard AND restart-time reconciliation all
    depend on.

    What it must NOT do is what it did until 2026-08-20: turn that order into
    nothing. An order the broker holds and Argus cannot read is the single most
    important thing the reconciliation rail exists to see, and a dropped row is
    indistinguishable from a flat account. So the failure becomes a value.

    Event-driven mapping (`execution/fills.py`) still uses `map_placed_order` and
    still records loudly per event: there, one frame is one order, so there is no
    list to protect and no reason to soften the failure.
    """
    try:
        return map_placed_order(placed)
    except UnmappableOrderError as exc:
        return UnmappableOrder(
            broker_order_id=_int_or_none(getattr(placed, "id", None)),
            external_identifier=str(getattr(placed, "external_identifier", "") or ""),
            underlying=str(getattr(placed, "underlying_symbol", "") or ""),
            raw_status=_raw_status(placed),
            reason=str(exc),
        )
