"""The profit target — the fourth trigger, and the only one that is not an emergency.

`docs/decisions/2026-09-22-profit-target-and-re-entry.md` (§10, §11) is the design.
What that record settled, and what these tests hold:

  * the trigger is `round_to_tick(cost_to_close) == one tick` — buildable by
    construction, never firing where `build._limit_price` would refuse;
  * it is EVALUATED always (so `blind` stays reachable with the switch off) and ACTS
    only when `watchdog.profit_target_enabled`, when nothing else fired, when the
    emergency exit is unspent, and until its own two-send budget retires it;
  * it NEVER crosses the market: an unfilled take is settled through
    `_await_settled`'s positive confirmation and reported retryable, or retired;
  * and nothing it reports can spend `_attempted`, `_sends` or the flatten's
    override — a failed take must cost nothing, or the feature manufactures the
    uncovered tail it exists to remove (§11.1).

Two harnesses are reused rather than rebuilt: `test_watchdog`'s recording executor for
WHEN the watchdog acts, and `test_watchdog_exit`'s real executor over the real walk and
a fake broker for HOW a take reaches the market.
"""

from __future__ import annotations

import ast
import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest

import argus.execution.watchdog as wd
from argus.execution.build import UnpriceableSpreadError
from argus.execution.loop import PriceWalk, WalkOutcome, WalkPolicy, WalkResult
from argus.execution.models import OrderStatus
from argus.execution.store import OpenSpreadScan
from argus.execution.watchdog import (
    PROFIT_TARGET_MARK,
    ExitOutcome,
    ExitReport,
    TriggerReason,
    _EMERGENCY_TRIGGER_REASONS,
    _INSTANT_TRIGGER_REASONS,
    _MARKET_TRIGGER_REASONS,
    _PROFIT_RETIRE,
    _PROFIT_SEND_ATTEMPTS,
    _PROFIT_SETTLE,
    at_profit_target,
    evaluate_triggers,
)
from argus.reconcile import HaltGrade
from tests.support import make_config
from tests.test_watchdog import (
    LONG_STREAM,
    SHORT_STREAM,
    FakeClock,
    FakeETClock,
    RecordingExecutor,
    _LONG_BEFORE_FLATTEN,
    _triggered_and_priceable,
    build_quotes,
    build_watchdog,
    spread,
    thresholds,
)
from tests.test_watchdog_exit import (
    CREDIT_TRIGGER,
    Broker,
    build as build_executor,
    held,
)

PROFIT = frozenset({TriggerReason.PROFIT_TARGET})
_SRC = Path(__file__).resolve().parents[1] / "src" / "argus" / "execution"


def _on(**watchdog):
    """A config with the switch ON (and any other `[watchdog]` overrides)."""
    return make_config(watchdog={"profit_target_enabled": True, **watchdog})


def _at_one_tick(market, *, spot: float = 5100.0) -> None:
    """Both legs fresh, net mid EXACTLY one tick, spot far from the short strike.

    Short 0.10/0.15 floors to a 0.125 mid, long 0.05/0.10 to 0.075: net 0.05, the
    cheapest closing limit there is — and a crossing debit of 0.10, one tick over it,
    which is the modal near-worthless book the record measured (§10).
    """
    market.set(SHORT_STREAM, 0.10, 0.15)
    market.set(LONG_STREAM, 0.05, 0.10)
    market.set("SPX", spot, spot)


def _credit_stop(clock):
    """A setter that keeps the credit stop true on a fresh tick."""
    return lambda market: _triggered_and_priceable(clock, market)


async def _tick(watchdog, clock, market, setter=_at_one_tick, *, step: float = 1.5):
    """One cadence tick on fresh quotes, with any exit it hands off run to the end."""
    clock.advance(step)
    setter(market)
    await watchdog.check_once()
    await watchdog._drain_exits()


def _profit_watchdog(*, outcome=ExitOutcome.FILLED, config=None, et_clock=None):
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    executor = RecordingExecutor(outcome)
    watchdog, audit, alerts, halts = build_watchdog(
        clock=clock,
        quotes=quotes,
        scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor,
        config=config or _on(),
        et_clock=et_clock,
    )
    return watchdog, clock, market, executor, audit, alerts, halts


# -- the trigger: buildability at the minimum tick ----------------------------


@pytest.mark.parametrize(
    ("mid", "fires"),
    [
        (0.0, False),        # rounds to 0.00: `_limit_price` refuses — never fire here
        (0.024, False),      # still rounds to 0.00
        (0.025, True),       # ROUND_HALF_UP: builds at one tick
        (0.05, True),
        (0.0749, True),
        (0.075, False),      # rounds to 0.10 — past the point the take is free
        (0.10, False),
        (-0.05, False),      # a crossed book is not a winning spread
    ],
)
def test_the_trigger_is_exactly_the_cheapest_expressible_limit(mid, fires):
    assert at_profit_target(mid) is fires
    assert PROFIT_TARGET_MARK == Decimal("0.05")


@pytest.mark.parametrize(
    "mark", [1e22, 1e300, 1.7976931348623157e308, float("inf"), float("-inf"),
             float("nan"), 0.1, -1e300]
)
def test_the_trigger_is_total_over_every_float(mark):
    """Finding 4 of the 2026-09-23 review. It runs AFTER the credit stop has been
    decided in the same function, so it must never raise: `round_to_tick` throws
    `InvalidOperation` for a finite mark near 1e22 and `ValueError` for a non-finite
    one, and before the band check either would have discarded an evaluation whose
    stop was true."""
    assert at_profit_target(mark) is False


