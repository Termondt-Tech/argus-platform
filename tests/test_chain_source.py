"""DXLinkChainSource: contract mapping, 0DTE selection, strike window, join."""

from __future__ import annotations

import math
from datetime import date, time
from decimal import Decimal

import pytest

from tastytrade.instruments import Option, OptionType

from argus.config import SignalConfig
from argus.marketdata import FakeMarketData
from argus.signal.contracts import (
    OptionContract,
    filter_strike_window,
    map_option,
    select_expiration,
)
from argus.signal.live_chain import DXLinkChainSource
from argus.signal.strategy import CreditSpreadStrategy

TODAY = date(2026, 7, 18)
TOMORROW = date(2026, 7, 19)


def _sdk_option(strike, option_type, *, expiration=TODAY, streamer="."):
    return Option.model_construct(
        symbol=f"SPXW  260718{option_type.value}{int(strike):08d}",
        streamer_symbol=f"{streamer}SPXW260718{option_type.value}{strike:g}",
        strike_price=Decimal(str(strike)),
        option_type=option_type,
        expiration_date=expiration,
        underlying_symbol="SPX",
    )


# -- pure mapping / selection --------------------------------------------


def test_map_option_put_and_call():
    put = map_option(_sdk_option(4975, OptionType.PUT))
    call = map_option(_sdk_option(5025, OptionType.CALL))
    assert put is not None and put.right == "put" and put.strike == 4975.0
    assert call is not None and call.right == "call" and call.strike == 5025.0
    assert put.symbol != put.streamer_symbol  # OCC vs dxfeed are distinct


def test_map_option_drops_contracts_without_streamer_symbol():
    opt = _sdk_option(4975, OptionType.PUT)
    opt = Option.model_construct(**{**opt.model_dump(), "streamer_symbol": None})
    assert map_option(opt) is None


def test_select_expiration_picks_only_target_date():
    chain = {TODAY: [_sdk_option(4975, OptionType.PUT)],
             TOMORROW: [_sdk_option(4975, OptionType.PUT, expiration=TOMORROW)]}
    assert len(select_expiration(chain, TODAY)) == 1
    assert select_expiration(chain, date(2026, 1, 1)) == []  # no expiry that day


def test_filter_strike_window():
    contracts = [
        OptionContract("A", "sA", 4800.0, "put", TODAY),
        OptionContract("B", "sB", 4950.0, "put", TODAY),
        OptionContract("C", "sC", 5050.0, "call", TODAY),
        OptionContract("D", "sD", 5300.0, "call", TODAY),
    ]
    kept = {c.symbol for c in filter_strike_window(contracts, spot=5000.0, window_points=100.0)}
    assert kept == {"B", "C"}  # 4800 and 5300 are outside +/-100


# -- live source ----------------------------------------------------------


#: The QUOTE bound (`staleness.market_data_max_age_s`). Named so the boundary
#: tests below read as "at the bound / past the bound" rather than as bare floats.
MAX_AGE = 2.0
#: The GREEKS bound (`staleness.greeks_max_age_s`), and deliberately a DIFFERENT
#: number (2026-08-27). Two bounds that happen to be equal in the fixture cannot
#: fail when the code reads one where it meant the other, which is precisely the
#: regression these tests have to be able to catch.
GREEKS_MAX_AGE = 8.0


def _source(market, **kw):
    kw.setdefault("max_age_s", MAX_AGE)
    kw.setdefault("greeks_max_age_s", GREEKS_MAX_AGE)
    return DXLinkChainSource(
        market,
        underlying_symbol="SPX",
        chain_symbol="SPXW",
        strike_window_points=100.0,
        clock=lambda: 0.0,
        **kw,
    )


async def _loaded(market, strikes=(4975, 4970)):
    """A source with `strikes` subscribed and no quotes pushed yet."""

    async def fetcher(session, symbol):
        return {TODAY: [_sdk_option(s, OptionType.PUT) for s in strikes]}

    src = _source(market, chain_fetcher=fetcher)
    await src.load(object(), today=TODAY)
    return src


def _push_fresh(market, contract, *, bid=1.50, ask=1.60, delta=-0.15):
    market.push(contract.streamer_symbol, bid, ask)
    market.push_greeks(contract.streamer_symbol, delta=delta)


