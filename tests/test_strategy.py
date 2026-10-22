"""CreditSpreadStrategy: strike selection, credit/window/tradeability gating."""

from __future__ import annotations

from datetime import time

from argus.config import SignalConfig
from argus.signal.models import ChainSnapshot, OptionQuote
from argus.signal.strategy import CreditSpreadStrategy

MIDDAY = time(12, 0)


def _cfg(**over) -> SignalConfig:
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


def _opt(strike, right, delta, bid, ask):
    return OptionQuote(f"{right[0].upper()}{strike:g}", strike, right, delta, bid, ask)


def _put_chain():
    return ChainSnapshot(
        "SPXW",
        5000.0,
        (
            _opt(4985, "put", -0.25, 2.00, 2.10),
            _opt(4980, "put", -0.20, 1.80, 1.90),
            _opt(4975, "put", -0.15, 1.50, 1.60),  # short (delta 0.15)
            _opt(4970, "put", -0.10, 0.70, 0.80),  # long (4975 - 5)
            _opt(4965, "put", -0.07, 0.40, 0.50),
        ),
        captured_at=0.0,
    )


def _call_chain():
    return ChainSnapshot(
        "SPXW",
        5000.0,
        (
            _opt(5015, "call", 0.25, 2.00, 2.10),
            _opt(5020, "call", 0.20, 1.80, 1.90),
            _opt(5025, "call", 0.15, 1.50, 1.60),  # short
            _opt(5030, "call", 0.10, 0.70, 0.80),  # long (5025 + 5)
        ),
        captured_at=0.0,
    )


def test_selects_put_credit_spread_by_delta_and_width():
    sig = CreditSpreadStrategy(_cfg()).evaluate(_put_chain(), MIDDAY).signal
    assert sig is not None
    assert sig.side == "put"
    assert sig.short_leg.strike == 4975
    assert sig.long_leg.strike == 4970
    assert round(sig.credit, 2) == 0.80  # 1.55 - 0.75
    assert sig.max_loss_per_contract == (5.0 - 0.80) * 100


def test_signal_carries_the_snapshots_timestamp():
    """Freshness has to travel WITH the candidate: order construction bounds how
    stale a signal may be, and it can only do that if selection preserves when the
    chain was captured (`execution.build`)."""
    chain = ChainSnapshot("SPXW", 5000.0, _put_chain().options, captured_at=1234.5)
    sig = CreditSpreadStrategy(_cfg()).evaluate(chain, MIDDAY).signal
    assert sig is not None and sig.captured_at == 1234.5


def test_selects_call_credit_spread():
    sig = CreditSpreadStrategy(_cfg(sides=("call",))).evaluate(_call_chain(), MIDDAY).signal
    assert sig is not None
    assert sig.short_leg.strike == 5025
    assert sig.long_leg.strike == 5030


def test_no_signal_outside_entry_window():
    assert CreditSpreadStrategy(_cfg()).evaluate(_put_chain(), time(9, 0)).signal is None
    assert CreditSpreadStrategy(_cfg()).evaluate(_put_chain(), time(15, 30)).signal is None


def test_rejects_when_credit_below_minimum():
    assert CreditSpreadStrategy(_cfg(min_credit=1.00)).evaluate(_put_chain(), MIDDAY).signal is None


def _without_4970():
    chain = _put_chain()
    return ChainSnapshot(
        chain.underlying,
        chain.underlying_price,
        tuple(o for o in chain.options if o.strike != 4970),
        chain.captured_at,
    )


def test_falls_back_to_next_strike_when_exact_width_missing():
    # Ladders have gaps; the long leg becomes the nearest strike at least `width`
    # further OTM, and the economics use the ACTUAL distance (10, not 5).
    sig = CreditSpreadStrategy(_cfg()).evaluate(_without_4970(), MIDDAY).signal
    assert sig is not None
    assert sig.long_leg.strike == 4965
    assert sig.width == 10.0
    assert round(sig.credit, 2) == 1.10          # 1.55 - 0.45
    assert sig.max_loss_per_contract == (10.0 - 1.10) * 100  # actual width, not desired


