"""Chunk 5's session wiring: what gets built, and what must not be.

The session body itself needs a broker login, so these tests go at the two seams that
can be exercised offline: the trading-side builder (pure construction) and the
structure of the branch that decides whether to call it at all.

The properties under test are the ones the whole chunk rests on:

  - shadow's terminal action is UNCHANGED — `submit=None`, no watchdog, no fill
    consumer, and the same `order.event` record for an unexpected order;
  - the four trading-side pieces are built together or not at all;
  - a submitting environment with no halt seam REFUSES rather than degrading.
"""

from __future__ import annotations

import ast
import asyncio
import inspect

import pytest

import argus.session as session_mod
from argus.execution.audited import AuditedOrderExecution
from argus.execution.entry import EntryExecutor
from argus.execution.fills import FillConsumer
from argus.execution.pipeline import build_execution_stack, trades_for_real
from argus.execution.store import InMemoryOrderStore
from argus.execution.watchdog import PositionWatchdog
from argus.risk.killswitch import KillSwitch
from tests.support import make_config
from tests.test_execution_loop import FakeAudit


class _Account:
    account_number = "3AD79010"


class _Quotes:
    """A `LegQuoteSource` stand-in. The builder only passes it through."""

    def resolve(self, occ_symbol):
        raise AssertionError("not called during construction")

    def spot(self):
        return None


class _Audit(FakeAudit):
    async def write(self, fn):
        raise AssertionError("construction must not touch the database")


def _build(**overrides):
    audit = _Audit()
    config = overrides.pop("config", None) or make_config(environment="sandbox")
    stack = build_execution_stack(
        session=object(), account=_Account(), audit=audit, config=config
    )
    stop = asyncio.Event()

    async def halt(reason, *, source, detail=None):
        pass

    side = session_mod._build_trading_side(
        config=config,
        audit=audit,
        stack=stack,
        quotes=_Quotes(),
        stop=stop,
        alert=lambda k, d: None,
        halt=halt,
        killswitch=KillSwitch(audit),
        trading_date=session_mod.date(2026, 7, 30),
        fills=FillConsumer(store=stack.store, audit=audit),
        # REQUIRED, deliberately: a default would silently disable the watchdog's
        # book-agreement check, which is the "half a comparison never runs" failure
        # the both-or-neither guard exists to prevent.
        broker_position_count=lambda: 0,
        **overrides,
    )
    return side, audit, stop


# -- the trading side is one unit ---------------------------------------------


def test_all_four_pieces_are_built_together():
    """One switch, no partial states. A watchdog without the fill consumer would watch
    a `positions` table nobody writes — it would find nothing and report nothing wrong,
    the one failure it cannot detect about itself."""
    side, _audit, _stop = _build()
    assert isinstance(side.entry, EntryExecutor)
    assert isinstance(side.watchdog, PositionWatchdog)
    assert isinstance(side.fills, FillConsumer)
    assert isinstance(side.stack.execution, AuditedOrderExecution)


def test_the_entry_drift_breaker_reads_the_watchdog_s_OWN_quote_source():
    """One quote path, not two.

    The entry walk's drift breaker and the watchdog's underlying trigger ask the same
    question of the same feed, and the OCC-vs-`streamer_symbol` distinction is handled
    in exactly one place (`LegQuotes`). A second resolver here is the bug chunk 4
    already paid for — and an entry executor wired without a spot source is a breaker
    that silently never fires, which is invisible until it matters.
    """
    side, _audit, _stop = _build()
    assert side.entry._spot is not None, "the drift breaker must be armed in production"
    assert side.entry._spot.__self__ is side.watchdog._quotes


def test_the_entry_path_and_the_watchdog_share_ONE_execution_stack():
    """Shared because the rate limiter must be process-wide to mean anything: two
    limiters at 40 req/s are an 80 req/s ceiling."""
    side, _audit, _stop = _build()
    assert side.entry._execution is side.stack.execution
    assert side.watchdog._executor._execution is side.stack.execution
    assert side.entry._guard is side.stack.guard
    assert side.watchdog._executor._guard is side.stack.guard


def test_a_fill_makes_the_watchdog_look_now_rather_than_at_the_next_interval():
    """`request_refresh` is the wiring point chunk 4 left for this. Without it a
    newly-filled spread is unwatched for up to `[watchdog] refresh_interval_s`."""
    side, _audit, _stop = _build()
    side.watchdog._refresh_requested = False
    side.entry._on_filled()
    assert side.watchdog._refresh_requested is True


def test_the_watchdog_and_the_entry_path_share_the_session_stop_event():
    """The soft side of the entry boundary sets this same event, so ending the day
    stops the watchdog too — no position is left watched by a task nobody will join."""
    side, _audit, stop = _build()
    assert side.entry._stop is stop
    assert side.watchdog._stop is stop


def test_the_entry_policy_is_the_ENTRY_walk_policy_from_config():
    """`WalkPolicy.for_entry`, not the exit one: the two differ in cadence, step and
    concession, and using the exit's on an entry would give away credit far faster."""
    config = make_config(environment="sandbox")
    side, _audit, _stop = _build(config=config)
    assert side.entry._policy.cadence_s == config.execution.entry_cadence_s
    assert side.entry._policy.walk_away_pct == config.execution.entry_walk_away_pct
    assert side.entry._policy.step_ticks == config.execution.entry_step_ticks


# -- shadow is untouched -------------------------------------------------------


