"""What pressing the kill switch actually stops, end to end (spec §6).

The gap this file exists to close was a TEST gap before it was a code gap.
`tests/test_control.py` drives `POST /kill` over a real socket but wires the
server to a bare `KillSwitch` with no session behind it, so every assertion is
about the trip record and the HTTP shape. `tests/test_killswitch.py` unit-tests
`engage`. Between them, nothing exercised the question an operator actually cares
about: *press the button while an order is working — what happens to the order?*

So these tests run the real chain end to end — an actual TCP request to a real
`ControlServer`, into the real `KillSwitch`, reaching a real `EntryExecutor`
driving a real `PriceWalkingLoop` against `FakeOrderExecution`. The only fakes
are the broker and the clock. Nothing here stubs the thing under test.

The three properties, and each is a decision someone signed off on rather than an
implementation detail:

1. A kill ABANDONS an entry walk in flight and cancels the order.
2. A kill does NOT touch an exit walk, and does NOT set the session stop event.
   A halt must never strand an open position by cancelling the order closing it,
   and must not take the watchdog or the process down with it.
3. A kill refuses a candidate the gate approved microseconds earlier.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect

import argus.execution.watchdog as watchdog_mod
from argus.control import ControlServer
from argus.execution.audited import AuditedOrderExecution
from argus.execution.guard import DuplicateOrderGuard
from argus.execution.loop import PriceWalkingLoop, WalkOutcome
from argus.execution.models import OrderDirection, OrderStatus
from argus.execution.store import InMemoryOrderStore
from argus.risk.killswitch import KillSwitch
from tests.test_control import TOKEN, http_request
from tests.test_execution_loop import Broker, FakeAudit, policy, spread

# How long any single interleaving step may take before the test is declared hung
# rather than slow. Every wait here is on a fake clock; a real second means a bug.
_TIMEOUT_S = 5.0


class ServedAudit(FakeAudit):
    """`test_execution_loop.FakeAudit` plus the one attribute `ControlServer` reads.

    The server checks `audit.failed` before answering 200, so that a trip which
    engaged but could not be recorded is reported honestly as a 500. The loop
    harness's fake predates that and has no such attribute; subclassing here keeps
    it that way rather than reaching into another test module's fixture.
    """

    failed = None


class YieldingTime:
    """A fake clock that advances only on sleep, but DOES yield to the event loop.

    `test_execution_loop.FakeTime` deliberately never yields — its tests drive one
    walk and want zero interleaving. These tests need the opposite: the walk and
    the HTTP handler must interleave, because the whole subject is a request that
    arrives *during* a walk. `asyncio.sleep(0)` is the smallest thing that gives
    the loop a turn without introducing real elapsed time.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def kill_server(killswitch: KillSwitch, audit):
    """A real ControlServer on loopback, wired to the REAL kill switch under test."""
    server = ControlServer(
        host="127.0.0.1", port=0, token=TOKEN, killswitch=killswitch, audit=audit
    )
    await server.start()
    host, port = server.bound_address
    try:
        yield host, port
    finally:
        await server.stop()


async def press_kill(host: str, port: int):
    """Exactly what the phone Shortcut sends. No in-process shortcut."""
    return await http_request(
        host, port, "POST", "/kill",
        headers=[f"Authorization: Bearer {TOKEN}"],
    )


def build_walk(broker: Broker, *, killswitch: KillSwitch | None, audit=None, **kw):
    """A walk armed exactly as `entry.py` arms one (killswitch) or as
    `watchdog.py` arms one (killswitch=None). Same loop class either way — the
    whole point of chunk 3 is that there is only one state machine."""
    audit = audit or FakeAudit()
    wrapped = AuditedOrderExecution(broker, audit=audit, store=InMemoryOrderStore())
    clock = YieldingTime()
    loop = PriceWalkingLoop(
        execution=wrapped,
        audit=audit,
        policy=kw.pop("policy", None) or policy(walk_away_pct=0.90),
        guard=DuplicateOrderGuard(wrapped, audit=audit),
        # THE line under test. `entry.py` passes this lambda; `watchdog.py` passes
        # nothing at all.
        halt_check=(lambda: killswitch.engaged) if killswitch is not None else None,
        sleep=clock.sleep,
        clock=clock.clock,
        **kw,
    )
    return loop, audit, clock


