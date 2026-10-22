"""Fill consumption — the `AlertStreamer` order feed into Argus's position record.

**The transport was already there.** `account/stream.py` has listened for
`PlacedOrder` alerts since the account layer was built: `_listen_orders` runs one
`streamer.listen(PlacedOrder)` iterator inside the same `_pump` as positions and
balances, and hands each frame to an injected `on_order` callback. What did not
exist was anything to hand it to — the only consumer in the tree was the shadow
session's stub, which logs "an order event happened here, which is unexpected,
because shadow submits nothing". `FillConsumer` is the real one, and it plugs into
that existing hook with no change to the streamer at all.

That matters for correctness, not just tidiness: fills arrive as a PUSH, not by
polling (spec §5), and the stream re-sends an order on every status change. So the
consumer is deliberately dumb about repetition — it forwards everything to the
store, which books only what is new (see `store.py`). No de-duplication, no
sequencing, no "is this the fill I was waiting for" state anywhere.

**It will not adopt somebody else's fill.** An order whose `external_identifier`
is not Argus's (`is_argus_identifier`) is recorded and alerted, never applied.
Argus's `positions` table is its record of ITS OWN positions, and the entire point
of reconciliation is that a position Argus did not place shows up as a mismatch —
quietly absorbing a hand-placed fill into the expected set would make the rail
agree with reality by rewriting what it expected, which is precisely the guess
spec §6 forbids.
"""

from __future__ import annotations

import logging
from typing import Any

from argus.alerts import AlertFn, log_alert
from argus.audit.log import AuditLog
from argus.execution.models import (
    Order,
    OrderDisposition,
    UnmappableOrderError,
    is_argus_identifier,
    map_placed_order,
)
from argus.execution.store import AppliedFill, OrderStore
from argus.risk.loss_budget import DayLedger, LedgerFill

logger = logging.getLogger("argus.execution.fills")


