"""Shadow outcome marking: settlement math, and the deduplication that makes it
mean anything.

The dedup tests matter as much as the math. A 5s cadence re-proposes the same
spread hundreds of times a day (one real session: 228 `shadow.would_submit`
records for what was a single trade idea), so scoring raw proposals would report
sample size that does not exist.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from argus.risk.models import EntryProposal
from argus.shadow.outcome import (
    RESULT_FULL_CREDIT,
    RESULT_MAX_LOSS,
    RESULT_PARTIAL,
    Proposal,
    mark,
    markable_payload,
    rerank,
    settlement_liability,
    trade_ideas,
)
from argus.signal.models import OptionQuote, SpreadSignal

# A 5-wide bull put: short 7450 / long 7445, 0.80 credit.
PUT_SPREAD = dict(side="put", short_strike=7450.0, long_strike=7445.0)
# A 5-wide bear call: short 7500 / long 7505.
CALL_SPREAD = dict(side="call", short_strike=7500.0, long_strike=7505.0)


def _proposal(seq, *, hhmm="14:30", credit=0.80, contracts=2, day=21, **spread):
    spread = {**PUT_SPREAD, **spread}
    hh, mm = hhmm.split(":")
    return Proposal(
        seq=seq,
        # 14:30 UTC == 10:30 ET in July.
        at_utc=datetime(2026, 7, day, int(hh), int(mm), tzinfo=timezone.utc),
        width=abs(spread["long_strike"] - spread["short_strike"]),
        credit=credit,
        contracts=contracts,
        **spread,
    )


# -- settlement math -------------------------------------------------------


@pytest.mark.parametrize(
    "settlement,expected",
    [
        (7480.0, 0.0),    # well above the short put — expires worthless
        (7450.0, 0.0),    # exactly AT the short strike — still no intrinsic
        (7448.5, 1.5),    # between the strikes
        (7445.0, 5.0),    # exactly at the long strike — fully capped
        (7400.0, 5.0),    # far through — defined risk holds
    ],
)
def test_put_spread_liability(settlement, expected):
    assert settlement_liability(**PUT_SPREAD, settlement=settlement) == expected


@pytest.mark.parametrize(
    "settlement,expected",
    [
        (7480.0, 0.0),
        (7500.0, 0.0),
        (7502.25, 2.25),
        (7505.0, 5.0),
        (7600.0, 5.0),
    ],
)
def test_call_spread_liability(settlement, expected):
    assert settlement_liability(**CALL_SPREAD, settlement=settlement) == expected


def test_unknown_side_is_rejected():
    with pytest.raises(ValueError):
        settlement_liability("straddle", 7450.0, 7445.0, 7480.0)


def test_full_credit_outcome():
    idea = trade_ideas([_proposal(1)])[0]
    outcome = mark(idea, 7480.0)
    assert outcome.result == RESULT_FULL_CREDIT
    assert outcome.win is True
    assert outcome.breached is False
    assert outcome.pnl_per_contract == 80.0     # 0.80 credit * $100
    assert outcome.pnl == 160.0                 # 2 contracts


def test_max_loss_outcome_is_bounded_by_the_width():
    idea = trade_ideas([_proposal(1)])[0]
    outcome = mark(idea, 7000.0)  # blown far through both strikes
    assert outcome.result == RESULT_MAX_LOSS
    assert outcome.win is False
    # (0.80 credit - 5.00 width) * 100 = -420 per contract. Defined risk: a
    # settlement 450 points through the strikes costs no more than one 5 wide.
    assert outcome.pnl_per_contract == pytest.approx(-420.0)
    assert outcome.pnl == pytest.approx(-840.0)


def test_partial_outcome_between_the_strikes():
    idea = trade_ideas([_proposal(1)])[0]
    outcome = mark(idea, 7448.5)
    assert outcome.result == RESULT_PARTIAL
    assert outcome.breached is True
    assert outcome.pnl_per_contract == pytest.approx((0.80 - 1.5) * 100)
    assert outcome.win is False


def test_a_breach_can_still_be_a_win():
    # Settling just inside the short strike loses less than the credit taken.
    idea = trade_ideas([_proposal(1, credit=2.0)])[0]
    outcome = mark(idea, 7449.0)  # 1.0 liability against 2.0 credit
    assert outcome.breached is True
    assert outcome.win is True
    assert outcome.result == RESULT_PARTIAL


# -- deduplication ---------------------------------------------------------


def test_repeated_proposals_collapse_to_one_idea():
    # The bug this exists to prevent: 228 records != 228 samples.
    proposals = [_proposal(seq) for seq in range(1, 229)]
    ideas = trade_ideas(proposals)
    assert len(ideas) == 1
    assert ideas[0].proposals == 228


def test_first_credit_is_what_was_collectable():
    proposals = [
        _proposal(1, credit=0.80, hhmm="14:00"),
        _proposal(2, credit=1.40, hhmm="14:30"),
        _proposal(3, credit=0.60, hhmm="15:00"),
    ]
    idea = trade_ideas(proposals)[0]
    # Scored on the first credit — you cannot retroactively take the best one.
    assert idea.credit == 0.80
    # But the range is kept, so entry timing is measurable.
    assert (idea.best_credit, idea.worst_credit) == (1.40, 0.60)
    assert idea.first_seen_et.strftime("%H:%M") == "10:00"  # ET, not UTC
    assert idea.last_seen_et.strftime("%H:%M") == "11:00"


def test_different_strikes_are_different_ideas():
    ideas = trade_ideas([
        _proposal(1),
        _proposal(2, short_strike=7440.0, long_strike=7435.0),
    ])
    assert len(ideas) == 2
    assert {i.short_strike for i in ideas} == {7450.0, 7440.0}


def test_put_and_call_sides_are_separate_ideas_each_ranked_first():
    ideas = trade_ideas([_proposal(1), _proposal(2, **CALL_SPREAD)])
    assert len(ideas) == 2
    # Both are the first idea on their own side — a real account could hold both.
    assert {i.entry_rank for i in ideas} == {1}


def test_entry_rank_orders_ideas_within_a_side():
    ideas = trade_ideas([
        _proposal(1),
        _proposal(2, short_strike=7440.0, long_strike=7435.0),
        _proposal(3, short_strike=7430.0, long_strike=7425.0),
    ])
    ranks = {i.short_strike: i.entry_rank for i in ideas}
    assert ranks == {7450.0: 1, 7440.0: 2, 7430.0: 3}


def test_same_spread_on_different_days_is_not_one_idea():
    ideas = trade_ideas([_proposal(1, day=20), _proposal(2, day=21)])
    assert len(ideas) == 2
    assert {i.trading_date for i in ideas} == {date(2026, 7, 20), date(2026, 7, 21)}
    assert {i.entry_rank for i in ideas} == {1}  # rank resets each day


def test_out_of_order_seqs_still_take_the_earliest_as_first():
    ideas = trade_ideas([_proposal(9, credit=0.60), _proposal(2, credit=0.90)])
    assert ideas[0].credit == 0.90  # seq 2 came first
    assert ideas[0].first_seq == 2


def test_payload_is_json_safe_and_carries_the_diagnostics():
    import json

    idea = trade_ideas([_proposal(1, credit=0.80), _proposal(2, credit=1.10)])[0]
    payload = mark(idea, 7480.0).to_payload()
    json.dumps(payload)  # must survive the audit log's canonical encoding
    assert payload["trading_date"] == "2026-07-21"
    assert payload["proposals"] == 2
    assert payload["result"] == RESULT_FULL_CREDIT
    assert payload["entry_rank"] == 1


# -- the marking block inherits the signal's shape (2026-09-15) ---------------


def _spread_signal():
    return SpreadSignal(
        underlying="SPXW",
        side="put",
        short_leg=OptionQuote("SPXW 7450", 7450.0, "put", -0.15, 1.00, 1.20),
        long_leg=OptionQuote("SPXW 7445", 7445.0, "put", -0.10, 0.30, 0.50),
        credit=0.80,
        width=5.0,
        captured_at=0.0,
    )


def test_the_marking_block_carries_the_quoted_spread_for_free():
    """`markable_payload` embeds `signal.to_payload()` WHOLE rather than copying
    named keys out of it, so both terminal records — `shadow.would_submit` and
    `entry.dry_run` — inherit a new leg field with no writer being edited.

    That is desirable and it is exactly why the helper is a FUNCTION: two writers
    spelling the shape out by hand is how one of them drifts by a key name and
    produces records that decode to nothing, which is what happened to
    `entry.dry_run` and stopped the signal-quality series dead in 2026-08-11.
    """
    payload = markable_payload(
        _spread_signal(), EntryProposal("SPXW put credit spread 7450/7445", 2, 840.0)
    )
    assert (payload["signal"]["short"]["bid"], payload["signal"]["short"]["ask"]) == (1.00, 1.20)
    assert (payload["signal"]["long"]["bid"], payload["signal"]["long"]["ask"]) == (0.30, 0.50)
    # And the keys the marker actually scores on are untouched by the addition.
    assert payload["signal"]["credit"] == 0.80
    assert payload["proposal"]["contracts"] == 2


def test_the_marking_block_is_json_safe_with_a_hostile_quote():
    """Both writers are Tier 1, and a refused Tier 1 write raises into the caller
    — `shadow.would_submit`'s raise is swallowed by the signal loop's bad-iteration
    handler, which is precisely how a telemetry field becomes a silent trading
    outage. See `tests/test_payload_safety.py` for the full argument."""
    import json

    hostile = replace(
        _spread_signal(),
        short_leg=OptionQuote("SPXW 7450", 7450.0, "put", -0.15, float("nan"), 1.20),
    )
    payload = markable_payload(hostile, EntryProposal("x", 2, 840.0))
    json.dumps(payload, allow_nan=False)
    assert payload["signal"]["short"]["bid"] is None


# -- ranking across a merged set (2026-08-13) ---------------------------------


def test_rerank_is_a_no_op_on_trade_ideas_output():
    """`trade_ideas` already ranks in `first_seq` order, so `rerank` can be applied
    unconditionally after merging without a caller having to know whether the set
    it holds came from one reader or two."""
    proposals = [
        _proposal(1, short_strike=7450.0, long_strike=7445.0),
        _proposal(2, short_strike=7460.0, long_strike=7455.0),
        _proposal(3, short_strike=7470.0, long_strike=7465.0, side="call"),
    ]
    ideas = trade_ideas(proposals)
    assert rerank(ideas) == ideas


def test_rerank_counts_per_side_in_chain_order():
    """Rank means "the Nth idea of the day on this side", which is a property of
    the whole day — so an idea arriving from a second reader cannot be handed a
    rank by whichever reader produced it."""
    ideas = trade_ideas([
        _proposal(10, short_strike=7450.0, long_strike=7445.0),
        _proposal(20, short_strike=7460.0, long_strike=7455.0),
        _proposal(30, short_strike=7470.0, long_strike=7465.0, side="call"),
    ])
    # A fourth idea discovered later, but earlier in the chain than all of them.
    late = replace(ideas[0], short_strike=7400.0, long_strike=7395.0, first_seq=5)

    ranked = {(i.side, i.short_strike): i.entry_rank for i in rerank([*ideas, late])}
    assert ranked[("put", 7400.0)] == 1     # lowest seq on the put side
    assert ranked[("put", 7450.0)] == 2
    assert ranked[("put", 7460.0)] == 3
    assert ranked[("call", 7470.0)] == 1    # sides are counted independently
