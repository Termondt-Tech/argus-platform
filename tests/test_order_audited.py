"""Durable-before-the-wire order execution.

The ordering assertions are the load-bearing ones. A record written after the ack
is missing in precisely the crash it exists for: an order that reached the
exchange and is not in the log is invisible to a restart, and the recovery path
("ask the broker, hard-halt on anything unexplained") has nothing to explain it
with.

Chunk 1's adapter is not modified by any of this — the wrapper implements the same
port and delegates, which is what the four-separate-calls seam was for.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from argus.audit.log import AuditLog, AuditWriteError
from argus.execution.audited import AuditedOrderExecution
from argus.execution.broker import OrderExecutionPort
from argus.execution.fake import FakeOrderExecution
from argus.execution.models import (
    LegAction,
    Order,
    OrderAck,
    OrderDirection,
    OrderLeg,
    OrderStatus,
    map_placed_order,
)
from argus.execution.store import InMemoryOrderStore, SqliteOrderStore

OURS = "argus-open-20260729-put-5000-4995"


def spread() -> Order:
    return Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", LegAction.SELL_TO_OPEN, 1),
            OrderLeg("SPXW  P4995", LegAction.BUY_TO_OPEN, 1),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=OURS,
        candidate_id="20260729-put-5000-4995",
    )


class _Recorder:
    """Interleaves audit writes with broker calls so ORDER can be asserted.

    Keeps the first payload per category alongside the event names, so a test can
    assert WHAT was telemetered, not only that something was.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.payloads: dict[str, dict] = {}

    async def record(self, category, payload):
        self.events.append(category)
        self.payloads.setdefault(category, payload)

    def telemetry(self, category, payload):
        self.events.append(f"tier2:{category}")
        self.payloads.setdefault(category, payload)
        return True


class _TracingExecution(FakeOrderExecution):
    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self._log = log

    async def submit(self, order, *, dry_run):
        self._log.append("WIRE:submit")
        return await super().submit(order, dry_run=dry_run)

    async def cancel(self, broker_order_id):
        self._log.append("WIRE:cancel")
        return await super().cancel(broker_order_id)

    async def replace(self, broker_order_id, order):
        self._log.append("WIRE:replace")
        return await super().replace(broker_order_id, order)


@pytest.fixture
def wiring():
    audit = _Recorder()
    execution = _TracingExecution(audit.events)
    store = InMemoryOrderStore()
    return AuditedOrderExecution(execution, audit=audit, store=store), audit, store


# -- the port contract --------------------------------------------------------


def test_it_is_the_same_port_chunk_1_defined():
    # So chunk 3/5 can drop it in wherever the raw adapter would have gone.
    wrapped = AuditedOrderExecution(FakeOrderExecution(), audit=_Recorder())
    assert isinstance(wrapped, OrderExecutionPort)


# -- pre-send ordering --------------------------------------------------------


async def test_submit_records_before_it_reaches_the_broker(wiring):
    wrapped, audit, _ = wiring
    await wrapped.submit(spread(), dry_run=False)

    assert audit.events == ["order.submit_attempt", "WIRE:submit", "order.submitted"]


async def test_cancel_records_before_it_reaches_the_broker(wiring):
    wrapped, audit, _ = wiring
    ack = await wrapped.submit(spread(), dry_run=False)
    audit.events.clear()

    await wrapped.cancel(ack.order.broker_order_id)

    assert audit.events == ["order.cancel_attempt", "WIRE:cancel", "order.cancel_requested"]


async def test_replace_records_before_it_reaches_the_broker(wiring):
    wrapped, audit, _ = wiring
    ack = await wrapped.submit(spread(), dry_run=False)
    audit.events.clear()

    await wrapped.replace(ack.order.broker_order_id, spread().with_limit_price(Decimal("0.80")))

    assert audit.events == ["order.replace_attempt", "WIRE:replace", "order.replaced"]


async def test_a_dry_run_still_records_the_attempt(wiring):
    wrapped, audit, store = wiring
    await wrapped.submit(spread(), dry_run=True)

    assert audit.events == ["order.submit_attempt", "WIRE:submit", "order.submitted"]
    # ...but records no order: a dry run mints no broker id, so there is nothing
    # at the broker to account for.
    assert store.orders == {}


async def test_get_live_orders_is_a_pass_through(wiring):
    wrapped, audit, _ = wiring
    await wrapped.get_live_orders()
    assert audit.events == []   # a read changes nothing; nothing to make durable


# -- an order the broker holds and Argus cannot read --------------------------


