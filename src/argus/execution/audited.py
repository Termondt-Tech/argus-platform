"""Durable-before-the-wire order execution (spec §6) + order recording.

`AuditedOrderExecution` wraps chunk 1's `OrderExecutionPort` and writes a Tier 1
record BEFORE each call reaches the broker. It is a decorator, and that is the
point: chunk 1's `TastytradeOrderExecution` is not modified, does not import the
audit log, and does not know this exists. The seam was designed for exactly this —
submit/cancel/replace were kept as four separate atomic calls specifically so this
layer could sandwich its own writes around them (see `broker.py`'s module
docstring). It implements the same port, so chunk 3/5 can drop it in wherever the
raw adapter would have gone.

**Why the write comes first.** "Everything writes to the audit log before the next
action proceeds" only holds if the record beats the irreversible thing. An order
that reaches the exchange and is not in the log is invisible to a restart: the
recovery path is "ask the broker and hard-halt on anything unexplained", and the
pre-send record is what turns "unexplained" into "here is the order I was sending
when I died, at this price, for this candidate". A record written after the ack
would be missing in precisely the crash it exists for. The cost is the opposite
failure — a record for an order that never routed, because the call raised — and
that direction is fine: it is followed by a failure record, and reconciliation
compares against the broker either way.

An audit write failure here does NOT get caught. `AuditLog.record` raises
`AuditWriteError`, which latches the log failed and (through
`Application._halt_on_audit_failure`) engages the kill switch and stops the
process. Submitting an order while the black-box recorder is dead is the one thing
this layer must never allow, so the exception propagates and the submission simply
does not happen.

**Not the duplicate guard.** This layer does not call
`DuplicateOrderGuard.has_conflicting_order` — the guard belongs before a new trade
idea's FIRST submission and must not run before a price-walking replace, and only
the caller knows which of those it is doing (see `guard.py`). Fusing them here
would make the wrong one automatic.
"""

from __future__ import annotations

import logging
from typing import Any

from argus.alerts import AlertFn, log_alert
from argus.audit.log import AuditLog
from argus.execution.broker import OrderExecutionPort
from argus.execution.models import (
    LiveOrders,
    Order,
    OrderAck,
    UnmappableOrderError,
    infer_direction,
    parse_external_identifier,
)
from argus.execution.store import OrderStore

logger = logging.getLogger("argus.execution.audited")


