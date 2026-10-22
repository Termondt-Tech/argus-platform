"""Scoring a day on which orders actually filled (2026-08-13, Fix F).

Settlement scoring answers "what would this have been worth held to expiry",
which is the only question shadow and dry-run can ask. Live fire adds a second
shape — a position the watchdog closes intraday has a REALIZED result the 16:00
print says nothing about — and these tests fix both, plus the day that holds
both at once.

The fill payloads below are built BY HAND, matching what `FillConsumer` emits
(`AppliedFill.to_payload()` plus `status` and a NULL `candidate_id`), for the same
reason `test_shadow_marker.py` hand-builds its marking blocks: a fixture built by
the production writer cannot catch the production writer changing shape.

The numbers are the real ones. `20260813-call-7825-7830`, 2 contracts, opened at
0.55 credit and closed at 1.20 debit at 10:21 ET — realized -$130.
"""

from __future__ import annotations

from datetime import date

import pytest

from argus.shadow.marker import (
    mark_trading_date,
    pending_scores,
    read_executions,
    read_fills,
)
from argus.shadow.outcome import (
    SHAPE_CLOSED_INTRADAY,
    SHAPE_HELD_TO_EXPIRY,
    SHAPE_PARTLY_CLOSED,
    SHAPE_UNFILLED,
    SHAPE_UNPRICEABLE,
    mark,
)
from argus.signal.models import OptionQuote, SpreadSignal

TRADING_DATE = date(2026, 8, 13)

# 14:00 UTC == 10:00 ET. The live-fire round trip opened at 14:00 and the exit
# filled at 14:21.
OPENED_AT = "2026-08-13T14:00:08.900000Z"
CLOSED_AT = "2026-08-13T14:21:58.500000Z"


def _signal(short_strike=7825.0, long_strike=7830.0, credit=0.55, side="call"):
    return SpreadSignal(
        underlying="SPXW",
        side=side,
        short_leg=OptionQuote(
            symbol=f"SPXW {short_strike:g}", strike=short_strike, right=side,
            delta=0.15, bid=0.90, ask=1.00,
        ),
        long_leg=OptionQuote(
            symbol=f"SPXW {long_strike:g}", strike=long_strike, right=side,
            delta=0.10, bid=0.35, ask=0.45,
        ),
        credit=credit,
        width=abs(short_strike - long_strike),
        captured_at=0.0,
    )


def _candidate(signal):
    return (
        f"20260813-{signal.side}-{signal.short_leg.strike:g}-"
        f"{signal.long_leg.strike:g}"
    )


async def _finished(audit, signal, contracts=2, ts=OPENED_AT, outcome="filled"):
    """Exactly what `EntryExecutor._report` writes: the walk report PLUS the
    marking block, which is the whole of Fix F's writer half."""
    audit._clock = lambda: ts
    await audit.record(
        "entry.finished",
        {
            "candidate_id": _candidate(signal),
            "requested_contracts": contracts,
            "max_loss": contracts * signal.max_loss_per_contract,
            "outcome": outcome,
            "external_identifier": f"argus-open-{_candidate(signal)}",
            "broker_order_id": 1311951,
            "steps": 1,
            "filled_contracts": contracts if outcome == "filled" else 0,
            "final_limit_price": signal.credit,
            "cancel_requested": False,
            "recovered_disconnects": 0,
            "reason": None,
            "last_status": "Filled",
            "proposal": {
                "symbol": signal.describe(),
                "contracts": contracts,
                "max_loss": contracts * signal.max_loss_per_contract,
            },
            "signal": signal.to_payload(),
        },
    )


async def _fill(audit, signal, *, direction, action, leg, contracts, price, ts):
    """One `order.fill` record — one leg's movement.

    `candidate_id` is NULL exactly as it is in production: it does not round-trip
    through the broker, so the attribution has to come from `external_identifier`.
    """
    strike = signal.short_leg.strike if leg == "short" else signal.long_leg.strike
    audit._clock = lambda: ts
    await audit.record(
        "order.fill",
        {
            "broker_order_id": 1311951 if direction == "open" else 1311952,
            "external_identifier": f"argus-{direction}-{_candidate(signal)}",
            "symbol": f"SPXW  260813C0{strike:g}000",
            "action": action,
            "contracts": contracts,
            "signed_delta": contracts if action.startswith("Buy") else -contracts,
            "price": price,
            "position_before": 0,
            "position_after": 0,
            "status": "Filled",
            "candidate_id": None,
        },
    )


