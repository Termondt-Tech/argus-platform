"""The price-walking loop (spec §7) — ONE state machine, entry and exit both.

Spec §7 describes a single loop: route at the implied mid, wait a cycle, and if
it has not filled, cancel/replace stepping toward the market until it fills or the
walk-away threshold is hit. The exit variant is the same loop on a faster cadence.
So this is one implementation parameterized by a `WalkPolicy`, not an entry loop
that chunk 4 will later copy for exits. Two copies of a cancel/replace state
machine is how the exit path — the one that runs when a position is already
hurting — drifts from the entry path that gets all the testing.

Everything here is about TIMING and SEQUENCING. It builds no orders (chunk 1's
`build.py` does), prices nothing itself (chunk 1's `zerobid.py` does), records
nothing to the ledger (chunk 2's `AuditedOrderExecution` + `SqliteOrderStore` do),
and decides nothing about whether a trade is worth making (the gate does). It is
handed a built order and gets it filled or gives up.

Three properties are worth stating up front, because they are the ones that would
be easy to get wrong and expensive to have wrong:

**1. The cycle clock starts when the order is in the market, not when the broker
acknowledges it.** Chunk 1 measured the difference against sandbox: a pre-market
submission is accepted, carries `tif.next_valid_session`, and sits at `Received`
without being exposed to anyone. A loop that started counting at the ack would
spend its first cycle walking the price of an order no market maker can see, and
would cancel it before it ever had a chance. `_await_in_market` waits for a status
confirmed to mean "working at the exchange" (`models.CONFIRMED_IN_MARKET` — see
there for why that set is deliberately one value) before the cadence starts, and
does the same again after every replace, since a replaced order is a NEW order
that has to reach the market on its own.

**2. A cycle is at least one full COA settle window.** Not the response interval
alone. Cboe runs the Complex Order Auction for `[coa] response_window_ms`, and
then allows up to `[coa] extended_processing_ms` more to finish processing what
came back. Cancelling at the end of the response interval races a straggler
response the exchange is still working on: the cancel and a fill can cross, and
the position you thought you walked away from is one you now hold at a price you
have already stopped tracking. `WalkPolicy.cycle_wait_s` is `max(cadence, both
figures combined)`, so a cadence configured tighter than the auction (a 1s exit
against a 1s settle window) is raised to the auction's floor rather than
silently racing it.

**3. The result is a REPORT, not a guarantee.** `cancel()` returning means the
broker accepted a cancel REQUEST — the order goes to `Cancel Requested`, not
away — so a fill can still land after this loop has returned `WALKED_AWAY`. The
authoritative record of what Argus holds is `positions`, written by the fill
consumer off the `AlertStreamer` (chunk 2) and checked by reconciliation at
restart (spec §6). Nothing downstream may treat a `WalkResult` as proof of what
is or is not held; it is the record of what this loop did and last saw.

Since 2026-08-29 that sentence has a BACKSTOP for the order rather than only for
the position: `_finish` hands any order whose last observation was non-terminal to
`execution/residual_orders.py`, which watches it until the broker is seen to say it
is terminal. Nothing about this loop's contract changed — `_request_cancel` is
still best-effort, still non-fatal, and the walk's decision still stands either way
— but "the order goes to `Cancel Requested`, not away" is no longer where the story
ends.

**4. The ladder's reference can go stale, and a CIRCUIT BREAKER — not a re-quote —
is the answer.** `PriceWalk` is pure arithmetic on the gate-approved mid, which is
deliberate (see `price_at`) but has a cost: at 10-15 delta a 2-3 point SPX move
inside a 20-30s walk can move a 5-wide spread by a large fraction of its credit, and
every remaining rung is then priced off a market that has moved on. The loop does
NOT fix that by re-quoting — it still imports no market data, and `price_at`'s call
graph is still `{round_to_tick}`. Instead it accepts an OPTIONAL injected
`drift_check`, calls it at each cycle boundary, and abandons the walk (`DRIFTED`)
when the check says the reference no longer holds. With no check supplied the
behaviour is exactly what it was: a bolt-on, not a rearchitecture.

**On recovery.** A broker call that raises mid-walk is not a reconciliation event
and must not halt the process — reconciliation is a restart-time rail (chunk 2),
and a dropped connection at 10:42 is recoverable. But recovery is not retrying the
call: re-sending a replace that actually went through is the replay chunk 2's
design forbids outright. Instead the loop reconnects (if given a way to),
re-queries what the broker says exists under this `external_identifier`, and
resumes the state machine from reality. Reads are retried freely; mutations never
are.

**On concluding.** The same identifier-wide question is asked once more before any
walk reports a non-fill, and it is not a recovery: the walk holds facts about at most
one RUNG, while every non-fill outcome is a claim about the SPREAD, and a walk that
has repriced holds one of several orders sharing one key. Since 2026-09-10 the asking
lives in `_finish`, the sole constructor of every `WalkResult` here, gated on the
OUTCOME (`_ASK_BEFORE_CONCLUDING`) rather than on the branch that produced it —
because **the trigger is that the WALK is about to conclude, not that the BROKER
already did**, and the three branches that concluded off a clock rather than off a
broker answer were asking nothing. That read is the one exception to the sentence
above — it is deliberately NOT retried, because unlike every other read here the loop
can conclude perfectly well without it; see `_finish`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from argus.alerts import AlertFn, log_alert
from argus.audit.log import AuditLog, AuditWriteError
from argus.config import Config
from argus.execution.audited import AuditedOrderExecution
from argus.execution.broker import OrderExecutionPort
from argus.execution.guard import ConflictVerdict, DuplicateOrderGuard
from argus.execution.models import (
    MAX_REJECT_REASON_CHARS,
    Order,
    OrderDisposition,
    OrderStatus,
    UnmappableOrderError,
    classify_order,
    is_in_market,
)
from argus.execution.residual_orders import ResidualOrderRecheck
from argus.execution.zerobid import TICK, round_to_tick

logger = logging.getLogger("argus.execution.loop")

SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]
#: Re-establish the broker connection. The loop does not own the session, so this
#: is a seam the caller fills in (chunk 5) rather than something it can do itself.
ReconnectFn = Callable[[], Awaitable[None]]
#: "Has the market moved out from under the price this walk is offering?" Returns
#: None to continue, or a human-readable REASON to abandon the walk.
#:
#: An injected callable, and that is the whole design: this module must keep its
#: distance from market data (an order's legs carry OCC symbols while
#: `MarketDataPort` is keyed by `streamer_symbol` — `market.latest(leg.symbol)`
#: returns None forever without raising, the bug chunk 4 hit), so the loop asks a
#: question and never learns how the answer is obtained. Chunk 5 supplies the
#: implementation over chunk 4's existing quote path; nothing here imports a feed.
#:
#: A REASON rather than a bool because the reason is the diagnostic. "Aborted at
#: step 2 because SPX moved 3.4 points from the 5842.10 the gate saw" is a
#: different post-mortem from "aborted", and `walk.finished` is where someone
#: reading the audit trail will look for it.
#:
#: SYNCHRONOUS on purpose: it is answered from an already-received quote, so it
#: must not be a place where a walk can block or await.
DriftCheckFn = Callable[[], str | None]
#: "Has a halt been ordered?" True abandons the walk (cancel + `ABORTED`).
#:
#: The seam the KILL SWITCH reaches an in-flight walk through, and it is injected
#: for exactly the same reason `DriftCheckFn` is: this module must not learn what a
#: kill switch is. It asks a bool question; `entry.py` supplies the answer.
#:
#: Wired for ENTRY walks ONLY, and the asymmetry is the design (spec §6). A kill
#: press must stop the system taking on NEW risk, so an entry walk is abandoned and
#: its order cancelled. It must NOT strand an open position by cancelling the order
#: that is closing it, so `ExitExecutor` deliberately passes none and an exit walk
#: runs to completion after a trip. The session stop event (`stop=`) is the separate
#: reason that applies to BOTH — the close, a SIGTERM, the soft day-end.
#:
#: SYNCHRONOUS, like `DriftCheckFn`: it reads in-memory state, so it must never be
#: a place a walk can block or await.
HaltCheckFn = Callable[[], bool]

# Backoff between recovery attempts, in seconds, by attempt number. Short and
# bounded: this covers a dropped socket or a 502, not an outage — an outage
# exhausts the attempts and returns UNRESOLVED, which is the honest answer.
_RECOVERY_BACKOFF_S = (0.5, 1.0, 2.0)


class BrokerUnavailable(RuntimeError):
    """The broker could not be reached often enough to resolve the walk's state."""


class GuardUnreachable(RuntimeError):
    """`DuplicateOrderGuard` could not be ASKED, after a bounded burst. (2026-09-03)

    Raised, never returned, and that is the unchanged half of chunk 3's contract:
    *"I could not check" must not become a `WalkOutcome`*, or a caller reads it as
    one more not-filled case and sends anyway. Nothing was sent when this raises —
    the guard is a GET, asked before the first submit — so a caller that catches it
    has a position in exactly the state it was in a moment earlier.

    What changed on 2026-09-03 is that the exception is now TYPED and bounded.
    Before, whatever the transport raised propagated raw out of `run()` into
    `ExitExecutor.close`, and from there into the watchdog's hard-halt region: on
    2026-09-03 a single cert 502 on this GET halted the process at 11:04 ET and
    took the watchdog, the virtual stop and the 15:30 flatten down with it, leaving
    a triggered 0DTE spread unmanaged for the rest of expiry day.

    **Catching this is not permission to send.** The submit path is still reachable
    only through an answered `ConflictVerdict`; a caller's only sanctioned response
    is to send NOTHING and look again later.
    -> docs/decisions/2026-09-03-exit-guard-unreachable.md
    """

    def __init__(self, attempts: int, last: BaseException) -> None:
        super().__init__(
            f"duplicate-guard check could not be answered in {attempts} attempt(s): "
            f"{type(last).__name__}: {last}"
        )
        #: How many attempts the burst actually spent (it stops early on `stop`).
        self.attempts = attempts
        #: The last failure. The one an operator needs; the earlier ones are Tier 2.
        self.last = last


#: Attempts at ONE duplicate-guard check before `run()` gives up on the question.
#: NOT config, and separate from `entry._GUARD_ATTEMPTS` on purpose even though both
#: are 3 today: the two bursts have different consequences when spent (an entry
#: retires a trade idea and can eventually end the day; an exit must never do
#: either), so tying them together would make one number carry two decisions. Same
#: split `_EXIT_SEND_ATTEMPTS` already makes against `entry._GUARD_ATTEMPTS`. The one
#: number an operator has a reason to tune — the per-attempt bound — IS config, and
#: is the SAME key (`[execution] guard_timeout_s`): it bounds the same call, to the
#: same endpoint, through the same port method.
_GUARD_ATTEMPTS = 3


def _broker_text(exc: BaseException) -> str:
    """A failed mutation's cause, as one bounded operator-readable line. (2026-09-04)

    The TYPE is kept alongside the message, the way every `error_type`/`error` pair in
    this codebase's audit payloads already does: `instrument_validation_failed` and
    `timeout_error` are the two shapes measured so far, and the type is what makes a
    log filter possible without anyone matching prose.

    Bounded at `models.MAX_REJECT_REASON_CHARS`, the same bound the field it sits
    beside uses — this lands in a hash-chained audit payload, and an unbounded string
    from the broker is not something to hand the canonical serializer unmeasured.
    Never `""`: an exception with an empty message still names its type.
    """
    return f"{type(exc).__name__}: {exc}".strip()[:MAX_REJECT_REASON_CHARS]


