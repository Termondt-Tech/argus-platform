"""The duplicate-submission guard (spec §9) — the ONLY backstop that exists.

Chunk 1 assumed tastytrade enforced uniqueness on `external_identifier`. It does
not. Measured against the real sandbox on 2026-07-29: the same identifier was
accepted a second time both while the first order was still WORKING and after it
had been cancelled — two live orders, one identifier, different broker ids. So
spec §9's "duplicate/runaway order submission" risk has no broker-side protection
whatsoever. This module is it.

**Where the answer comes from matters.** The check asks the BROKER
(`get_live_orders()`), not local state, because the scenario it exists for is the
one where local state is exactly what was lost — a crash mid-submission, a
restart, a session that does not know what its predecessor sent. `Order`'s
deterministic `external_identifier` (`build.external_identifier`) is what makes
that possible: the same candidate rebuilds to the same key, so the broker can be
asked "have I already sent this?" and answer without Argus remembering anything.

**Not wired in here, on purpose.** Chunk 3/5 own calling this. The rule for
whoever does:

    Call it before every BRAND-NEW trade idea's first submission.
    Do NOT call it before a cancel/replace inside an attempt already in flight.

The second half is not an optimisation — it is correctness. A price-walking
replace re-sends the SAME `external_identifier` against an order that is, by
definition, still working; running the guard there would find that order, call it
a conflict, and block the loop from ever repricing its own order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from argus.audit.log import AuditLog
from argus.execution.broker import OrderExecutionPort
from argus.execution.models import (
    DISPOSITION_UNMAPPABLE,
    LiveOrders,
    Order,
    OrderDirection,
    OrderDisposition,
    UnmappableOrder,
    classify_order,
    parse_external_identifier,
    sibling_identifier,
)

logger = logging.getLogger("argus.execution.guard")

# Why a submission was refused. `WORKING` and `FILLED` are the two blocking
# dispositions; kept as distinct reasons because they mean very different things
# to whoever reads the audit record (one is "wait", the other is "you are in").
BLOCKED_WORKING = "conflicting_working_order"
BLOCKED_FILLED = "conflicting_filled_order"
#: The broker holds an order under this identifier that Argus cannot read at all.
#: A third reason rather than folding into `BLOCKED_WORKING`, because the operator
#: action is different: the other two say "wait" or "you are in", this one says
#: "go look at the account by hand, Argus does not know which".
BLOCKED_UNMAPPABLE = "conflicting_unmappable_order"
#: The OPENED quantity is an input to the CLOSE quantity rule (2026-09-24), and an
#: order Argus cannot read under the sibling OPEN identifier is an unknown input.
#: The same operator action as `BLOCKED_UNMAPPABLE` — go look by hand — but it
#: names the OTHER side of the position, which is where the look must go.
BLOCKED_OPENED_UNREADABLE = "opened_quantity_unreadable"
#: A leg of an order under EITHER identifier reported no `remaining_quantity`, so
#: a filled count cannot be computed. Fails closed: "cannot tell" must never read
#: as zero, the exact confusion `OrderLeg.remaining_quantity` exists to prevent.
BLOCKED_CLOSE_UNREADABLE = "close_quantity_unreadable"
#: The quantity rule's inequality failed: `closed_filled + q > opened_filled` on
#: some leg. Two different sentences share this reason — the LEDGER LAG (q
#: overstates held because a closing fill has not booked) and the DOUBLE CLOSE
#: (everything opened is already closed) — and the verdict's `quantity` block
#: carries the arithmetic that tells them apart, which is why it rides refusals
#: as well as permissions.
BLOCKED_CLOSE_EXCEEDED = "close_quantity_exceeded"


@dataclass(frozen=True)
class ConflictingOrder:
    broker_order_id: int | None
    status: str | None
    disposition: str
    #: Why the order could not be mapped, when it could not be. None for the
    #: normal case. Carried because a blocked submission whose only explanation is
    #: `status=None` tells an operator nothing at 10:05 on a Tuesday, and the raw
    #: status string in here is usually a one-line fix to `OrderStatus`.
    unmappable_reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "broker_order_id": self.broker_order_id,
            "status": self.status,
            "disposition": self.disposition,
            "unmappable_reason": self.unmappable_reason,
        }


@dataclass(frozen=True)
class CloseQuantity:
    """The broker's own per-leg arithmetic behind a CLOSE quantity verdict. (2026-09-24)

    `closed_filled + q <= opened_filled` per leg symbol, both sums taken from the
    BROKER's per-leg fill quantities off the one `get_live_orders()` fetch the
    guard already makes — never from the ledger, which lags the broker by the
    measured 4-5s and would make `q` understated on one side and the closed sum
    understated on the other. `allowance` is the min over the union of the two
    sides' symbols, because the over-close question is PER-LEG: a close of `q`
    closes `q` of each leg, so it over-closes exactly when `q > held[symbol]` for
    some leg, and the binding leg is the one that would invert.

    For every uniform fill `allowance` equals the spread count — the decision
    record's `sum(filled close quantity) + q <= opened quantity` verbatim. For an
    uneven one it is the conservative bound. A symbol present in the close set
    but not the open set has `opened_filled = 0` and refuses by arithmetic; a
    union with nothing in it at all has `allowance = 0`, because "no orders under
    the open identifier" is the absence the record names as not-a-verdict.

    Rides the verdict on refusals as well as permissions (§4.6 of the design):
    the arithmetic IS the operator's answer.
    """

    asked: int
    opened_filled: dict[str, int]
    closed_filled: dict[str, int]
    allowance: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "asked": self.asked,
            "opened_filled": dict(self.opened_filled),
            "closed_filled": dict(self.closed_filled),
            "allowance": self.allowance,
        }


@dataclass(frozen=True)
class ConflictVerdict:
    """The guard's answer.

    A verdict object rather than a bare bool because the REASON has to reach the
    audit log — "blocked" with no record of which broker order blocked it, and in
    what state, is not something anyone can act on at 10:05 on a Tuesday.
    Callers gate on `.blocked`; there is deliberately no `__bool__`, so nothing
    can accidentally test the object's truthiness and get the wrong answer.

    `quantity` is the CLOSE-rule arithmetic when the rule RAN — a CLOSE asked
    with an intended quantity, on a guard built with the rule, whose binary
    verdict was `conflicting_filled_order` — and None otherwise, which is every
    OPEN ask, every binary close ask and every ask the binary rule answered some
    other way, all of which keep today's rule byte-for-byte.
    """

    external_identifier: str
    blocked: bool
    reason: str | None = None
    conflicts: tuple[ConflictingOrder, ...] = ()
    quantity: CloseQuantity | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "external_identifier": self.external_identifier,
            "blocked": self.blocked,
            "reason": self.reason,
            "conflicts": [c.to_payload() for c in self.conflicts],
            "quantity": None if self.quantity is None else self.quantity.to_payload(),
        }


class DuplicateOrderGuard:
    def __init__(
        self,
        execution: OrderExecutionPort,
        *,
        audit: AuditLog | None = None,
        close_quantity_rule: bool = False,
    ) -> None:
        """`close_quantity_rule` arms the CLOSE quantity rule (2026-09-24).

        Off — the default — every ask is the binary rule byte-for-byte, and a
        `quantity` passed to `has_conflicting_order` is validated and otherwise
        IGNORED. The default is the conservative one on purpose: the rule only ever
        CLEARS a `conflicting_filled_order` refusal, so "off" can only refuse more,
        never permit more. The trading session arms it from
        `watchdog.stub_close_enabled` (`pipeline.build_execution_stack`), so that one
        switch rolls back BOTH halves of the stub-close design — the watchdog's
        continuation AND the guard permission it depends on, which the flatten
        override and every exit retry would otherwise also reach.
        """
        self._execution = execution
        self._audit = audit
        self._close_quantity_rule = close_quantity_rule

    async def has_conflicting_order(
        self, external_identifier: str, *, quantity: int | None = None
    ) -> ConflictVerdict:
        """Would submitting `external_identifier` now duplicate something?

        Applies `classify_order` — the shared three-way classification, the same
        one restart-time order reconciliation uses — to every broker order
        carrying this identifier:

            WORKING  -> BLOCK. It may still fill; a second order would mean two.
            FILLED   -> BLOCK. A real position exists (in whole or in part);
                        submitting again doubles it.
            DEAD     -> ALLOW. Terminal with nothing filled. This is the
                        crash-recovery and re-entry case, and blocking it would be
                        a defect, not a safety feature — re-entering a spread
                        after a cancelled attempt is exactly the behaviour the
                        deterministic identifier was designed to permit.

        The classification is NOT reimplemented here, and must not be: a local
        `status.is_terminal` check would read `FILLED` as safe, which is the one
        way to get this wrong that costs money.

        **An order that cannot be classified at all also BLOCKS** (2026-08-20).
        `get_live_orders()` used to silently drop an order it could not map, so an
        unmappable WORKING order under this identifier read here as no order at
        all and a second one went out — in the one module that is the only
        duplicate protection in the system. There is no safe reading of "the
        broker holds something under your key and Argus cannot say what": it is
        `WORKING`-or-worse for every purpose this guard has, so it refuses. The
        cost is a missed trade, visible in the audit log and named in an alert;
        the alternative cost is two spreads.

        **A CLOSE asked with `quantity` gets the quantity rule (2026-09-24)** —
        when the guard was built with `close_quantity_rule=True`. `quantity` is
        the contracts the submission would carry, and it participates ONLY on the
        CLOSE side: the guard parses the direction off the identifier and ignores
        `quantity` entirely for OPEN asks, because over-opening is the size rails'
        question, not the guard's.

        **The rule only ever CLEARS a refusal; it never adds one.** It is reached
        only when the binary rule's verdict is `conflicting_filled_order` — a
        FILLED (or partially-filled terminal) order under the close identifier,
        which is exactly the stub state. There it permits iff nothing is WORKING
        or unmappable under the close identifier (both already outrank FILLED and
        never reach the rule), nothing is unmappable under the sibling OPEN
        identifier, every leg under both reports a sane `remaining_quantity`, and
        `closed_filled + q <= opened_filled` per leg; otherwise the refusal stays,
        re-reasoned with the arithmetic. An ask the binary rule ALLOWS — a first
        close, a retry after a DEAD close — is allowed exactly as before: the
        ordinary stop, backstop and flatten must never be refused by an input the
        binary rule did not need (an opener Argus cannot read, an order window
        that no longer shows the opener). See `CloseQuantity` and
        `docs/designs/2026-09-24-quantity-aware-guard-for-close-identifiers.md`.
        A CLOSE asked WITHOUT `q`, any identifier that does not parse, and every
        ask on a guard built without the rule keep the binary rule byte-for-byte
        — fail closed means the old behaviour, not a new guess.

        `quantity <= 0` raises `ValueError`, on the empty-identifier precedent: a
        close of nothing is a caller bug, and the guard's job is to fail loud at
        the door rather than to reason about it.

        **Raises rather than reporting "no conflict" if the broker cannot be
        reached.** An unanswerable question must never read as permission, and a
        caller that swallows the exception and submits anyway has removed the only
        duplicate protection in the system. Fail closed: no answer, no order.
        """
        if not external_identifier:
            # An empty key would match every broker order that carries none —
            # anything placed by hand — and read as a conflict for reasons that
            # have nothing to do with this candidate. A caller with no identifier
            # has a bug upstream, not a duplicate.
            raise ValueError("cannot check for duplicates without an external_identifier")
        if quantity is not None and quantity <= 0:
            raise ValueError(
                f"a close quantity must be a positive contract count, not {quantity!r}"
            )

        live = await self._execution.get_live_orders()
        matches = [o for o in live.orders if o.external_identifier == external_identifier]
        # Read explicitly, not incidentally. `LiveOrders` has no `__iter__` for
        # exactly this reason: the previous shape let this line look complete
        # while the unmappable orders were never in the list at all.
        unreadable = live.unmappable_by_identifier(external_identifier)

        conflicts: list[ConflictingOrder] = []
        reason: str | None = None
        for order in matches:
            disposition = classify_order(order)
            if disposition is OrderDisposition.DEAD:
                continue
            conflicts.append(_describe(order, disposition))
            # A working order is the more actionable of the two — it can still be
            # cancelled — so it wins the headline reason when both are present.
            if disposition is OrderDisposition.WORKING:
                reason = BLOCKED_WORKING
            elif reason is None:
                reason = BLOCKED_FILLED

        for order in unreadable:
            conflicts.append(_describe_unmappable(order))
        if unreadable:
            # Outranks both other reasons. An operator who sees
            # `conflicting_working_order` will go look for a working order and
            # find something they cannot interpret; naming it up front is the
            # difference between a two-minute answer and a twenty-minute one.
            reason = BLOCKED_UNMAPPABLE

        # -- the CLOSE quantity rule (2026-09-24) --------------------------------
        #
        # Reached ONLY when the binary verdict is `conflicting_filled_order` for a
        # CLOSE identifier asked WITH a quantity, on a guard built with the rule.
        # Keyed on the REASON, which already encodes the precedence: a WORKING
        # order under the asked identifier (it can still fill; a second order
        # would mean two) and an unmappable one both outrank FILLED, so neither
        # can reach this block — and an ask the binary rule ALLOWS never reaches
        # it either, so the rule can only ever turn a refusal into a permission,
        # never a permission into a refusal. The verdict is `blocked` iff
        # `conflicts` is non-empty and the arithmetic did not clear them.
        quantity_block: CloseQuantity | None = None
        cleared = False
        rule = self._quantity_rule_inputs(external_identifier, quantity, reason)
        if rule is not None:
            sibling, asked = rule
            arithmetic, quantity_reason, open_conflicts = _close_quantity(
                live, sibling, asked, matches=matches
            )
            conflicts.extend(open_conflicts)
            quantity_block = arithmetic
            if quantity_reason is not None:
                # Still refused — now for the reason the arithmetic gives, which
                # is the operator's answer ("closed 1 + q 2 > opened 2" is the
                # ledger lag; "closed 2 + q 1 > opened 2" is the double close).
                reason = quantity_reason
            else:
                # The broker's own fills prove `q` fits what is still held: the
                # FILLED refusal, cleared by arithmetic. The conflicts stay on the
                # verdict — they are what was cleared, and the Tier 1 record below
                # is the post-mortem's proof of why the send went out.
                cleared = True
                reason = None

        verdict = ConflictVerdict(
            external_identifier=external_identifier,
            blocked=bool(conflicts) and not cleared,
            reason=reason,
            conflicts=tuple(conflicts),
            quantity=quantity_block,
        )
        await self._record(
            verdict, checked=len(matches) + len(unreadable), cleared=cleared
        )
        return verdict

    def _quantity_rule_inputs(
        self, external_identifier: str, quantity: int | None, reason: str | None
    ) -> tuple[str, int] | None:
        """`(sibling OPEN identifier, q)` when the quantity rule applies, else None.

        ALL of: the guard was built with the rule; a quantity was asked; the
        binary verdict is `conflicting_filled_order` (the ONLY refusal the rule
        may clear — see the call site); and the identifier parses as one of
        Argus's CLOSE identifiers, whose sibling is then derived by the grammar
        (`sibling_identifier`), never by string surgery here. Anything else keeps
        the binary rule.
        """
        if not self._close_quantity_rule or quantity is None:
            return None
        if reason != BLOCKED_FILLED:
            return None
        parsed = parse_external_identifier(external_identifier)
        if parsed is None or parsed[0] is not OrderDirection.CLOSE:
            return None
        sibling = sibling_identifier(external_identifier, OrderDirection.OPEN)
        if sibling is None:
            return None
        return sibling, quantity

    async def _record(
        self, verdict: ConflictVerdict, *, checked: int, cleared: bool = False
    ) -> None:
        if self._audit is None:
            return
        if cleared:
            # Tier 1 (2026-09-24): a quantity-permitted close is a decision to
            # trade — the inverse of `order.duplicate_blocked` and equally a
            # post-mortem's business. "Why did Argus send a second close under a
            # key that already had a filled one?" must be answered from the Tier 1
            # trail with the arithmetic attached, not reconstructed from Tier 2
            # telemetry that a full queue may have dropped. Volume is bounded by
            # stub frequency, which is rare by construction.
            logger.warning(
                "permitting close of %s by quantity: %s",
                verdict.external_identifier,
                verdict.quantity.to_payload() if verdict.quantity is not None else "-",
            )
            await self._audit.record(
                "order.duplicate_close_permitted", verdict.to_payload()
            )
        elif verdict.blocked:
            # Tier 1: a refused submission is a decision not to trade, and the
            # reason for it is exactly what a post-mortem needs.
            logger.warning(
                "blocking duplicate submission of %s (%s)",
                verdict.external_identifier, verdict.reason,
            )
            await self._audit.record("order.duplicate_blocked", verdict.to_payload())
        else:
            self._audit.telemetry(
                "order.duplicate_check",
                {**verdict.to_payload(), "matching_orders": checked},
            )


def _describe(order: Order, disposition: OrderDisposition) -> ConflictingOrder:
    return ConflictingOrder(
        broker_order_id=order.broker_order_id,
        status=str(order.status) if order.status is not None else None,
        disposition=str(disposition),
    )


def _describe_unmappable(order: UnmappableOrder) -> ConflictingOrder:
    """`status` is the broker's RAW string, not an `OrderStatus`.

    That is the whole content of the record: the enum could not take this value,
    so passing the value through verbatim is what makes the alert actionable.
    """
    return ConflictingOrder(
        broker_order_id=order.broker_order_id,
        status=order.raw_status,
        disposition=DISPOSITION_UNMAPPABLE,
        unmappable_reason=order.reason,
    )


def _close_quantity(
    live: LiveOrders,
    sibling: str,
    quantity: int,
    *,
    matches: list[Order],
) -> tuple[CloseQuantity | None, str | None, list[ConflictingOrder]]:
    """The CLOSE quantity rule's arithmetic, off the ONE `LiveOrders` answer.

    `sibling` is the asked CLOSE identifier's OPEN sibling, derived by the
    grammar at the call site; `matches` are the orders under the CLOSE
    identifier itself, already filtered by the binary rule.

    Returns `(arithmetic, reason, open_side_conflicts)`:

    - `reason is None` — every input was readable and `q <= allowance`. The
      caller clears the FILLED conflicts this computed against.
    - `reason` set — the typed refusal (`BLOCKED_OPENED_UNREADABLE`,
      `BLOCKED_CLOSE_UNREADABLE`, `BLOCKED_CLOSE_EXCEEDED`) and, where the
      arithmetic could be completed, the block that names the numbers.
    - `open_side_conflicts` — descriptions of unmappable OPEN-side orders, for
      the verdict's `conflicts` list: the operator's look must go there.

    **No new broker round trip.** Both sides of the inequality are filtered from
    the same `get_live_orders()` snapshot the binary rule already read, so they
    cannot disagree with each other about the state of either identifier. The
    replace chain sums correctly on the one live measurement that exists
    (2026-09-01: original filled 1, replacement carried the REMAINDER and filled
    1, position held 2 = the sum), and `candidate_id` embeds the trading date, so
    yesterday's identifiers never collide with today's sums inside the rolling
    24h order window.

    DEAD orders are NOT skipped from either sum, deliberately: a
    cancel-after-partial is DEAD-looking with fills, and skipping it would
    understate `closed_filled` — the exact error that over-closes. This includes
    the fill counts of orders the binary rule already ignores (its DEAD filter):
    the quantity rule reads the same fields `classify_order` already trusts and
    does not throw the number away.

    **The rule leans on `remaining_quantity` in the PERMISSIVE direction, which
    the binary rule never did.** `classify_order` only ever used a fill count to
    BLOCK (any fill, or "cannot tell", is FILLED), so a count reported too HIGH
    could only cost a missed trade. Here an opener's count reported too high, or a
    closer's too low, would ADD capacity. The standing evidence is that the ledger
    (`execution.store`) books every fill from this same field, diffed per order,
    and has reconciled clean against the broker through cert's many replaced walks
    — but the close-side replace chain has never been observed in live, so the
    per-leg count is bounded as well as required: a count outside
    `0 <= filled <= leg.quantity` is not a count and refuses as unreadable
    (`BLOCKED_CLOSE_UNREADABLE`), rather than being summed into an allowance.
    """
    open_unreadable = live.unmappable_by_identifier(sibling)
    if open_unreadable:
        return (
            None,
            BLOCKED_OPENED_UNREADABLE,
            [_describe_unmappable(order) for order in open_unreadable],
        )
    open_matches = [o for o in live.orders if o.external_identifier == sibling]

    opened: dict[str, int] = {}
    closed: dict[str, int] = {}
    for orders, sums in ((open_matches, opened), (matches, closed)):
        for order in orders:
            for leg in order.legs:
                filled = leg.filled_quantity
                if filled is None or not 0 <= filled <= leg.quantity:
                    # `None` means "not reported", NOT zero — conflating the two
                    # is the partial-fill bug `OrderLeg.remaining_quantity`
                    # exists to prevent, and here it would invent capacity. A
                    # count outside the leg's own size is a broker answer that
                    # does not describe a fill at all; summing it could invent
                    # capacity just the same.
                    return None, BLOCKED_CLOSE_UNREADABLE, []
                sums[leg.symbol] = sums.get(leg.symbol, 0) + filled

    symbols = set(opened) | set(closed)
    # The union EMPTY (nothing under either identifier) allowance is 0, not +inf:
    # "no orders under the open identifier" is the absence the decision record
    # names as not-a-verdict, and `min()` over nothing would permit any `q`.
    allowance = min(
        (opened.get(symbol, 0) - closed.get(symbol, 0) for symbol in symbols),
        default=0,
    )
    arithmetic = CloseQuantity(
        asked=quantity,
        opened_filled={s: opened[s] for s in sorted(opened)},
        closed_filled={s: closed[s] for s in sorted(closed)},
        allowance=allowance,
    )
    if quantity > allowance:
        return arithmetic, BLOCKED_CLOSE_EXCEEDED, []
    return arithmetic, None, []