async def _open_fills(audit, signal, contracts=2, credit=None, ts=OPENED_AT):
    """The two legs of an opening fill. Net = short premium - long premium."""
    credit = signal.credit if credit is None else credit
    long_price = 0.40
    await _fill(audit, signal, direction="open", action="Sell to Open", leg="short",
                contracts=contracts, price=credit + long_price, ts=ts)
    await _fill(audit, signal, direction="open", action="Buy to Open", leg="long",
                contracts=contracts, price=long_price, ts=ts)


async def _close_fills(audit, signal, contracts=2, debit=1.20, ts=CLOSED_AT):
    """The two legs of a closing fill. Net = buy-back cost - long leg sold."""
    long_price = 0.30
    await _fill(audit, signal, direction="close", action="Buy to Close", leg="short",
                contracts=contracts, price=debit + long_price, ts=ts)
    await _fill(audit, signal, direction="close", action="Sell to Close", leg="long",
                contracts=contracts, price=long_price, ts=ts)


# -- shape 1: closed intraday, scored from the fills --------------------------


async def test_closed_intraday_scores_realized_pnl_from_fill_prices(db, audit):
    """The 2026-08-13 round trip, to the dollar.

    0.55 credit in, 1.20 debit out, 2 contracts. Settlement is set to 7800 — a
    full-credit finish for a 7825/7830 call spread — precisely so that scoring
    this at settlement would report a WIN on a trade that lost $130.
    """
    signal = _signal()
    await _finished(audit, signal)
    await _open_fills(audit, signal)
    await _close_fills(audit, signal)

    [outcome] = [
        mark(idea, 7800.0, ex) for idea, ex in pending_scores(db, TRADING_DATE)
    ]
    assert outcome.shape == SHAPE_CLOSED_INTRADAY
    assert outcome.pnl == pytest.approx(-130.0)
    assert outcome.win is False

    # The settlement term is exactly zero — no contracts were held to 16:00.
    assert outcome.realized_pnl == pytest.approx(-130.0)
    assert outcome.expiry_pnl == 0.0

    # And the counterfactual, which is the number the virtual stop gets tuned on:
    # held to that settlement the same position would have kept the full credit.
    assert outcome.liability == 0.0
    assert outcome.counterfactual_pnl == pytest.approx(110.0)


async def test_the_fill_price_governs_not_the_order_limit(db, audit):
    """The exit filled at 1.20 net against a -1.30 limit. Scoring the limit would
    have overstated the loss by $20 on 2 contracts — so the limit must not appear
    anywhere in the arithmetic, and nothing here reads the `orders` table."""
    signal = _signal()
    await _finished(audit, signal)
    await _open_fills(audit, signal)
    await _close_fills(audit, signal, debit=1.20)

    execution = read_executions(db, TRADING_DATE)[_candidate(signal)]
    assert execution.entry_credit == 0.55
    assert execution.exit_debit == pytest.approx(1.20)


# -- shape 2: held to expiry, scored at settlement ----------------------------


async def test_held_to_expiry_scores_at_settlement_on_the_FILL_credit(db, audit):
    """No closing fills exist — 0DTE expiry generates no fill event — so this is
    the case the settlement math already handled. What changes is the basis: the
    credit the position actually opened at, not the credit it was proposed at."""
    signal = _signal(credit=0.55)
    await _finished(audit, signal)
    # Proposed at 0.55; the walk repriced and it actually filled at 0.65.
    await _open_fills(audit, signal, credit=0.65)

    [outcome] = [
        mark(idea, 7800.0, ex) for idea, ex in pending_scores(db, TRADING_DATE)
    ]
    assert outcome.shape == SHAPE_HELD_TO_EXPIRY
    assert outcome.credit_basis == 0.65      # the fill, not the proposal
    assert outcome.idea.credit == 0.55       # the proposal is still recorded
    assert outcome.realized_pnl == 0.0
    assert outcome.pnl == 130.0              # 0.65 x 2 x 100, full credit
    assert outcome.win is True