async def test_an_unmappable_order_gets_a_tier_1_record_and_an_alert():
    """The visibility half of C2.

    Before 2026-08-20 an unmappable order was dropped in the adapter and logged at
    WARNING: journal only. Nothing reached the audit trail and nothing reached a
    phone, so the condition that hard-halts the next morning's startup was
    invisible until the morning.
    """
    audit, alerts = _Recorder(), []
    execution = FakeOrderExecution()
    execution.add_unmappable(broker_order_id=4242, external_identifier=OURS)
    wrapped = AuditedOrderExecution(
        execution, audit=audit, alert=lambda k, d: alerts.append((k, d))
    )

    answer = await wrapped.get_live_orders()

    assert answer.unmappable, "and the answer itself is returned untouched"
    assert audit.events == ["order.unmappable_live"]
    (kind, detail) = alerts[0]
    assert kind == "unmappable_broker_order"
    assert detail["broker_order_id"] == 4242
    assert detail["raw_status"] == "Escrowed"    # the value the enum refused


async def test_the_unmappable_alert_does_not_storm():
    """`get_live_orders()` is POLLED — the price-walking loop reads it every cycle.

    One unreadable order must not become a per-second page for a fact that is not
    changing. Deduped on (id, raw status), so a STATUS change on the same order is
    a new fact and does announce again.
    """
    audit, alerts = _Recorder(), []
    execution = FakeOrderExecution()
    execution.add_unmappable(broker_order_id=4242, external_identifier=OURS)
    wrapped = AuditedOrderExecution(
        execution, audit=audit, alert=lambda k, d: alerts.append((k, d))
    )

    for _ in range(5):
        await wrapped.get_live_orders()
    assert len(alerts) == 1 and audit.events == ["order.unmappable_live"]

    execution.add_unmappable(broker_order_id=4242, raw_status="Escrowed Harder")
    await wrapped.get_live_orders()
    assert len(alerts) == 2


async def test_an_unmappable_order_announces_but_does_NOT_halt():
    """The deliberate asymmetry, and the choice worth being explicit about.

    Mid-session, an unmappable order is not proof anything of Argus's is wrong — a
    hand-placed order in another instrument with a status the mirrored enum lags on
    is real and benign — and halting here would take the watchdog down with it,
    abandoning positions Argus certainly does hold. Restart-time reconciliation is
    where "refuse to resume on anything unexplained" applies
    (`reconcile.UNMAPPABLE_BROKER_ORDER`), and the guard blocks the specific
    identifier in the meantime.
    """
    audit = _Recorder()
    execution = FakeOrderExecution()
    execution.add_unmappable(broker_order_id=4242)
    wrapped = AuditedOrderExecution(execution, audit=audit)

    answer = await wrapped.get_live_orders()   # returns; does not raise

    assert len(answer.unmappable) == 1


# -- failures -----------------------------------------------------------------


async def test_a_broker_failure_is_recorded_and_re_raised(wiring):
    wrapped, audit, _ = wiring
    wrapped._execution.fail_next(RuntimeError("gateway timeout"))

    with pytest.raises(RuntimeError):
        await wrapped.submit(spread(), dry_run=False)

    # The outcome is genuinely unknown — a timeout can mean the order routed
    # anyway — so this records what was attempted, and says nothing about whether
    # it took effect. Reconciliation decides that, from the broker.
    assert audit.events == ["order.submit_attempt", "WIRE:submit", "order.submit_failed"]


async def test_a_dead_audit_log_stops_the_submission(audit: AuditLog, monkeypatch):
    def boom(rec):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(audit, "_insert", boom)
    execution = FakeOrderExecution()
    wrapped = AuditedOrderExecution(execution, audit=audit)

    # Submitting while the black-box recorder is dead is the one thing this layer
    # must never allow. The exception propagates (and latches the log failed,
    # which halts the process through Application's on_failure hook).
    with pytest.raises(AuditWriteError):
        await wrapped.submit(spread(), dry_run=False)
    assert execution.submitted == []


# -- order recording ----------------------------------------------------------


async def test_an_acknowledged_order_becomes_accounted_for(wiring):
    wrapped, _, store = wiring
    ack = await wrapped.submit(spread(), dry_run=False)

    # This is what stops the next restart hard-halting over an order Argus itself
    # just placed.
    assert await store.known_orders([ack.order.broker_order_id]) == {
        ack.order.broker_order_id
    }


async def test_a_replacement_gets_its_own_ledger_row(wiring):
    wrapped, _, store = wiring
    ack = await wrapped.submit(spread(), dry_run=False)
    new = await wrapped.replace(
        ack.order.broker_order_id, spread().with_limit_price(Decimal("0.80"))
    )

    # A replace mints a NEW broker id (verified against sandbox in chunk 1), so
    # without this the replacement is the unexplained order at the next restart.
    assert new.broker_order_id != ack.order.broker_order_id
    assert await store.known_orders(
        [ack.order.broker_order_id, new.broker_order_id]
    ) == {ack.order.broker_order_id, new.broker_order_id}


