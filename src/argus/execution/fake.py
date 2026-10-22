"""FakeOrderExecution — deterministic order execution for the test suite.

Sits behind the same `OrderExecutionPort` as `TastytradeOrderExecution`, so the
whole unit-test suite runs with **no network** and no credentials. (Same role
`FakeMarketData` and `FakeBrokerAccount` play for their layers, and the same seed
for the planned tastytrade simulator.)

**This fake encodes ASSUMPTIONS about broker behaviour, and `argus.tools.order_smoke`
is what verifies them.** Both were checked against the real sandbox on 2026-07-29
and hold:

  * a replace mints a NEW order id, sets `replaces_order_id` on the new order and
    `replacing_order_id` on the old one, and terminates the old one (observed:
    1253729 -> 1253731, old order left `Cancelled`);
  * `external_identifier` is NOT unique-enforced by the broker, so this fake
    deliberately does not reject a duplicate either — see
    `test_fake_allows_duplicate_external_identifiers`. Do not "fix" that; matching
    the broker is the point, and a fake that rejects duplicates would hide the
    exact failure mode chunk 2 has to guard against itself.

If a future SDK or API change contradicts either, this file is what changes —
never the tests that describe reality.

Nothing here models timing: no fill delays, no cadence, no auction. Fills happen
only when a test asks for one.
"""

from __future__ import annotations

import dataclasses

from argus.execution.models import (
    LiveOrders,
    Order,
    OrderAck,
    OrderLeg,
    OrderStatus,
    UnmappableOrder,
)


class FakeOrderExecution:
    def __init__(self, *, first_order_id: int = 1000) -> None:
        self._next_id = first_order_id
        self._orders: dict[int, Order] = {}
        self._unmappable: list[UnmappableOrder] = []
        self.submitted: list[Order] = []      # every order handed to submit()
        self.dry_runs: list[Order] = []       # the subset that was a dry run
        self.cancelled: list[int] = []
        self.replaced: list[tuple[int, Order]] = []
        self._fail_next: Exception | None = None
        self._reject_next = False
        self._reject_reason: str | None = None

    # -- test controls ----------------------------------------------------

    def fail_next(self, exc: Exception) -> None:
        """Make the next port call raise — a transport/API failure."""
        self._fail_next = exc

    def reject_next(self, reason: str | None = None) -> None:
        """Make the next submit come back REJECTED (a broker refusal, not an
        exception — the call succeeds and the order is dead).

        `reason` is the broker's verbatim `reject_reason` prose, mirrored onto the
        returned `Order` exactly as `map_placed_order` does for the real SDK.
        """
        self._reject_next = True
        self._reject_reason = reason

    def set_status(self, broker_order_id: int, status: OrderStatus) -> Order:
        order = dataclasses.replace(self._orders[broker_order_id], status=status)
        self._orders[broker_order_id] = order
        return order

    def fill(self, broker_order_id: int, contracts: int | None = None) -> Order:
        """Fill `contracts` on every leg; the default fills the order completely.

        A partial fill leaves `remaining_quantity > 0` and the status working —
        the state the model exists to represent honestly.
        """
        order = self._orders[broker_order_id]
        legs = tuple(_fill_leg(leg, contracts) for leg in order.legs)
        remaining = max((leg.remaining_quantity or 0) for leg in legs)
        order = dataclasses.replace(
            order,
            legs=legs,
            status=OrderStatus.FILLED if remaining == 0 else OrderStatus.LIVE,
        )
        self._orders[broker_order_id] = order
        return order

    def order(self, broker_order_id: int) -> Order:
        return self._orders[broker_order_id]

    def add_unmappable(
        self,
        *,
        broker_order_id: int | None = None,
        external_identifier: str = "",
        underlying: str = "SPXW",
        raw_status: str | None = "Escrowed",
        reason: str = "unrecognized broker order status",
    ) -> UnmappableOrder:
        """Make the broker report an order this app cannot represent.

        The real trigger is an enum value outside the mirrored set — the default
        here is a status string that is not in `OrderStatus`. Scripted rather than
        derived from a raw SDK object because the point under test is what the
        REST of the system does with one, not the mapping (that is
        `test_execution_models.py`).
        """
        entry = UnmappableOrder(
            broker_order_id=broker_order_id if broker_order_id is not None else self._take_id(),
            external_identifier=external_identifier,
            underlying=underlying,
            raw_status=raw_status,
            reason=reason,
        )
        self._unmappable.append(entry)
        return entry

    # -- OrderExecutionPort ------------------------------------------------

    async def submit(self, order: Order, *, dry_run: bool) -> OrderAck:
        self._raise_if_scripted()
        self.submitted.append(order)
        if dry_run:
            # Mirrors the API: a dry run validates and echoes, but mints no order
            # id and leaves nothing working. A test that treats a dry run as a
            # placed order is a test that would have passed against a bug.
            self.dry_runs.append(order)
            return OrderAck(order=order, dry_run=True, buying_power_effect=0.0, fees=0.0)
        if self._reject_next:
            self._reject_next = False
            rejected = dataclasses.replace(
                order, status=OrderStatus.REJECTED, reject_reason=self._reject_reason
            )
            self._reject_reason = None
            return OrderAck(order=rejected, dry_run=False, errors=("rejected by fake",))
        placed = self._place(order)
        return OrderAck(order=placed, dry_run=False, buying_power_effect=0.0, fees=0.0)

    async def cancel(self, broker_order_id: int) -> None:
        self._raise_if_scripted()
        self.cancelled.append(broker_order_id)
        if broker_order_id in self._orders:
            self.set_status(broker_order_id, OrderStatus.CANCELLED)

    async def replace(self, broker_order_id: int, order: Order) -> Order:
        self._raise_if_scripted()
        self.replaced.append((broker_order_id, order))
        new = self._place(dataclasses.replace(order, replaces_order_id=broker_order_id))
        old = self._orders.get(broker_order_id)
        if old is not None:
            self._orders[broker_order_id] = dataclasses.replace(
                old,
                status=OrderStatus.CANCELLED,
                replacing_order_id=new.broker_order_id,
            )
        return new

    async def get_live_orders(self) -> LiveOrders:
        self._raise_if_scripted()
        return LiveOrders(
            orders=tuple(self._orders.values()),
            unmappable=tuple(self._unmappable),
        )

    # -- internals ---------------------------------------------------------

    def _take_id(self) -> int:
        order_id = self._next_id
        self._next_id += 1
        return order_id

    def _place(self, order: Order) -> Order:
        order_id = self._take_id()
        placed = dataclasses.replace(
            order,
            broker_order_id=order_id,
            status=OrderStatus.RECEIVED,
            legs=tuple(
                dataclasses.replace(leg, remaining_quantity=leg.quantity)
                for leg in order.legs
            ),
        )
        self._orders[order_id] = placed
        return placed

    def _raise_if_scripted(self) -> None:
        if self._fail_next is not None:
            exc, self._fail_next = self._fail_next, None
            raise exc


def _fill_leg(leg: OrderLeg, contracts: int | None) -> OrderLeg:
    outstanding = leg.quantity if leg.remaining_quantity is None else leg.remaining_quantity
    filled = outstanding if contracts is None else min(contracts, outstanding)
    return dataclasses.replace(leg, remaining_quantity=outstanding - filled)
