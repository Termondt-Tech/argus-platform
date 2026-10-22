"""The order-execution seam and its tastytrade adapter (spec §7).

`OrderExecutionPort` is four SEPARATE async calls — submit, cancel, replace,
get_live_orders — and that separation is the design, not an accident of listing
them. A fused "construct and send" or "cancel then replace" helper would make it
impossible for the caller to slot its own Tier 1 audit write between the decision
and the wire (spec §6: the gate decision and the submission are durable *before*
the next action proceeds). Keeping the primitives atomic leaves that sandwich to
chunk 2, which is the layer that owns it.

Consequently there is no looping, cadence, retry, or price-stepping anywhere in
this module. One call in, one response out.

**Plain multi-leg orders only.** A vertical spread is a plain limit order with two
legs, routed through `place_order` / `replace_order` / `delete_order`.
tastytrade's separate "complex order" API (`place_complex_order`,
`ComplexOrderType` = OCO/OTO/OTOCO/PAIRS) is for LINKED orders — an entry with an
attached stop and take-profit — which spec §6 rules out outright: no broker-side
stop orders, exits go through a virtual stop and the price-walking loop instead.
`place_complex_order`/`delete_complex_order` are therefore not used anywhere in
this arc, and `tests/test_order_broker.py` asserts that repo-wide.

**`LimitOrder`, not `NewOrder`.** Same endpoint, same plain (non-complex) path —
`NewOrder` is deprecated as of tastytrade SDK 13.x in favour of the per-type
classes, so using it would emit a DeprecationWarning on every submission for no
behavioural difference.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Protocol, runtime_checkable

from tastytrade.order import InstrumentType, Leg, LimitOrder, OrderAction, OrderTimeInForce

from argus.execution.models import (
    LiveOrders,
    Order,
    OrderAck,
    UnmappableOrder,
    map_live_order,
    map_placed_order,
)

logger = logging.getLogger("argus.execution")

# 0DTE: the position either closes or expires today, so a day order is the only
# time-in-force that makes sense. GTC on an instrument that ceases to exist at the
# close is a contradiction, not a tunable.
TIME_IN_FORCE = OrderTimeInForce.DAY


@runtime_checkable
class OrderExecutionPort(Protocol):
    """Atomic broker order operations. Implemented by the tastytrade adapter and
    by `FakeOrderExecution`, so everything above this line is testable offline."""

    async def submit(self, order: Order, *, dry_run: bool) -> OrderAck:
        """Place `order` as ONE multi-leg order.

        `dry_run` has no default on purpose: a caller must state whether it is
        validating or routing. A default — in either direction — is a value
        somebody can forget to think about on the one call that moves money.
        """

    async def cancel(self, broker_order_id: int) -> None:
        """Request cancellation. Returns when the broker has accepted the request,
        which is NOT the same as the order being cancelled — the status goes to
        CANCEL_REQUESTED first."""

    async def replace(self, broker_order_id: int, order: Order) -> Order:
        """Replace a working order with the same legs at new terms.

        There is no dry-run form: tastytrade offers none for replace, so every
        call here is live. The legs are not re-sent (the API replaces price/TIF
        only), which is why `Order.with_limit_price` is the right way to build the
        argument — same legs, same `external_identifier`, new number.
        """

    async def get_live_orders(self) -> LiveOrders:
        """Today's orders for the account, as the broker sees them.

        The authoritative answer to "what is actually working right now" — used
        after a restart to match Argus's `external_identifier` against reality
        rather than trusting local state (spec §6: the broker is the only source
        of truth).

        Returns `LiveOrders`, not a list, because "what the broker holds" and
        "what Argus can represent" are not the same set and every caller has to
        know the difference. See `LiveOrders`.
        """


def to_sdk_order(order: Order) -> LimitOrder:
    """Domain `Order` -> the SDK's plain multi-leg limit order."""
    return LimitOrder(
        time_in_force=TIME_IN_FORCE,
        legs=[
            Leg(
                instrument_type=InstrumentType(leg.instrument_type),
                symbol=leg.symbol,
                action=OrderAction(str(leg.action)),
                quantity=leg.quantity,
            )
            for leg in order.legs
        ],
        price=order.limit_price,
        external_identifier=order.external_identifier,
    )


def _messages(items: Any) -> tuple[str, ...]:
    return tuple(str(m) for m in (items or ()))


