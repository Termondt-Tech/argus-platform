"""Argus's order domain types, and their contract with the tastytrade SDK.

The drift tests are the load-bearing ones: `OrderStatus`/`LegAction` mirror the
SDK's enums value-for-value, and an SDK upgrade that adds or renames a value must
break CI here rather than fall through a mapping at runtime on a live order.
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

import pytest
from tastytrade.order import OrderAction as SdkOrderAction
from tastytrade.order import OrderStatus as SdkOrderStatus

from argus.execution.models import (
    EQUITY_OPTION,
    LegAction,
    Order,
    OrderDirection,
    OrderDisposition,
    OrderLeg,
    OrderStatus,
    UnmappableOrderError,
    infer_direction,
    map_placed_order,
    parse_status,
)


# -- enum drift against the SDK -------------------------------------------


def test_order_status_mirrors_the_sdk_exactly():
    assert {s.value for s in OrderStatus} == {s.value for s in SdkOrderStatus}


def test_leg_action_mirrors_the_sdk_exactly():
    assert {a.value for a in LegAction} == {a.value for a in SdkOrderAction}


# -- status semantics -----------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.REMOVED,
    ],
)
def test_terminal_statuses(status):
    assert status.is_terminal and not status.is_working


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.RECEIVED,
        OrderStatus.ROUTED,
        OrderStatus.LIVE,
        OrderStatus.IN_FLIGHT,
        OrderStatus.CONTINGENT,
        OrderStatus.CANCEL_REQUESTED,
        OrderStatus.REPLACE_REQUESTED,
    ],
)
def test_working_statuses(status):
    assert status.is_working and not status.is_terminal


def test_partially_removed_is_not_terminal():
    """Fail-closed: part of the order may still be working at the exchange.

    Reading this as finished would let a caller walk away from a live order.
    """
    assert OrderStatus.PARTIALLY_REMOVED.is_working


def test_order_with_unknown_status_counts_as_working():
    order = _order(status=None)
    assert order.is_working and not order.is_terminal


# -- partial fills are quantities, not booleans ---------------------------


def test_unreported_remaining_quantity_is_not_zero():
    """`None` (broker hasn't said) must stay distinct from 0 (fully filled)."""
    leg = OrderLeg("SPXW  P4975", LegAction.SELL_TO_OPEN, 3)
    assert leg.remaining_quantity is None
    assert leg.filled_quantity is None


def test_partial_fill_reports_filled_quantity():
    leg = OrderLeg("SPXW  P4975", LegAction.SELL_TO_OPEN, 3, remaining_quantity=2)
    assert leg.filled_quantity == 1


def test_contracts_is_the_smallest_leg_quantity():
    order = _order(
        legs=(
            OrderLeg("A", LegAction.SELL_TO_OPEN, 3),
            OrderLeg("B", LegAction.BUY_TO_OPEN, 2),
        )
    )
    assert order.contracts == 2  # never assume the legs agree


# -- width, off the order's own strikes (2026-09-17) -----------------------


def test_width_is_read_from_the_legs_own_OCC_symbols():
    """The bound a vertical's price can never exceed, and it comes from the SYMBOLS —
    not from a quote. That is what lets `PriceWalk` cap its floor at the width while
    importing no market data, so a rung stays computable in a dead book."""
    order = _order(
        legs=(
            OrderLeg("SPXW  260903C07725000", LegAction.BUY_TO_CLOSE, 2),
            OrderLeg("SPXW  260903C07730000", LegAction.SELL_TO_CLOSE, 2),
        )
    )
    assert order.width == Decimal("5.0")


def test_width_is_the_absolute_distance_whichever_leg_is_higher():
    """Leg order is short-then-long by construction, but for a CALL spread the short
    strike is the lower one and for a PUT it is the higher — so a signed subtraction
    would give a negative width on one side of the book."""
    lower_first = _order(
        legs=(
            OrderLeg("SPXW  260903P07580000", LegAction.SELL_TO_CLOSE, 1),
            OrderLeg("SPXW  260903P07585000", LegAction.BUY_TO_CLOSE, 1),
        )
    )
    assert lower_first.width == Decimal("5.0")


def test_width_handles_a_gapped_ladder():
    """Strike ladders have gaps, so the long leg is the nearest strike within
    `[width, max_width]` and all economics use the ACTUAL distance — never the
    configured desired one."""
    order = _order(
        legs=(
            OrderLeg("SPXW  260903C07725000", LegAction.BUY_TO_CLOSE, 1),
            OrderLeg("SPXW  260903C07735000", LegAction.SELL_TO_CLOSE, 1),
        )
    )
    assert order.width == Decimal("10.0")


@pytest.mark.parametrize(
    "legs",
    [
        # Not a vertical: one leg.
        (OrderLeg("SPXW  260903C07725000", LegAction.BUY_TO_CLOSE, 1),),
        # A symbol `occ_strike` cannot parse.
        (
            OrderLeg("not-an-occ-symbol", LegAction.BUY_TO_CLOSE, 1),
            OrderLeg("SPXW  260903C07730000", LegAction.SELL_TO_CLOSE, 1),
        ),
    ],
)
def test_width_is_None_when_it_cannot_be_said(legs):
    """`occ.py`'s standing rule: `None` means "cannot say" and every caller must fail
    closed on it. For `PriceWalk` that means the floor is simply not capped, which is
    the pre-2026-09-17 behaviour — refusing to build a walk would strand a triggered
    position over a symbol parse. The OPENING limit is capped independently from
    `SpreadSignal.width`, so the two bounds do not share a failure mode.

    For an order Argus built this is unreachable: `build_order` mints both symbols.
    """
    assert _order(legs=legs).width is None


# -- repricing ------------------------------------------------------------


def test_with_limit_price_keeps_identity_and_legs():
    """A repriced order is the SAME trade idea at a different number."""
    order = _order()
    repriced = order.with_limit_price(Decimal("1.30"))
    assert repriced.limit_price == Decimal("1.30")
    assert repriced.external_identifier == order.external_identifier
    assert repriced.legs == order.legs
    assert order.limit_price == Decimal("1.20")  # frozen: original untouched


# -- SDK mapping ----------------------------------------------------------


class _StubLeg:
    def __init__(self, symbol, action, quantity, remaining=None):
        self.symbol = symbol
        self.action = action
        self.quantity = quantity
        self.remaining_quantity = remaining
        self.instrument_type = EQUITY_OPTION


class _StubPlaced:
    def __init__(self, **kw):
        self.id = kw.get("id")
        self.status = kw.get("status", "Live")
        self.underlying_symbol = kw.get("underlying_symbol", "SPXW")
        self.price = kw.get("price", Decimal("1.20"))
        self.external_identifier = kw.get("external_identifier", "argus-open-x")
        self.replaces_order_id = kw.get("replaces_order_id")
        self.replacing_order_id = kw.get("replacing_order_id")
        self.reject_reason = kw.get("reject_reason")
        self.legs = kw.get(
            "legs",
            [
                _StubLeg("SPXW  P4975", "Sell to Open", 1, 1),
                _StubLeg("SPXW  P4970", "Buy to Open", 1, 1),
            ],
        )


def test_map_placed_order_reads_the_broker_response():
    order = map_placed_order(
        _StubPlaced(id=42, status="Live", replaces_order_id=41, replacing_order_id=43)
    )
    assert order.broker_order_id == 42
    assert order.status is OrderStatus.LIVE
    assert order.direction is OrderDirection.OPEN
    assert order.limit_price == Decimal("1.20")
    assert order.external_identifier == "argus-open-x"
    # The replace chain is READ, never reconstructed from what we think we sent.
    assert order.replaces_order_id == 41
    assert order.replacing_order_id == 43
    assert [leg.remaining_quantity for leg in order.legs] == [1, 1]


def test_map_placed_order_handles_a_dry_run_echo():
    """A dry run returns an order with no id — that must not be mistaken for one."""
    order = map_placed_order(_StubPlaced(id=None, status="Received"))
    assert order.broker_order_id is None


def test_map_placed_order_reads_the_rejection_reason():
    """The broker explains itself and the mapping must keep it.

    On 2026-08-27 two orders were routed and rejected inside a second and no
    reason was recorded for either — the field existed on `PlacedOrder` the whole
    time and this mapper never read it.
    """
    order = map_placed_order(
        _StubPlaced(id=1, status="Rejected", reject_reason="Insufficient buying power")
    )
    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason == "Insufficient buying power"
    assert order.to_payload()["reject_reason"] == "Insufficient buying power"


def test_a_source_without_the_field_maps_to_none():
    # This mapper is duck-typed so it serves a dry-run echo, a live order and a
    # test stub alike; a source lacking the field must yield None, not raise.
    stub = _StubPlaced(id=1)
    del stub.reject_reason
    assert map_placed_order(stub).reject_reason is None


def test_an_empty_rejection_reason_is_none_not_empty_string():
    """The API sends "" for an order it did not reject. "" and None must not read
    as two different states in a payload whose whole question is "was there a
    reason, and what was it"."""
    assert map_placed_order(_StubPlaced(id=1, reject_reason="   ")).reject_reason is None
    assert map_placed_order(_StubPlaced(id=1, reject_reason="")).reject_reason is None


def test_a_rejection_reason_is_bounded():
    # Broker prose lands in a hash-chained audit payload; an unbounded string from
    # the broker is not something to hand the canonical serializer unmeasured.
    order = map_placed_order(_StubPlaced(id=1, reject_reason="x" * 5000))
    assert len(order.reject_reason) == 500


def test_the_rejection_reason_does_not_change_classification():
    """Nothing decides on this string, and nothing should: a rejection reason is
    broker prose that changes without notice, and a rail built on matching it
    would fail open the day it does. `classify_order` reads status alone."""
    with_reason = map_placed_order(_StubPlaced(id=1, status="Live", reject_reason="odd"))
    without = map_placed_order(_StubPlaced(id=1, status="Live"))
    assert with_reason.disposition is without.disposition is OrderDisposition.WORKING


def test_map_placed_order_does_not_invent_a_candidate_id():
    # candidate_id is Argus's own linkage; the broker never round-trips it, so
    # matching a live order back to a trade idea goes via external_identifier.
    assert map_placed_order(_StubPlaced(id=1)).candidate_id is None


def test_map_placed_order_infers_close_direction():
    # `argus-close-x` so the fixture is coherent with its own legs: since the
    # 2026-09-03 identifier-over-broker fix, the minted identifier is authoritative
    # and Argus never mints an `argus-open-` identifier on a closing order. The
    # foreign-identifier fall-through is covered separately below.
    order = map_placed_order(
        _StubPlaced(
            id=7,
            external_identifier="argus-close-x",
            legs=[
                _StubLeg("SPXW  P4975", "Buy to Close", 1),
                _StubLeg("SPXW  P4970", "Sell to Close", 1),
            ],
        )
    )
    assert order.direction is OrderDirection.CLOSE


def test_sibling_identifier_round_trips_through_the_grammar():
    """(2026-09-24) The CLOSE quantity rule's sibling OPEN identifier is derived
    by the GRAMMAR, beside the parser — never by string surgery at a call site,
    which would be a second minting path free to drift from
    `build.external_identifier`. A multi-dash candidate must survive whole, an
    unparseable identifier gets no sibling, and both directions round-trip."""
    from argus.execution.models import sibling_identifier

    close_id = "argus-close-20260729-put-5000-4995"
    open_id = "argus-open-20260729-put-5000-4995"

    assert sibling_identifier(close_id, OrderDirection.OPEN) == open_id
    assert sibling_identifier(open_id, OrderDirection.CLOSE) == close_id
    # The derivation is a re-mint, not a splice: it matches what build mints.
    assert sibling_identifier(open_id, OrderDirection.OPEN) == open_id
    # "Cannot say" is a value, never a guess — the standing occ.py rule.
    assert sibling_identifier("hand-placed", OrderDirection.OPEN) is None
    assert sibling_identifier("argus-close-", OrderDirection.OPEN) is None
    assert sibling_identifier("", OrderDirection.OPEN) is None


# -- direction: the identifier Argus minted outranks the broker's leg actions ----
#
# tastytrade labels each leg relative to the ACCOUNT'S position, not to Argus's
# intent, so on a chained book (routine at signal.width = 5.0 against SPX's
# 5-point ladder) single-purpose orders come back with MIXED leg sets. Fixed
# 2026-09-03: `direction` comes from `parse_external_identifier` when the order
# is Argus's own, and `infer_direction` survives as the fallback for a foreign
# order — the case it was actually written for.
# → docs/decisions/2026-08-25-relabelled-closing-legs-open.md


def test_a_relabelled_closing_order_takes_direction_from_the_identifier():
    """The synthetic chained-pair partial close — the case that made this worth
    fixing. Closing 7605/7600 while 7600/7595 is still open means selling to
    close a 7600 the account is net FLAT on, so the broker echoes `Sell to Open`
    on it. Pre-fix, `infer_direction`'s any-opening-leg-wins rule read this CLOSE
    as an OPEN — durably, through `_upsert_order`'s INSERT-only direction
    column."""
    order = map_placed_order(
        _StubPlaced(
            id=1430448,
            external_identifier="argus-close-20260901-put-7605-7600",
            legs=[
                _StubLeg("SPXW  260901P07605000", "Buy to Close", 2, 0),
                _StubLeg("SPXW  260901P07600000", "Sell to Open", 2, 0),
            ],
        )
    )
    assert order.direction is OrderDirection.CLOSE


# The 2026-09-01 relabellings, verbatim off the audit chain — the transformation
# table in docs/decisions/2026-08-25-relabelled-closing-legs-open.md. These are
# the `order.submitted` rows: the order as the broker RETURNED it, which is what
# `map_placed_order` reads. The entry's broker order id is not quoted in the
# record; the load-bearing fields are the identifier, the strikes, the actions.
#   seq 618918  open-...-put-7600-7595   7600 Sell to Close  7595 Buy to Open
#   seq 619079  close-..-put-7605-7600   7605 Buy to Close   7600 Sell to Open
#   seq 625205  close-..-put-7600-7595   7600 Buy to Close   7595 Sell to Open
#   seq 625224  close-..-put-7600-7595   7600 Buy to Close   7595 Sell to Open
@pytest.mark.parametrize(
    "identifier,legs,broker_order_id,expected",
    [
        # 618918 — the benign relabelled ENTRY: "right by luck" pre-fix (the
        # untouched leg was Buy to Open), and the identifier agrees post-fix.
        (
            "argus-open-20260901-put-7600-7595",
            [
                ("SPXW  260901P07600000", "Sell to Close"),
                ("SPXW  260901P07595000", "Buy to Open"),
            ],
            1000,
            OrderDirection.OPEN,
        ),
        # 619079 — the relabelled CLOSE: pre-fix this inferred OPEN, and the
        # ledger row (1430448) still says dir=open, Filled, lim=-1.25.
        (
            "argus-close-20260901-put-7605-7600",
            [
                ("SPXW  260901P07605000", "Buy to Close"),
                ("SPXW  260901P07600000", "Sell to Open"),
            ],
            1430448,
            OrderDirection.CLOSE,
        ),
        # 625205 — relabelled CLOSE, rung 1 of the put-7600-7595 walk (1430542,
        # dir=open, Cancelled, lim=-0.05).
        (
            "argus-close-20260901-put-7600-7595",
            [
                ("SPXW  260901P07600000", "Buy to Close"),
                ("SPXW  260901P07595000", "Sell to Open"),
            ],
            1430542,
            OrderDirection.CLOSE,
        ),
        # 625224 — the same walk's rung 2 (1430543, dir=open, Filled, lim=-0.10):
        # the relabelling is deterministic given the book, so BOTH rungs carried
        # it and `head` being the LAST rung changed nothing.
        (
            "argus-close-20260901-put-7600-7595",
            [
                ("SPXW  260901P07600000", "Buy to Close"),
                ("SPXW  260901P07595000", "Sell to Open"),
            ],
            1430543,
            OrderDirection.CLOSE,
        ),
    ],
)
def test_the_real_2026_09_01_relabellings(identifier, legs, broker_order_id, expected):
    """Regression on the real orders, not synthetic ones — the rows the
    2026-09-03 ledger read found durably `direction = 'open'`. Every CLOSE row
    here would have asserted OPEN before the fix; these correct exactly that."""
    order = map_placed_order(
        _StubPlaced(
            id=broker_order_id,
            external_identifier=identifier,
            legs=[_StubLeg(symbol, action, 2, 0) for symbol, action in legs],
        )
    )
    assert order.direction is expected


def test_a_foreign_identifier_still_falls_through_to_infer_direction():
    """A hand-placed order carries no Argus identifier, so there is no minted
    answer — `infer_direction` on the echoed legs is, and remains, its only
    source of direction. Both directions."""
    opening = map_placed_order(
        _StubPlaced(
            external_identifier="manual-20260903-1",
            legs=[
                _StubLeg("SPXW  P4975", "Sell to Open", 1),
                _StubLeg("SPXW  P4970", "Buy to Open", 1),
            ],
        )
    )
    assert opening.direction is OrderDirection.OPEN

    closing = map_placed_order(
        _StubPlaced(
            external_identifier="manual-20260903-1",
            legs=[
                _StubLeg("SPXW  P4975", "Buy to Close", 1),
                _StubLeg("SPXW  P4970", "Sell to Close", 1),
            ],
        )
    )
    assert closing.direction is OrderDirection.CLOSE


def test_map_placed_order_parses_the_identifier_before_falling_back_to_inference():
    """Pinned structurally (repo convention): the identifier must be computed and
    parsed BEFORE `infer_direction` runs, and the SAME local must feed both the
    direction decision and the `Order.external_identifier` field. A future edit
    that reorders this — or computes the identifier twice under different code
    paths — silently re-opens the exit-mirror bug the decision record closed.
    """
    tree = ast.parse(inspect.getsource(map_placed_order))
    body = tree.body[0].body

    assigns = {
        target.id: node
        for node in body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert {"external_identifier", "parsed", "direction"} <= set(assigns), (
        "map_placed_order must compute external_identifier, parse it, and only "
        "then decide direction"
    )
    assert assigns["external_identifier"].lineno < assigns["direction"].lineno, (
        "the identifier must be computed before the direction is decided"
    )
    assert isinstance(assigns["direction"].value, ast.IfExp), (
        "direction is the parsed answer when present, inference otherwise — one "
        "expression over one `parsed` local"
    )

    # The parse is attempted before any fallback to inference runs.
    called = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("parse_external_identifier", "infer_direction")
    ]
    assert called and called[0] == "parse_external_identifier", (
        "parse_external_identifier must run before infer_direction — the minted "
        "answer outranks the broker's"
    )

    # And the Order constructor reuses the SAME local, never a second computation.
    order_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Order"
    )
    field = {kw.arg: kw.value for kw in order_call.keywords}["external_identifier"]
    assert isinstance(field, ast.Name) and field.id == "external_identifier", (
        "Order.external_identifier must reuse the local the parse read — two "
        "computations under different code paths is the seam a future edit "
        "silently desyncs"
    )


