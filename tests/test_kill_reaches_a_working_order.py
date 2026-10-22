"""A kill reaches an order working at the exchange EVEN WHEN THE FIRST CANCEL FAILS.

The non-negotiable is one sentence — a halt stops new entries immediately *"including
an entry order already working at the exchange"* — and until this file it was covered
by two tests that each hold one half of it:

  * `tests/test_kill_scope.py::test_the_order_a_KILL_abandons_is_registered_when_its_cancel_fails`
    drives the real chain from `POST /kill` down to a failed cancel, and stops at
    REGISTRATION. It asserts a residual exists; it never asks what becomes of it.
  * `tests/test_residual_orders.py::test_a_working_order_PAST_the_allowance_is_cancelled_once`
    starts from a HAND-BUILT registry and asserts the re-cancel is sent. It never
    asks how a residual gets there.

Two halves were enough while the second one sent nothing on the traded account:
`watchdog.unmanaged_order_cancel` was `false` in live from the 2026-09-15 cutover, so
the seam between them was `would_cancel` — recorded, inert. **v0.4.5 (2026-09-20)
promoted the flag in live, which made that seam load-bearing**, and the promotion was
argued from the kill-switch rule rather than from an observed live cancel (the gate
read found zero of those: no live walk has ever walked away). A promise that is now
carried end to end in production should be asserted end to end here.

So these tests span the whole sentence, in one chain, with nothing stubbed between the
socket and the broker:

    POST /kill  ->  KillSwitch  ->  halt_check  ->  _abort  ->  _request_cancel RAISES
                ->  _finish registers the residual  ->  the watchdog's poll
                ->  ResidualOrderRecheck.step  ->  the SECOND cancel  ->  order DEAD

**The wiring mirrors `session.py`.** The recheck's `cancel` is
`AuditedOrderExecution.cancel` — the same object, not a spy and not a second port —
because "P5 is one more caller of the call the walk already makes" is precisely the
argument the v0.4.5 decision rests on, and an independent spy would assert around it
rather than through it. `LiveOrders` comes from `get_live_orders()` on that same
execution port, the way `PositionWatchdog._recheck_residual_orders` gets it.

→ docs/decisions/2026-08-29-mid-session-cancel-recheck.md (P5)
→ docs/decisions/2026-09-19-live-cancel-recheck-promotion.md (the live promotion)
"""

from __future__ import annotations

import asyncio

from argus.execution.audited import AuditedOrderExecution
from argus.execution.guard import DuplicateOrderGuard
from argus.execution.loop import PriceWalkingLoop, WalkOutcome
from argus.execution.models import OrderStatus
from argus.execution.residual_orders import ResidualOrderRecheck
from argus.execution.store import InMemoryOrderStore
from argus.risk.killswitch import KillSwitch
from tests.support import make_config
from tests.test_execution_loop import Broker, policy, spread
from tests.test_kill_scope import (
    ServedAudit,
    YieldingTime,
    kill_server,
    press_kill,
    wait_until,
)

_TIMEOUT_S = 5.0
#: `exit_cancel_settle_s` in the test config — the allowance a just-registered
#: residual gets before a working sighting means anything. A cancel leaves an order
#: at `Cancel Requested`, not gone.
_SETTLE_S = 2.0


class FlakyCancelBroker(Broker):
    """A broker whose cancel DELETE fails the first `cancel_fails` times.

    This is 2026-08-28 at 10:10 ET: order 1424563's cancel came back a 502, the walk
    finished correctly, and the order was still working two hours later. The failure
    is scripted on the CANCEL specifically rather than through `fail_next`, which
    fires on whichever port call comes next — here that would be a status read.
    """

    def __init__(self, *, cancel_fails: int = 1, **kw) -> None:
        super().__init__(**kw)
        self.cancel_fails = cancel_fails
        self.cancel_calls: list[int] = []

    async def cancel(self, broker_order_id: int) -> None:
        self.cancel_calls.append(broker_order_id)
        if self.cancel_fails > 0:
            self.cancel_fails -= 1
            raise ConnectionError("502 Bad Gateway")
        await super().cancel(broker_order_id)


