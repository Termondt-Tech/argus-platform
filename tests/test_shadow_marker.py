"""Round-tripping proposal evidence through the real audit log.

Reads back exactly what the two writers produce (same payload shape), so a change
to either terminal record that breaks scoring fails here rather than silently
producing an empty mark weeks later.

TWO writers, ONE reader: `SignalLoop` writes `shadow.would_submit` in shadow and
`EntryExecutor._dry_run` writes `entry.dry_run` everywhere else. The payload
helpers below build each shape BY HAND rather than calling `markable_payload`,
deliberately — a test that constructs its fixture with the same function the
production writer uses cannot catch that function changing shape. The end-to-end
"real writer to real decoder" check lives in `test_entry.py`.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from argus.shadow.marker import OUTCOME, mark_trading_date, marked_dates, read_proposals
from argus.signal.models import OptionQuote, SpreadSignal

TRADING_DATE = date(2026, 7, 21)


def _signal(short_strike=7450.0, long_strike=7445.0, credit=0.80, side="put"):
    """Built from the real domain type, so the payload shape can't drift."""
    return SpreadSignal(
        underlying="SPXW",
        side=side,
        short_leg=OptionQuote(
            symbol=f"SPXW {short_strike:g}", strike=short_strike, right=side,
            delta=-0.15, bid=1.0, ask=1.2,
        ),
        long_leg=OptionQuote(
            symbol=f"SPXW {long_strike:g}", strike=long_strike, right=side,
            delta=-0.10, bid=0.3, ask=0.5,
        ),
        credit=credit,
        width=abs(short_strike - long_strike),
        captured_at=0.0,
    )


def _marking_block(signal, contracts):
    """The `signal` + `proposal` pair BOTH writers emit. Spelled out on purpose."""
    return {
        "proposal": {
            "symbol": signal.describe(),
            "contracts": contracts,
            "max_loss": contracts * signal.max_loss_per_contract,
        },
        "signal": signal.to_payload(),
    }


async def _would_submit(audit, signal, contracts=2, ts="2026-07-21T18:30:00.000000Z"):
    # Same payload the orchestrator writes, at a controlled timestamp.
    audit._clock = lambda: ts
    await audit.record("shadow.would_submit", _marking_block(signal, contracts))


async def _dry_run(audit, signal, contracts=2, ts="2026-07-21T18:30:00.000000Z"):
    """Same payload `EntryExecutor._dry_run` writes: the marking block PLUS the
    dry-run-specific fields, which the decoder must ignore rather than trip on."""
    audit._clock = lambda: ts
    await audit.record(
        "entry.dry_run",
        {
            "candidate_id": (
                f"20260721-{signal.side}-{signal.short_leg.strike:g}-"
                f"{signal.long_leg.strike:g}"
            ),
            "submitted": True,
            "walked": False,
            "why_not_walked": "a dry run mints no broker order id",
            "guard": {"external_identifier": "argus-open-x", "blocked": False,
                      "reason": None, "conflicts": []},
            "dry_run": True,
            "order": {"broker_order_id": None},
            "buying_power_effect": None,
            "fees": None,
            "warnings": [],
            "errors": [],
            **_marking_block(signal, contracts),
        },
    )


async def test_reads_back_what_the_loop_wrote(db, audit):
    await _would_submit(audit, _signal())
    proposals = read_proposals(db, TRADING_DATE)
    assert len(proposals) == 1
    assert (proposals[0].side, proposals[0].short_strike) == ("put", 7450.0)
    assert proposals[0].credit == 0.80
    assert proposals[0].contracts == 2


async def test_et_date_window_not_utc_date(db, audit):
    # 2026-07-21 00:30 UTC is still 2026-07-20 in ET. Bucketing on the UTC date
    # would file an evening session under the following trading day.
    await _would_submit(audit, _signal(), ts="2026-07-21T00:30:00.000000Z")
    assert read_proposals(db, date(2026, 7, 21)) == []
    assert len(read_proposals(db, date(2026, 7, 20))) == 1


