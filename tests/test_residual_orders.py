"""The residual-order recheck (P5): a cancel that was requested is not one that landed.

2026-08-28: order 1424563's cancel came back a 502 at 10:10, the walk finished, and
nothing ever asked about that order again — it was still working at 12:15 when a
restart's reconciliation halted on it. `_request_cancel` is best-effort, so the kill
switch's stated guarantee ("including an entry order already working at the
exchange") silently did not happen.

The tests are arranged the way the decision record argues the design:

  * registration at `_finish`, through the REAL `PriceWalkingLoop`;
  * the state table, exhaustively, on the pure reader;
  * direction — minted, cross-checked, and never off the broker's echoed legs;
  * the rate bound, including the ten-minute outage a COUNT bound would fail;
  * isolation, wiring, and two structural assertions that outlive the prose.

→ docs/decisions/2026-08-29-mid-session-cancel-recheck.md
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from argus.config import ConfigError, Config
from argus.execution.models import (
    LegAction,
    LiveOrders,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
    UnmappableOrder,
)
from argus.execution.residual_orders import (
    ResidualOrderRecheck,
    ResidualSighting,
    cancel_licence,
    read_sighting,
)
from tests.support import config_dict, make_config
from tests.test_execution_loop import (
    OURS as WALK_ID,
    Broker,
    FakeAudit,
    FakeTime,
    build_loop,
    policy,
    spread,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "argus" / "execution"

OPEN_ID = "argus-open-20260828-put-5000-4995"
CLOSE_ID = "argus-close-20260828-put-5000-4995"
#: What `spread()` (the loop suite's own order) carries, so the registration tests
#: assert against the identifier the REAL walk minted rather than a restated one.
WALK_CLOSE_ID = WALK_ID.replace("argus-open-", "argus-close-")


# -- harness ------------------------------------------------------------------


class CancelSpy:
    """The one outbound call this module can make."""

    def __init__(self, *, fails: int = 0, exc: Exception | None = None) -> None:
        self.calls: list[int] = []
        #: How many of the next calls raise. `None`-free by design: a test that
        #: wants a broker down for ten minutes sets a big number and lowers it.
        self.fails = fails
        self._exc = exc or ConnectionError("502 Bad Gateway")

    async def __call__(self, broker_order_id: int) -> None:
        self.calls.append(broker_order_id)
        if self.fails > 0:
            self.fails -= 1
            raise self._exc


def make_recheck(
    *,
    cancel_enabled: bool = True,
    clock: FakeTime | None = None,
    cancel: CancelSpy | None = None,
    **watchdog_overrides,
):
    """A recheck over a fake clock, a fake audit log and a cancel spy."""
    audit = FakeAudit()
    clock = clock or FakeTime()
    cancel = cancel or CancelSpy()
    alerts: list[tuple[str, dict]] = []
    watchdog = {"unmanaged_order_cancel": cancel_enabled, **watchdog_overrides}
    recheck = ResidualOrderRecheck(
        audit=audit,
        config=make_config(environment="sandbox", watchdog=watchdog),
        cancel=cancel,
        alert=lambda kind, detail: alerts.append((kind, detail)),
        clock=clock.clock,
    )
    return recheck, audit, clock, cancel, alerts


def order(
    *,
    broker_order_id: int = 1000,
    identifier: str = OPEN_ID,
    direction: OrderDirection = OrderDirection.OPEN,
    status: OrderStatus | None = OrderStatus.LIVE,
    remaining: int | None = 2,
) -> Order:
    short, long_ = (
        (LegAction.SELL_TO_OPEN, LegAction.BUY_TO_OPEN)
        if direction is OrderDirection.OPEN
        else (LegAction.BUY_TO_CLOSE, LegAction.SELL_TO_CLOSE)
    )
    return Order(
        direction=direction,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", short, 2, remaining_quantity=remaining),
            OrderLeg("SPXW  P4995", long_, 2, remaining_quantity=remaining),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=identifier,
        broker_order_id=broker_order_id,
        status=status,
    )


def register(
    recheck: ResidualOrderRecheck,
    *,
    broker_order_id: int = 1000,
    identifier: str = OPEN_ID,
    direction: OrderDirection = OrderDirection.OPEN,
    walk_outcome: str = "walked_away",
    cancel_requested: bool = False,
) -> None:
    recheck.register(
        broker_order_id=broker_order_id,
        external_identifier=identifier,
        minted_direction=direction,
        candidate_id="20260828-put-5000-4995",
        walk_outcome=walk_outcome,
        cancel_requested=cancel_requested,
    )


async def step_past_allowance(recheck, clock, live, *, settle_s: float = 2.0):
    """Advance past `exit_cancel_settle_s` and step once."""
    clock.now += settle_s + 0.1
    await recheck.step(live)


# -- registration: at `_finish`, on the STATE, through the real walk -----------


async def test_a_walked_away_order_is_registered():
    """The ordinary case: the ladder is spent, a cancel is requested, and the order
    is at `Cancel Requested` at best. That is a live order nobody is walking."""
    recheck, *_ = make_recheck()
    broker = Broker()
    loop, _audit, _clock = build_loop(broker, residuals=recheck, policy=policy())
    broker.on_poll = [lambda b: None] * 20

    result = await loop.run(spread(), guard_first_submission=True)

    assert str(result.outcome) == "walked_away"
    residuals = recheck.outstanding()
    assert len(residuals) == 1
    assert residuals[0].broker_order_id == result.broker_order_id
    assert residuals[0].external_identifier == WALK_ID
    assert residuals[0].minted_direction is OrderDirection.OPEN


async def test_a_filled_walk_registers_nothing():
    """Terminal is finished. The position is the fill consumer's business."""
    recheck, *_ = make_recheck()
    broker = Broker()
    loop, _audit, _clock = build_loop(broker, residuals=recheck)
    broker.on_poll = [lambda b: b.fill(next(iter(b.live_ids)))]

    result = await loop.run(spread(), guard_first_submission=True)

    assert str(result.outcome) == "filled"
    assert recheck.outstanding() == ()


