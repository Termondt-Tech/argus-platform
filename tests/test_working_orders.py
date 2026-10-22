"""`open_working_spread_equivalents` — working OPENING orders for the concurrency cap.

Finding 6, 2026-08-28: a walk's abandoned order is uncounted exposure until it
dies, and it can outlive the walk. This counts it.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from argus.execution.models import (
    LegAction,
    LiveOrders,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
    UnmappableOrder,
    map_placed_order,
)
from argus.execution.working_orders import open_working_spread_equivalents

OURS = "argus-open-20260828-put-5000-4995"
OURS_2 = "argus-open-20260828-call-5100-5105"


def _order(
    *,
    identifier: str = OURS,
    direction: OrderDirection = OrderDirection.OPEN,
    status: OrderStatus | None = None,
    remaining: int | None = None,
) -> Order:
    short, long_ = (
        (LegAction.SELL_TO_OPEN, LegAction.BUY_TO_OPEN)
        if direction is OrderDirection.OPEN
        else (LegAction.BUY_TO_CLOSE, LegAction.SELL_TO_CLOSE)
    )
    return Order(
        direction=direction,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", short, 2, remaining_quantity=remaining),
            OrderLeg("SPXW  P4995", long_, 2, remaining_quantity=remaining),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=identifier,
        status=status,
    )


def test_a_working_opening_order_counts():
    live = LiveOrders(orders=(_order(),))
    assert open_working_spread_equivalents(live) == 1


def test_nothing_working_counts_zero():
    assert open_working_spread_equivalents(LiveOrders()) == 0


def test_a_dead_opening_order_does_not_count():
    dead = _order(status=OrderStatus.CANCELLED, remaining=2)  # zero filled -> DEAD
    assert open_working_spread_equivalents(LiveOrders(orders=(dead,))) == 0


def test_a_working_CLOSING_order_does_not_count():
    """An exit's own order closes a position `open_spread_equivalent` already
    counts — counting it here would UNDER-count available concurrency."""
    closing = _order(direction=OrderDirection.CLOSE)
    assert open_working_spread_equivalents(LiveOrders(orders=(closing,))) == 0


def test_repriced_rungs_of_one_idea_count_once():
    """A walk reprices by cancel+replace: several rungs momentarily WORKING under
    one `external_identifier` is still one spread."""
    live = LiveOrders(orders=(_order(), _order(), _order()))
    assert open_working_spread_equivalents(live) == 1


def test_two_distinct_ideas_count_two():
    live = LiveOrders(orders=(_order(identifier=OURS), _order(identifier=OURS_2)))
    assert open_working_spread_equivalents(live) == 2


def test_a_foreign_working_order_does_not_count():
    """The cap governs Argus's own concurrent trade ideas; a hand-placed order is
    a reconciliation concern, not an entry-budget one."""
    foreign = _order(identifier="not-argus-12345")
    assert open_working_spread_equivalents(LiveOrders(orders=(foreign,))) == 0


def test_an_unmappable_order_counts_regardless_of_direction():
    """Working-or-worse for every purpose the concurrency rail has — the same
    fail-closed posture the duplicate guard takes."""
    live = LiveOrders(
        unmappable=(
            UnmappableOrder(
                broker_order_id=999,
                external_identifier=OURS,
                underlying="SPXW",
                raw_status="Frobnicated",
                reason="status outside the mirrored set",
            ),
        )
    )
    assert open_working_spread_equivalents(live) == 1


def test_a_working_order_and_its_own_unmappable_rung_count_once():
    live = LiveOrders(
        orders=(_order(identifier=OURS),),
        unmappable=(
            UnmappableOrder(
                broker_order_id=999,
                external_identifier=OURS,
                underlying="SPXW",
                raw_status="Frobnicated",
                reason="x",
            ),
        ),
    )
    assert open_working_spread_equivalents(live) == 1


def test_a_relabelled_closing_order_is_not_counted_as_an_opening_working_order():
    """The 2026-09-01 exit mirror, proven on the production path: order 1430448
    (`argus-close-20260901-put-7605-7600`, `dir=open` in the ledger to this day)
    came back `Buy to Close` on 7605 and a relabelled `Sell to Open` on the
    shared 7600 strike. Pre-fix, `map_placed_order` inferred OPEN from those legs
    and this count held a phantom opening spread — inflating the concurrency
    cap's third term and blocking an entry. Built through the mapper, not by
    hand, so mapper and reader are covered together."""
    placed = SimpleNamespace(
        id=1430448,
        status="Live",
        underlying_symbol="SPXW",
        price=Decimal("-1.25"),
        external_identifier="argus-close-20260901-put-7605-7600",
        legs=[
            SimpleNamespace(
                symbol="SPXW  260901P07605000", action="Buy to Close",
                quantity=2, remaining_quantity=2, instrument_type="Equity Option",
            ),
            SimpleNamespace(
                symbol="SPXW  260901P07600000", action="Sell to Open",
                quantity=2, remaining_quantity=2, instrument_type="Equity Option",
            ),
        ],
    )
    order = map_placed_order(placed)
    assert order.direction is OrderDirection.CLOSE
    assert open_working_spread_equivalents(LiveOrders(orders=(order,))) == 0