async def test_marking_records_outcomes_and_scores_them(db, audit):
    await _would_submit(audit, _signal())
    outcomes = await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    )
    assert len(outcomes) == 1
    assert outcomes[0].pnl == 160.0

    rows = db.execute(
        "SELECT payload FROM audit_log WHERE category = ?", (OUTCOME,)
    ).fetchall()
    assert len(rows) == 1
    recorded = json.loads(rows[0][0])
    assert recorded["result"] == "full_credit"
    assert recorded["settlement"] == 7480.0
    assert recorded["settlement_source"] == "manual"
    assert recorded["pnl"] == 160.0
    assert recorded["first_seen_et"] == "14:30:00"  # 18:30 UTC in ET


async def test_marking_dedups_before_scoring(db, audit):
    # 50 records of the same spread must score as one idea, not fifty.
    for _ in range(50):
        await _would_submit(audit, _signal())
    outcomes = await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    )
    assert len(outcomes) == 1
    assert outcomes[0].idea.proposals == 50


async def test_double_marking_is_refused(db, audit):
    await _would_submit(audit, _signal())
    await mark_trading_date(db, audit, TRADING_DATE, 7480.0, settlement_source="manual")
    # Append-only: a second scoring can't replace the first, so it must be a
    # deliberate act rather than something a repeated command does silently.
    with pytest.raises(ValueError, match="already marked"):
        await mark_trading_date(db, audit, TRADING_DATE, 7480.0, settlement_source="manual")

    await mark_trading_date(
        db, audit, TRADING_DATE, 7400.0, settlement_source="manual", remark=True
    )
    assert marked_dates(db) == {"2026-07-21"}
    assert len(db.execute("SELECT 1 FROM audit_log WHERE category = ?", (OUTCOME,)).fetchall()) == 2


async def test_marking_an_unmarked_date_leaves_others_alone(db, audit):
    await _would_submit(audit, _signal(), ts="2026-07-20T18:30:00.000000Z")
    await _would_submit(audit, _signal(short_strike=7460.0, long_strike=7455.0))
    outcomes = await mark_trading_date(
        db, audit, date(2026, 7, 20), 7480.0, settlement_source="manual"
    )
    assert len(outcomes) == 1
    assert outcomes[0].idea.short_strike == 7450.0
    assert marked_dates(db) == {"2026-07-20"}


async def test_a_day_with_no_candidates_marks_cleanly(db, audit):
    assert await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    ) == []


async def test_malformed_record_is_skipped_not_fatal(db, audit):
    audit._clock = lambda: "2026-07-21T18:30:00.000000Z"
    await audit.record("shadow.would_submit", {"proposal": {}, "signal": {"side": "put"}})
    await _would_submit(audit, _signal())
    # One bad row must not cost the whole day's scoring.
    assert len(read_proposals(db, TRADING_DATE)) == 1


# =============================================================================
# The category union: `shadow.would_submit` OR `entry.dry_run`, keyed on which
# records the date actually holds — never on which environment is configured.
#
# `shadow.would_submit` stopped being written at the 2026-08-11 sandbox flip and
# a marker that filtered on it alone reported zero ideas for every day after.
# The requirement these tests hold down is not just "sandbox works now" but
# "either environment works, in either direction, with no branch to flip".
# =============================================================================


async def test_a_pure_shadow_day_is_unchanged(db, audit):
    """REGRESSION. Shadow-mode marking behaviour must be untouched by the union:
    weeks of Phase 0 evidence are scored by this path and a change to it would
    silently re-grade history."""
    signal = _signal()
    for _ in range(5):
        await _would_submit(audit, signal)
    await _would_submit(audit, _signal(short_strike=7460.0, long_strike=7455.0, credit=0.55))

    outcomes = await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    )
    assert [o.idea.short_strike for o in outcomes] == [7450.0, 7460.0]
    assert [o.idea.proposals for o in outcomes] == [5, 1]
    assert [o.idea.entry_rank for o in outcomes] == [1, 2]
    assert outcomes[0].pnl == 160.0


async def test_a_pure_dry_run_day_is_marked(db, audit):
    """THE core deliverable: a sandbox dry-run day, which writes only
    `entry.dry_run`, scores exactly as a shadow day would."""
    await _dry_run(audit, _signal())

    outcomes = await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    )
    assert len(outcomes) == 1
    assert (outcomes[0].idea.side, outcomes[0].idea.short_strike) == ("put", 7450.0)
    assert outcomes[0].idea.credit == 0.80
    assert outcomes[0].idea.contracts == 2
    assert outcomes[0].pnl == 160.0
    assert outcomes[0].result == "full_credit"