def build_chain(broker: Broker, *, cancel_enabled: bool):
    """The entry-side chain, wired as `session.py` wires it.

    One `AuditedOrderExecution` shared by the walk and the recheck, so the second
    cancel goes out through the SAME port as the first and is recorded at the same
    tier by the same decorator.
    """
    audit = ServedAudit()
    killswitch = KillSwitch(audit)
    wrapped = AuditedOrderExecution(broker, audit=audit, store=InMemoryOrderStore())
    clock = YieldingTime()
    alerts: list[tuple[str, dict]] = []
    recheck = ResidualOrderRecheck(
        audit=audit,
        config=make_config(
            environment="sandbox",
            watchdog={"unmanaged_order_cancel": cancel_enabled},
        ),
        # THE line this file exists for: the recheck's outbound cancel IS the walk's.
        cancel=wrapped.cancel,
        alert=lambda kind, detail: alerts.append((kind, detail)),
        clock=clock.clock,
    )
    loop = PriceWalkingLoop(
        execution=wrapped,
        audit=audit,
        policy=policy(walk_away_pct=0.90),
        guard=DuplicateOrderGuard(wrapped, audit=audit),
        halt_check=lambda: killswitch.engaged,
        residuals=recheck,
        sleep=clock.sleep,
        clock=clock.clock,
    )
    return loop, recheck, killswitch, audit, clock, alerts


async def kill_a_working_walk(loop, killswitch, broker, audit):
    """Press `/kill` over a real socket while the order is Live, and return the walk.

    Killing before the submit would prove nothing — the gate already refuses that
    case (`test_kill_scope.py`). The kill has to land while there is something at the
    exchange to reach.
    """
    async with kill_server(killswitch, audit) as (host, port):
        walk = asyncio.create_task(loop.run(spread(), guard_first_submission=False))
        await wait_until(
            lambda: any(
                o.status is OrderStatus.LIVE for o in broker._orders.values()
            ),
            what="the order to go Live at the broker",
        )
        assert not walk.done(), "the walk finished before the kill could land"
        response = await press_kill(host, port)
        assert response.code == 200
        return await asyncio.wait_for(walk, timeout=_TIMEOUT_S)


async def test_a_kill_whose_cancel_FAILS_still_reaches_the_order():
    """THE sentence, end to end, in one test.

    The first cancel raises, so the abandoned order is still Live at the broker when
    the walk finishes — which is exactly the state that went unnoticed for two hours
    on 2026-08-28. The recheck then re-asks on the watchdog's own poll answer, and
    the order is genuinely DEAD at the broker afterwards: asserted on the broker's
    state, never on a spy's call list.
    """
    broker = FlakyCancelBroker(cancel_fails=1, live_after_polls=0)
    loop, recheck, killswitch, audit, clock, alerts = build_chain(
        broker, cancel_enabled=True
    )

    result = await kill_a_working_walk(loop, killswitch, broker, audit)

    # -- half one: the kill landed, the cancel did not ------------------------
    assert result.outcome is WalkOutcome.ABORTED
    assert result.reason == "kill switch engaged"
    assert result.cancel_requested is False, "the DELETE raised — this is 2026-08-28"
    assert result.filled_contracts in (None, 0), "a killed entry must not fill"
    assert broker.live_ids == [result.broker_order_id], (
        "the order is STILL WORKING at the exchange after the kill — the state the "
        "recheck exists for. If this ever reads [] the fake stopped modelling the "
        "failure and the rest of this test proves nothing."
    )
    assert [r.broker_order_id for r in recheck.outstanding()] == [
        result.broker_order_id
    ]

    # -- the seam: the watchdog's poll, on the same port ----------------------
    live = await broker.get_live_orders()
    clock.now += _SETTLE_S + 0.1
    await recheck.step(live)

    # -- half two: the SECOND cancel went out and landed ---------------------
    assert broker.cancel_calls == [result.broker_order_id] * 2, (
        "two attempts on one order: the walk's, which raised, and the recheck's"
    )
    assert broker.live_ids == [], "the order is dead at the broker"
    assert audit.events.count("order.cancel_attempt") == 2
    assert "order.cancel_failed" in audit.events
    assert "order.cancel_requested" in audit.events
    assert [k for k, _ in alerts] == ["residual_order_working"]
    assert "order.residual_unmanaged" in audit.events

    # The residual stays watched: a cancel REQUEST is not a cancel, and only a
    # broker sighting resolves one. The next poll is what retires it.
    assert len(recheck.outstanding()) == 1
    await recheck.step(await broker.get_live_orders())
    assert recheck.outstanding() == ()