def _strategy_cfg(**over) -> SignalConfig:
    """The production-shaped signal config, for the end-to-end claims below.

    Several claims here span the source AND the strategy — the source drops a
    delta, the strategy decides what that costs — and neither module states them
    alone, so they are asserted through the real `CreditSpreadStrategy`.
    """
    base = dict(
        sides=("put",),
        target_short_delta=0.15,
        width=5.0,
        max_width=10.0,
        min_credit=0.75,
        entry_earliest=time(10, 0),
        entry_latest=time(15, 0),
        cadence_s=5.0,
        short_delta_source="feed",
    )
    base.update(over)
    return SignalConfig(**base)


def test_spot_none_until_underlying_quotes():
    market = FakeMarketData()
    src = _source(market)
    assert src.spot() is None
    market.push("SPX", 4999.0, 5001.0)
    assert src.spot() == 5000.0


async def test_load_requires_underlying_quote():
    src = _source(FakeMarketData())
    with pytest.raises(RuntimeError, match="no underlying quote"):
        await src.load(object(), today=TODAY)


async def test_load_filters_to_window_and_subscribes():
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)  # spot 5000

    async def fetcher(session, symbol):
        return {
            TODAY: [
                _sdk_option(4950, OptionType.PUT),   # in window
                _sdk_option(5050, OptionType.CALL),  # in window
                _sdk_option(4000, OptionType.PUT),   # outside +/-100
            ],
            TOMORROW: [_sdk_option(4975, OptionType.PUT, expiration=TOMORROW)],  # not 0DTE
        }

    src = _source(market, chain_fetcher=fetcher)
    n = await src.load(object(), today=TODAY)
    assert n == 2
    strikes = {c.strike for c in src.contracts}
    assert strikes == {4950.0, 5050.0}
    # Those streamer symbols are now subscribed for quotes+greeks.
    assert {c.streamer_symbol for c in src.contracts} <= market.subscribed


async def test_load_handles_no_expiry_today():
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)

    async def fetcher(session, symbol):
        return {TOMORROW: [_sdk_option(4975, OptionType.PUT, expiration=TOMORROW)]}

    src = _source(market, chain_fetcher=fetcher)
    assert await src.load(object(), today=TODAY) == 0
    assert src.snapshot() is None


async def test_snapshot_joins_quotes_and_greeks():
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)

    async def fetcher(session, symbol):
        return {TODAY: [_sdk_option(4975, OptionType.PUT), _sdk_option(4970, OptionType.PUT)]}

    src = _source(market, chain_fetcher=fetcher)
    await src.load(object(), today=TODAY)
    for c in src.contracts:
        market.push(c.streamer_symbol, 1.50, 1.60)
        market.push_greeks(c.streamer_symbol, delta=-0.15)

    snap = src.snapshot()
    assert snap is not None
    assert snap.underlying_price == 5000.0
    assert len(snap.options) == 2
    assert all(o.delta == -0.15 for o in snap.options)
    assert {o.strike for o in snap.options} == {4975.0, 4970.0}


async def test_snapshot_admits_a_delta_less_contract_without_inventing_a_delta():
    # A contract with a market but no greeks is a valid LONG leg and nothing else.
    # It is admitted with `delta=None` — never defaulted to 0.0, which is what
    # `LegQuotes` does on the exit path (where nothing selects on delta) and which
    # here would read as a 0-delta strike and be selected against the target.
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)

    async def fetcher(session, symbol):
        return {TODAY: [_sdk_option(4975, OptionType.PUT), _sdk_option(4970, OptionType.PUT)]}

    src = _source(market, chain_fetcher=fetcher)
    await src.load(object(), today=TODAY)
    first, second = src.contracts
    market.push(first.streamer_symbol, 1.50, 1.60)
    market.push_greeks(first.streamer_symbol, delta=-0.15)
    market.push(second.streamer_symbol, 0.70, 0.80)  # quote but NO greeks

    snap = src.snapshot()
    assert snap is not None
    assert len(snap.options) == 2
    by_symbol = {o.symbol: o for o in snap.options}
    assert by_symbol[first.symbol].delta == -0.15 and by_symbol[first.symbol].has_delta
    assert by_symbol[second.symbol].delta is None
    assert not by_symbol[second.symbol].has_delta
    assert by_symbol[second.symbol].tradeable  # priceable, just not selectable

    cov = src.coverage()
    assert (cov.admissible, cov.short_admissible) == (2, 1)


