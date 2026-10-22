# Argus: The Kill Switch Does Not Survive a Restart

## Priority

**DESIGN FIRST — do not implement without Richard's sign-off.** This changes the
behaviour of the one rail CLAUDE.md calls non-negotiable, and it deliberately
reverses part of an existing decision
([`docs/decisions/2026-08-26-reconcile-broker-unreachable.md`](../decisions/2026-08-26-reconcile-broker-unreachable.md),
quoted below) that argued for keeping the kill switch a one-way latch. That
tradeoff needs to be revisited on purpose, not routed around.

**This is a live-flip blocker.** In sandbox a spurious resume just re-proposes
cert orders. Live, the halt an operator pressed silently stops being in force
after any restart, and the restart is invisible to the person who pressed it.

## The issue

`KillSwitch.__init__` ([`src/argus/risk/killswitch.py`](../../src/argus/risk/killswitch.py))
sets `self._engaged = False` unconditionally and the object holds no persistent
state. A trip writes a durable Tier 1 `kill_switch.trip` row, but nothing reads it
back on startup. The engaged/not-engaged state lives only in one process's memory.

### The failure

1. Operator presses `POST /kill` at 10:00. Entries halt, exits keep running, the
   operator walks away trusting the halt is in force.
2. The process restarts — container OOM or crash, droplet reboot, a deploy
   (`argus-deploy.sh` restarts the service), or a crash-loop.
   `restart: unless-stopped` + systemd `Restart=always` all do this.
3. The new process constructs `KillSwitch` with `_engaged = False`, reconciles
   clean, starts a session, and **entries resume**.
4. No alert fires — `KillSwitch.engage` was never called this time. `/status`
   shows `kill_switch.engaged: false`. Nobody is watching, because everybody
   believes the system was stopped.

### `/status` misreports it too

`status.py` reads `killswitch_source` / `killswitch_since` from the newest
`kill_switch.trip` row **only when `ks.engaged` is true**
(`trip = db.kill_trip if ks.engaged else None`). After a restart-while-halted,
`ks.engaged` is false, so the page shows green with null source/since — the
reassuring reading at the one moment it is wrong. (Contrast the `daily_loss.breach`
defect fixed on 2026-08-20, and the breaker's own rehydration in
[`src/argus/risk/rehydrate.py`](../../src/argus/risk/rehydrate.py) — the kill switch
never got the equivalent.)

## Why the obvious fix is not obvious

The daily-loss breaker had the identical bug and was fixed by rehydrating today's
trip from the audit log per trading date (`risk/rehydrate.py`). The kill switch did
not get the same treatment because it raises a question the breaker never had to
answer.

- The breaker latch is **naturally per-trading-date** — it resets every morning
  anyway — so "restore today's trip" has an obvious expiry.
- The kill switch is deliberately **process-lifetime with no reset**: a day
  rollover must never un-halt it (one of the standing landmines in CLAUDE.md — the
  `trip_on_sustained_stall` reasoning). So "rehydrate it" forces: *for how long
  does a restored trip stay in force?*

And [`docs/decisions/2026-08-26-reconcile-broker-unreachable.md`](../decisions/2026-08-26-reconcile-broker-unreachable.md)
explicitly avoided the machinery a clear would need:

> The park never enters the halted state while it is still trying — the literal
> "halt then clear it" would have needed `KillSwitch.disengage`, a clearable
> `_halted`, and the first automated halted→trading transition in the system; the
> surface binds before the rail, so the loop sits in FRONT of both one-way latches
> instead.

## The direction (professional-desk framing)

The "for how long" question dissolves under the right principle, so lead with it.

**An automated strategy never resumes trading on its own after a halt or an
unclean stop. The default startup posture is halted-pending-explicit-clearance,
regardless of why the process restarted.** A restored kill does not expire — it is
cleared only by a deliberate, authenticated, audited human action.

That gives:

1. **Halt state is state of the world, not state of a process.** It lives durably
   with reason + timestamp + source. The process *reads* it on startup; it never
   *owns* it. A restart or a deploy cannot clear it as a side effect.
2. **On startup, if halted, come up in monitor-only** — watchdog runs, exits and
   the `FLATTEN_BEFORE_CLOSE` trigger run, entries refused. This is already how a
   live manual kill behaves (the switch does not call `_halt_process`; the gate
   just denies on `kill_switch_engaged`), and it is the `HaltGrade.ENTRIES` posture
   P3 built for the reconcile path. The fix should reuse that posture, never `ALL`
   (which parks and loses the watchdog — the opposite of what we want).