async def test_a_dry_run_day_scores_identically_to_the_same_shadow_day(db, audit):
    """The two categories are interchangeable as evidence — same spread, same
    settlement, same number. If they ever diverge, the signal-quality series has a
    step change at every environment flip rather than a continuous record."""
    await _would_submit(audit, _signal())
    shadow_only = await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    )

    await _dry_run(audit, _signal(), ts="2026-07-22T18:30:00.000000Z")
    dry_only = await mark_trading_date(
        db, audit, date(2026, 7, 22), 7480.0, settlement_source="manual"
    )

    assert len(shadow_only) == len(dry_only) == 1
    assert dry_only[0].pnl == shadow_only[0].pnl
    assert dry_only[0].result == shadow_only[0].result
    assert dry_only[0].idea.credit == shadow_only[0].idea.credit


async def test_dry_run_dedup_keeps_the_first_credit_too(db, audit):
    """Dedup semantics are identical across categories. A dry-run day normally
    writes ONE record per candidate (`_attempted` claims the spread before the
    submit), but a mid-day process restart re-arms that set — so more than one is
    possible, and the first one still has to win."""
    await _dry_run(audit, _signal(credit=0.80))
    await _dry_run(audit, _signal(credit=1.40))
    await _dry_run(audit, _signal(credit=0.20))

    (outcome,) = await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    )
    assert outcome.idea.credit == 0.80, "credit is the FIRST proposal's, by seq"
    assert outcome.idea.proposals == 3
    assert (outcome.idea.best_credit, outcome.idea.worst_credit) == (1.40, 0.20)


async def test_shadow_dedup_keeps_the_first_credit_too(db, audit):
    """The same property for the other category, asserted independently rather
    than assumed to follow from the dry-run case."""
    await _would_submit(audit, _signal(credit=0.80))
    await _would_submit(audit, _signal(credit=1.40))

    (outcome,) = await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    )
    assert outcome.idea.credit == 0.80
    assert outcome.idea.proposals == 2


async def test_a_mixed_day_collapses_overlapping_spreads_into_one_idea(db, audit):
    """Structurally possible: a process restarted mid-day across an environment
    change writes both categories to the same date. Defined behaviour — grouping
    ignores the category, so the SAME spread is one idea whose credit comes from
    whichever record has the lower chain `seq`, not from whichever category is
    read first."""
    await _would_submit(audit, _signal(credit=0.80))          # seq N   — first
    await _dry_run(audit, _signal(credit=1.40))               # seq N+1 — same spread
    await _would_submit(audit, _signal(credit=0.90))          # seq N+2 — same spread
    await _dry_run(audit, _signal(short_strike=7460.0, long_strike=7455.0, credit=0.55))

    outcomes = await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    )
    assert len(outcomes) == 2, "the overlapping spread must NOT be counted twice"
    first = outcomes[0]
    assert first.idea.short_strike == 7450.0
    assert first.idea.credit == 0.80, "earliest seq wins, regardless of category"
    assert first.idea.proposals == 3, "all three records are proposals of one idea"
    assert (first.idea.best_credit, first.idea.worst_credit) == (1.40, 0.80)
    assert [o.idea.entry_rank for o in outcomes] == [1, 2]


async def test_a_mixed_day_orders_by_seq_not_by_category(db, audit):
    """The ordering is the chain's, so a dry-run record written BEFORE a shadow
    record of the same spread supplies the credit — the reverse of the case
    above. If the union were read category-by-category this would come out 0.80."""
    await _dry_run(audit, _signal(credit=1.40))
    await _would_submit(audit, _signal(credit=0.80))

    (outcome,) = await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    )
    assert outcome.idea.credit == 1.40
    assert outcome.idea.proposals == 2


async def test_both_categories_on_different_spreads_are_both_scored(db, audit):
    await _would_submit(audit, _signal(side="put"))
    await _dry_run(audit, _signal(short_strike=7550.0, long_strike=7555.0, side="call"))

    outcomes = await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    )
    assert {o.idea.side for o in outcomes} == {"put", "call"}
    assert all(o.result == "full_credit" for o in outcomes)


