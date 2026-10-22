"""The stub close — ONE further close for a partial close's stub. (2026-09-24)

`docs/designs/2026-09-24-quantity-aware-guard-for-close-identifiers.md` is the
design; `docs/decisions/2026-09-23-closing-the-stub-a-partial-close-leaves.md`
is the decision record it discharges. What these hold, per its §9.3–§9.5 and the
2026-09-24 review (§12 of the design):

  * detection needs BOOKED evidence — the scan holding FEWER contracts than the
    close was sized for — so a close whose fills are still in flight (full or
    partial) is never mistaken for a stub;
  * the handoff carries the ORIGIN's reasons plus any EMERGENCY reason firing
    now, so a profit-origin stub whose stop is true closes on the exit ladder;
  * past the flatten mark the continuation YIELDS to the flatten's own unspent
    allowance, so nothing it does can keep the flatten from being reached;
  * the allowance is ONE further close: the Tier 1 `watchdog.stub_close` record
    is awaited BEFORE the FIRST handoff and written ONCE; the restore is only on
    the two proven-no-send values — `NOT_ATTEMPTED`, and a walk the guard
    `BLOCKED` for any reason — paced one refresh interval apart, `BLOCKED`
    bounded at three;
  * the stub path never writes `_attempted`, `_sends`, `_flatten_attempted` or
    `_profit_spent` — its one allowance is its only budget;
  * the boot read rehydrates the durable spend (unreadable, or a key the grammar
    cannot parse, fails toward spent, one page, no halt) and
    `stub_close_enabled = false` is exactly today's behaviour.

The guard half's own suite is `tests/test_order_guard.py`; the two deliberate
updates to the profit-target tests live there, in their file.
"""

from __future__ import annotations

import ast
import dataclasses
import math
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import argus.execution.watchdog as wd
from argus.audit.log import AuditLog
from argus.clock import et_today
from argus.config import Config, ConfigError
from argus.execution.guard import BLOCKED_CLOSE_EXCEEDED, BLOCKED_WORKING
from argus.execution.loop import WalkOutcome, WalkResult
from argus.execution.models import (
    LegAction,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
)
from argus.execution.store import OpenSpreadScan
from argus.execution.watchdog import (
    ExitOutcome,
    ExitReport,
    SpentExitAttempt,
    TriggerReason,
    _STUB_BLOCKED_RETRIES,
    _stub_handoff_reasons,
)
from argus.risk.rehydrate import (
    STUB_CLOSE_CATEGORY,
    StubCloseRehydrationError,
    load_stub_close_spent,
)
from support import make_config
from test_watchdog import (
    FakeAudit,
    FakeClock,
    FakeETClock,
    LONG_STREAM,
    SHORT_STREAM,
    RecordingExecutor,
    _triggered_and_priceable,
    build_quotes,
    build_watchdog,
    spread,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "argus"
PROFIT = frozenset({TriggerReason.PROFIT_TARGET})
CREDIT = frozenset({TriggerReason.CREDIT_MULTIPLE})
FLATTEN = TriggerReason.FLATTEN_BEFORE_CLOSE
KEY = spread().external_identifier                    # argus-open-20260729-put-x
CLOSE_KEY = f"argus-close-{spread().candidate_id}"
#: The test config's `watchdog.refresh_interval_s` — the retry pacing.
REFRESH_S = make_config().watchdog.refresh_interval_s


def _stub_on(**watchdog):
    """A config with the stub close ON (and any other `[watchdog]` overrides)."""
    return make_config(watchdog={"stub_close_enabled": True, **watchdog})


def _credit_stop(clock):
    """A setter that keeps the credit stop true on a fresh tick."""
    return lambda market: _triggered_and_priceable(clock, market)


def _quiet(market):
    """Both legs fresh, NO trigger true (mark 0.35 against a 1.70 stop), spot far
    from the strike — a stub nothing is firing on."""
    market.set(SHORT_STREAM, 0.45, 0.55)
    market.set(LONG_STREAM, 0.10, 0.20)
    market.set("SPX", 5100.0, 5100.0)


async def _tick(watchdog, clock, market, setter, *, step: float = 1.5):
    clock.advance(step)
    setter(market)
    await watchdog.check_once()
    await watchdog._drain_exits()


def _at_one_tick(market):
    """Both legs fresh, net mid exactly one tick, spot far from the strike —
    the profit target's own setter (test_profit_target's, restated so the
    import is local and visible)."""
    market.set(SHORT_STREAM, 0.10, 0.15)
    market.set(LONG_STREAM, 0.05, 0.10)
    market.set("SPX", 5100.0, 5100.0)


def _stub_watchdog(*, outcome=ExitOutcome.FILLED, config=None, et_clock=None,
                   executor=None, audit=None):
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    executor = executor if executor is not None else RecordingExecutor(outcome)
    watchdog, live_audit, alerts, halts = build_watchdog(
        clock=clock,
        quotes=quotes,
        scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor,
        config=config or _stub_on(),
        et_clock=et_clock,
        audit=audit,
    )
    return watchdog, clock, market, executor, live_audit, alerts, halts


async def _spend_on_a_filled_emergency_exit(watchdog, clock, market):
    """Drive the ordinary path to a close that FILLED, sized for 2, while the
    ledger still holds the full 2 — the state BEFORE any booking lands."""
    for _ in range(3):
        await _tick(watchdog, clock, market, _credit_stop(clock))
    assert KEY in watchdog._attempted
    spent = watchdog._attempted[KEY]
    assert spent.outcome is ExitOutcome.FILLED
    assert spent.reasons == CREDIT
    assert spent.contracts == 2


def _book_the_partial(watchdog, *, held: int = 1):
    """The partial's closing fill books: the scan now holds the stub."""
    watchdog._spreads.scan = OpenSpreadScan(spreads=(spread(contracts_held=held),))
    watchdog.request_refresh()


async def _a_stub(watchdog, clock, market):
    await _spend_on_a_filled_emergency_exit(watchdog, clock, market)
    _book_the_partial(watchdog)


def _stub_calls(executor):
    return [c for c, stub in zip(executor.calls, executor.stubs) if stub]


# -- detection and the handoff --------------------------------------------------


async def test_an_unbooked_close_is_not_a_stub():
    """Review finding 4. After a close FILLED, a scan still holding the FULL size
    is every close whose fills have not booked yet — a full close in flight looks
    exactly like this. No handoff, no Tier 1 record, no guard ask: it waits, and
    a full close then simply leaves the scan."""
    watchdog, clock, market, executor, audit, alerts, _ = _stub_watchdog()
    await _spend_on_a_filled_emergency_exit(watchdog, clock, market)
    for _ in range(6):
        await _tick(watchdog, clock, market, _credit_stop(clock))

    assert len(executor.calls) == 1
    assert "watchdog.stub_close" not in audit.events
    (row,) = watchdog.spent_exit_attempts()
    assert row.stub_close == "available"

    watchdog._spreads.scan = OpenSpreadScan()          # the full close booked
    watchdog.request_refresh()
    await _tick(watchdog, clock, market, _credit_stop(clock))
    assert watchdog.spent_exit_attempts() == ()
    assert len(executor.calls) == 1


async def test_detection_hands_off_once_with_the_origin_reasons():
    """§9.3.1/§9.3.2. A spent FILLED position the scan holds FEWER contracts of
    gets exactly one `_run_exit(stub=True)` handoff, sized to the stub, with the
    ORIGIN's reasons."""
    watchdog, clock, market, executor, audit, alerts, _ = _stub_watchdog()
    await _a_stub(watchdog, clock, market)

    await _tick(watchdog, clock, market, _credit_stop(clock))

    assert executor.stubs == [False, True]
    stub_spread, reasons = executor.calls[-1]
    assert stub_spread.contracts == 1
    assert reasons == CREDIT, "the ORIGIN's ladder, carried on the spend"
    assert KEY in watchdog._stub_spent
    # Tier 1, with the B1 throttle scope, the position key the boot read reads,
    # and the grammar's close sibling.
    detail = audit.payload("watchdog.stub_close")
    assert detail["external_identifier"] == KEY
    assert detail["candidate_id"] == spread().candidate_id
    assert detail["close_identifier"] == CLOSE_KEY
    assert detail["origin_reasons"] == ["credit_multiple"]
    assert detail["handoff_reasons"] == ["credit_multiple"]
    assert (detail["contracts"], detail["origin_contracts"]) == (1, 2)
    # And bounded: further ticks page, never hand off again (§9.3.3).
    for _ in range(6):
        await _tick(watchdog, clock, market, _credit_stop(clock))
    assert len(executor.calls) == 2
    (row,) = watchdog.spent_exit_attempts()
    assert row.stub_close == "sent"


async def test_the_durable_spend_is_on_the_trail_before_the_handoff():
    """§9.3.1's ordering assertion, on the recording fake: the executor sees the
    Tier 1 record already written when it is reached — the audit-before-action
    rule, and the row the boot read rehydrates from."""
    class OrderingExecutor(RecordingExecutor):
        def __init__(self, audit):
            super().__init__(ExitOutcome.FILLED)
            self.audit = audit
            self.seen: list[bool] = []

        async def close(self, spread_, market, reasons, *, stub=False):
            self.seen.append("watchdog.stub_close" in self.audit.events)
            return await super().close(spread_, market, reasons, stub=stub)

    audit = FakeAudit()
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    executor = OrderingExecutor(audit)
    watchdog, _, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, config=_stub_on(), audit=audit,
    )
    await _a_stub(watchdog, clock, market)
    await _tick(watchdog, clock, market, _credit_stop(clock))

    assert executor.stubs[-1] is True
    assert executor.seen[-1] is True, "the spend is durable before the send"


