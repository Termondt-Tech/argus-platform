"""Chunk 5's entry path: gate-approved candidate -> real order.

No network: `FakeOrderExecution` through the same audited stack production uses, on
the fake clock from `test_execution_loop`. The assertions that matter most are the
ones a reader cannot check by looking at the code —

  - which side of the exception boundary a given failure lands on,
  - that the duplicate guard runs exactly once and never before a replace,
  - that EVERY walk outcome is handled rather than falling through a default,
  - that dry-run mode really does not route,

because those are the ones a refactor breaks silently.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from tastytrade.utils import TastytradeError

from support import fresh_loss_budget
from argus.execution.audited import AuditedOrderExecution
from argus.execution.entry import EntryExecutor, UnderlyingDriftCheck
from argus.execution.loop import WalkOutcome, WalkResult
from argus.execution.models import OrderStatus
from argus.execution.store import InMemoryOrderStore
from argus.execution.guard import DuplicateOrderGuard
from argus.risk.killswitch import KillSwitch
from argus.risk.models import EntryProposal, RiskSnapshot
from argus.signal.models import OptionQuote, SpreadSignal
from tests.support import make_config
from tests.test_execution_loop import Broker, FakeAudit, FakeTime

TRADING_DATE = date(2026, 7, 29)
CANDIDATE = "20260729-put-4975-4970"
IDENTIFIER = f"argus-open-{CANDIDATE}"


# -- harness ------------------------------------------------------------------


def make_signal(
    *, captured_at: float = 1000.0, underlying_price: float = 4990.0
) -> SpreadSignal:
    """A real candidate, so chunk 1's credit-matches-legs assertion is exercised."""
    short = OptionQuote("SPXW  P4975", 4975.0, "put", -0.15, 1.20, 1.40)
    long_ = OptionQuote("SPXW  P4970", 4970.0, "put", -0.10, 0.0, 0.30)  # zero bid
    return SpreadSignal(
        underlying="SPXW",
        side="put",
        short_leg=short,
        long_leg=long_,
        credit=short.floored_mid - long_.floored_mid,
        width=5.0,
        captured_at=captured_at,
        # The arrival price: the underlying level this credit was worth, and the
        # reference the drift breaker measures every cycle against.
        underlying_price=underlying_price,
    )


PROPOSAL = EntryProposal(symbol="SPXW put credit spread 4975/4970", contracts=2, max_loss=840.0)
RISK = RiskSnapshot(
    account_nlv=100_000.0,
    daily_pnl=0.0,
    open_position_count=0,
    market_data_age_s=0.1,
    account_stream_age_s=0.5, loss_budget=fresh_loss_budget(),
)


class HaltSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, reason, *, source, detail=None):
        self.calls.append((reason, source))


def build_entry(
    broker: Broker | None = None,
    *,
    dry_run: bool = False,
    walk_result: WalkResult | None = None,
    walk_raises: BaseException | None = None,
    spot=None,
    #: The residual-order recheck (P5), when a test is about what an entry walk
    #: leaves behind. None everywhere else, which is the pre-P5 behaviour.
    residuals=None,
    **config_overrides,
):
    """An executor over the REAL audited stack, optionally with a scripted walk.

    `walk_result` / `walk_raises` replace only the loop, via the `loop_factory` seam —
    the guard, the store, the audit writes and the order construction all stay real,
    so a test about outcome handling is not also a test about a stubbed pipeline.
    """
    broker = broker if broker is not None else Broker()
    audit = FakeAudit()
    store = InMemoryOrderStore()
    stack = AuditedOrderExecution(broker, audit=audit, store=store)
    clock = FakeTime()
    execution_cfg = {"dry_run_submissions": dry_run}
    execution_cfg.update(config_overrides.pop("execution", {}))
    config = make_config(
        environment="sandbox", execution=execution_cfg, **config_overrides
    )
    stop = asyncio.Event()
    halt = HaltSpy()
    killswitch = KillSwitch(audit)
    refreshed: list[int] = []

    loop_factory = None
    if walk_result is not None or walk_raises is not None:
        class _StubLoop:
            def __init__(self) -> None:
                self.calls: list[bool] = []

            async def run(self, order, *, guard_first_submission: bool):
                self.calls.append(guard_first_submission)
                if walk_raises is not None:
                    raise walk_raises
                return replace(walk_result, external_identifier=order.external_identifier)

        stub = _StubLoop()
        # `**_` because the real factory is called with the per-candidate drift
        # check; this stub replaces the loop, not the wiring that reaches it.
        loop_factory = lambda **_: stub  # noqa: E731 - one expression, read once
    else:
        stub = None

    entry = EntryExecutor(
        execution=stack,
        audit=audit,
        config=config,
        guard=DuplicateOrderGuard(stack, audit=audit),
        killswitch=killswitch,
        halt=halt,
        stop=stop,
        alert=lambda kind, detail: alerts.append(kind),
        clock=clock.clock,
        sleep=clock.sleep,
        spot=spot,
        trading_date=lambda: TRADING_DATE,
        on_filled=lambda: refreshed.append(1),
        loop_factory=loop_factory,
        residuals=residuals,
    )
    alerts: list[str] = []
    return SimpleHarness(
        entry=entry, audit=audit, broker=broker, store=store, stop=stop,
        halt=halt, alerts=alerts, clock=clock, refreshed=refreshed, stub=stub,
        killswitch=killswitch,
    )


class SimpleHarness:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    async def submit(self, signal=None):
        await self.entry.submit(signal or make_signal(captured_at=self.clock.now), PROPOSAL, RISK)


# -- an unanswerable duplicate guard ------------------------------------------
#
# Regression harness for 2026-08-25: one timed-out `get_live_orders()` on the day's
# FIRST tick (10:00:00 ET) ended the whole session with ~5 hours of entry window
# left. `Broker.fail_next` fires once, which is exactly the blip the retry now
# absorbs, so a test about GIVING UP needs a guard that stays dead.

#: `guard_unreachable_max_streak` in `tests.support.make_config`, restated so the
#: tests below read as "the configured streak" rather than as a magic 3.
MAX_STREAK = 3


class DeadGuardBroker(Broker):
    """Fails `get_live_orders()` for its first `times` calls, then behaves.

    Only the guard's question — the walk polls the same method, so `times=None`
    (dead forever) is right for tests where nothing is expected to be submitted,
    and a finite `times` is what lets a test watch a candidate recover and go on
    to walk normally.
    """

    def __init__(self, exc: Exception, *, times: int | None = None) -> None:
        super().__init__()
        self._exc = exc
        #: Attempts still to be refused; None means "dead forever". Public so a
        #: test can kill the broker again partway through.
        self.remaining = times
        #: Guard/walk reads that were REFUSED, so a test can count attempts without
        #: `polls`, which also counts the successful ones.
        self.refused = 0

    async def get_live_orders(self):
        if self.remaining is None or self.remaining > 0:
            if self.remaining is not None:
                self.remaining -= 1
            self.refused += 1
            raise self._exc
        return await super().get_live_orders()


