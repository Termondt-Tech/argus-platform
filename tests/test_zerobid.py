"""The zero-bid floor and tick rounding — spec §7's pricing arithmetic.

There must be exactly ONE implementation of the floor, so this also pins the
signal layer's `OptionQuote.floored_mid` to it: if someone reintroduces a local
copy there, `test_option_quote_delegates_to_the_shared_floor` keeps passing but
`test_shared_floor_is_the_only_implementation` catches the duplicate.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from argus.execution.zerobid import (
    TICK,
    floor_bid,
    floored_mid,
    round_to_tick,
    spread_mid,
)
from argus.marketdata.models import SPX_MIN_TICK
from argus.signal.models import OptionQuote


# -- the floor ------------------------------------------------------------


def test_zero_bid_floors_to_one_tick():
    # The whole point: an unfloored 0-bid leg prices at ask/2, a mid nobody fills.
    assert floor_bid(0.0) == SPX_MIN_TICK
    assert floored_mid(0.0, 0.35) == pytest.approx((0.05 + 0.35) / 2)


def test_negative_bid_also_floors():
    # A negative bid is nonsense from a feed, but it must not produce a mid BELOW
    # the zero-bid case — fail toward the conservative price, not through it.
    assert floor_bid(-1.0) == SPX_MIN_TICK


def test_real_bid_is_left_alone():
    assert floor_bid(1.20) == 1.20
    assert floored_mid(1.20, 1.40) == pytest.approx(1.30)


def test_spread_mid_is_short_minus_long():
    # short 1.20/1.40 (mid 1.30) vs long 0.0/0.30 (floored mid 0.175) -> 1.125
    assert spread_mid(1.20, 1.40, 0.0, 0.30) == pytest.approx(1.125)


# -- tick rounding --------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (1.125, "1.15"),   # tie -> away from zero
        (1.124, "1.10"),
        (1.13, "1.15"),
        (0.80, "0.80"),
        (-1.125, "-1.15"),  # symmetric for debits
        (-1.124, "-1.10"),
    ],
)
def test_round_to_tick(price, expected):
    assert round_to_tick(price) == Decimal(expected)


def test_rounded_prices_are_exact_tick_multiples():
    # Float arithmetic would produce 1.2000000000000002 on the wire.
    for price in (1.1999, 0.3333, 7.7777):
        assert round_to_tick(price) % TICK == 0


def test_round_to_tick_refuses_non_finite():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            round_to_tick(bad)


# -- single implementation ------------------------------------------------


def test_option_quote_delegates_to_the_shared_floor():
    quote = OptionQuote("SPXW P", 5000.0, "put", -0.15, 0.0, 0.30)
    assert quote.floored_mid == floored_mid(0.0, 0.30)


def test_shared_floor_is_the_only_implementation():
    """No second copy of `bid if bid > 0 else tick` anywhere in the tree.

    A duplicated safety rule is how one copy quietly drifts from the other — and
    this one decides the price that actually gets routed.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "argus"
    offenders = [
        path.relative_to(src)
        for path in src.rglob("*.py")
        if path.name != "zerobid.py" and "SPX_MIN_TICK" in path.read_text()
    ]
    assert offenders == [Path("marketdata/models.py")], (
        f"SPX_MIN_TICK should only be defined in marketdata/models.py and consumed "
        f"by execution/zerobid.py; also found in {offenders}"
    )