async def test_a_quiet_profit_origin_re_takes_on_the_profit_ladder():
    """§9.3.2. A profit-take stub nothing else is firing on continues on the
    profit ladder — the origin's decision, carried."""
    watchdog, clock, market, executor, audit, _, _ = _stub_watchdog(
        config=_stub_on(profit_target_enabled=True)
    )
    for _ in range(3):        # the take fires, sustained, and FILLS
        await _tick(watchdog, clock, market, _at_one_tick)
    assert [r for _, r in executor.calls] == [PROFIT]
    assert watchdog._attempted[KEY].reasons == PROFIT
    _book_the_partial(watchdog)

    await _tick(watchdog, clock, market, _at_one_tick)
    assert executor.stubs[-1] is True
    assert executor.calls[-1][1] == PROFIT, "the continuation re-takes"
    assert audit.payload("watchdog.stub_close")["origin_reasons"] == ["profit_target"]


async def test_a_profit_origin_whose_stop_fires_closes_on_the_exit_ladder():
    """Review finding 2. A profit-origin stub is handed the take while the take
    is all there is — and when its mark has left one tick, `close()` refuses to
    build that take (`NOT_ATTEMPTED`), which restores. The moment the credit
    stop FIRES, the next handoff is the stop, on the exit ladder, and it goes AT
    ONCE: an emergency is never delayed by the retry pacing (the profit target's
    own stand-down rule)."""
    executor = SequencedExecutor([NOT_ATTEMPTED])
    watchdog, clock, market, executor, audit, _, _ = _stub_watchdog(
        config=_stub_on(profit_target_enabled=True), executor=executor
    )
    for _ in range(3):
        await _tick(watchdog, clock, market, _at_one_tick)
    assert watchdog._attempted[KEY].reasons == PROFIT
    _book_the_partial(watchdog)

    # The stop is TRUE but not yet sustained: the handoff is still the take,
    # which `close()` would refuse to build at this mark.
    await _tick(watchdog, clock, market, _credit_stop(clock))
    assert executor.stubs == [False, True]
    assert executor.calls[-1][1] == PROFIT
    assert KEY in watchdog._stub_retry_at, "restored, paced"

    # Well inside the pacing interval, the stop sustains and FIRES: it goes now.
    for _ in range(2):
        await _tick(watchdog, clock, market, _credit_stop(clock), step=1.0)
    assert executor.stubs == [False, True, True]
    assert executor.calls[-1][1] == CREDIT
    assert KEY in watchdog._stub_spent
    retry = audit.payload("watchdog.stub_close_retry")
    assert retry["origin_reasons"] == ["profit_target"]
    assert retry["handoff_reasons"] == ["credit_multiple"]