class HangingGuardBroker(Broker):
    """Accepts the socket and never answers — the one case a timeout is FOR.

    A 502 or a refused connection comes back in milliseconds and never touches the
    bound; this is the shape that made `broker.http_timeout_s` load-bearing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hangs = 0

    async def get_live_orders(self):
        self.hangs += 1
        await asyncio.Event().wait()   # never set, and never reached by a real test
        raise AssertionError("unreachable")  # pragma: no cover


def _nth_candidate(n: int, captured_at: float) -> SpreadSignal:
    """The n-th DISTINCT trade idea. `candidate_id` keys on the strike pair, so the
    streak — which counts distinct ideas, never ticks — needs different strikes."""
    short_strike = 4975.0 - 10.0 * n
    short = OptionQuote(f"SPXW  P{short_strike:.0f}", short_strike, "put", -0.15, 1.20, 1.40)
    long_ = OptionQuote(
        f"SPXW  P{short_strike - 5:.0f}", short_strike - 5.0, "put", -0.10, 0.0, 0.30
    )
    return SpreadSignal(
        underlying="SPXW",
        side="put",
        short_leg=short,
        long_leg=long_,
        credit=short.floored_mid - long_.floored_mid,
        width=5.0,
        captured_at=captured_at,
        underlying_price=4990.0,
    )


async def _drive_streak(h: SimpleHarness, n: int, *, start: int = 0) -> None:
    """Submit `n` DISTINCT candidates, one per call, as separate loop ticks would."""
    for i in range(start, start + n):
        await h.submit(signal=_nth_candidate(i, h.clock.now))


def result(outcome: WalkOutcome, **kw) -> WalkResult:
    base = dict(
        outcome=outcome,
        external_identifier=IDENTIFIER,
        broker_order_id=1,
        steps=0,
        requested_contracts=2,
        filled_contracts=None,
        final_limit_price=Decimal("0.85"),
        cancel_requested=False,
        recovered_disconnects=0,
    )
    base.update(kw)
    return WalkResult(**base)


# -- the environment gate: shadow is untouched --------------------------------


async def test_shadow_never_reaches_this_module_at_all():
    """`trades_for_real` is the ONE switch, and shadow is on the other side of it.

    Asserted here rather than only in the session tests because it is the property
    the whole chunk rests on: in shadow, `submit` is None and `SignalLoop` takes the
    would-submit branch, so no code in `entry.py` runs. If this ever passes for
    shadow, real orders are one config typo away.
    """
    from argus.execution.pipeline import trades_for_real

    assert trades_for_real(make_config(environment="sandbox")) is True
    assert trades_for_real(make_config(environment="shadow")) is False
    # live needs durable audit writes and a NAMED account before the loader will
    # build it at all.
    live = make_config(
        environment="live",
        audit={"synchronous": "FULL"},
        broker={"account_number": "5WX00000"},
    )
    assert trades_for_real(live) is True


# -- the duplicate guard: exactly once, and not before a replace --------------


async def test_the_guard_runs_here_and_the_loop_is_told_not_to_repeat_it():
    """`guard_first_submission=False` means "already done", not "skipped".

    The check happens in `submit`, above the exception boundary, because a guard
    failure is a pre-submission failure and must end the day rather than halt the
    process. Delegating it to the loop would put that raise inside the hard-halt
    region — which is what the first version of this did.
    """
    h = build_entry(walk_result=result(WalkOutcome.FILLED, filled_contracts=2))
    await h.submit()
    assert h.stub.calls == [False], "the loop must not re-run a check already made"
    # ...and it really was made, exactly once.
    assert [c for c, _ in h.audit.payloads].count("order.duplicate_check") == 1


async def test_a_replace_is_never_preceded_by_a_guard_check():
    """A price-walking replace re-sends the SAME identifier against its own working
    order, so guarding there would find that order, correctly call it a conflict, and
    deadlock the loop against itself. Driven through the REAL loop (no stub) so the
    count comes from the state machine, not from an assertion about it."""
    broker = Broker()
    h = build_entry(broker)
    await h.submit()
    blocked = [c for c, _ in h.audit.payloads if c == "order.duplicate_blocked"]
    checks = [c for c, _ in h.audit.payloads if c == "order.duplicate_check"]
    assert blocked == [], "a walk must never block itself"
    assert len(checks) == 1, f"expected exactly one guard check, got {len(checks)}"
    assert broker.replace_calls > 0, "the walk should have repriced at least once"


async def test_an_unreachable_broker_at_the_guard_ends_ENTRIES_not_the_process():
    """The guard raises rather than reporting "no conflict" (chunk 2). Nothing has been
    sent, so this is the SOFT side: end today's entries, keep the process. Continuing
    to re-propose every cadence tick while blind to what is already working is the
    runaway shape spec §9 names, so it must not simply be skipped either.

    CHANGED 2026-08-26: it now takes `guard_unreachable_max_streak` SPENT BURSTS to
    get here, not one exception. This test used `fail_next` (one call), which the
    retry now absorbs — the fallback behaviour it asserts is unchanged, only what it
    takes to reach it. See `test_a_single_transient_guard_failure_costs_nothing`.

    CHANGED 2026-09-01: the stand-down is the `EntriesEnded` latch, not the session
    `stop` event. `stop` is read by the watchdog's scan loop, an exit walk in flight
    and the session teardown, so ending entries through it took a held spread's
    virtual stop and its 15:30 flatten with it."""
    broker = DeadGuardBroker(ConnectionError("broker unreachable"))
    h = build_entry(broker)
    await _drive_streak(h, MAX_STREAK)

    assert h.halt.calls == [], "nothing was submitted; the process must survive"
    assert h.entry.entries_ended is not None, (
        "entries must end rather than keep re-proposing blind"
    )
    assert not h.stop.is_set(), (
        "and the SESSION must not: the watchdog, the exits and the 15:30 flatten "
        "all live on this event"
    )
    assert h.audit.payload("entry.aborted_day")["kind"] == "entry_guard_unreachable"
    assert broker.submitted == [], "no order may go out on an unanswered check"


# -- every outcome, explicitly ------------------------------------------------

#: Every `WalkOutcome`, with whether `_report` should page someone for it. Kept as
#: a named constant so the coverage claim below can be checked MECHANICALLY rather
#: than asserted in a comment — see `test_the_outcome_table_is_the_whole_enum`.
OUTCOME_EXPECTATIONS = [
    (WalkOutcome.FILLED, False),
    (WalkOutcome.WALKED_AWAY, False),
    (WalkOutcome.DRIFTED, False),
    (WalkOutcome.REJECTED, True),
    (WalkOutcome.BLOCKED, False),
    (WalkOutcome.NEVER_WORKED, True),
    (WalkOutcome.CANCELLED_ELSEWHERE, True),
    (WalkOutcome.UNRESOLVED, True),
    # Carved out of UNRESOLVED on 2026-08-28 and alerting for the same reason it
    # always did: it is the same broker fault mid-session, only now with a name that
    # says the broker answered. The split changes what the EXIT path does with it,
    # never what the entry path reports.
    (WalkOutcome.NO_ORDER_EXISTS, True),
    (WalkOutcome.ABORTED, False),
]


def test_the_outcome_table_is_the_whole_enum():
    """The drift test that makes the parameterization below mean what it claims.

    Same shape as `test_order_status_mirrors_the_sdk_exactly`: a set equality, not a
    comment. Without it the table is just a list that happens to be complete today,
    and a `WalkOutcome` added to chunk 3 would land in `_report`'s catch-all with
    nothing failing — which is precisely how the DRIFTED follow-on could have gone.
    """
    covered = {outcome for outcome, _ in OUTCOME_EXPECTATIONS}
    assert covered == set(WalkOutcome), (
        "every WalkOutcome needs a row in OUTCOME_EXPECTATIONS, and an explicit "
        f"branch in EntryExecutor._report; missing: {set(WalkOutcome) - covered}"
    )
    assert len(covered) == len(OUTCOME_EXPECTATIONS), "duplicate row in the table"


@pytest.mark.parametrize("outcome, expect_alert", OUTCOME_EXPECTATIONS)
async def test_every_walk_outcome_is_handled_and_recorded(outcome, expect_alert):
    """Each outcome handled, and `entry.finished` recorded for it.

    Parameterized over the whole enum on purpose: a new outcome added to chunk 3
    fails here rather than falling through a default branch in `_report`. The
    "whole enum" part is enforced by the set equality above, not by this list
    happening to be current.
    """
    h = build_entry(walk_result=result(outcome))
    await h.submit()

    assert "entry.finished" in h.audit.events
    payload = h.audit.payload("entry.finished")
    assert payload["outcome"] == str(outcome)
    assert payload["candidate_id"] == CANDIDATE
    assert bool(h.alerts) is expect_alert, f"{outcome}: alerts={h.alerts}"


async def test_unresolved_alerts_because_it_is_the_one_outcome_meaning_go_look():
    h = build_entry(walk_result=result(WalkOutcome.UNRESOLVED, reason="two_working_orders"))
    await h.submit()
    assert h.alerts == ["entry_unresolved"]


async def test_a_recovered_disconnect_alerts_even_on_a_good_outcome():
    """The outcome stands — but a broker disconnect while a real order was working is
    worth knowing about even when the walk recovered from it."""
    h = build_entry(
        walk_result=result(WalkOutcome.FILLED, filled_contracts=2, recovered_disconnects=2)
    )
    await h.submit()
    assert "entry_recovered_disconnects" in h.alerts


async def test_a_fill_asks_the_watchdog_to_look_now_and_books_nothing_itself():
    """Position recording is the fill consumer's job, off the `AlertStreamer`,
    regardless of who submitted. Booking it from the walk's report too would
    double-book — and `WalkResult` is explicitly a report, not a claim about what is
    held. All the entry path does is shorten the watchdog's wait."""
    h = build_entry(walk_result=result(WalkOutcome.FILLED, filled_contracts=2))
    await h.submit()
    assert h.refreshed == [1]
    assert h.store.positions == {}, "the entry path must not write positions"


