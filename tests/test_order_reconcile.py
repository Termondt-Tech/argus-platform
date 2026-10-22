"""Restart-time reconciliation extended to ORDERS (spec §6).

Positions alone were never the whole of "has Argus lost track of reality": a
working order nobody knows about becomes a position the moment it fills. The
recovery contract is explicit — ask the broker what actually happened, hard-halt
on anything unexplained, never resume or replay a sent message — and this is the
asking half.

The verdict rides in the SAME `ReconciliationResult` the position check already
produced, so the refusal reaches the operator through the existing
`Application._reconcile` / `_halt_process` path rather than a second rail with its
own environment gate to get wrong.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from argus.account.fake import FakeBrokerAccount
from argus.account.models import Position
from argus.app import Application
from argus.config import Config
from argus.db import connect
from argus.execution.broker import TastytradeOrderExecution
from argus.execution.fake import FakeOrderExecution
from argus.execution.models import (
    LegAction,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
    UnmappableOrder,
)
from argus.execution.store import InMemoryOrderStore
from argus.reconcile import (
    MISSING_AT_BROKER,
    UNEXPECTED_AT_BROKER,
    UNEXPECTED_FILLED_ORDER,
    UNEXPECTED_WORKING_ORDER,
    UNMAPPABLE_BROKER_ORDER,
    Discrepancy,
    HaltGrade,
    InMemoryPositionStore,
    ReconciliationError,
    ReconciliationResult,
    Reconciler,
    halt_grade,
    mismatch_halts_process,
    reconcile_and_record,
    reconcile_orders,
    shared_account_hint,
)
from support import make_config

OURS = "argus-open-20260729-put-5000-4995"


def order(
    *,
    order_id: int | None = 1000,
    status: OrderStatus | None = OrderStatus.LIVE,
    remaining: tuple[int | None, int | None] = (1, 1),
    identifier: str = OURS,
    quantity: int = 1,
) -> Order:
    return Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", LegAction.SELL_TO_OPEN, quantity, remaining[0]),
            OrderLeg("SPXW  P4995", LegAction.BUY_TO_OPEN, quantity, remaining[1]),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=identifier,
        broker_order_id=order_id,
        status=status,
    )


def _unmappable(
    *, order_id: int | None = 4242, identifier: str = OURS
) -> UnmappableOrder:
    """What the broker reported and the mapper refused.

    The realistic trigger is enum drift: tastytrade ships a status string that
    `OrderStatus` does not have. `test_execution_models.py` covers the mapping
    itself; this is about what the rest of the system does with the result.
    """
    return UnmappableOrder(
        broker_order_id=order_id,
        external_identifier=identifier,
        underlying="SPXW",
        raw_status="Escrowed",
        reason="unrecognized broker order status 'Escrowed'",
    )


# -- the pure comparison ------------------------------------------------------


def test_a_known_order_is_accounted_for():
    assert reconcile_orders({1000}, [order()], unmappable=()) == ()


def test_an_unknown_working_order_is_a_discrepancy():
    (d,) = reconcile_orders(set(), [order()], unmappable=())
    assert d.kind == UNEXPECTED_WORKING_ORDER
    assert d.broker_order_id == 1000
    # Carried verbatim: an `argus-` prefix says Argus placed this and lost the
    # record (look for order.submit_attempt); anything else says somebody else is
    # trading the account.
    assert d.external_identifier == OURS


def test_an_unknown_filled_order_is_a_discrepancy():
    (d,) = reconcile_orders(
        set(), [order(status=OrderStatus.FILLED, remaining=(0, 0))], unmappable=()
    )
    assert d.kind == UNEXPECTED_FILLED_ORDER


def test_an_unknown_partially_filled_then_cancelled_order_is_a_discrepancy():
    (d,) = reconcile_orders(
        set(),
        [order(status=OrderStatus.CANCELLED, remaining=(2, 2), quantity=3)],
        unmappable=(),
    )
    assert d.kind == UNEXPECTED_FILLED_ORDER


@pytest.mark.parametrize(
    "status", [OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED]
)
def test_an_unknown_dead_order_is_not_a_discrepancy(status):
    # `get_live_orders()` reaches back 24h, so yesterday afternoon's rejected order
    # is in every morning's answer. It cannot fill and it holds nothing, so halting
    # the process over it would brick the day while managing no risk at all.
    assert reconcile_orders(set(), [order(status=status)], unmappable=()) == ()


def test_a_foreign_working_order_is_still_a_discrepancy():
    # Provenance does not soften it: an order Argus did not place, working in the
    # account it is trading, means state is not solely under its control. Same
    # posture as `unexpected_at_broker` on the position side.
    (d,) = reconcile_orders(set(), [order(identifier="placed-by-hand")], unmappable=())
    assert d.kind == UNEXPECTED_WORKING_ORDER


def test_an_order_with_no_broker_id_cannot_be_accounted_for():
    (d,) = reconcile_orders({1000}, [order(order_id=None)], unmappable=())
    assert d.broker_order_id is None


def test_an_unmappable_order_is_a_discrepancy():
    """The C2 defect. This is the case reconciliation exists for.

    An order whose status the mirrored enum cannot take was DROPPED by
    `get_live_orders()` before this function ever saw it, so a single unrecognized
    string made an unknown order invisible to the rail whose whole premise is
    halting on anything unexplained.
    """
    (d,) = reconcile_orders(set(), [], unmappable=[_unmappable()])

    assert d.kind == UNMAPPABLE_BROKER_ORDER
    assert d.broker_order_id == 4242
    assert d.disposition == "unmappable"   # not a classification — nobody derived one
    assert d.status == "Escrowed"          # the broker's RAW string: the fix instruction
    assert d.external_identifier == OURS


def test_an_unmappable_order_is_a_discrepancy_even_when_argus_placed_it():
    """A ledger row answers "did Argus place this?", never "can Argus read it?".

    The other two kinds are suppressed by `known_order_ids` and should be. This one
    must not be: an order of ours wearing a status we cannot parse is exactly as
    unexplained as a stranger's, and resuming over it is guessing.
    """
    (d,) = reconcile_orders({4242}, [], unmappable=[_unmappable()])
    assert d.kind == UNMAPPABLE_BROKER_ORDER


def test_an_unmappable_order_does_not_suppress_the_mappable_ones():
    """One bad row must neither hide the others nor be hidden by them."""
    kinds = {
        d.kind
        for d in reconcile_orders(set(), [order()], unmappable=[_unmappable()])
    }
    assert kinds == {UNEXPECTED_WORKING_ORDER, UNMAPPABLE_BROKER_ORDER}


def test_reconcile_orders_will_not_let_a_caller_forget_the_unreadable_ones():
    """`unmappable` is keyword-only with NO default, and that is load-bearing.

    The bug this parameter fixes was a caller reconciling against a set that
    silently excluded the orders it most needed to see. A default of `()` would let
    the next caller reconstruct it by not knowing the argument existed.
    """
    with pytest.raises(TypeError):
        reconcile_orders(set(), [order()])          # type: ignore[call-arg]


def test_there_is_no_missing_at_broker_for_orders():
    # Asymmetric with positions on purpose: an order Argus recorded that the
    # broker no longer lists cannot execute, and everything older than 24h would
    # otherwise read as missing forever.
    assert reconcile_orders({1, 2, 3}, [], unmappable=()) == ()


# -- through the Reconciler ---------------------------------------------------


async def test_reconciler_reports_orders_alongside_positions():
    execution = FakeOrderExecution()
    await execution.submit(order(order_id=None, status=None), dry_run=False)

    result = await Reconciler(
        FakeBrokerAccount([]),
        InMemoryPositionStore(),
        execution=execution,
        order_store=InMemoryOrderStore(),
    ).reconcile()

    assert result.ok is False
    assert result.discrepancies == ()                       # positions are clean
    assert len(result.order_discrepancies) == 1
    assert result.total_discrepancies == 1                  # counted in the refusal message


async def test_reconciler_is_clean_when_the_order_is_argus_own():
    execution, store = FakeOrderExecution(), InMemoryOrderStore()
    ack = await execution.submit(order(order_id=None, status=None), dry_run=False)
    await store.record_order(ack.order)

    result = await Reconciler(
        FakeBrokerAccount([]), InMemoryPositionStore(),
        execution=execution, order_store=store,
    ).reconcile()

    assert result.ok is True


async def test_reconciler_without_an_execution_port_is_position_only():
    # Shadow submits nothing, so it has no execution port and nothing of its own
    # to find at the broker. The check simply does not run.
    result = await Reconciler(FakeBrokerAccount([]), InMemoryPositionStore()).reconcile()
    assert result.ok is True
    assert result.order_discrepancies == ()


def test_half_wired_reconciler_is_a_loud_error():
    with pytest.raises(ValueError, match="both"):
        Reconciler(
            FakeBrokerAccount([]), InMemoryPositionStore(),
            execution=FakeOrderExecution(),
        )


async def test_position_and_order_discrepancies_both_land_in_one_payload():
    import json

    execution = FakeOrderExecution()
    await execution.submit(order(order_id=None, status=None), dry_run=False)
    broker = FakeBrokerAccount(
        [Position("SPXW260729P05000000", "SPX", "Equity Option", -1, 1.2, 0.9)]
    )

    result = await Reconciler(
        broker, InMemoryPositionStore(),
        execution=execution, order_store=InMemoryOrderStore(),
    ).reconcile()
    payload = result.as_payload()

    json.dumps(payload)   # goes into the audit log and the kill-switch trip detail
    assert len(payload["discrepancies"]) == 1
    assert len(payload["order_discrepancies"]) == 1
    assert result.total_discrepancies == 2


# -- the halt itself, through the existing path -------------------------------


def _config(tmp_path: Path, **overrides) -> Config:
    audit = {**overrides.pop("audit", {}), "db_path": str(tmp_path / "argus.db")}
    return make_config(audit=audit, **overrides)


async def test_an_unexplained_order_halts_sandbox_startup(tmp_path: Path):
    """Exactly like a position mismatch does — same verdict, same halt.

    Nothing about the halt is new here, and that is the point: the order check
    produces the same `ReconciliationResult`, so it inherits the environment gate,
    the alert, and the refusal without a second implementation of any of them.
    Since 2026-08-17 that refusal PARKS the process rather than exiting it, and
    this path inherits that too — again without its own implementation.
    """
    execution = FakeOrderExecution()
    await execution.submit(order(order_id=None, status=None), dry_run=False)
    cfg = _config(tmp_path, environment="sandbox")
    app = Application(
        cfg,
        reconciler=Reconciler(
            FakeBrokerAccount([]), InMemoryPositionStore(),
            execution=execution, order_store=InMemoryOrderStore(),
        ),
    )

    await app.startup()
    try:
        assert app._halted is not None
        assert app._halted.source == "reconciliation_mismatch"
    finally:
        await app.shutdown()

    conn = connect(cfg.audit.db_path)
    rows = conn.execute(
        "SELECT category, payload FROM audit_log ORDER BY seq"
    ).fetchall()
    conn.close()
    cats = [r["category"] for r in rows]
    assert "reconcile.result" in cats
    # The discrepancy detail is durable, so the operator can diagnose it from the
    # record — and now also live, from a /status the halt no longer takes down.
    verdict = next(r["payload"] for r in rows if r["category"] == "reconcile.result")
    assert "unexpected_working_order" in verdict


class _StubLeg:
    symbol = "SPXW  P5000"
    action = "Sell to Open"
    quantity = 1
    remaining_quantity = 1
    instrument_type = "Equity Option"
    fills: tuple = ()


class _StubPlaced:
    """A `PlacedOrder`-shaped object, duck-typed the way the SDK's really is."""

    def __init__(self, order_id: int, status: str) -> None:
        self.id = order_id
        self.status = status
        self.underlying_symbol = "SPXW"
        self.price = Decimal("0.85")
        self.external_identifier = OURS
        self.replaces_order_id = None
        self.replacing_order_id = None
        self.legs = [_StubLeg(), _StubLeg()]


