"""THE payload-safety invariant: a hostile quote must never refuse a record.

This file exists because of what a refused audit write actually costs, which is
worse than "the record is missing" on both tiers and different on each:

  * **Tier 1** — `AuditLog.record` raises `ValueError` from
    `_reject_unserializable`, NOT `AuditWriteError`. `signal.candidate` is written
    in `orchestrator.evaluate_once` ABOVE the gate and above the submit, and
    `SignalLoop.run` catches the raise in its generic "a bad iteration must not
    kill the loop" handler. So one non-finite field silently suppresses EVERY
    ENTRY for as long as the condition holds — no alert, no `/status` row, no
    audit record, one `logger.exception` per tick. A telemetry field would have
    become a trading-behaviour change.
  * **Tier 2** — `_consume` drops the WHOLE record with a `tier2_unserializable`
    alert. On `signal.none` that discards the near-miss dataset; on
    `watchdog.check` it discards the mark trail `tools.desk` reads.

So the invariant is pinned BEHAVIOURALLY — substitute NaN, +inf and -inf into
each numeric input in turn and assert the payload still encodes — rather than by
an AST test over the spelling. It pins the harm, not the shape, and it is the
test that would have caught the unguarded `cost_to_close` that reached
`TriggerEvaluation.to_payload` for as long as that method existed.

**Why these values cannot arrive from the live adapter, and why the guard is not
therefore pointless.** `DXLinkMarketData._ingest` maps every side through
`_to_float`, which returns `None` for NaN/inf and then maps `None` to `0.0`, so
`Quote.bid`/`ask` are finite by construction there. But `MarketDataPort` is a
`Protocol` with three implementations today and a `SimBroker` planned, and it
makes NO finiteness promise. The guard is a property of the payload boundary, not
of whichever adapter happens to be installed — the same reason
`evaluate_triggers` already tests `is_finite(cost_to_close)` and `has_delta`
already tests the delta.

→ docs/designs/2026-09-15-recording-the-quoted-spread.md
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import math
import pathlib
from datetime import date, time

import pytest

from support import loss_budget_source
from argus.execution.watchdog import (
    LegQuote,
    SpreadMarket,
    TriggerEvaluation,
    TriggerReason,
    _STOP_TRIGGER_REASONS,
)
from argus.signal.contracts import OptionContract
from argus.signal.models import (
    ChainSnapshot,
    OptionQuote,
    SpreadSignal,
    payload_number,
)
from argus.signal.strategy import CreditSpreadStrategy, RegimeSnapshot

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "argus"

#: Every value `json.dumps(..., allow_nan=False)` refuses. The canonical hash uses
#: exactly that call, so this list IS the set of inputs that can refuse a record.
HOSTILE = [float("nan"), float("inf"), float("-inf")]


def _encodes(payload) -> str:
    """The canonical encoding, verbatim. Raises exactly where the audit log would."""
    return json.dumps(payload, allow_nan=False)


# -- the helper itself --------------------------------------------------------


@pytest.mark.parametrize("value", HOSTILE)
def test_the_helper_answers_none_for_everything_json_refuses(value):
    assert payload_number(value) is None


@pytest.mark.parametrize("value", [0.0, -0.0, 1.5, -1.5, 1e308, 4975, None])
def test_the_helper_is_transparent_to_everything_else(value):
    """Including `0.0` and `None`, which are DIFFERENT answers and must stay so: a
    zero bid is real market data (deep OTM), and a long leg legitimately carries no
    delta at all. Collapsing either into the other is the failure this guard must
    not introduce while preventing the other one."""
    assert payload_number(value) == value or (value is None and payload_number(value) is None)


def test_the_helper_agrees_with_every_spelling_it_replaced():
    """The collapse is behaviour-identical, which is what made it safe.

    Four spellings existed: `_payload_delta`'s `leg.delta if leg.has_delta else
    None`, `_payload_age`'s and `underlying_price`'s inline `is_finite` ternaries,
    and `strategy._num`'s `value is not None and math.isfinite(value)`. They agree
    on every input, `None` and NaN included — asserted here rather than by reading.
    """
    for value in [*HOSTILE, None, 0.0, -0.0, 1.5, -1.5, 4975.0]:
        leg = OptionQuote("P4975", 4975.0, "put", value, 1.5, 1.6)
        old_delta_spelling = leg.delta if leg.has_delta else None
        old_num_spelling = (
            value if value is not None and math.isfinite(value) else None
        )
        assert payload_number(value) is old_delta_spelling
        assert payload_number(value) is old_num_spelling


# -- SpreadSignal.to_payload (Tier 1, on the signal loop) ---------------------


def _quote(strike, /, **over) -> OptionQuote:
    # Positional-only, so a test can substitute a hostile `strike=` through `over`
    # without colliding with the one the symbol is named from.
    base = dict(
        symbol=f"P{strike:g}", strike=float(strike), right="put",
        delta=-0.15, bid=1.50, ask=1.60, greeks_age_s=1.0,
    )
    base.update(over)
    return OptionQuote(**base)


def _signal(**over) -> SpreadSignal:
    base = dict(
        underlying="SPXW", side="put",
        short_leg=_quote(4975), long_leg=_quote(4970, delta=-0.10, bid=0.70, ask=0.80),
        credit=0.80, width=5.0, captured_at=0.0, underlying_price=5000.0,
    )
    base.update(over)
    return SpreadSignal(**base)


#: Every numeric that arrives from a market-data port or is derived from one.
#: Config constants are deliberately NOT in here — they come through the
#: fail-closed loader, which is a different boundary with its own validation.
LEG_NUMERICS = ["strike", "delta", "bid", "ask", "greeks_age_s"]
SIGNAL_NUMERICS = ["credit", "width", "underlying_price"]


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("leg", ["short_leg", "long_leg"])
@pytest.mark.parametrize("field", LEG_NUMERICS)
def test_a_hostile_leg_quote_still_encodes(field, leg, value):
    hostile = dataclasses.replace(getattr(_signal(), leg), **{field: value})
    signal = _signal(**{leg: hostile})
    _encodes(signal.to_payload())


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("field", SIGNAL_NUMERICS)
def test_a_hostile_signal_number_still_encodes(field, value):
    _encodes(_signal(**{field: value}).to_payload())


def test_the_raw_market_reaches_the_record_beside_the_floored_mid():
    """The change itself. `mid` answers *what did Argus think this was worth*;
    `bid`/`ask` answer *what would it have cost to trade*, and until now only the
    first of those was ever recorded."""
    payload = _signal().to_payload()
    assert payload["short"]["bid"] == 1.50 and payload["short"]["ask"] == 1.60
    assert payload["long"]["bid"] == 0.70 and payload["long"]["ask"] == 0.80
    assert payload["short"]["mid"] == pytest.approx(1.55)
    _encodes(payload)


def test_a_zero_bid_is_recorded_as_zero_and_the_mid_is_not():
    """The whole argument for raw prices over a derived half-spread.

    `floor_bid` returns one tick for any `bid <= 0`, so the recorded `mid` is
    `(0.05 + ask)/2` with nothing in the record saying the floor fired — and the
    credit built on it is systematically optimistic relative to a crossing exit by
    exactly that floor. A derived half-spread would hide it twice over: `(ask - 0)/2`
    is not a half-spread, it is half the ask, and it would enter the dataset looking
    like a very wide market rather than an absent one.
    """
    payload = _signal(long_leg=_quote(4970, delta=-0.10, bid=0.0, ask=0.10)).to_payload()
    assert payload["long"]["bid"] == 0.0            # the floor's firing, visible
    assert payload["long"]["mid"] == pytest.approx(0.075)   # (0.05 + 0.10) / 2
    _encodes(payload)


def test_the_greeks_age_is_only_on_the_short_leg():
    """It is the age of the delta the strike was CHOSEN BY, and only one leg is
    chosen by delta. Carrying it on the long leg would assert a selection that
    never happened."""
    payload = _signal().to_payload()
    assert payload["short"]["greeks_age_s"] == 1.0
    assert "greeks_age_s" not in payload["long"]


# -- the near-miss detail (Tier 2, `signal.none`) -----------------------------


def _cfg(**over):
    from argus.config import SignalConfig

    base = dict(
        sides=("put",), target_short_delta=0.15, width=5.0, max_width=10.0,
        min_credit=0.75, entry_earliest=time(10, 0), entry_latest=time(15, 0),
        cadence_s=5.0,
        short_delta_source="feed",
    )
    base.update(over)
    return SignalConfig(**base)


def _chain(*options) -> ChainSnapshot:
    return ChainSnapshot("SPXW", 5000.0, tuple(options), captured_at=0.0)


def _near_miss_chain(**over) -> ChainSnapshot:
    """A ladder that reaches the near-miss branch: both legs selected and priced."""
    return _chain(
        _quote(4975, **over),
        _quote(4970, delta=-0.10, bid=0.70, ask=0.80),
    )


def test_the_near_miss_carries_the_quoted_spread_of_both_legs():
    """The decision that makes the dataset usable rather than merely present.

    `min_credit` rejects on CREDIT, credit is a difference of floored mids, and a
    floored mid is a function of the very spread being measured — so recording the
    market only where the credit cleared samples the tail of its own distribution.
    """
    result = CreditSpreadStrategy(_cfg(min_credit=5.00)).evaluate(
        _near_miss_chain(), time(12, 0)
    )
    put = result.detail["per_side"]["put"]
    assert result.reason == "credit_below_min"
    assert (put["short_bid"], put["short_ask"]) == (1.50, 1.60)
    assert (put["long_bid"], put["long_ask"]) == (0.70, 0.80)
    _encodes(result.detail)


def test_the_near_miss_keys_are_flat_like_the_record_they_sit_in():
    """Match the record you are joining, not the other record. `SpreadSignal`
    nests (`short`/`long` dicts) and the near-miss does not (`short_strike`,
    `long_delta`), so the two shapes differ deliberately."""
    result = CreditSpreadStrategy(_cfg(min_credit=5.00)).evaluate(
        _near_miss_chain(), time(12, 0)
    )
    put = result.detail["per_side"]["put"]
    assert "short" not in put and "long" not in put
    assert {"short_bid", "short_ask", "long_bid", "long_ask"} <= set(put)


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("field", LEG_NUMERICS)
def test_a_hostile_quote_never_refuses_a_signal_none_record(field, value):
    """Whatever branch a hostile quote lands in — no tradeable legs, no fresh
    delta, a near miss, or a fired candidate — the record it produces must encode.
    The branch is not the invariant; the encodability is."""
    strategy = CreditSpreadStrategy(_cfg(min_credit=5.00))
    result = strategy.evaluate(_near_miss_chain(**{field: value}), time(12, 0))
    _encodes(result.detail)
    if result.signal is not None:
        _encodes(result.signal.to_payload())


#: The mid selector's per-tick inputs (2026-09-21): the regime's remaining life
#: and the spot the forward falls back to.
_MID_REGIME = RegimeSnapshot(
    atm_iv=0.2, skew=0.0, today=date(2026, 9, 22), minutes_to_settlement=300.0
)


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("field", LEG_NUMERICS)
@pytest.mark.parametrize("min_credit", [0.05, 5.00], ids=["fires", "near-miss"])
def test_a_hostile_quote_never_refuses_a_mid_selected_record(field, value, min_credit):
    """The same invariant through the MID selector, where a hostile quote reaches
    arithmetic — an inversion, a forward — before it reaches the record. Every
    number it produces (`mid_delta`, `mid_iv`, `forward`, `alt_pick_strike`) goes
    through `payload_number`, and a refused `signal.candidate` would silently
    suppress every entry."""
    strategy = CreditSpreadStrategy(_cfg(min_credit=min_credit, short_delta_source="mid"))
    chain = ChainSnapshot(
        "SPXW", 5000.0,
        (_quote(4975, **{field: value}), _quote(4970, delta=-0.10, bid=0.70, ask=0.80)),
        captured_at=0.0,
    )
    result = strategy.evaluate(chain, time(12, 0), _MID_REGIME)
    _encodes(result.detail)
    if result.signal is not None:
        _encodes(result.signal.to_payload())


@pytest.mark.parametrize("spot", HOSTILE)
@pytest.mark.parametrize("minutes", [*HOSTILE, 300.0])
def test_a_hostile_spot_or_clock_never_refuses_a_mid_selected_record(spot, minutes):
    """The forward's fallback reads spot, and the time comes off the regime."""
    regime = dataclasses.replace(_MID_REGIME, minutes_to_settlement=minutes)
    strategy = CreditSpreadStrategy(_cfg(short_delta_source="mid"))
    result = strategy.evaluate(_chain_at(spot), time(12, 0), regime)
    _encodes(result.detail)
    if result.signal is not None:
        _encodes(result.signal.to_payload())


def _chain_at(spot: float) -> ChainSnapshot:
    return ChainSnapshot(
        "SPXW", spot,
        (_quote(4975), _quote(4970, delta=-0.10, bid=0.70, ask=0.80)),
        captured_at=0.0,
    )


def test_the_selection_context_goes_through_the_guard():
    from argus.signal.models import SelectionContext

    for value in HOSTILE:
        context = SelectionContext(
            delta_source="mid", forward=value, forward_source="spot",
            alt_pick_strike=value, short_mid_delta=value, short_mid_iv=value,
            long_mid_delta=value, long_mid_iv=value,
        )
        _encodes(_signal(selection=context).to_payload())
        _encodes(context.context_payload())


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("field", ["atm_iv", "skew", "minutes_to_settlement"])
def test_a_hostile_regime_reading_still_encodes(field, value):
    """The regime block rides on the same two records, and `atm_iv`/`skew` are NaN
    by DESIGN when the strip is too thin to estimate from — so this one is reached
    on ordinary days, not only by a broken port."""
    regime = RegimeSnapshot(
        **{"atm_iv": 0.2, "skew": 0.0, "today": date(2026, 9, 15),
           "minutes_to_settlement": 300.0, field: value}
    )
    _encodes(regime.to_payload())


# -- TriggerEvaluation.to_payload (Tier 2, the highest-cadence category) ------


def _leg(strike, bid, ask, right="put") -> LegQuote:
    return LegQuote(
        contract=OptionContract(
            f"SPXW  260915{right[0].upper()}0{int(strike)}000",
            f".SPXW260915{right[0].upper()}{strike:g}",
            float(strike), right, date(2026, 9, 15),
        ),
        bid=bid, ask=ask, received_at=0.0, age_s=0.0, delta=-0.15,
    )


def _market(short=(1.50, 1.60), long_=(0.70, 0.80)) -> SpreadMarket:
    return SpreadMarket(short=_leg(5000, *short), long=_leg(4995, *long_))


def _evaluation(**over) -> TriggerEvaluation:
    market = over.pop("market", _market())
    base = dict(
        true_reasons=frozenset(),
        unknown_reasons=frozenset(),
        problems=(),
        cost_to_close=market.cost_to_close if market is not None else None,
        credit_stop=1.60,
        spot=5050.0,
        market=market,
        seconds_to_flatten=1800.0,
        acting_stops=_STOP_TRIGGER_REASONS,
    )
    base.update(over)
    return TriggerEvaluation(**base)


def test_the_crossing_debit_reaches_the_record_beside_the_mark():
    evaluation = _evaluation()
    payload = evaluation.to_payload()
    assert payload["cost_to_close"] == pytest.approx(0.80)   # 1.55 - 0.75
    assert payload["crossing_debit"] == pytest.approx(0.90)  # 1.60 - 0.70
    _encodes(payload)


def test_the_gap_between_them_is_exactly_both_half_spreads():
    """THE identity that decides one exit-side field instead of four.

        cost_to_close  = (fb_s + a_s)/2 - (fb_l + a_l)/2
        crossing_debit =  a_s - b_l
        difference     = (a_s - b_s)/2 + (a_l - b_l)/2

    Vilkov's friction layer (ii) for a two-leg structure, recoverable in full from
    one number plus the one already recorded. Leg ATTRIBUTION is not recoverable
    (four unknowns, two equations) and is accepted: by exit time the strikes are
    fixed and there is no selection decision left to inform.
    """
    from argus.execution.zerobid import PRICE_EPSILON

    market = _market(short=(1.50, 1.60), long_=(0.70, 0.80))
    payload = _evaluation(market=market).to_payload()
    half_spreads = (1.60 - 1.50) / 2 + (0.80 - 0.70) / 2
    assert payload["crossing_debit"] - payload["cost_to_close"] == pytest.approx(
        half_spreads, abs=PRICE_EPSILON
    )


def test_a_zero_bid_long_leg_diverges_from_the_identity_by_the_floor():
    """The caveat, pinned rather than left in prose. `cost_to_close` floors a zero
    bid up to a tick and `crossing_debit` deliberately does not — flooring it would
    make the one order whose entire purpose is to be filled LESS aggressive — so
    the gap carries an extra half-tick. Analysis conditions on two-sidedness, which
    is what the entry side's raw bids are recorded for."""
    market = _market(short=(1.50, 1.60), long_=(0.0, 0.10))
    payload = _evaluation(market=market).to_payload()
    half_spreads = (1.60 - 1.50) / 2 + (0.10 - 0.0) / 2
    gap = payload["crossing_debit"] - payload["cost_to_close"]
    assert gap - half_spreads == pytest.approx(0.025)


def test_a_blind_position_records_null_and_never_zero():
    """Same rule `cost_to_close` already holds, and the reason `/status` renders
    `unpriced` rather than a number: 0.0 is a price, and "could not be priced" is
    not one. A reader that took the difference would otherwise see the whole
    crossing cost appear out of an unpriceable tick."""
    payload = _evaluation(
        market=None,
        cost_to_close=None,
        unknown_reasons=frozenset({TriggerReason.CREDIT_MULTIPLE}),
    ).to_payload()
    assert payload["crossing_debit"] is None
    assert payload["cost_to_close"] is None
    _encodes(payload)


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("field", ["cost_to_close", "credit_stop", "spot", "seconds_to_flatten"])
def test_a_hostile_evaluation_number_still_encodes(field, value):
    _encodes(_evaluation(**{field: value}).to_payload())


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("side", ["short", "long_"])
@pytest.mark.parametrize("price", [0, 1])
def test_a_hostile_leg_price_never_refuses_a_watchdog_record(side, price, value):
    """The regression this file is named for, one field over. `evaluate_triggers`
    already marks `CREDIT_MULTIPLE` unknown and files a `problems` entry when the
    cost to close is unusable — and `to_payload` then recorded the raw value
    anyway, so in the ONE branch where the number is known to be bad, the record
    that would explain the refusal is the record that got dropped."""
    prices = [1.50, 1.60] if side == "short" else [0.70, 0.80]
    prices[price] = value
    market = _market(**{side: tuple(prices)})
    _encodes(_evaluation(market=market).to_payload())


# -- the entry path, end to end (the failure mode this file is about) ---------


def _signal_loop(audit, chain, **signal_over):
    """A real `CreditSpreadStrategy` on a real `SignalLoop` — the stub strategy the
    rest of the orchestrator suite uses cannot reach this, because the stub never
    builds a payload from a quote."""
    from argus.account.fake import FakeAccountState
    from argus.marketdata import FakeMarketData
    from argus.orchestrator import SignalLoop
    from argus.risk import DailyLossBreaker, KillSwitch, PreTradeGate
    from argus.risk.snapshot import DailyPnl, RiskSnapshotBuilder
    from argus.signal.chain import FakeChainSource
    from tests.support import make_config

    cfg = make_config(signal=signal_over)
    market = FakeMarketData()
    market.push("SPX", 5000.0, 5000.5)
    account = FakeAccountState()
    account.set_net_liq(100_000)
    return SignalLoop(
        strategy=CreditSpreadStrategy(cfg.signal),
        chain_source=FakeChainSource(chain),
        snapshot_builder=RiskSnapshotBuilder(
            market, account, DailyPnl(anchor_nlv=100_000), reference_symbols=("SPX",), loss_budget=loss_budget_source()
        ),
        gate=PreTradeGate(
            config=cfg,
            audit=audit,
            killswitch=KillSwitch(audit),
            breaker=DailyLossBreaker(audit, daily_max_loss=cfg.risk.daily_max_loss),
        ),
        audit=audit,
        config=cfg,
        clock_et=lambda: time(12, 0),
        submit=None,
    )


def _categories(audit) -> list[str]:
    return [
        r["category"]
        for r in audit._conn.execute("SELECT category FROM audit_log ORDER BY seq")
    ]


async def test_a_hostile_quote_does_not_silently_suppress_every_entry(audit):
    """THE reason the guard is not a nicety.

    `signal.candidate` is Tier 1 and is written ABOVE `gate.evaluate` and above
    `self._submit`. A refused write raises `ValueError`, which `SignalLoop.run`
    swallows as "a bad iteration" — so before the guard, one non-finite number on
    one leg would have stopped the day's entries outright, with nothing anywhere
    saying so except a `logger.exception` per tick.

    The assertion is therefore not "the payload encodes" (the tests above cover
    that) but "the candidate still REACHED THE GATE". Nothing else in the suite
    would catch this: every other orchestrator test drives a stub strategy whose
    payload never touches a quote.
    """
    # A short leg with a NaN bid. It is still tradeable (`ask > 0`), still
    # selectable (`has_delta`), and `floor_bid` turns the NaN into a tick — so the
    # candidate fires normally and the ONLY casualty is the record. That is what
    # makes this the dangerous shape: nothing about the tick looks wrong.
    chain = _chain(
        _quote(4975, bid=float("nan"), ask=3.30),   # floored mid 1.675
        _quote(4970, delta=-0.10, bid=0.70, ask=0.80),  # mid 0.75 -> credit 0.925
    )
    await _signal_loop(audit, chain).evaluate_once()

    assert _categories(audit) == [
        "signal.candidate", "gate.decision", "shadow.would_submit",
    ]
    audit.verify_chain()

    row = audit._conn.execute(
        "SELECT payload FROM audit_log WHERE category = 'signal.candidate'"
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["short"]["bid"] is None      # recorded as absent, not as a number
    assert payload["short"]["ask"] == 3.30      # and the rest of the leg survives


async def test_the_recorded_candidate_carries_the_market_it_was_priced_from(audit):
    """The ordinary path, asserted through the real audit encoder rather than
    through `to_payload` alone — canonical hashing is what refuses a payload, and
    it is a different call from the one the unit tests make."""
    chain = _chain(
        _quote(4975),
        _quote(4970, delta=-0.10, bid=0.70, ask=0.80),
    )
    await _signal_loop(audit, chain).evaluate_once()

    row = audit._conn.execute(
        "SELECT payload FROM audit_log WHERE category = 'signal.candidate'"
    ).fetchone()
    payload = json.loads(row["payload"])
    assert (payload["short"]["bid"], payload["short"]["ask"]) == (1.50, 1.60)
    assert (payload["long"]["bid"], payload["long"]["ask"]) == (0.70, 0.80)
    audit.verify_chain()


async def test_a_hostile_quote_does_not_drop_the_signal_none_record(audit):
    """The Tier 2 half. A dropped `signal.none` is not a halted loop — it is the
    near-miss dataset this change exists to collect, silently missing the exact
    ticks a broken port produced."""
    chain = _chain(
        _quote(4975, bid=float("nan")),
        _quote(4970, delta=-0.10, bid=0.70, ask=0.80),
    )
    audit.start()
    await _signal_loop(audit, chain, min_credit=5.00).evaluate_once()
    await audit.aclose()

    # The minute's `signal.ladder` row (2026-09-29) survived the same input.
    assert _categories(audit) == ["signal.ladder", "signal.none"]
    row = audit._conn.execute(
        "SELECT payload FROM audit_log WHERE category = 'signal.none'"
    ).fetchone()
    put = json.loads(row["payload"])["per_side"]["put"]
    assert put["short_bid"] is None and put["short_ask"] == 1.60


@pytest.mark.parametrize("spot", [float("nan"), float("inf"), float("-inf")])
async def test_a_hostile_SPOT_does_not_drop_the_signal_none_record(audit, spot):
    """The last raw numeric on an entry record, closed 2026-09-18.

    `orchestrator.evaluate_once` wrote `chain.underlying_price` onto `signal.none`
    directly — one file outside the three this change's guard was scoped to. The
    live adapter cannot produce a non-finite spot (`dxlink._to_float` maps NaN/inf to
    `None` and then `0.0`), but `Quote.mid` lets one through from any other port: a
    NaN fails `ask <= 0.0`, and `live_chain.snapshot()` checks only `is None`. Tier 2
    then drops the WHOLE row, so what is lost is not the spot — it is the near miss
    and its quoted spread, on exactly the ticks a broken port produced.

    The strategy never reads the spot, so the ladder still prices; the assertion is
    that the record survives WITH its near miss, and the spot is `None`, not a number.
    """
    chain = ChainSnapshot(
        "SPXW", spot,
        (_quote(4975), _quote(4970, delta=-0.10, bid=0.70, ask=0.80)),
        captured_at=0.0,
    )
    audit.start()
    await _signal_loop(audit, chain, min_credit=5.00).evaluate_once()
    await audit.aclose()

    # The minute's `signal.ladder` row (2026-09-29) survived the same input.
    assert _categories(audit) == ["signal.ladder", "signal.none"]
    row = audit._conn.execute(
        "SELECT payload FROM audit_log WHERE category = 'signal.none'"
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["underlying_price"] is None
    put = payload["per_side"]["put"]
    assert (put["short_bid"], put["short_ask"]) == (1.50, 1.60)
    audit.verify_chain()


# -- the readers, unbroken ----------------------------------------------------
#
# Adding keys is additive-safe because every reader of these payloads either names
# the keys it wants (`marker._decode`) or uses `.get()` (`tools/desk.py`) — which
# is also why there is no migration and no `PRAGMA user_version` move: the audit
# payload is ONE JSON `TEXT` column, so these are keys in a blob, not columns in a
# table. A database holding rows from both before and after the deploy is the
# expected state, not a reconciliation problem.


async def test_the_marker_decodes_a_candidate_carrying_the_new_keys(db, audit):
    """`shadow.would_submit` embeds `signal.to_payload()` WHOLE through
    `markable_payload`, so the new leg fields arrive in the scoring record for
    free — which is desirable, and is why that helper is a function rather than a
    convention."""
    from argus.risk.models import EntryProposal
    from argus.shadow.marker import read_proposals
    from argus.shadow.outcome import markable_payload

    signal = _signal()
    audit._clock = lambda: "2026-09-15T18:30:00.000000Z"
    await audit.record(
        "shadow.would_submit",
        markable_payload(signal, EntryProposal(signal.describe(), 2, 840.0)),
    )

    proposals = read_proposals(db, date(2026, 9, 15))
    assert len(proposals) == 1
    assert proposals[0].short_strike == 4975.0 and proposals[0].credit == 0.80

    stored = json.loads(
        audit._conn.execute(
            "SELECT payload FROM audit_log WHERE category = 'shadow.would_submit'"
        ).fetchone()["payload"]
    )
    assert stored["signal"]["short"]["bid"] == 1.50
    assert stored["signal"]["long"]["ask"] == 0.80


async def test_the_desk_reads_a_mark_carrying_the_crossing_debit(db, audit):
    """`tools/desk.py` pulls named keys off the payload with `.get()`, so an extra
    one is inert. Asserted rather than assumed, because `watchdog.check` is the
    highest-cadence category in the system and the desk view is the instrument the
    trading day is read off at the close."""
    from argus.tools.desk import load

    candidate = "20260915-put-5000-4995"
    audit._clock = lambda: "2026-09-15T18:30:00.000000Z"
    await audit.record(
        "watchdog.check",
        {
            "candidate_id": candidate,
            "external_identifier": f"argus-open-{candidate}",
            "contracts": 2,
            "entry_credit": 0.55,
            "problems": [],
            "sustaining": {},
            "true_reasons": [],
            **_evaluation(credit_stop=1.10).to_payload(),
        },
    )

    day = load(db, date(2026, 9, 15))
    mark = day.marks[candidate]
    assert mark.cost_to_close == pytest.approx(0.80)
    assert mark.credit_stop == pytest.approx(1.10)


# -- the structural halves ----------------------------------------------------


def _functions(path: pathlib.Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text())
    return {
        n.name: n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _is_finiteness_wrapper(node: ast.AST) -> bool:
    """A function whose whole body is `return <x> if <finite(...)> else None`."""
    body = [s for s in node.body if not isinstance(s, ast.Expr)]  # drop the docstring
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    value = body[0].value
    if not isinstance(value, ast.IfExp):
        return False
    if not (isinstance(value.orelse, ast.Constant) and value.orelse.value is None):
        return False
    names = {
        n.attr if isinstance(n, ast.Attribute) else n.id
        for n in ast.walk(value.test)
        if isinstance(n, (ast.Name, ast.Attribute))
    }
    return bool(names & {"is_finite", "isfinite", "has_delta"})


def test_the_payload_guard_has_exactly_one_definition():
    """`zerobid`'s own rule, applied to the guard rather than to the floor: *"Two
    implementations of the same safety rule is exactly how one of them quietly
    drifts, so there is one."* That module exists because this exact mistake was
    anticipated for the tick floor; a fourth spelling of the payload guard while
    citing it for the floor would be hard to defend.

    Scoped to the three files that BUILD these payloads. `signal/regime.py` is
    deliberately out of scope and its `_variance` / `_forward_price` are not
    violations: their `None` means "could not estimate", not "cannot be encoded",
    and no AST shape can tell those two apart. `risk/gate.py::_num` is out for a
    stronger reason — it stringifies rather than nulls, on purpose.
    """
    found = []
    for rel in ("signal/models.py", "signal/strategy.py", "execution/watchdog.py"):
        for name, node in _functions(_SRC / rel).items():
            if _is_finiteness_wrapper(node):
                found.append(f"{rel}::{name}")
    assert found == ["signal/models.py::payload_number"], (
        f"a second spelling of the payload guard appeared: {found}"
    )


def test_nothing_in_src_reads_the_crossing_debit():
    """`reject_reason`'s precedent, for the same reason: *permanent-vs-transient
    from a recorded number is the tempting wrong fix.* Every field this change adds
    is written and never consulted — no comparison, no branch, no threshold — which
    is what makes "no behaviour change" a structural claim rather than a review
    opinion.

    The sanctioned readers are the property's own definition site plus
    `_cross_limit`, which acted on `crossing_debit` long before it was ever
    recorded and is the one place it decides anything — and ONE qualified site,
    `ExitExecutor.close`, which records it on `watchdog.exit_started` (restored
    2026-09-18: the join that replaced it is not exact past the trigger's rising
    edge). Qualified by path, so another method named `close` is still refused.
    """
    allowed = {"crossing_debit", "to_payload", "_cross_limit"}
    allowed_sites = {"execution/watchdog.py::close"}
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            reads = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Attribute) and n.attr == "crossing_debit"
            ]
            site = f"{path.relative_to(_SRC)}::{node.name}"
            if reads and node.name not in allowed and site not in allowed_sites:
                offenders.append(site)
    assert not offenders, f"crossing_debit is decided on in {offenders}"


def test_the_new_entry_fields_are_written_and_never_read():
    """The same claim for the entry side, which cannot be made the same way: `bid`
    and `ask` are read all over `src/` — that is what pricing IS. So the assertion
    is narrowed to what is actually new: the payload KEYS. Nothing may look them
    up, because a `payload["short"]["bid"]` anywhere in the runtime would be the
    record feeding a decision back into the system it records.
    """
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            key = node.slice
            if isinstance(key, ast.Constant) and key.value in {
                "short_bid", "short_ask", "long_bid", "long_ask",
            }:
                offenders.append(str(path.relative_to(_SRC)))
    assert not offenders, f"a near-miss price key is read back in {offenders}"


def test_the_mid_selection_telemetry_is_written_and_never_read():
    """The mid selector's record (2026-09-21) is an instrument, like the quoted
    spread: nothing in `src/` may look its keys up, or the record of a selection
    would be feeding the next selection. `tools/` is out of scope — reading the
    record back is what a tool is for."""
    keys = {
        "mid_delta", "mid_iv", "short_mid_delta", "short_mid_iv", "long_mid_delta",
        "alt_pick_strike", "delta_source", "forward_source",
    }
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        if "tools" in path.relative_to(_SRC).parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                if node.slice.value in keys:
                    offenders.append(str(path.relative_to(_SRC)))
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value in keys
            ):
                offenders.append(str(path.relative_to(_SRC)))
    assert not offenders, f"a mid-selection record key is read back in {offenders}"


def test_the_helper_is_reached_by_import_not_by_copy():
    """`strategy.py`'s `_num` was one of the three spellings and is gone. If it
    comes back as a local definition the test above catches the shape; this catches
    the import going away, which is the change that would precede it."""
    tree = ast.parse((_SRC / "signal" / "strategy.py").read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "argus.signal.models"
        for alias in node.names
    }
    assert "payload_number" in imported
    assert inspect.getmodule(payload_number).__name__ == "argus.signal.models"