def test_no_signal_when_no_strike_within_max_width():
    # max_width == width means only an exact match qualifies; 4970 is gone.
    strategy = CreditSpreadStrategy(_cfg(max_width=5.0))
    assert strategy.evaluate(_without_4970(), MIDDAY).signal is None


# -- rejection diagnostics (the shadow-mode tuning dataset) ---------------


def test_credit_rejection_records_the_near_miss():
    # The whole point: a rejected evaluation must say WHICH strikes, at what
    # deltas, and what credit they'd have paid — so min_credit can be tuned
    # from the audit log instead of re-running the day.
    result = CreditSpreadStrategy(_cfg(min_credit=1.00)).evaluate(_put_chain(), MIDDAY)
    assert result.signal is None
    assert result.reason == "credit_below_min"
    put = result.detail["per_side"]["put"]
    assert put["short_strike"] == 4975 and put["long_strike"] == 4970
    assert put["short_delta"] == -0.15
    assert round(put["credit"], 2) == 0.80   # would have paid 0.80
    assert put["min_credit"] == 1.00         # against the threshold that rejected it
    assert put["width"] == 5.0


def test_outside_window_rejection_records_times():
    result = CreditSpreadStrategy(_cfg()).evaluate(_put_chain(), time(9, 0))
    assert result.reason == "outside_entry_window"
    assert result.detail["now_et"] == "09:00:00"
    assert result.detail["entry_earliest"] == "10:00:00"


def test_no_long_leg_rejection_records_short_leg():
    result = CreditSpreadStrategy(_cfg(max_width=5.0)).evaluate(_without_4970(), MIDDAY)
    assert result.reason == "no_long_leg_within_max_width"
    put = result.detail["per_side"]["put"]
    assert put["short_strike"] == 4975
    assert put["max_width"] == 5.0


def test_headline_reason_is_the_furthest_advanced_side():
    # Puts get to a credit near-miss; calls have no tradeable legs at all. The
    # near-miss is the informative one, so it becomes the headline.
    chain = ChainSnapshot(
        "SPXW", 5000.0,
        _put_chain().options + (_opt(5025, "call", 0.15, 0.0, 0.0),),  # call untradeable
        0.0,
    )
    result = CreditSpreadStrategy(
        _cfg(sides=("call", "put"), min_credit=1.00)
    ).evaluate(chain, MIDDAY)
    assert result.reason == "credit_below_min"           # not "no_tradeable_legs"
    assert set(result.detail["per_side"]) == {"put", "call"}  # both still recorded


def test_rejection_detail_is_json_serializable():
    import json

    result = CreditSpreadStrategy(_cfg(min_credit=1.00)).evaluate(_put_chain(), MIDDAY)
    json.dumps(result.detail, allow_nan=False)  # audit hashing forbids NaN/inf


def test_untradeable_legs_yield_no_signal():
    chain = ChainSnapshot(
        "SPXW", 5000.0,
        tuple(_opt(s, "put", -0.15, 0.0, 0.0) for s in (4975, 4970)),  # ask 0 -> untradeable
        0.0,
    )
    assert CreditSpreadStrategy(_cfg()).evaluate(chain, MIDDAY).signal is None


# -- the delta a leg needs depends on the role it plays (2026-08-24) ------
#
# `live_chain._admit` admits a leg on a fresh QUOTE and leaves `delta` None when
# the greeks behind it are stale, because no leg has a role until `_select` runs.
# This is where the greeks-freshness rail lives now: narrowed to the short leg,
# never dropped.


def _chain(options):
    return ChainSnapshot("SPXW", 5000.0, tuple(options), captured_at=0.0)


