# The kill switch does not survive a restart (2026-09-01)

**Status: DESIGNED, SIGNED OFF, and BUILT 2026-09-01** — design first, sign-off
before any code, then implemented as specified with ONE correction recorded in
Q9 below (the `/status` gate the design said to delete is kept, because
rehydration makes it correct and deleting it would introduce the mirror bug).
This alters the behaviour of the rail CLAUDE.md calls a non-negotiable and it
adds the first human-authenticated halted→trading control in the system, which
[`2026-08-26-reconcile-broker-unreachable.md`](2026-08-26-reconcile-broker-unreachable.md)
deliberately avoided building.

**`src/` only — no new config key, so the image deploys alone.** Three new Tier 1
audit categories (`kill_switch.resume`, `kill_switch.restored`,
`kill_switch.restore_failed`), no migration. 33 tests in
`tests/test_kill_persistence.py`; four mutations verified to fail it (no
rehydration; any-resume-exists instead of the `seq` comparison; a date bound on
the read; `disengage` flipping before it records).

**Recommendation in one line: the halt is state of the world, not state of a
process.** Persist it (it already is — nothing reads it back), rehydrate the
KILL SWITCH and nothing else, and add `POST /resume` as the one thing that can
clear it. **No expiry, no day bound, no automatic clear, and no new
`HaltGrade`.**

**This is a live-flip blocker.** In cert a spurious resume re-proposes cert
orders. Live, the halt an operator pressed stops being in force at the next
restart, and the restart is invisible to the person who pressed it.