async def test_a_partial_fill_alerts_because_the_size_is_not_what_was_sized():
    h = build_entry(walk_result=result(WalkOutcome.FILLED, filled_contracts=1))
    await h.submit()
    assert "entry_partially_filled" in h.alerts


async def test_walking_away_is_quiet_and_never_crosses_the_market():
    """Entries do NOT escalate. Chunk 4's exits cross because the alternative is
    holding a position that is already hurting; a missed entry costs only the trade,
    and paying up to open one nobody has to open inverts the edge."""
    broker = Broker()
    h = build_entry(broker, walk_result=result(WalkOutcome.WALKED_AWAY, cancel_requested=True))
    await h.submit()
    assert h.alerts == [], "a common designed ending must not page anyone"
    assert broker.submitted == [], "no crossing order may be built for an entry"


# -- one attempt per candidate ------------------------------------------------


async def test_a_candidate_is_attempted_once_per_session():
    """`SignalLoop` re-proposes a qualifying spread every cadence tick — one shadow
    session logged 456 would_submit records for 10 distinct spreads. Without this, a
    WALKED_AWAY entry would be re-entered seconds later, all day: the guard allows it
    (a terminal-unfilled order is DEAD, deliberately unblocked for crash recovery) and
    so does the concurrent-position limit (nothing filled, nothing held)."""
    h = build_entry(walk_result=result(WalkOutcome.WALKED_AWAY))
    await h.submit()
    await h.submit()
    await h.submit()

    assert h.stub.calls == [False], "only the first proposal may reach the broker"
    assert h.audit.events.count("entry.finished") == 1
    assert h.audit.events.count("tier2:entry.already_attempted") == 2
    # And the guard is not re-asked either: a re-proposal must not cost a broker round
    # trip per cadence tick for the rest of the day.
    assert [c for c, _ in h.audit.payloads].count("order.duplicate_check") == 1


async def test_was_attempted_reads_the_same_claim_submit_makes_and_changes_nothing():
    """The side-balance selector's read of this dedup (2026-09-26). It answers
    exactly what `submit` would refuse, and ASKING it claims nothing."""
    h = build_entry(walk_result=result(WalkOutcome.WALKED_AWAY))
    signal = make_signal(captured_at=h.clock.now)
    assert h.entry.was_attempted(signal) is False
    assert h.entry.was_attempted(signal) is False       # a read claims nothing
    await h.submit(signal)
    assert h.entry.was_attempted(signal) is True
    await h.submit(signal)
    assert h.audit.events.count("tier2:entry.already_attempted") == 1


async def test_a_candidate_counts_as_attempted_even_if_the_submit_blew_up():
    """Claimed BEFORE anything can reach the broker: after a failed submit an order
    may exist, and a second attempt at the same strikes is exactly the duplicate this
    arc is built to avoid."""
    h = build_entry(walk_raises=RuntimeError("boom"))
    await h.submit()
    h.stop.clear()
    await h.submit()
    assert h.audit.events.count("tier2:entry.already_attempted") == 1


# -- the exception-policy boundary --------------------------------------------


async def test_a_stale_candidate_is_recorded_and_the_session_CONTINUES():
    """Not a failure — a refusal. The price aged past tolerance between gate-pass and
    construction, so this candidate is abandoned and the next tick may propose it
    again with a fresh quote. Ending the day over a 1.1s-stale candidate would be
    wildly disproportionate."""
    h = build_entry()
    stale = make_signal(captured_at=h.clock.now - 60.0)
    await h.entry.submit(stale, PROPOSAL, RISK)

    assert h.audit.payload("entry.not_built")["reason"] == "StaleSignalError"
    assert not h.stop.is_set(), "a stale candidate must not end the day"
    assert h.halt.calls == []
    assert h.broker.submitted == []


async def test_an_unpriceable_candidate_is_a_refusal_not_a_defect_on_the_entry_path():
    """The entry-side cost of `UnpriceableSpreadError`, stated rather than discovered.

    It was added for the EXIT path (2026-09-02), where a mid rounding to 0.00 is the
    flatten's modal regime. `entry.not_built` records `type(exc).__name__`, so the same
    refusal now records the more specific name here too. That is the whole side effect,
    it is strictly more informative, and it is not reachable in practice —
    `signal.min_credit = 0.50` keeps an entry credit two orders of magnitude above the
    tick.

    **What must not change is the CATEGORY.** The subclass keeps it on the soft,
    session-continues side of `entry.py`'s exception boundary; if it ever stopped being
    a `ValueError` it would fall through to `entry_build_failed` and end the day's
    entries over a price that rounded down.
    """
    from argus.execution.build import UnpriceableSpreadError

    h = build_entry()
    # Legs that are genuinely worthless, not merely a zeroed credit: chunk 1 recomputes
    # the floored mid from the legs and would refuse the mismatch FIRST, which is a
    # different refusal and the one the old version of this test accidentally asserted.
    base = make_signal(captured_at=h.clock.now)
    short = OptionQuote("SPXW  P4975", 4975.0, "put", -0.15, 0.0, 0.0)
    long_ = OptionQuote("SPXW  P4970", 4970.0, "put", -0.10, 0.0, 0.0)
    worthless = replace(
        base,
        short_leg=short,
        long_leg=long_,
        credit=short.floored_mid - long_.floored_mid,
    )
    await h.entry.submit(worthless, PROPOSAL, RISK)

    assert h.audit.payload("entry.not_built")["reason"] == "UnpriceableSpreadError"
    assert issubclass(UnpriceableSpreadError, ValueError)
    assert h.halt.calls == [], "a refusal, never a halt"
    assert h.broker.submitted == []
    assert "entry_build_failed" not in str(h.audit.events), (
        "it must stay on the soft side of the boundary — the day continues"
    )