def test_an_absurd_mark_fires_the_stop_instead_of_crashing_the_evaluation():
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 1e24, 3e24)      # one corrupt leg quote, still finite
    market.set(LONG_STREAM, 0.30, 0.50)
    market.set("SPX", 5100.0, 5100.0)
    ev = evaluate_triggers(
        spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN
    )
    assert TriggerReason.CREDIT_MULTIPLE in ev.true_reasons
    assert TriggerReason.PROFIT_TARGET not in ev.true_reasons


def test_the_trigger_fires_exactly_where_the_close_builds_at_one_tick():
    """§10's correction, held over the whole near-worthless zone rather than argued.

    The first draft of the record triggered on `crossing_debit <= 0.05`, which fires
    on 0 of 5,010 buildable ticks — an arithmetic empty set. The corrected trigger IS
    buildability at the minimum tick, so it must be true exactly where the executor's
    own builder produces a one-tick close, and never where it refuses.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    executor, *_ = build_executor()
    grid = [round(0.05 * i, 2) for i in range(0, 7)]     # 0.00 .. 0.30, on the tick grid
    checked = fired = refused = 0
    for s_bid in grid:
        for s_ask in grid:
            for l_bid in grid:
                for l_ask in grid:
                    if s_ask < s_bid or l_ask < l_bid:
                        continue
                    market.set(SHORT_STREAM, s_bid, s_ask)
                    market.set(LONG_STREAM, l_bid, l_ask)
                    market.set("SPX", 5100.0, 5100.0)
                    ev = evaluate_triggers(
                        spread(), quotes, thresholds(),
                        seconds_to_flatten=_LONG_BEFORE_FLATTEN,
                    )
                    triggered = TriggerReason.PROFIT_TARGET in ev.true_reasons
                    try:
                        order = executor._build_exit_order(spread(), ev.market)
                    except UnpriceableSpreadError:
                        refused += 1
                        assert not triggered, (s_bid, s_ask, l_bid, l_ask)
                        continue
                    checked += 1
                    fired += triggered
                    assert triggered is (order.limit_price == -PROFIT_TARGET_MARK), (
                        s_bid, s_ask, l_bid, l_ask, order.limit_price,
                    )
    # The sweep must actually visit all three regions, or it proves nothing.
    assert fired and refused and checked > fired


def test_the_profit_target_is_unknown_exactly_when_the_credit_stop_is():
    """It reads the same mid in the same branch, so the two are always evaluable or
    unknown TOGETHER — which is why joining `_MARKET_TRIGGER_REASONS` leaves `blind`'s
    superset test meaning what it meant (§11.9)."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    # Both legs priced -> both evaluable.
    _at_one_tick(market)
    ev = evaluate_triggers(
        spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN
    )
    assert not ev.unknown_reasons
    # A leg lost -> both unknown, never one without the other.
    market.drop(LONG_STREAM)
    ev = evaluate_triggers(
        spread(), quotes, thresholds(), seconds_to_flatten=_LONG_BEFORE_FLATTEN
    )
    assert {TriggerReason.CREDIT_MULTIPLE, TriggerReason.PROFIT_TARGET} <= ev.unknown_reasons
    assert TriggerReason.PROFIT_TARGET in _MARKET_TRIGGER_REASONS