async def test_the_same_chain_sends_NOTHING_in_would_cancel_mode():
    """The control for the test above, and what live did from 2026-09-15 to v0.4.5.

    Identical chain, one config flag flipped. The decision is reached and recorded;
    the order stays working. This is what the flag actually buys, asserted rather
    than described — and it is the rollback's behaviour, so it has to keep working.
    """
    broker = FlakyCancelBroker(cancel_fails=1, live_after_polls=0)
    loop, recheck, killswitch, audit, clock, alerts = build_chain(
        broker, cancel_enabled=False
    )

    result = await kill_a_working_walk(loop, killswitch, broker, audit)
    assert result.outcome is WalkOutcome.ABORTED

    live = await broker.get_live_orders()
    clock.now += _SETTLE_S + 0.1
    await recheck.step(live)

    assert broker.cancel_calls == [result.broker_order_id], "only the walk's own"
    assert broker.live_ids == [result.broker_order_id], "still working, by design"
    assert "tier2:order.residual_would_cancel" in audit.events
    assert audit.payload("order.residual_would_cancel")["attempts"] == 1
    # The page fires either way: observer mode is not silent mode.
    assert [k for k, _ in alerts] == ["residual_order_working"]


async def test_a_BLIND_POLL_WINDOW_does_not_retire_the_residual():
    """The correlation that makes this backstop's worst case its own trigger.

    The recheck rides the watchdog's `get_live_orders()` poll, so the input it needs
    is the same call that is failing when a cancel fails — a broker having a bad
    minute blinds the recheck for exactly as long as it blinds everything else. One
    failed poll simply means `step()` is not called that tick (the watchdog swallows
    it and pages `watchdog.residual_recheck_failed`).

    What must survive that window is the REGISTRY, and the rate bound must be on
    TIME rather than on ticks — which is the decision recorded as *"the bound is on
    RATE, not COUNT"*: a budget of three attempts would have been spent inside the
    2026-08-28 outage and never retried after recovery. Here the poll is dark across
    the whole settle allowance and well past it, and the first answer that arrives
    still gets a cancel.
    """
    broker = FlakyCancelBroker(cancel_fails=1, live_after_polls=0)
    loop, recheck, killswitch, audit, clock, _alerts = build_chain(
        broker, cancel_enabled=True
    )

    result = await kill_a_working_walk(loop, killswitch, broker, audit)
    assert broker.live_ids == [result.broker_order_id]

    # Ten minutes of no usable answer: the poll is failing, so nothing calls step().
    clock.now += 600.0
    assert broker.cancel_calls == [result.broker_order_id], "nothing was sent blind"
    assert len(recheck.outstanding()) == 1, "and nothing was retired"

    # The broker recovers. The first answer is enough.
    await recheck.step(await broker.get_live_orders())
    assert broker.cancel_calls == [result.broker_order_id] * 2
    assert broker.live_ids == []
