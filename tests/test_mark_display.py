"""The mark tool's price columns must not depend on float representation error.

Observed on 2026-07-30 and reproduced here: two trade ideas with the SAME credit
(0.525) printed one row apart in the same run, one as 0.52 and the other as 0.53.
A deterministic rounding rule — even a wrong one — cannot do that. Only a float
tie can, and a half-tick credit sits exactly on one.

These tests go through the production arithmetic (`OptionQuote.floored_mid`, i.e.
`execution.zerobid`) rather than hand-writing the floats, so they keep testing the
real thing if the mid ever changes shape.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from argus.clock import ET
from argus.shadow.outcome import Outcome, TradeIdea
from argus.signal.models import OptionQuote
from argus.tools.mark import _fixed, _format_row, _MONEY_PLACES, _PRICE_PLACES

# The 2026-07-30 pair, with leg quotes that reproduce each side of the tie.
# Both nets are 21 * $0.025 = $0.525; the floats differ in the last bit.
RANK_17 = dict(short=(7400.0, 0.65, 0.70), long=(7395.0, 0.10, 0.20))  # -> 0.525
RANK_19 = dict(short=(7410.0, 0.60, 0.70), long=(7405.0, 0.10, 0.15))  # -> 0.5249999999999999


def _credit(spread) -> float:
    """The net credit exactly as `strategy._select` computes it."""
    short_strike, sb, sa = spread["short"]
    long_strike, lb, la = spread["long"]
    short = OptionQuote("S", short_strike, "put", -0.12, sb, sa)
    long_ = OptionQuote("L", long_strike, "put", -0.08, lb, la)
    return short.floored_mid - long_.floored_mid


def _outcome(spread, *, rank: int, contracts: int = 4, liability: float = 0.0) -> Outcome:
    at = datetime(2026, 7, 30, 14, 30, tzinfo=timezone.utc).astimezone(ET)
    idea = TradeIdea(
        trading_date=date(2026, 7, 30),
        side="put",
        short_strike=spread["short"][0],
        long_strike=spread["long"][0],
        width=abs(spread["short"][0] - spread["long"][0]),
        contracts=contracts,
        credit=_credit(spread),
        best_credit=_credit(spread),
        worst_credit=_credit(spread),
        proposals=57,
        first_seen_et=at,
        last_seen_et=at,
        first_seq=1,
        entry_rank=rank,
    )
    return Outcome(idea=idea, settlement=7480.0, liability=liability)


def test_the_two_credits_are_the_same_value_but_different_floats():
    """The premise. Without this, the rest of the file is testing nothing."""
    assert _credit(RANK_17) != _credit(RANK_19)
    assert _credit(RANK_17) == pytest.approx(0.525, abs=1e-9)
    assert _credit(RANK_19) == pytest.approx(0.525, abs=1e-9)


def test_plain_float_formatting_is_what_split_them():
    """Pin the defect itself, so nobody reintroduces `.2f` thinking it was fine."""
    assert f"{_credit(RANK_17):.2f}" == "0.53"
    assert f"{_credit(RANK_19):.2f}" == "0.52"


def test_identical_credits_print_identically():
    a, b = _outcome(RANK_17, rank=17), _outcome(RANK_19, rank=19)
    assert _fixed(a.idea.credit, _PRICE_PLACES) == _fixed(b.idea.credit, _PRICE_PLACES)
    assert "0.525" in _format_row(a)
    assert "0.525" in _format_row(b)


def test_credit_column_reconciles_against_its_own_pnl():
    """The property the column is read for: credit x contracts x 100 == P&L.

    This is what the two-decimal column could not do — 0.52 or 0.53 on a 4-lot
    gives $208 or $212 against a P&L of $210.
    """
    for spread, rank in ((RANK_17, 17), (RANK_19, 19)):
        o = _outcome(spread, rank=rank)
        credit = _fixed(o.idea.credit, _PRICE_PLACES)
        pnl = _fixed(o.pnl, _MONEY_PLACES)
        assert credit * o.idea.contracts * 100 == pnl
        assert pnl == 210


def test_partial_row_reconciles_too():
    """(credit - liability) x contracts x 100 == P&L, on a row that finished ITM."""
    o = _outcome(RANK_17, rank=17, liability=2.25)
    credit = _fixed(o.idea.credit, _PRICE_PLACES)
    liability = _fixed(o.liability, _PRICE_PLACES)
    assert (credit - liability) * o.idea.contracts * 100 == _fixed(o.pnl, _MONEY_PLACES)


@pytest.mark.parametrize(
    "value,places,expected",
    [
        # Both sides of the tie, and the exact decimal, all agree.
        (0.5249999999999999, _PRICE_PLACES, "0.525"),
        (0.525, _PRICE_PLACES, "0.525"),
        (0.5250000000000001, _PRICE_PLACES, "0.525"),
        # A real recorded credit from the shadow log (2026-07-22).
        (0.5500000000000002, _PRICE_PLACES, "0.550"),
        # Ties round away from zero, symmetrically — same rule as zerobid.
        (0.0125, _PRICE_PLACES, "0.013"),
        (-0.0125, _PRICE_PLACES, "-0.013"),
        (209.99999999999997, _MONEY_PLACES, "210.00"),
        (-1778.0, _MONEY_PLACES, "-1778.00"),
    ],
)
def test_fixed_rounding(value, places, expected):
    assert str(_fixed(value, places)) == expected


def test_fixed_refuses_a_non_finite_number():
    with pytest.raises(ValueError):
        _fixed(float("nan"), _PRICE_PLACES)


# -- the live-fire columns (2026-08-13) ---------------------------------------


def _execution(**kw):
    from argus.shadow.outcome import Execution
    at = datetime(2026, 7, 30, 14, 30, tzinfo=timezone.utc).astimezone(ET)
    defaults = dict(
        candidate_id="20260730-put-7400-7395",
        opened_contracts=4, closed_contracts=4,
        entry_credit=0.55, exit_debit=1.20,
        first_seq=1, first_fill_et=at, last_fill_et=at, problems=(),
    )
    return Execution(**{**defaults, **kw})


def test_a_closed_position_shows_its_FILL_credit_and_filled_size():
    """The credit column is the cost basis the P&L was computed from. Showing the
    proposed credit next to a realized P&L derived from a different number is a
    table nobody can reconcile by hand — which is what the column is read for."""
    from dataclasses import replace

    base = _outcome(RANK_17, rank=1, contracts=9)   # proposed 9 ...
    outcome = replace(base, execution=_execution(opened_contracts=4))  # ... filled 4
    row = _format_row(outcome)

    assert "  0.550 " in row, row      # the fill credit, not the 0.525 proposed
    assert "   4 " in row, row         # the filled size, not 9
    assert "closed_intraday" in row


def test_an_unpriceable_position_prints_a_dash_not_a_zero():
    """A fabricated 0.00 in a column that gets summed is worse than a hole."""
    from dataclasses import replace

    base = _outcome(RANK_17, rank=1)
    outcome = replace(base, execution=_execution(entry_credit=None, exit_debit=None))
    row = _format_row(outcome)

    assert outcome.pnl is None
    assert "unpriceable" in row
    # The P&L column, between the liability and the settlement result.
    assert row.split()[-5] == "-", row


def test_an_unfilled_idea_still_prints_exactly_as_it_did():
    """Every row written before live fire was an unfilled idea. The `how` column is
    added; nothing left of it moves."""
    row = _format_row(_outcome(RANK_17, rank=17))
    assert row.startswith("  17  put  7400/7395")
    assert "  0.525 " in row and "unfilled" in row


def test_the_stop_bill_compares_LIKE_FOR_LIKE(capsys):
    """The two figures must cover the SAME positions. Comparing realized-on-what-
    closed against a counterfactual over everything filled is two different
    subsets, and the delta a reader takes from it would mean nothing."""
    from dataclasses import replace

    from argus.tools.mark import _print_table

    # Stopped out at a loss on a day the spread would have expired worthless.
    stopped = replace(
        _outcome(RANK_17, rank=1, contracts=2),
        execution=_execution(opened_contracts=2, closed_contracts=2,
                             entry_credit=0.55, exit_debit=1.20),
    )
    # Held to expiry, full credit.
    held = replace(
        _outcome(RANK_19, rank=2, contracts=2),
        execution=_execution(opened_contracts=2, closed_contracts=0,
                             entry_credit=0.60, exit_debit=None),
    )
    _print_table([stopped, held])
    out = capsys.readouterr().out

    # scored: -130 + 120 = -10.  held to settlement: 110 + 120 = 230.
    assert "2 idea(s) actually filled (1 closed intraday)" in out
    assert "scored -10.00" in out
    assert "would have been 230.00" in out
    assert "exiting early cost 240.00" in out


def test_the_settlement_tally_says_it_is_about_settlement(capsys):
    """It answers "where did the index settle", NOT "how did these do". (2026-08-19)

    `result` is settlement-derived for every row, so for anything closed intraday it
    is a counterfactual. Unlabelled, the real 2026-08-19 run printed
    `full credit 2  partial 0  max loss 0` directly beneath `1 win / 1 loss`, on a
    day whose second idea was stopped out for -120. Read back a week later that line
    says "no losers" about a day that had one.
    """
    from dataclasses import replace

    from argus.tools.mark import _print_table

    stopped = replace(
        _outcome(RANK_17, rank=1, contracts=2),
        execution=_execution(opened_contracts=2, closed_contracts=2,
                             entry_credit=0.55, exit_debit=1.20),
    )
    held = replace(
        _outcome(RANK_19, rank=2, contracts=2),
        execution=_execution(opened_contracts=2, closed_contracts=0,
                             entry_credit=0.60, exit_debit=None),
    )
    _print_table([stopped, held])
    out = capsys.readouterr().out

    # Both settled beyond the short strike, so the tally itself is unchanged...
    assert "where the index settled: full credit 2  partial 0  max loss 0" in out
    # ... but it no longer stands alone next to a realized loss.
    assert "1 of these closed before 16:00" in out
    assert "settlement did not govern P&L" in out


def test_no_disclosure_line_when_everything_was_held_to_expiry(capsys):
    """Nothing counterfactual in the tally, so nothing to disclaim — the note must
    not become boilerplate that gets skimmed past on the days it matters."""
    from dataclasses import replace

    from argus.tools.mark import _print_table

    held = replace(
        _outcome(RANK_19, rank=1, contracts=2),
        execution=_execution(opened_contracts=2, closed_contracts=0,
                             entry_credit=0.60, exit_debit=None),
    )
    _print_table([held])
    out = capsys.readouterr().out

    assert "where the index settled:" in out
    assert "closed before 16:00" not in out