async def test_snapshot_skips_a_contract_with_no_quote_at_all():
    # The quote is what admission is FOR. Greeks without a market buy nothing:
    # there is no price to build a leg from, in either role.
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market)
    quoted, unquoted = src.contracts
    _push_fresh(market, quoted)
    market.push_greeks(unquoted.streamer_symbol, delta=-0.10)  # greeks but NO quote

    snap = src.snapshot()
    assert snap is not None
    assert [o.symbol for o in snap.options] == [quoted.symbol]
    cov = src.coverage()
    assert (cov.admissible, cov.short_admissible) == (1, 1)


async def test_coverage_distinguishes_missing_quotes_from_missing_greeks():
    # The diagnostic that matters: quotes flowing but no greeks (an options
    # entitlement gap) must look different from no data at all.
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)

    async def fetcher(session, symbol):
        return {TODAY: [_sdk_option(s, OptionType.PUT) for s in (4975, 4970, 4965)]}

    src = _source(market, chain_fetcher=fetcher)
    await src.load(object(), today=TODAY)

    cov = src.coverage()
    assert (cov.total, cov.with_quote, cov.with_greeks, cov.with_both) == (3, 0, 0, 0)

    for c in src.contracts:                # quotes arrive, greeks do not
        market.push(c.streamer_symbol, 1.50, 1.60)
    cov = src.coverage()
    assert (cov.total, cov.with_quote, cov.with_greeks, cov.with_both) == (3, 3, 0, 0)

    market.push_greeks(src.contracts[0].streamer_symbol, delta=-0.15)
    cov = src.coverage()
    assert (cov.with_quote, cov.with_greeks, cov.with_both) == (3, 1, 1)


# -- per-leg freshness gate (2026-08-21) ----------------------------------
#
# Until this gate existed the only admission test was `is None`, so a leg that
# quoted once and went quiet was joined into every later snapshot at any age, and
# the entry was priced, sized and submitted off it. Nothing downstream caught it:
# the pre-trade gate bounds the UNDERLYING only, and `execution.signal_max_age_s`
# measures snapshot-to-build latency, not quote age.


async def test_snapshot_rejects_leg_with_stale_quote():
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market)
    fresh_leg, stale_leg = src.contracts

    _push_fresh(market, stale_leg)
    market.advance(MAX_AGE + 1.0)   # the stale leg's last print recedes ...
    _push_fresh(market, fresh_leg)  # ... while this one prints right now

    snap = src.snapshot()
    assert snap is not None
    assert [o.symbol for o in snap.options] == [fresh_leg.symbol]


async def test_fresh_quote_with_stale_greeks_is_admitted_without_its_delta():
    """The role split (2026-08-24): stale greeks cost a leg its DELTA, not its
    place in the snapshot.

    Entry still differs from exit on the short leg: `LegQuotes` defaults a missing
    delta to 0.0 because the watchdog never SELECTS on delta, while
    `strategy._select` picks the short strike BY delta magnitude — so a stale delta
    there silently picks a different strike. The long leg is picked by strike
    distance and has no such exposure, which is the whole asymmetry.
    """
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market)
    good, lagging = src.contracts

    market.push_greeks(lagging.streamer_symbol, delta=-0.15)
    market.advance(GREEKS_MAX_AGE + 1.0)              # past the GREEKS bound
    market.push(lagging.streamer_symbol, 0.70, 0.80)  # quote fresh, greeks are not
    _push_fresh(market, good)

    snap = src.snapshot()
    assert snap is not None
    by_symbol = {o.symbol: o for o in snap.options}
    assert set(by_symbol) == {good.symbol, lagging.symbol}
    # The stale delta is dropped, never carried at its last-known value — that
    # would be the pre-2026-08-21 defect back in a narrower place.
    assert by_symbol[lagging.symbol].delta is None
    assert by_symbol[good.symbol].delta == -0.15

    cov = src.coverage()
    assert (cov.admissible, cov.short_admissible) == (2, 1)


async def test_snapshot_admits_fully_fresh_leg():
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market, strikes=(4975,))
    (leg,) = src.contracts
    _push_fresh(market, leg, bid=1.50, ask=1.60, delta=-0.15)

    snap = src.snapshot()
    assert snap is not None
    assert len(snap.options) == 1
    assert snap.options[0].delta == -0.15