def _guard_backoff_s(attempt: int) -> float:
    """1s, then 2s. `entry._guard_backoff_s`'s ladder, and for its reason.

    Every second here is unchecked price staleness on the walk's first rung — the
    one rung `UnderlyingDriftCheck` never sees, since it runs at cycle boundaries
    AFTER the first submit — so a slower ladder buys this call site nothing. That
    argument was written for the entry walk and transfers unchanged: the exit walk's
    first rung has no drift check either.
    """
    return 1.0 * attempt


# -- policy -------------------------------------------------------------------


@dataclass(frozen=True)
class WalkPolicy:
    """Everything about HOW a walk paces and steps. Entry and exit differ ONLY here.

    Constructed from config for entries (`for_entry`); chunk 4 builds its own
    instance for exits with a faster cadence and whatever concession an exit
    justifies. Nothing in the state machine reads config directly, so a second
    parameterization needs no change to the machine.
    """

    #: Seconds to hold an order at a price before repricing it. Spec §7's
    #: "wait one cycle" — ~3-5s for entries, ~1s for exits.
    cadence_s: float
    #: `[coa] response_window_ms + extended_processing_ms`, in seconds. The floor
    #: under `cadence_s`; see the module docstring, property 2.
    coa_settle_s: float
    #: Ticks conceded per reprice step. Spec §7's "$0.05 increments" is 1.
    step_ticks: int
    #: Fraction of the ORIGINAL limit's magnitude this walk may concede before
    #: abandoning. 0.20 on a 0.85 credit stops at 0.70; on a -0.85 debit it stops
    #: at -1.00. One number, both directions — see `PriceWalk`.
    walk_away_pct: float
    #: Minimum number of REPRICES this walk must have, whatever `walk_away_pct`
    #: rounds to. 0 disables it.
    #:
    #: A percentage of a price on a tick grid vanishes below half a tick, and the
    #: floor then rounds back onto the initial: at `walk_away_pct = 0.20` an exit
    #: opening at a debit of 0.05 or 0.10 has ZERO rungs. That is not the tail — it
    #: is the ordinary state of a winning 0DTE spread being flattened at 15:30, and
    #: on 2026-08-31 it meant the ladder contributed nothing to either flatten and
    #: both outcomes rode on a single instantaneous market cross.
    #:
    #: **STEPS, not ticks.** The guarantee is "this many reprices", so it holds if
    #: `step_ticks` is ever raised above 1.
    #:
    #: **This is where the entry/exit asymmetry lives, and that is the point.** The
    #: same collapse exists on the entry side below a credit of 0.25 and is currently
    #: unreachable only because `signal.min_credit` is 0.50 — so `for_entry` passes 0
    #: EXPLICITLY rather than inheriting the exit's value. Putting the number here
    #: instead of branching on the sign inside `PriceWalk` keeps the state machine's
    #: "no direction branch" contract intact: this class is where entry and exit are
    #: allowed to differ.
    #:
    #: **No default**, on `seconds_to_flatten`'s precedent (2026-08-28): a default
    #: here would be silent, and both possible defaults are wrong for one of the two
    #: parameterizations. Every `WalkPolicy` states it.
    #: → docs/decisions/2026-08-31-exit-ladder-near-tick.md
    min_concession_steps: int
    #: How long to wait for a submitted order to reach a confirmed in-market
    #: status before giving up on it and cancelling.
    working_timeout_s: float
    #: How often to ask the broker for an order's status while waiting for it to
    #: reach the market. Purely an operational knob: it trades request volume
    #: against how fast the loop notices a transition, and has no bearing on price.
    status_poll_interval_s: float
    #: Consecutive broker failures tolerated before the walk returns UNRESOLVED.
    max_recovery_attempts: int

    def __post_init__(self) -> None:
        for name in (
            "cadence_s", "coa_settle_s", "working_timeout_s", "status_poll_interval_s"
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"WalkPolicy.{name} must be > 0")
        if self.step_ticks <= 0:
            raise ValueError("WalkPolicy.step_ticks must be >= 1")
        if not 0 < self.walk_away_pct <= 1:
            raise ValueError("WalkPolicy.walk_away_pct must be in (0, 1]")
        if self.min_concession_steps < 0:
            raise ValueError("WalkPolicy.min_concession_steps must be >= 0")
        if self.max_recovery_attempts <= 0:
            raise ValueError("WalkPolicy.max_recovery_attempts must be >= 1")

    @property
    def step(self) -> Decimal:
        """Concession per reprice, as an exact tick multiple."""
        return TICK * self.step_ticks

    @property
    def cycle_wait_s(self) -> float:
        """How long to actually hold a price. Never shorter than the auction."""
        return max(self.cadence_s, self.coa_settle_s)

    @property
    def clamped_by_coa(self) -> bool:
        return self.cadence_s < self.coa_settle_s

    @classmethod
    def for_entry(cls, config: Config) -> "WalkPolicy":
        """The ENTRY parameterization, straight from config.

        The COA settle window is assembled here rather than stored as one number,
        because Cboe publishes the two figures separately and changes them
        separately (spec §7: never hardcode the auction timing).

        **`min_concession_steps=0` is deliberate and is a REPORTED, UNFIXED condition
        rather than an omission** (2026-08-31). The near-tick floor collapse the exit
        side just had fixed exists here too, one step lower: at
        `entry_walk_away_pct = 0.10` a credit of 0.25 or less gets zero rungs. It is
        unreachable today for one reason and it is not in this file — `signal.min_credit`
        is 0.50, so `strategy.py` emits nothing that could produce it. Lower that
        `[signal]` key to 0.25 and every entry walk silently becomes zero-rung, which
        combined with `entry.py`'s one-attempt-per-`candidate_id` rule RETIRES the strike
        pair for the process lifetime on the first unfilled cycle. The 0 is written here
        so that dependency is greppable from the walk rather than implied by its absence.
        → docs/decisions/2026-08-31-exit-ladder-near-tick.md
        """
        coa = config.coa
        execution = config.execution
        policy = cls(
            cadence_s=execution.entry_cadence_s,
            coa_settle_s=(coa.response_window_ms + coa.extended_processing_ms) / 1000.0,
            step_ticks=execution.entry_step_ticks,
            walk_away_pct=execution.entry_walk_away_pct,
            min_concession_steps=0,
            working_timeout_s=execution.working_timeout_s,
            status_poll_interval_s=execution.status_poll_interval_s,
            max_recovery_attempts=execution.max_recovery_attempts,
        )
        if policy.clamped_by_coa:
            logger.warning(
                "execution.entry_cadence_s=%.3fs is shorter than the COA settle "
                "window (%.3fs); holding each price for the auction's duration "
                "instead — cancelling inside the window races a straggler response",
                policy.cadence_s, policy.coa_settle_s,
            )
        return policy

    @classmethod
    def for_exit(cls, config: Config) -> "WalkPolicy":
        """The EXIT parameterization (chunk 4), from `[watchdog]`.

        This is the whole of what makes the state machine below an exit loop — the
        difference between entering and exiting is a `WalkPolicy` instance and a
        negative starting price, as `PriceWalk` explains. Nothing in the machine
        reads config or branches on direction, so there is no second implementation
        to keep in step.

        A classmethod here rather than a constructor call in the watchdog for one
        reason: the COA settle window is assembled from the two `[coa]` figures, and
        that assembly must exist exactly once. Duplicating it in the watchdog is how
        an exit ends up racing the auction after somebody changes one of the two
        keys (spec §7: never hardcode the auction timing).

        The four knobs that differ — cadence, step, walk-away, and (since
        2026-08-31) the walk-away's minimum in steps — are exactly the ones that are
        strategy decisions, and they live in `[watchdog]` alongside the triggers.
        `exit_walk_min_steps` is config for the same reason `exit_walk_away_pct` is:
        it decides how much more this walk will pay to close, and the data-backed
        pass that re-derives the percentage has to be able to move both in one edit.

        **The operational knobs (`working_timeout_s`, `status_poll_interval_s`,
        `max_recovery_attempts`) stay shared, and since 2026-08-21 (A2) that is a
        CHECKED decision rather than an inherited one.** The original reasoning —
        "they trade request volume against reaction time and touch no price, so an
        exit has no reason to want different ones" — was entry-shaped:
        `working_timeout_s`'s config comment is written entirely about a pre-market
        ENTRY sitting at `Received` under `tif.next_valid_session`, which is a state
        the watchdog cannot be in. It only runs against a position that has already
        filled. What was re-derived for the exit path:

        - `working_timeout_s` (30s) is not a passive wait here. A fill or a terminal
          status short-circuits `_await_in_market` on the poll that sees it, so the
          full bound only elapses on an order that is persistently NEITHER in-market
          nor terminal. Shortening it for exits is the wrong direction: the timeout's
          exit is `NEVER_WORKED`, which requests a cancel it never confirms
          (`_request_cancel` is best-effort — the order sits at `Cancel Requested`
          and can still fill) and lands the position in `ExitOutcome.NOT_RESOLVED`.
          A tighter bound reaches that state sooner, and reaches it on orders that
          were about to work.
        - `max_recovery_attempts` (3, ~1.5s of `_RECOVERY_BACKOFF_S`) exhausts into
          UNRESOLVED — "nobody read this order". More attempts against an unreadable
          broker buy an exit nothing it may act on: an unread order never authorizes
          a second closing order (`_await_settled`), so the extra patience would end
          at the same refusal.

        So the exposure this pair creates is not the seconds it spends, it is the
        state it lands in — and that state is bounded in the WATCHDOG, by
        `PositionWatchdog._page_if_spent_and_still_held`, not by a number here.
        → docs/decisions/2026-08-21-exit-attempt-spent-is-not-the-end.md
        """
        policy = cls._exit_from_config(config)
        if policy.clamped_by_coa:
            # Expected for an exit, not an error: spec §7 suggests ~1s exit cadence
            # and the settle window is 1s, so the clamp usually binds. Logged at INFO
            # rather than WARNING because it is the designed behaviour here — the
            # exit still runs at the fastest cadence the auction permits.
            logger.info(
                "watchdog.exit_cadence_s=%.3fs is at or inside the COA settle window "
                "(%.3fs); the exit walk will hold each price for the auction's "
                "duration, which is the floor rather than a misconfiguration",
                policy.cadence_s, policy.coa_settle_s,
            )
        return policy

    @classmethod
    def _exit_from_config(cls, config: Config) -> "WalkPolicy":
        """The exit parameterization, built and not announced.

        The ONE place the exit's fields — and with them the COA settle window — are
        read from config, shared by `for_exit` (which adds the clamp log) and
        `for_profit_target` (which derives from it), so the assembly `for_exit`
        requires to "exist exactly once" still does. Split out 2026-09-23 so the
        profit policy could stop re-spelling it without logging the clamp twice.
        """
        coa = config.coa
        watchdog = config.watchdog
        return cls(
            cadence_s=watchdog.exit_cadence_s,
            coa_settle_s=(coa.response_window_ms + coa.extended_processing_ms) / 1000.0,
            step_ticks=watchdog.exit_step_ticks,
            walk_away_pct=watchdog.exit_walk_away_pct,
            min_concession_steps=watchdog.exit_walk_min_steps,
            working_timeout_s=config.execution.working_timeout_s,
            status_poll_interval_s=config.execution.status_poll_interval_s,
            max_recovery_attempts=config.execution.max_recovery_attempts,
        )

    @classmethod
    def for_profit_target(cls, config: Config) -> "WalkPolicy":
        """The PROFIT-TARGET parameterization (2026-09-23) — the exit's, with ONE rung.

        The same seam used a third time: a profit take is an exit, so cadence,
        walk-away percentage and the three operational knobs are `for_exit`'s, field
        for field, and the state machine is untouched. What differs are the two
        fields that together set how much it may pay.

        **`min_concession_steps=1` and `step_ticks=1`, both stated rather than
        inherited, and together they are the profit target's MAXIMUM DEBIT.** A profit
        take opens at exactly one tick — `ExitExecutor.close` refuses to walk any other
        opening limit — so the floor is `-0.05 - 0.05 = -0.10` for EVERY legal
        `exit_walk_away_pct`: at `(0, 1]` the percentage of a 0.05 debit never concedes
        more than the one tick the minimum already does. `step_ticks` is pinned rather
        than read from `exit_step_ticks` because a rung of two ticks would make that
        one rung 0.15, and a knob tuned for the emergency ladder must not be able to
        move this bound. One rung is the offer: a 0.05 mid sits one tick under a 0.10
        crossing debit on the modal book (2026-09-22 record, §10), so the second rung
        lifts it.

        **Not `exit_walk_min_steps` (3), deliberately.** That minimum buys the
        FLATTEN range in the one regime where the cross can be absent entirely; three
        rungs from 0.05 walks to 0.20, and the record measured a 0.15/0.20 take as
        WORSE THAN DOING NOTHING — it buys the win rate by giving away credit the
        position would have kept (§3). An emergency exit may pay to get flat; a
        profit take that has to pay more than a tick above its trigger is not one.

        DERIVED from the exit's policy rather than re-assembled (2026-09-23 review), so
        a change to how the settle window is built reaches both walks or neither. No
        clamp log here: the cadence and settle window are the exit's own, and
        `for_exit` has already said so.
        → docs/decisions/2026-09-22-profit-target-and-re-entry.md §11.5
        """
        return replace(
            cls._exit_from_config(config), step_ticks=1, min_concession_steps=1
        )