def test_the_body_gates_the_whole_trading_side_on_trades_for_real():
    """Structural, because the alternative is a live-fire test.

    Asserted by AST rather than by grep: the body must call `_build_trading_side`
    exactly once, and inside a branch. A future edit that hoists it out of the
    conditional — the one change that would make shadow submit — fails here.
    """
    source = inspect.getsource(session_mod.run_session)
    tree = ast.parse(source.lstrip())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_build_trading_side"
    ]
    assert len(calls) == 1, "the trading side must be built in exactly one place"

    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id == "_build_trading_side"
            for c in ast.walk(node)
        )
    ]
    assert guarded, "_build_trading_side must sit inside a conditional, never at top level"


def test_the_watchdog_s_live_evaluability_is_published_to_status():
    """Otherwise `/status`'s exit-triggers row renders "no watchdog" forever and the
    seam's own tests pass against nothing. Structural, like the gating test above:
    running the real body needs a broker.

    Also asserts the source is built INSIDE a conditional — shadow has no watchdog,
    and publishing an empty tuple there would render "all evaluable" for positions
    that do not exist and are not being watched.
    """
    source = inspect.getsource(session_mod.run_session)
    tree = ast.parse(source.lstrip())

    published = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "session_running"
        and any(kw.arg == "evaluability_source" for kw in node.keywords)
    ]
    assert published, "session_running must publish evaluability_source"

    # The watchdog's live accessor is what feeds it — not a re-derivation from the
    # audit tail, which is the truncation problem this seam exists to sidestep.
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "impaired_positions"
        for node in ast.walk(tree)
    ), "the published source must read PositionWatchdog.impaired_positions()"

    # Specifically a branch testing `trading`, not merely "some conditional" — the
    # enclosing `if status is not None:` would satisfy the looser check while the
    # source sat outside the trading guard, which is the failure being prevented.
    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(isinstance(n, ast.Name) and n.id == "trading" for n in ast.walk(node.test))
        and any(
            isinstance(inner, ast.FunctionDef) and inner.name == "evaluability"
            for inner in ast.walk(node)
        )
    ]
    assert guarded, "the evaluability source must be built inside a `trading is not None` branch"


def test_the_watchdog_s_spent_exit_attempts_are_published_to_status():
    """The successor state to the row above, and the same structural risk (A2b).

    A spent attempt is popped out of `impaired_positions()`, so if this seam is not
    published the page renders a position with no automated path to close as an
    ordinary open position — which is the whole reason the field exists. Guarded on
    `trading` for the same reason too: an empty tuple in shadow reads as "every held
    position still has its exit attempt", about positions that do not exist.
    """
    source = inspect.getsource(session_mod.run_session)
    tree = ast.parse(source.lstrip())

    published = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "session_running"
        and any(kw.arg == "spent_exit_source" for kw in node.keywords)
    ]
    assert published, "session_running must publish spent_exit_source"

    assert any(
        isinstance(node, ast.Attribute) and node.attr == "spent_exit_attempts"
        for node in ast.walk(tree)
    ), "the published source must read PositionWatchdog.spent_exit_attempts()"

    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(isinstance(n, ast.Name) and n.id == "trading" for n in ast.walk(node.test))
        and any(
            isinstance(inner, ast.FunctionDef) and inner.name == "spent_exits"
            for inner in ast.walk(node)
        )
    ]
    assert guarded, "the spent-exit source must be built inside a `trading is not None` branch"


def test_shadow_submits_nothing_by_construction():
    assert trades_for_real(make_config(environment="shadow")) is False


def test_the_session_module_still_exports_the_old_name():
    """`tools.shadow`, the supervisor and existing tests all reference it. Renamed
    because a function called `run_shadow_session` that submits real orders in sandbox
    is exactly the drift CLAUDE.md exists to prevent — but not broken."""
    assert session_mod.run_shadow_session is session_mod.run_session


# -- the halt seam is not optional for a submitting environment ---------------


def test_the_body_refuses_to_trade_without_a_halt_seam():
    """Both the entry executor and the watchdog escalate through `halt`; an executor
    with nowhere to halt is a safety rail that silently does nothing. Checked at wiring
    time rather than at 10:42."""
    source = inspect.getsource(session_mod.run_session)
    assert "refusing to trade with nowhere to escalate" in source

    # And the constructors really do refuse, so the check above is a better error
    # message rather than the only thing standing in the way.
    class _Executor:
        def bind_quotes(self, quotes):
            pass

        def bind_scan_age(self, scan_age):
            pass

    with pytest.raises(ValueError, match="halt"):
        PositionWatchdog(
            spreads=InMemoryOrderStore(),
            quotes=_Quotes(),
            executor=_Executor(),  # type: ignore[arg-type]
            audit=_Audit(),
            config=make_config(environment="sandbox"),
            halt=None,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="halt"):
        EntryExecutor(
            execution=build_execution_stack(
                session=object(),
                account=_Account(),
                audit=_Audit(),
                config=make_config(environment="sandbox"),
            ).execution,
            audit=_Audit(),
            config=make_config(environment="sandbox"),
            guard=None,  # type: ignore[arg-type]
            killswitch=KillSwitch(_Audit()),
            halt=None,  # type: ignore[arg-type]
            stop=asyncio.Event(),
        )


def test_the_watchdog_gets_BOTH_sides_of_the_book_agreement_check():
    """The wiring, not the logic: the watchdog can only compare the broker against
    the ledger if this assembly hands it both. Half of it raises at construction, so
    the failure this pins is a silent one — a check that never runs."""
    side, _, _ = _build()
    wd = side.watchdog
    assert wd._broker_position_count is not None, "broker side must be wired"
    assert wd._ledger_state is not None, "ledger side must be wired"
    assert wd._ledger_state.__self__ is wd._spreads, (
        "the ledger count must come from the SAME store the watch list does"
    )
