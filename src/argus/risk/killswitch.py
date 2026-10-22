"""Kill switch — halts new entries in one action (spec §6, CLAUDE.md non-negotiable).

This is the in-process primitive. The phone-reachable authenticated endpoint that
drives it is a separate layer built on top; what lives here is the guarantee that
engaging is *immediate* and *fail-safe*:

- State flips first (`_engaged = True`) and the wake `Event` is set, so any task
  gating on it halts at once — before the audit write, which could be slow.
- The Tier 1 audit record is written after the flip. If persisting the trip were
  to fail, the system stays halted anyway; safety does not depend on the log
  write succeeding.

What a trip actually stops (verified end to end, not inherited):

- **New entries, immediately.** `PreTradeGate.evaluate` denies every candidate
  while `engaged` is True (`risk/gate.py`).
- **An entry walk already in flight.** `PriceWalkingLoop` polls a `halt_check`
  wired to `engaged` for ENTRY walks, and abandons + cancels within one cycle
  (`execution/loop.py`, `execution/entry.py`). `EntryExecutor.submit` also
  re-checks, closing the gap between a gate pass and the submit that follows it.

What a trip deliberately does NOT stop:

- **Exits.** The watchdog keeps watching and an in-flight closing order runs to
  completion — exit walks pass no `halt_check`. A halt must never strand an open
  0DTE position by cancelling the order that is closing it.
- **The process.** Engaging is not `Application._halt_process`; it sets no stop
  event, and `/status` stays up. The paths that also stop the process (audit
  failure, reconcile mismatch, entry/watchdog exceptions) pair the two explicitly.

Engaging still never PLACES or cancels an order itself — it flips state, alerts,
and records. The cancel that follows an entry abort is the walk's, on the walk's
own terms.

**The halt is state of the world, not state of this process** (2026-09-01). The
switch used to be constructed disengaged unconditionally, so a restart — an OOM,
a deploy, a reboot, a crash-loop — silently cleared a halt an operator had
pressed and walked away from. It is now built from what the audit log says
(`risk/kill_state.py`), and cleared by exactly one thing: `disengage`, reached
only from `POST /resume`. A restored trip does not expire; no clock and no
rollover clears it.
→ docs/decisions/2026-09-01-kill-switch-persistence.md
"""

from __future__ import annotations

import logging

from argus.alerts import AlertFn, log_alert
from argus.audit.log import AuditLog

logger = logging.getLogger("argus.risk.killswitch")

#: Tier 1, one per trip. THE durable record of a halt, and since 2026-09-01 also
#: the thing a starting process reads to know whether one is in force
#: (`risk/kill_state.py`). Named here, beside the writer, on `breaker.py`'s
#: precedent — the reader imports the writer's constant, never the reverse.
TRIP_CATEGORY = "kill_switch.trip"

#: Tier 1, one per human clear. Its `seq` relative to the newest trip's IS the
#: durable engaged/not-engaged answer, so it is never written speculatively:
#: `disengage` writes one only when it actually clears something.
RESUME_CATEGORY = "kill_switch.resume"