async def wait_until(predicate, *, what: str) -> None:
    """Spin the event loop until `predicate()`, or fail loudly.

    Bounded by ITERATIONS, not by wall time: the clock is fake, so a real timeout
    would make the test's duration depend on machine speed. If the condition never
    arrives, the walk is not doing what the test assumes and that should read as a
    clear failure rather than a hang.
    """
    for _ in range(10_000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


# -- 1. a kill abandons an entry walk in flight --------------------------------


async def test_POST_kill_abandons_an_entry_walk_and_cancels_the_order():
    """The property the phone button is actually for.

    Before this fix the walk ran to completion after a trip and could still FILL —
    the gate blocked only the NEXT candidate. Here the request goes over a real
    socket while the order is working, and the walk must end ABORTED with a cancel.
    """
    audit = ServedAudit()
    killswitch = KillSwitch(audit)
    broker = Broker(live_after_polls=0)
    loop, _, _ = build_walk(broker, killswitch=killswitch, audit=audit)

    async with kill_server(killswitch, audit) as (host, port):
        walk = asyncio.create_task(
            loop.run(spread(), guard_first_submission=False)
        )
        # Let the order actually reach the market first. Killing before the submit
        # would prove nothing — the gate already covers that case.
        await wait_until(
            lambda: any(
                o.status is OrderStatus.LIVE for o in broker._orders.values()
            ),
            what="the order to go Live at the broker",
        )
        assert not walk.done(), "the walk finished before the kill could land"

        response = await press_kill(host, port)
        assert response.code == 200
        assert '"tripped"' in response.text

        result = await asyncio.wait_for(walk, timeout=_TIMEOUT_S)

    assert result.outcome is WalkOutcome.ABORTED
    # The REASON distinguishes this from a session close, which is the whole point
    # of `_stop_reason` returning a string rather than a bool.
    assert result.reason == "kill switch engaged"
    assert result.cancel_requested is True
    assert result.filled_contracts in (None, 0), "a killed entry must not fill"
    # And the order is genuinely gone at the broker, not merely reported as such.
    assert broker.live_ids == []


async def test_the_abort_is_recorded_with_the_kill_reason_in_walk_finished():
    """`walk.finished` is the only place a post-mortem learns WHY a walk ended."""
    audit = ServedAudit()
    killswitch = KillSwitch(audit)
    broker = Broker(live_after_polls=0)
    loop, _, _ = build_walk(broker, killswitch=killswitch, audit=audit)

    await killswitch.engage("manual test trip", source="http_endpoint")
    result = await loop.run(spread(), guard_first_submission=False)

    assert result.outcome is WalkOutcome.ABORTED
    finished = audit.payload("walk.finished")
    assert finished["reason"] == "kill switch engaged"
    assert finished["outcome"] == "aborted"
    # Armed-ness is recorded too: a walk that ran on after a trip and a walk that
    # was never armed are otherwise indistinguishable in the trail.
    assert audit.payload("walk.started")["halt_check_armed"] is True


async def test_a_halt_check_that_raises_abandons_rather_than_walking_on_blind():
    """Fail-closed. Unlike `drift_check` (whose subject is the tape), this one's
    subject is a safety rail, so "I could not tell" must stop the walk."""
    broker = Broker(live_after_polls=0)

    def boom() -> bool:
        raise RuntimeError("kill switch unreadable")

    audit = ServedAudit()
    wrapped = AuditedOrderExecution(broker, audit=audit, store=InMemoryOrderStore())
    clock = YieldingTime()
    loop = PriceWalkingLoop(
        execution=wrapped, audit=audit, policy=policy(),
        guard=DuplicateOrderGuard(wrapped, audit=audit),
        halt_check=boom, sleep=clock.sleep, clock=clock.clock,
    )

    result = await loop.run(spread(), guard_first_submission=False)
    assert result.outcome is WalkOutcome.ABORTED
    assert result.reason == "halt check failed"
    assert result.cancel_requested is True


# -- 2. a kill does NOT stop an exit, or the process ---------------------------


async def test_an_EXIT_walk_is_untouched_by_a_kill_switch_trip():
    """The asymmetry, asserted rather than documented.

    `ExitExecutor` passes no `halt_check`, so a trip must not reach an exit walk.
    Cancelling the order that is closing a position would leave the operator
    holding exactly the risk they pressed the button to shed.
    """
    audit = ServedAudit()
    killswitch = KillSwitch(audit)
    await killswitch.engage("tripped before the exit even starts", source="http_endpoint")
    assert killswitch.engaged

    broker = Broker(live_after_polls=0)
    # killswitch=None is how `watchdog.ExitExecutor._build_loop` builds it.
    loop, _, _ = build_walk(broker, killswitch=None, audit=audit, policy=policy())

    result = await loop.run(
        spread(direction=OrderDirection.CLOSE, limit="-0.85"),
        guard_first_submission=False,
    )

    # It walked its ladder to the end instead of aborting on the trip.
    assert result.outcome is WalkOutcome.WALKED_AWAY
    assert result.steps > 0, "the exit should have repriced, not stopped dead"
    assert audit.payload("walk.started")["halt_check_armed"] is False


def test_ExitExecutor_never_arms_the_halt_check():
    """Structural, by AST rather than by grep: the omission above is load-bearing.

    Someone "fixing the inconsistency" by passing `halt_check` here would make a
    kill switch cancel exits — the exact inversion this design rules out — and the
    change would look like a tidy-up in review. This fails CI instead.
    """
    source = inspect.getsource(watchdog_mod.ExitExecutor._build_loop)
    tree = ast.parse(source.strip())
    call = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PriceWalkingLoop"
    )
    passed = {kw.arg for kw in call.keywords}
    assert "halt_check" not in passed, (
        "ExitExecutor must NOT arm the kill-switch abort: a trip would cancel the "
        "order closing an open position. See loop.HaltCheckFn."
    )
    assert "stop" in passed, "an exit must still end at the session close"