async def test_a_never_worked_order_whose_cancel_FAILED_is_registered():
    """2026-08-28 exactly: the DELETE raises, the walk finishes anyway (correctly,
    by design), and nothing else in the process would ever ask again."""
    recheck, *_ = make_recheck()
    broker = Broker(live_after_polls=None)  # never reaches the market
    loop, _audit, _clock = build_loop(
        broker, residuals=recheck, policy=policy(working_timeout_s=1.0)
    )

    async def cancel_fails(broker_order_id: int) -> None:
        raise ConnectionError("502 Bad Gateway")

    broker.cancel = cancel_fails  # type: ignore[method-assign]
    result = await loop.run(spread(), guard_first_submission=True)

    assert str(result.outcome) == "never_worked"
    assert result.cancel_requested is False
    assert [r.broker_order_id for r in recheck.outstanding()] == [result.broker_order_id]


async def test_an_order_that_was_never_VISIBLE_is_registered_though_no_cancel_was_asked():
    """`UNRESOLVED: order_not_visible_at_broker` requests no cancel at all — the one
    of the three ways an order is left working that a hook on the cancel's `except`
    branch could never have seen."""
    recheck, *_ = make_recheck()

    class Invisible(Broker):
        async def get_live_orders(self):
            return LiveOrders()

    broker = Invisible(live_after_polls=None)
    loop, _audit, _clock = build_loop(
        broker, residuals=recheck, guard=False, policy=policy(working_timeout_s=1.0)
    )
    result = await loop.run(spread(), guard_first_submission=False)

    assert result.reason == "order_not_visible_at_broker"
    assert result.cancel_requested is False
    assert len(recheck.outstanding()) == 1


async def test_the_order_a_KILL_abandons_is_registered_when_its_cancel_fails():
    """THE case this exists for. The kill switch promises to reach "an entry order
    already working at the exchange", and it reaches it through `_request_cancel`,
    which swallows every exception. Before this the promise silently went unmet."""
    recheck, *_ = make_recheck()
    broker = Broker()
    checks: list[int] = []

    async def cancel_fails(broker_order_id: int) -> None:
        raise ConnectionError("502 Bad Gateway")

    broker.cancel = cancel_fails  # type: ignore[method-assign]

    def halt_check() -> bool:
        checks.append(1)
        return len(checks) > 1  # engaged after the first cycle boundary

    loop, _audit, _clock = build_loop(broker, residuals=recheck, halt_check=halt_check)
    result = await loop.run(spread(), guard_first_submission=True)

    assert str(result.outcome) == "aborted"
    assert result.reason == "kill switch engaged"
    assert result.cancel_requested is False, "the cancel raised — this is 2026-08-28"
    assert [r.broker_order_id for r in recheck.outstanding()] == [result.broker_order_id]


async def test_a_blocked_walk_registers_nothing():
    """Nothing was sent, so there is no order to leave behind."""
    recheck, *_ = make_recheck()
    broker = Broker()
    conflicting = spread()
    await broker.submit(conflicting, dry_run=False)
    loop, _audit, _clock = build_loop(broker, residuals=recheck)

    result = await loop.run(spread(), guard_first_submission=True)

    assert str(result.outcome) == "blocked"
    assert recheck.outstanding() == ()


async def test_a_PARTIAL_fill_registers_its_still_working_remainder():
    """The outcome is FILLED and the last observation is WORKING, so it registers —
    and it should. The remainder's cancel is best-effort like every other, and a
    remainder that fills is a bigger spread than the one that was gated."""
    recheck, *_ = make_recheck()
    broker = Broker()
    loop, _audit, _clock = build_loop(broker, residuals=recheck)
    broker.on_poll = [lambda b: b.fill(next(iter(b.live_ids)), 1)]

    result = await loop.run(spread(), guard_first_submission=True)

    assert result.reason == "partial fill; remainder cancelled"
    assert len(recheck.outstanding()) == 1


async def test_a_DRIFTED_walks_abandoned_order_is_registered():
    """`_check_drift` is the fourth of the six `_request_cancel` sites, and it ends
    the same way: an order left at a price the loop has stopped standing behind."""
    recheck, *_ = make_recheck()
    broker = Broker()
    drifts = iter([None, "underlying moved 3.40 points"])
    loop, _audit, _clock = build_loop(
        broker, residuals=recheck, drift_check=lambda: next(drifts, "moved")
    )
    result = await loop.run(spread(), guard_first_submission=True)

    assert str(result.outcome) == "drifted"
    assert [r.broker_order_id for r in recheck.outstanding()] == [result.broker_order_id]


async def test_the_recovery_paths_duplicate_branch_registers_the_last_order():
    """Two working orders under one identifier: the walk cancels both, best-effort,
    and returns UNRESOLVED. Registering the one it ended holding is the fail-safe
    direction — a residual that is in fact dead resolves on the very first poll."""
    recheck, *_ = make_recheck()
    broker = Broker()
    await broker.submit(spread(), dry_run=False)
    await broker.submit(spread(), dry_run=False)
    broker.fail_next(ConnectionError("socket"))

    loop, _audit, _clock = build_loop(broker, residuals=recheck, guard=False)
    result = await loop.run(spread(), guard_first_submission=False)

    assert str(result.outcome) == "unresolved"
    assert len(recheck.outstanding()) == 1