3. **Add `POST /resume` — the mirror of `/kill`.** Authenticated, idempotent,
   writes a Tier 1 `kill_switch.resume` record, alerts, and calls a new
   `KillSwitch.disengage`. Never automatic, never a side effect of anything. This
   is the human-authenticated halted→trading transition — categorically different
   from the *automated* one the 2026-08-26 decision was protecting against.
4. **Distinguish clean from unclean shutdown.** A process that was never halted and
   shut down cleanly (SIGTERM, `process.stop` written) comes back trading normally —
   unattended overnight restarts and deploys must keep working. Anything else is a
   candidate for coming up halted. Whether an *unclean restart with no kill
   pressed* should itself force monitor-only is a larger policy call — evaluate it
   and recommend, but the core deliverable is kill-switch persistence.

## Design questions to resolve

1. **Where does the durable state live?** The audit log (new `kill_switch.resume`
   category, no migration, matches `rehydrate.py`) vs. a dedicated state row/file
   on the volume. Weigh: the audit log is already THE durable single-writer state
   store, but `AuditLog.read`'s contract is bounded index-backed queries only —
   confirm "latest `kill_switch.trip`, and is there a `kill_switch.resume` after
   it?" is two O(1) queries on the `(category, seq)` ordering.
2. **Rehydration scope.** The query is time-unbounded (must survive rollover),
   unlike the breaker's date-scoped one. State it precisely: engaged iff the latest
   `kill_switch.trip` has no `kill_switch.resume` with a higher `seq`.
3. **Which trip sources rehydrate?** Enumerate every `KillSwitch.engage` call site
   and its `source` (`http_endpoint`, `reconciliation_mismatch`, the audit-failure
   hook, `heartbeat` if ever enabled). Recommend: all of them (fail closed — a trip
   is a trip). Confirm the daily-loss breaker does **not** engage the kill switch
   (they are independent rails with separate gate reasons) — if it does, its
   date-scoped rehydration and the kill switch's unbounded one would contradict.
4. **Interaction with `_halt_process` / `HaltRecord` (the park).** A
   reconcile-mismatch or audit-failure halt engages the switch **and** parks the
   process. After the operator fixes the cause and restarts:
   - Rehydration restores `_engaged = True` even though the reconcile now passes —
     which is the *desired* desk behaviour (a human must assess before resuming).
     Confirm the session then comes up monitor-only (`ENTRIES` posture) rather than
     re-parking, since the `HaltRecord` is process-local and not itself rehydrated.
   - Does `HaltRecord` / `_halted` need its own rehydration too, or is
     "kill switch restored → monitor-only, operator hits `/resume` when satisfied"
     sufficient? Recommend explicitly.
5. **`/resume` while the process is parked.** A parked process (2026-08-17
   halted-but-alive) serves `/status` and `/kill` but runs no scheduler. Should
   `/resume` (a) clear the durable state so the *next restart* comes up trading, or
   (b) be refused with "restart required"? Recommend.
6. **`/resume` authorization.** Same `ARGUS_KILL_TOKEN`, or is re-enabling trading
   high-stakes enough to want a separate token / an explicit confirmation
   parameter / a required `reason` field in the body? Recommend.
7. **`/resume` idempotency and ordering.** `/resume` when not engaged → 200 no-op,
   mirroring `/kill` when already tripped. `/kill`→`/resume`→`/kill` must
   rehydrate correctly off `seq` ordering.
8. **The clean-shutdown path.** Define exactly what "clean" means in terms of audit
   records (`process.stop` after the last `process.start`, switch not engaged).
   Make sure the fix cannot impose a halt on a process that was never halted.
9. **What `/status` shows after the fix.** `killswitch.engaged: true` with
   `reason` / `since` from the trip row (which `status.py` already reads, just
   gated behind `ks.engaged`) — so this fix also closes the `/status` misreport.
   Confirm the HALTED row / `halt_grade` rendering is coherent for a
   rehydrated-kill monitor-only session.

## Code touchpoints (for the design to map precisely)