# The bound is INCLUSIVE (`argus.staleness.is_stale` accepts `age == max_age_s`),
# so it is bracketed rather than asserted at one point: an off-by-one in either
# direction here silently changes how much of the chain is tradeable.
#
# Each feed is bracketed against ITS OWN bound — they are different numbers since
# 2026-08-27 — and what breaching one costs still differs: a stale quote costs the
# leg, stale greeks cost only its short-strike candidacy.
@pytest.mark.parametrize(
    "offset, fresh", [(-0.1, True), (0.0, True), (0.1, False)]
)
@pytest.mark.parametrize("feed", ["quote", "greeks"])
async def test_freshness_bound_is_inclusive_on_both_feeds(offset, fresh, feed):
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market, strikes=(4975,))
    (leg,) = src.contracts
    age = (MAX_AGE if feed == "quote" else GREEKS_MAX_AGE) + offset

    # Age ONE feed by `age` and leave the other at zero, so each parametrisation
    # proves that feed's own bound rather than the pair's.
    if feed == "quote":
        market.push(leg.streamer_symbol, 1.50, 1.60)
        market.advance(age)
        market.push_greeks(leg.streamer_symbol, delta=-0.15)
    else:
        market.push_greeks(leg.streamer_symbol, delta=-0.15)
        market.advance(age)
        market.push(leg.streamer_symbol, 1.50, 1.60)

    # A breached QUOTE bound drops the leg; a breached GREEKS bound drops only the
    # delta. Both counts are asserted on every parametrisation so neither bound can
    # be loosened into the other's role without a failure here.
    admitted = fresh or feed == "greeks"
    snap = src.snapshot()
    assert (snap is not None) is admitted
    if snap is not None:
        assert (snap.options[0].delta is not None) is fresh
    cov = src.coverage()
    assert cov.admissible == int(admitted)
    assert cov.short_admissible == int(fresh)


async def test_greeks_older_than_the_quote_bound_still_selects_a_short_strike():
    """THE 2026-08-27 change, stated as behaviour.

    A delta past `market_data_max_age_s` but inside `greeks_max_age_s` is
    selectable. Under the old code — one bound for both feeds — this leg reached
    the strategy with `delta=None` and every tick like it reported
    `no_short_leg_with_fresh_delta`, which was 97% of them.
    """
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market, strikes=(4975,))
    (leg,) = src.contracts

    market.push_greeks(leg.streamer_symbol, delta=-0.15)
    market.advance(MAX_AGE * 2)          # well past the QUOTE bound...
    assert MAX_AGE * 2 < GREEKS_MAX_AGE  # ...and still inside the GREEKS bound
    market.push(leg.streamer_symbol, 1.50, 1.60)

    snap = src.snapshot()
    assert snap is not None
    assert snap.options[0].delta == -0.15
    assert src.coverage().short_admissible == 1


async def test_the_greeks_bound_still_binds_and_is_not_merely_absent():
    """The other half: widening is not removing.

    A delta past `greeks_max_age_s` is still refused its short role, and the leg is
    still admitted for its price. Without this, "the bound was raised" and "the
    bound was deleted" pass identically.
    """
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market, strikes=(4975,))
    (leg,) = src.contracts

    market.push_greeks(leg.streamer_symbol, delta=-0.15)
    market.advance(GREEKS_MAX_AGE + 0.1)
    market.push(leg.streamer_symbol, 1.50, 1.60)

    snap = src.snapshot()
    assert snap is not None
    assert snap.options[0].delta is None       # not short-selectable
    assert snap.options[0].tradeable           # still priceable as a long leg
    cov = src.coverage()
    assert (cov.admissible, cov.short_admissible) == (1, 0)


async def test_greeks_age_travels_with_the_leg_even_when_it_is_refused():
    """The age survives the verdict.

    `delta is None` records THAT the feed was behind and destroys BY HOW MUCH, and
    the distance is the dataset the next bound is sized from. It has to reach the
    strategy on the leg, because `evaluate()` is pure and cannot ask the port.
    """
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market, strikes=(4975,))
    (leg,) = src.contracts

    market.push_greeks(leg.streamer_symbol, delta=-0.15)
    market.advance(GREEKS_MAX_AGE + 5.0)
    market.push(leg.streamer_symbol, 1.50, 1.60)

    snap = src.snapshot()
    assert snap is not None
    quote = snap.options[0]
    assert quote.delta is None
    assert quote.greeks_age_s == pytest.approx(GREEKS_MAX_AGE + 5.0)
    assert src.coverage().min_greeks_age_s == pytest.approx(GREEKS_MAX_AGE + 5.0)