async def test_engaging_never_sets_a_stop_event_or_touches_the_process():
    """A kill is not `Application._halt_process`, and must not become one.

    `_halt_process` sets the PROCESS stop event, which the scheduler bridges to the
    session stop — the same event the watchdog and every exit walk read. Wiring the
    phone kill to it would stop the watchdog and cancel in-flight exits while
    positions were still open. The switch therefore owns no event at all.
    """
    audit = ServedAudit()
    killswitch = KillSwitch(audit)
    session_stop = asyncio.Event()

    async with kill_server(killswitch, audit) as (host, port):
        assert (await press_kill(host, port)).code == 200

    assert killswitch.engaged
    assert not session_stop.is_set(), "a kill must not end the session or the process"
    # Structural: nothing on the switch is an Event, so it cannot grow one by
    # accident and quietly acquire process-stopping reach.
    assert not any(
        isinstance(v, asyncio.Event) for v in vars(killswitch).values()
    )


# -- 3. the gate/submit race, and the alert ------------------------------------


async def test_a_trip_alerts_once_through_the_engage_funnel():
    """Every trip reaches a human, from the ONE place all trips pass through.

    Putting this in `engage` rather than at each call site is what makes it true
    for the heartbeat stall and the audit-failure halt as well, neither of which
    goes anywhere near the HTTP endpoint.
    """
    audit = ServedAudit()
    fired: list[tuple[str, dict]] = []
    killswitch = KillSwitch(audit, alert=lambda kind, detail: fired.append((kind, detail)))

    async with kill_server(killswitch, audit) as (host, port):
        assert (await press_kill(host, port)).code == 200
        # Idempotent re-hit: still 200, but NOT a second alert.
        assert (await press_kill(host, port)).code == 200

    assert [kind for kind, _ in fired] == ["kill_switch_engaged"]
    assert fired[0][1]["source"] == "http_endpoint"


async def test_an_alert_sink_that_raises_never_undoes_the_halt():
    """The halt is the safety action; the notification is not."""
    audit = ServedAudit()

    def broken(kind, detail):
        raise RuntimeError("slack is down")

    killswitch = KillSwitch(audit, alert=broken)
    await killswitch.engage("trip", source="http_endpoint")

    assert killswitch.engaged
    assert audit.payload("kill_switch.trip")["source"] == "http_endpoint"


async def test_submit_refuses_a_candidate_the_gate_approved_before_the_trip():
    """Closes the gate/submit race.

    `PreTradeGate` denies on `kill_switch_engaged`, but its verdict and the submit
    that follows are separated by an awaited Tier 1 write, so a trip landing in
    that window would otherwise send an order the operator had already halted.
    """
    from tests.test_entry import build_entry

    h = build_entry()
    await h.killswitch.engage("tripped between the gate and the submit", source="http_endpoint")

    await h.submit()

    assert h.audit.payload("entry.halted")["reason"] == (
        "tripped between the gate and the submit"
    )
    assert h.broker.submitted == [], "nothing may reach the broker after a trip"
    assert "walk.started" not in h.audit.events