async def test_a_failure_BEFORE_the_submit_ends_the_day_and_keeps_the_process():
    """The SOFT side. Nothing reached the broker, so there is no order state to
    protect; a latched kill switch would also cost tomorrow."""
    h = build_entry()
    bad = replace(make_signal(captured_at=h.clock.now), credit=float("nan"))
    await h.entry.submit(bad, PROPOSAL, RISK)
    # A NaN credit is a ValueError from the builder — an expected refusal, so the
    # session continues. The unexpected kind is covered below.
    assert h.audit.payload("entry.not_built")["reason"] == "ValueError"
    assert h.halt.calls == []


async def test_an_UNEXPECTED_failure_before_the_submit_ends_entries():
    h = build_entry()
    # A proposal the builder cannot use in a way that is not one of the expected
    # refusals: `contracts` is not an int at all.
    broken = EntryProposal(symbol="x", contracts=None, max_loss=840.0)
    await h.entry.submit(make_signal(captured_at=h.clock.now), broken, RISK)

    assert h.entry.entries_ended is not None, "today's entries end"
    assert not h.stop.is_set(), "but the session does not"
    assert h.halt.calls == [], "and neither does the process"
    assert h.audit.payload("entry.aborted_day")["kind"] == "entry_build_failed"


async def test_a_failure_AT_OR_AFTER_the_submit_HALTS_THE_PROCESS():
    """The HARD side, and the reason the boundary is one call boundary rather than a
    condition: past this point an order may exist at the broker, and a live order with
    nobody walking it is what the watchdog's posture exists for."""
    h = build_entry(walk_raises=RuntimeError("the socket died mid-walk"))
    await h.submit()

    assert len(h.halt.calls) == 1
    reason, source = h.halt.calls[0]
    assert source == "entry_execution_exception"
    assert CANDIDATE in reason
    assert "entry_execution_failed" in h.alerts


async def test_a_dead_audit_log_propagates_rather_than_becoming_a_soft_day_end():
    """`AuditWriteError` must never be converted into a handled day-end: that would
    hide a broken black box behind a tidy-looking session. It propagates, `SignalLoop`
    stops itself, and the audit log's own failure hook halts the process."""
    from argus.audit.log import AuditWriteError

    h = build_entry()

    async def boom(category, payload):
        raise AuditWriteError("chain is broken")

    h.audit.record = boom
    stale = make_signal(captured_at=h.clock.now - 60.0)
    with pytest.raises(AuditWriteError):
        await h.entry.submit(stale, PROPOSAL, RISK)
    assert h.halt.calls == []


# -- the dry-run activation mode ----------------------------------------------


async def test_dry_run_submits_with_dry_run_true_and_routes_nothing():
    broker = Broker()
    h = build_entry(broker, dry_run=True)
    await h.submit()

    assert len(broker.submitted) == 1
    assert broker.dry_runs == broker.submitted, (
        "the broker must be told this is a validation call"
    )
    assert broker.submitted[0].external_identifier == IDENTIFIER
    assert h.store.orders == {}, "a dry run mints no id, so nothing is recorded"


async def test_dry_run_does_not_walk_and_says_why():
    """It stops after the submit deliberately: a dry run returns no broker order id
    (measured against sandbox, chunk 1), so there is nothing to poll, reprice or
    cancel, and driving the state machine against that would test a fiction."""
    broker = Broker()
    h = build_entry(broker, dry_run=True)
    await h.submit()

    payload = h.audit.payload("entry.dry_run")
    assert payload["submitted"] is True
    assert payload["walked"] is False
    assert "no broker order id" in payload["why_not_walked"]
    assert broker.replace_calls == 0
    assert "walk.started" not in h.audit.events


async def test_dry_run_still_runs_the_guard_exactly_once():
    """There is no walk to delegate to, so the check happens here — still once, still
    before the (validation-only) submit."""
    broker = Broker()
    h = build_entry(broker, dry_run=True)
    await h.submit()
    assert [c for c, _ in h.audit.payloads].count("order.duplicate_check") == 1


# -- the dry-run record is the SCOREABLE record of a dry-run day ---------------
#
# `entry.dry_run` is what `shadow/marker.py` reads on any day the environment is
# not shadow. These tests are the contract between the writer here and that
# reader; the last one closes the loop by decoding the real emitted payload with
# the real decoder rather than re-asserting a hand-written shape.


async def test_dry_run_records_the_signal_and_proposal_for_marking():
    """Without these the record says a candidate happened but not WHAT — which is
    exactly the gap that made every dry-run day after the 2026-08-11 sandbox flip
    unscoreable."""
    h = build_entry(dry_run=True)
    await h.submit()

    payload = h.audit.payload("entry.dry_run")
    assert payload["signal"]["side"] == "put"
    assert payload["signal"]["short"]["strike"] == 4975.0
    assert payload["signal"]["long"]["strike"] == 4970.0
    assert payload["signal"]["width"] == 5.0
    # The zero-bid-floored credit the order was actually built at, not a re-quote.
    assert payload["signal"]["credit"] == pytest.approx(make_signal().credit)
    assert payload["proposal"]["contracts"] == PROPOSAL.contracts
    assert payload["proposal"]["max_loss"] == PROPOSAL.max_loss
    assert payload["proposal"]["symbol"] == PROPOSAL.symbol


async def test_dry_run_marking_block_is_byte_identical_to_shadows():
    """One shape, two writers. The marker decodes ONE payload layout, so a drift
    between `shadow.would_submit` and `entry.dry_run` breaks scoring silently —
    which is why both go through `markable_payload` rather than each spelling the
    dict out."""
    from argus.shadow.outcome import markable_payload

    h = build_entry(dry_run=True)
    await h.submit()

    payload = h.audit.payload("entry.dry_run")
    expected = markable_payload(make_signal(), PROPOSAL)
    assert {k: payload[k] for k in expected} == expected