def test_blind_stays_reachable_with_the_switch_off():
    """Evaluating the trigger only while ENABLED would leave it out of `unknown` while
    off, and `blind` — the one evaluability state that pages — would then be false for
    every input. The switch gates the action, never the evaluation."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    ev = evaluate_triggers(
        spread(), quotes, thresholds(profit_target_enabled=False),
        seconds_to_flatten=_LONG_BEFORE_FLATTEN,
    )   # no quotes at all, no spot
    assert ev.blind is True


def test_the_profit_target_is_debounced_and_is_not_an_emergency():
    assert TriggerReason.PROFIT_TARGET not in _INSTANT_TRIGGER_REASONS
    assert _EMERGENCY_TRIGGER_REASONS == (
        frozenset(TriggerReason) - {TriggerReason.PROFIT_TARGET}
    )


# -- the switch -----------------------------------------------------------------


async def test_off_the_trigger_is_recorded_and_nothing_is_sent():
    """`false` is the shipped value, and what it must mean is OFF: no executor call,
    no trigger record, no page — while `watchdog.check` still carries the reason, which
    is the replay input §11.6 said an observe mode would have produced for nothing."""
    watchdog, clock, market, executor, audit, alerts, _ = _profit_watchdog(
        config=make_config()
    )
    for _ in range(5):
        await _tick(watchdog, clock, market)

    assert executor.calls == []
    assert "watchdog.triggered" not in audit.events
    assert alerts == []
    checks = [p for c, p in audit.payloads if c == "watchdog.check"]
    assert checks[-1]["true_reasons"] == ["profit_target"]
    assert checks[-1]["sustaining"]["profit_target"] >= 2.0


async def test_on_a_sustained_one_tick_mark_is_taken_once_and_quietly():
    watchdog, clock, market, executor, audit, alerts, _ = _profit_watchdog()
    await _tick(watchdog, clock, market)
    await _tick(watchdog, clock, market)
    assert executor.calls == [], "debounced at sustained_s like any market trigger"
    await _tick(watchdog, clock, market)

    assert [reasons for _, reasons in executor.calls] == [PROFIT]
    assert "watchdog.triggered" in audit.events, "the episode is still Tier 1"
    # The stop-loss page is for the stop loss. A take pages when it CLOSES the
    # position, and that page is the executor's (`watchdog_exit_filled`).
    assert not [k for k, _ in alerts if k == "watchdog_triggered"]
    assert watchdog.profit_target_state().taken == 1


# -- precedence: it is never the reason a spread is closed the gentle way -------


def test_an_emergency_reason_strips_the_profit_target():
    watchdog, *_ = _profit_watchdog()
    key = spread().external_identifier
    both = frozenset({TriggerReason.PROFIT_TARGET, TriggerReason.FLATTEN_BEFORE_CLOSE})
    assert watchdog._may_act_on(key, both) == {TriggerReason.FLATTEN_BEFORE_CLOSE}
    assert watchdog._may_act_on(key, PROFIT) == PROFIT
    # Only ever REMOVES the profit target: emergency sets pass through untouched.
    for reason in _EMERGENCY_TRIGGER_REASONS:
        assert watchdog._may_act_on(key, frozenset({reason})) == {reason}


async def test_a_backstop_breach_at_one_tick_exits_as_an_emergency():
    """Both conditions sustained on the same tick: the exit is the emergency one, with
    its cross and its one attempt, and the profit target is not in the reasons."""
    watchdog, clock, market, executor, *_ = _profit_watchdog()

    def near_strike(m):
        _at_one_tick(m, spot=5003.0)     # 3 points above a 5000 put, buffer 5.0

    for _ in range(3):
        await _tick(watchdog, clock, market, near_strike)
    assert [reasons for _, reasons in executor.calls] == [
        frozenset({TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE})
    ]


async def test_a_spent_position_never_gets_a_profit_take():
    """§11.4 B's precondition, kept: a spent position's only remaining action is the
    flatten's one-shot override, and a profit take is not a way around that rule."""
    watchdog, clock, market, executor, *_ = _profit_watchdog(
        outcome=ExitOutcome.NOT_RESOLVED
    )
    for _ in range(3):
        await _tick(watchdog, clock, market, _credit_stop(clock))
    assert len(executor.calls) == 1 and watchdog._attempted

    for _ in range(5):
        await _tick(watchdog, clock, market)     # now a one-tick winner
    assert len(executor.calls) == 1


# -- the budget, and the allowances it must never touch -------------------------


def _emergency_allowances(watchdog):
    return (dict(watchdog._attempted), dict(watchdog._sends), set(watchdog._flatten_attempted))


@pytest.mark.parametrize("outcome", [ExitOutcome.UNFILLED, ExitOutcome.NOTHING_AT_BROKER])
async def test_two_benign_misses_retire_the_take_and_nothing_else(outcome):
    watchdog, clock, market, executor, audit, alerts, _ = _profit_watchdog(
        outcome=outcome
    )
    for _ in range(8):
        await _tick(watchdog, clock, market)

    assert len(executor.calls) == _PROFIT_SEND_ATTEMPTS == 2
    assert _emergency_allowances(watchdog) == ({}, {}, set())
    assert "tier2:watchdog.profit_target_retry" in audit.events
    retired = audit.payload("watchdog.profit_target_retired")
    assert retired["order_may_be_working"] is False
    assert not [k for k, _ in alerts if "profit" in k], "nothing at the broker: no page"

    (row,) = watchdog.profit_target_state().retired
    assert (row.outcome, row.order_may_be_working) == (str(outcome), False)


async def test_a_retired_take_leaves_the_stop_armed_and_its_page_unswallowed():
    """The reason the profit target has its OWN episode tag. A take that goes unfilled
    leaves its edge risen; had it shared `_TRIGGERED`, the backstop firing on the tick
    after would arrive as Tier 2 `triggered_still` with no page on the phone."""
    watchdog, clock, market, executor, audit, alerts, _ = _profit_watchdog(
        outcome=ExitOutcome.UNFILLED
    )

    def near_strike(m):
        _at_one_tick(m, spot=5003.0)

    await _tick(watchdog, clock, market)
    await _tick(watchdog, clock, market)
    await _tick(watchdog, clock, market, near_strike)   # take #1; backstop window opens
    await _tick(watchdog, clock, market, near_strike)   # take #2; retired
    assert [r for _, r in executor.calls] == [PROFIT, PROFIT]

    executor.outcome = ExitOutcome.FILLED
    await _tick(watchdog, clock, market, near_strike)   # backstop sustained
    assert executor.calls[-1][1] == {TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE}
    assert [k for k, _ in alerts].count("watchdog_triggered") == 1