async def test_held_to_expiry_through_the_short_strike(db, audit):
    """Settlement between the strikes: a partial. The realized term stays zero and
    the liability does the work, exactly as it always has."""
    signal = _signal(credit=0.55)
    await _finished(audit, signal)
    await _open_fills(audit, signal, credit=0.55)

    [outcome] = [
        mark(idea, 7827.0, ex) for idea, ex in pending_scores(db, TRADING_DATE)
    ]
    assert outcome.shape == SHAPE_HELD_TO_EXPIRY
    assert outcome.liability == 2.0          # 7827 - 7825, inside the 5-wide
    assert outcome.pnl == (0.55 - 2.0) * 2 * 100
    assert outcome.breached is True


# -- shape 3: one day holding BOTH --------------------------------------------


async def test_a_day_with_one_closed_and_one_expired_position(db, audit):
    """The 2026-08-13 shape: a stopped-out spread, then a replacement that ran to
    the close. Two ideas, two different scoring rules, one command and one total."""
    stopped = _signal(short_strike=7825.0, long_strike=7830.0, credit=0.55)
    held = _signal(short_strike=7835.0, long_strike=7840.0, credit=0.60)

    await _finished(audit, stopped)
    await _open_fills(audit, stopped, credit=0.55)
    await _close_fills(audit, stopped, debit=1.20)

    await _finished(audit, held, ts="2026-08-13T14:22:09.500000Z")
    await _open_fills(audit, held, credit=0.60, ts="2026-08-13T14:22:09.500000Z")

    outcomes = [
        mark(idea, 7800.0, ex) for idea, ex in pending_scores(db, TRADING_DATE)
    ]
    by_strike = {o.idea.short_strike: o for o in outcomes}
    assert by_strike[7825.0].shape == SHAPE_CLOSED_INTRADAY
    assert by_strike[7825.0].pnl == pytest.approx(-130.0)
    assert by_strike[7835.0].shape == SHAPE_HELD_TO_EXPIRY
    assert by_strike[7835.0].pnl == pytest.approx(120.0)
    assert sum(o.pnl for o in outcomes) == pytest.approx(-10.0)

    # Both are the FIRST idea on the call side in chain order... which they are
    # not: rank is assigned across the merged set, so they are 1 and 2.
    assert sorted(o.idea.entry_rank for o in outcomes) == [1, 2]


async def test_a_day_mixing_a_filled_position_and_an_unfilled_proposal(db, audit):
    """A walk that walked away still writes `entry.finished`, and that record is
    still a proposal worth scoring against settlement — the counterfactual series
    does not stop just because the day also traded."""
    traded = _signal(short_strike=7825.0, long_strike=7830.0, credit=0.55)
    declined = _signal(short_strike=7845.0, long_strike=7850.0, credit=0.40)

    await _finished(audit, traded)
    await _open_fills(audit, traded)
    await _finished(audit, declined, outcome="walked_away",
                    ts="2026-08-13T15:00:00.000000Z")

    outcomes = [
        mark(idea, 7800.0, ex) for idea, ex in pending_scores(db, TRADING_DATE)
    ]
    shapes = {o.idea.short_strike: o.shape for o in outcomes}
    assert shapes == {7825.0: SHAPE_HELD_TO_EXPIRY, 7845.0: SHAPE_UNFILLED}

    unfilled = next(o for o in outcomes if o.shape == SHAPE_UNFILLED)
    # Unchanged pre-Fix-F behaviour: proposed credit, proposed size, settlement.
    assert unfilled.credit_basis == 0.40
    assert unfilled.pnl == 0.40 * 2 * 100
    assert unfilled.execution is None