def _put_chain_without_delta(*strikes):
    """`_put_chain()` with `strikes`' deltas dropped, everything else identical."""
    return _chain(
        OptionQuote(o.symbol, o.strike, o.right, None, o.bid, o.ask)
        if o.strike in strikes
        else o
        for o in _put_chain().options
    )


def test_a_leg_without_a_delta_is_still_a_long_leg():
    # 4970 is the long leg of the baseline spread and is chosen by strike distance
    # alone. Dropping its delta must change nothing at all — not the pairing, not
    # the width, not the credit.
    sig = CreditSpreadStrategy(_cfg()).evaluate(_put_chain_without_delta(4970), MIDDAY).signal
    assert sig is not None
    assert (sig.short_leg.strike, sig.long_leg.strike) == (4975, 4970)
    assert sig.long_leg.delta is None
    assert sig.width == 5.0
    assert round(sig.credit, 2) == 0.80


def _ladder_targeting_4975(delta_4975):
    """A ladder where 4975 is the delta target and 4980 is unambiguously the
    next-nearest.

    `_put_chain()`'s -0.20 and -0.10 neighbours are equidistant from 0.15, and
    float arithmetic — not the rule under test — settles that tie. 4975 doubles as
    the long leg of the 4980 spread, so one chain shows both halves of the split:
    refused as a short strike, used as a long one.
    """
    return _chain((
        _opt(4985, "put", -0.30, 2.00, 2.10),
        _opt(4980, "put", -0.18, 1.60, 1.70),
        _opt(4975, "put", delta_4975, 0.70, 0.80),
        _opt(4970, "put", -0.08, 0.40, 0.50),
        _opt(4965, "put", -0.05, 0.20, 0.30),
    ))


def test_a_leg_without_a_delta_is_never_the_short_leg():
    # 4975 sits exactly on `target_short_delta` and is what `min()` returns when
    # nothing filters it — including when its key is None or NaN, neither of which
    # ever loses a comparison. So this is the case the filter exists for.
    sig = CreditSpreadStrategy(_cfg()).evaluate(_ladder_targeting_4975(None), MIDDAY).signal
    assert sig is not None
    assert sig.short_leg.strike == 4980       # next-nearest FRESH delta
    assert sig.short_leg.delta == -0.18
    assert sig.long_leg.strike == 4975        # the skipped leg, in the role it can hold
    assert sig.long_leg.delta is None

    # The control, on the identical ladder: with that delta fresh, 4975 IS the
    # short leg. So the refusal above is the delta's freshness and nothing else.
    # (`min_credit` relaxed only here — the 4975/4970 pair pays 0.30.)
    fresh = CreditSpreadStrategy(_cfg(min_credit=0.25)).evaluate(
        _ladder_targeting_4975(-0.15), MIDDAY
    ).signal
    assert fresh is not None and fresh.short_leg.strike == 4975


def test_no_short_leg_when_no_delta_is_fresh_anywhere():
    chain = _put_chain_without_delta(4985, 4980, 4975, 4970, 4965)
    result = CreditSpreadStrategy(_cfg()).evaluate(chain, MIDDAY)
    assert result.signal is None
    assert result.reason == "no_short_leg_with_fresh_delta"
    put = result.detail["per_side"]["put"]
    # The ladder was fully priceable — that is the diagnosis, and it is what
    # separates a lagging greeks feed from an empty chain.
    assert put["candidates"] == 5


# -- the age survives the verdict (2026-08-27) ----------------------------
#
# `delta is None` records THAT the greeks feed was behind and destroys BY HOW MUCH.
# The distance is the dataset the next bound — a drift-based one — gets sized from,
# so it has to reach the log on the tick that was REFUSED, not only on the ones
# that produced a candidate.