@dataclass(frozen=True)
class PriceWalk:
    """Where the limit goes next, and when to stop. Pure — no I/O, no clock.

    **Conceding is always DOWN, for both entries and exits**, and that falls out
    of tastytrade's sign convention rather than being a rule this module invents.
    A limit price is signed positive for a credit and negative for a debit
    (`Order.limit_price`), so "give the market a better deal" is:

        opening a credit spread   +0.85 -> +0.80 -> +0.75   collect less
        closing it for a debit    -0.85 -> -0.90 -> -0.95   pay more

    Both are the same arithmetic — subtract a tick — which is why the state
    machine has no direction branch anywhere in it. An exit walk is an entry walk
    with a different policy and a negative starting price, and that is the whole
    difference.

    All prices go through `zerobid.round_to_tick`, the one tick-rounding
    implementation in the codebase, so a walk can never produce a limit the
    exchange would reject for sub-tick precision.
    """

    initial: Decimal
    step: Decimal
    floor: Decimal

    @classmethod
    def from_policy(
        cls, initial: Decimal, policy: WalkPolicy, *, width: Decimal | None = None
    ) -> "PriceWalk":
        """`width` is the order's own strike distance (`Order.width`), NOT a quote.

        Passing it caps the floor at the most the spread can possibly be worth — see
        the clamp at the bottom of this method. It is keyword-only and defaults to
        `None` so the many unit tests that construct a walk from a bare price keep
        working, but **both production call sites pass it** and
        `tests/test_execution_loop.py` asserts that structurally, because an
        uncapped floor is how a flatten came to offer more than max loss.
        """
        if initial == 0:
            raise ValueError("cannot walk a zero limit price")
        raw_floor = round_to_tick(initial - abs(initial) * Decimal(str(policy.walk_away_pct)))
        # A PERCENTAGE OF A PRICE ON A TICK GRID VANISHES BELOW HALF A TICK, and the
        # floor then rounds back onto `initial` — a walk with nowhere to walk. At
        # `walk_away_pct = 0.20` that is every exit opening at a debit of 0.10 or
        # less, which is the ordinary state of a winning 0DTE spread being flattened
        # at 15:30 rather than a tail (2026-08-31: both flattens got zero rungs and
        # rode entirely on the market cross, and one of the two had no offer to lift).
        #
        # DIRECTION-FREE, and that is load-bearing: conceding is subtraction for both
        # a credit and a debit (see the class docstring), so `least` is sign-correct
        # with no test on `initial` and the state machine keeps its no-direction-branch
        # contract. The asymmetry that DOES exist lives in `WalkPolicy` — `for_entry`
        # passes 0, so this clause is inert for entries and the entry ladder is
        # bit-identical to what it was.
        #
        # Compared as CONCESSION MAGNITUDES rather than as floors, so it reads the same
        # for both signs and can only ever widen the walk, never narrow one that
        # already has more range than the minimum.
        least = round_to_tick(initial - policy.step * policy.min_concession_steps)
        if abs(raw_floor - initial) < abs(least - initial):
            raw_floor = least
        if initial > 0 and raw_floor < TICK:
            # A credit walk must never reach zero or cross into a debit: paying to
            # OPEN a credit spread is a different trade from the one that was
            # sized and gated, not a more aggressive version of it. One tick is the
            # least credit that is still a credit.
            #
            # It runs AFTER the minimum above, and the order is the safe one: a credit
            # walk given a nonzero `min_concession_steps` is still clamped here, so the
            # minimum can never push an entry across zero. Reversing them would let it.
            raw_floor = TICK
        if width is not None and abs(raw_floor) > width:
            # THE WIDTH CAP, and it is LAST so nothing downstream can push past it —
            # a cap that another clause may override is not a cap (2026-09-17).
            #
            # A vertical cannot be worth more than the distance between its strikes, so
            # every rung past the width offers to pay more than the position's maximum
            # possible loss. Unbounded, this was real: on 2026-09-03 a 5-wide spread
            # opened its exit at -5.05 and floored at **-6.05** — 20 rungs, the last 21
            # of them unpayable by construction. Walking that ladder to the end would
            # have paid (0.70 - 6.05) x 2 x 100 = -1,070 against an -860 maximum: $210
            # WORSE than doing nothing, and past `max_loss_per_position` too, on the one
            # rail whose entire premise is that loss is bounded by the width.
            #
            # `copy_sign` keeps this DIRECTION-FREE, which is the same contract
            # `min_concession_steps` is written to above: the floor always sits on
            # `initial`'s side of zero (the credit clamp guarantees it for an opening
            # walk), so bounding the MAGNITUDE needs no test on the sign and the state
            # machine keeps having no direction branch anywhere in it.
            #
            # INERT FOR ENTRIES, and by arithmetic rather than by a flag: an opening
            # walk concedes DOWNWARD from a credit that `build._limit_price` has already
            # capped at the width, so `abs(raw_floor) <= abs(initial) <= width` holds
            # before this line is reached. The entry ladder is bit-identical.
            #
            # NB a pre-fix order can still arrive here with `abs(initial) > width` (the
            # 2026-09-03 order is exactly that shape). The floor then lands INSIDE the
            # initial and `price_at(1)` returns None — zero rungs, no walking past the
            # cap. That is the safe direction, and `build._limit_price` now stops such
            # an order being built in the first place.
            raw_floor = width.copy_sign(raw_floor)
        return cls(initial=initial, step=policy.step, floor=raw_floor)

    def price_at(self, step_index: int) -> Decimal | None:
        """The limit for step `step_index`, or None once the walk is exhausted.

        Step 0 is the original gate-approved price — the mid `build.py` put on the
        order — which is never recomputed here.
        """
        if step_index <= 0:
            return self.initial
        price = round_to_tick(self.initial - self.step * step_index)
        return None if price < self.floor else price

    def step_of(self, price: Decimal) -> int | None:
        """Which step `price` corresponds to, or None if it is not on this walk.

        Used by the recovery path: after a replace whose outcome is unknown, the
        broker's answer for the order that actually exists carries the limit price
        that actually applies, and the step counter has to be re-derived from it
        rather than assumed to have advanced. Getting that wrong skips a rung of
        the ladder — small, but it means the walk no longer matches the audit trail.
        """
        offset = self.initial - price
        if offset < 0 or self.step <= 0:
            return None
        steps, remainder = divmod(offset, self.step)
        return int(steps) if remainder == 0 else None


# -- outcomes -----------------------------------------------------------------


class WalkOutcome(StrEnum):
    """How a walk ended. Deliberately more than success/failure: chunk 5 has to
    treat "I gave up on a good price" very differently from "the broker refused
    it" and differently again from "I do not know"."""

    #: Filled — wholly, or partly with the remainder cancelled. `WalkResult`
    #: carries the contract count; a partial is a smaller spread, not a naked leg,
    #: because the whole thing is one multi-leg order.
    FILLED = "filled"
    #: The walk-away threshold was reached without a fill. A cancel was requested.
    WALKED_AWAY = "walked_away"
    #: The underlying left the level this candidate was priced at, so the ladder's
    #: reference no longer describes the market. A cancel was requested.
    #:
    #: DISTINCT from `WALKED_AWAY`, deliberately. Both end with no fill and a
    #: cancel, but they say opposite things about the same numbers: `WALKED_AWAY`
    #: means the concession budget was spent and the auction still would not pay,
    #: which is evidence about `entry_walk_away_pct`; `DRIFTED` means the walk was
    #: pulled while it still had budget left, which is evidence about nothing
    #: except the tape. Collapsing them would make the walk-away statistics — the
    #: main thing anyone will tune from — quietly wrong.
    DRIFTED = "drifted"
    #: The broker refused the order outright. Terminal immediately — this is not a
    #: "not filled yet" case and must never be stepped or retried.
    REJECTED = "rejected"
    #: The duplicate guard found a conflicting order. Nothing was sent.
    BLOCKED = "blocked"
    #: Accepted but never reached a confirmed in-market status inside the timeout
    #: (the measured pre-market `Received` case). A cancel was requested.
    NEVER_WORKED = "never_worked"
    #: Something other than this loop terminated it with nothing filled.
    CANCELLED_ELSEWHERE = "cancelled_elsewhere"
    #: The broker could not be reached often enough to say what happened. The one
    #: outcome that means "go look" — nothing may be inferred from it.
    #:
    #: **Narrowed on 2026-08-28.** It used to cover a case that is its exact
    #: opposite: a mutation failed, the broker WAS re-queried successfully, and the
    #: answer named nothing. That is now `NO_ORDER_EXISTS`. Everything still landing
    #: here is a genuine read failure — the broker unreachable, an order that kept
    #: vanishing from the answers, or two working orders under one identifier that
    #: were all cancelled and none of the cancels confirmed.
    UNRESOLVED = "unresolved"
    #: A mutation failed, the broker was ASKED what exists under this identifier,
    #: and it answered: nothing working, nothing filled.
    #:
    #: DISTINCT from `UNRESOLVED`, and the distinction is the whole point (2026-08-28).
    #: Both end with no fill, but they are opposite epistemic states: `UNRESOLVED`
    #: means nobody read the broker, this means the broker was read and had nothing.
    #: Collapsing them made a caller that can safely retry — the exit path, whose
    #: retry re-runs the duplicate guard — refuse to, on reasoning ("a second order
    #: would stack onto something we cannot see") that is false when there is
    #: nothing to see. That cost a triggered 0DTE spread its only exit attempt.
    #:
    #: **This is not a claim that no order can ever appear.** The submit may have
    #: reached the broker and simply not be visible yet. What it licenses is asking
    #: AGAIN, through the guard, which is the same protection every first submission
    #: already relies on — never re-sending blind on the strength of this answer.
    NO_ORDER_EXISTS = "no_order_exists"
    #: The caller asked to stop (session end, kill switch). A cancel was requested.
    ABORTED = "aborted"