async def test_dry_run_addition_is_purely_additive():
    """Nothing that already read this record may see a different value. The two
    contributed keys (`signal`, `proposal`) must also not collide with the order
    ack's keys, which are merged into the same dict."""
    h = build_entry(dry_run=True)
    await h.submit()

    payload = h.audit.payload("entry.dry_run")
    # The pre-existing fields, unchanged.
    assert payload["candidate_id"] == CANDIDATE
    assert payload["submitted"] is True
    assert payload["walked"] is False
    assert "no broker order id" in payload["why_not_walked"]
    assert payload["guard"]["blocked"] is False
    assert payload["guard"]["external_identifier"] == IDENTIFIER
    # The order ack, unchanged — and `order` is still the ORDER, not overwritten.
    assert payload["dry_run"] is True
    assert payload["order"]["external_identifier"] == IDENTIFIER
    assert payload["order"]["broker_order_id"] is None
    assert payload["warnings"] == [] and payload["errors"] == []
    # Exactly two keys added, and no more.
    assert set(payload) == {
        "candidate_id", "submitted", "walked", "why_not_walked", "guard",
        "order", "dry_run", "buying_power_effect", "fees", "warnings", "errors",
        "signal", "proposal",
    }
    # Still JSON-encodable under the audit log's canonical rules (no NaN/inf) —
    # `signal.underlying_price` is the one field that can be non-finite.
    json.dumps(payload, allow_nan=False)


async def test_the_emitted_dry_run_record_decodes_through_the_real_marker():
    """The end-to-end contract, with no hand-written payload anywhere in it: what
    `_dry_run` actually writes is fed to the marker's own decoder and must come
    out as a scoreable trade idea at the right strikes, credit and size."""
    from argus.shadow.marker import DRY_RUN, _decode
    from argus.shadow.outcome import mark, trade_ideas

    h = build_entry(dry_run=True)
    await h.submit()

    proposal = _decode(
        1, "2026-07-29T18:30:00.000000Z", DRY_RUN,
        json.dumps(h.audit.payload("entry.dry_run")),
    )
    assert proposal is not None, "the marker could not decode its own source record"

    (idea,) = trade_ideas([proposal])
    assert (idea.side, idea.short_strike, idea.long_strike) == ("put", 4975.0, 4970.0)
    assert idea.contracts == PROPOSAL.contracts
    assert idea.proposals == 1, "a dry run records once per candidate, never per tick"
    # SPX settled at 5000 — a 4975/4970 put spread keeps the whole credit.
    assert mark(idea, 5000.0).pnl == pytest.approx(idea.credit * 100 * idea.contracts)


async def test_the_dry_run_candidate_id_is_the_marker_dedup_key():
    """`candidate_id` and `trade_ideas`' grouping key are the same tuple by
    construction (`build.candidate_id`), which is what lets a dry-run record and a
    shadow record of the same spread collapse into one idea rather than two."""
    from argus.shadow.marker import DRY_RUN, _decode

    h = build_entry(dry_run=True)
    await h.submit()
    payload = h.audit.payload("entry.dry_run")

    decoded = _decode(1, "2026-07-29T18:30:00.000000Z", DRY_RUN, json.dumps(payload))
    assert payload["candidate_id"] == (
        f"{TRADING_DATE:%Y%m%d}-{decoded.side}-"
        f"{decoded.short_strike:g}-{decoded.long_strike:g}"
    )


async def test_finished_addition_is_purely_additive():
    """`entry.finished` gained the same marking block on 2026-08-13, and the same
    rule applies: nothing that already read this record may see a different value.

    The block goes into the RECORD only. The alert payloads built alongside it are
    read on a phone and repeating the strikes in a second nesting buys nothing —
    so `signal`/`proposal` must appear here and NOT in what `_alert` was handed.
    """
    h = build_entry(dry_run=False)
    await h.submit()

    payload = h.audit.payload("entry.finished")
    # The pre-existing fields, unchanged.
    assert payload["candidate_id"] == CANDIDATE
    assert payload["requested_contracts"] == PROPOSAL.contracts
    assert payload["max_loss"] == PROPOSAL.max_loss
    assert payload["external_identifier"] == IDENTIFIER
    assert "outcome" in payload and "filled_contracts" in payload
    # Exactly two keys added, and no more. `drift` is present only when a drift
    # check ran, so it is excluded rather than asserted either way.
    #
    # `mutation_error` joined the set on 2026-09-04, from `WalkResult.to_payload()` —
    # additive in exactly the sense this test polices: every key above still carries
    # the value it always did, and the new one is `None` on every outcome but
    # `NO_ORDER_EXISTS`. The closed set is the point; a field arriving here without
    # anyone noticing is what it exists to prevent.
    assert set(payload) - {"drift"} == {
        "candidate_id", "requested_contracts", "max_loss",
        "outcome", "external_identifier", "broker_order_id", "steps",
        "filled_contracts", "final_limit_price", "cancel_requested",
        "recovered_disconnects", "reason", "reject_reason", "mutation_error",
        "last_status", "signal", "proposal",
    }
    json.dumps(payload, allow_nan=False)


async def test_the_emitted_finished_record_decodes_through_the_real_marker():
    """The end-to-end contract for the LIVE path, no hand-written payload in it.

    This is the check that would have caught Fix F before live fire: the record
    the live entry path actually writes, fed to the marker's own decoder, must
    come out as a scoreable trade idea. Before 2026-08-13 it decoded to nothing.
    """
    from argus.shadow.marker import FINISHED, PROPOSAL_CATEGORIES, _decode
    from argus.shadow.outcome import mark, trade_ideas

    assert FINISHED in PROPOSAL_CATEGORIES, (
        "writing the block is only half the fix — the marker has to read the category"
    )

    h = build_entry(dry_run=False)
    await h.submit()

    proposal = _decode(
        1, "2026-07-29T18:30:00.000000Z", FINISHED,
        json.dumps(h.audit.payload("entry.finished")),
    )
    assert proposal is not None, "the marker could not decode its own source record"

    (idea,) = trade_ideas([proposal])
    assert (idea.side, idea.short_strike, idea.long_strike) == ("put", 4975.0, 4970.0)
    assert idea.contracts == PROPOSAL.contracts
    assert mark(idea, 5000.0).pnl == pytest.approx(idea.credit * 100 * idea.contracts)


async def test_the_marking_block_is_byte_identical_across_all_three_writers():
    """One constructor, three writers. A writer that drifted by one key name would
    produce records that decode to nothing, and nobody would find out until a
    marking run weeks later reported zero — twice now, that is exactly what
    happened."""
    from argus.shadow.outcome import markable_payload

    live = build_entry(dry_run=False)
    await live.submit()
    dry = build_entry(dry_run=True)
    await dry.submit()

    expected = markable_payload(make_signal(), PROPOSAL)
    for category, h in (("entry.finished", live), ("entry.dry_run", dry)):
        payload = h.audit.payload(category)
        assert {k: payload[k] for k in ("signal", "proposal")} == expected, category


@pytest.mark.parametrize("dry_run", [True, False])
async def test_a_conflicting_order_stops_the_submit_in_BOTH_modes(dry_run):
    """The guard runs before the mode branch, so dry-run and live share one decision
    rather than each having a copy of it — which is exactly what the dry-run mode is
    for: rehearsing the real path, not a parallel one."""
    broker = Broker()
    existing = await broker.submit(_working_order(), dry_run=False)
    broker.set_status(existing.order.broker_order_id, OrderStatus.LIVE)
    h = build_entry(broker, dry_run=dry_run)
    await h.submit()

    assert h.audit.payload("order.duplicate_blocked")["blocked"] is True
    assert "entry.dry_run" not in h.audit.events
    assert len(broker.submitted) == 1, "only the pre-existing order was ever sent"
    assert h.halt.calls == [] and not h.stop.is_set(), "a block is not a failure"