async def test_min_greeks_age_is_infinite_when_no_leg_has_ever_had_greeks():
    """Never-seen is not the same condition as late, and must not read as 0.

    A chain with no greeks at all is an entitlement/feed problem; a chain whose
    greeks are behind is a cadence problem. Collapsing them is what a defaulted
    zero would do to the dataset.
    """
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market, strikes=(4975,))
    (leg,) = src.contracts
    market.push(leg.streamer_symbol, 1.50, 1.60)

    cov = src.coverage()
    assert cov.with_greeks == 0
    assert cov.min_greeks_age_s == math.inf
    snap = src.snapshot()
    assert snap is not None
    assert snap.options[0].greeks_age_s == math.inf


async def test_gate_keys_on_age_not_on_a_thin_deep_otm_market():
    """A sparsely-quoting deep-OTM wing is admitted while it is CURRENT.

    Deep-OTM 0DTE wings quote 0.00 x 0.05 and update rarely — 14% of exit-path
    ticks went stale against this same bound on 2026-08-13. That is the case most
    at risk of being starved by a freshness gate, so it is pinned: a thin market is
    not a stale one. The zero bid is handled by the zero-bid floor, and `tradeable`
    only requires a real offer. The SAME leg is then aged to show the rejection is
    the clock's doing and nothing about the price.
    """
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market, strikes=(4975,))
    (wing,) = src.contracts

    _push_fresh(market, wing, bid=0.0, ask=0.05, delta=-0.02)
    snap = src.snapshot()
    assert snap is not None and len(snap.options) == 1
    assert snap.options[0].bid == 0.0 and snap.options[0].tradeable

    market.advance(MAX_AGE + 0.1)  # same market, only older
    assert src.snapshot() is None


async def test_snapshot_none_when_the_whole_chain_is_stale():
    # Fails closed to an ordinary `signal.skip` (SignalLoop's "no_chain"), never to
    # a snapshot built from prices the market has moved past.
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market)
    for c in src.contracts:
        _push_fresh(market, c)
    assert src.snapshot() is not None

    market.advance(MAX_AGE + 0.1)
    assert src.snapshot() is None


async def test_coverage_separates_present_from_admissible():
    # `with_both` counts presence, `admissible` counts priceability and
    # `short_admissible` counts selectability; the gaps are the gate's cost per
    # role, and each must be attributable to a feed.
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market, strikes=(4975, 4970, 4965))
    stale_q, stale_g, fresh = src.contracts

    market.push(stale_q.streamer_symbol, 1.50, 1.60)
    market.push_greeks(stale_g.streamer_symbol, delta=-0.15)
    # Past BOTH bounds, so each leg's surviving reading breaches its own.
    market.advance(GREEKS_MAX_AGE + 0.1)
    market.push_greeks(stale_q.streamer_symbol, delta=-0.15)  # quote is the old one
    market.push(stale_g.streamer_symbol, 0.70, 0.80)          # greeks are the old ones
    _push_fresh(market, fresh)

    cov = src.coverage()
    assert (cov.total, cov.with_quote, cov.with_greeks, cov.with_both) == (3, 3, 3, 3)
    # The stale-quote leg is gone; the stale-greeks leg survives as a long-leg
    # candidate. Only the fully fresh one can be a short strike.
    assert (cov.admissible, cov.short_admissible) == (2, 1)
    assert (cov.stale_quote, cov.stale_greeks) == (1, 1)


async def test_admissible_equals_what_snapshot_emits():
    """`coverage()` and `snapshot()` cannot disagree — on EITHER count.

    coverage() is the operator's read on the gate; if it could disagree with the
    snapshot the strategy gets, it would be worse than no instrument at all. The
    role split doubled the ways that could happen, so both counts are pinned
    against the snapshot rather than only the one that existed before it.
    """
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market, strikes=(4975, 4970, 4965))
    a, b, c = src.contracts

    _push_fresh(market, a)                      # everything about `a` goes stale
    market.advance(MAX_AGE + 0.1)
    _push_fresh(market, b)                      # fully fresh
    market.push(c.streamer_symbol, 0.70, 0.80)  # fresh quote, greeks never arrived

    snap = src.snapshot()
    assert snap is not None
    cov = src.coverage()
    assert len(snap.options) == cov.admissible == 2
    assert sum(o.has_delta for o in snap.options) == cov.short_admissible == 1
    assert {o.symbol for o in snap.options} == {b.symbol, c.symbol}
    assert cov.short_admissible <= cov.admissible  # the subset relation, stated


