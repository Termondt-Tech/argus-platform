"""A cancel that was requested is not a cancel that landed. (P5, 2026-08-29)

Every walk that ends leaves at most one order behind. This module holds those
orders until the broker is SEEN to say they are terminal, and while one is still
positively confirmed WORKING it re-requests the cancel of an **opening** order on a
backing-off schedule and pages for either kind on a repeating bound.

**This is a broken safety rail, not a diagnosability problem.** `CLAUDE.md`'s first
non-negotiable says the kill switch halts new entries "including an entry order
already working at the exchange". That guarantee is implemented by
`HaltCheckFn` -> `_abort` -> `_request_cancel`, and `_request_cancel` swallows every
exception. On 2026-08-28 the DELETE for order 1424563 came back a 502, the walk
finished (correctly, by design), and nothing in the process ever asked about that
order again — it was still working two hours later when a restart's reconciliation
halted on it. So on the one rail called non-negotiable, "cancelled" was an
intention rather than an outcome.

**It triggers on the STATE, never on the error.** A cancel that RAISED and a cancel
that was ACCEPTED and stuck at `Cancel Requested` are the same object from the
outside: a live order nobody is walking. Hooking the audited layer's `except` branch
would have seen only the first, and would have missed the walk that abandoned an
order it could not even see (`UNRESOLVED: order_not_visible_at_broker` requests no
cancel at all). Registration therefore happens at `_finish`, where the rule is
simply *the last observation of this order was not terminal*.

**Nothing here decides to cancel anything.** `_request_cancel` already decided,
`order.cancel_attempt` already recorded it at Tier 1, and `walk.finished` already
reported the outcome that depended on it. This only asks whether the decision took
effect — which is why it needs no licence of the kind a second CLOSING order needs
(`DuplicateOrderGuard` re-asking the broker). A cancel is monotone in the direction
of less exposure: it cannot create a position and cannot increase size.

Five rules hold this together, and each is a way to be wrong:

**1. Direction comes from what Argus MINTED, never from the broker's echo.**
`Order.direction` as it arrives from `get_live_orders()` is `infer_direction`'s
reading of the leg actions the broker sent back, and tastytrade labels each leg
relative to THE ACCOUNT'S POSITION, not to Argus's intent — selling to close a
strike the account is net flat on comes back `Sell to Open`, so a CLOSING order is
reported OPEN (`docs/decisions/2026-08-25-broker-relabels-leg-actions.md`, and the
open decision in `2026-08-25-relabelled-closing-legs-open.md`). On a chained book
— routine at `signal.width = 5.0` — that is not a tail case. So the minted
direction is the primary source, `parse_external_identifier` (pure, local, reading
a string Argus minted) is the cross-check, and a disagreement refuses to cancel and
pages. The field is called `minted_direction` so that ANY `.direction` attribute
read in this module is visibly wrong; a test asserts there are none.

  Note the deliberate difference from `working_orders.py`, which *does* read
  `order.direction`. There, a mislabelled closing order inflates a count that
  blocks an entry — the fail-closed direction. **Here the same mistake cancels a
  live close.** Same field, opposite consequence: a later "consistency" pass that
  unified the two would be a regression.

**2. An OPENING order gets the automatic cancel; a CLOSING one never does.**
Inherited verbatim from `loop.HaltCheckFn`, which explains why the kill switch is
wired for entry walks only: a kill press must stop the system taking on NEW risk,
and it must not strand an open position by cancelling the order that is closing it.
A cancel is monotone *except* on the exit path, and that single exception keeps this
module entirely out of the exit-re-attempt open decision's territory.

**3. Absence is not a verdict, in either direction.** An answer that does not name
this order is a read gap, not a state: it does not resolve a residual and it does
not authorize a cancel. Same rule as `_await_settled`'s ("present-and-working, and
absent from an answer of any size, both refuse" — `any()` over an empty list is
`False`), pointed the other way. The page distinguishes still-working from
not-visible because the operator's next move differs: chase a live order, or chase
a read.

**4. The registry is IN-PROCESS and is never rehydrated.** A registry rebuilt from
the audit log would let a restart pick up the previous process's orphan and cancel
it — an automated action on an order whose provenance is by construction unclear,
which is exactly what reconciliation exists to adjudicate and what P3's record
forbids being smuggled in here. **P5 prevents 2026-08-28's state from arising; it
does not resolve it after a restart.**

**5. Bounded on RATE, not on COUNT** — the opposite of P0's `_EXIT_SEND_ATTEMPTS`,
and for a stated reason. That budget exists because each exit retry costs a
SUBMISSION; a cancel is not a submission, so only the volume argument survives and a
rate bound answers it completely. A hard count is also wrong on the facts of the
incident it is for: cert was 502-ing from 10:00 to 10:13 and the order sat working
until 12:15, so a budget of three would have been spent inside the outage — on the
one stretch where nothing could have worked — and never retried after recovery. A
rate bound self-heals.

**Two things it does NOT gate on.** Not the kill switch: cancelling an abandoned
opening order while halted is precisely what the operator asked for, and a recheck
disabled by a halt would be disabled exactly when it matters most. Not
`dry_run_submissions`: a dry run routes nothing, so there is never a residual to
register and the registry is empty by construction — a test, not a branch.

**Staged rollout.** `watchdog.unmanaged_order_cancel` decides whether the outbound
cancel is actually sent. `false` is `would_cancel` mode: the same registry, the same
decision — direction sourcing, the OPEN/CLOSE split, the identifier cross-check, the
working/terminal/absent reading — computed and recorded at Tier 2 with no broker
call, which is `shadow.would_submit`'s shape. The outbound call sits behind that ONE
flag in ONE place, asserted structurally, so the two modes cannot drift into
scattered conditionals.

→ docs/decisions/2026-08-29-mid-session-cancel-recheck.md
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from argus.alerts import AlertFn, log_alert
from argus.audit.log import AuditLog
from argus.config import Config
from argus.execution.escalation import _Escalation
from argus.execution.models import (
    LiveOrders,
    Order,
    OrderDirection,
    OrderDisposition,
    classify_order,
    parse_external_identifier,
)

logger = logging.getLogger("argus.execution.residual_orders")

#: Ask the broker to cancel one order. In production this is
#: `AuditedOrderExecution.cancel`, so `order.cancel_attempt` / `order.cancel_failed`
#: / `order.cancel_requested` are written by the existing layer at the existing
#: tier — this module adds no cancel record types of its own.
CancelFn = Callable[[int], Awaitable[None]]
#: Monotonic seconds, the same clock the walk and the watchdog use.
ClockFn = Callable[[], float]

#: Gap between re-cancel attempts, by attempt number, before the floor takes over.
#: Geometric, and capped at `watchdog.unmanaged_order_max_s` — so at the shipped
#: 900s a four-hour stuck order costs ~30 cancel requests over a whole session,
#: against a documented ceiling of 40 per SECOND and `ratelimit.py`'s process-wide
#: governor underneath that.
#:
#: A constant rather than config, deliberately: the burst's SHAPE is code and the
#: numbers an operator tunes are the alert cadence and the floor, which is the same
#: line `entry._GUARD_ATTEMPTS` and `watchdog._EXIT_SEND_ATTEMPTS` draw.
_BACKOFF_S = (15.0, 30.0, 60.0, 120.0, 240.0, 480.0)

#: `_Escalation` tags. Each is a distinct EPISODE per residual, so a state that
#: changes shape re-pages immediately instead of inheriting a spent bound.
#: Confirmed working past the settle allowance, opening — the one that acts.
_STILL_WORKING = "still_working"
#: Confirmed working, CLOSING. Paged and never cancelled (rule 2).
_CLOSING = "closing_still_working"
#: Confirmed working, but the minted direction and the identifier disagree. Should
#: be unreachable — both derive from one construction — which is what makes it a
#: good tripwire for a future change that breaks the invariant.
_DIRECTION_UNTRUSTED = "direction_disagreement"
#: The broker's answer did not name this order. A read gap, not a state (rule 3).
_NOT_VISIBLE = "not_visible"
#: Present under this identifier and unreadable. Same as absent: cannot conclude.
_UNREADABLE = "unreadable"
#: More than one order came back under this broker id. Fail closed, as
#: `_await_settled` does rather than picking one.
_AMBIGUOUS = "ambiguous"


class ResidualSighting(StrEnum):
    """What one broker answer said about one registered order.

    Six values rather than working/not-working, because three of them are read gaps
    and a read gap is not a state. Only `WORKING` may be acted on, and only `DEAD`
    and `FILLED` may resolve a residual; the other three keep watching.
    """

    #: Named, terminal, nothing filled. The cancel landed (or something else killed
    #: it). Resolved.
    DEAD = "dead"
    #: Named, and it left a position. Resolved here; the fill books through
    #: `FillConsumer` exactly as it does today and nothing in this module touches
    #: `positions`.
    FILLED = "filled"
    #: Named, and the broker may still act on it. The only actionable sighting.
    WORKING = "working"
    #: Not in the answer at all.
    ABSENT = "absent"
    #: Present under this identifier or this id, and unmappable.
    UNREADABLE = "unreadable"
    #: Two orders under this broker id, or one whose `external_identifier` is not
    #: the one that was registered.
    AMBIGUOUS = "ambiguous"

    @property
    def resolves(self) -> bool:
        return self in (ResidualSighting.DEAD, ResidualSighting.FILLED)


def read_sighting(
    live: LiveOrders, *, broker_order_id: int, external_identifier: str
) -> tuple[ResidualSighting, Order | None]:
    """What `live` says about ONE registered order. Pure; the whole state table.

    A free function on `LiveOrders` for the same reason
    `open_working_spread_equivalents` is one: the reading is the part worth testing
    exhaustively, and it needs no registry, no clock and no broker.

    The two tripwires are both here. **An unreadable order under this identifier or
    this id reads as UNREADABLE**, never as absent and never as resolved — an order
    nobody could read once passed for one confirmed cancelled (A3b). **Two orders
    under one broker id, or one carrying a different identifier, read as AMBIGUOUS**
    — the broker does not enforce identifier uniqueness (measured, chunk 1), so
    picking one would be a guess at exactly the moment a guess sends a cancel.
    """
    unreadable = [
        u
        for u in live.unmappable
        if u.broker_order_id == broker_order_id
        or u.external_identifier == external_identifier
    ]
    if unreadable:
        return ResidualSighting.UNREADABLE, None

    matches = [o for o in live.orders if o.broker_order_id == broker_order_id]
    if not matches:
        return ResidualSighting.ABSENT, None
    if len(matches) > 1:
        return ResidualSighting.AMBIGUOUS, None

    order = matches[0]
    if order.external_identifier != external_identifier:
        # The id matched and the key did not. Either the broker reused an id or this
        # registration is about a different order; both are "cannot say", and
        # cannot-say never authorizes a cancel.
        return ResidualSighting.AMBIGUOUS, order

    disposition = classify_order(order)
    if disposition is OrderDisposition.FILLED:
        return ResidualSighting.FILLED, order
    if disposition is OrderDisposition.DEAD:
        return ResidualSighting.DEAD, order
    return ResidualSighting.WORKING, order


def cancel_licence(
    *, minted_direction: OrderDirection, external_identifier: str
) -> tuple[bool, str, str | None]:
    """May this residual's cancel be re-requested? Pure. Returns (yes, tag, refusal).

    Rule 1 and rule 2 of the module docstring, in one place and reading nothing that
    came off the wire. `minted_direction` is what Argus passed to `build_order`;
    `parse_external_identifier` re-derives the same fact from a string Argus minted.
    They agree by construction, so a disagreement is a defect somewhere upstream —
    and the answer to a defect near a cancel is to page, not to guess.
    """
    parsed = parse_external_identifier(external_identifier)
    if parsed is None or parsed[0] is not minted_direction:
        stated = "unparseable" if parsed is None else str(parsed[0])
        return (
            False,
            _DIRECTION_UNTRUSTED,
            f"minted direction {minted_direction} disagrees with the identifier "
            f"({stated}) — refusing to cancel an order whose direction is in doubt",
        )
    if minted_direction is OrderDirection.CLOSE:
        return (
            False,
            _CLOSING,
            "this is a CLOSING order; cancelling it would strand an open position",
        )
    return True, _STILL_WORKING, None


@dataclass
class ResidualOrder:
    """One order a finished walk left behind, and what has been done about it.

    Mutable and in-process, deliberately (rule 4). Keyed on `broker_order_id`, which
    is the thing the broker can be asked about — never on `external_identifier`,
    which a walk reprices under so several abandoned rungs share one.
    """

    broker_order_id: int
    external_identifier: str
    #: THE direction, taken from `state.sent.direction` at registration — what Argus
    #: passed to `build_order`, before anything round-tripped through a broker.
    minted_direction: OrderDirection
    candidate_id: str | None
    #: The walk outcome and whether it managed to ask for a cancel. For the record
    #: and the page; NEVER for the decision, which reads the broker's answer alone.
    walk_outcome: str
    cancel_requested: bool
    #: Monotonic reading taken by the REGISTRY, not by the caller, so the settle
    #: allowance is measured against the same clock that spends it.
    registered_at: float
    attempts: int = 0
    #: Earliest clock reading at which another cancel may be sent. None until the
    #: first one is.
    next_attempt_at: float | None = None
    last_sighting: str | None = None
    last_status: str | None = None
    #: The order's limit as Argus sent it, when the registrant knows it (2026-09-25).
    #: For the page and `/status` only — nothing decides on it.
    limit_price: float | None = None
    #: True for an order that is SUPPOSED to be working: the market-crossing limit a
    #: `crossed` exit leaves resting (2026-09-25). Its page waits
    #: `unmanaged_order_max_s` rather than the cancel-settle allowance, because a
    #: cross still working three seconds later is doing its job, not abandoned. It is
    #: always a CLOSE, so `cancel_licence` never lets it be cancelled either way.
    working_expected: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            # TOP level, both of them. `SlackNotifier` scopes its 900s throttle on
            # `(kind, scope)` and reads the scope from here (2026-08-21, B1) —
            # nested inside another object, two residuals' pages would coalesce and
            # the second would arrive as a bare `(+1 suppressed)` up to 15 minutes
            # late.
            "candidate_id": self.candidate_id,
            "external_identifier": self.external_identifier,
            "broker_order_id": self.broker_order_id,
            "minted_direction": str(self.minted_direction),
            "walk_outcome": self.walk_outcome,
            "cancel_requested": self.cancel_requested,
            "attempts": self.attempts,
            "last_sighting": self.last_sighting,
            "last_status": self.last_status,
            "limit_price": self.limit_price,
            "working_expected": self.working_expected,
        }


@dataclass(frozen=True)
class OutstandingResidual:
    """One residual order as `/status` needs it: frozen, clock-resolved, mode-stamped.

    Deliberately NOT `ResidualOrder`, which is mutable registry state keyed on a
    monotonic reading only this module holds. Same relationship `SpentExitAttempt` has
    to the watchdog's internal `_SpentAttempt`, and for the same two reasons: a
    renderer must not hold a handle into state `step()` mutates, and a duration must be
    computed against the clock that produced its origin.

    Every field is a plain type, so the `/status` seam that copies it is a
    field-for-field translation with no arithmetic and no formatting — which is what
    lets it be swept structurally (`tests/test_session_status_seams.py`).
    """

    broker_order_id: int
    external_identifier: str
    candidate_id: str | None
    #: `open` / `close`, as MINTED by Argus. Never the broker's relabelled leg action.
    #: Also the eligibility test for the automatic re-cancel: opening orders get it,
    #: closing ones never do (cancelling a close strands a position).
    minted_direction: str
    walk_outcome: str
    cancel_requested: bool
    attempts: int
    #: Seconds since registration, on the registry's own clock.
    working_for_s: float
    last_status: str | None
    #: The last `ResidualSighting`, as a string. Three of its six values are READ GAPS
    #: (`absent` / `unreadable` / `ambiguous`) and mean the broker will neither confirm
    #: this order dead nor license a cancel — the state `/status` renders red.
    last_sighting: str | None
    #: `watchdog.unmanaged_order_cancel`. Identical on every row of one render: it is a
    #: property of the recheck, not of a residual. False is `would_cancel` — the
    #: shipped mode — where the decision is recorded and no cancel is sent.
    cancel_enabled: bool
    #: See `ResidualOrder.limit_price` / `.working_expected` (2026-09-25).
    limit_price: float | None = None
    working_expected: bool = False


class ResidualOrderRecheck:
    """The registry and the recheck. Driven by the watchdog's existing poll.

    Constructed once per trading side and shared by the entry walk, the exit walk
    and the watchdog: the walks register, the watchdog steps. It owns no task and
    opens no connection — `step` is handed the `LiveOrders` answer
    `_refresh_working_orders` has already fetched, so this costs zero additional
    broker requests and the answer is in hand at exactly the moment it is wanted.

    The policy lives here rather than in `watchdog.py`, which is a
    position-monitoring module whose docstring earns its keep on that framing. That
    also makes the whole of it unit-testable with no watchdog at all.
    """

    def __init__(
        self,
        *,
        audit: AuditLog,
        config: Config,
        cancel: CancelFn,
        alert: AlertFn | None = None,
        clock: ClockFn | None = None,
    ) -> None:
        self._audit = audit
        self._cancel = cancel
        # Defaults to `log_alert`, NOT to silence — the same posture as the walk and
        # the fill consumer. Every page here is a live order nobody is managing.
        self._alert = alert or log_alert
        self._clock = clock or time.monotonic
        wd = config.watchdog
        #: How long a requested cancel is given to be confirmed terminal before it
        #: is concluded not to have landed. REUSED from the exit path rather than
        #: given a key of its own: it is the same physical quantity on both paths —
        #: the one the open question "what does a real `Cancel Requested` take to go
        #: terminal?" is about — so whoever tunes it should move both. The opposite
        #: of the `market_data_max_age_s` landmine, where one key governed two
        #: genuinely different things.
        self._settle_s = wd.exit_cancel_settle_s
        #: The floor on the re-cancel rate AND the page's repeat bound.
        self._max_s = wd.unmanaged_order_max_s
        #: `would_cancel` when False. See the module docstring's staged rollout.
        self._cancel_enabled = wd.unmanaged_order_cancel
        #: broker_order_id -> the residual. In-process, never rehydrated (rule 4).
        self._residuals: dict[int, ResidualOrder] = {}
        #: Its OWN escalation instance, sharing only the class with the watchdog's:
        #: these episodes are keyed by ORDER and the watchdog's by position, and a
        #: shared instance would let one `forget` clear the other's slots.
        self._escalation = _Escalation(self._clock)

    # -- registration ------------------------------------------------------

    def register(
        self,
        *,
        broker_order_id: int,
        external_identifier: str,
        minted_direction: OrderDirection,
        candidate_id: str | None,
        walk_outcome: str,
        cancel_requested: bool,
        last_status: str | None = None,
        limit_price: float | None = None,
        working_expected: bool = False,
    ) -> None:
        """Record that a finished walk left this order behind. Never awaits.

        Called from `PriceWalkingLoop._finish`, which is the one funnel every walk
        ends through, and called there rather than at the audited layer's cancel
        failure for the reason in the module docstring: the failed cancel is only
        one of the three ways an order is left working.

        **First registration wins.** Re-registering the same broker id would hand
        out a fresh settle allowance and reset the backoff — the id cannot be
        re-registered by a second walk (a broker id belongs to one order), so this
        is a guard against a caller looping, not a merge.
        """
        if broker_order_id in self._residuals:
            return
        residual = ResidualOrder(
            broker_order_id=broker_order_id,
            external_identifier=external_identifier,
            minted_direction=minted_direction,
            candidate_id=candidate_id,
            walk_outcome=walk_outcome,
            cancel_requested=cancel_requested,
            # The REGISTRY's clock, never the caller's.
            registered_at=self._clock(),
            last_status=last_status,
            limit_price=limit_price,
            working_expected=working_expected,
        )
        self._residuals[broker_order_id] = residual
        logger.info(
            "residual order %s registered (%s, %s, cancel_requested=%s)",
            broker_order_id, external_identifier, residual.walk_outcome,
            cancel_requested,
        )
        # Tier 2: a residual that resolves on the very next poll is the common case
        # and is not a safety event. What it IS is the evidence `would_cancel` mode
        # exists to collect — every registration exercises the direction sourcing
        # and the identifier cross-check whether or not it ever becomes actionable.
        self._audit.telemetry("order.residual_registered", residual.to_payload())

    def outstanding(self) -> tuple[ResidualOrder, ...]:
        """Residuals still being watched, oldest first. The MUTABLE registry entries.

        For tests and for `residual_rows()` below. A caller that only wants to render
        them should take that one instead: these objects are the registry's own state
        and their `registered_at` is on a monotonic clock nothing outside this module
        holds.
        """
        return tuple(
            sorted(self._residuals.values(), key=lambda r: r.registered_at)
        )

    def residual_rows(self) -> tuple[OutstandingResidual, ...]:
        """Orders left working that nobody is driving, right now. For `/status`.

        The live read behind the residual row, and the counterpart to
        `PositionWatchdog.impaired_positions()` / `spent_exit_attempts()` — same
        contract in every respect: a pure in-memory read, safe to call from another
        task on the same loop, no I/O, no lock, and empty is the steady state.

        **It resolves the two things a caller cannot.** `registered_at` is on THIS
        registry's monotonic clock, so `working_for_s` can only be computed here — a
        status seam differencing it against any other clock would produce a number
        that looks right and is not. And `cancel_enabled` is stamped on every row
        rather than exposed separately, so the mode travels with the rows it applies
        to and cannot be reported for a session that has none.

        `outstanding()` stays as it was: this returns a FROZEN snapshot, so a renderer
        cannot hold a handle into the registry's mutable state and read a half-applied
        update from `step()`.
        """
        now = self._clock()
        return tuple(
            OutstandingResidual(
                broker_order_id=r.broker_order_id,
                external_identifier=r.external_identifier,
                candidate_id=r.candidate_id,
                # THE direction, minted by Argus. Never `Order.direction` — the broker
                # relabels leg actions, and this is also what says whether the
                # automatic re-cancel applies to this row at all.
                minted_direction=str(r.minted_direction),
                walk_outcome=r.walk_outcome,
                cancel_requested=r.cancel_requested,
                attempts=r.attempts,
                working_for_s=max(0.0, now - r.registered_at),
                last_status=r.last_status,
                last_sighting=r.last_sighting,
                cancel_enabled=self._cancel_enabled,
                limit_price=r.limit_price,
                working_expected=r.working_expected,
            )
            for r in self.outstanding()
        )

    def working_close_for(self, external_identifier: str) -> OutstandingResidual | None:
        """The most recently registered CLOSING residual under this identifier. (2026-09-25)

        What the watchdog's spent-exit page and `/status` row name as "a close is still
        working at the broker" — on 2026-09-24 the page said nothing was trying to close
        the position while a crossing limit was. A registration is removed the moment
        the broker is seen to say it is DEAD or FILLED, so "registered" here means "not
        yet seen finished", which is what the page needs. Pure, like `residual_rows`.
        """
        closes = [
            row
            for row in self.residual_rows()
            if row.external_identifier == external_identifier
            and row.minted_direction == str(OrderDirection.CLOSE)
        ]
        if not closes:
            return None
        # `residual_rows` is oldest first. The resting CROSS is the order the page is
        # about (the walk's abandoned rung is normally confirmed dead within a poll),
        # so it wins; otherwise the newest registration.
        for row in reversed(closes):
            if row.working_expected:
                return row
        return closes[-1]

    # -- the recheck -------------------------------------------------------

    async def step(self, live: LiveOrders) -> None:
        """Read every residual against one broker answer, and act on what it says.

        Called by the watchdog inside `_refresh_if_due`, on the `LiveOrders` it has
        already fetched. A raise from here must never halt the watchdog — the caller
        swallows it, for the reason `_refresh_working_orders` gives and with more
        force: a failing cancel is this module's expected input, and taking the
        watchdog down over one would trade a live position's stop for an unfilled
        order's tidiness.
        """
        if not self._residuals:
            return
        now = self._clock()
        for residual in list(self._residuals.values()):
            sighting, order = read_sighting(
                live,
                broker_order_id=residual.broker_order_id,
                external_identifier=residual.external_identifier,
            )
            residual.last_sighting = str(sighting)
            if order is not None and order.status is not None:
                residual.last_status = str(order.status)
            if sighting.resolves:
                await self._resolve(residual, sighting)
                continue
            if sighting is ResidualSighting.WORKING:
                await self._working(residual, now)
                continue
            # ABSENT / UNREADABLE / AMBIGUOUS. A read gap is not a state: it does
            # not resolve the residual and it does not authorize a cancel (rule 3).
            await self._page(
                residual,
                tag=_READ_GAP_TAGS[sighting],
                kind="residual_order_unread",
                extra={"sighting": str(sighting)},
            )

    async def _resolve(self, residual: ResidualOrder, sighting: ResidualSighting) -> None:
        """The broker named this order and it is finished. Stop watching it."""
        self._residuals.pop(residual.broker_order_id, None)
        self._escalation.forget(str(residual.broker_order_id))
        payload = {**residual.to_payload(), "sighting": str(sighting)}
        if sighting is ResidualSighting.FILLED and not residual.working_expected:
            # Tier 1: an order the walk walked away from FILLING is a decision that
            # got reversed by the market. The position books through `FillConsumer`
            # and the watchdog adopts it exactly as it does any other — nothing here
            # touches `positions` — but the fact that it happened belongs in the
            # durable trail next to the `walk.finished` that gave up on it.
            #
            # NOT for a `working_expected` order (2026-09-25 review). A resting cross
            # FILLING is the cross doing its job, not a walk's decision reversed:
            # `order.residual_filled` and its page mean "an ABANDONED order filled —
            # the position is real", and on a cross that would tell an operator who
            # has just been stopped out that a new position opened. The fill itself
            # is `order.fill` at Tier 1 and closes the position through the ledger,
            # exactly as it did before the cross was registered at all; here it
            # resolves like any other finished residual, below.
            logger.warning(
                "residual order %s FILLED after the walk abandoned it (%s)",
                residual.broker_order_id, residual.external_identifier,
            )
            await self._audit.record("order.residual_filled", payload)
            self._alert("residual_order_filled", payload)
            return
        logger.info(
            "residual order %s resolved: %s", residual.broker_order_id, sighting
        )
        self._audit.telemetry("order.residual_resolved", payload)

    async def _working(self, residual: ResidualOrder, now: float) -> None:
        """Positively confirmed still working. The only actionable sighting."""
        allowance = self._max_s if residual.working_expected else self._settle_s
        if now - residual.registered_at < allowance:
            # A `working_expected` order (a resting cross) is SUPPOSED to be working;
            # it becomes a page only once it has rested `unmanaged_order_max_s`.
            #
            # Otherwise: inside the allowance. `cancel()` leaves an order at `Cancel Requested`,
            # not gone, and the watchdog's refresh fires early on any fill — so the
            # first sighting of a just-registered residual can be under a second old.
            # Measured explicitly rather than assumed from the 15s cadence.
            return

        may_cancel, tag, refusal = cancel_licence(
            minted_direction=residual.minted_direction,
            external_identifier=residual.external_identifier,
        )
        await self._page(
            residual,
            tag=tag,
            kind="residual_order_working",
            extra={"sighting": str(ResidualSighting.WORKING), "refused": refusal},
        )
        if not may_cancel:
            # A CLOSING order, or one whose direction is in doubt. Page-only, always,
            # under any grade or halt (rules 1 and 2).
            return
        if residual.next_attempt_at is not None and now < residual.next_attempt_at:
            return

        residual.attempts += 1
        residual.next_attempt_at = now + self._interval(residual.attempts)
        payload = {
            **residual.to_payload(),
            "next_attempt_in_s": round(residual.next_attempt_at - now, 3),
        }
        # THE outbound call, and the ONLY one in this module. It sits behind this one
        # flag on purpose — `would_cancel` is the same registry and the same decision
        # with the call not made, which is `shadow.would_submit`'s shape, and a test
        # asserts structurally that there is exactly one such call and that it is
        # inside this branch.
        if self._cancel_enabled:
            logger.warning(
                "re-requesting cancel of residual order %s (attempt %d, %s)",
                residual.broker_order_id, residual.attempts, residual.external_identifier,
            )
            try:
                await self._cancel(residual.broker_order_id)
            except Exception as exc:  # noqa: BLE001 - a failing cancel is the input
                # Expected, not exceptional: the whole reason this module exists is a
                # cancel that failed. `AuditedOrderExecution.cancel` has already
                # written `order.cancel_failed` at Tier 1; this only says the RETRY
                # is the thing that failed, and the schedule above already holds the
                # next attempt.
                logger.error(
                    "residual cancel attempt %d failed for order %s: %s",
                    residual.attempts, residual.broker_order_id, exc,
                )
                self._audit.telemetry(
                    "order.residual_cancel_failed",
                    {**payload, "error_type": type(exc).__name__, "error": str(exc)[:500]},
                )
        else:
            # `would_cancel`. Tier 2, and the record is the point of the stage: it
            # says the decision was reached, on which order, in which direction, at
            # which attempt — everything except the call.
            logger.warning(
                "would re-request cancel of residual order %s (attempt %d, %s) — "
                "watchdog.unmanaged_order_cancel is false",
                residual.broker_order_id, residual.attempts, residual.external_identifier,
            )
            self._audit.telemetry("order.residual_would_cancel", payload)

    def _interval(self, attempts: int) -> float:
        """Seconds until the next attempt, after `attempts` have been made.

        Geometric while the table lasts, then flat at `unmanaged_order_max_s`, and
        never longer than it: the floor is the bound an operator tunes, so a table
        entry may not outrun it.
        """
        index = attempts - 1
        step = _BACKOFF_S[index] if 0 <= index < len(_BACKOFF_S) else self._max_s
        return min(step, self._max_s)

    async def _page(
        self,
        residual: ResidualOrder,
        *,
        tag: str,
        kind: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Rising edge is Tier 1 and immediate; the repeats ride `unmanaged_order_max_s`.

        `_Escalation.overdue` rather than a one-shot edge, for the reason it was built
        (A2): a bounded RATE is what the 2026-08-05 storm argues for, and a single
        page is one missed notification away from the silence this exists to prevent.
        A live order Argus has stopped managing is not a condition that becomes less
        serious by lasting longer.
        """
        key = str(residual.broker_order_id)
        rising = self._escalation.rising(key, tag)
        elapsed = self._escalation.overdue(key, tag, self._max_s)
        if not rising and elapsed is None:
            return
        detail = {
            **residual.to_payload(),
            **(extra or {}),
            "condition": tag,
            "unmanaged_for_s": round(self._clock() - residual.registered_at, 3),
            "bound_s": self._max_s,
            "cancel_enabled": self._cancel_enabled,
        }
        logger.error(
            "UNMANAGED ORDER: %s (%s) — %s after %.1fs",
            residual.broker_order_id, residual.external_identifier, tag,
            detail["unmanaged_for_s"],
        )
        await self._audit.record("order.residual_unmanaged", detail)
        self._alert(kind, detail)


#: Which episode a read gap belongs to. A dict rather than a branch so a new
#: `ResidualSighting` that nobody mapped fails loudly here instead of silently
#: sharing another condition's bound.
_READ_GAP_TAGS = {
    ResidualSighting.ABSENT: _NOT_VISIBLE,
    ResidualSighting.UNREADABLE: _UNREADABLE,
    ResidualSighting.AMBIGUOUS: _AMBIGUOUS,
}


__all__ = [
    "CancelFn",
    "OutstandingResidual",
    "ResidualOrder",
    "ResidualOrderRecheck",
    "ResidualSighting",
    "cancel_licence",
    "read_sighting",
]