#: Outcomes that assert "this trade idea did not fill". Each one is a claim about the
#: SPREAD, and the walk holds facts about at most one RUNG — so the identifier is
#: asked before any of them is returned. Membership is by that property alone; adding
#: a WalkOutcome without classifying it fails `test_every_outcome_is_classified`.
#:
#: **The trigger for asking is that the WALK is about to conclude, not that the BROKER
#: already did.** 2026-09-02 hung the same read off a terminal broker observation and
#: argued from exactly that; the deadline branches have no such observation — they
#: conclude off a clock, with the broker having said nothing — and concluded about the
#: spread from a fact about one rung anyway. `_finish` is the sole constructor of every
#: `WalkResult` here, so it is the only place this rule can be an INVARIANT rather than
#: a habit, and the habit has now failed three times (2026-08-26 twice, 2026-09-02).
#: → docs/decisions/2026-09-10-never-worked-is-not-a-verdict.md
_ASK_BEFORE_CONCLUDING = frozenset({
    WalkOutcome.NEVER_WORKED,
    WalkOutcome.UNRESOLVED,
    WalkOutcome.REJECTED,
    WalkOutcome.CANCELLED_ELSEWHERE,
    WalkOutcome.NO_ORDER_EXISTS,
    WalkOutcome.WALKED_AWAY,
    WalkOutcome.DRIFTED,
})

#: Outcomes that must NOT ask, each for its own reason. Not "everything else": the
#: union with `_ASK_BEFORE_CONCLUDING` must cover `WalkOutcome` exactly, so a new
#: member cannot be silently absent from both.
#:
#: - `FILLED` — it is the answer. Probing would re-ask a question already resolved.
#: - `BLOCKED` — nothing was sent, so there is no claim about a spread to be wrong
#:   about; and `_ask_guard` asked the identifier-wide question one frame earlier and
#:   acted on it. Two independent reasons, either sufficient.
#: - `ABORTED` — `_abort` is the kill-switch and session-close path, and **a halt must
#:   not add a broker round trip.** The phone button's stated bound is one
#:   `cycle_wait_s`; a GET inside the abort frame widens it for no benefit, since
#:   nothing downstream acts on the abort's fill count. This exclusion is a SAFETY
#:   RAIL, not an optimisation.
_NEVER_ASKS = frozenset({
    WalkOutcome.FILLED,
    WalkOutcome.BLOCKED,
    WalkOutcome.ABORTED,
})


@dataclass(frozen=True)
class WalkResult:
    """What the walk did and last saw. NOT a statement about what is held.

    See the module docstring, property 3: a cancel request and a fill can cross,
    so `positions` (chunk 2) and reconciliation (spec §6) remain authoritative.
    """

    outcome: WalkOutcome
    external_identifier: str
    #: The LAST broker id in the replace chain — each reprice mints a new one.
    broker_order_id: int | None
    #: Reprices performed. 0 means it resolved at the gate-approved mid.
    steps: int
    requested_contracts: int
    #: Contracts the broker reported filled. `None` means it never said, which is
    #: distinct from 0 and must not be read as "nothing filled".
    filled_contracts: int | None
    final_limit_price: Decimal | None
    #: True if this loop asked for a cancel. The order may still have been filling.
    cancel_requested: bool
    #: Broker failures this walk recovered from. Non-zero with a normal outcome is
    #: the "disconnected then recovered" case.
    recovered_disconnects: int
    reason: str | None = None
    #: The broker's verbatim explanation, present only on a `REJECTED` outcome and
    #: `None` otherwise (never `""`). `reason` already carries a short summary —
    #: `ack.errors` on the first submit, a fixed string on a later sighting — but
    #: the broker's own prose lives on the `Order`, not the ack, so a rejection
    #: seen mid-walk would otherwise lose it. This puts it on the SAME surface
    #: (`WalkResult` → `entry_rejected` / `entry.finished`, `ExitReport`) that every
    #: other outcome's reason already reaches, so an operator need not correlate
    #: against a separate `order.event` record by hand. Nothing decides on it.
    reject_reason: str | None = None
    #: The broker's verbatim explanation of the MUTATION that failed, present only on
    #: a `NO_ORDER_EXISTS` outcome and `None` otherwise (never `""`). (2026-09-04)
    #:
    #: **A refusal to accept an order must not report as an absence.** `_recover`'s
    #: terminal branch has the causing exception in scope and threw it away, so a
    #: permanent `instrument_validation_failed: Trading of SPXW <symbol> is not
    #: supported` and a transient `timeout_error` arrived identically: the same
    #: `NO_ORDER_EXISTS`, the same fixed template `reason`, `reject_reason: null`.
    #: Measured 2026-09-02 (7 of 8) and 2026-09-03 (**5 of 7 entry attempts, 71%** —
    #: the day's dominant failure mode), by which point it also cost the exit path a
    #: send from `_EXIT_SEND_ATTEMPTS`' budget of three.
    #:
    #: Named for the common case; it carries a failed REPRICE too, because `_recover`
    #: is shared and reachable from both. `reason` names which (`"submit failed …"` /
    #: `"replace failed …"`), and the two are always written together.
    #:
    #: **Nothing decides on it** — `Order.reject_reason`'s rule, inherited whole. The
    #: outcome, the retry set and the one-attempt rule are all unchanged by this
    #: field's presence or its contents; it is what an operator reads, never what the
    #: code branches on. Distinguishing permanent from transient is a separate,
    #: larger decision and must not be built by matching this text.
    mutation_error: str | None = None
    last_status: OrderStatus | None = None

    @property
    def partially_filled(self) -> bool:
        return (
            self.outcome is WalkOutcome.FILLED
            and self.filled_contracts is not None
            and 0 < self.filled_contracts < self.requested_contracts
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "external_identifier": self.external_identifier,
            "broker_order_id": self.broker_order_id,
            "steps": self.steps,
            "requested_contracts": self.requested_contracts,
            "filled_contracts": self.filled_contracts,
            "final_limit_price": (
                float(self.final_limit_price) if self.final_limit_price is not None else None
            ),
            "cancel_requested": self.cancel_requested,
            "recovered_disconnects": self.recovered_disconnects,
            "reason": self.reason,
            "reject_reason": self.reject_reason,
            "mutation_error": self.mutation_error,
            "last_status": str(self.last_status) if self.last_status is not None else None,
        }


# -- observing the broker -----------------------------------------------------


@runtime_checkable
class OrderView(Protocol):
    """Read-only access to what the broker says about an order.

    A seam, because the production answer will not always come from a poll. The
    `AlertStreamer` already pushes a `PlacedOrder` on every status change (chunk 2
    consumes it for fills), so chunk 5 can back this with the stream and drop the
    polling entirely without touching the state machine. `LiveOrderView` is the
    implementation that works today and needs nothing wired.
    """

    async def by_id(self, broker_order_id: int) -> Order | None: ...

    async def by_identifier(self, external_identifier: str) -> list[Order]: ...


class LiveOrderView:
    """`OrderView` over `get_live_orders()`.

    One call answers both questions, so a recovery requery costs the same as a
    status poll. Goes through whatever port it is given — in production that is
    the throttled, audited stack, so these reads sit under the same governor as
    everything else.

    **An unmappable order matching the query RAISES; it never reads as absent**
    (2026-08-20). Both methods answer a question the walk acts on — "is my order
    still out there?" and "what exists under my key after that call failed?" — and
    for both of them, silently omitting an order the broker DOES hold is the
    dangerous direction. `by_id` omitting it makes a live order look cancelled;
    `by_identifier` omitting it makes `_recover` conclude nothing was sent. So an
    unmappable order carrying the id or the identifier being asked about comes
    back as `UnmappableOrderError`, which `_observe`/`_recover` already treat as a
    broker that cannot answer: retried, then escalated to `BrokerUnavailable`,
    which aborts the walk and requests a cancel. That is bounded, loud, and leaves
    the order to the watchdog and the next reconcile — all of which beat repricing
    or re-sending against a state nobody read.

    Unmappable orders that match NEITHER are none of this view's business — they
    belong to another trade idea or another instrument, and reconciliation is what
    answers for them.
    """

    def __init__(self, execution: OrderExecutionPort) -> None:
        self._execution = execution

    async def by_id(self, broker_order_id: int) -> Order | None:
        live = await self._execution.get_live_orders()
        for unreadable in live.unmappable:
            if unreadable.broker_order_id == broker_order_id:
                raise UnmappableOrderError(
                    f"broker order {broker_order_id} is present but unreadable "
                    f"(status={unreadable.raw_status!r}): {unreadable.reason}"
                )
        for order in live.orders:
            if order.broker_order_id == broker_order_id:
                return order
        return None

    async def by_identifier(self, external_identifier: str) -> list[Order]:
        live = await self._execution.get_live_orders()
        unreadable = live.unmappable_by_identifier(external_identifier)
        if unreadable:
            raise UnmappableOrderError(
                f"{len(unreadable)} order(s) under {external_identifier} are present "
                f"but unreadable: {unreadable[0].reason}"
            )
        return [
            order
            for order in live.orders
            if order.external_identifier == external_identifier
        ]


# -- the state machine --------------------------------------------------------


@dataclass
class _Walk:
    """Mutable per-run state. Not exported — a walk is a call, not an object."""

    sent: Order                    # the order AS ARGUS BUILT IT (carries candidate_id)
    walk: PriceWalk
    broker_order_id: int | None = None
    steps: int = 0
    cancel_requested: bool = False
    recovered: int = 0
    last: Order | None = None
    #: Consecutive cycles that produced no observation. A walk is otherwise
    #: bounded by its ladder — it reprices a fixed number of times and then walks
    #: away — but a cycle that sees nothing never reprices, so a broker that
    #: intermittently omits the order from its answer would loop forever.
    blind_cycles: int = 0
    #: Why the identifier-wide fill check could not be answered, or None. Set once,
    #: in the terminating frame, and read only to qualify the reason that frame
    #: reports — see `_finish`'s probe block and `_unfilled_reason`.
    probe_failed: str | None = None