**PROVEN IN CERT 2026-09-01, and across a code-version boundary.** The first boot
of the v0.3.14 image (17:02 EDT) rehydrated the switch from the audit log and came
up ENGAGED, restoring `kill_switch.trip` seq 496115 — the 2026-08-28 12:15 ET
`reconciliation_mismatch` halt, written by the *pre*-v0.3.14 image, which four
subsequent sessions had booted `_engaged = False` and traded straight through.
Every design property held: `kill_switch.restored` at Tier 1 (not a second trip),
one page, `/status` showing `ENGAGED (RESTORED)` with the original press time,
`halted.halted: false` (switch restored, `HaltRecord` not), and the same boot's
`reconcile.result` clean — a stale halt, correctly held until a human cleared it.
`POST /resume?confirm=yes` then cleared it in one call, `restart_required: false`,
seq 625667 carrying `cleared_reason`/`cleared_source` and the peer IP. Still
exercised only by the unit suite: `restore_failed`, `restart_required: true`, and
the two `/resume` refusal paths.
→ [second v0.3.13 day, addendum](2026-09-01-second-live-fire-day.md#addendum--2026-09-01-evening-v0314-deployed-and-the-persistence-rail-proved-itself-across-the-version-boundary)

**One-time cutover cost.** Because the read has no date bound, the first boot of
this image restores *any* un-resumed trip in the whole audit history — the
`resume` category is new, so nothing before it was ever cleared through the
mechanism. Check `SELECT ... FROM audit_log WHERE category = 'kill_switch.trip'
ORDER BY seq DESC LIMIT 1` before the cutover restart; assess and `/resume` if a
row stands. Documented in [`docs/releases/v0.3.14.md`](../releases/v0.3.14.md).
The backlog is drained exactly once.

---

## The defect

`KillSwitch.__init__` (`src/argus/risk/killswitch.py:63`) sets
`self._engaged = False` unconditionally. The object holds no persistence. A trip
writes a durable Tier 1 `kill_switch.trip` row and **nothing ever reads it
back**, so the engaged state lives only in one process's memory.

```
10:00  operator presses POST /kill      → _engaged = True, kill_switch.trip written
10:05  OOM / deploy / droplet reboot    → restart: unless-stopped, Restart=always
10:05  new process: _engaged = False    → reconciles clean, session starts
10:06  entries resume
```

Nothing fires. `KillSwitch.engage` was never called in the new process, so there
is no trip, no alert, and no `process.halted`. The operator is not watching,
because the operator believes the system is stopped.

**`/status` misreports it, in the reassuring direction, at the one moment it is
wrong.** `status.py:1074` reads

```python
trip = db.kill_trip if ks.engaged else None
```

so after a restart-while-halted the page shows *armed (not engaged)* with a null
source and a null `since` — even though the `kill_switch.trip` row is sitting
right there in the DB the page just queried. This is the fourth instance of the
pattern the decisions index already names three times (`daily_loss.breach`
rendering TRIPPED off a log row; a spent exit attempt rendering as an ordinary
open position; an unwatchable position rendering as watched): **a page that
reads calm for a state the operator needs to act on.** Here it is the mirror
image — the live object is wrong and the log row is right, and the page trusts
the object.

**`README.md` documents the bug as behaviour.** Its kill-switch section says the
halt "persists across day-rollover **until the process is restarted**." That
sentence is accurate about the code and wrong about what a halt is for.

### Why it was never fixed with the breaker

The daily-loss breaker had the identical bug and was fixed on 2026-08-20 by
rehydrating today's trip per trading date
([restart rehydration](2026-08-20-daily-loss-breaker-restart-rehydration.md)).
The kill switch did not get the same treatment because it raises a question the
breaker never had to answer:

- the breaker's latch is **naturally per-trading-date** — it resets every
  morning anyway — so "restore today's trip" has an obvious expiry;
- the kill switch is deliberately **process-lifetime with no reset**
  ([kill-switch scope](2026-08-03-kill-switch-scope.md)), so "rehydrate it"
  forces: *for how long does a restored trip stay in force?*

---

## The principle that dissolves "for how long"

**An automated strategy never resumes trading on its own after a halt. A
restored kill does not expire; it is cleared only by a deliberate,
authenticated, audited human action.**

The question "for how long" only looks unanswerable while the halt is treated as
a *timer*. It is not a timer, it is a *state*, and states are cleared by
transitions, not by clocks. Three consequences:

1. **Halt state lives durably, with reason, source and timestamp.** The process
   *reads* it at startup; it never *owns* it. A restart, a deploy, an OOM or a
   reboot cannot clear it as a side effect, because none of them is a decision.
2. **A restored kill is a kill, not a halt** — see the next section. It stops
   exactly what a live `/kill` stops, and nothing more.
3. **`POST /resume` is the only clear**, and it is the mirror of `/kill`:
   authenticated, idempotent, durably recorded before it takes effect, alerted.

### How this reconciles with the 2026-08-26 one-way-latch reasoning

That record says (verbatim):

> **The park never enters the halted state while it is still trying, and that is
> the design.** The literal reading — halt, then clear the halt on a timer —
> would have required three changes to primitives that are deliberately one-way:
> adding `KillSwitch.disengage` (a latch a restart currently clears, and nothing
> else), making `_halted` clearable (it is first-writer-wins so a cascade cannot
> overwrite the cause), and teaching `_await_exit_while_halted` to hand control
> back to the scheduler — the first automated halted → trading transition in the
> system.

and closes by naming this record's existence in advance:

> **`KillSwitch` still has no `disengage`, and `_halted` is still terminal.** The
> self-heal was built to avoid needing either. A future task that genuinely needs
> a clearable halt is its own design, and it touches a non-negotiable.

**Its parenthesis is the bug, stated as a property**: *"a latch a restart
currently clears, and nothing else."* That was written as a description of a
one-way primitive; read as a statement about durability it says a restart is the
one thing in the system that CAN clear a halt, which is precisely backwards —
a restart is the one event that carries no human decision at all.

The load-bearing word in the first quote is **automated**. What that record refused was a *rail*
deciding on its own that a halt no longer applies — a retry loop that halts on
attempt 3 and un-halts on attempt 4, where the same code both sets and clears
the state and any bug in it fails open. `/resume` is categorically different: a
human, over an authenticated channel, having assessed the cause. That is the
transition a desk actually performs, and today it is performed by *restarting
the process*, which is the same transition wearing a disguise — with none of the
audit trail and none of the deliberateness.

**Two of its three requirements are still refused by this design.**
`KillSwitch.disengage` is built — the one that record said would be its own
design, which this is. **`_halted` stays a one-way latch** (see Q5), and
**`_await_exit_while_halted` never hands control back to the scheduler**, so the
automated halted→trading transition it refused is still not built (see "What
must NOT change").

---

## Q4 first, because everything else depends on it: what gets rehydrated — and the startup posture (deliverable 4)

**Recommendation: rehydrate the KILL SWITCH only. Do NOT rehydrate
`HaltRecord`, and do NOT mark a restored kill as a `HaltGrade.ENTRIES` halt.**

The design prompt proposed reusing P3's `ENTRIES` posture
([halt scope split](2026-08-29-halt-scope-split.md)) to come up "monitor-only".
That is the right *posture* and the wrong *mechanism*, for one reason:

> **Rehydration must reproduce the state the trip left — not a stronger one.**
> Anything else means a restart changes the meaning of the halt, which is the
> defect being fixed, in mirror image.

A live `/kill` at 10:00 produces: switch engaged, process otherwise entirely
normal, resumes trading tomorrow *if a human says so*. If a restart at 10:05
instead produced a permanent `ENTRIES` halt, the restart would have *promoted*
the operator's halt into something they did not ask for and cannot undo without
another restart.

**And the kill switch's own scope is already exactly "monitor-only."** From
[kill-switch scope](2026-08-03-kill-switch-scope.md), verified in source and
pinned by `tests/test_kill_scope.py` and an AST test:

| Stopped by a trip | Not stopped by a trip |
|---|---|
| `PreTradeGate.evaluate` → `kill_switch_engaged` (`risk/gate.py:72`) | Exits — `ExitExecutor` arms no `halt_check`, pinned by an AST test |
| `EntryExecutor.submit`'s pre-build re-check → `entry.halted` (`execution/entry.py:453`) | The watchdog, its virtual stops, and its marks |
| An entry walk at the exchange — `halt_check` (`execution/entry.py:1049`) → `ABORTED` + cancel | `FLATTEN_BEFORE_CLOSE` (`ExitExecutor.close`, same path) |
| | The process, `/status` and `/kill` (the switch owns no stop event; a test asserts it) |

That is the deliverable-4 answer: **"up but monitor-only" is carried by the kill
switch's existing three-point entry scope, which has been the definition of a
trip since 2026-08-03.** It is not a new code path because it is not a code path
at all — it is the mechanism a live `/kill` already uses, proven end to end over
a real TCP request into a real `PriceWalkingLoop`. `HaltGrade.ENTRIES` exists to
give a *reconciliation halt* the same posture; it is P3's answer to "given a
halt, how wide?", and a restored kill is not a halt.

**Is `HaltRecord` lost across the restart? Yes, and that is correct.** A
`HaltRecord` means "this PROCESS will not trade again without a human." It is
process-scoped by construction — first-writer-wins on `reason`, strictest-wins
on `grade`, never cleared. The durable fact that must survive is *"a halt was
imposed and not lifted"*, and the `kill_switch.trip` row carries it with its
source, so the reason a rail halted is preserved and shown. What must NOT
survive is the *verdict* — the original process halted because a reconcile
mismatched, and the new process re-runs reconciliation at startup for exactly
this reason. If the cause persists, it halts again on its own evidence. If it
does not, the restored kill still refuses every entry until a human clears it.
**Either way nothing trades without a human, which is the whole requirement.**

### The cost of this choice, stated because it is real

With the entry hook wired and the gate denying, a restored-kill session runs the
full pipeline and records one `signal.candidate` + one `gate.decision` per
qualifying tick, all day. That is **not new**: it is precisely what a live
`/kill` at 10:00 leaves behind today, and it stops at the gate — `SignalLoop`
returns on `not decision.allowed` (`orchestrator.py:213`) *before* `_submit`, so
no order is built, no `DuplicateOrderGuard` call reaches the broker, and
`entry.guard_unreachable` cannot fire. No extra broker traffic, no extra risk;
Tier 1 volume identical to today's post-`/kill` state.

### The one alternative worth naming, and why it is rejected

Grading the restoration *by trip source* — `http_endpoint` restores as a plain
kill, everything else as an `ENTRIES` halt — is coherent and was considered. It
is rejected because it buys nothing: under either version no entry is possible,
and it costs the operator a second restart (the halt latch is one-way, so
`/resume` could not clear it in-process) while making the page claim HALTED for
a condition that may have just reconciled clean. **The trip's `source` is
preserved and displayed either way**, so the operator loses no information.

---

## Q1 — where the durable state lives

**The audit log. No migration, no new table, no state file.**

It already IS the durable state store: single-writer, `flock`-enforced,
hash-chained, crash-durable, and the same store `risk/rehydrate.py` reads the
breaker's latch back out of. A dedicated row or a file on the volume would be a
second place for the same fact to live and disagree — and the trip record is
*already written today*; the only thing missing is a reader.

**The `AuditLog.read` bounded-query contract holds, and the precedent is
exact.** `status.py:956` already runs the identical latest-trip query on every
`/status` hit and documents it as O(1)-ish:

```sql
SELECT seq, ts, payload FROM audit_log
 WHERE category = 'kill_switch.trip'  ORDER BY seq DESC LIMIT 1;

SELECT seq FROM audit_log
 WHERE category = 'kill_switch.resume' ORDER BY seq DESC LIMIT 1;
```

Two queries, each an equality on `category` with `ORDER BY seq DESC LIMIT 1`.
`seq` is `INTEGER PRIMARY KEY`, i.e. the rowid, so `idx_audit_log_category`
(migration 0001) stores that category's entries already ordered by seq — a
reverse scan returns the first row and stops. **They are unbounded in TIME and
bounded in CARDINALITY**, which is the necessary trade (see Q2) and is safe
because these two categories hold single digits of rows per year for the whole
history of the system — unlike `shadow.would_submit`, whose ~3k rows/day is what
migration 0003 exists for. The read runs **once per process start**, not in a
hot path, and holds the chain lock for microseconds.

**Do not "improve" this into one query with a subselect or a `MAX()` over a
join.** Two independent index-backed lookups compared in Python is the cheapest
form and the one whose plan cannot regress.

---

## Q2 — rehydration scope, stated precisely

> **Engaged iff the latest `kill_switch.trip` exists and there is no
> `kill_switch.resume` with a higher `seq`.**

Time-unbounded, deliberately — the opposite of the breaker's ET-day window, and
the two must never be made to look alike (see Q3's note on where the code
lives). A Friday `/kill` is still in force on Monday. A trip from three weeks ago
that was never resumed is still in force, because nobody ever said otherwise,
and "nobody said otherwise" is the *only* correct reading of an unlifted halt.

**Ordered by `seq`, never by `ts`.** Two reasons, both load-bearing: `seq` is
app-assigned, monotonic and gapless across processes (`AuditLog._load_head`), so
it totally orders records the wall clock cannot; and `ts` is millisecond
UTC — a `/kill` and a `/resume` in the same millisecond would tie, and a tie in
this comparison decides whether the system trades.

**Reason and source are restored from the trip row's payload.**
`KillSwitch.check()` asserts `self._reason is not None` when engaged, so
`initially_engaged=True` with no reason is a broken object; the constructor must
refuse it (`ValueError`), matching the assert rather than tolerating it.

### When the read fails

**Fail closed to ENGAGED — not to a halt, and never to "assume not engaged."**

- "I cannot say whether a kill is in force" must never resolve to "it isn't."
  That is the whole failure this record exists to close.
- It must equally not resolve to a `HaltGrade.ALL` park. Coming up engaged is
  strictly weaker, it is the state the read was probably about to report, and it
  preserves `/status` and the watchdog — which matters most when the DB is sick.
- The reason is synthesized (`"kill-switch state unreadable at startup: …"`),
  the source is `restore_failed`, a Tier 1 `kill_switch.restore_failed` record
  is attempted best-effort, and a page fires.
- It self-escalates without help: if the audit DB is genuinely broken, the very
  next Tier 1 write latches `AuditLog.failed` → `on_failure` → `_halt_process`
  at `ALL`. The rail above is not needed here and would only pre-empt a better
  diagnosis.

This is a **deliberate divergence from `DayRiskRehydrationError`**, which is
terminal. The difference is that the breaker has no safe assumption available
(re-anchoring to a post-loss NLV is unsafe, and so is refusing to measure), while
the kill switch has one: engaged.

---

## Q3 — which trip sources rehydrate

**All of them. A trip is a trip.** Enumerated from source (`grep 'source="'`),
these are every `KillSwitch.engage` call site in the system:

| `source` | Reached from | Rehydrates |
|---|---|---|
| `http_endpoint` | `control.py:296`, `POST /kill` | yes |
| `reconciliation_mismatch` | `app.py:549` (startup), `session.py:505` (per-day) | yes |
| `broker_unreachable` | `app.py:564`, `:574` | yes |
| `day_risk_rehydrate` | `app.py:916` | yes |
| `audit_log` | `app.py:1348`, the `on_failure` hook | yes |
| `heartbeat` | `app.py:1353`, only if `trip_on_sustained_stall` | yes — **see the landmine below** |
| `entry_execution_exception` | `execution/entry.py:635` | yes |
| `watchdog_exception` | `execution/watchdog.py:3288` (`HALT_SOURCE`) | yes |

Every one of them arrives through `_halt_process` except `http_endpoint` and
`heartbeat`, and every one of them means a human has to look. Filtering by source
would mean deciding, in code, that some halts are less real than others — and
the source is *already* preserved in the payload and rendered on `/status`, so
the operator can make that judgement with better information than a constant
in a set literal could.

**The daily-loss breaker does NOT engage the kill switch — confirmed.**
`risk/breaker.py` holds no reference to it, and `PreTradeGate.evaluate` appends
two separate reasons (`kill_switch_engaged`, `daily_loss_breaker_tripped`) from
two independent rails. So the breaker's date-scoped rehydration and the kill
switch's unbounded one cannot contradict each other. **This is precisely why
CLAUDE.md's open decision says a breaker→kill-switch coupling should wait on this
one** — and after this design it is not merely "wait", it is a hazard with a
name:

> **Persistence makes any DAILY trip source a permanent halt requiring a manual
> `/resume`.** 2026-08-03 already warned that a per-day source halts *every day
> until someone restarts*. With persistence, the restart no longer clears it.

Two standing landmines get sharper and both must be restated wherever they live:

- **`[heartbeat] trip_on_sustained_stall` must stay `false`.** The chain
  subscription stalls the loop ~0.5s every morning; today that means a halt
  until a restart, and after this change it means a halt until a human presses
  `/resume` — **every single trading day, forever.**
- **A breaker→kill-switch coupling would convert a per-day loss latch into a
  permanent one.** The right question before any new automatic trip source is
  still 2026-08-03's: *"would this fire on an ordinary morning?"* — and the cost
  of getting it wrong just went up.

---

## The rehydration design (deliverable 3)

**A new module, `src/argus/risk/kill_state.py`** — deliberately NOT added to
`risk/rehydrate.py`.

The two reads look similar and have **opposite scoping rules**: one is bounded
to an ET day and must never leak across days; the other is unbounded and must
never acquire a day bound. `rehydrate.py`'s module docstring is written entirely
around "same-day", and its `_read_day_risk_state` carries a comment explaining
that the upper `ts` bound "is the episode boundary… which is how a per-day latch
leaks across days." Putting an unbounded query into that file invites exactly
the tidy-up that gives the kill switch a `day_start`/`day_end` and silently
un-halts it at midnight. Separate files; each docstring states the other's rule
and why it is not this one's.

```python
KILL_TRIP_CATEGORY   = "kill_switch.trip"      # exists today
KILL_RESUME_CATEGORY = "kill_switch.resume"    # new
KILL_RESTORED_CATEGORY = "kill_switch.restored"  # new, mirrors day_risk.restored

@dataclass(frozen=True)
class KillSwitchState:
    engaged: bool
    reason: str | None
    source: str | None
    trip_seq: int | None
    trip_ts: str | None
    restored: bool          # engaged AND read from a previous process's trip

async def load_kill_switch_state(audit: AuditLog) -> KillSwitchState: ...
```

**Wiring point: `Application.startup()`, immediately before the `KillSwitch` is
constructed** (`app.py:433`) — after `self._audit.start()`, before
`ControlServer` (which takes the switch) and therefore before the bind. It is a
read on the audit connection; it cannot bind, cannot reach the network, and
cannot fail in a way the fail-closed path above does not cover.

```python
kill_state = await load_kill_switch_state(self._audit)   # never raises; fails closed
self._killswitch = KillSwitch(
    self._audit, alert=self._alert,
    initially_engaged=kill_state.engaged,
    initial_reason=kill_state.reason,
)
```

**No `kill_switch.trip` is written by the restoration, and that falls out of
`engage`'s existing idempotency** — nothing calls `engage`, so nothing writes.
This matters more than it looks: it keeps the newest trip row the ORIGINAL press,
so `/status`'s `killswitch_since` shows *when the operator actually pressed it*
rather than when the box last rebooted, and the seq-ordering rule in Q2 stays
stable across arbitrarily many restarts.

**Instead, a restored kill announces itself once**, on the
`_record_restored_day_risk` precedent (`app.py:1038`) and for the identical
reason — a restart into a state already in force is otherwise invisible in the
trail:

- Tier 1 `kill_switch.restored`, payload
  `{trip_seq, trip_ts, source, reason}` — written after `process.start`, so the
  trail reads *process started → and it started halted*.
- One Slack page, `kill_switch_restored`. Not `kill_switch_engaged`: nothing was
  engaged, something was *found* engaged, and an operator paging through
  history must be able to tell a new halt from a re-read of an old one.
- A `logger.warning` naming the original trip's timestamp and source.

**`KillSwitch` API changes** (`risk/killswitch.py`), mirroring
`DailyLossBreaker(initially_tripped=...)`:

```python
def __init__(self, audit, *, alert=None,
             initially_engaged: bool = False,
             initial_reason: str | None = None) -> None:
    if initially_engaged and not initial_reason:
        raise ValueError(...)      # `check()` asserts a reason exists when engaged
```

---

## `KillSwitch.disengage` — and the ordering rule that is NOT symmetry

```python
async def disengage(self, *, source: str, detail: dict | None = None) -> bool
```

Returns True if this call is what cleared it. Idempotent: disengaging a
disengaged switch is a no-op that **writes no record** (so the seq-ordering rule
in Q2 stays clean — a bare `resume` with no live trip in front of it never needs
to mean anything).

**`disengage` writes the audit record FIRST and flips state only on success.
This is the exact inverse of `engage`, and it is not an inconsistency — the
ordering follows the SAFETY DIRECTION, not the method names.**

- `engage` flips first because halting is the safety action: it must not depend
  on a write that goes through `to_thread` and can be slow or fail. A halt with
  no record is bad; a halt that did not happen is worse.
- `disengage` is the **unsafe direction**. If it flipped first and the write
  failed, the process would resume trading while the durable state still said
  *halted* — so the next restart would re-halt, and in between, entries would
  flow with no record that anyone authorized them. Writing first makes a failed
  clear a **no-op**: still halted, `500` to the caller, nothing changed.

If the log has already latched failed, `record()` raises `AuditWriteError`,
`disengage` propagates it, and the switch stays engaged. Fail-closed twice over.

The alert fires **after** the successful write (`kill_switch_disengaged`), again
inverting `engage` for the same reason: `engage` alerts before its write because
a halt nobody was told about is the failure it exists to prevent; a resume
nobody was told about only matters if it actually happened.

---

## `POST /resume` — the endpoint spec (deliverable 2)

| | |
|---|---|
| **Method / path** | `POST /resume`, optionally with a query string. `GET` → `405 Allow: POST`. |
| **Listener** | The same `ControlServer`, same Tailscale-only bind, same always-on lifetime. No second port, no second process. |
| **Auth** | The same `ARGUS_KILL_TOKEN` bearer, constant-time, **never logged and never recorded** — `_serve` already authenticates *every* request before routing, so this is inherited, not re-implemented. A bad token is `401` + `control.auth_rejected` exactly as today. |
| **Required parameter** | `?confirm=yes`. Anything else (absent, misspelled, `no`) → `400 {"error": "resume requires ?confirm=yes"}`. |
| **Optional parameter** | `reason=<text>`, recorded (truncated to 200 chars) when present. |
| **Body** | Ignored, as today. The reader drains a bounded body and does not parse it; **this design adds no body parsing.** |
| **Environment gating** | None, matching `/kill`. It is proven in shadow so it is ready when it matters. |

### Status codes

| Code | Body | When |
|---|---|---|
| `200` | `{"status":"resumed","restart_required":false}` | Engaged, cleared, `kill_switch.resume` durably written, process not halted. Entries are live again **immediately** — no restart. |
| `200` | `{"status":"resumed","restart_required":true,"halt_grade":"all"\|"entries"}` | Same, but this process also holds a `HaltRecord`. The durable state is cleared so the **next** start comes up trading; this process will not. See Q5. |
| `200` | `{"status":"not_engaged"}` | Idempotent no-op, mirroring `/kill`'s `already_tripped`. **No record written.** |
| `400` | `{"error":"resume requires ?confirm=yes"}` | Missing/incorrect confirmation. |
| `401` | `{"error":"unauthorized"}` | Bad or missing bearer token. |
| `500` | `{"status":"engaged","audit":"write_failed"}` | The clear could not be recorded, so **it did not happen**. Still halted. |

### Q6 — authorization: why a required confirmation, and why not a second token

`/resume` is the only route in the system that *enables* risk. `/kill` is
one-tap by design because a mis-tap costs nothing; a mis-tapped `/resume` on a
phone in a pocket would resume trading on a system somebody deliberately
stopped — this record's own failure, arriving through a different door.
`?confirm=yes` makes the resume URL structurally different from the kill URL, so
no stray POST and no mis-fired shortcut can reach it, and it costs no body
parsing on a hand-rolled HTTP reader.

**A separate token is rejected.** It doubles the ways to be locked out at the
moment access matters most, it doubles the secret-rotation surface, and it buys
nothing against the actual threat model — the bind is tailnet-only and anyone
holding the kill token can already halt the system indefinitely. The gap it
would close (a compromised kill token being used to *un*-halt) is not the gap
that has ever cost anything here.

### Routing, stated so the implementation cannot widen `/kill`

Today `_serve` matches `if path != KILL_PATH: 404`, so `/kill?x=1` is a 404.
**That must not change.** The resume route is matched as `path == "/resume"` or
`path.startswith("/resume?")`, inserted *before* the existing `KILL_PATH` check,
and the query is split only on that branch. `/kill`'s matching is not touched by
a single character.

### The audit record

Tier 1 `kill_switch.resume`, and the `200` does not return until it has
committed — the same "200 means durably recorded" discipline `/kill` states,
and here it is doubly load-bearing because the write is what *permits* the flip.

```json
{"peer": "192.0.2.7",
 "cleared_trip_seq": 4821, "cleared_source": "http_endpoint",
 "cleared_reason": "kill switch tripped via HTTP control endpoint",
 "restart_required": false, "halt_grade": null,
 "reason": "<operator text, if supplied>"}
```

Peer IP only, never the token — the precedent is `control.auth_rejected`.

### The alert

`kill_switch_disengaged`, fired from inside `disengage` — the same
one-funnel-every-caller-passes-through discipline `engage` uses, so no future
caller can add a clear path that does not page. **A resume is at least as
alert-worthy as a trip**: it is the one event after which the system starts
taking risk again.

---

## Q5 — `/resume` while the process is parked or monitor-only

**Recommendation: (a) clear the durable state, report `restart_required: true`,
and leave the process exactly as it is.**

A `HaltRecord` is process-scoped and one-way by construction — first-writer-wins
on `reason`, strictest-wins on `grade`, cleared by nothing. **This design does
not make it clearable**, and that is a deliberate refusal, not an omission:

- The 2026-08-26 record's third requirement was "a clearable `_halted`". Two of
  its three are still refused; this is one of them.
- `HaltRecord.grade` only ever tightens (`HaltGrade.tighten`). A clear would be
  the first operation that relaxes it, and the invariant "an `ALL` halt is never
  relaxed by a later narrow verdict" is what makes the per-session re-derivation
  safe to run on an already-halted process.
- Under `ENTRIES`, `run_session` computes `monitor_only` **once**, at session
  start (`session.py:971`). Clearing the latch mid-session would not re-wire the
  entry hook anyway, so a "clearable `_halted`" would half-work — the worst
  possible outcome for a safety control.

Refusing the request outright ("restart required") was considered and rejected:
the durable clear is the *entire point* of the route, and refusing it would force
the operator to SSH in order to reach a state a phone should reach. So `/resume`
always clears the durable state and always disengages the in-process switch
(which keeps `/status` honest); it just tells the truth about whether this
process can act on it.

**This yields a one-restart runbook, in this order:**

```
assess the cause  →  POST /resume?confirm=yes  →  restart the service
```

Resuming *before* the restart is what makes it one restart instead of two. The
runbook must say so explicitly, because the intuitive order is the wrong one.

---

## Q7 — idempotency and ordering

- `/kill` on an engaged switch → `200 already_tripped`, no second trip row
  (today's behaviour, unchanged).
- `/resume` on a disengaged switch → `200 not_engaged`, **no resume row.**
- `kill → resume → kill → restart` rehydrates **engaged**: latest trip
  `seq = 12` > latest resume `seq = 11`. The comparison is between the two
  latest rows only; no scan, no history walk.
- `kill → resume → restart` rehydrates **disengaged**: resume `seq = 11` > trip
  `seq = 10`.
- Two `/resume` calls in flight concurrently: both authenticate, both reach
  `disengage`, and `disengage` is guarded by the same "already in the target
  state" early return `engage` uses — the second is a no-op. The control server
  handles connections on separate tasks but one event loop, and neither
  `engage` nor `disengage` awaits between reading `_engaged` and writing it in
  the direction that matters (`engage` flips before awaiting; `disengage` writes
  first, then flips, and a second caller that arrives mid-write sees
  `_engaged = True` and does its own write — producing at most one redundant
  `kill_switch.resume` row, which is harmless under Q2's rule). If that
  redundancy is judged untidy, an `asyncio.Lock` on the switch is the fix; a
  check-then-act "optimization" is not.

---

## Q8 — the clean-shutdown path

**The core fix reads nothing about shutdown cleanliness, and that is the
answer.** Engagement is decided entirely by `trip` vs `resume` ordering, so:

> A process that was never halted has no `kill_switch.trip` row after its last
> `kill_switch.resume`, therefore rehydrates **disengaged**, therefore trades.

Deploys, overnight reboots and OOM restarts of an un-halted process all keep
working unattended, unchanged, with no manual step — because nothing about them
is even consulted. **The fix is structurally incapable of imposing a halt on a
process that was never halted**, and the test list pins that as its own case.

For the record, "clean" *would* mean: a `process.stop` record after the newest
`process.start` (written by `Application.shutdown`, `app.py:614`, and skipped
when the audit log has latched failed).

### The generalization — evaluated, and recommended AGAINST for now

*Should any unclean restart force monitor-only, even with no kill pressed?*

It is nearly free to **detect** (one more O(1) query of the same shape) and
expensive to **act on**:

- `process.stop` is skipped whenever the audit log latched failed — the exact
  condition that already halts — so the two overlap where it matters and the
  extra rail adds nothing there.
- A `SIGKILL` after a slow drain (Docker's stop timeout) writes no
  `process.stop`, so an ordinary deploy can look unclean.
- The cost of a false positive is a full trading day of no entries, discovered
  at 09:35 by someone who has to be awake to press `/resume`. An overnight
  droplet reboot at 03:00 would silently cost the next session.

**Recommendation: measure before deciding.** Add `previous_shutdown:
"clean" | "unclean" | "unknown"` to the existing `process.start` payload — one
extra O(1) read at startup, no behaviour change, no config key — and revisit
once there is a month of data on how often unclean restarts actually happen and
what caused them. Building the rail first would be sizing a bound from a guess,
which is the thing this repo refuses to do everywhere else.

---

## Q9 — what `/status` shows after the fix

Three changes, all in `status.py`, all small — **and the first one was designed
wrong. Corrected at build time, recorded here rather than quietly fixed:**

1. ~~**Delete the `if ks.engaged` gate on the trip row**~~ — **KEPT.** The design
   said to read `db.kill_trip` unconditionally. That is wrong, and the existing
   comment on that line says why: `source`/`since` next to `engaged: false` read
   as a live halt that is not happening. **The gate was never the defect** — the
   defect was that `ks.engaged` could be false while a halt was in force, so the
   page hid a row it should have shown. Rehydration fixes that at the source: the
   object is now built from that very row at startup, so the two can no longer
   disagree, and the gate becomes *correct* rather than obsolete. Deleting it
   would have introduced the mirror bug — a RESUMED trip rendering as a live one
   — which is the same misreport class this record exists to close, one direction
   over. The misreport closes as a **consequence** of the rehydration, with no
   change to this line at all. Pinned by
   `test_status_service_still_gates_the_trip_row_on_the_live_switch`.
2. **Add `killswitch_restored: bool`** to `StatusSnapshot`, read off the live
   switch (`KillSwitch.restored`, not the row — the same row is the source of
   `since` in both cases, so it cannot answer "was this inherited?"). Rendered as
   the badge `ENGAGED (RESTORED)` plus a detail line saying `/resume` clears it
   and a restart does not. Without it the page can tell an operator *that* the
   system is halted but not that the halt **predates the process they are looking
   at**, which is the single most decision-relevant fact after a restart.
   `restored` is False whenever nothing is engaged, and a `/kill` pressed after a
   `/resume` reads as this process's own.
3. **Correct the HALTED row's prose.** The snapshot's comment said *"engaged
   means no new entries and a `/kill` at 10:00 leaves a process that resumes
   tomorrow"* — which stops being true. Now: engaged means no new entries **until
   a human clears it with `POST /resume`**; halted means additionally that THIS
   process will not trade again whatever anyone does.

**Coherence check for a restored-kill session:** `halted: false`,
`halt_grade: null`, `killswitch_engaged: true`, `killswitch_restored: true`,
`killswitch_source`/`since` from the original trip, phase `running`,
`breaker_tripped` whatever today's rail says. That page is honest and reads
correctly: the process is healthy, watching and flattening; it is not halted; it
will enter nothing. **For a parked process that has been resumed**:
`halted: true`, `halt_grade: "all"`, `killswitch_engaged: false` — coherent,
because HALTED is strictly stronger and is rendered first, above everything
whose meaning it changes.

---

## What must NOT change

- **No automated halted→trading transition.** Rehydration only ever *restores* a
  halt; it can never clear one. `/resume` is the only clear and it is reachable
  only through an authenticated HTTP request. **Nothing in `src/` may call
  `disengage` except the `/resume` handler** — worth an AST test, on the
  precedent of the "trading is built inside a conditional" and "`residual_orders`
  reads no `.direction`" tests.
- **A trip still does not stop exits, the watchdog, or the flatten.** A restored
  kill is a kill; the AST test pinning `ExitExecutor`'s absent `halt_check`
  covers it unchanged.
- **A trip still does not stop the process.** The switch owns no stop event, and
  restoring one must not give it one.
- **The token never lands in a log or an audit record**, on either route,
  success or failure.
- **`/kill`'s route matching, ordering and semantics are untouched.**
- **`_halted` stays one-way.** No clear, no relax, no `grade` that loosens.
- **The read stays two bounded index-backed queries** on `AuditLog.read`. Do not
  add a third, do not add a scan, do not give it a date range.

## What this does NOT fix

- **A wiped volume loses the halt.** The state is as durable as `argus.db`. A
  container recreate or a deploy keeps it (same volume); a restored-from-scratch
  droplet does not. Named, not solved — the alternative is an off-box state
  store, which is a much larger dependency for a rarer failure.
- **A halt imposed on a process that is not running.** There is nothing to
  press. The state is only read at startup, so a kill cannot be *pre-armed* for
  a process that has not started; if that is ever wanted, it is a different
  design (a state file the operator writes), not this one.
- **The breaker→kill-switch coupling.** Still an open decision, and this record
  makes the case against a naive version sharper rather than settling it.
- ~~**`_end_day` stopping the watchdog on a live position.** Untouched, still not
  a halt, still open.~~ **CLOSED the same day**: it ends ENTRIES alone now, through
  a session-scoped latch. It is still not a halt and deliberately so — no kill
  switch, no `HaltRecord`, and it clears at the rollover, which is the exact
  opposite of what this record made durable. The two are the pair `/status` renders
  as two rows: one needs `POST /resume`, the other needs nothing.
  → [end-day keeps the watchdog](2026-09-01-end-day-keeps-the-watchdog.md)

---

## Test list (deliverable 5)

**BUILT: `tests/test_kill_persistence.py`, 33 tests.** Each numbered case below is
covered; the file adds coverage the design did not enumerate (both grades of
`restart_required` parametrized, the `disengage` idempotency and
attribution cases, and the two structural assertions). **Four mutations were run
and each fails the suite** — no rehydration at all (7 failures, the pre-change
behaviour); `if resume is not None` instead of the `seq` comparison (1); a `ts`
bound added to the read (2); `disengage` flipping before it records (2).

**Rehydration — the restart matrix.** Driven the way
`tests/test_day_risk_rehydrate.py` drives the breaker's: one audit DB, two
`Application` lifecycles over it.

1. `kill → restart` → the new process's switch is `engaged`, with the ORIGINAL
   reason and source, and `PreTradeGate.evaluate` denies on
   `kill_switch_engaged`. **(The headline case; fails today.)**
2. `kill → resume → restart` → `not engaged`, gate allows.
3. `kill → resume → kill → restart` → `engaged`. Pins the seq comparison; a
   `ts`-ordered or "any resume exists" implementation passes 2 and fails this.
4. **Never halted, clean shutdown → restart → trades.** The unattended-deploy
   guarantee. Must also hold with `process.stop` ABSENT (unclean), to pin that
   the read consults nothing but trip/resume.
5. **Day rollover with a trip in force** → still engaged the next trading day.
   The date-bound mutation (someone "harmonising" this with `rehydrate.py`) must
   fail here.
6. `reconciliation_mismatch` halt → restart with a now-CLEAN reconcile → the
   process is `engaged` and NOT `halted`, the scheduler runs, the watchdog is
   built, and no entry is taken. Pins Q4: monitor-only, not parked, not trading.
7. Unreadable/corrupt latest trip payload → comes up **engaged** with the
   synthesized reason, `kill_switch.restore_failed` recorded, alert fired — not
   disengaged, and not parked.
8. Restoration writes **no** `kill_switch.trip` row, and `/status`'s
   `killswitch_since` still reports the ORIGINAL trip's timestamp after two
   restarts.
9. `kill_switch.restored` (Tier 1) + exactly one `kill_switch_restored` alert
   per restored start; none when nothing was restored.

**`/resume`, over a real TCP request to a real `ControlServer`** — the
`tests/test_kill_scope.py` discipline, because `test_control.py` wires a bare
switch with nothing behind it:

10. `POST /resume?confirm=yes` with a valid token → `200 resumed`, switch
    disengaged, Tier 1 `kill_switch.resume` present, alert fired.
11. Missing/incorrect `confirm` → `400`, switch **still engaged**, no record.
12. Bad/absent token → `401`, `control.auth_rejected`, switch still engaged, and
    the offered token appears in no log line and no record.
13. `GET /resume` → `405 Allow: POST`. `/resumex`, `/resume/` → `404`.
14. `/kill?confirm=yes` → still `404`. Pins that the query split did not widen
    the kill route.
15. `/resume` when not engaged → `200 not_engaged`, **zero** `kill_switch.resume`
    rows.
16. **Audit write fails during `/resume`** → `500`, and the switch is **still
    engaged**. The ordering test: an implementation that flips before writing
    passes every other case and fails this one.
17. `/resume` on a parked (`ALL`) process → `200` with
    `restart_required: true`, durable state cleared, `_halted` unchanged, no
    session starts. Then a restart → comes up trading.
18. `/resume` on an `ENTRIES`-halted process → same, and the CURRENT session
    still takes no entry.

**Structural / AST:**

19. No module in `src/` outside the `/resume` handler references `disengage`.
20. `KillSwitch(initially_engaged=True, initial_reason=None)` raises.

**Status:**

21. Restored kill → `/status` JSON has `kill_switch.engaged: true` with non-null
    `source`/`since`/`restored`, and the HTML shows the ENGAGED badge with the
    original press time. Today's code renders green with nulls here.

---

## Live-flip checklist — what this closes and what it adds

Closes: **"a halt in force survives a restart."** It is not on the checklist
today because it was an open decision rather than a known-broken rail; it should
be on it either way.

- [ ] **Kill persistence proven in cert**: `POST /kill`, `systemctl restart
      argus`, confirm `/status` shows `kill_switch.engaged: true` with
      `restored: true` and the ORIGINAL timestamp, and confirm the session takes
      no entry.
- [ ] **`/resume` proven in cert**: `POST /resume?confirm=yes`, confirm entries
      resume within one cadence tick with no restart, and confirm a
      `kill_switch.resume` row and a Slack page.
- [ ] **`[heartbeat] trip_on_sustained_stall = false` re-verified in
      `argus.live.toml`** — with persistence this key is a daily manual `/resume`,
      not a daily restart.
- [ ] **The phone shortcut for `/resume` exists and is distinct from the `/kill`
      one** (different URL, deliberately not adjacent on the home screen).

## Runbook step for `README.md` (deliverable 6)

To be added directly under the existing kill-switch section, whose closing
sentence — *"the halt persists across day-rollover until the process is
restarted"* — must be corrected in the same edit to *"until a human clears it
with `POST /resume`; a restart does not clear it."*

````markdown
### Clearing a halt — `POST /resume`

The mirror of `/kill`, on the same endpoint and the same token. Nothing else
clears a halt: a restart, a deploy and a reboot all come back halted.

```bash
curl -fsS -XPOST -H "Authorization: Bearer $ARGUS_KILL_TOKEN" \
     "http://<tailscale-ip>:<port>/resume?confirm=yes"
```

`?confirm=yes` is required — this is the one control that lets the system take
risk again, so it is deliberately not one-tap.

| Response | Meaning |
|---|---|
| `200 {"status":"resumed","restart_required":false}` | Cleared and durably recorded. Entries resume within a cadence tick; no restart needed. |
| `200 {"status":"resumed","restart_required":true}` | Cleared durably, but THIS process also hit a rail (see `/status`'s HALTED row). Restart it now — it comes up trading. |
| `200 {"status":"not_engaged"}` | Nothing was halted. Safe no-op. |
| `400` | Missing `?confirm=yes`. |
| `500 {"audit":"write_failed"}` | The clear was NOT recorded, so it did not happen — still halted. Investigate the DB (§9). |

**Order matters when the process is also halted: assess the cause, then
`/resume`, then restart.** Resuming first makes it one restart instead of two.
````

---

## Sequencing

`src/` only — **no new config key, so the image deploys alone.** That is worth
stating plainly given how many recent changes forced a coupled config deploy;
this one does not, and it should not acquire one (a `[control] allow_resume`
flag would be a way to ship a safety control switched off).

New audit categories: `kill_switch.resume`, `kill_switch.restored`,
`kill_switch.restore_failed` — all Tier 1, all additive, no migration.
`README.md`'s audit-category table gains three rows.

The change is independent of every open item in flight and touches no file the
exit-ladder work touches. It is a good standalone deploy day.