async def test_snapshot_none_only_when_no_leg_is_priceable():
    # The stale-greeks case must NOT collapse the chain: that mistake cost 95.7% of
    # loop iterations to `signal.skip` on 2026-08-24, on a chain whose every quote
    # was current. `None` now means "no market anywhere", nothing weaker.
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market)
    for c in src.contracts:
        market.push_greeks(c.streamer_symbol, delta=-0.15)
    market.advance(GREEKS_MAX_AGE + 0.1)
    for c in src.contracts:                     # quotes current, every greek stale
        market.push(c.streamer_symbol, 1.50, 1.60)

    snap = src.snapshot()
    assert snap is not None
    assert len(snap.options) == 2 and all(o.delta is None for o in snap.options)
    cov = src.coverage()
    assert (cov.admissible, cov.short_admissible, cov.stale_greeks) == (2, 0, 2)


async def test_stale_target_strike_is_routed_around_not_blocked():
    """A stale leg costs that STRIKE, not the tick — through the real strategy.

    The gate drops legs from the snapshot rather than refusing the candidate, and
    `strategy._select` takes the nearest-delta leg among those admitted. So the
    consequence of a stale target strike is the next-nearest one, not a dead
    session. Asserted end-to-end because the claim spans two modules: neither the
    source nor the strategy states it alone.
    """
    strategy = CreditSpreadStrategy(_strategy_cfg())
    deltas = {4980: -0.18, 4975: -0.15, 4970: -0.10}  # 4975 is the target
    prices = {4980: (1.80, 1.90), 4975: (1.50, 1.60), 4970: (0.70, 0.80)}

    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _loaded(market, strikes=tuple(deltas))
    by_strike = {c.strike: c for c in src.contracts}

    def push(strikes):
        for strike in strikes:
            bid, ask = prices[strike]
            _push_fresh(market, by_strike[strike], bid=bid, ask=ask, delta=deltas[strike])

    push(deltas)
    snap = src.snapshot()
    assert snap is not None
    result = strategy.evaluate(snap, time(12, 0))
    assert result.signal is not None
    assert result.signal.short_leg.strike == 4975.0  # the target, while it is fresh

    # Now let 4975 alone go quiet; the other two keep printing.
    market.advance(MAX_AGE + 0.1)
    push([4980, 4970])
    snap = src.snapshot()
    assert snap is not None
    assert 4975.0 not in {o.strike for o in snap.options}

    result = strategy.evaluate(snap, time(12, 0))
    assert result.signal is not None
    # Next-nearest admitted delta, and a long leg re-derived from what remains —
    # a real spread off live prices, not the stale one and not nothing.
    assert result.signal.short_leg.strike == 4980.0
    assert result.signal.long_leg.strike == 4970.0


# -- role-scoped admission (2026-08-24) -----------------------------------
#
# The 2026-08-21 gate demanded fresh quote AND fresh greeks of EVERY leg. Right
# for the short strike (chosen BY delta), wrong for the long one (chosen by strike
# distance, contributing no delta to anything) — and the greeks feed runs behind
# the quote feed, so it was the long leg's requirement that starved the day.


async def _ladder(market, deltas, prices, *, stale_greeks=()):
    """A loaded source where every QUOTE is current and `stale_greeks` are not.

    That is the shape the live feed held all day on 2026-08-24: quotes inside the
    quote bound, greeks outside the greeks bound.
    """
    src = await _loaded(market, strikes=tuple(deltas))
    by_strike = {c.strike: c for c in src.contracts}
    for strike in deltas:
        market.push_greeks(by_strike[strike].streamer_symbol, delta=deltas[strike])
    market.advance(GREEKS_MAX_AGE + 0.1)
    for strike in deltas:
        bid, ask = prices[strike]
        market.push(by_strike[strike].streamer_symbol, bid, ask)
        if strike not in stale_greeks:
            market.push_greeks(by_strike[strike].streamer_symbol, delta=deltas[strike])
    return src


_DELTAS = {4980: -0.18, 4975: -0.15, 4970: -0.10, 4965: -0.07}
_PRICES = {4980: (1.80, 1.90), 4975: (1.50, 1.60), 4970: (0.70, 0.80), 4965: (0.40, 0.50)}