async def test_registration_takes_the_direction_ARGUS_MINTED():
    """From `state.sent`, never `state.last` — the broker relabels leg actions."""
    recheck, *_ = make_recheck()
    broker = Broker()
    loop, _audit, _clock = build_loop(broker, residuals=recheck, guard=False)
    broker.on_poll = [lambda b: None] * 20

    await loop.run(
        replace(
            spread(direction=OrderDirection.CLOSE, limit="-1.70"),
            external_identifier=WALK_CLOSE_ID,
        ),
        guard_first_submission=False,
    )

    assert recheck.outstanding()[0].minted_direction is OrderDirection.CLOSE


async def test_registration_is_first_writer_wins():
    recheck, audit, *_ = make_recheck()
    register(recheck, walk_outcome="walked_away")
    register(recheck, walk_outcome="aborted")
    assert [r.walk_outcome for r in recheck.outstanding()] == ["walked_away"]


async def test_a_walk_with_no_recheck_wired_behaves_exactly_as_before():
    """None is the pre-P5 behaviour and every existing construction site keeps it."""
    broker = Broker()
    loop, _audit, _clock = build_loop(broker, policy=policy())
    broker.on_poll = [lambda b: None] * 20
    result = await loop.run(spread(), guard_first_submission=True)
    assert str(result.outcome) == "walked_away"


# -- the state table, exhaustively --------------------------------------------


def _read(live: LiveOrders, broker_order_id: int = 1000, identifier: str = OPEN_ID):
    return read_sighting(
        live, broker_order_id=broker_order_id, external_identifier=identifier
    )


def test_a_terminal_order_reads_DEAD():
    live = LiveOrders(orders=(order(status=OrderStatus.CANCELLED),))
    assert _read(live)[0] is ResidualSighting.DEAD


def test_a_filled_order_reads_FILLED():
    live = LiveOrders(orders=(order(status=OrderStatus.FILLED, remaining=0),))
    assert _read(live)[0] is ResidualSighting.FILLED


def test_a_partially_filled_terminal_order_reads_FILLED():
    """`classify_order`'s rule: a cancelled order that filled 1 of 2 left a real
    position. Resolving it as DEAD would file it next to a clean cancel."""
    live = LiveOrders(orders=(order(status=OrderStatus.CANCELLED, remaining=1),))
    assert _read(live)[0] is ResidualSighting.FILLED


def test_a_working_order_reads_WORKING():
    assert _read(LiveOrders(orders=(order(),)))[0] is ResidualSighting.WORKING


def test_a_cancel_requested_order_still_reads_WORKING():
    """The whole point: the broker accepted the REQUEST. It is not gone."""
    live = LiveOrders(orders=(order(status=OrderStatus.CANCEL_REQUESTED),))
    assert _read(live)[0] is ResidualSighting.WORKING


def test_an_absent_order_reads_ABSENT_and_never_DEAD():
    """The mutation this exists for: absence treated as resolution. An answer that
    does not name this order is a read gap — `any()` over an empty list is `False`,
    and an order nobody read once passed for one confirmed cancelled."""
    assert _read(LiveOrders())[0] is ResidualSighting.ABSENT
    assert _read(LiveOrders(orders=(order(broker_order_id=7777),)))[0] is (
        ResidualSighting.ABSENT
    )


def test_an_unmappable_order_under_this_id_reads_UNREADABLE():
    live = LiveOrders(
        unmappable=(
            UnmappableOrder(
                broker_order_id=1000,
                external_identifier="",
                underlying="SPXW",
                raw_status="Escrowed",
                reason="status outside the mirrored set",
            ),
        )
    )
    assert _read(live)[0] is ResidualSighting.UNREADABLE


def test_an_unmappable_order_under_this_IDENTIFIER_reads_UNREADABLE():
    live = LiveOrders(
        orders=(order(),),
        unmappable=(
            UnmappableOrder(
                broker_order_id=1001,
                external_identifier=OPEN_ID,
                underlying="SPXW",
                raw_status="Escrowed",
                reason="x",
            ),
        ),
    )
    assert _read(live)[0] is ResidualSighting.UNREADABLE


def test_two_orders_under_one_broker_id_read_AMBIGUOUS():
    """Fail closed rather than picking one, exactly as `_await_settled` does."""
    live = LiveOrders(orders=(order(), order()))
    assert _read(live)[0] is ResidualSighting.AMBIGUOUS


def test_an_id_match_carrying_a_DIFFERENT_identifier_reads_AMBIGUOUS():
    """The identifier cross-check. Dropping it is one of the named mutations: an id
    that matched and a key that did not is "cannot say", and cannot-say never
    authorizes a cancel."""
    live = LiveOrders(orders=(order(identifier="argus-open-20260828-call-1-2"),))
    assert _read(live)[0] is ResidualSighting.AMBIGUOUS


def test_only_DEAD_and_FILLED_resolve():
    resolving = {s for s in ResidualSighting if s.resolves}
    assert resolving == {ResidualSighting.DEAD, ResidualSighting.FILLED}


def test_every_non_actionable_sighting_has_an_escalation_tag():
    """A new `ResidualSighting` nobody mapped must not silently share another
    condition's bound."""
    from argus.execution.residual_orders import _READ_GAP_TAGS

    gaps = {
        s
        for s in ResidualSighting
        if not s.resolves and s is not ResidualSighting.WORKING
    }
    assert set(_READ_GAP_TAGS) == gaps
    assert len(set(_READ_GAP_TAGS.values())) == len(gaps), "tags must be distinct"