class HaltedError(Exception):
    """Raised by the entry path when the kill switch is engaged."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"kill switch engaged: {reason}")
        self.reason = reason


class KillSwitch:
    def __init__(
        self,
        audit: AuditLog,
        *,
        alert: AlertFn | None = None,
        initially_engaged: bool = False,
        initial_reason: str | None = None,
        initial_source: str | None = None,
    ) -> None:
        """`initially_*` is the rehydration seam, mirroring
        `DailyLossBreaker(initially_tripped=...)`.

        The supervisor fills it from `kill_state.load_kill_switch_state`; every
        other caller (tests, tools) gets today's disengaged default. Constructing
        engaged deliberately writes NO trip record — nothing tripped, a trip was
        FOUND — which is also what keeps `/status`'s `since` reporting the moment
        the operator actually pressed it rather than the moment the box rebooted.
        """
        if initially_engaged and not initial_reason:
            # `check()` asserts a reason exists whenever engaged, so this object
            # would be a landmine one call away from an AssertionError inside the
            # entry path. Refuse at construction instead.
            raise ValueError(
                "initially_engaged=True requires initial_reason: an engaged switch "
                "with no reason cannot explain itself to `check()` or to /status"
            )
        self._audit = audit
        # Defaults to `log_alert`, never to silence — the same discipline
        # `FillConsumer` and `PriceWalkingLoop` settled on. A caller that forgets to
        # pass a sink degrades a trip notification to a log line, not to nothing.
        # `Application.startup` passes the process-wide Slack sink.
        self._alert = alert or log_alert
        self._engaged = initially_engaged
        self._reason = initial_reason if initially_engaged else None
        self._source = initial_source if initially_engaged else None
        # True only while the CURRENT engagement is one this process inherited.
        # `engage` never sets it (it is unreachable while engaged), `disengage`
        # clears it — so a `/kill` pressed after a `/resume` reads as this
        # process's own. /status renders it, because "halted" and "halted before
        # this process existed" are different things to an operator at 09:35.
        self._restored = initially_engaged

    @property
    def engaged(self) -> bool:
        return self._engaged

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def source(self) -> str | None:
        """Who tripped the halt in force, or None. Attribution follows the state,
        so a restored halt keeps the ORIGINAL trip's source."""
        return self._source

    @property
    def restored(self) -> bool:
        """True when the halt in force was inherited from a previous process."""
        return self._restored

    async def engage(
        self, reason: str, *, source: str, detail: dict | None = None
    ) -> None:
        """Halt new entries immediately, then record the trip (Tier 1).

        Idempotent: re-engaging an already-engaged switch keeps the original
        reason and does not write a duplicate trip record.

        `source` identifies who tripped it (e.g. 'http_endpoint',
        'daily_loss_breaker', 'reconciliation_mismatch') for the audit trail.
        `detail`, when given, is nested under a 'detail' key in the trip record so
        a reviewer reading the trip ALONE — via /status's tail or a direct audit
        query — gets the whole story (e.g. the reconcile discrepancies) without
        correlating adjacent records. Omitting it keeps the payload minimal
        (`{reason, source}`), unchanged for the callers that don't need it.
        """
        if self._engaged:
            return
        # Flip state before anything that can block — halting is the safety action.
        self._engaged = True
        self._reason = reason
        self._source = source
        payload = {"reason": reason, "source": source}
        if detail is not None:
            payload["detail"] = detail
        # Notify BEFORE the audit write, and here rather than at the call sites:
        # `engage` is the one funnel every trip passes through (the HTTP endpoint,
        # the heartbeat stall, the audit-failure hook, a reconcile mismatch, the
        # entry and watchdog exception halts), and it is idempotent — so this fires
        # exactly once per trip and no future caller can forget it. Ordering matters
        # for the same reason the state flip comes first: the audit write goes
        # through `to_thread` and can be slow or fail outright, and a halt nobody was
        # told about is the failure this is here to prevent.
        try:
            self._alert(
                "kill_switch_engaged", {"reason": reason, "source": source}
            )
        except Exception:
            # An alert that raises must never undo a halt. `AlertFn` is documented
            # not to raise (`notify.SlackNotifier` only enqueues), so reaching here
            # is a defect in the sink — logged, and stepped over.
            logger.exception("kill-switch alert sink raised; halt stands")
        try:
            await self._audit.record(TRIP_CATEGORY, payload)
        except Exception:
            # The docstring's promise, now actually kept: the switch stays
            # ENGAGED even if the trip cannot be persisted. This is not
            # hypothetical — a failed audit log is itself a trip trigger, so the
            # one path guaranteed to hit this is the one where re-raising would
            # undo the halt it was called to perform.
            logger.exception(
                "kill switch ENGAGED (%s: %s) but the trip record could not be "
                "written — halt stands, audit trail incomplete",
                source, reason,
            )

    async def disengage(self, *, source: str, detail: dict | None = None) -> bool:
        """Clear the halt: RECORD first, then flip. Returns True if this cleared it.

        **The one clear in the system, and the only automated caller it may ever
        have is none.** `POST /resume` calls this; nothing in `src/` else does,
        and an AST test pins that. Rehydration only ever RESTORES a halt — the
        automated halted→trading transition
        `docs/decisions/2026-08-26-reconcile-broker-unreachable.md` refused is
        still not built, and a human over an authenticated channel is a different
        thing from a rail deciding on its own.

        **The write comes FIRST, which is the exact inverse of `engage`, and that
        is not an inconsistency — the ordering follows the SAFETY DIRECTION, not
        the symmetry of the two method names.**

        - `engage` flips first because halting is the safety action: it must not
          depend on a write that goes through `to_thread` and can be slow or fail
          outright. A halt with no record is bad; a halt that did not happen is
          worse.
        - `disengage` is the UNSAFE direction. Flipping first and failing the
          write would resume trading while the durable state still said halted —
          so entries would flow with nothing on the record authorizing them, and
          the next restart would re-halt. Writing first makes a failed clear a
          NO-OP: still engaged, the caller gets its exception, nothing changed.

        `AuditWriteError` therefore propagates (the caller answers 500). The
        alert fires after the successful write, inverting `engage` for the same
        reason: a halt nobody was told about is the failure `engage` guards; a
        resume only matters once it has actually happened.

        Idempotent, and writes NOTHING when already disengaged — a bare resume
        with no live trip in front of it must never enter the `seq` comparison
        `kill_state` decides engagement with.
        """
        if not self._engaged:
            return False
        cleared_reason, cleared_source = self._reason, self._source
        payload: dict = {
            "source": source,
            "cleared_reason": cleared_reason,
            "cleared_source": cleared_source,
        }
        if detail is not None:
            payload["detail"] = detail
        # Not wrapped: an unrecorded clear must not take effect. This is the one
        # place in the class where an audit failure changes the outcome.
        await self._audit.record(RESUME_CATEGORY, payload)
        self._engaged = False
        self._reason = None
        self._source = None
        self._restored = False
        logger.warning(
            "kill switch DISENGAGED by %s; the halt (%s: %s) is cleared and durably "
            "recorded — entries are permitted again",
            source, cleared_source, cleared_reason,
        )
        try:
            self._alert(
                "kill_switch_disengaged",
                {
                    "source": source,
                    "cleared_reason": cleared_reason,
                    "cleared_source": cleared_source,
                },
            )
        except Exception:
            # Mirrors `engage`: a raising sink must not undo the state change —
            # here the clear is already durable, so swallowing is the only honest
            # option. `AlertFn` is documented not to raise; reaching here is a
            # defect in the sink.
            logger.exception("kill-switch alert sink raised; the resume stands")
        return True

    def check(self) -> None:
        """Raise HaltedError if engaged. The entry path calls this before sizing."""
        if self._engaged:
            assert self._reason is not None
            raise HaltedError(self._reason)