- [`src/argus/risk/killswitch.py`](../../src/argus/risk/killswitch.py) —
  `KillSwitch` gains a rehydration seam (constructor arg like
  `initially_engaged` / `initial_reason`, matching `DailyLossBreaker(initially_tripped=...)`)
  and a `disengage(reason, *, source)` method. Keep `engage` idempotent and
  alert-on-the-edge; `disengage` mirrors it.
- [`src/argus/risk/rehydrate.py`](../../src/argus/risk/rehydrate.py) — the pattern
  to follow; the new read may belong here or beside it.
- [`src/argus/control.py`](../../src/argus/control.py) — new `POST /resume` route,
  same bind / same auth / same "200 means durably recorded" discipline as `_kill`.
- [`src/argus/app.py`](../../src/argus/app.py) — `startup()` wires the rehydrated
  state into the `KillSwitch` it constructs; `_reconcile` / `_run_session_day` /
  `run` decide the monitor-only posture (reuse the `HaltGrade.ENTRIES` plumbing,
  do not invent a parallel path).
- [`src/argus/session.py`](../../src/argus/session.py) — `run_session` already
  withholds `submit` under `ENTRIES`; confirm a rehydrated kill reaches the same
  state.
- [`src/argus/status.py`](../../src/argus/status.py) — the `trip = db.kill_trip if
  ks.engaged else None` line, and the HALTED / `halt_grade` rendering.
- [`docs/decisions/2026-08-03-kill-switch-scope.md`](../decisions/2026-08-03-kill-switch-scope.md),
  [`2026-08-17-halted-but-alive.md`](../decisions/2026-08-17-halted-but-alive.md),
  [`2026-08-26-reconcile-broker-unreachable.md`](../decisions/2026-08-26-reconcile-broker-unreachable.md),
  [`2026-08-29-halt-scope-split.md`](../decisions/2026-08-29-halt-scope-split.md),
  [`2026-08-20-daily-loss-breaker-restart-rehydration.md`](../decisions/2026-08-20-daily-loss-breaker-restart-rehydration.md)
  — read before touching anything; the design must state how it reconciles with
  each.

## Deliverables

1. A **decision record** in `docs/decisions/` (dated, added to
   `docs/decisions/README.md`, and the CLAUDE.md non-negotiable / open-decisions
   entries updated): the bug, the desk principle, the answers to every design
   question above, and how it reconciles with the 2026-08-26 one-way-latch
   reasoning (the resolution being: no *automated* clear; a human-authenticated
   `/resume` is a different thing).
2. The **`/resume` endpoint spec** — method, auth, body, status codes, idempotency,
   audit record shape, alert, behaviour while parked.
3. The **rehydration design** — exact query, scope, which sources, wiring point,
   and the `AuditLog.read` bounded-query confirmation.
4. The **startup posture** — precisely which existing mechanism carries "up but
   monitor-only" and why it is not a new code path.
5. A **mutation / test list** — the restart scenarios that must each fail a test:
   kill→restart stays halted; kill→resume→restart comes up trading;
   kill→resume→kill→restart stays halted; clean shutdown of an un-halted process →
   trades; reconcile-mismatch halt→restart-after-fix comes up monitor-only not
   parked and not trading.
6. The **live-flip checklist** line this closes, and the operator runbook step for
   `/resume` (the phone one-liner, mirroring the `/kill` one).

## Non-negotiables this must respect

- **No automated halted→trading transition.** Rehydration only ever *restores* a
  halt; only `/resume` (human, authenticated, audited) clears one.
- The kill switch still must not stop exits, the watchdog, or the flatten — a
  restored kill is monitor-only, never a park.
- `/resume` must never be reachable without auth, and the token still never lands
  in a log or an audit record (same discipline as `/kill`).
- A process that was never halted and shut down cleanly must still restart
  unattended and trade — the fix cannot make every deploy require a manual
  `/resume`.
- `NEVER TOUCH THE LIVE DATABASE` for any exploration; `AuditLog.read`'s
  bounded-query contract holds for the new read.

## Out of scope

- Implementing it.
- Generalising to "any unclean restart forces monitor-only regardless of a kill" —
  evaluate and recommend, but building it is a follow-up unless the design shows it
  is free.
- The daily-loss breaker's cert-inertness and the shared-live-account issue
  (separate prompts).
- A breaker→kill-switch coupling — CLAUDE.md's open decision says that should wait
  on this one; note it, do not build it.