# -- what the recheck DOES with each of those ---------------------------------


async def test_a_terminal_sighting_resolves_the_residual():
    recheck, audit, clock, cancel, _alerts = make_recheck()
    register(recheck)
    await step_past_allowance(
        recheck, clock, LiveOrders(orders=(order(status=OrderStatus.CANCELLED),))
    )
    assert recheck.outstanding() == ()
    assert "tier2:order.residual_resolved" in audit.events
    assert cancel.calls == []


async def test_a_FILLED_sighting_resolves_and_records_at_TIER_1():
    """An order the walk walked away from filling is a decision the market
    reversed. Tier 2 would put it below the `walk.finished` that gave up on it."""
    recheck, audit, clock, cancel, alerts = make_recheck()
    register(recheck)
    await step_past_allowance(
        recheck,
        clock,
        LiveOrders(orders=(order(status=OrderStatus.FILLED, remaining=0),)),
    )
    assert recheck.outstanding() == ()
    assert "order.residual_filled" in audit.events
    assert [k for k, _ in alerts] == ["residual_order_filled"]
    assert cancel.calls == []


async def test_a_working_order_INSIDE_the_settle_allowance_waits():
    """`cancel()` leaves an order at `Cancel Requested`, and the watchdog's refresh
    fires early on any fill — so the first sighting can be under a second old."""
    recheck, _audit, clock, cancel, alerts = make_recheck()
    register(recheck)
    clock.now += 1.0  # exit_cancel_settle_s is 2.0 in the test config
    await recheck.step(LiveOrders(orders=(order(),)))
    assert cancel.calls == []
    assert alerts == []
    assert len(recheck.outstanding()) == 1


async def test_a_working_order_PAST_the_allowance_is_cancelled_once():
    recheck, audit, clock, cancel, alerts = make_recheck()
    register(recheck)
    await step_past_allowance(recheck, clock, LiveOrders(orders=(order(),)))
    assert cancel.calls == [1000]
    assert [k for k, _ in alerts] == ["residual_order_working"]
    assert "order.residual_unmanaged" in audit.events
    # Still watched: a cancel REQUEST is not a cancel. Only a broker sighting resolves.
    assert len(recheck.outstanding()) == 1


async def test_an_ABSENT_order_is_neither_resolved_nor_cancelled():
    """Both halves of "absence is not a verdict", in one test."""
    recheck, audit, clock, cancel, alerts = make_recheck()
    register(recheck)
    await step_past_allowance(recheck, clock, LiveOrders())
    assert cancel.calls == []
    assert len(recheck.outstanding()) == 1
    assert [k for k, _ in alerts] == ["residual_order_unread"]
    assert audit.payload("order.residual_unmanaged")["condition"] == "not_visible"


async def test_an_UNREADABLE_order_is_neither_resolved_nor_cancelled():
    recheck, audit, clock, cancel, alerts = make_recheck()
    register(recheck)
    live = LiveOrders(
        unmappable=(
            UnmappableOrder(
                broker_order_id=1000,
                external_identifier=OPEN_ID,
                underlying="SPXW",
                raw_status="Escrowed",
                reason="x",
            ),
        )
    )
    await step_past_allowance(recheck, clock, live)
    assert cancel.calls == []
    assert len(recheck.outstanding()) == 1
    assert audit.payload("order.residual_unmanaged")["condition"] == "unreadable"


async def test_an_AMBIGUOUS_answer_is_neither_resolved_nor_cancelled():
    recheck, audit, clock, cancel, _alerts = make_recheck()
    register(recheck)
    await step_past_allowance(recheck, clock, LiveOrders(orders=(order(), order())))
    assert cancel.calls == []
    assert len(recheck.outstanding()) == 1
    assert audit.payload("order.residual_unmanaged")["condition"] == "ambiguous"


async def test_the_page_distinguishes_still_working_from_not_visible():
    """The operator's next move differs: chase a live order, or chase a read."""
    recheck, _audit, clock, _cancel, alerts = make_recheck()
    register(recheck)
    await step_past_allowance(recheck, clock, LiveOrders(orders=(order(),)))
    register(recheck, broker_order_id=2000, identifier="argus-open-20260828-call-1-2")
    clock.now += 5.0
    await recheck.step(LiveOrders(orders=(order(),)))
    kinds = {k for k, _ in alerts}
    assert kinds == {"residual_order_working", "residual_order_unread"}


# -- direction: minted, cross-checked, never the broker's -----------------------


def test_an_opening_residual_may_be_cancelled():
    may, tag, refusal = cancel_licence(
        minted_direction=OrderDirection.OPEN, external_identifier=OPEN_ID
    )
    assert may is True and refusal is None and tag == "still_working"


def test_a_CLOSING_residual_may_never_be_cancelled():
    may, tag, refusal = cancel_licence(
        minted_direction=OrderDirection.CLOSE, external_identifier=CLOSE_ID
    )
    assert may is False
    assert tag == "closing_still_working"
    assert "strand an open position" in refusal


def test_a_direction_that_disagrees_with_the_identifier_may_not_be_cancelled():
    """Unreachable by construction — both derive from one `build_order` call —
    which is exactly what makes it a tripwire for a change that breaks that."""
    may, tag, refusal = cancel_licence(
        minted_direction=OrderDirection.OPEN, external_identifier=CLOSE_ID
    )
    assert may is False
    assert tag == "direction_disagreement"
    assert "disagrees with the identifier" in refusal