async def test_it_works_without_a_store(wiring):
    # A validation run has nothing to record.
    wrapped = AuditedOrderExecution(FakeOrderExecution(), audit=_Recorder())
    ack = await wrapped.submit(spread(), dry_run=True)
    assert ack.dry_run is True


async def test_the_recorded_order_reaches_the_real_ledger(audit: AuditLog, db):
    audit.start()
    store = SqliteOrderStore(audit.write)
    wrapped = AuditedOrderExecution(FakeOrderExecution(), audit=audit, store=store)

    ack = await wrapped.submit(spread(), dry_run=False)
    await audit.aclose()

    row = db.execute(
        "SELECT external_identifier, candidate_id, status FROM orders WHERE broker_order_id = ?",
        (ack.order.broker_order_id,),
    ).fetchone()
    assert row["external_identifier"] == OURS
    assert row["candidate_id"] == "20260729-put-5000-4995"
    assert row["status"] == str(OrderStatus.RECEIVED)


# -- direction disagreement telemetry (2026-09-03) -----------------------------
#
# tastytrade labels each leg relative to the ACCOUNT'S position, so on a chained
# book a closing order's shared-strike leg comes back `Sell to Open` and
# `infer_direction` on the echoed legs contradicts the direction the identifier
# gave. Known-normal — Tier 2 telemetry at the write moment, and nothing else.


def closing_spread() -> Order:
    return Order(
        direction=OrderDirection.CLOSE,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", LegAction.BUY_TO_CLOSE, 1),
            OrderLeg("SPXW  P4995", LegAction.SELL_TO_CLOSE, 1),
        ),
        limit_price=Decimal("-1.25"),
        external_identifier="argus-close-20260729-put-5000-4995",
        candidate_id="20260729-put-5000-4995",
    )


class _RelabellingExecution(FakeOrderExecution):
    """The 2026-09-01 shape at the wire: a closing order on a chained pair whose
    shared-strike leg the broker echoes back relabelled, re-read exactly the way
    the production adapter reads a response — through `map_placed_order`. The
    ack's legs are therefore the BROKER's, and its direction is the mapper's
    post-2026-09-03 answer: the identifier's."""

    async def submit(self, order, *, dry_run):
        await super().submit(order, dry_run=dry_run)
        return OrderAck(order=self._relabelled(order), dry_run=dry_run)

    async def replace(self, broker_order_id, order):
        await super().replace(broker_order_id, order)
        return self._relabelled(order)

    @staticmethod
    def _relabelled(order: Order) -> Order:
        class _Leg:
            def __init__(self, symbol, action, quantity):
                self.symbol = symbol
                self.action = action
                self.quantity = quantity
                self.remaining_quantity = None
                self.instrument_type = "Equity Option"

        class _Placed:
            def __init__(self, **kw):
                self.id = kw.get("id")
                self.status = kw.get("status", "Live")
                self.underlying_symbol = kw.get("underlying_symbol", "SPXW")
                self.price = kw.get("price")
                self.external_identifier = kw.get("external_identifier", "")
                self.legs = kw.get("legs")

        sent = order.legs
        return map_placed_order(
            _Placed(
                id=4242,
                price=order.limit_price,
                external_identifier=order.external_identifier,
                legs=[
                    _Leg(sent[0].symbol, str(sent[0].action), sent[0].quantity),
                    # The relabelled shared strike: the account is net flat on the
                    # long leg, so closing it echoes as an OPEN.
                    _Leg(sent[1].symbol, "Sell to Open", sent[1].quantity),
                ],
            )
        )


async def test_a_direction_disagreement_is_tier_2_telemetry_and_nothing_else():
    """A closing order on a chained pair comes back with its shared-strike leg
    relabelled, so `infer_direction` on the echoed legs (OPEN) contradicts the
    direction the identifier gave (CLOSE). Known-normal — Tier 2 telemetry, no
    Tier 1 record, no exception, and the ack still comes back."""
    audit = _Recorder()
    wrapped = AuditedOrderExecution(
        _RelabellingExecution(), audit=audit, store=InMemoryOrderStore()
    )
    ack = await wrapped.submit(closing_spread(), dry_run=False)

    assert ack.order.direction is OrderDirection.CLOSE
    assert "order.direction_disagreement" not in audit.events       # never Tier 1
    assert "tier2:order.direction_disagreement" in audit.events     # Tier 2 only
    payload = audit.payloads["order.direction_disagreement"]
    assert payload["minted_direction"] == "close"
    assert payload["broker_inferred_direction"] == "open"
    assert payload["external_identifier"] == "argus-close-20260729-put-5000-4995"
    assert payload["broker_order_id"] == 4242