# -- the partial close, which needs no special case ---------------------------


async def test_a_partial_close_scores_both_terms(db, audit):
    """Half bought back, half expired. Realized on what closed, settlement on what
    did not — the same two-term formula every other shape is a corner of."""
    signal = _signal(credit=0.55)
    await _finished(audit, signal, contracts=4)
    await _open_fills(audit, signal, contracts=4, credit=0.55)
    await _close_fills(audit, signal, contracts=1, debit=1.20)

    [outcome] = [
        mark(idea, 7800.0, ex) for idea, ex in pending_scores(db, TRADING_DATE)
    ]
    assert outcome.shape == SHAPE_PARTLY_CLOSED
    assert outcome.execution.closed_contracts == 1
    assert outcome.execution.expired_contracts == 3
    assert outcome.realized_pnl == pytest.approx(-65.0)   # (0.55 - 1.20) x 1 x 100
    assert outcome.expiry_pnl == pytest.approx(165.0)     # 0.55 x 3 x 100, full credit
    assert outcome.pnl == pytest.approx(100.0)


# -- a real position is never silently unscored -------------------------------


async def test_fills_alone_are_enough_to_score_a_position(db, audit):
    """THE 2026-08-13 BACKFILL QUESTION, answered by the normal path.

    That day's live entries wrote `entry.finished` before it carried a markable
    block, so there is no proposal record to decode. There are still fills, and
    `candidate_id` IS (date, side, strikes) by construction — so the position
    scores anyway, with no second decoder and no one-off backfill script.
    """
    signal = _signal()
    await _open_fills(audit, signal, credit=0.55)
    await _close_fills(audit, signal, debit=1.20)

    [(idea, execution)] = pending_scores(db, TRADING_DATE)
    assert (idea.side, idea.short_strike, idea.long_strike) == ("call", 7825.0, 7830.0)
    assert idea.width == 5.0
    assert idea.contracts == 2
    assert idea.credit == 0.55
    # Honest about what was genuinely lost with the proposal record: the
    # re-proposal history, and nothing else.
    assert idea.proposals == 0
    assert idea.first_seq == 1   # anchored on the first fill, a real chain record

    assert mark(idea, 7800.0, execution).pnl == pytest.approx(-130.0)


async def test_a_synthesized_idea_does_not_duplicate_a_real_one(db, audit):
    """When both exist they are ONE idea. The join is the candidate key, which the
    proposal side derives from (date, side, strikes) and the fill side reads out
    of the identifier — the same string either way."""
    signal = _signal()
    await _finished(audit, signal)
    await _open_fills(audit, signal)

    scores = pending_scores(db, TRADING_DATE)
    assert len(scores) == 1
    idea, execution = scores[0]
    assert idea.proposals == 1          # came from the proposal record, not the fills
    assert execution is not None


async def test_a_position_whose_fills_carry_no_price_is_unpriceable_not_zero(db, audit):
    """`LegFill` is documented as best-effort — it prices a fill, it does not count
    one. With no price at all the honest answer is that the cost is unknown; a 0.00
    or an order-limit substitute would put a fabricated number in a summed column."""
    signal = _signal()
    await _finished(audit, signal)
    await _fill(audit, signal, direction="open", action="Sell to Open", leg="short",
                contracts=2, price=None, ts=OPENED_AT)
    await _fill(audit, signal, direction="open", action="Buy to Open", leg="long",
                contracts=2, price=None, ts=OPENED_AT)

    [outcome] = [
        mark(idea, 7800.0, ex) for idea, ex in pending_scores(db, TRADING_DATE)
    ]
    assert outcome.shape == SHAPE_UNPRICEABLE
    assert outcome.pnl is None
    assert outcome.win is None
    assert outcome.execution.opened_contracts == 2   # the position is still recorded


# -- attribution integrity ----------------------------------------------------