def test_the_handoff_reasons_are_the_origin_plus_what_is_urgent_now():
    """`_stub_handoff_reasons`, pure. The origin stands when nothing emergency
    fires; any emergency reason joins it and strips the profit target."""
    stop = TriggerReason.CREDIT_MULTIPLE
    backstop = TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE
    assert _stub_handoff_reasons(PROFIT, frozenset()) == PROFIT
    assert _stub_handoff_reasons(PROFIT, PROFIT) == PROFIT
    assert _stub_handoff_reasons(PROFIT, {stop}) == {stop}
    assert _stub_handoff_reasons(PROFIT, {stop, TriggerReason.PROFIT_TARGET}) == {stop}
    assert _stub_handoff_reasons(frozenset({FLATTEN}), frozenset()) == {FLATTEN}
    assert _stub_handoff_reasons(CREDIT, {backstop}) == {stop, backstop}


async def test_a_filled_continuation_drops_the_spread_and_the_page_stops():
    """§9.3.7. The ledger is the truth: once the closing fill books and the scan
    drops the spread, the spent row and its repeating page are gone."""
    watchdog, clock, market, executor, audit, alerts, _ = _stub_watchdog()
    await _a_stub(watchdog, clock, market)
    await _tick(watchdog, clock, market, _credit_stop(clock))
    assert len(executor.calls) == 2

    watchdog._spreads.scan = OpenSpreadScan()      # the fill booked
    watchdog.request_refresh()
    await _tick(watchdog, clock, market, _credit_stop(clock))

    assert watchdog.spent_exit_attempts() == ()
    assert not [k for k, _ in alerts if k == "watchdog_exit_spent_still_held"]


# -- the allowance: restores, their pacing and their bound -----------------------


class SequencedExecutor(RecordingExecutor):
    """Reports a scripted sequence AFTER the original: the FIRST call is the
    partial that leaves the stub (`FILLED`, as the real walk reports a partial),
    and each later call pops the next scripted `(outcome, walk_reason)` — the
    CONTINUATION's (or the flatten's) report. `walk_reason` set means the walk
    ended `BLOCKED` on that guard reason. Once the script runs out it repeats
    `tail` forever."""

    def __init__(self, script, *, tail=(ExitOutcome.FILLED, None)):
        super().__init__(ExitOutcome.FILLED)
        self._script = [(ExitOutcome.FILLED, None), *script]
        self._tail = tail

    async def close(self, spread_, market, reasons, *, stub=False):
        self.calls.append((spread_, reasons))
        self.stubs.append(stub)
        outcome, walk_reason = self._script.pop(0) if self._script else self._tail
        walk = None
        if walk_reason is not None:
            walk = WalkResult(
                outcome=WalkOutcome.BLOCKED,
                external_identifier=f"argus-close-{spread_.candidate_id}",
                broker_order_id=None,
                steps=0,
                requested_contracts=spread_.contracts,
                filled_contracts=None,
                final_limit_price=Decimal("-1.70"),
                cancel_requested=False,
                recovered_disconnects=0,
                reason=walk_reason,
            )
        return ExitReport(
            outcome=outcome,
            external_identifier=f"argus-close-{spread_.candidate_id}",
            reasons=tuple(sorted(reasons, key=str)),
            walk=walk,
            reason=None if walk is None else f"walk ended {walk.outcome}: {walk.reason}",
        )


NOT_ATTEMPTED = (ExitOutcome.NOT_ATTEMPTED, None)
WORKING_BLOCK = (ExitOutcome.NOT_RESOLVED, BLOCKED_WORKING)


async def test_not_attempted_restores_paced_and_records_tier1_once():
    """Review finding 1 (the storm half). A continuation that can never be built
    restores its allowance — but retries ONE refresh interval apart, not every
    tick, and the durable Tier 1 row is written ONCE: every later handoff of the
    same allowance is Tier 2 `watchdog.stub_close_retry`."""
    executor = SequencedExecutor([], tail=NOT_ATTEMPTED)
    watchdog, clock, market, executor, audit, alerts, _ = _stub_watchdog(
        executor=executor
    )
    await _a_stub(watchdog, clock, market)
    ticks, step = 40, 1.5
    for _ in range(ticks):
        await _tick(watchdog, clock, market, _credit_stop(clock), step=step)

    handoffs = len(_stub_calls(executor))
    assert handoffs >= 2, "restored, retried"
    assert handoffs <= math.ceil(ticks * step / REFRESH_S) + 1, "paced, not per tick"
    assert audit.events.count("watchdog.stub_close") == 1, "Tier 1 once"
    assert audit.events.count("tier2:watchdog.stub_close_retry") == handoffs - 1
    # One page for the episode, the rest Tier 2.
    assert audit.events.count("watchdog.stub_close_unattempted") == 1
    assert [k for k, _ in alerts].count("watchdog_stub_close_unattempted") == 1
    (row,) = watchdog.spent_exit_attempts()
    assert row.stub_close in ("available", "sent")


