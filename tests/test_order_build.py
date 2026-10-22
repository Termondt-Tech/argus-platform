"""Order construction from an approved candidate (spec §7).

Covers the three rules `build.py` enforces structurally: one multi-leg order (never
per-leg), the limit price is the gate-approved floored mid (never a refetch), and a
candidate that aged past tolerance is refused rather than submitted.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from argus.execution.build import (
    StaleSignalError,
    _limit_price,
    build_order,
    candidate_id,
    candidate_key,
    external_identifier,
    parse_candidate_key,
)
from argus.execution.models import LegAction, OrderDirection, parse_external_identifier
from argus.signal.models import OptionQuote, SpreadSignal

TRADING_DATE = date(2026, 7, 28)
MAX_AGE = 1.0


def _signal(*, captured_at: float = 100.0, credit: float | None = None) -> SpreadSignal:
    """A put credit spread whose credit IS its legs' floored mid: 1.30 - 0.175."""
    short = OptionQuote("SPXW  P4975", 4975.0, "put", -0.15, 1.20, 1.40)
    long_ = OptionQuote("SPXW  P4970", 4970.0, "put", -0.10, 0.0, 0.30)  # zero bid
    return SpreadSignal(
        underlying="SPXW",
        side="put",
        short_leg=short,
        long_leg=long_,
        credit=short.floored_mid - long_.floored_mid if credit is None else credit,
        width=5.0,
        captured_at=captured_at,
    )


def _build(signal=None, **overrides):
    kwargs = {
        "direction": OrderDirection.OPEN,
        "contracts": 1,
        "trading_date": TRADING_DATE,
        "now": 100.0,
        "max_signal_age_s": MAX_AGE,
    }
    kwargs.update(overrides)
    return build_order(signal or _signal(), **kwargs)


# -- single-legging -------------------------------------------------------


def test_a_spread_is_one_order_with_two_legs():
    """Spec §7: getting filled on the short leg only, naked, is unacceptable."""
    order = _build()
    assert len(order.legs) == 2
    assert [leg.symbol for leg in order.legs] == ["SPXW  P4975", "SPXW  P4970"]
    assert all(leg.quantity == 1 for leg in order.legs)


def test_contracts_apply_to_both_legs():
    order = _build(contracts=3)
    assert [leg.quantity for leg in order.legs] == [3, 3]
    assert order.contracts == 3


# -- leg actions and price sign per direction -----------------------------


def test_open_sells_the_short_strike_for_a_credit():
    order = _build(direction=OrderDirection.OPEN)
    assert order.legs[0].action is LegAction.SELL_TO_OPEN
    assert order.legs[1].action is LegAction.BUY_TO_OPEN
    # Positive price == credit in tastytrade's convention.
    assert order.limit_price == Decimal("1.15")  # 1.125 -> nearest tick, away from zero


def test_close_buys_the_short_strike_back_for_a_debit():
    """Chunk 4's exit reuses this builder — same strikes, actions and sign flipped."""
    order = _build(direction=OrderDirection.CLOSE)
    assert order.legs[0].action is LegAction.BUY_TO_CLOSE
    assert order.legs[1].action is LegAction.SELL_TO_CLOSE
    assert order.limit_price == Decimal("-1.15")


# -- the price is the gated mid, not a fresh quote ------------------------


def test_limit_price_comes_from_the_signals_floored_mid():
    """The long leg has bid 0.0; an unfloored mid would price the spread at 1.30."""
    signal = _signal()
    order = _build(signal)
    assert signal.credit == pytest.approx(1.125)
    assert order.limit_price == Decimal("1.15")


def test_credit_that_disagrees_with_its_own_legs_is_refused():
    """Guards the one rule this module exists for: submit the price that was gated.

    A signal whose credit came from somewhere other than the zero-bid-floored mid
    of its legs would put an unevaluated number on the wire.
    """
    with pytest.raises(ValueError, match="does not match"):
        _build(_signal(credit=2.50))


def test_a_mid_that_rounds_to_zero_has_no_valid_limit():
    """A worthless spread cannot be expressed as a limit.

    Whether it is worth paying one tick to close it is exit policy (chunk 4), not
    something the builder should substitute in silently.
    """
    with pytest.raises(ValueError, match="no valid limit price"):
        _build(_worthless_signal())