class AuditedOrderExecution:
    """`OrderExecutionPort` + a Tier 1 write before every call + order recording.

    `store` is optional so this can be used for validation runs (a dry-run smoke
    test has nothing to record); with a store, every order the broker acknowledges
    is written to Argus's own ledger, which is what makes it 'accounted for' at
    the next restart-time reconciliation.
    """

    def __init__(
        self,
        execution: OrderExecutionPort,
        *,
        audit: AuditLog,
        store: OrderStore | None = None,
        alert: AlertFn | None = None,
    ) -> None:
        self._execution = execution
        self._audit = audit
        self._store = store
        # Defaults to `log_alert`, NOT to silence — the same posture as
        # `fills.OrderEventConsumer`. An omitted sink degrades an unmappable-order
        # alert to a log line; it never degrades it to nothing.
        self._alert = alert or log_alert
        #: Unmappable orders already announced, keyed (broker_order_id, raw_status).
        #: `get_live_orders()` is POLLED — the price-walking loop reads it every
        #: cycle — so an un-deduped alert would be a per-second page for a
        #: condition that does not change. Process-lifetime by design: it is one
        #: fact about one broker order, and re-announcing it on a restart is
        #: correct (a restart is when reconciliation halts on it anyway).
        self._announced_unmappable: set[tuple[int | None, str | None]] = set()

    @property
    def records_orders(self) -> bool:
        """Whether acknowledged orders reach Argus's own ledger.

        Public so a caller can REFUSE to run without it rather than discovering the
        omission a day later. The price-walking loop (chunk 3) checks this at
        construction: an order placed without a ledger row is unaccounted for at the
        next restart, and order reconciliation hard-halts sandbox/live on exactly
        that — a silent, delayed failure with a hard stop at the end of it.
        """
        return self._store is not None

    async def submit(self, order: Order, *, dry_run: bool) -> OrderAck:
        await self._audit.record(
            "order.submit_attempt", {**order.to_payload(), "dry_run": dry_run}
        )
        try:
            ack = await self._execution.submit(order, dry_run=dry_run)
        except Exception as exc:
            await self._record_failure("order.submit_failed", exc, order.to_payload())
            raise
        # Ledger BEFORE the post-write: if the audit write fails the process is
        # about to halt, and an order that reached the broker is far better
        # recorded than not — an unrecorded one hard-halts the next reconcile.
        # A dry run mints no id, and the store ignores it.
        if not dry_run and self._store is not None:
            await self._store.record_order(ack.order)
        await self._audit.record("order.submitted", ack.to_payload())
        self._note_direction_disagreement(ack.order)
        return ack

    async def cancel(self, broker_order_id: int) -> None:
        await self._audit.record("order.cancel_attempt", {"broker_order_id": broker_order_id})
        try:
            await self._execution.cancel(broker_order_id)
        except Exception as exc:
            await self._record_failure(
                "order.cancel_failed", exc, {"broker_order_id": broker_order_id}
            )
            raise
        # Deliberately NOT recorded as "cancelled": the broker has accepted the
        # request, which leaves the order at CANCEL_REQUESTED, not gone. The fill
        # consumer records the actual terminal status when the stream reports it.
        await self._audit.record(
            "order.cancel_requested", {"broker_order_id": broker_order_id}
        )

    async def replace(self, broker_order_id: int, order: Order) -> Order:
        await self._audit.record(
            "order.replace_attempt",
            {"replaces_order_id": broker_order_id, **order.to_payload()},
        )
        try:
            replaced = await self._execution.replace(broker_order_id, order)
        except Exception as exc:
            await self._record_failure(
                "order.replace_failed",
                exc,
                {"replaces_order_id": broker_order_id, **order.to_payload()},
            )
            raise
        # A replace mints a NEW broker id (verified against sandbox, chunk 1), so
        # the new order needs its own ledger row or the next restart would find a
        # working order it cannot account for.
        if self._store is not None:
            await self._store.record_order(replaced)
        await self._audit.record("order.replaced", replaced.to_payload())
        self._note_direction_disagreement(replaced)
        return replaced

    async def get_live_orders(self) -> LiveOrders:
        """Pass-through for the answer; NOT a pass-through for what it admits.

        A read changes nothing, so there is nothing to make durable before it —
        and this is called on the recovery path, where the audit log may be
        exactly what we are trying to reconcile against. The result is returned
        untouched.

        What is added is the visibility an unmappable order was missing. Before
        2026-08-20 such an order was dropped in the adapter and logged at WARNING:
        journal only, no audit record, nothing on a phone. This layer is where the
        stack's audit log lives, so this is where a Tier 1 record and an alert go.

        **This announces; it does not halt.** The distinction is deliberate and is
        the same one `fills.py` already draws. Mid-session, an unmappable order is
        not by itself proof that anything of Argus's is wrong — a hand-placed
        order in another instrument with a status the mirrored enum lags on is a
        real and benign possibility — and halting the process on it would take the
        watchdog down with it, abandoning positions Argus certainly does hold.
        What DOES halt is restart-time reconciliation, where the premise is
        "refuse to resume on anything unexplained" and an unreadable order is the
        definition of unexplained (`reconcile.UNMAPPABLE_BROKER_ORDER`). In
        between, the duplicate guard blocks the specific identifier, so nothing
        doubles while a human reads the alert.

        → docs/decisions/2026-08-20-unmappable-orders-reach-reconciliation.md
        """
        live = await self._execution.get_live_orders()
        for order in live.unmappable:
            key = (order.broker_order_id, order.raw_status)
            if key in self._announced_unmappable:
                continue
            self._announced_unmappable.add(key)
            logger.error(
                "broker order %s is unmappable (status=%r): %s",
                order.broker_order_id, order.raw_status, order.reason,
            )
            self._alert("unmappable_broker_order", order.to_payload())
            # Tier 1: an order Argus cannot read is a fact the next restart's
            # reconciliation will halt on, and the trail has to say when it was
            # first seen — not just that it was still there at 09:31 the next day.
            await self._audit.record("order.unmappable_live", order.to_payload())
        return live

    def _note_direction_disagreement(self, order: Order) -> None:
        """Tier 2 telemetry when the echoed leg actions contradict the identifier (2026-09-03).

        tastytrade labels each leg relative to the ACCOUNT'S position, so on a
        chained book a closing order comes back with a `Sell to Open` leg and
        `infer_direction` reads the close as an entry. Known-normal, not a fault:
        this never halts, never raises, never writes Tier 1, and feeds no rail.
        Called at the WRITE moment (`submit`/`replace`) only — that is where
        `_upsert_order`'s first-write-wins durability makes the value being
        written the one that sticks — and deliberately never from
        `get_live_orders()`, which polls on every cycle and would re-report a
        durable property of one order as noise.

        **`infer_direction` here is a genuinely new call, not a repeat of one
        `map_placed_order` already survived.** When the identifier parses,
        `map_placed_order` takes `direction` from it and never calls
        `infer_direction` at all — so a leg shape `infer_direction` cannot
        classify (no `_TO_OPEN`/`_TO_CLOSE` suffix; the futures case this
        SPX-only app never routes) is being asked here for the first time,
        after the order is already durably recorded. Pre-fix, that same shape
        would have failed loud INSIDE `map_placed_order`, before anything was
        written — the accepted, documented behaviour. This diagnostic-only
        check must not turn that into a raise on an already-successful,
        already-recorded submission, so an unclassifiable leg set skips the
        comparison rather than propagating.
        """
        parsed = parse_external_identifier(order.external_identifier)
        if parsed is None:
            # A foreign order has no minted answer; `infer_direction` was, and
            # remains, its only source of direction.
            return
        try:
            inferred = infer_direction(order.legs)
        except UnmappableOrderError:
            # Cannot form the comparison. Not evidence of a disagreement, and
            # not grounds to raise out of an otherwise-successful write.
            return
        if inferred is order.direction:
            return
        self._audit.telemetry(
            "order.direction_disagreement",
            {
                "broker_order_id": order.broker_order_id,
                "external_identifier": order.external_identifier,
                "minted_direction": str(parsed[0]),
                "broker_inferred_direction": str(inferred),
            },
        )

    async def _record_failure(
        self, category: str, exc: Exception, detail: dict[str, Any]
    ) -> None:
        """Record a broker call that raised.

        The outcome is genuinely UNKNOWN — a timeout can mean the order routed
        anyway — so this records what was attempted and what came back, and says
        nothing about whether it took effect. Deciding that is reconciliation's
        job, from the broker.
        """
        logger.error("%s: %s: %s", category, type(exc).__name__, exc)
        await self._audit.record(
            category,
            {**detail, "error_type": type(exc).__name__, "error": str(exc)[:500]},
        )