async def test_the_continuation_never_starves_the_flatten():
    """Review finding 1 (the starvation half), the probe that found it made a
    regression test: a continuation that keeps coming back `NOT_ATTEMPTED` used
    to re-hand-off every tick and return before `_flatten_may_act` was ever
    reached — 40 ticks past 15:30, zero flatten sends. Past the mark it now
    YIELDS to the flatten's unspent allowance."""
    et_clock = FakeETClock(hour=15, minute=0)
    executor = SequencedExecutor([], tail=NOT_ATTEMPTED)
    watchdog, clock, market, executor, audit, _, _ = _stub_watchdog(
        executor=executor, et_clock=et_clock
    )
    await _a_stub(watchdog, clock, market)
    await _tick(watchdog, clock, market, _credit_stop(clock))
    assert executor.stubs[-1] is True, "the continuation ran before the mark"

    et_clock.at(15, 31)
    before = len(executor.calls)
    for _ in range(20):
        await _tick(watchdog, clock, market, _credit_stop(clock))

    after_mark = list(zip(executor.calls[before:], executor.stubs[before:]))
    assert after_mark, "something was handed off past the mark"
    assert all(FLATTEN in reasons for (_, reasons), _ in after_mark)
    assert not any(stub for _, stub in after_mark), "the stub yields to the flatten"


async def test_the_continuation_resumes_once_the_flatten_override_is_spent():
    """The yield is to the flatten's UNSPENT allowance only. Once the override
    has gone out, a still-unspent stub allowance is not lost to it."""
    et_clock = FakeETClock(hour=15, minute=0)
    watchdog, clock, market, executor, audit, _, _ = _stub_watchdog(
        et_clock=et_clock
    )
    await _a_stub(watchdog, clock, market)
    # The override already went out on an earlier handoff (its own report is
    # not what this pins, so the spend is set directly).
    watchdog._flatten_attempted.add(KEY)
    et_clock.at(15, 31)

    await _tick(watchdog, clock, market, _credit_stop(clock))

    assert executor.stubs[-1] is True
    assert FLATTEN in executor.calls[-1][1], "the urgent reason rides the handoff"


async def test_a_working_block_restores_paced_and_bounded():
    """Review finding 3. The partial's own remainder still at `Cancel Requested`
    blocks the guard `conflicting_working_order` a second after the partial —
    and a `BLOCKED` walk sent nothing, whatever the guard's reason. So the
    allowance comes back, paced; once the cancel lands the next ask proceeds."""
    executor = SequencedExecutor([WORKING_BLOCK])
    watchdog, clock, market, executor, audit, alerts, _ = _stub_watchdog(
        executor=executor
    )
    await _a_stub(watchdog, clock, market)
    await _tick(watchdog, clock, market, _credit_stop(clock))
    assert executor.stubs == [False, True]
    assert KEY not in watchdog._stub_spent, "nothing was sent; restored"
    assert audit.payload("watchdog.stub_close_blocked")["guard_reason"] == (
        BLOCKED_WORKING
    )

    # Paced: the next tick does NOT ask again.
    await _tick(watchdog, clock, market, _credit_stop(clock))
    assert len(executor.calls) == 2
    # One refresh interval later it does, and the (now clear) ask fills.
    clock.advance(REFRESH_S)
    await _tick(watchdog, clock, market, _credit_stop(clock))
    assert executor.stubs == [False, True, True]
    assert KEY in watchdog._stub_spent
    assert not [k for k, _ in alerts if k == "watchdog_stub_close_exhausted"]