def test_a_refused_tick_records_how_far_behind_the_greeks_feed_was():
    chain = _chain(
        (
            OptionQuote("P4980", 4980, "put", None, 1.80, 1.90, greeks_age_s=310.0),
            OptionQuote("P4975", 4975, "put", None, 1.50, 1.60, greeks_age_s=137.0),
            OptionQuote("P4970", 4970, "put", None, 0.70, 0.80, greeks_age_s=421.0),
        )
    )
    result = CreditSpreadStrategy(_cfg()).evaluate(chain, MIDDAY)
    assert result.reason == "no_short_leg_with_fresh_delta"
    put = result.detail["per_side"]["put"]
    # The FRESHEST reading on the side, which is what a bound would have had to
    # clear. A mean or a max would answer a question nobody asked.
    assert put["min_greeks_age_s"] == 137.0


def test_a_side_that_never_had_greeks_reports_none_not_zero():
    """Never-seen and late are different conditions.

    A chain with no greeks at all is an entitlement or feed problem; one whose
    greeks are behind is a cadence problem. A defaulted 0 would collapse them, and
    +inf cannot reach the audit log at all — `json.dumps(allow_nan=False)` refuses
    it, and a refused Tier 1 write halts trading.
    """
    import json

    chain = _put_chain_without_delta(4985, 4980, 4975, 4970, 4965)
    result = CreditSpreadStrategy(_cfg()).evaluate(chain, MIDDAY)
    assert result.detail["per_side"]["put"]["min_greeks_age_s"] is None
    json.dumps(result.detail, allow_nan=False)


def test_a_candidate_records_the_age_of_the_delta_it_was_chosen_by():
    """The paired half. Together with `underlying_price` on the same record, the
    realized spot drift over that age is recoverable at analysis time — which is
    what a drift tolerance is calibrated against."""
    import json

    chain = _chain(
        (
            OptionQuote("P4975", 4975, "put", -0.15, 1.50, 1.60, greeks_age_s=88.0),
            OptionQuote("P4970", 4970, "put", -0.10, 0.70, 0.80, greeks_age_s=91.0),
        )
    )
    result = CreditSpreadStrategy(_cfg()).evaluate(chain, MIDDAY)
    assert result.signal is not None
    payload = result.signal.to_payload()
    assert payload["short"]["greeks_age_s"] == 88.0
    json.dumps(payload, allow_nan=False)


def test_a_synthesised_candidate_serialises_with_no_age():
    """Chunk 4's exit path builds a `SpreadSignal` purely to reach `build_order`,
    from quotes that carry no greeks age at all. +inf is the DEFAULT there, so
    without the guard every such candidate would carry an unencodable value into a
    Tier 1 write."""
    import json

    chain = _chain(
        (
            OptionQuote("P4975", 4975, "put", -0.15, 1.50, 1.60),  # default +inf
            OptionQuote("P4970", 4970, "put", -0.10, 0.70, 0.80),
        )
    )
    result = CreditSpreadStrategy(_cfg()).evaluate(chain, MIDDAY)
    assert result.signal is not None
    payload = result.signal.to_payload()
    assert payload["short"]["greeks_age_s"] is None
    json.dumps(payload, allow_nan=False)


def test_a_credit_rejection_carries_the_age_too():
    # A tick refused on CREDIT still selected a short strike, so it belongs in the
    # same age distribution. Leaving it off biases the dataset toward the ticks
    # that happened to clear `min_credit`.
    chain = _chain(
        (
            OptionQuote("P4975", 4975, "put", -0.15, 1.50, 1.60, greeks_age_s=64.0),
            OptionQuote("P4970", 4970, "put", -0.10, 1.40, 1.50, greeks_age_s=64.0),
        )
    )
    result = CreditSpreadStrategy(_cfg(min_credit=5.00)).evaluate(chain, MIDDAY)
    assert result.reason == "credit_below_min"
    assert result.detail["per_side"]["put"]["short_greeks_age_s"] == 64.0