class PriceWalkingLoop:
    """Submit, hold, reprice, walk away. One trade idea per `run()` call.

    `execution` must be an `AuditedOrderExecution` constructed WITH a store, and
    that is enforced at construction rather than documented. Chunk 1's raw adapter
    writes no audit record before the wire and no ledger row after it, so a walk
    driving it directly would place orders that are invisible to the audit trail
    and — worse, because it fails silently and late — unaccounted for by the next
    restart's order reconciliation, which hard-halts sandbox/live on an order it
    cannot explain. That failure surfaces the morning after, in the one component
    whose job is to refuse to guess.
    """

    def __init__(
        self,
        *,
        execution: AuditedOrderExecution,
        audit: AuditLog,
        policy: WalkPolicy,
        guard: DuplicateOrderGuard | None = None,
        view: OrderView | None = None,
        reconnect: ReconnectFn | None = None,
        drift_check: DriftCheckFn | None = None,
        halt_check: HaltCheckFn | None = None,
        alert: AlertFn | None = None,
        stop: asyncio.Event | None = None,
        sleep: SleepFn | None = None,
        clock: ClockFn | None = None,
        residuals: ResidualOrderRecheck | None = None,
        guard_timeout_s: float | None = None,
    ) -> None:
        if not isinstance(execution, AuditedOrderExecution):
            raise TypeError(
                "PriceWalkingLoop requires an AuditedOrderExecution — every order "
                "this loop places must be recorded before it reaches the wire"
            )
        if not execution.records_orders:
            raise ValueError(
                "AuditedOrderExecution must be constructed with store= : without "
                "the order ledger, every order this loop places is unaccounted for "
                "at the next restart and order reconciliation hard-halts on it"
            )
        self._execution = execution
        self._audit = audit
        self._policy = policy
        self._guard = guard
        self._view = view if view is not None else LiveOrderView(execution)
        self._reconnect = reconnect
        # OPTIONAL, and None is the shipped-before-this behaviour exactly: no check,
        # no call, a walk bounded only by its ladder. Every existing caller that
        # omits it is unchanged, which is what makes this a bolt-on.
        self._drift_check = drift_check
        # OPTIONAL for the same reason, and with the same "None == exactly the old
        # behaviour" guarantee. Wired for ENTRY walks only — see `HaltCheckFn`: a
        # kill must stop new risk without cancelling the order closing a position.
        self._halt_check = halt_check
        # Defaults to `log_alert`, NOT to silence — see `FillConsumer` for the same
        # change and the same reason. The `max_recovery_attempts`-exhausted path
        # (`BrokerUnavailable` -> `walk_unresolved`) is documented as alerting; with a
        # None default, a caller that omitted the argument got no alert at all on the
        # one outcome that means "a human must go look". `argus.notify` now provides
        # the real sink and `Application.startup` wires it process-wide.
        self._alert = alert or log_alert
        self._stop = stop
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        # OPTIONAL, and None is exactly the pre-P5 behaviour: a walk that
        # ends with an order still working simply leaves it, as it always did.
        # Production always passes one (`session._build_trading_side`), because that
        # order is the kill switch's stated guarantee going quietly unmet — see
        # `execution/residual_orders.py` and `_register_residual` below.
        self._residuals = residuals
        # OPTIONAL, and None is EXACTLY the pre-2026-09-03 behaviour: ONE guard
        # attempt, no bound of its own, whatever it raises propagating raw. Every
        # caller that omits it is unchanged — which matters because the only caller
        # that passes `guard_first_submission=True` is `ExitExecutor.close`, and the
        # test suite reaches this branch from a dozen places that route nothing.
        # Production passes `[execution] guard_timeout_s`, from `_build_loop`.
        self._guard_timeout_s = guard_timeout_s

    # -- entry point -------------------------------------------------------

    async def run(self, order: Order, *, guard_first_submission: bool) -> WalkResult:
        """Get `order` filled, or give up. Returns how it ended; see `WalkResult`.

        `guard_first_submission` has NO default, for the same reason `submit`'s
        `dry_run` has none: it must be stated. Pass True for a brand-new trade
        idea, which runs `DuplicateOrderGuard` once before the first submission.
        The guard is never consulted again inside this call — a price-walking
        replace re-sends the same `external_identifier` against its own still-
        working order, so a guard check there would find that order, correctly call
        it a conflict, and deadlock the loop against itself (see `guard.py`).

        A guard failure PROPAGATES rather than becoming an outcome. The guard is
        the only duplicate protection that exists, it raises when it cannot reach
        the broker, and turning "I could not check" into a returned value invites a
        caller to treat it as one more not-filled case and try again. Since
        2026-09-03 what propagates is a TYPED, BOUNDED `GuardUnreachable` — see
        there; the "not an outcome" half is unchanged, and callers are expected to
        catch it and send nothing rather than to let it reach a halt.

        **The guard is asked BEFORE `walk.started` is recorded**, and the ordering is
        load-bearing rather than incidental. A question asked before anything is sent
        did not start a walk, and on 2026-09-03 the trail carried the proof of what
        that costs a reader: `walk.started: 11` against `walk.finished: 10`, with the
        missing one being the walk that never happened. `walk.started` now means a
        walk actually began, and every `BLOCKED` still has its pair (the record is
        above the `blocked` return, deliberately).
        """
        if order.broker_order_id is not None:
            raise ValueError(
                f"run() takes an UNSENT order; this one already has broker id "
                f"{order.broker_order_id}"
            )
        state = _Walk(
            sent=order,
            # `order.width` is read off the order's own OCC symbols, so the walk stays
            # market-free and a rung is still computable in a dead book (2026-09-17).
            walk=PriceWalk.from_policy(
                order.limit_price, self._policy, width=order.width
            ),
        )
        verdict = await self._ask_guard(order) if guard_first_submission else None
        await self._audit.record(
            "walk.started",
            {
                **order.to_payload(),
                "policy": self._policy_payload(),
                "walk_away_price": float(state.walk.floor),
                "guard_first_submission": guard_first_submission,
                # Whether the drift breaker is armed for this walk. Recorded because
                # its absence is invisible otherwise: a walk with no check looks
                # identical in the trail to one whose check never fired.
                "drift_check_armed": self._drift_check is not None,
                # Same reason as above, and it is the one field that distinguishes
                # an entry walk from an exit walk in the trail: only entries arm the
                # kill-switch abort. A walk that ran on after a trip and one that was
                # never armed look identical without it.
                "halt_check_armed": self._halt_check is not None,
            },
        )

        if verdict is not None and verdict.blocked:
            # The guard already wrote order.duplicate_blocked (Tier 1) with the
            # conflicting orders; this records only how the walk ended.
            return await self._finish(state, WalkOutcome.BLOCKED, reason=verdict.reason)

        try:
            return await self._walk(state)
        except BrokerUnavailable as exc:
            self._notify("walk_unresolved", {
                "external_identifier": order.external_identifier, "error": str(exc)
            })
            # `probe=False`: `_recover` just spent `max_recovery_attempts` on this
            # exact `by_identifier` call and failed every one. One more is not a
            # different question, it is the same one the broker has been refusing.
            return await self._finish(
                state, WalkOutcome.UNRESOLVED, reason=str(exc), probe=False
            )

    # -- the duplicate guard -----------------------------------------------

    async def _ask_guard(self, order: Order) -> ConflictVerdict:
        """Ask THE guard, retrying a broker that cannot be reached. (2026-09-03)

        Returns a `ConflictVerdict` or raises `GuardUnreachable`. There is no third
        answer and no path to a submit without the first — that is the whole of how
        the duplicate invariant survives this change. **The classification is
        untouched**: WORKING/FILLED block, DEAD allows, unclassifiable blocks, all of
        it `guard.py`'s and none of it reachable from here. This retries the
        QUESTION, never the verdict.

        Since 2026-09-24 the question carries `quantity=order.contracts`, which
        arms the guard's CLOSE-side quantity rule (and is ignored for OPEN
        identifiers). `_ask_guard` is the EXIT path's seam —
        `guard_first_submission=True` has one caller in `src/` — so the quantity
        rule reaches the broker question of exit walks only; the entry path's own
        guard burst in `entry._check_for_duplicate` stays binary and untouched.

        **The retry's scope is drawn by POSITION, not by exception type** — the rule
        `reconcile._ask_broker` established and `entry._check_for_duplicate` already
        follows. Only the guard await sits inside the `try`, so nothing else can be
        retried, and no type list has to keep up with whichever transport error the
        SDK raises this month (a raw `httpcore2.ReadTimeout` escaped unwrapped in the
        2026-08-26 traceback, and 2026-09-03's was a `TastytradeError` wrapping an
        nginx 502 page).

        **One type IS load-bearing, and it is the same one.** `has_conflicting_order`
        writes Tier 1 `order.duplicate_blocked` from inside itself, so an
        `AuditWriteError` can surface from what looks like a pure read. A dead
        recorder halts the process and must never be re-attempted (CLAUDE.md), so it
        is re-raised untouched, before anything else.

        **`guard_timeout_s=None` keeps the pre-2026-09-03 behaviour exactly**: one
        attempt, no bound, the raw exception out. Every test double and every caller
        that does not opt in is byte-identical; production opts in through
        `ExitExecutor._build_loop`.
        """
        if self._guard is None:
            raise ValueError(
                "guard_first_submission=True requires a DuplicateOrderGuard — "
                "the broker does not enforce external_identifier uniqueness, so "
                "there is no other duplicate protection"
            )
        identifier = order.external_identifier
        if self._guard_timeout_s is None:
            return await self._guard.has_conflicting_order(
                identifier, quantity=order.contracts
            )

        timeout_s = self._guard_timeout_s
        attempts = 0
        last: BaseException | None = None
        for attempt in range(1, _GUARD_ATTEMPTS + 1):
            if attempt > 1 and self._stop is not None and self._stop.is_set():
                # Stopped during the previous attempt or its backoff. Asking again
                # after being told to stop is the round trip nobody wants, and it is
                # the same early-out `entry._check_for_duplicate` takes.
                break
            attempts = attempt
            try:
                # Applied HERE rather than pushed into the port, exactly as
                # `entry._check_for_duplicate` and `ReconcileRetry.request_timeout_s`
                # do: the execution port is narrow by design and a timeout argument on
                # every method is the wrong seam. Safe in the way it explicitly is NOT
                # for the account refresher — this is an async httpx call on the loop,
                # so cancelling it really cancels the request rather than abandoning a
                # running worker thread.
                async with asyncio.timeout(timeout_s):
                    return await self._guard.has_conflicting_order(
                        identifier, quantity=order.contracts
                    )
            except AuditWriteError:
                raise
            except TimeoutError as exc:
                # `asyncio.timeout` raises a MESSAGELESS `TimeoutError`. Keep the type
                # (`error_type` readers match on it) and spell out what was waited for.
                last = exc if str(exc) else TimeoutError(
                    f"duplicate-guard check for {identifier} did not complete "
                    f"within {timeout_s:g}s"
                )
                self._note_guard_attempt(identifier, attempt, last)
            except Exception as exc:  # noqa: BLE001 - see the docstring
                last = exc
                self._note_guard_attempt(identifier, attempt, exc)
            if attempt < _GUARD_ATTEMPTS:
                await self._backoff(_guard_backoff_s(attempt))
        assert last is not None  # the loop reaches here only having failed
        raise GuardUnreachable(attempts, last) from last

    def _note_guard_attempt(
        self, identifier: str, attempt: int, exc: BaseException
    ) -> None:
        """Tier 2 per failed attempt, mirroring `entry.guard_unreachable_attempt`.

        Tier 2 because the burst has not given up yet and most bursts recover; a
        Tier 1 row per attempt would put three on the log for a blip that cost
        nothing. GIVING UP is the crash-relevant fact and it is Tier 1, recorded by
        the CALLER — `ExitExecutor` — because only the caller knows whether this is
        the first episode or the two-hundredth.
        """
        logger.warning(
            "duplicate-guard check attempt %d/%d failed for %s: %s: %s",
            attempt, _GUARD_ATTEMPTS, identifier, type(exc).__name__, exc,
        )
        self._audit.telemetry(
            "walk.guard_unreachable_attempt",
            {
                "external_identifier": identifier,
                "attempt": attempt,
                "attempts": _GUARD_ATTEMPTS,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )

    async def _backoff(self, seconds: float) -> None:
        """Wait between guard attempts, returning the instant the walk is stopped.

        A bare sleep would sit on a session close for the length of the ladder. Same
        mechanism as `entry._backoff` — a race rather than a `wait_for` timeout, so
        the injected `sleep` seam is what the suite drives.
        """
        if self._stop is None:
            await self._sleep(seconds)
            return
        stopped = asyncio.ensure_future(self._stop.wait())
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        try:
            await asyncio.wait({stopped, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopped.cancel()
            sleeper.cancel()

    # -- the cycle ---------------------------------------------------------

    async def _walk(self, state: _Walk) -> WalkResult:
        resolved = await self._first_submit(state)
        if resolved is not None:
            return resolved

        while True:
            if (halt := self._stop_reason()) is not None:
                return await self._abort(state, halt)

            # (1) The cadence clock does not start until the order is genuinely in
            # the market — after the first submit AND after every reprice, since a
            # replace mints a new order that has to get there on its own.
            resolved = await self._await_in_market(state)
            if resolved is not None:
                return resolved

            # (2) Hold the price for a full cycle — never less than the auction.
            await self._hold(state)
            if (halt := self._stop_reason()) is not None:
                return await self._abort(state, halt)

            # (3) Ask what happened, then act on what the broker actually says.
            observed = await self._observe(state)
            resolved = await self._resolve(state, observed)
            if resolved is not None:
                return resolved
            if observed is None:
                # The order was not in the broker's answer. That is a read gap, not
                # a verdict, and repricing an order we cannot see would be acting
                # on an assumption. Go round again — but not forever: a cycle with
                # no observation never reprices, so the ladder cannot bound it.
                state.blind_cycles += 1
                if state.blind_cycles >= self._policy.max_recovery_attempts:
                    self._notify("walk_order_kept_disappearing", {
                        "external_identifier": state.sent.external_identifier,
                        "broker_order_id": state.broker_order_id,
                        "cycles": state.blind_cycles,
                    })
                    return await self._finish(
                        state, WalkOutcome.UNRESOLVED,
                        reason=f"order absent from {state.blind_cycles} consecutive answers",
                    )
                continue
            state.blind_cycles = 0

            # (4) Still working — but is it still the same market? The ladder below
            # steps from a reference fixed at gate time, so this is the cycle
            # boundary where that reference gets challenged. Deliberately AFTER
            # `_resolve`: a fill or a terminal status observed in this same cycle
            # wins outright, because cancelling something that already filled is a
            # far worse mistake than walking a price one cycle too long. Equally
            # deliberately after the blind-cycle branch: an order we could not read
            # gets no verdict of any kind, drift included.
            resolved = await self._check_drift(state)
            if resolved is not None:
                return resolved

            # (5) Still working and still worth stepping: reprice.
            resolved = await self._reprice(state)
            if resolved is not None:
                return resolved

    async def _first_submit(self, state: _Walk) -> WalkResult | None:
        """Place the order. Returns a result only if the walk is already over."""
        try:
            ack = await self._execution.submit(state.sent, dry_run=False)
        except Exception as exc:
            # The outcome is genuinely unknown — a timeout can mean it routed. Ask
            # the broker what exists under this identifier rather than assuming
            # either way, and never re-send blind.
            logger.warning("submit failed for %s: %s", state.sent.external_identifier, exc)
            state.recovered += 1
            return await self._recover(state, what="submit", exc=exc)

        placed = ack.order
        state.last = placed
        if placed.status is OrderStatus.REJECTED:
            # A refusal, not a slow fill. Stepping the price of a rejected order
            # would send a second order the broker will refuse the same way.
            return await self._finish(
                state, WalkOutcome.REJECTED,
                reason="; ".join(ack.errors)[:200] or "broker rejected the order",
            )
        if placed.broker_order_id is None:
            # Only a dry run mints no id (measured, chunk 1), and this call is not
            # one. Nothing to walk and nothing to cancel — refuse to guess.
            raise BrokerUnavailable(
                "submission ack carried no broker order id; cannot track this order"
            )
        state.broker_order_id = placed.broker_order_id
        return None

    async def _await_in_market(self, state: _Walk) -> WalkResult | None:
        """Block until the order is confirmed working at the exchange.

        Polls rather than sleeping a fixed amount: the whole point is that "how
        long until it is live" is not something we get to assume. A fill or a
        terminal status seen here short-circuits — an order that filled inside the
        first auction never needs the cadence at all.
        """
        deadline = self._clock() + self._policy.working_timeout_s
        seen = False
        while True:
            observed = await self._observe(state)
            if observed is not None:
                seen = True
                if is_in_market(observed.status) or _reported_fill(observed):
                    # A fill is proof of exposure even if we never caught `Live`.
                    return None
                if observed.is_terminal:
                    return await self._resolve(state, observed)
            if self._clock() >= deadline:
                break
            if (halt := self._stop_reason()) is not None:
                return await self._abort(state, halt)
            await self._sleep(self._policy.status_poll_interval_s)

        if not seen:
            # Submitted, acknowledged with an id, and then invisible. That is not
            # evidence it never worked — it is no evidence at all, and the one
            # thing this loop must not do is decide which.
            self._notify("walk_order_not_visible", {
                "external_identifier": state.sent.external_identifier,
                "broker_order_id": state.broker_order_id,
            })
            return await self._finish(
                state, WalkOutcome.UNRESOLVED, reason="order_not_visible_at_broker"
            )
        # Seen, but never in the market — the measured pre-market case. It must be
        # cancelled, not left: `tif.next_valid_session` means it goes live at the
        # next open, unattended, at a price nobody is walking.
        await self._request_cancel(state)
        return await self._finish(
            state, WalkOutcome.NEVER_WORKED,
            reason=f"no confirmed in-market status within {self._policy.working_timeout_s:g}s",
        )

    async def _hold(self, state: _Walk) -> None:
        wait = self._policy.cycle_wait_s
        self._audit.telemetry(
            "walk.holding",
            {
                "external_identifier": state.sent.external_identifier,
                "broker_order_id": state.broker_order_id,
                "limit_price": float(state.sent.limit_price),
                "step": state.steps,
                "wait_s": wait,
                "coa_settle_s": self._policy.coa_settle_s,
            },
        )
        await self._sleep(wait)

    async def _resolve(self, state: _Walk, observed: Order | None) -> WalkResult | None:
        """Read one observation. Returns a result if the walk is over, else None."""
        if observed is None:
            # Vanished between cycles. Treated as a transient read gap, not a
            # verdict: the recovery path is the only thing allowed to conclude
            # anything from an absence, and it needs more than one look.
            return None
        state.last = observed

        if observed.is_terminal:
            disposition = classify_order(observed)
            if disposition is OrderDisposition.FILLED:
                return await self._finish(state, WalkOutcome.FILLED)
            # This RUNG is terminal with nothing filled. That is not yet a verdict
            # about the SPREAD — a walk that has repriced holds one of several
            # orders sharing one `external_identifier`, and the fill can be on any
            # of them. Ask before concluding (2026-09-02) — and since 2026-09-10 the
            # asking is `_finish`'s, for every outcome that claims a non-fill rather
            # than for these two alone. This branch chooses between REJECTED and
            # CANCELLED_ELSEWHERE and passes its BASE reason; the gate composes.
            if observed.status is OrderStatus.REJECTED:
                return await self._finish(
                    state, WalkOutcome.REJECTED, reason="broker rejected the order",
                )
            # Every cancel this loop requests is immediately followed by a
            # `_finish`, so a terminal order seen HERE was never one we cancelled.
            return await self._finish(
                state, WalkOutcome.CANCELLED_ELSEWHERE,
                reason=f"terminated at the broker as {observed.status}",
            )

        filled = _reported_fill(observed)
        if filled:
            # A partial on a still-working order. Because a vertical is ONE
            # multi-leg order, a partial fill is a SMALLER SPREAD, not a naked leg
            # — so the position is defined-risk and safe to keep.
            #
            # The remainder is cancelled rather than walked. What tastytrade's
            # replace does to a partially-filled order — whether the new order
            # carries the original quantity or the remainder — has not been
            # measured, and the way to be wrong about it is to re-request the full
            # size and end up with double the intended position. Chunk 2 refused to
            # guess at broker behaviour and so does this; if chasing the remainder
            # is ever wanted, measure the replace semantics first.
            await self._request_cancel(state)
            return await self._finish(
                state, WalkOutcome.FILLED, reason="partial fill; remainder cancelled"
            )
        return None

    async def _check_drift(self, state: _Walk) -> WalkResult | None:
        """Ask whether the ladder's reference still describes the market.

        Returns a result only when the walk is being abandoned. The order is
        CANCELLED first, for the same reason the walk-away path cancels: an order
        left working at a price this loop has stopped standing behind is exactly
        the unattended-order case the rest of the arc is arranged to prevent.

        **A check that raises abandons the walk rather than propagating.** The
        check is in-memory arithmetic over an already-received quote, so an
        exception is a defect in the injected callable, not a broker problem — and
        letting it propagate would hand it to chunk 5's hard-halt region and take
        the process down. Halting exists to stop a live order being left with nobody
        walking it; this path cancels the order, so the condition that justifies a
        halt does not arise. Fail closed on the walk, loudly, and keep the process.
        """
        if self._drift_check is None:
            return None
        try:
            reason = self._drift_check()
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.error(
                "drift check raised for %s: %s", state.sent.external_identifier, exc,
                exc_info=exc,
            )
            self._notify("walk_drift_check_failed", {
                "external_identifier": state.sent.external_identifier,
                "broker_order_id": state.broker_order_id,
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            })
            reason = f"drift check raised {type(exc).__name__}: {exc}"
        if reason is None:
            return None
        await self._request_cancel(state)
        return await self._finish(state, WalkOutcome.DRIFTED, reason=reason[:500])

    async def _reprice(self, state: _Walk) -> WalkResult | None:
        price = state.walk.price_at(state.steps + 1)
        if price is None:
            await self._request_cancel(state)
            return await self._finish(
                state, WalkOutcome.WALKED_AWAY,
                reason=f"walk-away threshold reached at {state.walk.floor}",
            )

        # `replace`, not cancel-then-submit. The SDK's replace is the atomic
        # cancel-and-resubmit; doing it by hand opens a window in which the old
        # order is cancelled and the new one is not yet placed — a gap where the
        # spread is unquoted and, worse, where a crash leaves nothing working and
        # nothing recorded as intended.
        assert state.broker_order_id is not None
        next_sent = state.sent.with_limit_price(price)
        try:
            replaced = await self._execution.replace(state.broker_order_id, next_sent)
        except Exception as exc:
            logger.warning("replace failed for %s: %s", state.sent.external_identifier, exc)
            state.recovered += 1
            # The replace may or may not have taken. `_recover` asks the broker;
            # it never re-sends.
            state.sent = next_sent
            state.steps += 1
            return await self._recover(state, what="replace", exc=exc)

        if replaced.broker_order_id is None:
            raise BrokerUnavailable("replace response carried no broker order id")
        state.sent = next_sent
        state.broker_order_id = replaced.broker_order_id
        state.steps += 1
        state.last = replaced
        self._audit.telemetry(
            "walk.step",
            {
                "external_identifier": state.sent.external_identifier,
                "broker_order_id": replaced.broker_order_id,
                "replaces_order_id": replaced.replaces_order_id,
                "step": state.steps,
                "limit_price": float(price),
                "walk_away_price": float(state.walk.floor),
            },
        )
        return None

    # -- broker interaction helpers ---------------------------------------

    async def _observe(self, state: _Walk) -> Order | None:
        """Current broker state for the order being walked. Reads are retryable."""
        assert state.broker_order_id is not None
        order_id = state.broker_order_id
        attempts = 0
        while True:
            try:
                return await self._view.by_id(order_id)
            except Exception as exc:
                attempts += 1
                state.recovered += 1
                await self._record_disconnect(state, what="observe", exc=exc, attempt=attempts)
                if attempts >= self._policy.max_recovery_attempts:
                    raise BrokerUnavailable(
                        f"could not read order {order_id} after {attempts} attempts: {exc}"
                    ) from exc
                await self._reconnect_and_back_off(attempts)

    async def _request_cancel(self, state: _Walk) -> None:
        """Ask the broker to cancel. Best-effort, and deliberately not fatal.

        A cancel that raises must not turn a decided outcome (walked away, aborted,
        partially filled) into an exception — the decision stands either way, and
        the working order it failed to cancel is exactly what restart-time order
        reconciliation exists to catch. `cancel_requested` records that we ASKED;
        the order is at `Cancel Requested` at best, and may still fill.
        """
        if state.broker_order_id is None:
            return
        try:
            await self._execution.cancel(state.broker_order_id)
            state.cancel_requested = True
        except Exception as exc:
            state.recovered += 1
            logger.error(
                "cancel failed for order %s (%s): %s",
                state.broker_order_id, state.sent.external_identifier, exc,
            )
            self._notify("walk_cancel_failed", {
                "broker_order_id": state.broker_order_id,
                "external_identifier": state.sent.external_identifier,
                "error": str(exc)[:200],
            })

    async def _recover(self, state: _Walk, *, what: str, exc: Exception) -> WalkResult | None:
        """A mutation failed. Ask the broker what exists, then resume from that.

        Never re-sends the failed call. `external_identifier` is what makes this
        possible: the same trade idea always carries the same key, so the broker
        can be asked "what do you have under this?" without Argus needing to
        remember what it managed to send (chunk 1).
        """
        await self._record_disconnect(state, what=what, exc=exc, attempt=1)
        await self._reconnect_and_back_off(1)

        identifier = state.sent.external_identifier
        attempts = 0
        while True:
            try:
                candidates = await self._view.by_identifier(identifier)
                break
            except Exception as read_exc:
                attempts += 1
                if attempts >= self._policy.max_recovery_attempts:
                    raise BrokerUnavailable(
                        f"{what} failed and the broker could not be re-queried "
                        f"after {attempts} attempts: {read_exc}"
                    ) from read_exc
                await self._reconnect_and_back_off(attempts + 1)

        working = [o for o in candidates if classify_order(o) is OrderDisposition.WORKING]
        # Shared with `_finish`'s probe block since 2026-09-02, and
        # behaviour-identical to the `filled[0]` this replaced: "which order counts as
        # having left a position" is one question and must have one implementation, or
        # the two paths that ask it drift.
        filled = _first_filled(candidates)

        if filled is not None:
            state.last = filled
            state.broker_order_id = filled.broker_order_id
            return await self._finish(
                state, WalkOutcome.FILLED, reason=f"discovered filled while recovering from {what}"
            )

        if len(working) > 1:
            # Two live orders under one identifier. The broker does not enforce
            # uniqueness (measured, chunk 1), so this is reachable, and it is the
            # runaway case spec §9 names: both can fill and the position doubles.
            # Cancel everything and stop. Missing an entry costs a trade; an
            # unmanaged duplicate costs the size of the account's risk limit.
            logger.error(
                "%d working orders under %s after a failed %s — cancelling all",
                len(working), identifier, what,
            )
            self._notify("walk_duplicate_working_orders", {
                "external_identifier": identifier,
                "broker_order_ids": [o.broker_order_id for o in working],
            })
            for order in working:
                if order.broker_order_id is not None:
                    state.broker_order_id = order.broker_order_id
                    await self._request_cancel(state)
            # `probe=False`: the `by_identifier` read a few statements up ANSWERED,
            # and that answer IS this verdict. Re-asking it would be the same
            # question, not an additive one.
            return await self._finish(
                state, WalkOutcome.UNRESOLVED,
                reason=f"{len(working)} working orders share this external_identifier",
                probe=False,
            )

        if working:
            # Exactly one working order — whether the mutation landed or not, THIS
            # is the order that exists, so the walk continues against it, at the
            # price IT carries. Re-deriving the step counter from the broker's
            # limit price is the point: a failed replace leaves the local counter
            # claiming a rung the order may never have reached, and every
            # subsequent price would be computed from a fiction.
            live = working[0]
            state.broker_order_id = live.broker_order_id
            state.last = live
            state.sent = state.sent.with_limit_price(live.limit_price)
            step = state.walk.step_of(live.limit_price)
            if step is not None:
                state.steps = step
            logger.info(
                "resumed %s against order %s at %s (step %d) after a failed %s",
                identifier, state.broker_order_id, live.limit_price, state.steps, what,
            )
            return None

        # Nothing working and nothing filled. Whether the call never reached the
        # broker or reached it and died is not decidable from here, and the two
        # differ by an order. Say so, and let the caller (which can re-run the
        # guard) decide — that is the whole reason the guard asks the broker.
        #
        # Until 2026-08-28 this said so as `UNRESOLVED`, which is the one outcome
        # meaning "nobody read the broker" — so the caller could not act on the very
        # distinction this branch exists to draw, and the exit path treated a
        # confirmed-empty broker as an unreadable one. `NO_ORDER_EXISTS` carries the
        # provenance the comment above always assumed the caller would have.
        # The causing exception has been in scope this whole function and was thrown
        # away until 2026-09-04. It is the ONLY place the broker's own account of why
        # nothing exists is available — `by_identifier` above can only report the
        # absence, never its cause — so an operator got "no order exists" for a
        # permanent instrument refusal and for a transient timeout alike.
        # `probe=False`: the same read, positively empty, IS the whole basis of this
        # outcome — `NO_ORDER_EXISTS` means the broker was asked and had nothing.
        # Asking again would be re-deriving the outcome from itself.
        return await self._finish(
            state, WalkOutcome.NO_ORDER_EXISTS,
            reason=f"{what} failed and no order exists under this identifier",
            mutation_error=_broker_text(exc),
            probe=False,
        )

    async def _reconnect_and_back_off(self, attempt: int) -> None:
        if self._reconnect is not None:
            try:
                await self._reconnect()
            except Exception as exc:
                logger.warning("reconnect attempt %d failed: %s", attempt, exc)
        index = min(attempt, len(_RECOVERY_BACKOFF_S)) - 1
        await self._sleep(_RECOVERY_BACKOFF_S[index])

    # -- termination -------------------------------------------------------

    async def _abort(self, state: _Walk, reason: str = "stop requested") -> WalkResult:
        """The caller asked to stop, or a halt was ordered. Cancel, then report.

        `reason` comes from `_stop_reason` and is carried into `walk.finished`, so
        a session-close abort and a kill-switch abort are distinguishable in the
        trail rather than collapsed into one `ABORTED`.

        A walk is abandoned at CYCLE GRANULARITY — the check runs around the hold,
        not inside it — so an abort takes up to one `cycle_wait_s` to take effect.
        For the session close that is merely fine. For the kill switch it is the
        stated bound on the phone button: the gate refuses new entries the instant
        the switch flips (`risk/gate.py`), while an order already at the exchange is
        cancelled within one cycle. Tightening it would mean interrupting `_hold`,
        which would cancel inside the COA window and race a straggler auction
        response — the exact failure `cycle_wait_s` exists to prevent.
        """
        await self._request_cancel(state)
        return await self._finish(state, WalkOutcome.ABORTED, reason=reason)

    async def _finish(
        self,
        state: _Walk,
        outcome: WalkOutcome,
        *,
        reason: str | None = None,
        mutation_error: str | None = None,
        probe: bool = True,
    ) -> WalkResult:
        """Build the one `WalkResult` this walk returns — asking first, if it claims
        a non-fill. (2026-09-10)

        **This is the sole constructor of a `WalkResult` in this module**, which is
        the whole reason the identifier-wide FILLED read lives here. Every path out
        of a walk goes through this frame, so *no `WalkResult` may report a non-fill
        without the identifier having been asked* is an INVARIANT rather than a
        habit — and the habit is what failed, three times, each one a branch the rule
        was applied one short of (`reconcile._ask_broker` and
        `entry._check_for_duplicate` on 2026-08-26, `_resolve`'s terminal branch on
        2026-09-02, and the deadline branches until now). `_ASK_BEFORE_CONCLUDING`
        carries the classification and the argument for it.

        `probe` defaults to True and MUST stay that way: the default is the safe
        side, so a call site that forgets it asks one unnecessary question rather
        than skipping a necessary one. Pass False only where `by_identifier` has just
        been read and that read IS the verdict — the three sites that do, and nowhere
        else.

        The probe's own contract is 2026-09-02's, unweakened: ONE attempt, no retry
        and no reconnect (`_observe`'s ladder is for reads the loop cannot continue
        without; this one's absence is a no-op, and retrying would put up to
        `sum(_RECOVERY_BACKOFF_S)` of sleeping on the EXIT path's terminating frame
        while a triggered 0DTE position is open); a failure FALLS BACK to the verdict
        the caller already had rather than manufacturing `UNRESOLVED` out of a read
        that did not happen; FILLED only, never "what else is out there", which would
        re-open the double-send reasoning `_await_settled` exists to hold; and
        `_first_filled` stays the one shared definition of "which order counts as
        having left a position".

        It runs AFTER `_request_cancel` on every path that cancels, which is what
        falls out of living here. That ordering is right on its own terms: cancelling
        the current rung is correct regardless of what a prior rung did, and probing
        last makes the answer as fresh as the frame allows. A cancel cannot hide a
        fill on a different rung — and it can REVEAL one, which is not a theory:
        hoist this above the cancel on the `WALKED_AWAY` path and the fill that
        crossed with that cancel is missed, which is what
        `test_a_fill_that_crosses_the_cancel_is_reported_and_not_crossed` catches.

        Two more properties of the read, inherited whole from 2026-09-02:

        - **STATE-BASED, never the prose.** The input is `classify_order` over what
          the broker says exists under the identifier. `reject_reason` is read only to
          be QUOTED into the reason — never matched, never parsed, not even with a
          `startswith`. Broker prose changes without notice and a rail built on it
          fails open the day it does (`Order.reject_reason`: *"Nothing decides on
          it"*).
        - **UNGATED on `state.steps`.** The broker does not enforce
          `external_identifier` uniqueness (measured, chunk 1), and there are live
          paths that put a second order under one key at step 0 — `_recover` resuming
          against an order a "failed" submit actually landed, and the exit retry
          re-running a whole walk under the same `argus-close-*` key. Gating an
          identifier-wide question on a rung-scoped counter would repeat the exact
          category error this exists to correct.

        **It cannot see a fill from an EARLIER walk.** Those same two paths are the
        only ones that put more than one order under one key. `_recover` is within one
        walk and is the condition this exists for; the exit retry is safe because of
        what LICENSES it — `_RETRYABLE_EXITS` holds only `NOTHING_AT_BROKER` and
        `CROSS_UNPRICEABLE`, and **both mean nothing filled** — so a walk that filled
        never produces either. That is a property of the retry taxonomy, not of this
        gate, and a member that could follow a PARTIAL fill would have it redone.

        **The block is INLINE and must stay inline.** Two names for one step is how
        this module ends up with two probe sites again, which is the shape both this
        fix and 2026-09-02's were written to remove.
        → docs/decisions/2026-09-10-never-worked-is-not-a-verdict.md
        """
        if probe and outcome in _ASK_BEFORE_CONCLUDING:
            try:
                candidates = await self._view.by_identifier(
                    state.sent.external_identifier
                )
            except Exception as exc:  # noqa: BLE001 - falls back; see the docstring
                state.recovered += 1
                state.probe_failed = f"{type(exc).__name__}: {exc}"[:200]
                logger.warning(
                    "could not ask what exists under %s before concluding %s: %s",
                    state.sent.external_identifier, outcome, exc,
                )
                await self._record_disconnect(
                    state, what="terminal_probe", exc=exc, attempt=1
                )
            else:
                if (filled := _first_filled(candidates)) is not None:
                    # A TERMINAL last observation is 2026-09-02's shape (the broker
                    # said something conclusive about this rung); the deadline
                    # branches have no such observation, and their `terminal_*: null`
                    # is the SIGNATURE telling the two apart in the trail.
                    # `state.last` there is either absent or a NON-terminal sighting,
                    # and neither is the broker concluding anything.
                    terminal = (
                        state.last if _is_terminal_observation(state.last) else None
                    )
                    # Tier 2, and the Tier 1 `walk.finished` below carries the same
                    # correction in prose. This exists because the headline number in
                    # both incidents is a RATE and prose is not countable.
                    self._audit.telemetry(
                        "walk.filled_under_identifier",
                        {
                            "external_identifier": state.sent.external_identifier,
                            # Which verdict this read suppressed. Without it the two
                            # shapes collapse into one category and 2026-09-02's rate
                            # can no longer be read apart from this one.
                            "suppressed_outcome": str(outcome),
                            "terminal_broker_order_id": (
                                terminal.broker_order_id if terminal is not None else None
                            ),
                            "terminal_status": (
                                str(terminal.status) if terminal is not None else None
                            ),
                            "terminal_reject_reason": (
                                terminal.reject_reason if terminal is not None else None
                            ),
                            "filled_broker_order_id": filled.broker_order_id,
                            "filled_contracts": filled.reported_fill_quantity,
                            "steps": state.steps,
                        },
                    )
                    # BOTH, exactly as `_recover` does it. Moving the outcome without
                    # moving these leaves `filled_contracts` reporting the abandoned
                    # rung's 0 under a FILLED headline — a half-applied fix, and
                    # harder to spot than the bug it replaces.
                    #
                    # `state.sent` deliberately does NOT move: `final_limit_price` is
                    # read off it, and relabelling that as the filled rung's limit
                    # dresses one limit up as more fill-like than another — a limit is
                    # not a fill price, the `entry_credit` lesson from the same pass.
                    # `state.cancel_requested` does not move either. A cancel WAS
                    # requested, and the record should keep saying so.
                    state.last = filled
                    state.broker_order_id = filled.broker_order_id
                    outcome = WalkOutcome.FILLED
                    reason = _corrected_reason(reason, terminal, filled)
                    # Not a refusal any more. `mutation_error`'s contract is "present
                    # only on NO_ORDER_EXISTS" and the gate below enforces it — but
                    # the one caller that passes one also passes `probe=False`, so
                    # this is belt-and-braces rather than the thing under test.
                    mutation_error = None
            if outcome is not WalkOutcome.FILLED:
                reason = self._unfilled_reason(state, reason)
        # The converted FILLED is built HERE, by the one construction below, rather
        # than by re-entering `_finish`. `FILLED` is not in the set, so a re-entering
        # version would merely terminate — this makes it a fact rather than a
        # coincidence.
        result = WalkResult(
            outcome=outcome,
            external_identifier=state.sent.external_identifier,
            broker_order_id=state.broker_order_id,
            steps=state.steps,
            requested_contracts=state.sent.contracts,
            filled_contracts=(
                state.last.reported_fill_quantity if state.last is not None else None
            ),
            final_limit_price=state.sent.limit_price,
            cancel_requested=state.cancel_requested,
            recovered_disconnects=state.recovered,
            reason=reason,
            reject_reason=(
                state.last.reject_reason
                if state.last is not None and outcome is WalkOutcome.REJECTED
                else None
            ),
            # Gated on the OUTCOME exactly as `reject_reason` is one line above, and
            # for the same reason: a field whose contract says "present only on X"
            # must be unable to appear on anything else, whatever a caller passes.
            # The two gates are deliberately separate — a rejection and a refused
            # submit are different events and neither implies the other.
            mutation_error=(
                mutation_error if outcome is WalkOutcome.NO_ORDER_EXISTS else None
            ),
            last_status=state.last.status if state.last is not None else None,
        )
        # Tier 1: how a trade idea ended is the record everything downstream reads
        # — and for BLOCKED/UNRESOLVED/NEVER_WORKED it is the only place the reason
        # is written down.
        await self._audit.record(
            "walk.finished", {**result.to_payload(), "candidate_id": state.sent.candidate_id}
        )
        self._register_residual(state, outcome)
        logger.info(
            "walk %s: %s after %d step(s) at %s (%s)",
            state.sent.external_identifier, outcome, state.steps,
            state.sent.limit_price, reason or "-",
        )
        return result

    def _unfilled_reason(self, state: _Walk, base: str | None) -> str | None:
        """`base`, qualified when the identifier-wide check could not be answered.

        A verdict reached WITH the probe answered is about the spread; one reached
        with the probe unanswered is about the rung alone, and the operator reading
        the page is the person who has to know which. Same discipline as
        `_SETTLE_REFUSALS` in the watchdog: all of these refuse, and they are not the
        same event.

        Takes `str | None` because `_finish`'s `reason` is optional, and returns the
        base UNCHANGED when the probe was answered — a read that found nothing
        rewrites nothing.
        """
        if state.probe_failed is None:
            return base
        qualifier = (
            f"the identifier-wide fill check could not be answered "
            f"({state.probe_failed}), so this verdict is about the rung alone"
        )
        return f"{base}; {qualifier}" if base else qualifier

    def _register_residual(self, state: _Walk, outcome: WalkOutcome) -> None:
        """Hand the order this walk is walking away from to the recheck. (2026-08-29, P5)

        `_finish` is where this belongs, and the reason is the property stated at
        `_resolve`: **every `_request_cancel` is immediately followed by a `_finish`**,
        at all six call sites. So a cancel that failed is by construction an
        abandoned order, and this one funnel sees it without any call site having to
        remember — the same argument `ratelimit.py` makes for being a decorator.

        The test is the STATE, not the error, and it is one line: *if the last
        observation of this order was not terminal, this walk leaves a residual.*
        That covers all three ways an order is left working, only one of which a hook
        on the cancel's `except` branch would have seen:

        - the cancel RAISED (2026-08-28's 502);
        - the cancel was ACCEPTED and the order is sitting at `Cancel Requested`;
        - no cancel was requested at all — `UNRESOLVED: order_not_visible_at_broker`
          abandons an order it never saw, and `state.last is None` registers it.

        A terminal last observation registers nothing: FILLED and DEAD are both
        finished, and the position (if any) is the fill consumer's business. A
        PARTIAL fill on a still-working order DOES register, deliberately — the
        remainder can still fill, and the remainder-cancel is best-effort like every
        other.

        Direction comes from `state.sent`, which is the order AS ARGUS BUILT IT.
        Never from `state.last`, whose leg actions came back through the broker and
        are labelled relative to the ACCOUNT'S position rather than to Argus's
        intent — the recheck cancels on this, and cancelling a mislabelled CLOSE
        strands a position.
        → docs/decisions/2026-08-29-mid-session-cancel-recheck.md
        """
        if self._residuals is None or state.broker_order_id is None:
            return
        if state.last is not None and not state.last.is_working:
            return
        self._residuals.register(
            broker_order_id=state.broker_order_id,
            external_identifier=state.sent.external_identifier,
            # `state.sent`, never `state.last`. See the docstring.
            minted_direction=state.sent.direction,
            candidate_id=state.sent.candidate_id,
            walk_outcome=str(outcome),
            cancel_requested=state.cancel_requested,
            last_status=(
                str(state.last.status)
                if state.last is not None and state.last.status is not None
                else None
            ),
        )

    # -- small helpers -----------------------------------------------------

    def _stop_reason(self) -> str | None:
        """Why this walk must be abandoned now, or None to keep walking.

        Returns the REASON rather than a bool for the same reason `DriftCheckFn`
        does: it lands in `walk.finished`, and "the operator hit the kill switch"
        is a different post-mortem from "the session closed" even though both end
        in a cancel and an `ABORTED`.

        Two independent sources, checked in that order:

        - The session stop event — the ET close, a SIGTERM, or the entry path's
          soft day-end. Applies to EVERY walk, entry and exit alike.
        - `halt_check` — the kill switch, wired for ENTRY walks only. Exits pass
          none on purpose (see `HaltCheckFn`).

        A `halt_check` that RAISES counts as halted. It is an in-memory predicate,
        so an exception is a defect in the callable — and unlike `drift_check`,
        whose subject is the tape, this one's subject is a safety rail, where the
        fail-closed direction is to stop walking rather than to walk on blind.
        """
        if self._stop is not None and self._stop.is_set():
            return "stop requested"
        if self._halt_check is not None:
            try:
                if self._halt_check():
                    return "kill switch engaged"
            except Exception:
                logger.exception("halt check raised — abandoning the walk")
                return "halt check failed"
        return None

    def _policy_payload(self) -> dict[str, Any]:
        return {
            "cadence_s": self._policy.cadence_s,
            "coa_settle_s": self._policy.coa_settle_s,
            "cycle_wait_s": self._policy.cycle_wait_s,
            "step_ticks": self._policy.step_ticks,
            "walk_away_pct": self._policy.walk_away_pct,
            "working_timeout_s": self._policy.working_timeout_s,
        }

    async def _record_disconnect(
        self, state: _Walk, *, what: str, exc: Exception, attempt: int
    ) -> None:
        # Tier 1: a broker call that failed mid-walk is an anomaly with an order at
        # the other end of it, and the recovery decision that follows is only
        # readable next to the failure that prompted it.
        await self._audit.record(
            "walk.disconnected",
            {
                "external_identifier": state.sent.external_identifier,
                "broker_order_id": state.broker_order_id,
                "call": what,
                "attempt": attempt,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )

    def _notify(self, kind: str, detail: dict[str, Any]) -> None:
        logger.warning("%s: %s", kind, detail)
        self._alert(kind, detail)


def _first_filled(candidates: list[Order]) -> Order | None:
    """The first order here the broker says left a position, or None.

    THE definition of "did this trade idea fill?" for both callers that ask it of a
    whole `external_identifier` — `_recover` after a failed mutation, and
    `_finish`'s probe block before any non-fill verdict. One implementation on
    purpose: two would be free to disagree about the same broker answer, and the
    disagreement would be between a path that concludes FILLED and one that concludes
    REJECTED about the same spread.

    **FIRST, not largest, and never a sum.** The question is a BOOLEAN — did this
    trade idea leave a position — and the order returned supplies `broker_order_id`
    and `filled_contracts` for the RECORD only. `WalkResult` is a report, not a
    statement about what is held (module docstring, property 3); `positions`, written
    by the fill consumer off the fill feed, is authoritative about size. A walk that
    filled 1 on the original and 1 on the replacement therefore still reports one
    rung's count — a known, REPORTED under-count (2026-09-01, finding 5) left
    unchanged here rather than half-fixed by an aggregation this function has no
    mandate for.

    `classify_order`'s fail-closed posture is consumed as-is: an unknown status is
    WORKING, and "terminal but the broker never said what filled" is FILLED, which
    yields `filled_contracts=None` — already documented as distinct from 0.
    """
    for order in candidates:
        if classify_order(order) is OrderDisposition.FILLED:
            return order
    return None


def _is_terminal_observation(observed: Order | None) -> bool:
    """Did the BROKER conclude about this rung, as opposed to the walk concluding?

    The discriminator between the two shapes the probe now covers. 2026-09-02's fires
    off a terminal broker observation and quotes it; the deadline branches fire off a
    clock with `state.last` either absent or holding a non-terminal sighting (the ack
    at `Received`, the replacement that never went `Live`). Neither of those is the
    broker saying anything conclusive, so both report `terminal_status: null` — which
    is what keeps the two rates countable apart (§3.6).
    """
    return observed is not None and observed.is_terminal


def _corrected_reason(base: str | None, terminal: Order | None, filled: Order) -> str:
    """What `walk.finished` says when the probe overturned the verdict.

    Two shapes, because there are two ways to arrive: quoting the broker's terminal
    word on this rung, or quoting the walk's own reason for concluding without one.
    The first is 2026-09-02's string verbatim — the surfaces downstream read this
    prose, and rewriting it would break an operator's eye for no gain.
    """
    if terminal is not None:
        prose = f": {terminal.reject_reason}" if terminal.reject_reason else ""
        return (
            f"order {terminal.broker_order_id} came back {terminal.status}, but "
            f"{filled.broker_order_id} under this identifier is filled{prose}"
        )
    head = base or "the walk concluded without a fill"
    return f"{head}, but {filled.broker_order_id} under this identifier is filled"


def _reported_fill(order: Order) -> int:
    """Contracts the broker reports filled, treating "unreported" as zero.

    Safe ONLY in the two places it is used — deciding whether an order has been
    exposed to the market, and detecting a partial on a still-working order —
    where the fail-closed reading lives in `classify_order` instead. Never use
    this to decide whether an order left a position.
    """
    return order.reported_fill_quantity or 0