async def test_the_third_block_keeps_it_spent_and_pages_once():
    """§5.2's bound, now over every `BLOCKED`: three refusals span ~45s of
    pacing, an order of magnitude past a cancel landing or a fill booking. The
    third keeps the allowance spent and pages ONCE with the guard's reason; the
    first two are Tier 2, and none is paged by the executor."""
    executor = SequencedExecutor(
        [(ExitOutcome.NOT_RESOLVED, BLOCKED_CLOSE_EXCEEDED)] * _STUB_BLOCKED_RETRIES
    )
    watchdog, clock, market, executor, audit, alerts, _ = _stub_watchdog(
        executor=executor
    )
    await _a_stub(watchdog, clock, market)
    for _ in range(_STUB_BLOCKED_RETRIES):
        await _tick(watchdog, clock, market, _credit_stop(clock))
        clock.advance(REFRESH_S)
    assert len(_stub_calls(executor)) == _STUB_BLOCKED_RETRIES
    assert KEY in watchdog._stub_spent

    for _ in range(6):
        clock.advance(REFRESH_S)
        await _tick(watchdog, clock, market, _credit_stop(clock))
    assert len(_stub_calls(executor)) == _STUB_BLOCKED_RETRIES, "the bound is three"
    assert watchdog._stub_blocked[KEY] == _STUB_BLOCKED_RETRIES
    (row,) = watchdog.spent_exit_attempts()
    assert row.stub_close == "exhausted"
    assert audit.events.count("tier2:watchdog.stub_close_blocked") == 2
    assert audit.events.count("watchdog.stub_close_exhausted") == 1
    exhausted = [d for k, d in alerts if k == "watchdog_stub_close_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0]["guard_reason"] == BLOCKED_CLOSE_EXCEEDED
    assert exhausted[0]["candidate_id"] == spread().candidate_id
    assert audit.events.count("watchdog.stub_close") == 1


async def test_the_flatten_survives_an_exhausted_stub():
    """§9.3.5. After the stub allowance is exhausted, the flatten's one-shot
    override remains available behind it, exactly as for any spent position."""
    et_clock = FakeETClock(hour=15, minute=0)
    executor = SequencedExecutor([WORKING_BLOCK] * _STUB_BLOCKED_RETRIES)
    watchdog, clock, market, executor, audit, alerts, _ = _stub_watchdog(
        executor=executor, et_clock=et_clock
    )
    await _a_stub(watchdog, clock, market)
    for _ in range(_STUB_BLOCKED_RETRIES):
        await _tick(watchdog, clock, market, _credit_stop(clock))
        clock.advance(REFRESH_S)
    assert watchdog.spent_exit_attempts()[0].stub_close == "exhausted"

    et_clock.at(15, 31)
    await _tick(watchdog, clock, market, _credit_stop(clock))
    assert FLATTEN in executor.calls[-1][1]
    assert executor.stubs[-1] is False
    assert KEY in watchdog._flatten_attempted, "the override is spent by its send"


async def test_the_unattempted_page_falls_so_a_new_episode_pages_again():
    """Review finding 10. The stub's `NOT_ATTEMPTED` edge falls when a
    continuation reaches the guard, so a later, separate nothing-was-built
    episode on the same position pages again instead of going quiet."""
    executor = SequencedExecutor([NOT_ATTEMPTED, WORKING_BLOCK, NOT_ATTEMPTED])
    watchdog, clock, market, executor, audit, alerts, _ = _stub_watchdog(
        executor=executor
    )
    await _a_stub(watchdog, clock, market)
    for _ in range(3):
        await _tick(watchdog, clock, market, _credit_stop(clock))
        clock.advance(REFRESH_S)

    assert executor.stubs == [False, True, True, True]
    pages = [k for k, _ in alerts if k == "watchdog_stub_close_unattempted"]
    assert len(pages) == 2


async def test_the_stub_path_never_touches_the_emergency_allowances():
    """§9.3.6, as state assertions; the AST half is below. Whatever a stub
    continuation reports, the original spent row keeps describing the original
    partial, and every other allowance is untouched."""
    watchdog, clock, market, executor, audit, alerts, _ = _stub_watchdog()
    await _a_stub(watchdog, clock, market)
    original = watchdog._attempted[KEY]
    await _tick(watchdog, clock, market, _credit_stop(clock))

    assert executor.stubs[-1] is True
    assert watchdog._attempted[KEY] == original, "outcome, since AND reasons intact"
    assert watchdog._sends == {}
    assert watchdog._flatten_attempted == set()
    assert watchdog._profit_spent == {}

    # And on a crash: the allowance was spent at the handoff, the crash does
    # not grant, and `_attempted` is untouched (§5.4).
    watchdog, clock, market, executor, audit, alerts, halts = _stub_watchdog()
    await _a_stub(watchdog, clock, market)
    original = watchdog._attempted[KEY]
    executor.raises = RuntimeError("boom")
    await _tick(watchdog, clock, market, _credit_stop(clock))
    assert watchdog._attempted[KEY] == original
    assert KEY in watchdog._stub_spent, "a crash must not grant"
    crash = audit.payload("watchdog.exit_crashed")
    assert crash["stub_close"] is True and crash["exit_attempt_intact"] is False
    assert halts and halts[0]["grade"].name == "ENTRIES"


async def test_off_is_exactly_todays_behaviour():
    """§9.3.9. `stub_close_enabled = false`: no handoff, no record — the state
    is today's, and the row says `disabled`. (The guard half of the switch is
    pinned in `test_order_guard.py`.)"""
    watchdog, clock, market, executor, audit, alerts, _ = _stub_watchdog(
        config=make_config(watchdog={"stub_close_enabled": False})
    )
    await _a_stub(watchdog, clock, market)
    for _ in range(5):
        await _tick(watchdog, clock, market, _credit_stop(clock))

    assert len(executor.calls) == 1
    assert "watchdog.stub_close" not in audit.events
    assert watchdog._stub_spent == set()
    (row,) = watchdog.spent_exit_attempts()
    assert row.stub_close == "disabled"


async def test_a_non_finite_mark_never_reaches_the_tier1_record(monkeypatch):
    """Review finding 11. The durable record's numbers go through
    `payload_number`: a non-finite value in a Tier 1 payload is a REFUSED write,
    which here would surface as a watchdog exception and halt entries."""
    real = wd.evaluate_triggers

    def nan_mark(*args, **kwargs):
        evaluation = real(*args, **kwargs)
        return dataclasses.replace(
            evaluation, cost_to_close=math.nan, credit_stop=math.inf, spot=-math.inf
        )

    watchdog, clock, market, executor, audit, _, _ = _stub_watchdog()
    await _a_stub(watchdog, clock, market)
    monkeypatch.setattr(wd, "evaluate_triggers", nan_mark)
    await _tick(watchdog, clock, market, _quiet)

    detail = audit.payload("watchdog.stub_close")
    assert (detail["cost_to_close"], detail["credit_stop"], detail["spot"]) == (
        None, None, None,
    )


# -- the boot read ---------------------------------------------------------------


class ReadBackAudit(FakeAudit):
    """A FakeAudit whose `.read` serves canned `watchdog.stub_close` rows."""

    def __init__(self, payloads=()):
        super().__init__()
        self.payloads = list(payloads)

    async def read(self, fn):
        payloads = self.payloads

        class _Conn:
            def execute(self, sql, params=()):
                class _Rows:
                    def __init__(self, rows):
                        self.rows = rows

                    def fetchall(self):
                        return [{"payload": p} for p in self.rows]

                return _Rows(payloads)

        return fn(_Conn())


async def test_rehydrate_seeds_the_allowance_from_todays_rows():
    """§9.3.8. Today's `watchdog.stub_close` row seeds the allowance spent, so
    the spent position pages on without a second allowance-backed send."""
    watchdog, clock, market, executor, audit, alerts, _ = _stub_watchdog(
        audit=ReadBackAudit([f'{{"external_identifier": "{KEY}"}}'])
    )
    await watchdog.rehydrate_stub_close()
    assert watchdog._stub_spent == {KEY}
    assert watchdog._stub_recorded == {KEY}

    await _a_stub(watchdog, clock, market)
    await _tick(watchdog, clock, market, _credit_stop(clock))
    assert len(executor.calls) == 1, "no allowance-backed send on top"


async def test_rehydrate_without_rows_leaves_the_allowance_available():
    watchdog, *_ = _stub_watchdog(audit=ReadBackAudit())
    await watchdog.rehydrate_stub_close()
    assert watchdog._stub_spent == set()
    assert watchdog._stub_close_unreadable is False


@pytest.mark.parametrize(
    "row",
    [
        "not json",
        '{"close_identifier": "argus-close-20260729-put-x"}',   # no position key
        f'{{"external_identifier": "{CLOSE_KEY}"}}',            # not an OPEN key
        '{"external_identifier": "hand-placed"}',                # not Argus's
    ],
)
async def test_an_unreadable_boot_fails_toward_spent_and_pages_once(row):
    """§9.3.8/§5.5, and review finding 8's other half: the risk package returns
    raw keys and the WATCHDOG applies the grammar — a key it cannot parse as an
    opening order is as unreadable as a row that is not JSON. The kill-state
    posture: no halt, no continuation until a readable boot, one page."""
    watchdog, clock, market, executor, audit, alerts, halts = _stub_watchdog(
        audit=ReadBackAudit([row])
    )
    await watchdog.rehydrate_stub_close()

    assert watchdog._stub_close_unreadable is True
    assert "watchdog.stub_close_unreadable" in audit.events
    assert [k for k, _ in alerts] == ["watchdog_stub_close_unreadable"]
    assert halts == [], "fail closed without halting"

    await _a_stub(watchdog, clock, market)
    await _tick(watchdog, clock, market, _credit_stop(clock))
    assert len(executor.calls) == 1, "no continuation may fire"
    (row_,) = watchdog.spent_exit_attempts()
    assert row_.stub_close == "disabled"


async def test_the_boot_read_is_day_scoped_and_keys_by_position(audit: AuditLog):
    """§9.3.8. The read is bounded to one ET day and returns the POSITION keys
    the rows name."""
    audit.start()
    await audit.record(
        STUB_CLOSE_CATEGORY,
        {"external_identifier": KEY, "close_identifier": CLOSE_KEY},
    )
    await audit.aclose()

    assert await load_stub_close_spent(audit, trading_date=et_today()) == {KEY}
    # A yesterday query does not see today's row: the allowance is per day by
    # construction, the same reason the kill state needs no date bound.
    yesterday = et_today() - timedelta(days=1)
    assert await load_stub_close_spent(audit, trading_date=yesterday) == frozenset()


async def test_an_unreadable_row_refuses_rather_than_skips(audit: AuditLog):
    """A row that exists but cannot be parsed names a position whose allowance
    is spent — dropping it would hand a crash loop a fresh budget for exactly
    that position."""
    audit.start()
    await audit.record(STUB_CLOSE_CATEGORY, {"external_identifier": KEY})
    await audit.aclose()
    audit._conn.execute(
        f"UPDATE audit_log SET payload = 'not json' WHERE category = '{STUB_CLOSE_CATEGORY}'"
    )

    with pytest.raises(StubCloseRehydrationError, match="unreadable"):
        await load_stub_close_spent(audit, trading_date=et_today())


async def test_a_row_without_a_position_key_refuses(audit: AuditLog):
    audit.start()
    await audit.record(STUB_CLOSE_CATEGORY, {"close_identifier": CLOSE_KEY})
    await audit.aclose()

    with pytest.raises(StubCloseRehydrationError, match="unreadable"):
        await load_stub_close_spent(audit, trading_date=et_today())


def test_the_rehydrate_module_imports_nothing_from_execution():
    """Review finding 8. `risk/rehydrate.py` keeps no dependency on the
    execution package — the FILL_CATEGORY rule its own comments state, and the
    premise of the watchdog's import comment."""
    tree = ast.parse((_SRC / "risk" / "rehydrate.py").read_text())
    imported = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not [m for m in imported if m.startswith("argus.execution")], imported


# -- end to end: the real executor, guard and walk (§9.4) ------------------------


class ScriptedFills:
    """Mixin for the exit harness's Broker: fills each submitted order, per a
    plan of `order_id -> contracts`, the first time it is read LIVE."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def script_fills(self, plan: dict[int, int | None]) -> None:
        self._fill_plan = dict(plan)

    async def get_live_orders(self):
        for order_id, contracts in list(getattr(self, "_fill_plan", {}).items()):
            order = self._orders.get(order_id)
            if order is not None and order.status is OrderStatus.LIVE:
                self.fill(order_id, contracts=contracts)
                del self._fill_plan[order_id]
        return await super().get_live_orders()


def _e2e(*, stub_close_enabled: bool = True):
    """The real executor over the real walk and guard, on a NEAR-WORTHLESS book
    (a take is sent only at the cheapest expressible limit) and a broker that
    fills to a script. The guard's rule rides the switch, as in production."""
    from test_watchdog_exit import Broker, build, held

    class ScriptedBroker(ScriptedFills, Broker):
        pass

    executor, broker, audit, quotes, clock, alerts, slept = build(
        broker=ScriptedBroker(),
        short=(0.10, 0.15),
        long_=(0.05, 0.10),
        config=make_config(watchdog={"stub_close_enabled": stub_close_enabled}),
    )
    return executor, broker, audit, quotes, held, alerts


async def test_end_to_end_a_partial_leaves_a_stub_and_the_continuation_closes_it():
    """§9.4's happy path, on the REAL `ExitExecutor` + `DuplicateOrderGuard` +
    `PriceWalkingLoop` + `FakeOrderExecution`: the take fills 1 of 2, the
    remainder is cancelled, and the ONE further close of the booked stub is
    permitted by the broker's own fills — with the permission in the Tier 1
    trail — and closes it."""
    from test_watchdog_exit import CREDIT_TRIGGER

    executor, broker, audit, quotes, held, _ = _e2e()
    broker.script_fills({1000: 1})                       # the take: 1 of 2

    take = await executor.close(held(), quotes.market(), PROFIT)
    assert take.outcome is ExitOutcome.FILLED
    assert broker.order(1000).status is OrderStatus.CANCELLED   # remainder cancelled

    # The booked stub: ONE further close, sized 1, permitted by arithmetic.
    broker.script_fills({1001: None})                    # the continuation: full
    report = await executor.close(
        held(contracts=1), quotes.market(), CREDIT_TRIGGER, stub=True
    )
    assert report.outcome is ExitOutcome.FILLED

    permitted = audit.payload("order.duplicate_close_permitted")
    assert permitted["quantity"]["asked"] == 1
    assert permitted["quantity"]["allowance"] == 1
    assert permitted["quantity"]["closed_filled"] == {
        "SPXW  260729P04995000": 1,
        "SPXW  260729P05000000": 1,
    }
    # The position is FLAT at the broker: opened 2 == closed 1 + 1, per leg.
    from test_watchdog_exit import CANDIDATE

    e2e_close_id = f"argus-close-{CANDIDATE}"
    closed: dict[str, int] = {}
    for order in (await broker.get_live_orders()).orders:
        if order.external_identifier != e2e_close_id:
            continue
        for leg in order.legs:
            closed[leg.symbol] = closed.get(leg.symbol, 0) + (leg.filled_quantity or 0)
    assert closed == {"SPXW  260729P04995000": 2, "SPXW  260729P05000000": 2}


async def test_end_to_end_a_stale_scan_is_refused_by_arithmetic():
    """§9.4's inversion guard: the same world, but the scan still fed the FULL
    2-contract spread — `close_quantity_exceeded`, no order, the pages keep
    their meaning."""
    from test_watchdog_exit import CREDIT_TRIGGER

    executor, broker, audit, quotes, held, _ = _e2e()
    broker.script_fills({1000: 1})
    take = await executor.close(held(), quotes.market(), PROFIT)
    assert take.outcome is ExitOutcome.FILLED
    submitted_before = len(broker.submitted)

    stale = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)

    assert stale.outcome is ExitOutcome.NOT_RESOLVED
    assert stale.walk.outcome is WalkOutcome.BLOCKED
    assert BLOCKED_CLOSE_EXCEEDED in stale.reason
    assert len(broker.submitted) == submitted_before, "nothing was routed"
    blocked = audit.payload("order.duplicate_blocked")
    assert blocked["reason"] == BLOCKED_CLOSE_EXCEEDED
    assert blocked["quantity"]["allowance"] == 1