def test_the_age_is_telemetry_and_never_an_admission_test():
    """`has_delta` remains the single gate. A leg whose delta is present is
    selectable however old the reading behind it says it is — the bound was
    already applied upstream, in `live_chain._admit`, and applying it twice
    against two different numbers is how they drift apart."""
    chain = _chain(
        (
            OptionQuote("P4975", 4975, "put", -0.15, 1.50, 1.60, greeks_age_s=9_999.0),
            OptionQuote("P4970", 4970, "put", -0.10, 0.70, 0.80, greeks_age_s=9_999.0),
        )
    )
    result = CreditSpreadStrategy(_cfg()).evaluate(chain, MIDDAY)
    assert result.signal is not None
    assert result.signal.short_leg.strike == 4975


def test_a_nan_delta_is_refused_as_a_short_leg_too():
    # `OptionGreeks.delta` is a float straight off the feed. NaN is the value
    # `min()` cannot reject for itself: every comparison against it is False, so it
    # is RETURNED rather than skipped. `has_delta` tests finiteness for that reason.
    sig = CreditSpreadStrategy(_cfg()).evaluate(
        _ladder_targeting_4975(float("nan")), MIDDAY
    ).signal
    assert sig is not None
    assert sig.short_leg.strike == 4980
    assert sig.long_leg.strike == 4975
    # And it never reaches the audit log as NaN, which would refuse the Tier 1
    # write and halt trading.
    assert sig.to_payload()["long"]["delta"] is None


def test_a_delta_less_long_leg_stays_audit_safe():
    # Canonical audit hashing runs `json.dumps(..., allow_nan=False)`, and a
    # refused Tier 1 write halts trading. A long leg with no delta must serialize
    # as null and must never reach the log as NaN.
    import json

    result = CreditSpreadStrategy(_cfg()).evaluate(_put_chain_without_delta(4970), MIDDAY)
    assert result.signal is not None
    payload = result.signal.to_payload()
    assert payload["long"]["delta"] is None
    json.dumps(payload, allow_nan=False)

    refused = CreditSpreadStrategy(_cfg(min_credit=1.00)).evaluate(
        _put_chain_without_delta(4970), MIDDAY
    )
    assert refused.detail["per_side"]["put"]["long_delta"] is None
    json.dumps(refused.detail, allow_nan=False)


def test_a_lagging_greeks_feed_outranks_an_empty_chain_in_the_headline():
    # Puts are fully priceable and only their deltas are missing; calls have no
    # market at all. The greeks diagnosis is the informative one — it says the
    # ladder was there and something else was wrong.
    chain = _chain(
        _put_chain_without_delta(4985, 4980, 4975, 4970, 4965).options
        + (_opt(5025, "call", 0.15, 0.0, 0.0),)  # call untradeable
    )
    result = CreditSpreadStrategy(_cfg(sides=("call", "put"))).evaluate(chain, MIDDAY)
    assert result.reason == "no_short_leg_with_fresh_delta"  # not "no_tradeable_legs"
    assert set(result.detail["per_side"]) == {"put", "call"}


# -- the quoted spread reaches the near-miss too (2026-09-15) -------------
#
# `mid` was recorded from the first day and the market it came from never was, so
# the log could say what Argus THOUGHT a spread was worth and never what it would
# have COST to trade. These four fields are the second half, and they are on the
# rejected tick as well as the accepted one deliberately: `min_credit` rejects on
# credit, credit is a difference of floored mids, and a floored mid is a function
# of the very spread being measured — so recording the market only where the
# credit cleared samples the tail of its own distribution.


def test_a_credit_rejection_records_the_market_both_legs_were_quoted_at():
    result = CreditSpreadStrategy(_cfg(min_credit=1.00)).evaluate(_put_chain(), MIDDAY)
    put = result.detail["per_side"]["put"]
    assert (put["short_bid"], put["short_ask"]) == (1.50, 1.60)
    assert (put["long_bid"], put["long_ask"]) == (0.70, 0.80)
    # The credit it was rejected ON is the floored-mid difference of exactly those
    # four numbers, so the friction the credit ignores is now recoverable from the
    # same record rather than lost with the tick.
    assert round(put["credit"], 2) == 0.80


