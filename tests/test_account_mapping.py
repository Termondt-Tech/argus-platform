"""Account mapping (wire -> domain) against real tastytrade SDK objects."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tastytrade.account import AccountBalance, CurrentPosition

from argus.account.models import map_balances, map_position, signed_quantity
from argus.account.rest import BrokerAccount


def _position(**fields) -> CurrentPosition:
    base = dict(
        account_number="5WX00000",
        symbol="SPXW260718P05000000",
        instrument_type="Equity Option",
        underlying_symbol="SPX",
        quantity=Decimal("1"),
        quantity_direction="Long",
        average_open_price=Decimal("1.20"),
        multiplier=100,
    )
    base.update(fields)
    return CurrentPosition.model_construct(**base)


@pytest.mark.parametrize(
    "direction,qty,expected",
    [("Long", 2, 2), ("Short", 3, -3), ("Zero", 5, 0), ("long", 1, 1)],
)
def test_signed_quantity(direction, qty, expected):
    assert signed_quantity(qty, direction) == expected


def test_map_position_long():
    p = map_position(_position(quantity=Decimal("2"), quantity_direction="Long"))
    assert p.symbol == "SPXW260718P05000000"
    assert p.underlying == "SPX"
    assert p.signed_quantity == 2
    assert p.average_open_price == 1.20


def test_map_position_short_is_negative():
    p = map_position(_position(quantity=Decimal("1"), quantity_direction="Short"))
    assert p.signed_quantity == -1


def test_map_balances():
    bal = AccountBalance.model_construct(
        account_number="5WX00000", net_liquidating_value=Decimal("123456.78")
    )
    assert map_balances(bal).net_liquidating_value == 123456.78


async def test_broker_account_fetch_positions_maps_all():
    class _SdkAccount:
        account_number = "5WX00000"

        async def get_positions(self, session, **kwargs):
            return [
                _position(symbol="A", quantity=Decimal("1"), quantity_direction="Long"),
                _position(symbol="B", quantity=Decimal("2"), quantity_direction="Short"),
            ]

    broker = BrokerAccount(session=object(), account=_SdkAccount())
    positions = await broker.fetch_positions()
    by_symbol = {p.symbol: p.signed_quantity for p in positions}
    assert by_symbol == {"A": 1, "B": -2}