class FillConsumer:
    """Turns `PlacedOrder` alerts into `positions` rows and a Tier 1 trail.

    Shaped to be passed straight to `AlertStreamerAccount(on_order=...)`: it is
    callable, takes one order object, and returns None.
    """

    def __init__(
        self,
        *,
        store: OrderStore,
        audit: AuditLog,
        alert: AlertFn | None = None,
        ledger: DayLedger | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        # Today's loss-budget ledger (2026-09-21), fed from EXACTLY the movements
        # written below as `order.fill` — the rows it is rebuilt from on a restart —
        # so the in-memory and rehydrated values are one computation over one set of
        # numbers. None only off the trading path (tools, tests): the session passes
        # its day's ledger, and `test_loss_budget` pins that it does.
        self._ledger = ledger
        # Defaults to `log_alert`, NOT to silence. The foreign-fill path below is
        # documented as "Tier 1 + alert"; when `alert` could default to None, an
        # omitted argument quietly turned that into "Tier 1 only". Now the real
        # phone-reachable sink (`argus.notify`, wired process-wide by
        # `Application.startup`) is what gets passed in, and forgetting to pass it
        # degrades to a log line rather than to nothing.
        self._alert = alert or log_alert
        #: Broker order ids already recorded DEAD at Tier 1, so the promotion below
        #: fires ONCE per order rather than once per frame.
        #:
        #: Not defensive. The AlertStreamer re-delivers current state on reconnect,
        #: and a Tier 1 write is awaited, holds the writer lock and fsyncs under
        #: `audit.synchronous = "FULL"` — 2026-08-13 put 1,517 of them in the log
        #: from one flapping condition, which is the precedent this exists to avoid
        #: repeating. Bounded by construction: one entry per order that dies, which
        #: is single digits a day.
        self._terminal_recorded: set[Any] = set()

    def _is_first_terminal(self, order: Order) -> bool:
        """True the FIRST time this order is seen DEAD, false ever after.

        DEAD only — never FILLED. A filled order is terminal in the lifecycle sense
        and is the one thing that must not be filed next to a cancellation
        (`OrderDisposition`'s docstring); its record is `order.fill`, written on
        `consume`'s applied path, and it never reaches this branch on the movement
        that matters.

        Keyed on `broker_order_id`, falling back to `(identifier, status)` when the
        broker sent none. NOT on `external_identifier` alone: a walk reprices, so
        several rungs share one identifier, and deduping on it would swallow the
        second rung's rejection — the same "one identifier, many orders" trap the
        market-crossing confirmation is scoped around.
        """
        if order.disposition is not OrderDisposition.DEAD:
            return False
        key: Any = (
            order.broker_order_id
            if order.broker_order_id is not None
            else (order.external_identifier, str(order.status))
        )
        if key in self._terminal_recorded:
            return False
        self._terminal_recorded.add(key)
        return True

    async def __call__(self, placed: Any) -> None:
        await self.consume(placed)

    async def consume(self, placed: Any) -> tuple[AppliedFill, ...]:
        """Handle one order event. Raises only if the audit log is dead.

        Everything else is caught and recorded, because this runs inside the
        account stream's listen loop: an exception escaping here would tear down
        `_pump` and force a stream reconnect, so one strange order frame from
        elsewhere in the account would take out the position and balance feeds
        with it.

        Returns what actually MOVED — the empty tuple for the common case of a
        status change or a repeat. The watchdog's `note_fills` needs it: a leg
        Argus itself just closed is expected to linger in the broker's book for a
        few seconds, and a leg nobody closed is not, and nothing downstream of this
        can tell those apart from position counts alone (2026-08-20). Returned
        rather than pushed through a callback because `on_order` already holds the
        late-bound watchdog reference, and one seam is enough.
        """
        try:
            order = map_placed_order(placed)
        except UnmappableOrderError as exc:
            # Loud, and Tier 1. Deliberately still `map_placed_order`, not
            # `map_live_order`: there, one call carries a LIST and one bad row must
            # not fail the whole read, so the failure becomes a value. Here one
            # frame is one order — there is no list to protect, and an unmappable
            # order EVENT may have been a fill, which is a position we will not
            # record. Restart-time order reconciliation is the backstop; this
            # record is how anyone finds out before then.
            self._notify("order_event_unmappable", {"error": str(exc)})
            await self._audit.record(
                "order.unmappable",
                {"error": str(exc), "broker_order_id": getattr(placed, "id", None)},
            )
            return ()

        if not is_argus_identifier(order.external_identifier):
            self._notify(
                "foreign_order_event",
                {
                    "broker_order_id": order.broker_order_id,
                    "external_identifier": order.external_identifier,
                },
            )
            await self._audit.record("order.foreign", order.to_payload())
            return ()

        applied = await self._store.apply(order)
        if not applied:
            payload = {
                "broker_order_id": order.broker_order_id,
                "external_identifier": order.external_identifier,
                "status": str(order.status) if order.status is not None else None,
                "disposition": str(order.disposition),
                # The broker's own words for why, when it gave any. Present on every
                # `order.event` rather than only the DEAD ones: a reason arriving on
                # a status this code did not expect to carry one is exactly the kind
                # of thing worth seeing, and a null costs nothing.
                "reject_reason": order.reject_reason,
            }
            if self._is_first_terminal(order):
                # NOT a status change. A DEAD order is the END of a trade idea, and
                # in the common case the ONLY record that it ended — the walk logs
                # its own outcome, but nothing else names the broker's verdict. Tier
                # 2 is queued and droppable, which is right for a flap and wrong for
                # a terminal state; the whole point of capturing `reject_reason` is
                # lost if the record carrying it can be dropped under queue pressure.
                # `_is_first_terminal` keeps this to one write per order.
                await self._audit.record("order.event", payload)
                return ()
            # The common case by volume: a status change, or a repeat of a fill
            # already booked. Tier 2 — losing one of these costs nothing, and at
            # this cadence they would otherwise dominate the Tier 1 trail.
            self._audit.telemetry("order.event", payload)
            return ()

        # Position moved. Tier 1, one record per leg movement — this is the
        # evidence reconciliation's expected state is built from, and it is
        # written AFTER the store commit on purpose: if the process dies between
        # the two, the position is on disk without its audit line (recoverable,
        # and reconciliation still agrees with the broker), which is the better
        # half of that trade than an audit line for a position that was never
        # booked.
        for fill in applied:
            await self._audit.record(
                "order.fill",
                {
                    **fill.to_payload(),
                    "status": str(order.status) if order.status is not None else None,
                    "candidate_id": order.candidate_id,
                },
            )
            # AFTER the record, one movement at a time: a movement is in the ledger
            # iff its row is durable, so a restart rebuilds exactly this state. The
            # record raising (a dead audit log) halts trading before this line.
            if self._ledger is not None:
                self._ledger.note(LedgerFill.from_applied(fill))
        logger.info(
            "booked %d leg fill(s) for order %s (%s)",
            len(applied), order.broker_order_id, order.external_identifier,
        )
        return applied

    def _notify(self, kind: str, detail: dict[str, Any]) -> None:
        logger.warning("%s: %s", kind, detail)
        self._alert(kind, detail)