async def test_end_to_end_a_pending_remainder_blocks_the_stub_ask_unpaged():
    """Review findings 3 and 4, on the real executor and guard: the partial's
    remainder still WORKING under the close identifier blocks the stub's ask
    `conflicting_working_order` — nothing routed — and a STUB ask the guard
    refused is recorded Tier 1 but NOT paged (the watchdog restores and re-asks
    it). The same refusal on an ordinary exit still pages, unchanged."""
    from test_watchdog_exit import CANDIDATE, CREDIT_TRIGGER, LONG_OCC, SHORT_OCC

    for stub in (True, False):
        executor, broker, audit, quotes, held, alerts = _e2e()
        remainder = Order(
            direction=OrderDirection.CLOSE,
            underlying="SPXW",
            legs=(
                OrderLeg(SHORT_OCC, LegAction.BUY_TO_CLOSE, 1),
                OrderLeg(LONG_OCC, LegAction.SELL_TO_CLOSE, 1),
            ),
            limit_price=Decimal("-0.05"),
            external_identifier=f"argus-close-{CANDIDATE}",
        )
        await broker.submit(remainder, dry_run=False)    # sits working
        submitted_before = len(broker.submitted)

        report = await executor.close(
            held(contracts=1), quotes.market(), CREDIT_TRIGGER, stub=stub
        )

        assert report.walk.outcome is WalkOutcome.BLOCKED
        assert report.walk.reason == BLOCKED_WORKING
        assert len(broker.submitted) == submitted_before
        assert audit.payload("watchdog.exit_finished")["stub_close"] is stub
        paged = [k for k, _ in alerts if k == "watchdog_exit_not_resolved"]
        assert bool(paged) is (not stub)