def _working_order():
    """An order already at the broker under OUR identifier — the conflict case."""
    from argus.execution.models import LegAction, Order, OrderDirection, OrderLeg

    return Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P4975", LegAction.SELL_TO_OPEN, 2),
            OrderLeg("SPXW  P4970", LegAction.BUY_TO_OPEN, 2),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=IDENTIFIER,
        candidate_id=CANDIDATE,
    )


# -- the underlying-drift breaker ---------------------------------------------


def _check(reference: float = 4990.0, threshold: float = 3.0, spot=None):
    return UnderlyingDriftCheck(
        reference=reference, threshold_points=threshold, spot=spot or (lambda: reference)
    )


def test_movement_inside_the_threshold_lets_the_walk_continue():
    assert _check(spot=lambda: 4992.0)() is None
    assert _check(spot=lambda: 4988.0)() is None


def test_movement_at_or_past_the_threshold_abandons_the_walk_in_either_direction():
    """Symmetric on purpose. A put spread is hurt by a fall and a call spread by a
    rally, and the breaker is about the reference no longer describing the market —
    not about which way is bad for this particular side."""
    assert "moved 3.00 points" in (_check(spot=lambda: 4987.0)() or "")
    assert "moved 4.50 points" in (_check(spot=lambda: 4994.5)() or "")


def test_an_unreadable_underlying_abandons_rather_than_reading_as_no_drift():
    """`LegQuotes.spot` fails closed to None on a missing or stale quote. "I cannot
    tell whether the market moved" is not evidence that it did not — and the
    pre-trade gate would refuse this same candidate on this same feed right now."""
    assert "unreadable or stale" in (_check(spot=lambda: None)() or "")
    assert _check(spot=lambda: float("nan"))() is not None


def test_a_candidate_with_no_recorded_arrival_price_abandons_too():
    """NaN means "not recorded" (chunk 4's synthetic exit signals carry it). It must
    never become a silent comparison against 0.0, which would read as thousands of
    points of drift or — worse, if the sign worked out — as none."""
    assert "no gate-time underlying price" in (_check(reference=float("nan"))() or "")


def test_the_reference_is_the_arrival_price_and_never_rolls_forward():
    """Every cycle is compared against the price the GATE saw. A rolling comparison
    against the previous reading would let the market walk any distance at all, one
    sub-threshold step at a time — precisely the drift worth stopping."""
    prices = iter([4991.0, 4992.0, 4993.1])
    check = _check(spot=lambda: next(prices))

    # No single step is more than 1.1 points, so a rolling comparison would never
    # fire. Against the arrival price the third reading is 3.1 points out.
    assert check() is None
    assert check() is None
    assert check() is not None

    assert check.last["reference_underlying"] == 4990.0


def test_the_observation_is_recorded_as_values_not_prose():
    check = _check(spot=lambda: 4993.5)
    check()
    assert check.last == {
        "reference_underlying": 4990.0,
        "threshold_points": 3.0,
        "spot": 4993.5,
        "drift_points": pytest.approx(3.5),
    }


def test_a_non_finite_observation_never_reaches_the_audit_payload():
    """`entry.finished` is Tier 1 and the audit log refuses what `json` cannot
    encode — a refused Tier 1 write halts trading. An unreadable quote must not be
    able to halt the process on its way into the record."""
    check = _check(reference=float("nan"), spot=lambda: float("inf"))
    check()
    assert check.last["reference_underlying"] is None
    assert check.last["spot"] is None
    json.dumps(check.last)


async def test_no_spot_source_leaves_the_breaker_disarmed():
    """Backward compatibility with chunk 5 as already built: an executor wired
    without a quote source arms nothing and the walk is bounded only by its ladder."""
    armed: list[object] = []
    h = build_entry(walk_result=result(WalkOutcome.FILLED, filled_contracts=2))
    h.entry._loop_factory = lambda **kw: (armed.append(kw.get("drift_check")), h.stub)[1]

    await h.submit()

    assert armed == [None]
    assert "drift" not in h.audit.payload("entry.finished")


async def test_a_drifting_underlying_ends_the_entry_quietly_with_the_numbers_recorded():
    """End to end through the REAL loop: the candidate is gated at 4990, the market
    is at 4995, and the walk is abandoned rather than stepped down a ladder priced
    off a level that no longer exists."""
    broker = Broker()
    h = build_entry(broker, spot=lambda: 4995.0)

    await h.submit()

    payload = h.audit.payload("entry.finished")
    assert payload["outcome"] == "drifted"
    assert payload["drift"]["reference_underlying"] == 4990.0
    assert payload["drift"]["spot"] == 4995.0
    assert payload["drift"]["drift_points"] == pytest.approx(5.0)
    assert h.alerts == [], "a rail working correctly must not page anyone"
    assert h.halt.calls == [] and not h.stop.is_set()
    assert broker.replaced == [], "the price never stepped off the stale reference"
    assert broker.cancelled == [payload["broker_order_id"]], (
        "the order must not be left working at a price the loop has abandoned"
    )


async def test_a_steady_underlying_lets_the_entry_walk_normally():
    """The other half of the same wiring: with the market where the gate saw it, the
    breaker is silent and the walk runs its ladder to the walk-away threshold."""
    broker = Broker()
    h = build_entry(broker, spot=lambda: 4990.5)

    await h.submit()

    payload = h.audit.payload("entry.finished")
    assert payload["outcome"] == "walked_away"
    assert broker.replaced, "the walk must have actually stepped"
    # Recorded on EVERY outcome, not only when it fires: a near-miss is the
    # observation the threshold gets tuned from.
    assert payload["drift"]["drift_points"] == pytest.approx(0.5)


# -- a failed DRY RUN abandons the candidate, it does not end the day -----
#
# Regression for 2026-08-11: the first sandbox dry-run pass ended the whole trading
# day twenty minutes in, on its first candidate, because cert's nginx returned a
# 502. Nothing was at stake (a dry run mints no order id) and the real path would
# have recovered via PriceWalkingLoop._recover, so the mode built to DE-RISK
# execution was the only one with no retry on the submit.


def _other_candidate(captured_at: float) -> SpreadSignal:
    """A second, DISTINCT trade idea. `candidate_id` keys on the strike pair, so a
    different candidate needs different strikes — make_signal hardcodes 4975/4970."""
    short = OptionQuote("SPXW  P4950", 4950.0, "put", -0.15, 1.20, 1.40)
    long_ = OptionQuote("SPXW  P4945", 4945.0, "put", -0.10, 0.0, 0.30)
    return SpreadSignal(
        underlying="SPXW",
        side="put",
        short_leg=short,
        long_leg=long_,
        credit=short.floored_mid - long_.floored_mid,
        width=5.0,
        captured_at=captured_at,
        underlying_price=4990.0,
    )


class SubmitFailsBroker(Broker):
    """Fails ONLY on submit. `FakeOrderExecution.fail_next` fires on the next port
    call of any kind, which here is the guard's `get_live_orders()` — and a guard
    failure is correctly soft (`_end_entries_today`), so it would test the wrong
    branch."""

    def __init__(self, exc: Exception, *, times: int = 1) -> None:
        super().__init__()
        self._submit_exc = exc
        self._remaining = times

    async def submit(self, order, *, dry_run):
        if self._remaining > 0:
            self._remaining -= 1
            raise self._submit_exc
        return await super().submit(order, dry_run=dry_run)



