"""The three-way order classification — the one both chunk 2 consumers share.

The tests that matter here are the ones that would pass against a plain
terminal/non-terminal check and shouldn't: `FILLED` is terminal and must still
block, and a partially-filled-then-cancelled order is terminal, reports
`CANCELLED`, and left a real position.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from argus.execution.models import (
    LegAction,
    Order,
    OrderDirection,
    OrderDisposition,
    OrderLeg,
    OrderStatus,
    classify_order,
    is_argus_identifier,
)

WORKING_STATUSES = [
    OrderStatus.RECEIVED,
    OrderStatus.ROUTED,
    OrderStatus.LIVE,
    OrderStatus.IN_FLIGHT,
    OrderStatus.CONTINGENT,
    OrderStatus.CANCEL_REQUESTED,
    OrderStatus.REPLACE_REQUESTED,
    OrderStatus.PARTIALLY_REMOVED,
]
DEAD_STATUSES = [
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
    OrderStatus.REMOVED,
]


def _order(status, *, remaining=(1, 1), quantity=1) -> Order:
    """A two-leg vertical. `remaining` is per leg; None means 'broker hasn't said'."""
    return Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", LegAction.SELL_TO_OPEN, quantity, remaining[0]),
            OrderLeg("SPXW  P4995", LegAction.BUY_TO_OPEN, quantity, remaining[1]),
        ),
        limit_price=Decimal("0.85"),
        external_identifier="argus-open-20260729-put-5000-4995",
        status=status,
    )


# -- WORKING ------------------------------------------------------------------


@pytest.mark.parametrize("status", WORKING_STATUSES)
def test_non_terminal_statuses_are_working(status):
    assert classify_order(_order(status)) is OrderDisposition.WORKING


def test_partially_removed_with_quantity_outstanding_is_working():
    # Called out explicitly in the chunk brief: part of the order was removed and
    # the rest may still be out there. Reading it as finished would let a caller
    # walk away from a live order.
    order = _order(OrderStatus.PARTIALLY_REMOVED, remaining=(2, 2), quantity=3)
    assert classify_order(order) is OrderDisposition.WORKING


def test_unknown_status_is_working():
    # No status at all (an unsent order, or a stub) fails toward "might still be
    # out there" — the direction that cannot leave an unmanaged position.
    assert classify_order(_order(None)) is OrderDisposition.WORKING


# -- FILLED (blocking) --------------------------------------------------------


def test_filled_is_not_safe_to_resubmit():
    # THE trap. FILLED is terminal in the lifecycle sense, so a binary
    # terminal/non-terminal check files it next to CANCELLED and doubles the
    # position on the next submission.
    order = _order(OrderStatus.FILLED, remaining=(0, 0))
    assert order.status.is_terminal          # terminal, yes...
    assert classify_order(order) is OrderDisposition.FILLED  # ...and still blocking


@pytest.mark.parametrize("status", DEAD_STATUSES)
def test_terminal_after_a_partial_fill_is_filled(status):
    # A 3-lot that filled 1 and was then cancelled: status says CANCELLED, the
    # account says "you are in a 1-lot". The position is what counts.
    order = _order(status, remaining=(2, 2), quantity=3)
    assert classify_order(order) is OrderDisposition.FILLED


def test_a_single_legged_fill_counts():
    # Only the short leg filled. That is a naked short — very much a position —
    # even though the other leg reports nothing. Hence max, not min, across legs.
    order = _order(OrderStatus.CANCELLED, remaining=(0, 1))
    assert classify_order(order) is OrderDisposition.FILLED


@pytest.mark.parametrize("status", DEAD_STATUSES)
def test_terminal_with_unreported_quantities_fails_closed(status):
    # No evidence either way. Blocking costs a missed trade; allowing costs a
    # doubled position.
    order = _order(status, remaining=(None, None))
    assert classify_order(order) is OrderDisposition.FILLED


def test_one_unreported_leg_with_another_reporting_a_fill_is_filled():
    order = _order(OrderStatus.CANCELLED, remaining=(0, None))
    assert classify_order(order) is OrderDisposition.FILLED


# -- DEAD (the re-entry case that must stay allowed) --------------------------


@pytest.mark.parametrize("status", DEAD_STATUSES)
def test_terminal_with_nothing_filled_is_dead(status):
    # Blocking this would be a defect, not a safety feature: re-entering the same
    # spread after a cancelled attempt is what crash recovery needs, and it is
    # what chunk 1's sandbox run relied on working.
    order = _order(status, remaining=(1, 1))
    assert classify_order(order) is OrderDisposition.DEAD


# -- the property wrappers stay in step ---------------------------------------


@pytest.mark.parametrize("status", WORKING_STATUSES + DEAD_STATUSES + [OrderStatus.FILLED])
def test_is_working_is_exactly_the_working_disposition(status):
    order = _order(status, remaining=(1, 1))
    assert order.is_working is (order.disposition is OrderDisposition.WORKING)
    assert order.is_terminal is not order.is_working


# -- reported_fill_quantity: unknown is not zero ------------------------------


def test_reported_fill_quantity_distinguishes_unknown_from_zero():
    assert _order(OrderStatus.LIVE, remaining=(1, 1)).reported_fill_quantity == 0
    assert _order(OrderStatus.LIVE, remaining=(None, None)).reported_fill_quantity is None
    assert _order(OrderStatus.LIVE, remaining=(0, 1)).reported_fill_quantity == 1


# -- identifier provenance ----------------------------------------------------


@pytest.mark.parametrize(
    "identifier,ours",
    [
        ("argus-open-20260729-put-5000-4995", True),
        ("argus-close-20260729-put-5000-4995", True),
        ("some-humans-order", False),
        ("", False),
        (None, False),
    ],
)
def test_is_argus_identifier(identifier, ours):
    assert is_argus_identifier(identifier) is ours