async def test_an_unconfirmed_take_retires_pages_once_and_spares_the_emergency_exit():
    """§11.1's hole, closed. On the emergency path `NOT_RESOLVED` spends the ONE
    exit attempt; a take that ended there at 10:34 would have left 20260901-put-7605-
    7600 with no stop at 14:40. Here it retires the TAKE — and pages, because a close
    may be resting at the broker, the one fact about a take a human must hear."""
    watchdog, clock, market, executor, audit, alerts, _ = _profit_watchdog(
        outcome=ExitOutcome.NOT_RESOLVED
    )
    for _ in range(6):
        await _tick(watchdog, clock, market)

    assert len(executor.calls) == 1, "retired on the first unconfirmed take"
    assert _emergency_allowances(watchdog) == ({}, {}, set())
    pages = [d for k, d in alerts if k == "watchdog_profit_target_retired"]
    assert len(pages) == 1
    (page,) = pages
    assert page["order_may_be_working"] is True
    assert page["exit_attempt_intact"] is True
    assert page["candidate_id"] == spread().candidate_id     # B1's throttle scope
    assert "reject_reason" in page and "mutation_error" in page

    # And the stop is still there.
    executor.outcome = ExitOutcome.FILLED
    for _ in range(3):
        await _tick(watchdog, clock, market, _credit_stop(clock))
    assert executor.calls[-1][1] == CREDIT_TRIGGER