async def test_a_failed_dry_run_does_not_end_the_day():
    broker = SubmitFailsBroker(
        TastytradeError("Couldn't parse response: <html>502 Bad Gateway")
    )
    h = build_entry(broker, dry_run=True)
    await h.submit()

    assert "entry.aborted_day" not in h.audit.events, (
        "a dry run routes nothing, so a broker blip must not cost the session"
    )
    assert not h.stop.is_set(), "the session stop event must NOT be set"
    assert h.halt.calls == [], "and the process must not be halted either"


async def test_a_failed_dry_run_is_recorded_tier_1_and_alerted():
    broker = SubmitFailsBroker(TastytradeError("502 Bad Gateway"))
    h = build_entry(broker, dry_run=True)
    await h.submit()

    payload = h.audit.payload("entry.dry_run_failed")
    assert payload["candidate_id"]
    assert payload["error_type"] == "TastytradeError"
    assert "502" in payload["error"]
    assert "entry_dry_run_failed" in h.alerts, (
        "a human still needs to know the broker refused the payload"
    )
    assert "entry.dry_run" not in h.audit.events, "it did not succeed"


async def test_the_session_keeps_trading_after_a_failed_dry_run():
    """The point of the change: candidate N+1 is still attempted."""
    broker = SubmitFailsBroker(TastytradeError("502 Bad Gateway"))
    h = build_entry(broker, dry_run=True)

    await h.submit()                                  # fails
    await h.submit(signal=_other_candidate(h.clock.now))
    assert "entry.dry_run" in h.audit.events, "the next candidate must still run"


async def test_a_failed_dry_run_retires_only_that_candidate():
    """It is bounded, not a retry loop: `_attempted` claims the candidate before the
    dry run, so a systemic failure costs one record per DISTINCT idea, not a flood."""
    broker = SubmitFailsBroker(TastytradeError("502 Bad Gateway"))
    h = build_entry(broker, dry_run=True)

    await h.submit()
    submits_after_failure = len(broker.submitted)
    await h.submit()                                  # same candidate, re-proposed
    assert len(broker.submitted) == submits_after_failure, (
        "the same idea must not be re-sent"
    )
    assert "tier2:entry.already_attempted" in h.audit.events, (
        "the re-proposal is Tier 2 — high volume, costs no broker round trip"
    )


async def test_live_fire_submit_failure_is_still_the_HARD_region():
    """Unchanged on purpose: past _walk_and_report an order may exist."""
    h = build_entry(dry_run=False, walk_raises=TastytradeError("502 Bad Gateway"))
    await h.submit()

    assert h.halt.calls, "a live-fire failure must still halt the process"
    assert h.halt.calls[0][1] == "entry_execution_exception"


# -- an unreachable GUARD in dry-run mode also abandons, not ends the day --
#
# Same regression, one step earlier in the path. The guard makes a broker round trip
# (`get_live_orders()`), so during a cert 502 episode a candidate arriving while the
# account cache was still fresh enough for the gate would have cost the day.


async def test_an_unreachable_guard_in_dry_run_does_not_end_the_day():
    broker = Broker()
    broker.fail_next(TastytradeError("502 Bad Gateway"))   # hits get_live_orders
    h = build_entry(broker, dry_run=True)
    await h.submit()

    assert "entry.aborted_day" not in h.audit.events
    assert not h.stop.is_set(), "the session must survive an unanswerable guard"
    payload = h.audit.payload("entry.dry_run_failed")
    assert payload["kind"] == "entry_guard_unreachable", (
        "the record must say WHICH step failed, not just that one did"
    )
    assert "entry_guard_unreachable" in h.alerts


async def test_an_unreachable_guard_in_LIVE_FIRE_still_ends_entries():
    """Unchanged: with real orders possible, being blind to what is already working
    at the exchange is a real reason to stand down. What changed in 2026-08-26 is
    only the evidence required — SUSTAINED blindness rather than one round trip; what
    changed in 2026-09-01 is only how far the stand-down reaches."""
    broker = DeadGuardBroker(TastytradeError("502 Bad Gateway"))
    h = build_entry(broker, dry_run=False)
    await _drive_streak(h, MAX_STREAK)

    payload = h.audit.payload("entry.aborted_day")
    assert payload["kind"] == "entry_guard_unreachable"
    assert h.entry.entries_ended is not None, "live-fire must still stand entries down"
    assert not h.stop.is_set(), "and it must still leave the session up"
    assert h.halt.calls == [], "it is SOFT — the process stays up"


# -- a SINGLE unanswered guard check no longer costs the day ------------------
#
# Regression for 2026-08-25: 10:00:00 ET, the day's first tick, a clean 5-wide
# 7700/7705 the gate had approved, and one timed-out `get_live_orders()` against a
# cert that was 502-ing all morning. No order was ever at risk — the mechanism did
# exactly what it was built to do — but it cost the remaining ~5 hours of entry
# window. Two stages now: a bounded retry, then a streak.
# → docs/decisions/2026-08-26-entry-guard-unreachable-retry.md


async def test_a_single_transient_guard_failure_costs_nothing():
    """The headline case. One bad call, the next one answers, the entry proceeds."""
    broker = DeadGuardBroker(TastytradeError("502 Bad Gateway"), times=1)
    h = build_entry(broker, dry_run=False)
    await h.submit()

    assert broker.submitted, "the candidate must go on to be submitted normally"
    assert "entry.aborted_day" not in h.audit.events, "one blip is not a day"
    assert "entry.guard_unreachable_abandoned" not in h.audit.events, (
        "and it is not even worth retiring the candidate over"
    )
    assert not h.stop.is_set()
    assert "tier2:entry.guard_unreachable_attempt" in h.audit.events, (
        "the attempt that failed is still visible, at Tier 2"
    )


async def test_a_spent_burst_retires_the_candidate_and_the_session_continues():
    broker = DeadGuardBroker(TastytradeError("502 Bad Gateway"))
    h = build_entry(broker, dry_run=False)
    await h.submit()

    payload = h.audit.payload("entry.guard_unreachable_abandoned")
    assert payload["attempts"] == 3, "the whole burst is spent before giving up"
    assert payload["consecutive"] == 1
    assert payload["max_consecutive"] == MAX_STREAK
    assert payload["error_type"] == "TastytradeError", "the ORIGINAL failure, not the wrapper"
    assert "entry_guard_unreachable_abandoned" in h.alerts, (
        "every spent burst pages, at the same moment it always did"
    )
    assert not h.stop.is_set(), "one blind idea is not a blind day"
    assert broker.submitted == [], "and no order may go out on an unanswered check"


async def test_the_abandon_alert_names_the_candidate_at_the_TOP_level():
    """`SlackNotifier` scopes its 900s throttle on `(kind, scope)` and reads the
    scope from the top level of the detail (2026-08-21). Nested, two candidates'
    pages would coalesce and the second would arrive as a bare `(+1 suppressed)`
    up to 15 minutes late — which is the whole failure mode this alert is about."""
    from argus.notify import alert_scope

    broker = DeadGuardBroker(TastytradeError("502"))
    h = build_entry(broker, dry_run=False)
    await h.submit()

    detail = h.audit.payload("entry.guard_unreachable_abandoned")
    assert alert_scope(detail), "the throttle must be able to find a scope"


