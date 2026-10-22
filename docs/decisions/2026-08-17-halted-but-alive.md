# A halted process parks; it does not exit (2026-08-17)

*The always-on control surface was unreachable during exactly the failure it
exists for, and the process exited into a restart loop that could not fix the
condition.*

> Settled. Found by the operator during the 2026-08-14 incident: "I could not
> access the status or kill switch pages." See [`README.md`](README.md) for the
> index.

## What was wrong

Two defects, one root cause: **the control surface's lifetime was tied to a
successfully-started process.**

**1. The surface went down with the halt, and could not come back.**

At 09:40:02 on 2026-08-14 the per-session reconcile mismatched, called
`_halt_process`, and the process shut down cleanly (exit 0) — taking `/status`
and `/kill` with it. Docker restarted it, and from then on the failure moved to
the *startup* reconcile, where the ordering was:

```python
await self._reconcile()      # raises
self._heartbeat.start()
await self._control.start()  # never reached
```

So the listener never bound again across 33 further restarts. For the whole
event the operator had no `/status` to see why and no `/kill` to stop it — only
SSH.

This contradicted a stated non-negotiable. CLAUDE.md calls `POST /kill` the
always-on surface, "reachable in one action, from a phone," and the comment
directly above the offending lines called it "the always-on phone-reachable kill
surface." It was on only while a fully-started process was running.

Two invariants collided and nobody noticed one was losing. The ordering was
deliberate, for a different and still-valid reason — "a mismatch must not leave a
half-live process," and a bad bind must refuse startup rather than fall back to a
wider address. The consequence for the always-on property was simply never
considered.

**2. The restart loop was unbounded, and re-announced itself every iteration.**

`restart: unless-stopped` has no attempt cap, and systemd's `StartLimitBurst=5`
guards the *compose* process, not the container inside it. 34 iterations fired
`reconciliation_mismatch` + `kill_switch_engaged` each time — roughly 68 Slack
messages — and would have continued all weekend.

A reconciliation mismatch is a permanent condition requiring a human. Retrying it
cannot succeed by construction. The process was right to refuse; the supervisor
was wrong to keep asking.

## What the incident cost, stated honestly

Nothing was trading. The broker was flat, no order was working, and `/kill` halts
new entries rather than flattening, so even reachable it would have had nothing to
do. **The loss was observability**, plus the inability to stop the noise without a
shell.

The version that matters is the one that has not happened yet: a mid-session
crash, a restart, and a mismatch caused by a **real open position**. Then the
watchdog never starts (the position is unmanaged), the surface never binds (it is
also invisible), and the only route to either fact is SSH. That is a live-account
hazard, and it is why this was fixed rather than filed.

## The rule

**An unrecoverable halt parks the process instead of exiting it.** Halted, fully
observable, unable to trade.

Two events now, and the split is the whole design:

- **`_stop`** — "stop TRADING." End the session, start no more. Set by every halt.
- **`_exit`** — "leave the PROCESS." Set only by an operator signal
  (SIGINT/SIGTERM), via `request_exit`.

`run()` therefore has three ways out: signalled (shut down, unchanged), halted
(park and wait for a signal), and startup-halted (park without ever running the
scheduler).

Supporting changes, each load-bearing:

- **The control server binds BEFORE the reconciliation rail**, and
  `process.start` is recorded there too — by that point the process genuinely has
  started, and a halt after it reads as a state transition rather than as a
  process that never existed. A bad bind still tears down and re-raises; that
  property is untouched.
- **`HaltRecord` latches FIRST-writer-wins.** Halts cascade — a mismatch stops the
  day and the audit log may then latch failed on the way down. The first reason is
  the cause; overwriting it with a downstream symptom is how a post-mortem loses it.
- **One announcement, at one place.** `_await_exit_while_halted` writes the Tier 1
  `process.halted` record and fires the single `process_halted` alert. Every
  halting path — startup reconciliation, the per-session mismatch via
  `_halt_process`, the audit-failure hook — funnels through it, so there is exactly
  one of each no matter how the halt arrived.
- **`/status` reports `halted` explicitly**, separately from the kill switch,
  because they are different facts: engaged means "no new entries" and a `/kill` at
  10:00 leaves a process that resumes tomorrow; halted means "no further sessions,
  and a restart will not help."
- **`_run_session_day` refuses outright when halted.** `run()` already skips the
  scheduler, but this is the choke point every session path shares — including
  `run_single_session`, which calls it directly and would otherwise trade a halted
  process now that `startup()` completes instead of raising. This is the one sharp
  edge the change introduced, and it is closed at the point both paths cross.

## Why the restart loop needs no separate fix

A parked process does not exit, so Docker never restarts it and the storm cannot
form. The restart policy is deliberately left alone: a process that dies from a
genuine bug still exits non-zero and still gets restarted, which is what that
policy is for. Only *deliberate* halts stop looping.

## The tradeoff, stated because it is real

A parked process is `active (running)` in `systemctl status`, indistinguishable at
a glance from a working one. Three things carry the distinction instead: the
single Slack alert, `/status` (which shows HALTED first, above everything whose
meaning it changes), and a WARNING line repeated to the journal every 15 minutes —
often enough that a tail shows it promptly, rare enough that a multi-day halt does
not bury its surroundings.

That is a worse *default* visibility than a dead unit, and it was accepted
deliberately: a dead unit was not visible on 2026-08-14 either. It was in a
restart loop, showing as active, with the surface down.