async def test_an_exits_fills_under_an_entry_identifier_are_refused(db, audit):
    """The corruption the pre-2026-08-25 open/close check was reaching for: an
    exit's two legs carrying the OPEN identifier. Netting them as an entry would
    book a debit paid as a credit collected and flip the sign of a whole trade.

    Still refused — but on the CASH DIRECTION of each leg, which the broker does
    not relabel, rather than on the action suffix, which it does. And refused as a
    recorded problem rather than by deleting the records."""
    signal = _signal()
    await _finished(audit, signal)
    await _fill(audit, signal, direction="open", action="Buy to Close", leg="short",
                contracts=2, price=1.50, ts=CLOSED_AT)
    await _fill(audit, signal, direction="open", action="Sell to Close", leg="long",
                contracts=2, price=0.30, ts=CLOSED_AT)

    execution = read_executions(db, TRADING_DATE)[_candidate(signal)]
    assert execution.problems == (
        "the short leg of the open moved premium the wrong way (SPXW  260813C07825000)",
        "the long leg of the open moved premium the wrong way (SPXW  260813C07830000)",
    )
    assert not execution.priced

    # And it reaches the report as UNPRICEABLE rather than as a scored entry at a
    # NEGATIVE credit, which is what netting these as an open would have produced.
    [outcome] = [
        mark(idea, 7800.0, ex) for idea, ex in pending_scores(db, TRADING_DATE)
    ]
    assert outcome.shape == SHAPE_UNPRICEABLE
    assert outcome.pnl is None


async def test_a_relabelled_opening_buy_is_kept_and_priced(db, audit):
    """THE 2026-08-25 REGRESSION. Two chained verticals share a strike, so the
    second spread's opening BUY lands on a strike the first is already short and
    tastytrade echoes it back as `Buy to Close` on an `open` order.

    Real numbers, from audit seq 420583/420587: 7690/7695 calls, sell 7690 at
    1.70, buy 7695 at 1.20, net credit 0.50 — which is exactly the order's limit.
    The old check dropped the second leg, `_net_legs` then saw one leg where it
    needed two, and a filled position was reported as never taken."""
    signal = _signal(short_strike=7690.0, long_strike=7695.0, credit=0.50)
    await _finished(audit, signal)
    await _fill(audit, signal, direction="open", action="Sell to Open", leg="short",
                contracts=2, price=1.70, ts=OPENED_AT)
    await _fill(audit, signal, direction="open", action="Buy to Close", leg="long",
                contracts=2, price=1.20, ts=OPENED_AT)

    execution = read_executions(db, TRADING_DATE)[_candidate(signal)]
    assert execution.problems == ()
    assert execution.opened_contracts == 2
    assert execution.entry_credit == pytest.approx(0.50)

    # And it scores as what it was: a spread held to a full-credit expiry.
    [outcome] = [
        mark(idea, 7674.375, ex) for idea, ex in pending_scores(db, TRADING_DATE)
    ]
    assert outcome.shape == SHAPE_HELD_TO_EXPIRY
    assert outcome.pnl == pytest.approx(100.0)


async def test_two_chained_verticals_sharing_a_strike_both_score(db, audit):
    """THE 2026-08-25 DAY, end to end. 7695/7700 opened at 10:19, then 7690/7695 at
    10:23 — the second spread's LONG leg is the first spread's SHORT leg, which is
    the common case at `signal.width = 5.0` on a 5-point ladder, not a tail.

    The shared strike nets to zero in `positions` and at the broker, so the account
    holds one 7690/7700 10-wide. Attribution is per TRADE IDEA and must be
    unaffected by that: two ideas, both filled, $210 between them.

    Reported on the day as ONE filled idea worth $110."""
    first = _signal(short_strike=7695.0, long_strike=7700.0, credit=0.55)
    second = _signal(short_strike=7690.0, long_strike=7695.0, credit=0.50)
    await _finished(audit, first)
    await _finished(audit, second)
    # seq 420205 / 420208 — a clean pair, nothing yet held on either strike.
    await _fill(audit, first, direction="open", action="Sell to Open", leg="short",
                contracts=2, price=1.95, ts=OPENED_AT)
    await _fill(audit, first, direction="open", action="Buy to Open", leg="long",
                contracts=2, price=1.40, ts=OPENED_AT)
    # seq 420583 / 420587 — the 7695 buy lands on a strike already short, so the
    # broker returns `Buy to Close` and `position_after` is 0.
    await _fill(audit, second, direction="open", action="Sell to Open", leg="short",
                contracts=2, price=1.70, ts=OPENED_AT)
    await _fill(audit, second, direction="open", action="Buy to Close", leg="long",
                contracts=2, price=1.20, ts=OPENED_AT)

    found = read_executions(db, TRADING_DATE)
    assert not any(ex.problems for ex in found.values())
    assert found[_candidate(first)].entry_credit == pytest.approx(0.55)
    assert found[_candidate(second)].entry_credit == pytest.approx(0.50)

    outcomes = [
        mark(idea, 7674.375, ex) for idea, ex in pending_scores(db, TRADING_DATE)
    ]
    assert len(outcomes) == 2
    assert all(o.filled and o.shape == SHAPE_HELD_TO_EXPIRY for o in outcomes)
    assert sum(o.pnl for o in outcomes) == pytest.approx(210.0)


