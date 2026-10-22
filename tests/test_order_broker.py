"""The order-execution seam: the port, the tastytrade adapter, and the fake.

No network anywhere — the adapter is exercised against a stub `Account` that
records what it was called with, and everything else runs on
`FakeOrderExecution`. The one thing a stub cannot prove is that the real API
behaves as documented; `argus.tools.order_smoke` is the manual step for that.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from tastytrade.order import LimitOrder, OrderType

from argus.execution.broker import (
    OrderExecutionPort,
    TastytradeOrderExecution,
    to_sdk_order,
)
from argus.execution.fake import FakeOrderExecution
from argus.execution.models import (
    EQUITY_OPTION,
    LegAction,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
)


def _order(**overrides) -> Order:
    fields = {
        "direction": OrderDirection.OPEN,
        "underlying": "SPXW",
        "legs": (
            OrderLeg("SPXW  P4975", LegAction.SELL_TO_OPEN, 1),
            OrderLeg("SPXW  P4970", LegAction.BUY_TO_OPEN, 1),
        ),
        "limit_price": Decimal("1.15"),
        "external_identifier": "argus-open-20260728-put-4975-4970",
        "candidate_id": "20260728-put-4975-4970",
    }
    fields.update(overrides)
    return Order(**fields)


# -- the seam -------------------------------------------------------------


def test_both_implementations_satisfy_the_port():
    assert isinstance(FakeOrderExecution(), OrderExecutionPort)
    assert isinstance(TastytradeOrderExecution(None, None), OrderExecutionPort)


def test_the_four_operations_are_separate_calls():
    """Not a fused construct-and-send: chunk 2 must be able to slot its own Tier 1
    audit write between the decision and the wire (spec §6)."""
    for name in ("submit", "cancel", "replace", "get_live_orders"):
        assert callable(getattr(TastytradeOrderExecution, name))


def test_complex_order_api_is_not_used_anywhere():
    """Spec §6 rules out broker-side stops, so the linked-order API has no place
    in this arc — exits go through a virtual stop and the price-walking loop.

    Parsed rather than grepped: the modules that explain *why* the complex-order
    API is unused name it in prose, and a text search would flag the
    documentation instead of a call.
    """
    forbidden = {"place_complex_order", "delete_complex_order", "ComplexOrderType"}
    src = Path(__file__).resolve().parent.parent / "src" / "argus"
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            name = (
                node.attr if isinstance(node, ast.Attribute)
                else node.id if isinstance(node, ast.Name)
                else None
            )
            if name in forbidden:
                offenders.append(f"{path.relative_to(src)}:{node.lineno} {name}")
    assert offenders == []


# -- SDK order construction -----------------------------------------------


def test_to_sdk_order_builds_one_plain_limit_order():
    sdk = to_sdk_order(_order())
    assert isinstance(sdk, LimitOrder)
    assert sdk.order_type is OrderType.LIMIT      # plain, not a "complex order"
    assert len(sdk.legs) == 2                     # ONE order, both legs
    assert sdk.price == Decimal("1.15")
    assert sdk.external_identifier == "argus-open-20260728-put-4975-4970"
    assert str(sdk.time_in_force) == "Day"        # 0DTE: nothing outlives today
    assert str(sdk.legs[0].action) == "Sell to Open"
    assert str(sdk.legs[0].instrument_type) == EQUITY_OPTION


def test_sdk_serialization_splits_the_sign_into_a_price_effect():
    """tastytrade has no negative numbers on the wire — a debit is a positive
    price plus `price-effect: Debit`. Getting the sign wrong here submits the
    opposite economics, so pin the round trip."""
    import json

    credit = json.loads(to_sdk_order(_order()).model_dump_json(by_alias=True))
    debit = json.loads(
        to_sdk_order(_order(limit_price=Decimal("-1.15"))).model_dump_json(by_alias=True)
    )
    assert credit["price"] == "1.15" and credit["price-effect"] == "Credit"
    assert debit["price"] == "1.15" and debit["price-effect"] == "Debit"


# -- the tastytrade adapter, against a recording stub ---------------------


class _StubLeg:
    def __init__(self, symbol, action, quantity, remaining):
        self.symbol = symbol
        self.action = action
        self.quantity = quantity
        self.remaining_quantity = remaining
        self.instrument_type = EQUITY_OPTION


class _StubPlaced:
    def __init__(self, order_id=555, status="Live", **kw):
        self.id = order_id
        self.status = status
        self.underlying_symbol = "SPXW"
        self.price = Decimal("1.15")
        self.external_identifier = "argus-open-20260728-put-4975-4970"
        self.replaces_order_id = kw.get("replaces_order_id")
        self.replacing_order_id = kw.get("replacing_order_id")
        self.legs = [
            _StubLeg("SPXW  P4975", "Sell to Open", 1, 1),
            _StubLeg("SPXW  P4970", "Buy to Open", 1, 1),
        ]


class _StubResponse:
    def __init__(self, order):
        self.order = order
        self.buying_power_effect = type("BPE", (), {"change_in_buying_power": Decimal("-385")})()
        self.fee_calculation = type("Fees", (), {"total_fees": Decimal("2.60")})()
        self.warnings = None
        self.errors = None


class _StubAccount:
    """Records calls; raises on anything this arc must not touch."""

    account_number = "5WX00000"

    def __init__(self):
        self.calls: list[tuple] = []
        self.live: list = []

    async def place_order(self, session, order, dry_run):
        self.calls.append(("place_order", order, dry_run))
        return _StubResponse(_StubPlaced(status="Received" if not dry_run else "Received"))

    async def replace_order(self, session, old_order_id, new_order):
        self.calls.append(("replace_order", old_order_id, new_order))
        return _StubPlaced(order_id=556, replaces_order_id=old_order_id)

    async def delete_order(self, session, order_id):
        self.calls.append(("delete_order", order_id))

    async def get_live_orders(self, session):
        self.calls.append(("get_live_orders",))
        return self.live

    async def place_complex_order(self, *a, **kw):  # pragma: no cover - must not run
        raise AssertionError("spreads route as a plain multi-leg order, never a complex order")


async def test_submit_places_one_plain_order_and_maps_the_response():
    account = _StubAccount()
    execution = TastytradeOrderExecution(object(), account)

    ack = await execution.submit(_order(), dry_run=False)

    name, sent, dry_run = account.calls[0]
    assert name == "place_order" and dry_run is False
    assert isinstance(sent, LimitOrder) and len(sent.legs) == 2
    assert ack.dry_run is False
    assert ack.order.broker_order_id == 555
    assert ack.order.status is OrderStatus.RECEIVED
    assert ack.buying_power_effect == -385.0
    assert ack.fees == 2.60
    # candidate_id is Argus's own linkage — the broker can't echo it, so the
    # adapter carries it across or the trail back to the signal is lost.
    assert ack.order.candidate_id == "20260728-put-4975-4970"


async def test_submit_forwards_dry_run():
    account = _StubAccount()
    ack = await TastytradeOrderExecution(object(), account).submit(_order(), dry_run=True)
    assert account.calls[0][2] is True
    assert ack.dry_run is True


def test_submit_requires_an_explicit_dry_run_choice():
    """No default in either direction: routing vs validating must be stated."""
    import inspect

    param = inspect.signature(TastytradeOrderExecution.submit).parameters["dry_run"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


async def test_cancel_deletes_by_id():
    account = _StubAccount()
    await TastytradeOrderExecution(object(), account).cancel(555)
    assert account.calls == [("delete_order", 555)]


async def test_replace_reads_the_link_off_the_response():
    account = _StubAccount()
    execution = TastytradeOrderExecution(object(), account)

    replaced = await execution.replace(555, _order().with_limit_price(Decimal("1.10")))

    name, old_id, sent = account.calls[0]
    assert name == "replace_order" and old_id == 555
    assert sent.price == Decimal("1.10")
    assert replaced.broker_order_id == 556
    assert replaced.replaces_order_id == 555   # from the broker, not reconstructed
    assert replaced.candidate_id == "20260728-put-4975-4970"


async def test_get_live_orders_does_not_fail_the_whole_read_on_one_bad_order(caplog):
    """A hand-placed or non-option order in the account must not take out the call
    chunk 3 uses to find its own working orders."""
    account = _StubAccount()
    unmappable = _StubPlaced(order_id=999)
    unmappable.status = "Escrowed"
    account.live = [_StubPlaced(order_id=555), unmappable]

    with caplog.at_level("WARNING"):
        answer = await TastytradeOrderExecution(object(), account).get_live_orders()

    assert [o.broker_order_id for o in answer.orders] == [555]
    assert "999" in caplog.text  # never silent


async def test_get_live_orders_carries_the_unmappable_instead_of_dropping_it():
    """The C2 fix. An order the mapper refuses must survive the read.

    Until 2026-08-20 it did not: the adapter dropped it and logged, on a comment
    saying reconciliation never read this call. Reconciliation reads it, so one
    unrecognized status string could hide an unknown WORKING or FILLED order from
    the rail that exists to halt on exactly that.
    """
    account = _StubAccount()
    unmappable = _StubPlaced(order_id=999)
    unmappable.status = "Escrowed"
    unmappable.external_identifier = "argus-somebody-elses-idea"
    account.live = [_StubPlaced(order_id=555), unmappable]

    answer = await TastytradeOrderExecution(object(), account).get_live_orders()

    (carried,) = answer.unmappable
    assert answer.has_unmappable
    assert carried.broker_order_id == 999
    # The RAW status string, verbatim — the enum is what refused it, so this is
    # the field that names the fix.
    assert carried.raw_status == "Escrowed"
    assert "Escrowed" in carried.reason
    # The identifier survives a mapping failure (it is read straight off the
    # response, not through any enum), which is what lets the guard block on one.
    assert carried.external_identifier == "argus-somebody-elses-idea"


async def test_live_orders_cannot_be_iterated_as_if_it_were_a_list():
    """`for o in live_orders` must not compile away the unmappable ones.

    The defect's shape was "a return type with nowhere to put the bad rows". A
    `LiveOrders` that iterated its good rows would put them back in that position
    for the next caller who reached for the obvious loop.
    """
    account = _StubAccount()
    account.live = [_StubPlaced(order_id=555)]
    answer = await TastytradeOrderExecution(object(), account).get_live_orders()

    with pytest.raises(TypeError):
        list(answer)


# -- the deterministic fake ------------------------------------------------


async def test_fake_dry_run_mints_no_order():
    fake = FakeOrderExecution()
    ack = await fake.submit(_order(), dry_run=True)
    assert ack.dry_run is True
    assert ack.order.broker_order_id is None
    assert (await fake.get_live_orders()).orders == ()


async def test_fake_submit_places_a_working_order():
    fake = FakeOrderExecution()
    ack = await fake.submit(_order(), dry_run=False)
    assert ack.order.broker_order_id == 1000
    assert ack.order.status is OrderStatus.RECEIVED
    assert ack.order.is_working
    assert [leg.remaining_quantity for leg in ack.order.legs] == [1, 1]


async def test_fake_models_a_partial_fill_by_quantity():
    fake = FakeOrderExecution()
    ack = await fake.submit(_order(legs=(
        OrderLeg("SPXW  P4975", LegAction.SELL_TO_OPEN, 3),
        OrderLeg("SPXW  P4970", LegAction.BUY_TO_OPEN, 3),
    )), dry_run=False)

    partial = fake.fill(ack.order.broker_order_id, contracts=1)
    assert [leg.remaining_quantity for leg in partial.legs] == [2, 2]
    assert partial.is_working             # neither filled nor unfilled

    complete = fake.fill(ack.order.broker_order_id)
    assert complete.status is OrderStatus.FILLED
    assert all(leg.filled_quantity == 3 for leg in complete.legs)


async def test_fake_cancel_terminates_the_order():
    fake = FakeOrderExecution()
    ack = await fake.submit(_order(), dry_run=False)
    await fake.cancel(ack.order.broker_order_id)
    assert fake.order(ack.order.broker_order_id).status is OrderStatus.CANCELLED


async def test_fake_allows_duplicate_external_identifiers():
    """Mirrors VERIFIED broker behaviour (sandbox, 2026-07-29): tastytrade accepts
    a second order with the same `external_identifier`, both while the first is
    working and after it is terminal.

    The fake must not be "hardened" to reject duplicates. Doing so would hide the
    fact that spec §9's duplicate-submission risk has no broker-side backstop —
    chunk 2 has to check `get_live_orders()` itself, and a fake that bounced the
    second submit would let that omission pass its tests.
    """
    fake = FakeOrderExecution()
    first = await fake.submit(_order(), dry_run=False)
    second = await fake.submit(_order(), dry_run=False)

    assert second.order.broker_order_id != first.order.broker_order_id
    assert second.order.external_identifier == first.order.external_identifier
    live = await fake.get_live_orders()
    assert len([o for o in live.orders if o.is_working]) == 2


async def test_fake_replace_links_the_pair():
    """Encodes VERIFIED behaviour (sandbox, 2026-07-29): new id, linked both ways,
    old order left terminal."""
    fake = FakeOrderExecution()
    ack = await fake.submit(_order(), dry_run=False)
    old_id = ack.order.broker_order_id

    new = await fake.replace(old_id, ack.order.with_limit_price(Decimal("1.10")))

    assert new.broker_order_id != old_id
    assert new.replaces_order_id == old_id
    assert fake.order(old_id).replacing_order_id == new.broker_order_id
    assert fake.order(old_id).status is OrderStatus.CANCELLED
    assert new.external_identifier == ack.order.external_identifier  # same trade idea


async def test_fake_can_script_a_rejection_and_a_failure():
    fake = FakeOrderExecution()
    fake.reject_next()
    ack = await fake.submit(_order(), dry_run=False)
    assert ack.order.status is OrderStatus.REJECTED and ack.errors

    fake.fail_next(RuntimeError("broker unreachable"))
    with pytest.raises(RuntimeError):
        await fake.submit(_order(), dry_run=False)


async def test_fake_has_no_timing_behaviour():
    """No cadence, no auto-fill, no auction: nothing in this chunk loops."""
    fake = FakeOrderExecution()
    ack = await fake.submit(_order(), dry_run=False)
    assert fake.order(ack.order.broker_order_id).status is OrderStatus.RECEIVED