async def test_a_stale_delta_costs_a_leg_its_short_role_and_nothing_else():
    """The measured 2026-08-24 defect, end to end: a hole in the ladder.

    4970's greeks lag; its market is current. Under the pre-split gate 4970 was
    invisible, so the long-leg search jumped to the next VISIBLE strike and built
    a 10-wide spread where 5 was available — which `max_loss_per_position` then
    clamped to one contract, producing the first-ever 1-lot and the first-ever
    10-wide position in the record, plus `position_max_loss_exceeded` and
    `size_cap_exceeded` denials that appear on no prior day.
    """
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _ladder(market, _DELTAS, _PRICES, stale_greeks=(4970,))

    snap = src.snapshot()
    assert snap is not None
    assert len(snap.options) == 4  # nothing lost: every quote is current
    assert {o.strike for o in snap.options if not o.has_delta} == {4970.0}

    sig = CreditSpreadStrategy(_strategy_cfg()).evaluate(snap, time(12, 0)).signal
    assert sig is not None
    assert sig.short_leg.strike == 4975.0     # picked by a delta that IS fresh
    assert sig.long_leg.strike == 4970.0      # the nearest strike, delta or not
    assert sig.long_leg.delta is None
    assert sig.width == 5.0                   # not the 10 the hole used to force
    assert sig.max_loss_per_contract == (5.0 - sig.credit) * 100


async def test_a_stale_delta_never_becomes_a_short_strike():
    """Fail-closed survives the widening: the short leg still needs a real delta.

    4975 is numerically nearest to `target_short_delta` and is exactly the leg
    `min()` would pick — so this pins that the refusal is the delta's freshness and
    not an accident of ordering. It is admitted, priced, and used as the LONG leg
    of the spread built around the next-nearest fresh delta.
    """
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _ladder(market, _DELTAS, _PRICES, stale_greeks=(4975,))

    snap = src.snapshot()
    assert snap is not None
    assert 4975.0 in {o.strike for o in snap.options}  # admitted, unlike before

    # `min_credit` relaxed: the 4980/4975 pair this forces pays 0.30, and the claim
    # under test is which leg takes which role, not what that pair is worth.
    cfg = _strategy_cfg(min_credit=0.25)
    sig = CreditSpreadStrategy(cfg).evaluate(snap, time(12, 0)).signal
    assert sig is not None
    assert sig.short_leg.strike == 4980.0  # NOT 4975, whose delta is stale
    assert sig.short_leg.delta == -0.18
    assert sig.long_leg.strike == 4975.0   # the same leg, in the role it can hold


async def test_a_chain_with_no_fresh_delta_refuses_rather_than_guessing():
    # The residual the split does not remove, and must not: with every delta stale
    # there is no short strike to choose, so the entry is refused — under its own
    # reason, which names the lagging greeks feed instead of vanishing into the
    # `signal.skip` that hid this for a session.
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)
    src = await _ladder(market, _DELTAS, _PRICES, stale_greeks=tuple(_DELTAS))

    snap = src.snapshot()
    assert snap is not None and len(snap.options) == 4  # the ladder is fully priceable

    result = CreditSpreadStrategy(_strategy_cfg()).evaluate(snap, time(12, 0))
    assert result.signal is None
    assert result.reason == "no_short_leg_with_fresh_delta"
    assert result.detail["per_side"]["put"]["candidates"] == 4


def test_max_age_is_required_and_has_no_silent_default():
    # A default is how one call site ends up ungated. Both production callers pass
    # `staleness.market_data_max_age_s`; the constructor refuses to let a third
    # forget.
    with pytest.raises(TypeError, match="max_age_s"):
        DXLinkChainSource(
            FakeMarketData(),
            underlying_symbol="SPX",
            chain_symbol="SPXW",
            strike_window_points=100.0,
            clock=lambda: 0.0,
        )


async def test_snapshot_none_when_nothing_has_data():
    market = FakeMarketData()
    market.push("SPX", 4999.0, 5001.0)

    async def fetcher(session, symbol):
        return {TODAY: [_sdk_option(4975, OptionType.PUT)]}

    src = _source(market, chain_fetcher=fetcher)
    await src.load(object(), today=TODAY)
    assert src.snapshot() is None  # subscribed, but no quotes/greeks arrived yet