# -- structural: what a convention would let drift (§9.5) ------------------------


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _writes(node: ast.AST) -> set[str]:
    """`self.<attr>` names this subtree WRITES (the profit suite's helper,
    restated locally so this module owns its sweep)."""
    out: set[str] = set()

    def self_attr(expr) -> str | None:
        if isinstance(expr, ast.Subscript):
            expr = expr.value
        if (
            isinstance(expr, ast.Attribute)
            and isinstance(expr.value, ast.Name)
            and expr.value.id == "self"
        ):
            return expr.attr
        return None

    for n in ast.walk(node):
        targets = []
        if isinstance(n, ast.Assign):
            targets = n.targets
        elif isinstance(n, ast.AugAssign | ast.AnnAssign):
            targets = [n.target]
        elif isinstance(n, ast.Delete):
            targets = n.targets
        for t in targets:
            if (name := self_attr(t)) is not None:
                out.add(name)
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"add", "discard", "pop", "update", "clear",
                                "setdefault", "remove"}
            and (name := self_attr(n.func.value)) is not None
        ):
            out.add(name)
    return out


_EMERGENCY_STATE = {"_attempted", "_sends", "_flatten_attempted", "_profit_spent"}


def test_the_stub_path_writes_no_emergency_allowance():
    """§9.5: the profit-path allowance contract, extended to name the stub
    path's single write. No stub-path function writes an emergency allowance,
    and `_stub_spent` has exactly the writers the design names."""
    tree = ast.parse((_SRC / "execution" / "watchdog.py").read_text())
    fns = _functions(tree)
    for name in ("_maybe_stub_close", "_after_stub_close", "_restore_stub_allowance",
                 "_stub_close_state", "rehydrate_stub_close", "spent_exit_attempts"):
        assert not (_writes(fns[name]) & _EMERGENCY_STATE), name

    writers = {n for n, fn in fns.items() if "_stub_spent" in _writes(fn)}
    assert writers == {
        "__init__", "_maybe_stub_close", "_restore_stub_allowance", "rehydrate_stub_close",
    }, "the spend, the restore, the boot seed — nothing else"
    # And the durable-once set: written at the first handoff and the boot seed.
    recorders = {n for n, fn in fns.items() if "_stub_recorded" in _writes(fn)}
    assert recorders == {"__init__", "_maybe_stub_close", "rehydrate_stub_close"}