async def test_a_relabelled_closing_sell_is_kept_and_priced(db, audit):
    """The exit mirror of the same relabelling: selling to close a long leg the
    account is net FLAT on comes back `Sell to Open` on a `close` order. Refusing
    it would score a stopped-out spread as held to expiry — a loss recorded as a
    full-credit win, biased in one direction, in the dataset `credit_multiple` is
    tuned against."""
    signal = _signal()
    await _finished(audit, signal)
    await _open_fills(audit, signal)
    await _fill(audit, signal, direction="close", action="Buy to Close", leg="short",
                contracts=2, price=1.50, ts=CLOSED_AT)
    await _fill(audit, signal, direction="close", action="Sell to Open", leg="long",
                contracts=2, price=0.30, ts=CLOSED_AT)

    execution = read_executions(db, TRADING_DATE)[_candidate(signal)]
    assert execution.problems == ()
    assert execution.closed_contracts == 2
    assert execution.exit_debit == pytest.approx(1.20)


async def test_a_fill_on_a_strike_that_is_neither_leg_is_a_problem(db, audit):
    """Something the open/close check could not see. On a chained book one strike
    is legitimately a leg of two different spreads, so a fill carrying the wrong
    candidate's identifier is a real way to net a movement onto a position it never
    touched."""
    signal = _signal()
    await _finished(audit, signal)
    await _open_fills(audit, signal)
    audit._clock = lambda: OPENED_AT
    await audit.record(
        "order.fill",
        {
            "broker_order_id": 1311951,
            "external_identifier": f"argus-open-{_candidate(signal)}",
            "symbol": "SPXW  260813C07900000",   # neither 7825 nor 7830
            "action": "Sell to Open", "contracts": 2, "signed_delta": -2,
            "price": 0.95, "position_before": 0, "position_after": -2,
            "status": "Filled", "candidate_id": None,
        },
    )
    execution = read_executions(db, TRADING_DATE)[_candidate(signal)]
    assert "SPXW  260813C07900000 is neither leg of this spread" in execution.problems
    assert not execution.priced


async def test_a_fill_whose_symbol_carries_no_strike_is_a_problem(db, audit):
    """`occ_strike` returning None means "cannot say", so the role cannot be
    established. Recorded, not assumed — and not silently dropped either."""
    signal = _signal()
    await _finished(audit, signal)
    audit._clock = lambda: OPENED_AT
    await audit.record(
        "order.fill",
        {
            "broker_order_id": 1311951,
            "external_identifier": f"argus-open-{_candidate(signal)}",
            "symbol": "not-an-occ-symbol",
            "action": "Sell to Open", "contracts": 2, "signed_delta": -2,
            "price": 0.95, "position_before": 0, "position_after": -2,
            "status": "Filled", "candidate_id": None,
        },
    )
    execution = read_executions(db, TRADING_DATE)[_candidate(signal)]
    assert "not-an-occ-symbol is neither leg of this spread" in execution.problems
    assert not execution.priced