def test_an_unparseable_identifier_may_not_be_cancelled():
    may, _tag, refusal = cancel_licence(
        minted_direction=OrderDirection.OPEN, external_identifier="somebody-elses-order"
    )
    assert may is False and "unparseable" in refusal


async def test_a_CLOSING_residual_is_never_cancelled_however_long_it_works():
    """Asserted on the CALL, not just on the licence. Cancelling a close strands an
    open position — `HaltCheckFn`'s asymmetry, inherited verbatim."""
    recheck, _audit, clock, cancel, alerts = make_recheck()
    register(recheck, identifier=CLOSE_ID, direction=OrderDirection.CLOSE)
    closing = order(identifier=CLOSE_ID, direction=OrderDirection.CLOSE)
    for _ in range(50):
        clock.now += 60.0
        await recheck.step(LiveOrders(orders=(closing,)))
    assert cancel.calls == []
    assert len(recheck.outstanding()) == 1
    assert alerts, "a stuck closing order must still be paged"
    assert {d["condition"] for _k, d in alerts} == {"closing_still_working"}


async def test_the_RELABELLED_closing_order_is_still_treated_as_a_CLOSE():
    """The real case, not a stub. On a chained book the account can be net flat on
    the strike an exit is selling to close, so tastytrade echoes `Sell to Open` and
    `infer_direction` calls the order OPEN. The recheck must not.

    → docs/decisions/2026-08-25-relabelled-closing-legs-open.md
    """
    recheck, _audit, clock, cancel, alerts = make_recheck()
    register(recheck, identifier=CLOSE_ID, direction=OrderDirection.CLOSE)
    # What the broker actually sends back for that close: OPENING leg actions.
    relabelled = Order(
        direction=OrderDirection.OPEN,  # what `infer_direction` produced
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", LegAction.SELL_TO_OPEN, 2, remaining_quantity=2),
            OrderLeg("SPXW  P4995", LegAction.BUY_TO_OPEN, 2, remaining_quantity=2),
        ),
        limit_price=Decimal("-1.70"),
        external_identifier=CLOSE_ID,
        broker_order_id=1000,
        status=OrderStatus.LIVE,
    )
    assert relabelled.direction is OrderDirection.OPEN, "the premise of this test"

    await step_past_allowance(recheck, clock, LiveOrders(orders=(relabelled,)))

    assert cancel.calls == [], "the broker's echoed direction must not license a cancel"
    assert {d["condition"] for _k, d in alerts} == {"closing_still_working"}


# -- rate, not count ------------------------------------------------------------


async def test_attempts_follow_the_geometric_backoff():
    # A loose floor, so the gaps under test are the TABLE's (15, 30, 60) rather
    # than the cap — which is what the next test is for.
    recheck, _audit, clock, cancel, _alerts = make_recheck(unmanaged_order_max_s=600.0)
    register(recheck)
    live = LiveOrders(orders=(order(),))
    clock.now += 3.0
    await recheck.step(live)
    assert cancel.calls == [1000]

    # A poll one second later must not re-cancel: the floor is 15s.
    clock.now += 1.0
    await recheck.step(live)
    assert len(cancel.calls) == 1

    clock.now += 15.0
    await recheck.step(live)
    assert len(cancel.calls) == 2

    clock.now += 20.0  # the second gap is 30s, not 15
    await recheck.step(live)
    assert len(cancel.calls) == 2
    clock.now += 11.0
    await recheck.step(live)
    assert len(cancel.calls) == 3


async def test_the_rate_floors_at_unmanaged_order_max_s():
    """Past the table, one attempt per bound — for as long as it is confirmed
    working. The bound is 30.0 in the test config."""
    recheck, _audit, clock, cancel, _alerts = make_recheck()
    register(recheck)
    live = LiveOrders(orders=(order(),))
    clock.now += 3.0
    # Every gap here is far past both the table and the bound, so each poll earns
    # exactly one attempt — the steady state of a four-hour stuck order.
    for _ in range(20):
        clock.now += 600.0
        await recheck.step(live)
    assert len(cancel.calls) == 20
    # And an attempt inside the bound earns none: the floor holds, never the tick.
    clock.now += 1.0
    await recheck.step(live)
    assert len(cancel.calls) == 20


async def test_a_broker_down_for_ten_minutes_is_STILL_tried_after_it_recovers():
    """The test a hard COUNT bound would fail. Cert 502-ed from 10:00 to 10:13 and
    the order sat working until 12:15 — a budget of three attempts would have been
    spent inside the outage, on the one stretch where nothing could have worked."""
    cancel = CancelSpy(fails=100)
    recheck, _audit, clock, cancel, _alerts = make_recheck(cancel=cancel)
    register(recheck)
    live = LiveOrders(orders=(order(),))
    clock.now += 3.0
    for _ in range(10):
        await recheck.step(live)
        clock.now += 60.0
    failed = len(cancel.calls)
    assert failed >= 3, "the outage must not be silent"

    cancel.fails = 0  # cert recovers
    clock.now += 600.0
    await recheck.step(live)
    assert len(cancel.calls) == failed + 1


async def test_a_failing_cancel_is_recorded_and_never_raises():
    recheck, audit, clock, cancel, _alerts = make_recheck(cancel=CancelSpy(fails=1))
    register(recheck)
    await step_past_allowance(recheck, clock, LiveOrders(orders=(order(),)))
    assert "tier2:order.residual_cancel_failed" in audit.events
    assert len(recheck.outstanding()) == 1