def test_the_zero_mid_refusal_has_its_own_type_and_is_still_a_value_error():
    """(2026-09-02) Both halves matter and they pull in opposite directions.

    **Its own type**, so a caller can tell the routine refusal — the market saying this
    spread is worth approximately nothing, which near the close is what a WINNING
    position looks like — apart from this module's genuine defect signatures, WITHOUT
    matching the message text. That rule is already held for `Order.reject_reason`:
    prose changes wording without notice, and a branch on it fails open the day it does.

    **Still a `ValueError`**, so every existing `except (StaleSignalError, ValueError)`
    around `build_order` catches it unchanged and no call site had to widen. Break the
    subclassing and both the exit path and the entry path stop catching their most
    common refusal — the entry one would fall through to `entry_build_failed` and end
    the day's entries.
    """
    from argus.execution.build import UnpriceableSpreadError

    assert issubclass(UnpriceableSpreadError, ValueError)
    with pytest.raises(UnpriceableSpreadError) as exc:
        _build(_worthless_signal())
    assert exc.value.magnitude == Decimal("0")
    assert "no valid limit price" in str(exc.value)

    # And the module's OTHER refusals stay plain `ValueError`s — the distinction is
    # worthless if everything raises the specific type.
    with pytest.raises(ValueError, match="does not match") as other:
        _build(_signal(credit=2.50))
    assert not isinstance(other.value, UnpriceableSpreadError)


# -- freshness ------------------------------------------------------------


def test_a_fresh_signal_builds():
    _build(now=100.5)  # 0.5s old, inside the 1.0s tolerance


def test_a_stale_signal_is_refused():
    """A stall between gate-pass and construction means the gated price is gone."""
    with pytest.raises(StaleSignalError) as exc:
        _build(now=104.0)
    assert exc.value.age_s == pytest.approx(4.0)
    assert exc.value.max_age_s == MAX_AGE


def test_a_signal_from_the_future_is_refused():
    """Fail closed on a negative age — same rule the pre-trade gate uses."""
    with pytest.raises(StaleSignalError):
        _build(now=99.0)


def test_freshness_check_does_no_io():
    """A refetch here would add a round trip AND change the trade being submitted.

    Enforced by construction: `build_order` is a plain function over values, so
    this test simply asserts it works with nothing but the numbers in hand.
    """
    order = build_order(
        _signal(captured_at=42.0),
        direction=OrderDirection.OPEN,
        contracts=1,
        trading_date=TRADING_DATE,
        now=42.25,
        max_signal_age_s=MAX_AGE,
    )
    assert order.limit_price == Decimal("1.15")


# -- identity -------------------------------------------------------------


def test_external_identifier_is_deterministic_per_trade_idea():
    """Same candidate rebuilt after a crash -> same key, so `get_live_orders()`
    can answer "did I already send this?" from the broker."""
    first = _build()
    second = _build(_signal(captured_at=100.0))
    assert first.external_identifier == second.external_identifier
    assert first.external_identifier == "argus-open-20260728-put-4975-4970"


def test_open_and_close_get_different_identifiers():
    assert (
        _build(direction=OrderDirection.OPEN).external_identifier
        != _build(direction=OrderDirection.CLOSE).external_identifier
    )


def test_candidate_id_matches_the_shadow_dedup_key():
    """Same (date, side, strikes) tuple `shadow.outcome.trade_ideas` groups on, so
    a live order joins straight to its shadow-mode history."""
    signal = _signal()
    assert candidate_id(signal, TRADING_DATE) == "20260728-put-4975-4970"
    assert _build(signal).candidate_id == candidate_id(signal, TRADING_DATE)


def test_external_identifier_composition():
    assert (
        external_identifier("20260728-put-4975-4970", OrderDirection.CLOSE)
        == "argus-close-20260728-put-4975-4970"
    )


# -- input validation -----------------------------------------------------


@pytest.mark.parametrize("contracts", [0, -1])
def test_non_positive_contracts_refused(contracts):
    with pytest.raises(ValueError, match="contracts"):
        _build(contracts=contracts)


def test_unsent_order_has_no_broker_state():
    order = _build()
    assert order.broker_order_id is None
    assert order.status is None
    assert order.replaces_order_id is None
    assert all(leg.remaining_quantity is None for leg in order.legs)