async def test_a_foreign_identifier_is_not_attributed_to_any_idea(db, audit):
    """`fills.py` never writes one, but the decoder must not invent a candidate
    from a string it cannot parse."""
    audit._clock = lambda: OPENED_AT
    await audit.record(
        "order.fill",
        {
            "broker_order_id": 999, "external_identifier": "hand-placed-42",
            "symbol": "SPXW  260813C07825000", "action": "Sell to Open",
            "contracts": 2, "signed_delta": -2, "price": 0.95,
            "position_before": 0, "position_after": -2,
            "status": "Filled", "candidate_id": None,
        },
    )
    assert read_fills(db, TRADING_DATE) == []


async def test_fills_are_bounded_by_the_ET_trading_day(db, audit):
    """Same day boundary as the proposals, on the same indexed column. 00:30 UTC
    on the 14th is still the 13th in ET."""
    signal = _signal()
    await _open_fills(audit, signal, ts="2026-08-14T00:30:00.000000Z")
    assert read_fills(db, date(2026, 8, 14)) == []
    assert len(read_fills(db, TRADING_DATE)) == 2


async def test_a_candidate_from_another_date_is_not_attached_to_today(db, audit):
    """Argus trades 0DTE only, so a candidate never spans a date boundary. One that
    claims to is a broken identifier or a broken clock, not a position to score."""
    audit._clock = lambda: OPENED_AT
    await audit.record(
        "order.fill",
        {
            "broker_order_id": 1, "external_identifier": "argus-open-20260812-call-7825-7830",
            "symbol": "SPXW  260812C07825000", "action": "Sell to Open",
            "contracts": 2, "signed_delta": -2, "price": 0.95,
            "position_before": 0, "position_after": -2,
            "status": "Filled", "candidate_id": None,
        },
    )
    assert read_fills(db, TRADING_DATE) == []


# -- the whole command, end to end --------------------------------------------


async def test_marking_a_live_day_writes_the_realized_number(db, audit):
    """One operator command, no flag saying which mode produced the day, and the
    outcome record carries what actually happened alongside the settlement facts."""
    stopped = _signal(short_strike=7825.0, long_strike=7830.0, credit=0.55)
    held = _signal(short_strike=7835.0, long_strike=7840.0, credit=0.60)
    await _finished(audit, stopped)
    await _open_fills(audit, stopped, credit=0.55)
    await _close_fills(audit, stopped, debit=1.20)
    await _finished(audit, held, ts="2026-08-13T14:22:09.500000Z")
    await _open_fills(audit, held, credit=0.60, ts="2026-08-13T14:22:09.500000Z")

    outcomes = await mark_trading_date(
        db, audit, TRADING_DATE, 7800.0, settlement_source="manual"
    )
    assert len(outcomes) == 2

    payloads = {
        p["short_strike"]: p
        for p in (o.to_payload() for o in outcomes)
    }
    closed = payloads[7825.0]
    assert closed["outcome_shape"] == SHAPE_CLOSED_INTRADAY
    assert closed["pnl"] == pytest.approx(-130.0)
    assert closed["realized_pnl"] == pytest.approx(-130.0)
    assert closed["expiry_pnl"] == 0.0
    assert closed["counterfactual_pnl"] == pytest.approx(110.0)
    assert closed["entry_fill_credit"] == 0.55
    assert closed["exit_fill_debit"] == pytest.approx(1.20)
    assert closed["opened_contracts"] == 2 and closed["closed_contracts"] == 2
    # The settlement facts are still recorded — they are what the counterfactual
    # is built from, and `breached` is a statement about the print, not the trade.
    assert closed["result"] == "full_credit" and closed["breached"] is False

    expired = payloads[7835.0]
    assert expired["outcome_shape"] == SHAPE_HELD_TO_EXPIRY
    assert expired["pnl"] == pytest.approx(120.0)
    assert expired["closed_contracts"] == 0