async def test_a_pre_fix_dry_run_record_is_skipped_not_fatal(db, audit):
    """Every `entry.dry_run` written before 2026-08-12 carries no signal/proposal
    block. Those rows are unscoreable — that is the 2026-08-11 discontinuity, and
    it is not backfilled — but they must not stop a mixed date from scoring the
    records that ARE complete."""
    audit._clock = lambda: "2026-07-21T18:30:00.000000Z"
    await audit.record(
        "entry.dry_run",
        {
            "candidate_id": "20260721-put-7450-7445",
            "submitted": True,
            "walked": False,
            "why_not_walked": "a dry run mints no broker order id",
            "guard": {"blocked": False},
            "dry_run": True,
        },
    )
    await _dry_run(audit, _signal(short_strike=7460.0, long_strike=7455.0))

    proposals = read_proposals(db, TRADING_DATE)
    assert len(proposals) == 1
    assert proposals[0].short_strike == 7460.0


async def test_the_et_date_window_applies_to_dry_run_records_too(db, audit):
    """The union changed the category filter, not the day boundary."""
    await _dry_run(audit, _signal(), ts="2026-07-21T00:30:00.000000Z")
    assert read_proposals(db, date(2026, 7, 21)) == []
    assert len(read_proposals(db, date(2026, 7, 20))) == 1


async def test_other_entry_categories_are_not_read_as_proposals(db, audit):
    """The union is three NAMED categories, not a prefix match.

    `entry.not_built` and `entry.aborted_day` carry a `candidate_id` and sit right
    next to `entry.finished` in the same module, so a filter that matched `entry.*`
    would read a refusal as a proposal. `entry.finished` itself joined the union on
    2026-08-13 and has its own coverage below.
    """
    audit._clock = lambda: "2026-07-21T18:30:00.000000Z"
    for category in ("entry.not_built", "entry.aborted_day",
                     "signal.candidate", "gate.decision"):
        await audit.record(category, _marking_block(_signal(), 2))
    assert read_proposals(db, TRADING_DATE) == []


# -- the property that keeps this fixed if the environment ever reverts --------


def test_the_marker_never_consults_the_environment():
    """The union is keyed on CATEGORY PRESENCE, and this is what keeps it that way.

    An `if environment == "sandbox"` anywhere in this path would re-break marking
    the moment the environment moved again — in either direction — and would also
    make an old date's score depend on today's config rather than on what was
    recorded that day. The module must not even be able to ask: it imports no
    `Config` and names no environment.
    """
    import ast
    import inspect

    from argus.shadow import marker

    tree = ast.parse(inspect.getsource(marker))

    imported = {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.Import) for alias in node.names
    } | {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any("config" in (name or "") for name in imported), (
        f"marker.py must not import configuration: {imported}"
    )

    # No identifier, attribute or string literal naming an environment — in CODE.
    # Docstrings and comments explain WHY there is no branch and must stay.
    code = [
        node for node in ast.walk(tree)
        if not isinstance(node, (ast.Expr, ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef))
    ]
    names = {n.id for n in code if isinstance(n, ast.Name)}
    names |= {n.attr for n in code if isinstance(n, ast.Attribute)}
    assert not names & {"environment", "ARGUS_ENV", "Config", "config"}, names

    # A bare environment name as a literal, which is what a comparison would use.
    # Exact match only: prose in the docstrings explaining why there is no branch
    # says "shadow" constantly, and `WOULD_SUBMIT` is legitimately "shadow.*".
    literals = {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value in {"shadow", "sandbox", "live"}
    }
    assert not literals, f"marker.py names an environment as a literal: {literals}"


async def test_marking_needs_no_config_at_all(db, audit):
    """Exercised end to end against fixture data with no `Config` object anywhere
    in the call — the query path cannot be environment-conditional if there is no
    environment available to condition on."""
    await _dry_run(audit, _signal())
    await _would_submit(audit, _signal(short_strike=7460.0, long_strike=7455.0))

    outcomes = await mark_trading_date(
        db, audit, TRADING_DATE, 7480.0, settlement_source="manual"
    )
    assert len(outcomes) == 2