def _over_width_signal() -> SpreadSignal:
    """Legs that genuinely price ABOVE their own 5.00 strike distance: 5.30 - 0.25.

    The 2026-09-03 shape — a deep-ITM spread whose mid landed above intrinsic on a
    wide print. Arithmetically impossible for the instrument, so it is always a bad
    number rather than an aggressive one.
    """
    short = OptionQuote("SPXW  P4975", 4975.0, "put", -0.95, 5.20, 5.40)
    long_ = OptionQuote("SPXW  P4970", 4970.0, "put", -0.90, 0.20, 0.30)
    return SpreadSignal(
        underlying="SPXW",
        side="put",
        short_leg=short,
        long_leg=long_,
        credit=short.floored_mid - long_.floored_mid,
        width=5.0,
        captured_at=100.0,
    )


# -- the width cap (2026-09-17) -------------------------------------------


def test_a_close_is_never_priced_above_the_spread_width():
    """THE PRODUCTION DEFECT, end to end through `build_order`.

    On 2026-09-03 `argus-close-20260903-call-7725-7730` (width 5.00) was marked at
    `cost_to_close = 5.05`, the order went out at -5.05 and filled, realizing -870.00
    against an entry credit of 0.70. Holding to settlement pays the width: -860.00. The
    flatten paid $870 to escape an $860 loss, and nothing compared the mark to the
    width — `_limit_price` refuses only a non-positive price.
    """
    signal = _over_width_signal()
    assert signal.credit == pytest.approx(5.05)  # the bad mark, faithfully
    order = _build(signal, direction=OrderDirection.CLOSE)
    assert order.limit_price == Decimal("-5.0"), "a close priced past its own width"
    # The whole point, in money: capped, the exit is exactly as good as holding to
    # expiry instead of $10 worse.
    assert (Decimal("0.70") - abs(order.limit_price)) * 200 == Decimal("-860.00")


def test_the_cap_binds_at_the_width_and_not_a_tick_early():
    """A spread marked at exactly its width is priceable AT the width — the cap is a
    ceiling, not a haircut. Getting this wrong would leave every deep-ITM flatten one
    tick short of the only price that closes it."""
    short = OptionQuote("SPXW  P4975", 4975.0, "put", -0.95, 5.20, 5.30)  # mid 5.25
    long_ = OptionQuote("SPXW  P4970", 4970.0, "put", -0.90, 0.20, 0.30)  # mid 0.25
    at_width = SpreadSignal(
        underlying="SPXW",
        side="put",
        short_leg=short,
        long_leg=long_,
        credit=short.floored_mid - long_.floored_mid,
        width=5.0,
        captured_at=100.0,
    )
    assert at_width.credit == pytest.approx(5.00)
    order = _build(at_width, direction=OrderDirection.CLOSE)
    assert order.limit_price == Decimal("-5.00")


@pytest.mark.parametrize(
    "credit, expected",
    [(5.05, "-5.0"), (5.50, "-5.0"), (12.00, "-5.0"), (5.00, "-5.00"), (4.95, "-4.95")],
)
def test_limit_price_caps_a_debit_at_the_width(credit: float, expected: str):
    assert _limit_price(credit, OrderDirection.CLOSE, width=5.0) == Decimal(expected)


def test_the_cap_rounds_the_width_DOWN_to_the_tick_never_up():
    """`round_to_tick` is ROUND_HALF_UP, which on an off-grid width would round the
    ceiling UP and put the bound back above the thing it bounds. Every SPX strike
    distance is already a multiple of 0.05 so this is a no-op today; it is written this
    way so a ladder with odd spacing cannot silently reopen the hole."""
    assert _limit_price(9.99, OrderDirection.CLOSE, width=5.03) == Decimal("-5.00")
    assert _limit_price(9.99, OrderDirection.CLOSE, width=5.07) == Decimal("-5.05")


def test_the_cap_is_direction_free_and_inert_for_entries_in_practice():
    """A credit above the width is free money, so `strategy.py` refuses it as
    `CREDIT_EXCEEDS_WIDTH` before an order is built — the clamp is unreachable on the
    entry path. It stays unconditional anyway: no direction branch, and a caller that
    bypasses the strategy (`tools.order_smoke` sets `min_credit = 0.0`) is bounded too.
    """
    assert _limit_price(5.05, OrderDirection.OPEN, width=5.0) == Decimal("5.0")
    # And an ordinary entry credit is untouched.
    assert _limit_price(0.85, OrderDirection.OPEN, width=5.0) == Decimal("0.85")