async def test_the_burst_backs_off_between_attempts_and_stops_at_three():
    broker = DeadGuardBroker(TastytradeError("502 Bad Gateway"))
    h = build_entry(broker, dry_run=False)
    await h.submit()

    assert broker.refused == 3, "three attempts, then give up on this candidate"
    assert h.clock.slept == [1.0, 2.0], (
        "1s then 2s — account/stream.py's ladder, and only BETWEEN attempts"
    )


async def test_the_guard_check_is_bounded_by_its_OWN_timeout():
    """A broker that accepts the socket and never answers must not hang the signal
    loop. Without this the bound is `broker.http_timeout_s` (30s), sized for a cold
    first call from a new host — and httpx reads a scalar as connect/read/write/pool
    INDEPENDENTLY, so the inherited worst case was far worse than the 30 it names."""
    broker = HangingGuardBroker()
    h = build_entry(broker, dry_run=False, execution={"guard_timeout_s": 0.01})
    await asyncio.wait_for(h.submit(), timeout=5.0)

    assert broker.hangs == 3, "each attempt is bounded, not just the burst"
    payload = h.audit.payload("entry.guard_unreachable_abandoned")
    assert payload["error_type"] == "TimeoutError"
    # `asyncio.timeout` raises a MESSAGELESS `TimeoutError`; the path names what it
    # waited for and how long rather than logging a bare type with an empty string.
    assert "did not complete within 0.01s" in payload["error"]
    assert broker.submitted == [], "a bound that expires is still no answer"


async def test_N_minus_1_spent_bursts_do_not_end_entries_and_the_Nth_does():
    broker = DeadGuardBroker(TastytradeError("502 Bad Gateway"))
    h = build_entry(broker, dry_run=False)

    await _drive_streak(h, MAX_STREAK - 1)
    assert h.entry.entries_ended is None, (
        f"{MAX_STREAK - 1} blind ideas must not end today's entries"
    )
    assert "entry.aborted_day" not in h.audit.events

    await _drive_streak(h, 1, start=MAX_STREAK - 1)
    payload = h.audit.payload("entry.aborted_day")
    assert payload["kind"] == "entry_guard_unreachable", "the ORIGINAL kind, unchanged"
    assert payload["consecutive"] == MAX_STREAK
    assert h.entry.entries_ended is not None
    assert not h.stop.is_set(), "entries, not the session"
    assert h.halt.calls == [], "still SOFT: nothing was submitted, so nothing to protect"


async def test_an_answered_check_resets_the_streak():
    """Reset-on-success, matching `AccountStateCache._note_refresh_success`.
    Without it a long session accumulates unrelated blips into a day-ending count."""
    # Dead for the first two candidates' bursts (3 attempts each), then alive.
    broker = DeadGuardBroker(TastytradeError("502 Bad Gateway"), times=2 * 3)
    h = build_entry(broker, dry_run=False)

    await _drive_streak(h, MAX_STREAK - 1)          # streak = 2
    await h.submit(signal=_nth_candidate(9, h.clock.now))   # answers -> streak = 0
    assert broker.submitted, "the third candidate really did get a verdict"

    broker.remaining = None                         # dead again, from here on
    await _drive_streak(h, MAX_STREAK - 1, start=MAX_STREAK)
    assert not h.stop.is_set(), (
        "two more after a reset is two, not four — the day must survive"
    )
    assert h.audit.payload("entry.guard_unreachable_abandoned")["consecutive"] == 1


async def test_a_stop_during_the_burst_abandons_and_never_blames_the_broker():
    """The close, SIGTERM, or a kill switch arriving through `_halt_process` all set
    this event. It says nothing about the broker, so it must neither spend another
    attempt nor count toward the streak — and it must not write `entry.aborted_day`,
    which would attribute a scheduled close to a cert failure."""
    broker = DeadGuardBroker(ConnectionError("cert down"))
    h = build_entry(broker, dry_run=False)
    h.stop.set()
    await h.submit()

    assert broker.refused == 1, "no further attempt after the session is stopping"
    assert "entry.aborted_day" not in h.audit.events
    payload = h.audit.payload("entry.guard_unreachable_abandoned")
    assert payload["consecutive"] == 0, "a stop is not evidence of unreachability"


async def test_the_day_ending_record_keeps_every_field_it_had():
    """`entry.aborted_day` gained two fields and changed none. Anything already
    reading `kind`, `candidate_id`, `error_type` or `error` sees what it always saw
    — including `error_type`, which names cert's failure and not the wrapper.

    The CATEGORY is deliberately not renamed alongside the method (2026-09-01):
    `tools.mark`, `tools.desk` and the marker tests read it, so it is an interface.
    The ALERT kind did change, on purpose — see the test below."""
    broker = DeadGuardBroker(TastytradeError("502 Bad Gateway"))
    h = build_entry(broker, dry_run=False)
    await _drive_streak(h, MAX_STREAK)

    payload = h.audit.payload("entry.aborted_day")
    assert payload["kind"] == "entry_guard_unreachable"
    assert payload["candidate_id"]
    assert payload["error_type"] == "TastytradeError"
    assert "502" in payload["error"]


async def test_no_retry_can_reach_a_submit_without_a_verdict():
    """THE invariant, asserted as a property of the module rather than of one path:
    every way out of an unanswerable guard returns from `submit` before
    `_walk_and_report`. A guard that never answers can never produce an order."""
    for times in (None, 1_000):
        broker = DeadGuardBroker(TastytradeError("502"), times=times)
        h = build_entry(broker, dry_run=False)
        await _drive_streak(h, MAX_STREAK + 2)
        assert broker.submitted == [], f"an order went out with no verdict (times={times})"


# -- dry-run is untouched by all of the above ---------------------------------


async def test_dry_run_makes_exactly_ONE_guard_attempt_and_never_retries():
    """The retry is live-fire only. Dry run routes nothing, so there is nothing for
    patience to protect — and its own soft-abandon has been correct since
    2026-08-11. Byte-identical: one call, no backoff, the raw exception recorded."""
    broker = DeadGuardBroker(TastytradeError("502 Bad Gateway"))
    h = build_entry(broker, dry_run=True)
    await h.submit()

    assert broker.refused == 1, "dry run must not retry"
    assert h.clock.slept == [], "and must not back off either"
    payload = h.audit.payload("entry.dry_run_failed")
    assert payload["kind"] == "entry_guard_unreachable"
    assert payload["error_type"] == "TastytradeError", "the raw exception, not the wrapper"
    assert "entry.guard_unreachable_abandoned" not in h.audit.events
    assert not h.stop.is_set()


async def test_a_dry_run_guard_failure_never_ends_the_day_however_often_it_repeats():
    """No streak in this mode: the counter is live-fire state and the dry-run branch
    never touches it, so a whole session of 502s stays a session of 502s."""
    broker = DeadGuardBroker(TastytradeError("502 Bad Gateway"))
    h = build_entry(broker, dry_run=True)
    await _drive_streak(h, MAX_STREAK + 2)

    assert "entry.aborted_day" not in h.audit.events
    assert not h.stop.is_set()
    assert h.halt.calls == []


async def test_a_failed_dry_run_records_which_step_failed():
    """The submit-failure path carries its own kind, so the two are distinguishable
    in the audit trail."""
    broker = SubmitFailsBroker(TastytradeError("502 Bad Gateway"))
    h = build_entry(broker, dry_run=True)
    await h.submit()

    assert h.audit.payload("entry.dry_run_failed")["kind"] == "entry_dry_run_failed"