def test_a_credit_rejection_records_a_zero_bid_as_zero():
    """The case the raw prices exist for. `floor_bid` turns a 0 bid into a tick
    before the mid is taken, so the recorded credit is optimistic by exactly that
    floor and nothing in the record said so. `long_bid == 0.0` is the only way the
    floor's firing is visible at all — and a derived half-spread would have hidden
    it, since `(ask - 0)/2` reads as a very wide market rather than an absent one."""
    chain = _chain((
        _opt(4975, "put", -0.15, 1.50, 1.60),
        _opt(4970, "put", -0.10, 0.00, 0.10),
    ))
    result = CreditSpreadStrategy(_cfg(min_credit=5.00)).evaluate(chain, MIDDAY)
    put = result.detail["per_side"]["put"]
    assert put["long_bid"] == 0.0
    # 1.55 - (0.05 + 0.10)/2 = 1.475: the floor, not the market, set the long mid.
    assert round(put["credit"], 3) == 1.475


def test_a_rejection_with_no_long_leg_claims_no_long_prices():
    """The scope line, pinned. The four price fields live on the near-miss — the
    branch where BOTH legs were selected and priced — and nowhere else. A rejection
    that never found a long leg has no long market to report, and inventing
    `long_bid: null` there would put a key in the dataset that means "this tick had
    no long leg" in one place and "the feed gave no bid" in another."""
    result = CreditSpreadStrategy(_cfg(max_width=5.0)).evaluate(_without_4970(), MIDDAY)
    put = result.detail["per_side"]["put"]
    assert result.reason == "no_long_leg_within_max_width"
    assert not {"short_bid", "short_ask", "long_bid", "long_ask"} & set(put)


def test_a_rejection_with_no_short_leg_claims_no_prices_either():
    chain = _put_chain_without_delta(4985, 4980, 4975, 4970, 4965)
    result = CreditSpreadStrategy(_cfg()).evaluate(chain, MIDDAY)
    put = result.detail["per_side"]["put"]
    assert result.reason == "no_short_leg_with_fresh_delta"
    assert not {"short_bid", "short_ask", "long_bid", "long_ask"} & set(put)


def test_a_candidate_records_the_raw_market_beside_the_floored_mid():
    """The entry record's half. RAW per leg rather than a derived width, because
    which leg is the wide one is a SELECTION question here — it bears on
    `target_short_delta` and on the long-leg search — and because every derived
    quantity follows from `(bid, ask)` while the reverse is false. The exit side
    goes the other way for the opposite reason; by then the strikes are fixed."""
    import json

    sig = CreditSpreadStrategy(_cfg()).evaluate(_put_chain(), MIDDAY).signal
    assert sig is not None
    payload = sig.to_payload()
    assert (payload["short"]["bid"], payload["short"]["ask"]) == (1.50, 1.60)
    assert (payload["long"]["bid"], payload["long"]["ask"]) == (0.70, 0.80)
    # `mid` STAYS despite being derivable: it is a wire contract `marker._decode`
    # and `tools.desk` read back, and dropping a key readers depend on to save 20
    # bytes is the trade nobody wants.
    assert payload["short"]["mid"] == 1.55
    json.dumps(payload, allow_nan=False)


def test_put_preferred_when_both_sides_configured():
    # Both sides present; put qualifies, so it's returned first.
    both = ChainSnapshot(
        "SPXW", 5000.0, _put_chain().options + _call_chain().options, 0.0
    )
    sig = CreditSpreadStrategy(_cfg(sides=("put", "call"))).evaluate(both, MIDDAY).signal
    assert sig is not None and sig.side == "put"