def _scalar(obj: Any, attr: str) -> float | None:
    value = getattr(obj, attr, None) if obj is not None else None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ack_from_response(response: Any, *, dry_run: bool, candidate_id: str | None) -> OrderAck:
    """Map a `PlacedOrderResponse` to an `OrderAck`.

    `candidate_id` is carried over from the order we sent because the broker has
    no idea about it — it is Argus's own linkage, and losing it on the response
    would break the trail from a live order back to the candidate that produced it.
    """
    order = map_placed_order(response.order)
    if candidate_id is not None:
        order = dataclasses.replace(order, candidate_id=candidate_id)
    return OrderAck(
        order=order,
        dry_run=dry_run,
        buying_power_effect=_scalar(
            getattr(response, "buying_power_effect", None), "change_in_buying_power"
        ),
        fees=_scalar(getattr(response, "fee_calculation", None), "total_fees"),
        warnings=_messages(getattr(response, "warnings", None)),
        errors=_messages(getattr(response, "errors", None)),
    )


class TastytradeOrderExecution:
    """Thin async wrapper over `tastytrade.Account`'s order methods.

    Thin is the requirement: every method is one SDK call plus a pure mapping, so
    there is nothing here that can behave differently from what
    `argus.tools.order_smoke` validates against the real sandbox.

    The SDK's order methods are already coroutines, so — unlike the audit log's
    disk writes — they do NOT need `asyncio.to_thread` to stay off the event loop
    (spec §4).

    `session` must be a TRADING session. `TastytradeSession.trading_session`
    enforces that structurally by refusing to hand over a market-data session,
    which may be pointed at the live API.
    """

    def __init__(self, session: Any, account: Any) -> None:
        # session: tastytrade Session; account: tastytrade Account.
        self._session = session
        self._account = account

    @property
    def account_number(self) -> str:
        return getattr(self._account, "account_number", "")

    async def submit(self, order: Order, *, dry_run: bool) -> OrderAck:
        response = await self._account.place_order(
            self._session, to_sdk_order(order), dry_run
        )
        return ack_from_response(
            response, dry_run=dry_run, candidate_id=order.candidate_id
        )

    async def cancel(self, broker_order_id: int) -> None:
        await self._account.delete_order(self._session, broker_order_id)

    async def replace(self, broker_order_id: int, order: Order) -> Order:
        placed = await self._account.replace_order(
            self._session, broker_order_id, to_sdk_order(order)
        )
        replaced = map_placed_order(placed)
        if order.candidate_id is None:
            return replaced
        return dataclasses.replace(replaced, candidate_id=order.candidate_id)

    async def get_live_orders(self) -> LiveOrders:
        """Map today's live orders, CARRYING any this app cannot represent.

        A sandbox (or live) account can hold orders Argus did not place — placed
        by hand, or in another instrument entirely — and raising on one of them
        would take out the call that chunk 3 uses to find ITS OWN working orders
        by `external_identifier`. So one bad row must not fail the whole read.

        **It must not vanish either, and until 2026-08-20 it did.** This method
        used to drop the row and log a WARNING, justified by a comment saying the
        reconciliation rail did not read it. That was true when it was written and
        false by the time anyone relied on it: `reconcile.Reconciler.reconcile`
        now calls this and feeds the answer straight into `reconcile_orders`, so a
        single unrecognized status string was enough to hide an unknown WORKING or
        FILLED order from the one rail whose entire premise is "halt on anything
        unexplained". The duplicate guard read it the same way, and would not have
        blocked a second order against an unmappable working one.

        The fix is not to drop more carefully — it is to not drop. An unmappable
        order comes back as an `UnmappableOrder` in `LiveOrders.unmappable`, where
        every consumer has to answer for it. Whoever adds the next caller: that
        field is not optional detail, it is the reason this type exists.

        → docs/decisions/2026-08-20-unmappable-orders-reach-reconciliation.md
        """
        placed = await self._account.get_live_orders(self._session)
        orders: list[Order] = []
        unmappable: list[UnmappableOrder] = []
        for raw in placed:
            mapped = map_live_order(raw)
            if isinstance(mapped, UnmappableOrder):
                # Still logged here — the adapter is where the raw row was seen —
                # but the log is no longer the ONLY trace. `AuditedOrderExecution`
                # turns this into a Tier 1 record and a phone-reachable alert, and
                # reconciliation halts on it.
                logger.warning(
                    "unmappable broker order id=%s status=%r: %s",
                    mapped.broker_order_id,
                    mapped.raw_status,
                    mapped.reason,
                )
                unmappable.append(mapped)
            else:
                orders.append(mapped)
        return LiveOrders(orders=tuple(orders), unmappable=tuple(unmappable))
