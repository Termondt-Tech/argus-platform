"""Durable kill-switch state — a halt outlives the process that imposed it.

`KillSwitch` is process state with no reset, and until 2026-09-01 that was the
whole story: `__init__` set `_engaged = False` unconditionally and nothing ever
read the durable `kill_switch.trip` row back. So a `/kill` at 10:00 followed by
an OOM, a deploy, a reboot or a crash-loop at 10:05 came back TRADING, with no
alert (nothing called `engage`, and the alert lives inside it) and a green
`/status` (which gates the trip row it just read behind `ks.engaged`). The one
event in the system that carried no human decision was the only thing that could
clear the rail CLAUDE.md calls non-negotiable.

**The principle, because it is what makes "for how long?" answerable.** A halt is
a STATE, not a timer, and states are cleared by transitions rather than by
clocks. So a restored trip NEVER expires: it is cleared only by `POST /resume` —
deliberate, authenticated, audited, and human. That is why this module has no
date bound anywhere in it.

    engaged iff the latest `kill_switch.trip` exists
    and no `kill_switch.resume` has a higher `seq`.

**Ordered by `seq`, never by `ts`.** `seq` is app-assigned, monotonic and gapless
across processes (`AuditLog._load_head`), so it totally orders records the wall
clock cannot; `ts` is millisecond UTC, and a `/kill` and a `/resume` landing in
the same millisecond would tie — on a comparison that decides whether the system
trades.

**Why the audit log and not a new table.** Same answer as `risk/rehydrate.py`:
it already IS the durable state store (single-writer, `flock`-enforced,
hash-chained, crash-durable), and the trip record has been written all along —
the only thing missing was a reader. A new table would be a second place for one
fact to live and disagree.

**Why this is NOT in `rehydrate.py`.** That module is the breaker's, and its two
queries are bounded to one ET day *deliberately* — its own comment calls the
upper `ts` bound "the episode boundary… which is how a per-day latch leaks across
days". This module's query must NEVER acquire a day bound: the breaker's latch
resets every morning and the kill switch's must not, so a Friday `/kill` is still
in force on Monday. The two reads look similar and have opposite scoping rules,
which is exactly the pair a later tidy-up unifies by accident. Separate files.

**The queries are unbounded in TIME and bounded in CARDINALITY**, which is the
necessary trade and is safe here: both categories hold single digits of rows per
year for the whole history of the system, and `status.py` already runs the
identical latest-trip lookup on every `/status` hit (documented there as
"O(1)-ish"). `seq` is `INTEGER PRIMARY KEY`, i.e. the rowid, so migration 0001's
`idx_audit_log_category` stores each category's entries already ordered by seq and
a reverse scan returns the first row and stops. Do NOT add a third query, a scan,
or a date range — `AuditLog.read`'s contract holds, and this runs ONCE per
process start.

→ docs/decisions/2026-09-01-kill-switch-persistence.md
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from argus.audit.log import AuditLog
from argus.risk.killswitch import RESUME_CATEGORY, TRIP_CATEGORY

logger = logging.getLogger("argus.risk.kill_state")

#: Tier 1, written by the supervisor when a process comes up on a halt it did not
#: impose. Not a second trip — nothing was engaged, something was FOUND engaged —
#: and the distinction matters to anyone reading the trail after a restart. Same
#: role as `rehydrate.RESTORED_CATEGORY` plays for the day's loss state.
RESTORED_CATEGORY = "kill_switch.restored"

#: Tier 1, written when the state could not be READ and the switch was engaged
#: fail-closed. Rare and load-bearing: it is the difference between "a human
#: halted this" and "the recorder could not say, so we assumed the worst".
RESTORE_FAILED_CATEGORY = "kill_switch.restore_failed"

#: The `source` a fail-closed engagement carries. Deliberately not one of the
#: eight real trip sources — nobody tripped anything.
RESTORE_FAILED_SOURCE = "restore_failed"

#: Stands in for a trip whose reason could not be recovered. `KillSwitch.check()`
#: asserts a reason exists whenever the switch is engaged, so this can never be
#: None or empty: an unreadable payload costs the STORY, never the halt.
_UNKNOWN_REASON = "kill switch restored from an unreadable trip record"


@dataclass(frozen=True)
class KillSwitchState:
    """What the audit log says about the kill switch, across all of history.

    `engaged` is the answer the process boots on. `reason`/`source`/`trip_seq`/
    `trip_ts` describe the trip being restored, so the halt an operator pressed
    keeps ITS attribution rather than acquiring the restart's.
    """

    engaged: bool
    reason: str | None = None
    source: str | None = None
    trip_seq: int | None = None
    trip_ts: str | None = None
    #: Set when the state was DEGRADED rather than read: the query failed, or the
    #: trip row exists and its payload could not be understood. Never a reason to
    #: come up disengaged — see `load_kill_switch_state`.
    restore_error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "trip_seq": self.trip_seq,
            "trip_ts": self.trip_ts,
            "source": self.source,
            "reason": self.reason,
        }
        if self.restore_error is not None:
            payload["restore_error"] = self.restore_error
        return payload


#: The ordinary answer: nothing is in force. A separate constant so "clear" is
#: one object rather than a literal repeated at three return sites.
CLEAR = KillSwitchState(engaged=False)


async def load_kill_switch_state(audit: AuditLog) -> KillSwitchState:
    """Read the halt in force, if any. **Never raises. Fails closed to ENGAGED.**

    "I cannot say whether a kill is in force" must never resolve to "it isn't" —
    that is the whole failure this module exists to close. It must equally not
    resolve to a `HaltGrade.ALL` park: coming up engaged is strictly weaker, it
    preserves `/status` and the watchdog (which matter most when the DB is sick),
    and it is the state the read was probably about to report anyway.

    **A deliberate divergence from `DayRiskRehydrationError`, which is terminal.**
    The breaker has no safe assumption available — re-anchoring to a post-loss NLV
    is unsafe and so is refusing to measure. The kill switch has one: engaged.

    It also self-escalates without help. If the audit DB is genuinely broken, the
    next Tier 1 write latches `AuditLog.failed` → `on_failure` → `_halt_process`
    at `ALL`, which is a better-diagnosed halt than anything this could raise.
    """
    try:
        return await audit.read(_read_kill_switch_state)
    except Exception as exc:  # a broken/unreadable audit DB
        logger.exception(
            "could not read the durable kill-switch state; ENGAGING fail-closed"
        )
        return KillSwitchState(
            engaged=True,
            reason=f"kill-switch state unreadable at startup ({exc})"[:500],
            source=RESTORE_FAILED_SOURCE,
            restore_error=f"{type(exc).__name__}: {exc}"[:500],
        )


def _read_kill_switch_state(conn: sqlite3.Connection) -> KillSwitchState:
    """The read itself. READ-ONLY, two index-backed lookups, no scan, no date range.

    Runs inside `AuditLog.read`, under the chain lock a crash-critical Tier 1
    write takes — so it must stay microseconds. Both queries are an equality on
    `category` with `ORDER BY seq DESC LIMIT 1`, which the leading-`category`
    index serves by walking that category's entries backwards and stopping at the
    first row.

    Two independent lookups compared in Python, deliberately: a correlated
    subselect or a join would be one query with a plan that can regress, and this
    comparison decides whether the system trades.
    """
    trip = conn.execute(
        "SELECT seq, ts, payload FROM audit_log WHERE category = ? "
        "ORDER BY seq DESC LIMIT 1",
        (TRIP_CATEGORY,),
    ).fetchone()
    if trip is None:
        # Never halted (or never halted since this DB existed). The unattended
        # restart path: nothing to restore, and the process trades.
        return CLEAR
    resume = conn.execute(
        "SELECT seq FROM audit_log WHERE category = ? ORDER BY seq DESC LIMIT 1",
        (RESUME_CATEGORY,),
    ).fetchone()
    if resume is not None and int(resume["seq"]) > int(trip["seq"]):
        # A human cleared it after that trip. kill → resume → kill → restart lands
        # HERE only when the newest resume precedes the newest trip, which is why
        # the comparison is between the two latest rows rather than a count.
        return CLEAR
    reason, error = _reason_from(trip["payload"], seq=int(trip["seq"]))
    return KillSwitchState(
        engaged=True,
        reason=reason,
        source=_source_from(trip["payload"]),
        trip_seq=int(trip["seq"]),
        trip_ts=str(trip["ts"]),
        restore_error=error,
    )


def _reason_from(raw: str, *, seq: int) -> tuple[str, str | None]:
    """Recover the trip's reason, or synthesize one. NEVER refuses.

    The contrast with `rehydrate._anchor_from` is the point, and it is not an
    inconsistency: an unreadable ANCHOR row means the day's baseline is lost and
    every later number would be wrong, so that one raises. Here the row's mere
    EXISTENCE is the load-bearing fact — a trip happened and nothing resumed it —
    so an unreadable payload costs the story and never the halt. Returning
    "disengaged" because the reason could not be parsed would be the bug.
    """
    try:
        payload = json.loads(raw)
        reason = payload["reason"]
    except Exception as exc:
        logger.error(
            "kill_switch.trip seq=%s is unreadable (%s); restoring the halt with a "
            "synthesized reason — the halt stands, its attribution does not",
            seq, exc,
        )
        return f"{_UNKNOWN_REASON} (seq {seq})", f"{type(exc).__name__}: {exc}"[:500]
    if not isinstance(reason, str) or not reason:
        return f"{_UNKNOWN_REASON} (seq {seq})", "trip payload carries no reason"
    return reason, None


def _source_from(raw: str) -> str | None:
    """The trip's `source`, or None. Best-effort by design — `/status` reads the
    row directly, so this is for the restored-state record and the log line."""
    try:
        source = json.loads(raw)["source"]
    except Exception:
        return None
    return source if isinstance(source, str) else None