async def test_an_unfilled_day_writes_byte_identical_fields_to_before(db, audit):
    """The regression that matters: every record written before live fire scored an
    idea that never filled, and those must be untouched. The new keys are all null
    on that path, so an existing reader sees exactly what it saw."""
    signal = _signal(credit=0.55)
    await _finished(audit, signal, outcome="walked_away")

    [outcome] = await mark_trading_date(
        db, audit, TRADING_DATE, 7827.0, settlement_source="manual"
    )
    payload = outcome.to_payload()
    assert payload["credit"] == 0.55
    assert payload["contracts"] == 2
    assert payload["liability"] == 2.0
    assert payload["pnl"] == (0.55 - 2.0) * 2 * 100
    assert payload["pnl_per_contract"] == (0.55 - 2.0) * 100
    assert payload["win"] is False
    assert payload["outcome_shape"] == SHAPE_UNFILLED
    assert payload["realized_pnl"] is None
    assert payload["opened_contracts"] is None
    assert payload["entry_fill_credit"] is None


async def test_closing_fills_with_no_opening_fills_are_not_scored_as_zero(db, audit):
    """There is no position to reconstruct from a close alone, and a zero-contract
    idea would report a confident $0 for records that are plainly incomplete."""
    signal = _signal()
    await _close_fills(audit, signal, debit=1.20)

    assert pending_scores(db, TRADING_DATE) == []


async def test_a_leg_both_bought_and_sold_in_one_direction_is_refused(db, audit):
    """Whatever that is, it is not a leg of a vertical — and netting it would
    quietly cancel two real movements against each other."""
    signal = _signal()
    await _fill(audit, signal, direction="open", action="Sell to Open", leg="short",
                contracts=2, price=0.95, ts=OPENED_AT)
    await _fill(audit, signal, direction="open", action="Buy to Open", leg="short",
                contracts=2, price=0.95, ts=OPENED_AT)
    await _fill(audit, signal, direction="open", action="Buy to Open", leg="long",
                contracts=2, price=0.40, ts=OPENED_AT)

    execution = read_executions(db, TRADING_DATE)[_candidate(signal)]
    assert execution.problems
    assert not execution.priced


async def test_a_malformed_execution_is_unpriceable_not_unfilled(db, audit):
    """A position whose fill records will not assemble must not read as one that
    never opened (2026-08-25).

    `_net_legs` reports zero contracts alongside its problems, so this used to fall
    through `filled` onto UNFILLED — which scores the idea counterfactually at
    settlement, on the PROPOSED size, and says nothing. Here settlement is a full
    credit finish, so the silent answer would have been a confident +110 win for a
    position nobody can account for."""
    signal = _signal()
    await _finished(audit, signal)
    await _fill(audit, signal, direction="open", action="Sell to Open", leg="short",
                contracts=2, price=0.95, ts=OPENED_AT)

    [outcome] = [
        mark(idea, 7800.0, ex) for idea, ex in pending_scores(db, TRADING_DATE)
    ]
    assert outcome.shape == SHAPE_UNPRICEABLE
    assert outcome.shape != SHAPE_UNFILLED
    assert outcome.pnl is None
    assert outcome.win is None
    assert outcome.execution.problems == ("1 filled leg(s), expected 2",)


async def test_an_unpriceable_position_writes_null_terms_not_zeros(db, audit):
    """A reader must never be able to sum the components into a number the `pnl`
    beside them refuses to state."""
    signal = _signal()
    await _finished(audit, signal)
    await _fill(audit, signal, direction="open", action="Sell to Open", leg="short",
                contracts=2, price=None, ts=OPENED_AT)
    await _fill(audit, signal, direction="open", action="Buy to Open", leg="long",
                contracts=2, price=None, ts=OPENED_AT)

    [outcome] = await mark_trading_date(
        db, audit, TRADING_DATE, 7800.0, settlement_source="manual"
    )
    payload = outcome.to_payload()
    assert payload["outcome_shape"] == SHAPE_UNPRICEABLE
    assert payload["pnl"] is None
    assert payload["realized_pnl"] is None and payload["expiry_pnl"] is None
    # The counts survive: the record still says a position existed.
    assert payload["opened_contracts"] == 2