async def test_a_refusal_is_free_and_stands_down_rather_than_monopolizing():
    """`NOT_ATTEMPTED` sent nothing, so it charges nothing and pages nothing — the
    whole design rests on a failed look costing zero.

    But it is not retried on the very next tick (2026-09-23 review): a refusal
    stands the take down for `_PROFIT_REFUSAL_BACKOFF_S`. In a broker outage each
    attempt sits up to ~12s in the guard's burst with the position in `_exiting`,
    where `check_once` skips it whole, so an every-tick retry held the stop, the
    backstop and the flatten to one look per burst for the length of the outage."""
    watchdog, clock, market, executor, audit, alerts, _ = _profit_watchdog(
        outcome=ExitOutcome.NOT_ATTEMPTED
    )
    for _ in range(3):
        await _tick(watchdog, clock, market)
    assert len(executor.calls) == 1
    checks_before = audit.events.count("tier2:watchdog.check")

    # Inside the stand-down: no take, and the position is EVALUATED every tick.
    ticks = int(wd._PROFIT_REFUSAL_BACKOFF_S // 1.5) - 1
    for _ in range(ticks):
        await _tick(watchdog, clock, market)
    assert len(executor.calls) == 1
    assert audit.events.count("tier2:watchdog.check") - checks_before == ticks

    # Past it, the take looks again.
    await _tick(watchdog, clock, market, step=3.0)
    assert len(executor.calls) == 2

    # Free throughout, and ONE episode: the stand-down must not re-open the edge.
    assert watchdog._profit_sends == {} and watchdog._profit_spent == {}
    assert _emergency_allowances(watchdog) == ({}, {}, set())
    assert audit.payload("watchdog.profit_target_unattempted")["retry_in_s"] == (
        wd._PROFIT_REFUSAL_BACKOFF_S
    )
    assert audit.events.count("watchdog.triggered") == 1
    assert "watchdog.exit_unattempted" not in audit.events, "not the emergency page"
    assert alerts == []


async def test_a_stand_down_never_delays_an_emergency():
    """`_may_act_on` only ever removes the profit target, so a stop that fires inside
    a refusal's stand-down is acted on at once."""
    watchdog, clock, market, executor, audit, alerts, _ = _profit_watchdog(
        outcome=ExitOutcome.NOT_ATTEMPTED
    )
    for _ in range(3):
        await _tick(watchdog, clock, market)
    assert [r for _, r in executor.calls] == [PROFIT]

    executor.outcome = ExitOutcome.FILLED
    for _ in range(3):
        await _tick(watchdog, clock, market, _credit_stop(clock))
    assert executor.calls[-1][1] == CREDIT_TRIGGER
    assert [k for k, _ in alerts].count("watchdog_triggered") == 1


async def test_a_closing_take_is_not_repeated_while_the_fill_books():
    watchdog, clock, market, executor, *_ = _profit_watchdog()
    for _ in range(8):     # the ledger keeps showing the spread: the fill has not booked
        await _tick(watchdog, clock, market)
    assert len(executor.calls) == 1
    (row,) = watchdog.profit_target_state().retired
    assert row.outcome == "filled" and row.order_may_be_working is False


async def test_a_filled_take_spends_the_close_so_a_partial_stub_is_paged_not_orphaned():
    """Finding 1 of the 2026-09-23 review, updated 2026-09-24. A take that fills PART
    of the position (the walk cancels the remainder and reports `FILLED`;
    `filled_contracts` is rung-scoped and cannot say which) leaves a stub — and the
    take's fill sits under the SAME `argus-close-*` key the guard now sizes closes
    against.

    Before the 2026-09-23 fix the stub was left believing its stop was intact:
    silent until the stop fired and came back `BLOCKED`. That fix spends the close
    exactly as an emergency fill does, landing the stub in the state built for it —
    a repeating page carrying the live mark, NO EXIT LEFT on `/status` — and since
    2026-09-24 the stub-close continuation adds exactly ONE further close on top
    (the guard's quantity rule is what makes it safe), then pages for good.

    Two review fixes are pinned here too: nothing is handed off while the scan
    still holds the FULL size (no booked evidence of a partial — a full take in
    flight looks the same), and with the stop TRUE the continuation closes on the
    EXIT ladder, never the one-tick take `close()` would refuse to build."""
    watchdog, clock, market, executor, audit, alerts, _ = _profit_watchdog(
        config=_on(stub_close_enabled=True)
    )
    for _ in range(3):
        await _tick(watchdog, clock, market)
    assert [r for _, r in executor.calls] == [PROFIT]
    key = spread().external_identifier
    assert watchdog._attempted[key].outcome is ExitOutcome.FILLED
    assert watchdog._attempted[key].contracts == 2
    assert watchdog._flatten_attempted == set() and watchdog._sends == {}

    # The scan still shows the full 2: no booked evidence, no handoff.
    for _ in range(3):
        await _tick(watchdog, clock, market, _credit_stop(clock))
    assert len(executor.calls) == 1, "unbooked is not a stub"
    assert "watchdog.stub_close" not in audit.events

    # The partial books: ONE further close — on the EXIT ladder, the stop is true.
    watchdog._spreads.scan = OpenSpreadScan(spreads=(spread(contracts_held=1),))
    watchdog.request_refresh()
    for _ in range(3):
        await _tick(watchdog, clock, market, _credit_stop(clock))
    assert len(executor.calls) == 2, "one take plus ONE stub continuation"
    assert executor.calls[-1][1] == CREDIT_TRIGGER, "the stop wins the ladder"
    assert executor.stubs == [False, True]
    assert audit.payload("watchdog.stub_close")["origin_reasons"] == ["profit_target"]
    for _ in range(15):     # past exit_unattempted_max_s (20s in the test config)
        await _tick(watchdog, clock, market, _credit_stop(clock))
    assert len(executor.calls) == 2, "the allowance is one further close, once"
    spent_pages = [d for k, d in alerts if k == "watchdog_exit_spent_still_held"]
    assert spent_pages and spent_pages[0]["exit_outcome"] == "filled"
    assert spent_pages[0]["still_triggered"] is True
    (row,) = watchdog.spent_exit_attempts()
    assert row.outcome == "filled"
    assert row.stub_close == "sent"


async def test_the_guard_sizes_a_close_after_a_partial_take():
    """The permission pin (2026-09-24). This WAS
    `test_the_guard_really_blocks_a_close_after_a_partial_take`, whose docstring
    said: "If the guard ever learns to size a close against what is still held,
    this fails — and the spend above should be revisited rather than kept." The
    guard has now learned, on the stub-close design's own terms: after a take
    fills 1 of 2 (remainder cancelled), a close of `q=1` is PERMITTED by the
    broker's own per-leg fills, a close of `q=2` — the stale scan the ledger
    lag produces — is refused `close_quantity_exceeded`, and both verdicts are
    in the trail with their arithmetic. With `stub_close_enabled = false` the
    original pin holds verbatim: the same `q=1` close is BLOCKED on the fill."""
    on = make_config(watchdog={"stub_close_enabled": True})
    executor, broker, audit, quotes, *_ = _near_worthless(config=on)
    broker.on_poll = [lambda b: b.fill(1000, contracts=1)]
    take = await executor.close(held(), quotes.market(), PROFIT)
    assert take.outcome is ExitOutcome.FILLED
    assert wd._take_may_be_working(take) is True, "the remainder cancel is best-effort"

    # q=1, the booked stub: the FILLED disposition the binary rule blocks on,
    # cleared by arithmetic — and recorded Tier 1 with the arithmetic attached.
    stop = await executor.close(held(contracts=1), quotes.market(), CREDIT_TRIGGER)
    assert stop.outcome is ExitOutcome.FILLED
    permitted = audit.payload("order.duplicate_close_permitted")
    assert permitted["quantity"] == {
        "asked": 1,
        "opened_filled": {"SPXW  260729P04995000": 2, "SPXW  260729P05000000": 2},
        "closed_filled": {"SPXW  260729P04995000": 1, "SPXW  260729P05000000": 1},
        "allowance": 1,
    }

    # q=2, the stale scan: refused by the inequality, nothing routed.
    executor, broker, audit, quotes, *_ = _near_worthless(config=on)
    broker.on_poll = [lambda b: b.fill(1000, contracts=1)]
    take = await executor.close(held(), quotes.market(), PROFIT)
    assert take.outcome is ExitOutcome.FILLED
    submitted_before = len(broker.submitted)

    stale = await executor.close(held(), quotes.market(), CREDIT_TRIGGER)
    assert stale.walk.outcome is WalkOutcome.BLOCKED
    assert stale.outcome is ExitOutcome.NOT_RESOLVED
    assert "close_quantity_exceeded" in stale.reason
    assert len(broker.submitted) == submitted_before
    blocked = audit.payload("order.duplicate_blocked")
    assert blocked["reason"] == "close_quantity_exceeded"
    assert blocked["quantity"]["allowance"] == 1

    # The switch OFF: the guard is binary, and the booked stub's close BLOCKS.
    off_config = make_config(watchdog={"stub_close_enabled": False})
    executor, broker, audit, quotes, *_ = _near_worthless(config=off_config)
    broker.on_poll = [lambda b: b.fill(1000, contracts=1)]
    take = await executor.close(held(), quotes.market(), PROFIT)
    assert take.outcome is ExitOutcome.FILLED
    submitted_before = len(broker.submitted)
    off = await executor.close(held(contracts=1), quotes.market(), CREDIT_TRIGGER)
    assert off.walk.outcome is WalkOutcome.BLOCKED
    assert "conflicting_filled_order" in off.reason
    assert len(broker.submitted) == submitted_before


async def test_a_clean_full_take_is_not_reported_as_possibly_working():
    executor, broker, _, quotes, *_ = _near_worthless()
    broker.on_poll = [lambda b: b.fill(1000)]
    take = await executor.close(held(), quotes.market(), PROFIT)
    assert take.outcome is ExitOutcome.FILLED
    assert wd._take_may_be_working(take) is False


class _RejectingExecutor(RecordingExecutor):
    """Reports what the real executor reports for a broker rejection at the door:
    `UNFILLED`, with the walk's `REJECTED` and the broker's own sentence."""

    async def close(self, spread, market, reasons, *, stub=False):
        self.calls.append((spread, reasons))
        self.stubs.append(stub)
        walk = WalkResult(
            outcome=WalkOutcome.REJECTED,
            external_identifier=f"argus-close-{spread.candidate_id}",
            broker_order_id=None,
            steps=0,
            requested_contracts=spread.contracts,
            filled_contracts=None,
            final_limit_price=Decimal("-0.05"),
            cancel_requested=False,
            recovered_disconnects=0,
            reason="rejected by broker",
            reject_reason="Closing orders are not permitted on this account",
        )
        return ExitReport(
            outcome=ExitOutcome.UNFILLED,
            external_identifier=walk.external_identifier,
            reasons=tuple(sorted(reasons, key=str)),
            walk=walk,
            reason="walk ended rejected",
        )


async def test_a_rejected_close_is_paged_once_with_the_brokers_words():
    """Finding 3 of the 2026-09-23 review. A take that does not fill is quiet — but a
    REJECTION is the broker refusing a CLOSE on this position (C5), and the emergency
    exit will send the same kind of order. Paged on the first, not again on the
    second, and the take still retires quietly on its budget."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=_RejectingExecutor(), config=_on(),
    )
    for _ in range(6):
        await _tick(watchdog, clock, market)

    pages = [d for k, d in alerts if k == "watchdog_profit_target_rejected"]
    assert len(pages) == 1
    (page,) = pages
    assert page["reject_reason"] == "Closing orders are not permitted on this account"
    assert page["candidate_id"] == spread().candidate_id
    assert page["exit_attempt_intact"] is True
    assert audit.events.count("watchdog.profit_target_rejected") == 1
    assert len(watchdog._executor.calls) == _PROFIT_SEND_ATTEMPTS
    assert _emergency_allowances(watchdog) == ({}, {}, set())


async def test_a_crashed_take_retires_the_take_and_keeps_the_exit():
    """In-memory first, like `_spend_on_crash` — but the allowance a crashed take may
    have used is the take's. If its order did land and is live, the emergency walk's
    duplicate guard BLOCKS rather than stacking on it: the flatten override's licence."""
    watchdog, clock, market, executor, audit, alerts, halts = _profit_watchdog()
    executor.raises = RuntimeError("boom")
    for _ in range(6):
        await _tick(watchdog, clock, market)

    assert len(executor.calls) == 1
    assert _emergency_allowances(watchdog) == ({}, {}, set())
    (row,) = watchdog.profit_target_state().retired
    assert row.order_may_be_working is True and "boom" in row.reason
    crash = audit.payload("watchdog.exit_crashed")
    assert crash["profit_target"] is True and crash["exit_attempt_intact"] is True
    # A crash in the exit task is a defect whichever trigger started it.
    assert halts and halts[0]["grade"] is HaltGrade.ENTRIES


async def test_the_flatten_override_is_untouched_by_a_retired_take():
    et_clock = FakeETClock(hour=15, minute=0)
    watchdog, clock, market, executor, *_ = _profit_watchdog(
        outcome=ExitOutcome.NOT_RESOLVED, et_clock=et_clock
    )
    for _ in range(4):
        await _tick(watchdog, clock, market)
    assert [r for _, r in executor.calls] == [PROFIT]
    assert watchdog._flatten_attempted == set()

    et_clock.at(15, 31)
    await _tick(watchdog, clock, market)
    assert executor.calls[-1][1] == {TriggerReason.FLATTEN_BEFORE_CLOSE}


# -- the executor: one rung, and never a cross -----------------------------------


def _near_worthless(**kwargs):
    """Net mid exactly one tick; crossing debit one tick over it."""
    return build_executor(short=(0.10, 0.15), long_=(0.05, 0.10), **kwargs)


@pytest.mark.parametrize("pct", [0.01, 0.20, 0.5, 1.0])
def test_the_profit_ladder_is_one_one_tick_rung_for_any_exit_config(pct):
    config = make_config(watchdog={"exit_walk_away_pct": pct, "exit_step_ticks": 2})
    policy = WalkPolicy.for_profit_target(config)
    assert (policy.min_concession_steps, policy.step_ticks) == (1, 1)
    walk = PriceWalk.from_policy(Decimal("-0.05"), policy, width=Decimal("5"))
    assert walk.floor == Decimal("-0.10")
    assert [walk.price_at(i) for i in range(3)] == [Decimal("-0.05"), Decimal("-0.10"), None]
    # Everything else is the exit's, field for field.
    exit_ = WalkPolicy.for_exit(config)
    assert dataclasses.replace(policy, min_concession_steps=0, step_ticks=1) == (
        dataclasses.replace(exit_, min_concession_steps=0, step_ticks=1)
    )


async def test_an_unfilled_take_walks_one_rung_settles_and_never_crosses():
    executor, broker, audit, quotes, _, alerts, _ = _near_worthless()
    report = await executor.close(held(), quotes.market(), PROFIT)

    assert [str(o.limit_price) for o in broker.submitted] == ["-0.05"]
    assert [str(o.limit_price) for _, o in broker.replaced] == ["-0.10"]
    assert report.walk.outcome is WalkOutcome.WALKED_AWAY
    assert report.outcome is ExitOutcome.UNFILLED
    assert report.cross_broker_order_id is None
    assert "watchdog.crossed" not in audit.events
    assert "watchdog.exit_finished" in audit.events
    assert alerts == [], "an unfilled take on a winning position does not page"
    started = audit.payload("watchdog.exit_started")
    assert started["walk_away_price"] == -0.10


async def test_a_take_that_fills_pages_like_any_close():
    executor, broker, _, quotes, _, alerts, _ = _near_worthless()
    broker.on_poll = [lambda b: b.fill(1000)]
    report = await executor.close(held(), quotes.market(), PROFIT)
    assert report.outcome is ExitOutcome.FILLED
    assert [k for k, _ in alerts] == ["watchdog_exit_filled"]


async def test_never_worked_is_settled_not_spent():
    """The thing §11.5 said must be prototyped first: a resting limit that never goes
    `Live` inside `working_timeout_s` is an ABSOLUTE spend on the emergency path, the
    very outcome C exists to avoid arriving through the front door. On the profit path
    it is cancelled, confirmed terminal, and reported retryable."""
    executor, broker, *_ = _near_worthless(broker=Broker(live_after_polls=None))
    quotes = executor._quotes
    report = await executor.close(held(), quotes.market(), PROFIT)
    assert report.walk.outcome is WalkOutcome.NEVER_WORKED
    assert report.outcome is ExitOutcome.UNFILLED

    emergency, _, _, e_quotes, *_ = build_executor(broker=Broker(live_after_polls=None))
    report = await emergency.close(held(), e_quotes.market(), CREDIT_TRIGGER)
    assert report.walk.outcome is WalkOutcome.NEVER_WORKED
    assert report.outcome is ExitOutcome.NOT_RESOLVED, "unchanged on the emergency path"


async def test_a_take_whose_cancel_never_confirms_is_retired_not_crossed():
    class StuckCancel(Broker):
        async def cancel(self, broker_order_id: int) -> None:
            self.cancelled.append(broker_order_id)
            self.set_status(broker_order_id, OrderStatus.CANCEL_REQUESTED)

    executor, broker, audit, quotes, *_ = _near_worthless(broker=StuckCancel())
    report = await executor.close(held(), quotes.market(), PROFIT)
    assert report.outcome is ExitOutcome.NOT_RESOLVED
    assert "last seen: still working" in report.reason
    assert "emergency exit keeps its attempt" in report.reason
    assert len(broker.submitted) == 1 and "watchdog.crossed" not in audit.events


async def test_a_take_refused_at_the_door_is_retryable_without_waiting():
    """A REJECTED ack carries no id for `_await_settled` to confirm, and nothing to
    confirm: the broker's own ack is the terminal word on the only order sent."""
    executor, broker, _, quotes, _, _, slept = _near_worthless()
    broker.reject_next("no")
    report = await executor.close(held(), quotes.market(), PROFIT)
    assert report.walk.outcome is WalkOutcome.REJECTED
    assert report.outcome is ExitOutcome.UNFILLED
    assert sum(slept) < make_config().watchdog.exit_cancel_settle_s


async def test_a_take_is_never_walked_from_anything_but_one_tick():
    """The maximum debit rests on the OPENING limit as much as on the policy, so the
    executor refuses to walk any other — above the walk, where refusing is free."""
    executor, broker, _, quotes, *_ = build_executor(short=(0.15, 0.20), long_=(0.05, 0.10))
    report = await executor.close(held(), quotes.market(), PROFIT)
    assert report.outcome is ExitOutcome.NOT_ATTEMPTED
    assert broker.submitted == []


def test_every_walk_outcome_has_one_profit_take_disposition():
    shared = {WalkOutcome.FILLED, WalkOutcome.NO_ORDER_EXISTS}
    groups = (shared, set(_PROFIT_SETTLE), set(_PROFIT_RETIRE))
    assert set().union(*groups) == set(WalkOutcome)
    assert sum(len(g) for g in groups) == len(WalkOutcome), "the groups must not overlap"


# -- structural: what a convention would let drift ------------------------------


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }


_EMERGENCY_STATE = {"_attempted", "_sends", "_flatten_attempted"}
_MUTATORS = {"add", "discard", "pop", "update", "clear", "setdefault", "remove"}


def _writes(node: ast.AST) -> set[str]:
    """`self.<attr>` names this subtree WRITES: item/attr assignment, `del`, or a
    mutating method call."""
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
            and n.func.attr in _MUTATORS
            and (name := self_attr(n.func.value)) is not None
        ):
            out.add(name)
    return out


_EMERGENCY_SPENDERS = {"_spend_attempt", "_spend_on_crash", "_spend_send"}


def _calls_to(node: ast.AST, names: set[str]) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in names
    ]


def test_a_profit_take_never_touches_an_emergency_allowance():
    """§11.5's load-bearing sentence — "its own `_profit_spent`, never `_attempted`" —
    held on the source rather than on a comment, with the ONE refinement the
    2026-09-23 review found necessary: a take that FILLED spends the close, because
    the guard blocks every later close under the same key (see
    `test_the_guard_really_blocks_a_close_after_a_partial_take`).

    So: no profit-path function WRITES an emergency allowance directly; no profit path
    reaches a spender, except exactly one `_spend_attempt` inside `_after_profit_take`'s
    `if outcome.closed:` branch; `_attempted` has exactly the writers it had plus the
    extracted `_spend_attempt`; and `_profit_spent` has exactly one writer."""
    tree = ast.parse((_SRC / "watchdog.py").read_text())
    fns = _functions(tree)
    for name in ("_after_profit_take", "_spend_profit_target", "_settle_profit_take",
                 "_may_act_on", "profit_target_state", "_page_profit_rejection"):
        assert not (_writes(fns[name]) & _EMERGENCY_STATE), name
    for name in ("_spend_profit_target", "_settle_profit_take", "_may_act_on",
                 "profit_target_state", "_page_profit_rejection"):
        assert not _calls_to(fns[name], _EMERGENCY_SPENDERS), name

    after = fns["_after_profit_take"]
    spends = _calls_to(after, _EMERGENCY_SPENDERS)
    assert [c.func.attr for c in spends] == ["_spend_attempt"]
    closed_branches = [
        n
        for n in ast.walk(after)
        if isinstance(n, ast.If) and ast.unparse(n.test) == "outcome.closed"
    ]
    assert len(closed_branches) == 1
    assert spends[0] in list(ast.walk(ast.Module(body=closed_branches[0].body,
                                                 type_ignores=[])))

    attempted_writers = {n for n, fn in fns.items() if "_attempted" in _writes(fn)}
    assert attempted_writers == {"__init__", "_spend_attempt", "_spend_on_crash"}

    profit_branches = [
        n
        for n in ast.walk(fns["_run_exit"])
        if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "profit"
    ]
    assert len(profit_branches) == 2, "the crash branch and the report branch"
    for branch in profit_branches:
        body = ast.Module(body=branch.body, type_ignores=[])
        assert not (_writes(body) & _EMERGENCY_STATE)
        assert not _calls_to(body, _EMERGENCY_SPENDERS)

    writers = {name for name, fn in fns.items() if "_profit_spent" in _writes(fn)}
    assert writers == {"__init__", "_spend_profit_target"}