async def test_the_page_repeats_on_the_bound_and_not_on_the_tick():
    """One record per bound, never one per poll — `_Escalation.overdue`, which is
    the mechanism A2 built for exactly this."""
    recheck, audit, clock, _cancel, alerts = make_recheck()
    register(recheck)
    live = LiveOrders(orders=(order(),))
    clock.now += 3.0
    for _ in range(10):  # ten polls inside one 30s bound
        clock.now += 1.0
        await recheck.step(live)
    assert len(alerts) == 1, "the rising edge, and nothing else"

    clock.now += 30.0
    await recheck.step(live)
    assert len(alerts) == 2
    assert audit.events.count("order.residual_unmanaged") == 2


# -- scope (B1) ------------------------------------------------------------------


async def test_two_residuals_page_with_their_own_identifiers_at_the_TOP_level():
    """`SlackNotifier` scopes its 900s throttle on `(kind, scope)` and reads the
    scope off the payload's top level. Nested, the second order's page would arrive
    as a bare `(+1 suppressed)` up to 15 minutes late."""
    from argus.notify import alert_scope

    recheck, _audit, clock, _cancel, alerts = make_recheck(cancel_enabled=False)
    other = "argus-open-20260828-call-5100-5105"
    register(recheck, broker_order_id=1000, identifier=OPEN_ID)
    recheck.register(
        broker_order_id=2000,
        external_identifier=other,
        minted_direction=OrderDirection.OPEN,
        candidate_id="20260828-call-5100-5105",
        walk_outcome="aborted",
        cancel_requested=True,
    )
    live = LiveOrders(
        orders=(order(), order(broker_order_id=2000, identifier=other)),
    )
    await step_past_allowance(recheck, clock, live)

    assert len(alerts) == 2
    scopes = {alert_scope(detail) for _kind, detail in alerts}
    assert scopes == {"20260828-put-5000-4995", "20260828-call-5100-5105"}


# -- would_cancel: the same decision, no outbound call -------------------------


async def test_would_cancel_mode_decides_and_records_but_sends_nothing():
    recheck, audit, clock, cancel, alerts = make_recheck(cancel_enabled=False)
    register(recheck)
    await step_past_allowance(recheck, clock, LiveOrders(orders=(order(),)))
    assert cancel.calls == []
    assert "tier2:order.residual_would_cancel" in audit.events
    decision = audit.payload("order.residual_would_cancel")
    assert decision["broker_order_id"] == 1000
    assert decision["minted_direction"] == "open"
    assert decision["attempts"] == 1
    # And it still pages: the order is unmanaged whether or not a cancel is sent.
    assert [k for k, _ in alerts] == ["residual_order_working"]
    assert alerts[0][1]["cancel_enabled"] is False


async def test_would_cancel_mode_runs_the_same_schedule():
    """So the stage measures the rate bound too, not only the inputs."""
    recheck, audit, clock, cancel, _alerts = make_recheck(cancel_enabled=False)
    register(recheck)
    live = LiveOrders(orders=(order(),))
    clock.now += 3.0
    await recheck.step(live)
    clock.now += 1.0
    await recheck.step(live)
    assert audit.events.count("tier2:order.residual_would_cancel") == 1
    clock.now += 15.0
    await recheck.step(live)
    assert audit.events.count("tier2:order.residual_would_cancel") == 2


async def test_would_cancel_mode_still_refuses_a_closing_order():
    """The OPEN/CLOSE split is decision logic, so it is exercised in both modes."""
    recheck, audit, clock, _cancel, _alerts = make_recheck(cancel_enabled=False)
    register(recheck, identifier=CLOSE_ID, direction=OrderDirection.CLOSE)
    await step_past_allowance(
        recheck,
        clock,
        LiveOrders(orders=(order(identifier=CLOSE_ID, direction=OrderDirection.CLOSE),)),
    )
    assert "tier2:order.residual_would_cancel" not in audit.events


async def test_registration_records_at_tier_2_in_both_modes():
    """Every residual it registers exercises the direction sourcing and the
    identifier cross-check — the evidence the staged rollout is for."""
    for enabled in (True, False):
        recheck, audit, *_ = make_recheck(cancel_enabled=enabled)
        register(recheck)
        assert "tier2:order.residual_registered" in audit.events


# -- driven by the watchdog's poll, and unable to take it down ------------------


async def test_the_recheck_rides_the_watchdogs_EXISTING_poll():
    """One fetch, two readers: P4's working-order count and this. A separate task
    would duplicate the request at the same cadence for no gain."""
    from tests.test_watchdog import FakeClock, build_quotes, build_watchdog
    from argus.execution.store import OpenSpreadScan

    recheck, _audit, clock, cancel, _alerts = make_recheck()
    register(recheck)
    polls = []

    async def live_orders():
        polls.append(1)
        return LiveOrders(orders=(order(),))

    wd_clock = FakeClock()
    _market, _dir, quotes = build_quotes(wd_clock)
    wd, _a, _al, _h = build_watchdog(
        clock=wd_clock,
        quotes=quotes,
        scan=OpenSpreadScan(),
        live_orders=live_orders,
        residuals=recheck,
    )
    clock.now += 5.0  # past the settle allowance on the RECHECK's own clock
    await wd._refresh_if_due()

    assert polls == [1], "one fetch, not two"
    assert wd.working_open_spread_count == 1
    assert cancel.calls == [1000]