class _SdkAccount:
    account_number = "5WX00000"

    def __init__(self, live: list) -> None:
        self._live = live

    async def get_live_orders(self, session):
        return list(self._live)


async def test_an_unmappable_order_halts_sandbox_startup_through_the_real_adapter(
    tmp_path: Path,
):
    """C2, end to end and through the code that had the defect.

    Deliberately NOT built on `FakeOrderExecution`: the drop lived in
    `TastytradeOrderExecution.get_live_orders`, so a test that starts downstream of
    it proves nothing about the fix. This one hands the real adapter an SDK-shaped
    order carrying a status `OrderStatus` does not have — the exact enum-drift case
    — and asserts it comes out the far end as a halt.

    Before 2026-08-20 that order was dropped in the adapter with a WARNING, the
    reconcile came back clean, and the process resumed trading against an account
    holding something it could not read.
    """
    execution = TastytradeOrderExecution(
        object(), _SdkAccount([_StubPlaced(4242, "Escrowed")])
    )
    cfg = _config(tmp_path, environment="sandbox")
    app = Application(
        cfg,
        reconciler=Reconciler(
            FakeBrokerAccount([]), InMemoryPositionStore(),
            execution=execution, order_store=InMemoryOrderStore(),
        ),
    )

    await app.startup()
    try:
        assert app._halted is not None, "an unreadable order must refuse the resume"
        assert app._halted.source == "reconciliation_mismatch"
        assert app.killswitch.engaged is True
    finally:
        await app.shutdown()

    conn = connect(cfg.audit.db_path)
    verdict = conn.execute(
        "SELECT payload FROM audit_log WHERE category = 'reconcile.result'"
    ).fetchone()["payload"]
    conn.close()
    assert UNMAPPABLE_BROKER_ORDER in verdict
    # The raw status the enum refused, durable in the trail — normally a one-line
    # fix to `OrderStatus`, and useless if it is not written down.
    assert "Escrowed" in verdict