@pytest.mark.parametrize("width", [0.0, -5.0, float("nan"), float("inf")])
def test_an_unusable_width_is_a_bug_not_a_market_condition(width: float):
    """`SpreadSignal.width` is the ACTUAL strike distance and every construction path
    computes it from two real strikes, so an unusable one means something upstream is
    broken. Raise rather than skip the cap — skipping would fail OPEN, which is the
    defect."""
    with pytest.raises(ValueError, match="width is not a usable bound"):
        _limit_price(1.50, OrderDirection.CLOSE, width=width)


def test_width_is_required_and_keyword_only():
    """It is the spread's own strike distance, not a quote, so it costs the caller
    nothing and must not be omittable by one that happens to lack market data."""
    with pytest.raises(TypeError):
        _limit_price(1.50, OrderDirection.CLOSE)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        _limit_price(1.50, OrderDirection.CLOSE, 5.0)  # type: ignore[misc]


def _worthless_signal() -> SpreadSignal:
    """Legs that genuinely price to a zero net mid — both at the minimum tick."""
    short = OptionQuote("SPXW  P4975", 4975.0, "put", -0.15, 0.05, 0.05)
    long_ = OptionQuote("SPXW  P4970", 4970.0, "put", -0.10, 0.05, 0.05)
    return SpreadSignal(
        underlying="SPXW",
        side="put",
        short_leg=short,
        long_leg=long_,
        credit=short.floored_mid - long_.floored_mid,
        width=5.0,
        captured_at=100.0,
    )


# -- reading the identity back out (2026-08-13) -------------------------------
#
# Post-close scoring reads `candidate_id` and `external_identifier` back out of
# the audit log to find out which spread a fill belongs to — `candidate_id` does
# not round-trip through the broker, but the identifier does. The parsers live
# beside the constructors so a format change is a change to a neighbour; these
# tests are what make that a checked fact rather than a hope.


@pytest.mark.parametrize(
    ("side", "short_strike", "long_strike"),
    [
        ("put", 4975.0, 4970.0),
        ("call", 7825.0, 7830.0),
        ("call", 7825.5, 7830.5),   # half-point strikes exist on the SPXW ladder
        ("put", 100.0, 95.0),
    ],
)
def test_candidate_key_round_trips(side, short_strike, long_strike):
    key = candidate_key(TRADING_DATE, side, short_strike, long_strike)
    assert parse_candidate_key(key) == (TRADING_DATE, side, short_strike, long_strike)


def test_candidate_id_and_candidate_key_are_the_same_string():
    """One format, two entry points. `candidate_id` is the order path's constructor
    and `candidate_key` is what a caller holding only the primitives calls; a
    difference between them would silently stop fills joining to trade ideas."""
    signal = _signal()
    assert candidate_id(signal, TRADING_DATE) == candidate_key(
        TRADING_DATE, signal.side, signal.short_leg.strike, signal.long_leg.strike
    )


@pytest.mark.parametrize("direction", list(OrderDirection))
def test_external_identifier_round_trips(direction):
    candidate = candidate_id(_signal(), TRADING_DATE)
    identifier = external_identifier(candidate, direction)
    assert parse_external_identifier(identifier) == (direction, candidate)


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "hand-placed-42",              # somebody else's order
        "argus-",                      # prefix with nothing after it
        "argus-sideways-2026-put-1-2", # a direction that is not one
        "argus-open-",                 # no candidate
    ],
)
def test_an_unreadable_identifier_says_so_rather_than_guessing(garbage):
    assert parse_external_identifier(garbage) is None


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "20260728-put-4975",            # three fields, not four
        "20260728-put-4975-4970-extra",
        "notadate-put-4975-4970",
        "20260728-straddle-4975-4970",  # not a side this app trades
        "20260728-put-nan-4970",        # parses as a float; is not a strike
        "20260728-put-inf-4970",
    ],
)
def test_an_unreadable_candidate_key_says_so_rather_than_guessing(garbage):
    assert parse_candidate_key(garbage) is None