async def test_a_step_that_raises_never_stops_the_watchdog():
    """The second place in the module that swallows an exception, and for more
    reason than the first: a failing cancel is this module's expected input, and
    taking the watchdog down over one would trade a live position's virtual stop
    for an unfilled order's tidiness."""
    from tests.test_watchdog import (
        FakeClock,
        SHORT_STREAM,
        LONG_STREAM,
        build_quotes,
        build_watchdog,
        spread as held_spread,
    )
    from argus.execution.store import OpenSpreadScan

    class Exploding:
        async def step(self, live):
            raise RuntimeError("boom")

    wd_clock = FakeClock()
    market, _dir, quotes = build_quotes(wd_clock)
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    market.set("SPX", 5100.0, 5100.0)

    async def live_orders():
        return LiveOrders()

    wd, audit, _alerts, halts = build_watchdog(
        clock=wd_clock,
        quotes=quotes,
        scan=OpenSpreadScan(spreads=(held_spread(),)),
        live_orders=live_orders,
        residuals=Exploding(),
    )
    await wd.check_once()

    assert halts == [], "a residual-recheck failure must not halt the watchdog"
    assert "tier2:watchdog.residual_recheck_failed" in audit.events
    # And the position was still evaluated on that same tick.
    assert "tier2:watchdog.check" in audit.events
    assert wd.last_failure is None


async def test_a_failed_broker_read_steps_nothing_rather_than_concluding_absence():
    """If the fetch fails there is no answer, and no answer is not an ABSENT
    sighting — it is nothing at all."""
    from tests.test_watchdog import FakeClock, build_quotes, build_watchdog
    from argus.execution.store import OpenSpreadScan

    recheck, _audit, clock, cancel, alerts = make_recheck()
    register(recheck)

    async def live_orders():
        raise RuntimeError("cert 502")

    wd_clock = FakeClock()
    _market, _dir, quotes = build_quotes(wd_clock)
    wd, _a, _al, _h = build_watchdog(
        clock=wd_clock,
        quotes=quotes,
        scan=OpenSpreadScan(),
        live_orders=live_orders,
        residuals=recheck,
    )
    clock.now += 60.0
    await wd._refresh_if_due()

    assert cancel.calls == []
    assert alerts == [], "an unread broker is not a sighting of any kind"
    assert len(recheck.outstanding()) == 1


# -- wiring --------------------------------------------------------------------


async def test_a_dry_run_registers_nothing_and_needs_no_branch_to_say_so():
    """A dry run mints no broker order id and leaves nothing working, so there is
    never a residual — by construction, not by a check."""
    from tests.test_entry import build_entry

    recheck, *_ = make_recheck()
    harness = build_entry(dry_run=True, residuals=recheck)
    await harness.submit()
    assert harness.broker.dry_runs, "the dry run must actually have happened"
    assert recheck.outstanding() == ()


def test_the_session_builds_ONE_recheck_and_shares_it_with_all_three():
    """The entry walk and the exit walk register into it and the watchdog steps it.
    A second instance would be a registry nobody polls."""
    from tests.test_session_trading_side import _build

    side, _audit, _stop = _build()
    recheck = side.entry._residuals
    assert recheck is not None
    assert side.watchdog._residuals is recheck
    assert side.watchdog._executor._residuals is recheck


def test_the_recheck_cancels_through_the_SAME_audited_throttled_stack():
    """So `order.cancel_attempt` / `order.cancel_failed` / `order.cancel_requested`
    are written by the existing layer at the existing tier, and this module adds no
    cancel record types of its own."""
    from tests.test_session_trading_side import _build

    side, _audit, _stop = _build()
    assert side.entry._residuals._cancel.__self__ is side.stack.execution
    assert side.entry._residuals._cancel.__func__.__name__ == "cancel"


def test_a_monitor_only_session_still_runs_the_recheck():
    """Structural, because running the real body needs a broker: a P3 `ENTRIES`
    halt builds the trading side with `submit=None`, and `_build_trading_side` is
    what wires the recheck — so it comes up with the watchdog either way."""
    import argus.session as session_mod

    source = inspect.getsource(session_mod._build_trading_side)
    tree = ast.parse(source.lstrip())
    built = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ResidualOrderRecheck"
    ]
    assert len(built) == 1, "one recheck, built unconditionally with the trading side"
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and built[0] in list(ast.walk(node))
    ], "the recheck must not sit behind a condition inside the builder"


# -- structure: two assertions that outlive the prose ---------------------------


def test_the_decision_path_reads_no_direction_off_a_broker_order():
    """The finding most likely to be got wrong from a summary, pinned structurally.

    `Order.direction` off `get_live_orders()` is `infer_direction`'s reading of the
    leg actions the BROKER echoed, and tastytrade labels each leg relative to the
    account's position rather than to Argus's intent. The field this module reads is
    called `minted_direction` precisely so that any `.direction` access in here is
    visibly wrong — including the "consistency" edit that would unify this with
    `working_orders.py`, where the same field is safe because the same mistake only
    inflates a count that blocks an entry.
    """
    tree = ast.parse((_SRC / "residual_orders.py").read_text())
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "direction"
    ]
    assert not reads, (
        "residual_orders.py reads a `.direction` attribute — direction must come "
        "from `state.sent` at registration and `parse_external_identifier` as the "
        "cross-check, never from an order that came back off the wire"
    )
    # And the cross-check is actually performed.
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "parse_external_identifier" in called


def test_the_outbound_cancel_sits_behind_ONE_flag_in_ONE_place():
    """`shadow.would_submit`'s shape, asserted the way that boundary is asserted.

    A staged rollout is only a rollout if the two stages are the same code. Scattered
    `if self._cancel_enabled` branches would let `would_cancel` mode drift from what
    it is supposed to be de-risking.
    """
    tree = ast.parse((_SRC / "residual_orders.py").read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_cancel"
    ]
    assert len(calls) == 1, "there must be exactly one outbound cancel call"

    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(n, ast.Attribute) and n.attr == "_cancel_enabled"
            for n in ast.walk(node.test)
        )
        and calls[0] in list(ast.walk(node))
    ]
    assert guarded, "the cancel must sit inside the `_cancel_enabled` branch"
    assert len(guarded) == 1, "one flag, one branch — not a scattered condition"