def test_the_switch_gates_the_action_in_one_place():
    """`shadow.would_submit`'s shape: one flag, read at one gate. It is also read to
    REPORT it (`/status`) and to carry it from config; nothing else may consult it,
    or "off" and "on" start to be different code."""
    tree = ast.parse((_SRC / "watchdog.py").read_text())
    readers = {
        name
        for name, fn in _functions(tree).items()
        if any(
            isinstance(n, ast.Attribute) and n.attr == "profit_target_enabled"
            for n in ast.walk(fn)
        )
    }
    assert readers == {"from_config", "_may_act_on", "profit_target_state"}


def test_the_profit_path_has_no_cross_in_it():
    tree = ast.parse((_SRC / "watchdog.py").read_text())
    settle = _functions(tree)["_settle_profit_take"]
    called = {
        n.func.attr
        for n in ast.walk(settle)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert not called & {"_cross", "_cross_limit", "submit", "replace"}
    assert "_await_settled" in called


def test_the_executor_and_the_watchdog_ask_one_question():
    """`_is_profit_take` is THE definition; three readers of one question must not be
    able to disagree about which walk ran and which allowance a report may touch."""
    source = (_SRC / "watchdog.py").read_text()
    tree = ast.parse(source)
    askers = {
        name
        for name, fn in _functions(tree).items()
        if any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_is_profit_take"
            for n in ast.walk(fn)
        )
    }
    assert {"close", "_after_walk", "_record_exit", "_run_exit", "_trigger"} <= askers
    # And nobody spells the question a second way.
    assert source.count("frozenset({TriggerReason.PROFIT_TARGET})") == 1
    assert wd._is_profit_take(PROFIT) is True
    assert wd._is_profit_take(PROFIT | {TriggerReason.FLATTEN_BEFORE_CLOSE}) is False


def test_the_profit_policy_is_derived_from_the_exits_and_logs_nothing(caplog):
    """Finding 5 of the 2026-09-23 review: the settle-window assembly exists once
    (`WalkPolicy._exit_from_config`), and deriving from it must not repeat
    `for_exit`'s clamp announcement."""
    import logging

    config = make_config(watchdog={"exit_cadence_s": 0.5})      # inside the COA window
    with caplog.at_level(logging.INFO, logger="argus.execution.loop"):
        profit = WalkPolicy.for_profit_target(config)
    assert caplog.records == []
    exit_ = WalkPolicy.for_exit(config)
    assert profit == dataclasses.replace(exit_, step_ticks=1, min_concession_steps=1)
    assert profit.coa_settle_s == exit_.coa_settle_s and profit.clamped_by_coa