def test_unknown_status_fails_loud():
    with pytest.raises(UnmappableOrderError, match="Escrowed"):
        parse_status("Escrowed")


def test_unknown_leg_action_fails_loud():
    with pytest.raises(UnmappableOrderError):
        map_placed_order(_StubPlaced(id=1, legs=[_StubLeg("X", "Frobnicate", 1)]))


def test_undeterminable_direction_fails_loud():
    # A futures BUY/SELL leg — never routed by this app, and not something to guess at.
    with pytest.raises(UnmappableOrderError, match="direction"):
        infer_direction((OrderLeg("/ESZ6", LegAction.BUY, 1),))


# -- audit payload --------------------------------------------------------


def test_to_payload_is_json_safe():
    import json

    payload = _order(status=OrderStatus.LIVE, broker_order_id=9).to_payload()
    json.dumps(payload, allow_nan=False)  # canonical audit hashing forbids NaN/inf
    assert payload["limit_price"] == 1.20
    assert payload["status"] == "Live"
    assert payload["legs"][0]["action"] == "Sell to Open"


def _order(**overrides) -> Order:
    fields = {
        "direction": OrderDirection.OPEN,
        "underlying": "SPXW",
        "legs": (
            OrderLeg("SPXW  P4975", LegAction.SELL_TO_OPEN, 1),
            OrderLeg("SPXW  P4970", LegAction.BUY_TO_OPEN, 1),
        ),
        "limit_price": Decimal("1.20"),
        "external_identifier": "argus-open-20260728-put-4975-4970",
    }
    fields.update(overrides)
    return Order(**fields)


def test_sells_and_signed_leg_delta_cannot_disagree():
    """ONE definition of which actions are sells. `signed_leg_delta` reads it for
    the position sign and post-close scoring reads it for the CASH sign — and the
    two are not the same question, which is exactly why they must not each keep
    their own list. A closing BUY and an opening BUY both pay premium."""
    from argus.execution.store import signed_leg_delta

    for action in LegAction:
        assert signed_leg_delta(action, 3) == (-3 if action.sells else 3), action


def test_sells_is_orthogonal_to_opens_and_closes():
    """The property that makes an exit price correctly: both directions contain
    one buy and one sell, so cash flow cannot be derived from open-vs-close."""
    assert LegAction.SELL_TO_OPEN.sells and LegAction.SELL_TO_OPEN.opens
    assert LegAction.BUY_TO_OPEN.opens and not LegAction.BUY_TO_OPEN.sells
    assert LegAction.SELL_TO_CLOSE.sells and LegAction.SELL_TO_CLOSE.closes
    assert LegAction.BUY_TO_CLOSE.closes and not LegAction.BUY_TO_CLOSE.sells