async def test_a_readable_order_still_reconciles_through_the_real_adapter(
    tmp_path: Path,
):
    """The regression half: the common path must be untouched.

    Same wiring, same adapter, a status the enum DOES have and a ledger that knows
    the order. If making unmappable orders visible cost a false halt here, it would
    brick every startup.
    """
    store = InMemoryOrderStore()
    placed = _StubPlaced(4242, "Live")
    await store.record_order(order(order_id=4242))
    app = Application(
        _config(tmp_path, environment="sandbox"),
        reconciler=Reconciler(
            FakeBrokerAccount([]), InMemoryPositionStore(),
            execution=TastytradeOrderExecution(object(), _SdkAccount([placed])),
            order_store=store,
        ),
    )

    await app.startup()
    try:
        assert app._halted is None
        assert app.killswitch.engaged is False
    finally:
        await app.shutdown()


async def test_a_dead_unexplained_order_does_not_refuse_startup(tmp_path: Path):
    execution = FakeOrderExecution()
    ack = await execution.submit(order(order_id=None, status=None), dry_run=False)
    await execution.cancel(ack.order.broker_order_id)

    app = Application(
        _config(tmp_path, environment="sandbox"),
        reconciler=Reconciler(
            FakeBrokerAccount([]), InMemoryPositionStore(),
            execution=execution, order_store=InMemoryOrderStore(),
        ),
    )
    await app.startup()
    try:
        assert app.killswitch.engaged is False
    finally:
        await app.shutdown()