def test_only_the_watchdog_steps_the_recheck_so_the_close_stops_it():
    """It owns no task and no cadence of its own. It lives and dies with the
    watchdog's refresh, deliberately and with the same blast radius, rather than
    outliving the session that can act on what it finds.

    **The stated reason changed on 2026-09-01 and the assertion did not.** It used to
    read "`_end_day` stops the watchdog", which was true and is now false: the entry
    path's stand-down ends entries alone. What stops the watchdog — and therefore
    this — is the session close, a SIGTERM, or an `ALL`-graded halt. The invariant is
    unchanged and in fact stronger; only what reaches it is narrower.
    """
    callers = []
    for path in _SRC.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "step"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_residuals"
            ):
                callers.append(path.name)
    assert callers == ["watchdog.py"], (
        "the recheck must be stepped only from the watchdog's refresh — a task of "
        f"its own would outlive the session that can act on it; found {callers}"
    )


def test_the_registry_is_in_process_and_reads_nothing_back():
    """No rehydration, ever. A registry rebuilt from the audit log would let a
    restart cancel the PREVIOUS process's orphan — an automated action on an order
    whose provenance is by construction unclear, which is reconciliation's question
    and P3's boundary.
    """
    source = (_SRC / "residual_orders.py").read_text()
    tree = ast.parse(source)
    reads = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for banned in ("read", "rehydrate", "query", "fetch"):
        assert banned not in reads, (
            f"residual_orders.py calls `.{banned}()` — the registry is in-process "
            "and must never recover a previous process's orders"
        )


def test_the_recheck_is_not_gated_on_the_kill_switch():
    """Cancelling an abandoned OPENING order while halted is precisely what the
    operator asked for. A recheck disabled by a halt would be disabled exactly when
    it matters most."""
    source = (_SRC / "residual_orders.py").read_text()
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "killswitch" not in names and "engaged" not in attrs
    assert "dry_run_submissions" not in attrs


# -- config ---------------------------------------------------------------------


def test_unmanaged_order_max_s_is_required():
    raw = config_dict()
    del raw["watchdog"]["unmanaged_order_max_s"]
    with pytest.raises(ConfigError, match="unmanaged_order_max_s"):
        Config.from_dict(raw)


def test_unmanaged_order_max_s_must_exceed_the_refresh_interval():
    """A bound at or below the poll cadence pages and re-cancels on every tick."""
    raw = config_dict()
    raw["watchdog"]["unmanaged_order_max_s"] = raw["watchdog"]["refresh_interval_s"]
    with pytest.raises(ConfigError, match="must be > watchdog.refresh_interval_s"):
        Config.from_dict(raw)


def test_unmanaged_order_cancel_defaults_to_would_cancel():
    """Absent means send nothing — the same side `execution.dry_run_submissions`
    defaults to, so an image carrying P5 cannot start cancelling orders against a
    config written before it."""
    raw = config_dict()
    del raw["watchdog"]["unmanaged_order_cancel"]
    assert Config.from_dict(raw).watchdog.unmanaged_order_cancel is False


def test_unmanaged_order_cancel_must_be_a_real_bool():
    raw = config_dict()
    raw["watchdog"]["unmanaged_order_cancel"] = "true"
    with pytest.raises(ConfigError, match="must be a boolean"):
        Config.from_dict(raw)


def test_the_shipped_configs_carry_the_key():
    # The bound is the same everywhere; the MODE is not, and that split is the
    # rollout. Cert was PROMOTED in v0.3.21 (F2) after `would_cancel` ran from
    # 2026-08-30 and validated the inputs on real cert residuals — registration at
    # `_finish`, the recheck on P4's poll, `minted_direction` read correctly on a
    # residual seen FILLED (2026-09-01) — without ever exercising the outbound call,
    # which the design predicted and said not to wait for.
    #
    # dev stays `would_cancel`: it registers no residuals at all (it routes
    # nothing), so the flag is moot and stays on the side that sends nothing.
    #
    # live was PROMOTED in v0.4.5 (2026-09-19). It had stayed `would_cancel` at the
    # cutover so the first real re-cancel would not land on the day of the first
    # real order, gated on a cert track record that stopped being possible once
    # cert lost its entry path (~2026-09-02). The gate is now live's own
    # `order.residual_*` record. → docs/decisions/2026-09-19-live-cancel-recheck-promotion.md
    # **A rail changing state still costs an explicit, reviewed edit here** —
    # demoting live again means editing this line, not deleting it.
    for path in ("argus.sandbox.toml", "argus.dev.toml", "argus.live.toml"):
        config = Config.load(path)
        assert config.watchdog.unmanaged_order_max_s == 900.0, path
    assert Config.load("argus.sandbox.toml").watchdog.unmanaged_order_cancel is True
    assert Config.load("argus.dev.toml").watchdog.unmanaged_order_cancel is False
    assert Config.load("argus.live.toml").watchdog.unmanaged_order_cancel is True


def test_the_settle_allowance_is_the_EXIT_paths_key():
    """Reused rather than duplicated: it is the same physical quantity on both
    paths — how long a requested cancel takes to go terminal."""
    config = make_config(environment="sandbox")
    recheck = ResidualOrderRecheck(
        audit=FakeAudit(), config=config, cancel=CancelSpy()
    )
    assert recheck._settle_s == config.watchdog.exit_cancel_settle_s
