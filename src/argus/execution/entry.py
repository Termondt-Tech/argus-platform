"""The entry path: a gate-approved candidate becomes a real order (spec §7).

This is chunk 5, and it is WIRING. It owns no execution primitive of its own: it
builds with chunk 1's `build.build_order`, checks with chunk 2's
`DuplicateOrderGuard`, records with chunk 2's `AuditedOrderExecution`, and gets
filled with chunk 3's `PriceWalkingLoop`. What it genuinely owns is the decision
about WHEN the pipeline is entered at all, what to do with each way the walk can
end, and — the part that needed a decision rather than a call — where the line
falls between a failure that ends the day and a failure that halts the process.

It is the `SubmitFn` seam `SignalLoop` has had since shadow mode was written:

    submit(signal, proposal, risk) -> awaitable

`SignalLoop` passes `submit=None` in shadow and logs `shadow.would_submit`; here it
passes `EntryExecutor.submit`. Nothing about shadow's code path changes — the
branch already existed and this fills in the other side of it.

Decisions that are settled — do not relitigate:

**The exception-policy boundary is ONE `try`, and it is the `try` around the walk.**
Signed off explicitly (2026-07-30) rather than defaulted into:

  - BEFORE the first submit call, an unexpected exception ends today's ENTRIES:
    Tier 1 record, alert, and the session-scoped `EntriesEnded` latch, which
    `submit` refuses on for the rest of the day. Nothing has reached the broker, so
    there is no order state to protect, and halting the whole process for a
    market-data hiccup during signal generation is disproportionate. **It ends
    entries and nothing else** — until 2026-09-01 it set the session `stop` event,
    which also stops the watchdog, an exit walk in flight and the session body, so
    a held 0DTE spread lost its virtual stop and its 15:30 flatten for the rest of
    expiry day. See `_end_entries_today`.
  - FROM the first submit call onward, ANY exception halts the PROCESS — kill switch
    plus `request_stop`, via the same `Application._halt_process` pairing the
    audit-failure, reconcile-mismatch and watchdog paths use. Past that point an
    order may exist at the broker, and an order nobody is walking is exactly what
    the watchdog's posture exists for. Deferring to the next reconciliation instead
    would leave a live order unattended for however long that is.

`_walk_and_report` is the whole of the hard-halt region and it contains nothing but
the walk and the reporting of its outcome. Everything soft happens above the call to
it, in `submit`. That is the line, and it is one call boundary rather than a
condition, so it cannot drift.

**Not every pre-submission stop is a failure.** A stale signal, a credit that has no
valid limit price, a candidate already attempted, or a guard verdict of BLOCKED are
NORMAL outcomes of a system that refuses to trade unless everything lines up. They
are recorded and the candidate is abandoned; the session continues. Only an
*unexpected* exception ends the day. Conflating the two would mean one 1.1s-stale
candidate costs a trading day.

**A guard failure never submits, in any mode. What it costs is what changed.**
`has_conflicting_order` raises when it cannot reach the broker (chunk 2: an
unanswerable question must never read as permission), and the guard is the only
duplicate protection that exists. NO path below turns a raise into a submission,
and that is the invariant: `_walk_and_report` is unreachable without a returned
`ConflictVerdict`. Everything here is about how much of the day one unanswered
question costs.

In DRY-RUN mode it abandons the candidate and keeps going, because the premise of
the rule is absent: that mode has never routed an order, so nothing can be working
under the identifier and there is nothing to stack on top of. Amended 2026-08-11,
after cert 502'd five times in twenty minutes during the first sandbox pass — the
guard was the last remaining way a transient gateway error could cost a whole
dry-run day. See `_abandon_dry_run`.

In LIVE-FIRE the premise IS present — orders may exist — so being blind is still a
real reason to stand down. But ONE timed-out REST call is not evidence of
blindness, and until 2026-08-26 it was treated as though it were: on 2026-08-25 a
single cert timeout on the day's very FIRST tick (10:00:00 ET) ended the session
with ~5 hours of entry window left and no order ever at risk. Now there are two
stages, and the second one is unchanged from what it always was:

  - `_check_for_duplicate` retries a broker that cannot be reached — bounded, with
    its OWN per-attempt timeout (`[execution] guard_timeout_s`), because the bound
    it inherited was `broker.http_timeout_s`, sized for a cold first call from a
    new host and read by httpx as connect/read/write/pool INDEPENDENTLY. A spent
    burst retires THIS trade idea and pages, exactly as the dry-run path does.
  - `_guard_unreachable` ends the day only after `[execution]
    guard_unreachable_max_streak` bursts have been spent BACK TO BACK, with no
    answered check between them. That is sustained unreachability rather than one
    bad round trip, and the response to it — `_end_entries_today`, no kill switch,
    same `entry.aborted_day` record — is the original one in every entry-side
    respect. What changed on 2026-09-01 is only its blast radius: the watchdog,
    the exit executor and the close-of-day flatten now survive it.

Two things about the streak that are easy to get wrong. It counts DISTINCT TRADE
IDEAS, not ticks: a failed candidate is already in `_attempted`, so the next
`signal.cadence_s` tick re-proposing the same strikes returns at
`entry.already_attempted` and never reaches the guard at all. And it resets on any
ANSWER, blocked or not — the same reset-on-success rule
`AccountStateCache._note_refresh_success` uses, because "consecutive" is the whole
of what the number means.

**A blip costs the trade idea either way, and that is deliberate.** `_attempted`
claims the candidate BEFORE the guard runs (see below), so an abandoned candidate
is retired for the process lifetime even though the guard is a GET that cannot
have created an order. Releasing the claim on this path was considered and
refused: it would turn an unconditional "claimed before anything can reach the
broker" into a conditional one, in the one arc that prevents a double entry, to
buy back one spread out of the ~10 a session proposes.
→ `docs/decisions/2026-08-26-entry-guard-unreachable-retry.md`

**The guard is called exactly once, HERE, and the loop is told not to.** `run()` is
passed `guard_first_submission=False` — not because the check is being skipped, but
because it has already happened, in `submit`, above the boundary. Both halves of
`guard.py`'s rule hold: once before a brand-new trade idea's first submission, and
never before a replace (the loop does not check at all, so a replace re-sending the
same `external_identifier` against its own still-working order cannot deadlock the
loop against itself).

Delegating it to the loop with `guard_first_submission=True` was the first shape of
this and it was WRONG, for a reason worth recording: a guard failure propagates out
of `run()` (chunk 3, deliberately — "I could not check" must not become an outcome),
which would land it inside the hard-halt region and take the whole process down for a
question that was asked before anything was sent. Running it here puts it on the side
of the boundary its own semantics call for.

**Entries never escalate to crossing the market.** `WALKED_AWAY` is the end of the
idea. That asymmetry with chunk 4's exits is deliberate and not an oversight: an
exit crosses because the alternative is holding a position that is already hurting,
while a missed entry costs nothing but the trade. Paying up to open a position
nobody has to open is how a mechanical edge becomes a negative one.

**One entry attempt per candidate per session.** `SignalLoop` re-proposes the same
spread every `signal.cadence_s` for as long as it qualifies — one shadow session
recorded 456 `would_submit` records for 10 distinct spreads — so without this, a
walk that ended `WALKED_AWAY` would be re-entered seconds later, all day, at the
same strikes it just conceded 10% on and failed to fill. The duplicate guard does
not stop it (a terminal-unfilled order is `DEAD`, which is deliberately unblocked so
crash recovery can re-enter) and neither does the concurrent-position limit (nothing
filled, so nothing is held). Whether a conceded entry is worth re-attempting is a
strategy decision nobody has made; until then, one attempt.

**The drift breaker is armed HERE, because this is where the reference lives.**
Chunk 3's ladder is pure arithmetic on the gate-approved mid and never re-quotes,
which is deliberate — but it means a fast tape can leave the reference behind.
`UnderlyingDriftCheck` is the circuit breaker: it compares the CURRENT underlying
against the single price recorded when the candidate was gated
(`SpreadSignal.underlying_price`), every cycle, and abandons the walk past
`[execution] entry_drift_abort_points`. Three things about it are decided:

  - **The reference is fixed, not rolling.** Compared against the arrival price
    every cycle, never against the previous cycle's reading. A rolling comparison
    would let the market walk any distance at all, one sub-threshold step at a
    time, which is precisely the case worth stopping.
  - **It reuses chunk 4's quote path** — the `spot` callable is `LegQuotes.spot`,
    the same bound method the watchdog's underlying trigger reads, passed in by
    `session.py`. Not a second quote path: verified compatible rather than assumed,
    since `ChainSnapshot.underlying_price` and `LegQuotes.spot()` are both the mid
    of `market.latest(<underlying>)` on the same feed. `LegQuotes.spot()` adds a
    staleness gate the chain source does not, which is the direction to err in.
  - **Unreadable is abandon, not continue.** No spot, a stale spot, or a
    non-finite reference all abandon the walk. "I cannot tell whether the market
    has moved" is not evidence that it has not, and this is the same posture
    `PreTradeGate` already takes: it will not approve an entry on stale market
    data, so the loop must not keep offering one either. The cost of being wrong
    is one missed entry.

**Dry-run mode stops after the submit, and says so.** `[execution]
dry_run_submissions` runs gate -> guard -> construction -> the audited/throttled
stack -> the broker with `dry_run=True`, which is the whole point: it proves the
wiring, the decorator layering and that the broker accepts the payload Argus builds.
It does NOT walk, because a dry run mints no order id (measured against sandbox,
chunk 1) — there is nothing to poll, reprice or cancel, and driving the state
machine against that would be testing the loop against a fiction.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from argus.alerts import AlertFn, log_alert
from argus.audit.log import AuditLog, AuditWriteError
from argus.clock import et_now, et_today
from argus.config import Config
from argus.execution.audited import AuditedOrderExecution
from argus.execution.build import StaleSignalError, build_order, candidate_id
from argus.execution.guard import DuplicateOrderGuard
from argus.execution.loop import PriceWalkingLoop, WalkOutcome, WalkPolicy, WalkResult
from argus.execution.models import OrderDirection
from argus.execution.residual_orders import ResidualOrderRecheck
from argus.execution.zerobid import is_finite
from argus.risk.killswitch import KillSwitch
from argus.risk.models import EntryProposal, RiskSnapshot
from argus.shadow.outcome import markable_payload
from argus.signal.models import SpreadSignal

logger = logging.getLogger("argus.execution.entry")

ClockFn = Callable[[], float]
TradingDateFn = Callable[[], date]
#: The underlying's current price, or None when it is missing or stale.
#: `LegQuotes.spot` in production — chunk 4's path, passed by reference rather
#: than imported, so this module needs no dependency on `watchdog.py` and there is
#: exactly one implementation of the OCC/streamer-symbol-aware quote lookup.
SpotFn = Callable[[], float | None]
#: halt(reason, *, source, detail) — `Application._halt_process`: engage the kill
#: switch AND request process stop. The hard side of the exception boundary.
HaltFn = Callable[..., Awaitable[None]]

#: Outcomes that mean a human should look at this now. `UNRESOLVED` is the headline
#: one — chunk 3 returns it when the broker could not be reached often enough to say
#: what happened, or when two working orders were found under one identifier and both
#: were cancelled. `NEVER_WORKED` and `CANCELLED_ELSEWHERE` are quieter but still
#: unexplained: an order was accepted and then either never reached the market or was
#: terminated by something that is not this loop.
#:
#: `NO_ORDER_EXISTS` joined them on 2026-08-28 without changing what this path does:
#: it was carved out of `UNRESOLVED`, so leaving it out would have SILENTLY dropped
#: an alert the entry path has always sent. It is the mildest member — a submit that
#: failed with nothing at the broker costs a trade and leaves no exposure — but it
#: is still a broker fault mid-session, which on the day this split was written
#: happened five times before 10:13. The split earns its keep on the EXIT side; here
#: it only gives the page a truer name than "unresolved".
_ALERTING_OUTCOMES = frozenset(
    {
        WalkOutcome.UNRESOLVED,
        WalkOutcome.NO_ORDER_EXISTS,
        WalkOutcome.NEVER_WORKED,
        WalkOutcome.CANCELLED_ELSEWHERE,
    }
)

#: Attempts at ONE duplicate-guard check before the candidate is abandoned. NOT
#: config, and deliberately — the same split `reconcile.py` makes for the same
#: reason: the burst's SHAPE mirrors an established precedent, while the two
#: numbers an operator has a reason to tune (`execution.guard_timeout_s` and
#: `execution.guard_unreachable_max_streak`) are config.
_GUARD_ATTEMPTS = 3
_GUARD_BACKOFF_BASE_S = 1.0


def _guard_backoff_s(attempt: int) -> float:
    """1s, then 2s. `account/stream.py`'s ladder, not `reconcile.py`'s min(2n, 5).

    Both are precedents for a transient cert failure on a single REST call; this
    burst sits on the ENTRY's critical path, where the account refresher's own
    reasoning applies most directly — "several attempts inside the window a single
    one used to occupy". Every second here is also unchecked price staleness on
    the walk's first rung (the one rung `UnderlyingDriftCheck` never sees, since
    it runs at cycle boundaries AFTER the first submit), so the slower ladder buys
    nothing this call site can spend.
    """
    return _GUARD_BACKOFF_BASE_S * (2 ** (attempt - 1))


@dataclass(frozen=True)
class EntriesEnded:
    """Why today's ENTRIES are over. Session-scoped, and the only thing it ends.

    Set once by `EntryExecutor._end_entries_today`, read at the top of `submit`
    and published to `/status`. Until 2026-09-01 that method ended entries by
    setting the SESSION stop event, which is also what stops the watchdog's scan
    loop, an exit walk in flight, and the session body — so a soft, entry-side
    stand-down on a transient cert blip took a held 0DTE spread's virtual stop,
    its mark and its 15:30 flatten with it, and spent that position's one exit
    attempt on the way out.

    Three properties are load-bearing and each one is pinned by a test:

    - **It is a frozen dataclass, NOT an `asyncio.Event`.** Nothing can await it,
      so it cannot grow into the second stop event P3 considered and rejected
      ("a second event means every one of those consumers picks a side, and
      picking wrong fails silently"). It narrows the ACTION, never the EVENT.
    - **It has ONE reader** — `EntryExecutor.submit`. It is handed to no other
      component, so the watchdog, the exit executor, the walk and the signal loop
      have no side to pick and are not modified at all.
    - **There is no clear, no expiry and no reset.** Entries do not resume today
      under any condition. It evaporates because `EntryExecutor` is built once
      per trading day (`session._build_trading_side`), so the transience is
      structural rather than a call somebody has to remember to make.

    Deliberately unlike `HaltGrade.ENTRIES`, which reaches the SAME runtime
    posture from the opposite direction: that one engages the kill switch, writes
    a `HaltRecord`, is process-wide, survives a restart, and clears only when a
    human sends `POST /resume`. This one does none of that and clears at the
    rollover. `/status` renders them as two states for exactly that reason — the
    operator already knows entries are off; what they need is what clears it.
    → docs/decisions/2026-09-01-end-day-keeps-the-watchdog.md
    """

    #: `entry_guard_unreachable` | `entry_build_failed` — the same `kind` the
    #: `entry.aborted_day` record carries, so the page and the log agree on cause.
    kind: str
    candidate_id: str
    error_type: str
    #: ET wall clock, for `/status`. Not the monotonic `_now()` the walk uses —
    #: this one is rendered to a human, never differenced against a capture time.
    since: datetime


class _GuardUnreachable(RuntimeError):
    """The duplicate guard could not be ANSWERED. Never "no conflict".

    Private, and deliberately NOT `reconcile.BrokerUnreachable` even though it
    carries the same payload field names so the three unreachable sites read alike
    in the log. This one never escapes the module — `submit` handles it and
    returns — so importing that type would buy an `argus.execution` ->
    `argus.reconcile` package edge and nothing else.

    `last` is the ORIGINAL exception, carried rather than swallowed: the dry-run
    path and `_end_entries_today` both report it, so their records keep saying
    `ConnectionError` (or whatever cert actually did) instead of naming this
    wrapper.
    """

    def __init__(
        self, attempts: int, last: BaseException, *, stopped: bool = False
    ) -> None:
        super().__init__(
            f"could not reach the broker to check for a duplicate order after "
            f"{attempts} attempt(s): {type(last).__name__}: {last}"
        )
        self.attempts = attempts
        self.last = last
        #: The burst was cut short by the session stopping rather than by its
        #: attempts running out. A stop says nothing about the broker, so it must
        #: not count as evidence toward the day-ending streak.
        self.stopped = stopped

    def as_payload(self) -> dict[str, Any]:
        return {
            "leg": "orders",
            "attempts": self.attempts,
            "error_type": type(self.last).__name__,
            "error": str(self.last)[:500],
        }


class UnderlyingDriftCheck:
    """The entry walk's circuit breaker: has the market left the gated price behind?

    One instance per trade idea, holding that idea's arrival price. Called by
    `PriceWalkingLoop` at each cycle boundary through the `DriftCheckFn` seam;
    returns None to continue or a reason to abandon.

    `last` keeps the most recent observation so `_report` can put the actual
    numbers in `entry.finished`. The loop's own record carries the reason string;
    this is what lets the post-mortem see the reference, the reading and the
    threshold as values rather than as prose.
    """

    def __init__(self, *, reference: float, threshold_points: float, spot: SpotFn) -> None:
        self._reference = reference
        self._threshold = threshold_points
        self._spot = spot
        # Every number in here is None-or-finite, never NaN: this dict goes into the
        # Tier 1 `entry.finished` payload, the audit log refuses anything `json`
        # cannot encode, and a refused Tier 1 write halts trading. An unreadable
        # quote must not be able to halt the process on its way into the record.
        self.last: dict[str, Any] = {
            "reference_underlying": reference if is_finite(reference) else None,
            "threshold_points": threshold_points,
            "spot": None,
            "drift_points": None,
        }

    def __call__(self) -> str | None:
        if not is_finite(self._reference):
            # No arrival price was recorded for this candidate, so there is nothing
            # to measure against. Unevaluable, and unevaluable abandons — see the
            # module docstring. In practice unreachable from the entry path
            # (`strategy.py` always carries `ChainSnapshot.underlying_price`); this
            # is the branch that keeps it from silently comparing against a NaN.
            return (
                "no gate-time underlying price was recorded for this candidate, so "
                "the walk's reference cannot be validated"
            )
        spot = self._spot()
        self.last["spot"] = spot if spot is not None and is_finite(spot) else None
        if spot is None or not is_finite(spot):
            # Missing or stale (`LegQuotes.spot` fails closed to None). Not "no
            # drift" — no evidence either way, and the pre-trade gate would refuse
            # to approve this same candidate on this same feed right now.
            return (
                f"underlying price is unreadable or stale; cannot confirm the market "
                f"is still at the {self._reference:.2f} this candidate was priced at"
            )
        drift = abs(spot - self._reference)
        self.last["drift_points"] = drift
        if drift >= self._threshold:
            return (
                f"underlying moved {drift:.2f} points (from {self._reference:.2f} at "
                f"gate time to {spot:.2f}), at or past the "
                f"{self._threshold:g}-point abort threshold"
            )
        return None


class EntryExecutor:
    """Turns gate-approved candidates into submitted, walked, recorded orders.

    `execution` must be the audited stack (`AuditedOrderExecution` wrapping
    `ThrottledOrderExecution` wrapping the real adapter — see `pipeline.py`).
    `PriceWalkingLoop` enforces that at construction; nothing here needs to
    re-check it.
    """

    def __init__(
        self,
        *,
        execution: AuditedOrderExecution,
        audit: AuditLog,
        config: Config,
        guard: DuplicateOrderGuard,
        killswitch: KillSwitch,
        halt: HaltFn,
        stop: asyncio.Event,
        alert: AlertFn | None = None,
        clock: ClockFn | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        spot: SpotFn | None = None,
        trading_date: TradingDateFn | None = None,
        on_filled: Callable[[], None] | None = None,
        loop_factory: Callable[..., PriceWalkingLoop] | None = None,
        residuals: ResidualOrderRecheck | None = None,
    ) -> None:
        self._execution = execution
        self._audit = audit
        self._config = config
        self._guard = guard
        self._policy = WalkPolicy.for_entry(config)
        # REQUIRED, both of them, for the two halves of the exception boundary:
        # `halt` is the hard side; `stop` is the session's own stop event, and this
        # executor is a READER of it, never a setter (2026-09-01) — it goes to the
        # entry walk, so the close and a SIGTERM reach an order in flight, and it
        # bounds the guard's retry backoff. The soft side is `EntriesEnded`, which
        # ends entries without touching anything else. A constructor that accepted
        # neither would produce an executor with no failure posture at all.
        if halt is None:  # pragma: no cover - defensive
            raise ValueError("EntryExecutor requires a halt seam")
        if stop is None:  # pragma: no cover - defensive
            raise ValueError("EntryExecutor requires the session stop event")
        # REQUIRED for the same reason, and it is the THIRD posture: `halt` is the
        # hard side, `stop` the soft side, and this is the operator's side. It is
        # read in two places — once before submitting (the gate/submit race) and
        # once per walk cycle (the in-flight abort) — so an executor built without
        # it would leave the phone kill unable to reach an order already sent.
        if killswitch is None:  # pragma: no cover - defensive
            raise ValueError("EntryExecutor requires the kill switch")
        self._halt = halt
        self._stop = stop
        self._killswitch = killswitch
        self._alert = alert or log_alert
        # Both None in production — the walk defaults to `time.monotonic` and
        # `asyncio.sleep`. Injected only so a test about a multi-cycle walk does not
        # have to wait real seconds for it.
        self._clock = clock
        self._sleep = sleep
        #: Arms the drift breaker. None disarms it — the walk then behaves exactly as
        #: it did before this seam existed. Production always passes `LegQuotes.spot`
        #: (`session.py`); it is optional so tests about outcome handling do not have
        #: to stand up a quote source to say nothing about drift.
        self._spot = spot
        self._trading_date = trading_date or et_today
        #: Called after a fill so the watchdog rebuilds its watch list immediately
        #: rather than up to `[watchdog] refresh_interval_s` later. The fill consumer
        #: calls it too (a fill can land without this path seeing it); both are
        #: idempotent flag-sets.
        self._on_filled = on_filled
        #: Where an abandoned OPENING order goes when the walk ends (2026-08-29, P5).
        #: This is the half of the kill switch's guarantee that was missing: `_abort`
        #: cancels an entry order already at the exchange, `_request_cancel` swallows
        #: the failure, and until this seam existed nothing ever asked again.
        self._residuals = residuals
        self._loop_factory = loop_factory or self._build_loop
        #: `candidate_id`s this process has already attempted. See the module
        #: docstring: one attempt per candidate per session.
        self._attempted: set[str] = set()
        #: Consecutive SPENT guard bursts, live-fire only. Reaching
        #: `[execution] guard_unreachable_max_streak` ends the day; any answered
        #: check resets it to zero. Counts distinct trade ideas, not ticks — see
        #: the module docstring.
        self._guard_unreachable_streak = 0
        #: Set once when today's entries end; never cleared (see `EntriesEnded`).
        #: This executor is rebuilt per trading day, so tomorrow starts at None.
        self._entries_ended: EntriesEnded | None = None

    @property
    def entries_ended(self) -> EntriesEnded | None:
        """Why today's entries are over, or None while they are live.

        Read-only on purpose: `_end_entries_today` is the only writer anywhere,
        and a setter would be the first step toward a second caller narrowing the
        session for a reason this latch has not been reasoned about for. Published
        to `/status` by `session.run_session` the same way the watchdog's
        evaluability and spent-exit rows are.
        """
        return self._entries_ended

    # -- the SubmitFn seam -------------------------------------------------

    def was_attempted(self, signal: SpreadSignal) -> bool:
        """Whether `submit` would refuse `signal` as already attempted today. READ-ONLY.

        For the side-balance selector in `orchestrator` (2026-09-26): without it, a
        repeated put candidate — refused here as `entry.already_attempted` — hid a
        qualifying call for the whole tick, because the loop never looked past the
        strategy's first side. Answering the question changes nothing: only
        `submit` claims a candidate, and it claims before anything reaches the
        broker, exactly as before.
        """
        return candidate_id(signal, self._trading_date()) in self._attempted

    async def submit(
        self, signal: SpreadSignal, proposal: EntryProposal, risk: RiskSnapshot
    ) -> None:
        """Submit `signal` for real. The `SubmitFn` `SignalLoop` calls.

        Everything in this method body is the SOFT side of the exception boundary
        (see the module docstring): nothing above `_walk_and_report` has sent
        anything, so a failure here ends today's ENTRIES rather than the process —
        and, since 2026-09-01, ends nothing beyond them.

        `risk` is part of the `SubmitFn` signature and deliberately unused here. The
        snapshot's job was to authorize this entry, and `PreTradeGate` already did that
        and recorded the whole snapshot in `gate.decision` before this is ever called.
        Re-reading it to make a second decision would be a second, unaudited gate.

        **This blocks `SignalLoop`'s iteration for the duration of the walk** — several
        COA cycles, potentially tens of seconds. That is wanted, not tolerated: it
        serializes entries, so two walks can never be in flight at once and the
        concurrent-position limit cannot be raced by a second candidate submitted
        before the first one's position appears.
        """
        candidate = candidate_id(signal, self._trading_date())
        try:
            if self._killswitch.engaged:
                # Closes the gate/submit race. `PreTradeGate` already denies on
                # `kill_switch_engaged`, so in the ordinary case this is never
                # reached — but the gate's verdict and this call are separated by an
                # awaited Tier 1 write (`gate.decision`), and a trip landing in that
                # window would otherwise send an order the operator had already
                # halted. Tier 1 and rare BY CONSTRUCTION: the gate absorbs every
                # subsequent tick, so this cannot become a per-cadence record.
                await self._audit.record(
                    "entry.halted",
                    {
                        "candidate_id": candidate,
                        "reason": self._killswitch.reason,
                        "contracts": proposal.contracts,
                    },
                )
                logger.warning(
                    "entry refused for %s: kill switch engaged (%s)",
                    candidate, self._killswitch.reason,
                )
                return

            if self._entries_ended is not None:
                # Today's entries are over (see `EntriesEnded`). Placed HERE — after
                # the kill switch, before the `_attempted` test and `build_order` —
                # for three reasons: the kill switch is the operator's rail and
                # nothing reorders in front of it; nothing below may run, since a
                # `build_order` on this path is the very thing that ended entries at
                # one of the two call sites; and the guard must never be re-asked,
                # which is what "no auto-retry" means in code rather than in prose.
                #
                # Tier 2, on `entry.already_attempted`'s precedent and for its
                # reason: the signal loop keeps running and keeps proposing, so at a
                # 5s cadence a Tier 1 record here would out-volume every real
                # decision. The Tier 1 record is written ONCE, on the edge, by
                # `_end_entries_today`.
                self._audit.telemetry(
                    "entry.entries_ended_today",
                    {
                        "candidate_id": candidate,
                        "contracts": proposal.contracts,
                        "kind": self._entries_ended.kind,
                    },
                )
                return

            if candidate in self._attempted:
                # Not a failure — the expected steady state. `SignalLoop` re-proposes
                # a qualifying spread on every cadence tick, so this is the common
                # path for the rest of the day after one attempt. Tier 2: at a 5s
                # cadence a Tier 1 record here would out-volume every real decision.
                self._audit.telemetry(
                    "entry.already_attempted",
                    {"candidate_id": candidate, "contracts": proposal.contracts},
                )
                return

            order = build_order(
                signal,
                direction=OrderDirection.OPEN,
                contracts=proposal.contracts,
                trading_date=self._trading_date(),
                now=self._now(),
                max_signal_age_s=self._config.execution.signal_max_age_s,
            )
        except (StaleSignalError, ValueError) as exc:
            # Expected refusals, both of them, and both mean "do not submit THIS
            # candidate": the price aged past tolerance between gate-pass and
            # construction, or the credit has no expressible limit. Tier 1 — a
            # candidate the gate approved and that was then NOT sent is exactly the
            # kind of thing a post-mortem asks about — but the session continues and
            # the next tick may propose it again with a fresh quote.
            await self._audit.record(
                "entry.not_built",
                {
                    "candidate_id": candidate,
                    "reason": type(exc).__name__,
                    "error": str(exc)[:500],
                    "contracts": proposal.contracts,
                },
            )
            logger.warning("entry not built for %s: %s", candidate, exc)
            return
        except AuditWriteError:
            # The recorder is dead. It propagates: `SignalLoop` stops itself on this
            # and the process halts through the audit log's own failure hook. Never
            # convert it into a soft day-end — that would hide a broken black box.
            raise
        except Exception as exc:  # noqa: BLE001 - the SOFT side of the boundary
            await self._end_entries_today("entry_build_failed", candidate, exc)
            return

        # Claimed BEFORE anything can reach the broker. If the submit raises, the
        # candidate must still count as attempted: an order may exist, and a second
        # attempt at the same strikes is the duplicate this arc is built to avoid.
        self._attempted.add(candidate)

        # THE duplicate-submission check: once, here, for this brand-new trade idea,
        # and on the SOFT side of the boundary because nothing has been sent yet. It
        # raises when the broker cannot be reached (chunk 2: an unanswerable question
        # must never read as permission), and neither branch below can reach the
        # submit — `_walk_and_report` is unreachable without the verdict this binds.
        try:
            verdict = await self._check_for_duplicate(order)
        except AuditWriteError:
            raise
        except _GuardUnreachable as exc:
            if self._config.execution.dry_run_submissions:
                # In DRY-RUN mode there is nothing for a duplicate check to protect:
                # this mode has never routed an order, so no order can be working
                # under this identifier. The guard's rule — "an unanswerable question
                # must never read as permission" — is about not stacking on top of
                # something already at the exchange, and in this mode nothing ever is.
                # Same reasoning as `_abandon_dry_run`, one step earlier in the path.
                #
                # `exc.last` and not `exc`: this mode makes exactly one attempt, so
                # the record must keep naming what cert actually did rather than the
                # wrapper the live-fire path needs.
                await self._abandon_dry_run("entry_guard_unreachable", candidate, exc.last)
            else:
                # LIVE-FIRE: orders may exist, and being blind to what is already
                # working is a real reason to stand down — but only once being blind
                # has been established rather than assumed from one round trip.
                await self._guard_unreachable(candidate, exc)
            return
        # The broker ANSWERED, blocked or not, so whatever run of unreachable checks
        # was building is over. Reset-on-success, matching
        # `AccountStateCache._note_refresh_success`: "consecutive" is the whole of
        # what the streak means, and one good answer is what makes it not.
        self._guard_unreachable_streak = 0
        if verdict.blocked:
            # Something is already working or filled under this identifier. The guard
            # has already written `order.duplicate_blocked` at Tier 1 with which order
            # blocked it; nothing further to do and nothing to alert.
            logger.warning(
                "entry blocked as a duplicate: %s (%s)",
                order.external_identifier, verdict.reason,
            )
            return

        if self._config.execution.dry_run_submissions:
            try:
                await self._dry_run(order, candidate, verdict, signal, proposal)
            except AuditWriteError:
                raise
            except Exception as exc:  # noqa: BLE001 - see _abandon_dry_run
                # A dry run routes NOTHING — the broker validates and discards — so
                # there is no order state to protect. Abandon THIS candidate and keep
                # the session running; do NOT end today's entries.
                await self._abandon_dry_run("entry_dry_run_failed", candidate, exc)
            return

        # Built here, on the soft side, from THIS candidate's arrival price — the
        # underlying level the credit being offered was worth. One instance per
        # trade idea: the reference is per-candidate, so a shared one would measure
        # the next walk against the last one's market.
        drift_check = self._drift_check_for(signal)
        await self._walk_and_report(order, candidate, signal, proposal, drift_check)

    # -- the HARD side of the boundary -------------------------------------

    async def _walk_and_report(
        self,
        order: Any,
        candidate: str,
        signal: SpreadSignal,
        proposal: EntryProposal,
        drift_check: UnderlyingDriftCheck | None = None,
    ) -> None:
        """Walk the order and report how it ended. Any exception HALTS THE PROCESS.

        This method IS the hard-halt region, and it deliberately contains only the
        walk and its reporting. From the first submit inside `PriceWalkingLoop.run`
        onward an order may exist at the broker, and the failure that matters is a
        live order with nobody walking it — the same thing chunk 4 halts for.

        `AuditWriteError` is not special-cased and needs no special case: it takes
        the same halt as everything else, which is the correct response to it anyway.
        """
        try:
            # False because the guard has ALREADY run, in `submit`, above the
            # boundary — not because it is being skipped. See the module docstring:
            # letting the loop do it would put a pre-submission failure inside this
            # hard-halt region.
            result = await self._loop_factory(drift_check=drift_check).run(
                order, guard_first_submission=False
            )
            await self._report(result, candidate, signal, proposal, drift_check)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - every failure halts; see above
            logger.critical(
                "entry execution failed for %s after the submit boundary — halting",
                candidate, exc_info=exc,
            )
            self._alert(
                "entry_execution_failed",
                {
                    "candidate_id": candidate,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
            )
            await self._halt(
                f"entry execution failed after submission for {candidate} "
                f"({type(exc).__name__}: {exc})",
                source="entry_execution_exception",
                detail={
                    "candidate_id": candidate,
                    "external_identifier": order.external_identifier,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
            )

    async def _report(
        self,
        result: WalkResult,
        candidate: str,
        signal: SpreadSignal,
        proposal: EntryProposal,
        drift_check: UnderlyingDriftCheck | None = None,
    ) -> None:
        """Handle EVERY outcome explicitly. No default branch, on purpose.

        Chunk 3 made the outcome set explicit rather than success/failure so that
        this method has to distinguish "I gave up on a good price" from "the broker
        refused it" from "I do not know". A catch-all `else` here would quietly
        collapse exactly those distinctions the moment the enum grows.

        **`entry.finished` is THE scoreable record of a live-fire day**, which is
        why `signal` is threaded down here. It is the live path's terminal record
        exactly as `shadow.would_submit` and `entry.dry_run` are shadow's and
        dry-run's, and `shadow/marker.py` reads all three as one union — so
        without `markable_payload` the signal-quality series stops the moment
        orders start filling for real, which is what happened on 2026-08-13 and
        what happened at the sandbox flip before it
        (`docs/decisions/2026-08-12-markable-dry-run-records.md`).

        It is written for outcomes that never filled too — walked away, drifted,
        rejected — and that is deliberate: an idea the walk declined is a proposal
        at a credit, scoreable against settlement exactly like a shadow one. What
        makes a FILLED idea different is handled by the marker reading `order.fill`
        alongside it, not by anything decided here.
        """
        payload = {
            "candidate_id": candidate,
            "requested_contracts": proposal.contracts,
            "max_loss": proposal.max_loss,
            **result.to_payload(),
        }
        if drift_check is not None:
            # The reference, the last reading and the threshold as VALUES, on every
            # outcome rather than only on `DRIFTED`. A walk that filled at step 3
            # with 2.9 points of drift against a 3.0 threshold is the observation
            # that tells you the threshold is nearly right; recording it only when
            # it fires would throw away exactly the near-misses worth tuning on.
            payload["drift"] = dict(drift_check.last)

        if result.outcome is WalkOutcome.FILLED:
            # Position recording is NOT done here. Fills arrive as a push on the
            # `AlertStreamer` order feed and are booked by chunk 2's `FillConsumer`
            # regardless of who submitted the order, so duplicating it from the walk's
            # report would double-book — and `WalkResult` is explicitly a report, not
            # a statement about what is held. All this does is ask the watchdog to
            # look sooner.
            if self._on_filled is not None:
                self._on_filled()
            if result.partially_filled:
                # A partial is a SMALLER SPREAD, not a naked leg (one multi-leg
                # order), so it is held and watched like any other — but the size is
                # not what was sized, so it is worth surfacing.
                self._alert("entry_partially_filled", payload)
        elif result.outcome is WalkOutcome.WALKED_AWAY:
            # The designed no-trade ending. Not alerted: on an unproven strategy with
            # a 10% concession limit this is expected to be common, and paging for it
            # would train the operator to ignore the channel.
            logger.info("entry walked away for %s at %s", candidate, result.final_limit_price)
        elif result.outcome is WalkOutcome.DRIFTED:
            # The breaker did its job. Not alerted, for the same reason
            # `WALKED_AWAY` is not: on a fast tape this is expected, it costs only
            # the trade, and paging for a rail working correctly is how an operator
            # learns to ignore the channel. The numbers ride in the payload above so
            # the threshold can be tuned from the record rather than from memory.
            logger.info(
                "entry abandoned on underlying drift for %s: %s",
                candidate, result.reason or "-",
            )
        elif result.outcome is WalkOutcome.REJECTED:
            # Terminal immediately — never a "not filled yet". The broker refusing an
            # order Argus built is a wiring or entitlement problem, not a market one.
            self._alert("entry_rejected", payload)
        elif result.outcome is WalkOutcome.BLOCKED:
            # Not reachable through this module today — the guard runs in `submit` and
            # returns early on a block, so the loop is never asked to check. Handled
            # anyway rather than left to a default branch: the outcome exists, and a
            # future caller that does delegate the check must not fall through here.
            logger.warning("entry blocked as a duplicate: %s", result.external_identifier)
        elif result.outcome in _ALERTING_OUTCOMES:
            self._alert(f"entry_{result.outcome}", payload)
        elif result.outcome is WalkOutcome.ABORTED:
            # Session end OR a kill-switch trip — `result.reason` says which, and
            # both are deliberate operator/schedule actions rather than faults, so
            # neither alerts. A cancel was requested; whether it landed is
            # reconciliation's question, not this one's.
            logger.info("entry aborted for %s (%s)", candidate, result.reason)
        else:  # pragma: no cover - unreachable; the enum is exhausted above
            # Unreachable today, and `test_the_outcome_table_is_the_whole_enum` is
            # what makes that a CHECKED fact rather than a claim: a `WalkOutcome`
            # added to chunk 3 without a branch here fails CI.
            #
            # It alerts rather than RAISING, deliberately. This method is inside
            # `_walk_and_report`, so a raise here would engage the kill switch and
            # stop the process — and would skip the `entry.finished` write below,
            # discarding the record of a walk that really happened while an order
            # may exist. The outcome is not unknown to the SYSTEM (it is a valid
            # enum member with defined semantics in `loop.py`, and the walk has
            # already requested any cancel it needed); only this dispatch table is
            # stale. Paging with a complete record beats halting without one, for a
            # defect the build now catches first.
            logger.error("unhandled walk outcome %r for %s", result.outcome, candidate)
            self._alert("entry_unknown_outcome", payload)

        if result.recovered_disconnects:
            # Recovered, so the outcome above stands — but a broker disconnect while a
            # real order was working is worth knowing about even when it ended well.
            self._alert("entry_recovered_disconnects", payload)

        # ADDITIVE, and the markable block goes only into the RECORD — not into the
        # alert payloads above, which are read on a phone and gain nothing from the
        # strikes being repeated in a second nesting. `markable_payload` contributes
        # `signal` and `proposal`; neither collides with `WalkResult.to_payload()`,
        # the three fields above it, or `drift`.
        await self._audit.record(
            "entry.finished", {**payload, **markable_payload(signal, proposal)}
        )

    # -- the dry-run activation mode ---------------------------------------

    async def _dry_run(
        self,
        order: Any,
        candidate: str,
        verdict: Any,
        signal: SpreadSignal,
        proposal: EntryProposal,
    ) -> None:
        """Run the full path with the broker's own `dry_run=True`. Routes nothing.

        Reached only with an unblocked verdict — `submit` runs the guard for both modes
        and returns early on a block, so the dry run rehearses exactly the real path's
        decision rather than a parallel copy of it.

        **This record is THE scoreable record of a dry-run day.** `signal` and
        `proposal` are carried for that reason and no other: `shadow/marker.py`
        reads this category in a union with `shadow.would_submit`, and without
        them a dry-run day is a pile of `candidate_id`s with no strikes, credit or
        size — unscoreable except by joining back to `signal.candidate`. Shadow's
        terminal record has carried them since it was written; this one did not
        until 2026-08-12, which cost the signal-quality series every dry-run day
        from the sandbox flip onward
        (`docs/decisions/2026-08-12-markable-dry-run-records.md`).

        It is `markable_payload` and not a local dict literal so the two writers
        cannot drift: the marker decodes ONE shape.

        **Once per candidate, not once per walk step.** `_attempted` claims the
        spread before this is reached and dry-run mode never walks, so each
        distinct trade idea produces exactly one of these. That is a different
        cadence from `shadow.would_submit`'s per-tick record and the dedup handles
        it — see `shadow.outcome.trade_ideas`.
        """
        ack = await self._execution.submit(order, dry_run=True)
        # `ack.order.broker_order_id` is None here — a dry run mints no id. Recorded
        # as-is rather than tidied away, because that absence is the whole reason this
        # mode stops instead of walking.
        await self._audit.record(
            "entry.dry_run",
            {
                "candidate_id": candidate,
                "submitted": True,
                "walked": False,
                "why_not_walked": (
                    "a dry run mints no broker order id, so there is nothing to poll, "
                    "reprice or cancel"
                ),
                "guard": verdict.to_payload(),
                # ADDITIVE. `markable_payload` contributes `signal` and `proposal`;
                # `ack.to_payload()` contributes `order`, `dry_run`,
                # `buying_power_effect`, `fees`, `warnings`, `errors`. No key of
                # either overlaps the other or the five above, so nothing that
                # already reads this record sees a changed value.
                **markable_payload(signal, proposal),
                **ack.to_payload(),
            },
        )
        logger.info(
            "DRY RUN accepted for %s (%s) — nothing routed; set "
            "[execution] dry_run_submissions = false to submit for real",
            candidate, order.external_identifier,
        )

    # -- the duplicate check, and what an unanswered one costs -------------

    async def _check_for_duplicate(self, order: Any) -> Any:
        """Ask THE guard. In live-fire, retry a broker that cannot be reached.

        Returns a `ConflictVerdict` or raises `_GuardUnreachable`. There is no
        third answer, and no caller can proceed to a submit without the first —
        that is the whole of how the duplicate invariant survives this change.

        **The classification is untouched.** WORKING/FILLED block, DEAD allows,
        unclassifiable blocks; all of that is `guard.py`'s and none of it is
        reachable from here. This retries the QUESTION, never the verdict.

        **The retry's scope is drawn by position, not by exception type** — the
        rule `reconcile._ask_broker` established: only the guard await sits inside
        the `try`, so nothing else can be retried, and no type list has to keep up
        with whichever transport error the SDK raises this month (a raw
        `httpcore2.ReadTimeout` escaped unwrapped in the 2026-08-26 traceback).

        **One type IS load-bearing, and it is the same one.** `has_conflicting_order`
        writes Tier 1 `order.duplicate_blocked` from inside itself, so an
        `AuditWriteError` can surface from what looks like a pure read. A dead
        recorder halts the process and must never be re-attempted (CLAUDE.md), so
        it is re-raised untouched, before anything else.

        **DRY-RUN is deliberately unchanged**: one attempt, no added bound, the
        raw exception carried out in `last`. That mode routes nothing, so there is
        nothing for patience to protect — and the 30s it inherits from
        `broker.http_timeout_s` is a pre-existing condition of a transitional
        activation mode, not something to fix on the way past.
        """
        identifier = order.external_identifier
        if self._config.execution.dry_run_submissions:
            try:
                return await self._guard.has_conflicting_order(identifier)
            except AuditWriteError:
                raise
            except Exception as exc:  # noqa: BLE001 - reported by the caller
                raise _GuardUnreachable(1, exc) from exc

        timeout_s = self._config.execution.guard_timeout_s
        attempts = 0
        last: BaseException | None = None
        for attempt in range(1, _GUARD_ATTEMPTS + 1):
            if attempt > 1 and self._stop.is_set():
                # Stopped during the previous attempt or its backoff — the close,
                # SIGTERM, or a kill switch arriving through `_halt_process`. Asking
                # again after being told to stop is the round trip nobody wants, and
                # `_backoff` returns early for the same reason.
                break
            attempts = attempt
            try:
                # Applied HERE rather than pushed into the port, exactly as
                # `ReconcileRetry.request_timeout_s` is: the execution port is narrow
                # by design and a timeout argument on every method is the wrong seam.
                # Safe in the way it explicitly is NOT for the account refresher —
                # this is an async httpx call on the loop, so cancelling it really
                # cancels the request rather than abandoning a running worker thread.
                async with asyncio.timeout(timeout_s):
                    return await self._guard.has_conflicting_order(identifier)
            except AuditWriteError:
                raise
            except TimeoutError as exc:
                # `asyncio.timeout` raises a MESSAGELESS `TimeoutError`, so the audit
                # trail and `_GuardUnreachable.last` would otherwise carry a bare type
                # name and an empty string. Keep the type ("TimeoutError" is what
                # `error_type` readers match on, and nothing downstream matches on the
                # instance) but spell out what was waited for and how long.
                timed_out = exc if str(exc) else TimeoutError(
                    f"duplicate-guard check for {identifier} did not complete "
                    f"within {timeout_s:g}s"
                )
                last = timed_out
                self._note_guard_attempt(identifier, attempt, timed_out)
            except Exception as exc:  # noqa: BLE001 - see the docstring
                last = exc
                self._note_guard_attempt(identifier, attempt, exc)
            if attempt < _GUARD_ATTEMPTS:
                await self._backoff(_guard_backoff_s(attempt))
        assert last is not None  # the loop reaches here only having failed
        raise _GuardUnreachable(attempts, last, stopped=self._stop.is_set()) from last

    async def _backoff(self, seconds: float) -> None:
        """Wait between attempts, returning the instant the session is stopped.

        A bare sleep here would sit on an operator's `/kill` for the length of the
        ladder. Same mechanism as `reconcile._sleep_or_stop` — a race rather than a
        `wait_for` timeout, so the injected `sleep` seam is what the suite drives.
        """
        sleep = self._sleep if self._sleep is not None else asyncio.sleep
        stopped = asyncio.ensure_future(self._stop.wait())
        sleeper = asyncio.ensure_future(sleep(seconds))
        try:
            await asyncio.wait({stopped, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopped.cancel()
            sleeper.cancel()

    def _note_guard_attempt(
        self, identifier: str, attempt: int, exc: BaseException
    ) -> None:
        """Tier 2 per failed attempt, mirroring `reconcile.unreachable_attempt`.

        Tier 2 because the burst has not given up yet and most bursts recover; a
        Tier 1 record per attempt would put three rows on the log for a blip that
        cost nothing. GIVING UP is the crash-relevant fact, and it is recorded
        once, below.
        """
        logger.warning(
            "duplicate-guard check attempt %d/%d failed for %s: %s: %s",
            attempt, _GUARD_ATTEMPTS, identifier, type(exc).__name__, exc,
        )
        self._audit.telemetry(
            "entry.guard_unreachable_attempt",
            {
                "external_identifier": identifier,
                "attempt": attempt,
                "attempts": _GUARD_ATTEMPTS,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )

    async def _guard_unreachable(self, candidate: str, exc: _GuardUnreachable) -> None:
        """LIVE-FIRE: a burst is spent. Retire the idea, or — eventually — the day.

        The escalation is the ONLY thing the streak decides. Every spent burst
        pages immediately and is recorded Tier 1 either way, so an operator hears
        about the first one at the same moment they always did; what changed is
        that hearing about it no longer costs the remaining entry window.
        """
        if exc.stopped:
            # The session is already ending, so this burst proves nothing about the
            # broker. Record it as the abandon it is and leave the streak alone —
            # `entry.aborted_day` here would attribute a close to a cert failure,
            # and incrementing would let a clean shutdown poison tomorrow's first
            # reading if this executor ever outlived a day.
            await self._abandon_guard(candidate, exc, streak=self._guard_unreachable_streak)
            return

        self._guard_unreachable_streak += 1
        streak = self._guard_unreachable_streak
        if streak < self._config.execution.guard_unreachable_max_streak:
            await self._abandon_guard(candidate, exc, streak=streak)
            return

        # Sustained. THE ORIGINAL POSTURE in every entry-side respect: end today's
        # entries, keep the process, no kill switch, same `entry.aborted_day` record
        # with the same `kind`. Since 2026-09-01 it also keeps the SESSION, so a
        # spread held through the outage keeps its virtual stop and its 15:30
        # flatten. `exc.last` and not `exc` so `error_type`/`error` keep naming what
        # cert actually did, which is what every existing reading of this record
        # expects to find there.
        await self._end_entries_today(
            "entry_guard_unreachable",
            candidate,
            exc.last,
            extra={"attempts": exc.attempts, "consecutive": streak},
        )

    async def _abandon_guard(
        self, candidate: str, exc: _GuardUnreachable, *, streak: int
    ) -> None:
        """A spent burst that is not (yet) the day's end. Retire this idea only.

        A distinct `kind` from the day-ending path's, on purpose: read on a phone,
        "one spread could not be checked" and "today's entries are over" must not
        look alike. That rule is what `_end_entries_today`'s own alert kind now
        follows too.
        """
        logger.error(
            "entry_guard_unreachable for %s — abandoning this candidate "
            "(%d/%d consecutive), session continues",
            candidate, streak, self._config.execution.guard_unreachable_max_streak,
            exc_info=exc.last,
        )
        detail = {
            # TOP level, not nested. `SlackNotifier` scopes its 900s throttle on
            # `(kind, scope)` and reads the scope from here — nested, two different
            # candidates' pages would coalesce and the second would arrive as a bare
            # `(+1 suppressed)` up to 15 minutes late (2026-08-21).
            "candidate_id": candidate,
            **exc.as_payload(),
            "consecutive": streak,
            "max_consecutive": self._config.execution.guard_unreachable_max_streak,
        }
        self._alert("entry_guard_unreachable_abandoned", detail)
        await self._audit.record("entry.guard_unreachable_abandoned", detail)

    # -- internals ---------------------------------------------------------

    def _drift_check_for(self, signal: SpreadSignal) -> UnderlyingDriftCheck | None:
        """The breaker for one candidate, or None when no quote source was wired.

        None is the disarmed case and it is the pre-existing behaviour exactly: the
        loop then never calls a check and the walk is bounded only by its ladder.
        """
        if self._spot is None:
            return None
        return UnderlyingDriftCheck(
            reference=signal.underlying_price,
            threshold_points=self._config.execution.entry_drift_abort_points,
            spot=self._spot,
        )

    def _build_loop(
        self, *, drift_check: UnderlyingDriftCheck | None = None
    ) -> PriceWalkingLoop:
        """A FRESH loop per trade idea. `run()` is one attempt; the loop keeps
        per-run state, so reusing one instance across candidates would be sharing
        mutable walk state between unrelated orders."""
        return PriceWalkingLoop(
            execution=self._execution,
            audit=self._audit,
            policy=self._policy,
            guard=self._guard,
            drift_check=drift_check,
            # Arms the kill switch against an order already at the exchange. ENTRY
            # walks only — `ExitExecutor` passes none, so a trip never cancels the
            # order closing a position (see `loop.HaltCheckFn`). A lambda rather
            # than the bound property so the loop re-reads it every cycle instead
            # of capturing one stale bool at construction.
            halt_check=lambda: self._killswitch.engaged,
            alert=self._alert,
            stop=self._stop,
            clock=self._clock,
            sleep=self._sleep,
            residuals=self._residuals,
        )

    def _now(self) -> float:
        """Monotonic seconds on the SAME clock that stamped `signal.captured_at`.

        `time.monotonic` in production — the clock `ChainSnapshot.captured_at` uses.
        Comparing it against any other clock's origin would produce a meaningless age,
        and that age is the entire freshness bound (chunk 1: freshness is a timestamp
        check, not a refetch), so the two must not be allowed to differ.
        """
        return self._clock() if self._clock is not None else time.monotonic()

    async def _abandon_dry_run(
        self, kind: str, candidate: str, exc: BaseException
    ) -> None:
        """A dry-run attempt failed. Abandon the candidate; the session CONTINUES.

        `kind` mirrors `_end_entries_today`'s, so the record says WHICH step failed — the
        submit itself (`entry_dry_run_failed`) or the duplicate guard's broker
        round trip (`entry_guard_unreachable`). Both are soft in this mode; only
        the second is still hard in live-fire.

        Changed 2026-08-11, after the first sandbox dry-run pass ended the whole
        trading day on a single `502 Bad Gateway` from cert's nginx, twenty minutes
        into the session and on the very first candidate.

        Why continuing is right, and why this is not a relaxation of the soft/hard
        boundary:

        - **Nothing is at stake.** A dry run mints no broker order id (MEASURED,
          chunk 1) — the broker validates and discards. A failure leaves no order,
          no position and no local state, so there is nothing an early stop could
          protect. `_end_entries_today`'s justification ("nothing reached the broker, so no
          order state to protect") is the argument for continuing here, not for
          stopping.
        - **The real path would have recovered.** `PriceWalkingLoop._first_submit`
          wraps its submit in `_recover`, which reconnects, re-queries
          `by_identifier` and resumes from what exists, only giving up after
          `max_recovery_attempts`. Dry-run mode skips the walk (correctly — there
          is no id to poll) and so skipped that recovery too, which made the mode
          built to DE-RISK execution the only one with no retry on the submit.
        - **cert 502s constantly.** `argus.toml` already documents the cert API as
          "returns 502s and hangs often enough that this matters", which is why the
          account liveness refresh has a 1s/2s/4s ladder. The entry path had none.
          Ending the day on one gateway blip means a dry-run session dies at
          random, and on a blackout-adjacent calendar that can cost the only
          window in a week.
        - **It is bounded, not a retry loop.** `_attempted` claims the candidate
          BEFORE the dry run, so this spread is retired either way — there is no
          re-attempt of the same idea. A systemic failure (a bad instrument type, a
          rejected payload shape) therefore surfaces as one record per DISTINCT
          candidate, roughly ten a session, which is a diagnosis rather than a
          flood.

        Live-fire behaviour is deliberately UNCHANGED: past `_walk_and_report` an
        order may exist, and that region still halts the process.
        """
        logger.error(
            "%s for %s (dry run) — abandoning this candidate, session continues",
            kind, candidate, exc_info=exc,
        )
        detail = {
            "candidate_id": candidate,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
        self._alert(kind, detail)
        await self._audit.record("entry.dry_run_failed", {"kind": kind, **detail})

    async def _end_entries_today(
        self,
        kind: str,
        candidate: str,
        exc: BaseException,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """The SOFT side of the boundary: end today's ENTRIES, keep everything else.

        Sets the session-scoped `EntriesEnded` latch, which `submit` refuses on for
        the rest of the day. No kill switch, no `HaltRecord`, no durable state:
        nothing reached the broker, so there is no order state to protect, and a
        latched halt would also cost tomorrow. Tomorrow's session builds a fresh
        executor and starts clean with no human step.

        **It no longer sets the session `stop` event, and that is the whole of the
        2026-09-01 change.** That event is read by four things with four different
        meanings — the watchdog's scan loop, an exit walk in flight, the session
        body's teardown, and the scheduler's "this day is done" — so ending entries
        through it took a held 0DTE spread's virtual stop, its mark and its 15:30
        flatten for the rest of expiry day, and cancelled an exit already walking,
        which returns `ABORTED` -> `NOT_RESOLVED` and thereby SPENDS that position's
        one exit attempt. None of that was ever the intent of this path; it was the
        blast radius of the mechanism. What this method promises is unchanged in
        every entry-side respect: entries end on the first occurrence, the guard is
        never re-asked, the unreachable streak is not reset, `_attempted` claims
        stand, and no condition resumes entries today.

        The day still ends at the real close, set by the scheduler exactly as on any
        other day, so "never re-enter a day already run" is untouched.

        `extra` is ADDITIVE and every existing key keeps its meaning: `kind`,
        `candidate_id`, `error_type` and `error` read exactly as they did before
        2026-08-26, so nothing already reading `entry.aborted_day` sees a changed
        value. The CATEGORY keeps its name for the same reason — `tools.mark`,
        `tools.desk` and the marker tests read it — while the METHOD was renamed
        from `_end_day`, because a method that no longer ends the day must not be
        called that. Category names are an interface; method names are documentation.
        → docs/decisions/2026-09-01-end-day-keeps-the-watchdog.md
        """
        logger.error(
            "%s for %s — ending today's ENTRIES (nothing was submitted); the "
            "watchdog, exits and the close-of-day flatten keep running",
            kind, candidate, exc_info=exc,
        )
        error_type = type(exc).__name__
        detail = {
            "candidate_id": candidate,
            "error_type": error_type,
            "error": str(exc)[:500],
            **(extra or {}),
        }
        # A DISTINCT alert kind from the two `kind`s, on `_abandon_guard`'s stated
        # rule: read on a phone, "the day is over" and "entries are done, the
        # watchdog is still working" must not look alike. `kind` rides in the detail
        # so the cause is still one glance away, and `candidate_id` stays at the TOP
        # level so `SlackNotifier`'s `(kind, scope)` throttle still scopes it.
        self._alert(
            "entries_ended_today",
            {**detail, "kind": kind, "entries": "ended", "watchdog": "running"},
        )
        await self._audit.record("entry.aborted_day", {"kind": kind, **detail})
        # The state made explicit, Tier 1 and exactly once. Named `session.*` though
        # it is written from here, to pair with P3's `session.monitor_only`: the two
        # are the complete answer to "why did this session route nothing", and
        # splitting them across prefixes would hide one from the query that finds the
        # other. A category names its SUBJECT, not its writer.
        ended = EntriesEnded(
            kind=kind, candidate_id=candidate, error_type=error_type, since=et_now()
        )
        await self._audit.record(
            "session.entries_ended",
            {
                "trading_date": self._trading_date().isoformat(),
                "kind": kind,
                "candidate_id": candidate,
                "error_type": error_type,
                "entries": "ended",
                "watchdog": "running",
                "clears": "next_trading_day",
            },
        )
        # LAST, and after both awaited writes. If either record fails the write is
        # `AuditWriteError`, which halts trading through the audit log's own latch —
        # a strictly stronger outcome than this latch — and setting it first would
        # leave entries silently off with no Tier 1 row saying so.
        self._entries_ended = ended