# -- the shared-account hint: a diagnosis, never a verdict (2026-09-10, C9) ----
#
# §4 of the 2026-08-31 record: a live halt that says "reconciliation failed with
# 7 discrepancies" when the cause is "this account is not dedicated to Argus"
# costs an operator an hour at exactly the wrong time. The hint names the LIKELY
# cause on the payload; it reaches no verdict, no grade and no halt.
# → docs/designs/2026-09-10-pinning-the-traded-account.md §3.6


def _unexpected(n: int) -> Discrepancy:
    return Discrepancy(f"SPXW260918P050{n}0000", UNEXPECTED_AT_BROKER, 0, -1)


class _ResultReconciler:
    """A `Reconciler` stand-in returning a fixed verdict.

    The hint is about the RECORD and the ALERT, not the compare, so the compare is
    the part to stub — and stubbing it is what lets a test hand `reconcile_and_
    record` a discrepancy set no fake broker can produce.
    """

    def __init__(self, result: ReconciliationResult) -> None:
        self._result = result

    async def reconcile(self, *, retry=None, on_attempt_failed=None):
        return self._result


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    async def record(self, category, payload):
        self.records.append((category, payload))


async def _record(result: ReconciliationResult, environment: str):
    """Drive `reconcile_and_record` with no order ledger and capture both sinks."""
    audit, alerts = _RecordingAudit(), []
    await reconcile_and_record(
        _ResultReconciler(result),
        audit=audit,
        environment=environment,
        today=date(2026, 9, 10),
        session_settled=False,
        alert=lambda kind, payload: alerts.append((kind, payload)),
    )
    return audit, alerts