def test_the_restore_is_reached_only_from_the_stub_report_path():
    """§9.5: the `NOT_ATTEMPTED`-sites pin, extended. The allowance comes back
    only inside `_after_stub_close` — a VALUE-driven restore on a report, never
    a presence-driven one on a scan — and exactly twice: the two proven-no-send
    values (`NOT_ATTEMPTED`, and a walk the guard `BLOCKED`)."""
    tree = ast.parse((_SRC / "execution" / "watchdog.py").read_text())
    fns = _functions(tree)
    callers = {
        name
        for name, fn in fns.items()
        if any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_restore_stub_allowance"
            for n in ast.walk(fn)
        )
    }
    assert callers == {"_after_stub_close"}
    restores = [
        n
        for n in ast.walk(fns["_after_stub_close"])
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_restore_stub_allowance"
    ]
    assert len(restores) == 2, "NOT_ATTEMPTED, and the guard's BLOCKED"


def test_only_one_walk_outcome_is_blocked_and_it_precedes_any_submit():
    """The premise the `BLOCKED` restore stands on: `WalkOutcome.BLOCKED` is
    produced at exactly ONE site in the walk — the guard's refusal, before the
    first submission — so a `BLOCKED` report can never describe a sent order."""
    tree = ast.parse((_SRC / "execution" / "loop.py").read_text())
    sites = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and n.attr == "BLOCKED"
        and isinstance(n.value, ast.Name)
        and n.value.id == "WalkOutcome"
    ]
    fns = _functions(tree)
    in_run = [
        n for n in ast.walk(fns["run"])
        if isinstance(n, ast.Attribute) and n.attr == "BLOCKED"
    ]
    # The frozen classification sets name it too; the only PRODUCER is `run`.
    producers = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and any(
            isinstance(a, ast.Attribute) and a.attr == "BLOCKED"
            for a in n.args
        )
    ]
    assert sites and len(in_run) == 1 and len(producers) == 1
    assert producers[0] in list(ast.walk(fns["run"]))


def test_quantity_reaches_the_guard_only_through_the_exit_seam():
    """§9.5: the guard's `quantity` parameter is asserted absent from every
    non-`loop._ask_guard` call path — the entry path's own guard burst stays
    binary, and no other caller grows a quantity rule by accident."""
    offenders: list[str] = []
    for path in sorted((_SRC).rglob("*.py")):
        tree = ast.parse(path.read_text())
        fns = _functions(tree)
        for name, fn in fns.items():
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "has_conflicting_order"
                    and any(kw.arg == "quantity" for kw in node.keywords)
                    and name != "_ask_guard"
                ):
                    offenders.append(f"{path.name}:{node.lineno} ({name})")
    assert not offenders, offenders


def test_the_category_literals_agree():
    """The writer's and the reader's literals are one category — the
    FILL_CATEGORY precedent, pinned rather than trusted."""
    import argus.risk.rehydrate as rh

    assert wd.STUB_CLOSE_CATEGORY == rh.STUB_CLOSE_CATEGORY == "watchdog.stub_close"


def test_the_profiles_ship_the_stub_close_on():
    """§5.6: `true` in all three profiles — the emergency-stub exposure exists
    today with the profit target off, and the gate this build discharges is the
    profit target's promotion."""
    import tomllib

    from test_config_profiles import PROFILE_FILES

    for profile, path in PROFILE_FILES.items():
        raw = tomllib.loads(path.read_text())
        value = raw["watchdog"]["stub_close_enabled"]
        assert value is True, f"{profile} ships {value!r}"
        assert Config.load(path).watchdog.stub_close_enabled is True


def test_the_switch_is_a_strict_bool():
    """A hand-edited `stub_close_enabled = 1` or `"true"` is a ConfigError at
    boot, not a live behaviour nobody chose."""
    from support import config_dict

    for bad in (1, "true", 0, "", None):
        with pytest.raises(ConfigError):
            Config.from_dict(config_dict(watchdog={"stub_close_enabled": bad}))


def test_the_seam_carries_the_stub_state():
    """§9.5: `SpentExitAttempt` -> `UnclosablePosition` names the new field —
    the 2026-09-02 under-copy rule, pinned here for the row this build adds."""
    from argus.session import _to_unclosable
    from argus.status import UnclosablePosition

    source = SpentExitAttempt(
        external_identifier=KEY,
        candidate_id=spread().candidate_id,
        contracts=1,
        outcome="filled",
        spent_for_s=12.0,
        stub_close="sent",
        credit_trigger_enabled=True,
    )
    (row,) = _to_unclosable([source])
    assert isinstance(row, UnclosablePosition)
    assert row.stub_close == "sent"
    # The field exists on the target (the order of fields is not the contract —
    # 2026-09-25 added the working-close fields after it).
    assert "stub_close" in {f.name for f in dataclasses.fields(UnclosablePosition)}