async def test_a_direction_disagreement_on_replace_is_tier_2_telemetry_too():
    """A replace mints a new rung under the same identifier and the relabelling
    is deterministic given the book, so the same telemetry belongs at the same
    write moment on this path."""
    audit = _Recorder()
    wrapped = AuditedOrderExecution(
        _RelabellingExecution(), audit=audit, store=InMemoryOrderStore()
    )
    replaced = await wrapped.replace(1000, closing_spread())

    assert replaced.direction is OrderDirection.CLOSE
    assert "order.direction_disagreement" not in audit.events
    assert "tier2:order.direction_disagreement" in audit.events


async def test_an_agreeing_direction_writes_no_disagreement_telemetry(wiring):
    """The ordinary case: an opening order whose echo is unrelabelled. The
    disagreement is the signal, not the mapping."""
    wrapped, audit, _ = wiring
    await wrapped.submit(spread(), dry_run=False)
    assert "tier2:order.direction_disagreement" not in audit.events
    assert "order.direction_disagreement" not in audit.events


async def test_a_foreign_order_never_emits_direction_disagreement_telemetry(wiring):
    """No Argus identifier, no minted answer: `infer_direction` was and remains
    the only source, so there is nothing to disagree with."""
    wrapped, audit, _ = wiring
    foreign = Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", LegAction.SELL_TO_OPEN, 1),
            OrderLeg("SPXW  P4995", LegAction.BUY_TO_OPEN, 1),
        ),
        limit_price=Decimal("0.85"),
        external_identifier="manual-20260903-1",
    )
    await wrapped.submit(foreign, dry_run=False)
    assert "tier2:order.direction_disagreement" not in audit.events


async def test_an_unclassifiable_leg_set_skips_the_comparison_rather_than_raising(
    wiring,
):
    """`infer_direction` is a NEW call at this site, not a repeat of one
    `map_placed_order` already survived: when the identifier parses,
    `map_placed_order` takes `direction` from it and never calls
    `infer_direction` at all, so a leg action `infer_direction` cannot classify
    (`LegAction.BUY`/`SELL` — futures, never produced by this app) is asked here
    for the first time, after `record_order` and the Tier 1 write already
    succeeded. It must not turn that into a raise on an already-recorded
    submission — no comparison, no telemetry, and the ack still comes back."""
    wrapped, audit, _ = wiring
    unclassifiable = Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", LegAction.SELL, 1),
            OrderLeg("SPXW  P4995", LegAction.BUY, 1),
        ),
        limit_price=Decimal("0.85"),
        external_identifier="argus-open-20260903-put-5000-4995",
    )
    ack = await wrapped.submit(unclassifiable, dry_run=False)
    assert ack.order.direction is OrderDirection.OPEN
    assert "tier2:order.direction_disagreement" not in audit.events
    assert "order.direction_disagreement" not in audit.events


# -- what this layer deliberately does NOT do ---------------------------------


async def test_it_does_not_run_the_duplicate_guard(wiring):
    wrapped, audit, _ = wiring
    await wrapped.submit(spread(), dry_run=False)
    await wrapped.submit(spread(), dry_run=False)   # the broker allows this, and so does this layer

    # The guard belongs before a NEW trade idea's first submission and must not
    # run before a price-walking replace; only the caller knows which it is doing
    # (chunk 3/5). Fusing it in here would make the wrong one automatic.
    assert "order.duplicate_blocked" not in audit.events
    assert len(wrapped._execution.submitted) == 2


def test_chunk_2_contains_no_retry_loop_or_cadence():
    """No in-flight order state machine, and no replay of a sent message.

    A crash mid-cancel/replace is recovered by asking the broker what actually
    happened and hard-halting on anything unexplained — never by resuming. The
    shape that would violate that is a wait-and-retry around a broker call, so
    this pins its absence structurally: no `while`, no sleep, anywhere in the
    modules this chunk added. Deciding when to call a primitive again is chunk 3's
    job, in chunk 3's file.

    Parsed, not grepped: every one of these modules discusses looping and retry in
    prose explaining why it has none.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "argus" / "execution"
    chunk_2 = ("store.py", "fills.py", "guard.py", "audited.py")
    offenders: list[str] = []
    for name in chunk_2:
        path = src / name
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.While):
                offenders.append(f"{name}:{node.lineno} while-loop")
            if isinstance(node, ast.Attribute) and node.attr == "sleep":
                offenders.append(f"{name}:{node.lineno} sleep")
    assert offenders == []