async def test_a_live_all_unexpected_mismatch_carries_the_shared_account_hint():
    """The one shape that means "somebody else is trading this account": every
    position discrepancy is a broker holding something Argus has no record of."""
    result = ReconciliationResult(
        ok=False, discrepancies=(_unexpected(1), _unexpected(2), _unexpected(3))
    )
    audit, alerts = await _record(result, "live")

    (kind, payload) = alerts[0]
    assert kind == "reconciliation_mismatch"
    assert "3 broker position(s)" in payload["hint"]
    assert "2026-08-31-live-account-is-not-dedicated.md" in payload["hint"]
    # The durable record carries the same sentence — the operator diagnosing from
    # the audit trail sees what the phone saw.
    (category, recorded) = audit.records[0]
    assert category == "reconcile.result"
    assert recorded["hint"] == payload["hint"]


def test_the_hint_changes_no_verdict():
    """The test the diagnostic exists to be bounded by.

    With the hint present, the verdict, the grade and the halt are exactly what
    they are without it — the hint is prose on a payload, and this pins that no
    future edit makes it load-bearing."""
    result = ReconciliationResult(
        ok=False, discrepancies=(_unexpected(1), _unexpected(2))
    )
    hint = shared_account_hint(result, environment="live")
    assert hint is not None

    assert result.ok is False
    assert halt_grade(result) is HaltGrade.ALL
    assert mismatch_halts_process("live") is True

    # And the payload the hint is folded into carries the SAME verdict fields.
    payload = {**result.as_payload(), "hint": hint}
    assert payload["ok"] is result.ok
    assert payload["discrepancies"] == result.as_payload()["discrepancies"]
    assert payload["order_discrepancies"] == result.as_payload()["order_discrepancies"]


async def test_a_mixed_discrepancy_set_gets_no_hint():
    """A missing_at_broker alongside the unexpected rows is Argus's own book
    disagreeing — pointing at the shared account sends the operator to the wrong
    document, so no hint key at all."""
    result = ReconciliationResult(
        ok=False,
        discrepancies=(
            _unexpected(1),
            _unexpected(2),
            Discrepancy("SPXW260918P05005000", MISSING_AT_BROKER, -1, 0),
        ),
    )
    audit, alerts = await _record(result, "live")

    _, payload = alerts[0]
    assert "hint" not in payload
    assert "hint" not in audit.records[0][1]


async def test_sandbox_gets_no_hint():
    """The same input under cert: `unexpected_at_broker` rows are ORDINARY there,
    inside the sandbox reset's 24-hour order-reporting window, and a hint that
    fires on the everyday cert failure is a hint nobody reads by the time live
    needs it."""
    result = ReconciliationResult(
        ok=False, discrepancies=(_unexpected(1), _unexpected(2))
    )
    audit, alerts = await _record(result, "sandbox")

    _, payload = alerts[0]
    assert "hint" not in payload
    assert "hint" not in audit.records[0][1]


async def test_a_clean_result_records_no_hint_key():
    """Absent, not None: a `"hint": null` on every clean `reconcile.result` row is
    noise on the durable chain, and a reader testing `"hint" in payload` is
    clearer than one testing for non-null."""
    result = ReconciliationResult(ok=True, discrepancies=())
    audit, alerts = await _record(result, "live")

    assert alerts == []  # clean never alerts
    (category, recorded) = audit.records[0]
    assert category == "reconcile.result"
    assert "hint" not in recorded
    assert recorded["ok"] is True
