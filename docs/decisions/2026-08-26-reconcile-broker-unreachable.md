# A broker that cannot be ASKED is not a broker that DISAGREED (2026-08-26)

*A cert 502 on the process-start reconcile killed the process five times in 33
seconds, with `/status` and `/kill` down for every one of them. The same evening,
a `ReadTimeout` on the per-day reconcile was filed as "a genuine bug".*

> Settled. Design and alternatives:
> [`docs/designs/2026-08-26-reconcile-broker-unreachable.md`](../designs/2026-08-26-reconcile-broker-unreachable.md)
> (approved with three amendments at sign-off). Task prompt:
> [`docs/tasks/argus_reconcile_broker_unreachable_prompt.md`](../tasks/argus_reconcile_broker_unreachable_prompt.md).
> Ships in v0.3.11. **The config deploys WITH the image** — a new REQUIRED
> `[reconcile]` section.

## What was wrong

**Reconciliation had a verdict for "the broker disagrees" and no verdict at all
for "the broker did not answer."** Three call legs, none of them handled:

| leg | what it raised | where it landed |
|---|---|---|
| `reconciler.reconcile()` → `fetch_positions` | raw `TastytradeError` | `startup()`'s `except BaseException` → teardown → **process exit** |
| `_fetch_accounts` exhaustion | `SessionAborted` | the same handler — **also an exit**, despite already having a retry and a domain exception |
| `tt.open()` (OAuth) | raw SDK/httpx error | the same handler — **also an exit**, with no retry at all |

`v0.3.10` was deployed intraday on a confirmed blackout day. The restart hit cert
returning a 502; the process exited; Docker's `restart: unless-stopped` restarted
it; cert 502'd again. **Five `process.start` records in 33 seconds** (`seq`
472443–472451, seven `process.start` across the whole incident), no
`process.stop` between any of them, and no `reconcile.result` — the process died
before the comparison every time, until cert answered on the fifth attempt.

For the whole episode there was no `/status` to read a reason from, no `/kill`,
and nothing written down about why. A `reconciliation_mismatch` halt gives all
three; this gave none.

The per-day site failed differently and just as wrongly: a `ReadTimeout` out of
`_reconcile_or_abort` propagated past `run_session`'s `except SessionAborted`
(it is not one) to `app.py`'s generic handler, recorded as `session.error` under a
comment reading *"reaching here means a genuine bug."* A network timeout against
an API this codebase's own comments document as *"hanging and 502-ing"* is not a
bug.

## The invariant, and how it is held

**Never let a retry blur "the broker is unreachable" into "the broker answered and
disagrees."** A mismatch must never be retried past — that is the whole rail.

It is held by **mechanism, not discipline**, and the mechanism was already in the
code:

> **A mismatch is a VALUE. Unreachable is an exception.**

`Reconciler.reconcile()` *returns* `ReconciliationResult(ok=False)`; it has never
raised on disagreement. `ReconciliationError` is constructed by the caller, after
the value comes back. So a retry loop wrapping a call that returns broker data
**cannot reach a mismatch**: a mismatch returns, which exits the loop on the first
pass having spent no attempts. No exception-type list is involved and nobody has to
remember a rule.

The second half is that a **bug** must not be retried either, and that is drawn by
**position in the call graph, not by exception type**. `_ask_broker` wraps exactly
one port await and nothing else, so the compare, the expected-position read and
the audit write are outside every `try` and are structurally out of scope. This
matters because a type list would have been wrong anyway: the 2026-08-26 traceback
carries a raw `httpcore2.ReadTimeout`, not the `httpx2` type a list would name, and
[`account/port.py`](../../src/argus/account/port.py) already documents why
third-party class names are the wrong thing to build a rail on.

**Exactly one exception type is load-bearing, and it is an exclusion.**
`AuditedExecution.get_live_orders` writes a Tier 1 `order.unmappable_live` record
*inside* the port call, so an `AuditWriteError` can surface from what reads as a
pure fetch. A dead recorder halts the process with no auto-retry (CLAUDE.md), so it
is re-raised untouched before anything else is considered.

| what happened | how it travels | retried? |
|---|---|---|
| broker answered, comparison disagrees | `ReconciliationResult(ok=False)` — a value | **never** — not an exception |
| broker call failed, budget left | exception, inside `_ask_broker` | yes |
| broker call failed, budget spent | `BrokerUnreachable` | no — terminal |
| `AuditWriteError` from inside a port call | re-raised first | **never** |
| bug in the compare / ledger / audit | its own exception, outside every `try` | **never** — not in scope |

Two tests pin this and should be the first thing read if either is ever touched:
`test_a_real_mismatch_is_never_retried` (a mismatch costs exactly ONE broker call
however large the budget) and `test_a_bug_in_the_compare_is_not_retried`.

## What was built

**Two nested budgets.** The inner one is per broker await: 3 attempts,
`min(2·n, 5)` backoff — `_fetch_accounts`'s existing cadence, reused unchanged.
Per-await rather than shared across the two fetches, deliberately: they are two
questions to two endpoints behind one gateway, and a hanging positions endpoint
must not spend the orders endpoint's attempts and turn a one-sided outage into a
refusal to reconcile at all.

The outer one is `[reconcile] retry_interval_s` / `retry_budget_s` — 60 seconds,
for 30 minutes. **This is what makes an unreachable broker survivable rather than
terminal**, and it is the amendment made at sign-off: the first proposal parked on
the first exhausted inner budget, and Richard asked for a self-healing park.

**The park never enters the halted state while it is still trying, and that is the
design.** The literal reading — halt, then clear the halt on a timer — would have
required three changes to primitives that are deliberately one-way: adding
`KillSwitch.disengage` (a latch a restart currently clears, and nothing else),
making `_halted` clearable (it is first-writer-wins so a cascade cannot overwrite
the cause), and teaching `_await_exit_while_halted` to hand control back to the
scheduler — the first automated halted → trading transition in the system.

None of that was needed. The control server binds **before** the reconciliation
rail (2026-08-17), so during the outer loop the process is alive, bound, serving
`/status` and `/kill`, **not trading** (the scheduler has not started) and **not
halted** — so a success needs nothing undone. It simply proceeds. Both latches stay
one-way because the loop sits in front of them rather than inside them.

**Three ways out of the loop**, and the first is the interesting one:

- **Success** — return the verdict. A MISMATCH counts as success here: the broker
  answered, so there is nothing to retry, and the verdict goes straight to the
  caller to refuse on.
- **`stop` set** — `/kill` during the retry, or the session ending. "Stop trading"
  reads as "stop trying to start trading". The wait is a race against
  `asyncio.sleep` rather than a `wait_for` timeout, so a 60-second interval never
  sits on an operator's kill for up to a minute.
- **Budget spent** — raise, having written **one** Tier 1 `reconcile.unreachable`
  and fired **one** alert for the whole episode. Per-attempt would be ~30 records
  and an alert storm for a single condition — the 2026-08-14 shape (34 restarts,
  ~68 Slack messages) rebuilt inside one process.

**The two call sites then differ in HOW they refuse, exactly as they already did
for a mismatch:**

- **Process start PARKS** — `_halt_process`, `source="broker_unreachable"`. Same
  seam, same non-negotiable, one source string away from the mismatch park.
- **Per trading day ABORTS THE DAY** — `SessionAborted`, no process halt, in every
  environment.

**`SessionAborted` was split**, and the self-heal decision is what exposed the need
for it. It carried two unlike conditions out of `startup_reconcile`: retry
exhaustion on the account fetch (transient — an outer loop must retry it) and a
wrong account number (permanent — an outer loop would spend 30 minutes re-proving
it and then halt with a reason a minute of reading would have given). The
could-not-ask legs — the reconcile fetch, `_fetch_accounts` exhaustion, and
`tt.open()` — now all raise `BrokerUnreachable`, carrying `leg`; the wrong-account
check stays `SessionAborted`, is never retried, and parks immediately.

**The login leg gets the park but NOT the retry**, and that is not an oversight.
`tt.open()` forces an OAuth refresh, and each failed refresh accrues toward
tastytrade's undocumented IP-block threshold — which is why `_login_with_retry`
uses 6 attempts a flat **10 minutes** apart. Retrying a login on the reconcile's
60-second cadence would be five times more aggressive than a number chosen against
a ban threshold. So the session is opened once, and a login failure becomes
`BrokerUnreachable` immediately. The self-heal covers the fetch legs, which is
where 2026-08-26 actually failed.

**The reconcile fetches got their own per-request bound.** `[reconcile]
request_timeout_s = 10.0`, not the client-wide `broker.http_timeout_s = 30.0` —
which was sized on 2026-07-23 for a cold, first-ever authenticated call from a new
IP, an allowance already spent by the time the reconcile runs (at process start
these are the second and third authenticated calls of the process). 10s is twice
the tuned `staleness.account_refresh_timeout_s`, on the reasoning that a
once-per-session reconcile can be more patient than a 10s-cadence liveness refresh
and needs far less than a cold start. It only bites when the broker accepts a
socket and never answers; a 502 or a refused connection returns in milliseconds.

**`/status` no longer reports `idle` through it.** `app._reconcile()` never
published a phase — harmless while the reconcile was one round trip, a multi-minute
misreport once it can occupy the retry budget. It now sets `reconciling` and
restores `idle` on every path including the halting ones, because
`StatusSnapshot.halted` sits above the phase and changes what it means.

## `app.py:952`'s comment: premise preserved, consequence overturned

The task existed to settle this comment on purpose rather than route around it:

```python
# Build one. Any failure here — unreachable broker, missing credentials,
# no matching account — propagates out of `startup()` and tears the
# half-built process down: sandbox/live must not start without having
# ASKED the broker, and "could not ask" is not "nothing to report".
```

**The premise is preserved and strengthened.** Sandbox/live still must not resume
without having asked, and "could not ask" is still not "nothing to report" — under
the park it is reported *more*: a Tier 1 record, a Slack alert, an engaged kill
switch attributed to `broker_unreachable`, `/status` rendering HALTED with the
reason, and a journal WARNING every 15 minutes. Under the exit it was reported not
at all.

**The consequence is overturned, by a change that had already landed.** The comment
says the failure "tears the *half-built* process down". That was true when written,
and stopped being true on 2026-08-17, when the control bind and the `process.start`
record moved **ahead** of this call — with a comment of their own eleven lines above
saying *"by here the process genuinely HAS started."* Two comments in one function
had come to disagree about whether the process was built, and only the newer one was
accurate. The reconcile comment was not revisited when the ordering under it
changed: **the same expiry class as a comment naming the callers it is safe for**
([unmappable orders](2026-08-20-unmappable-orders-reach-reconciliation.md)).

The symmetry argument settles the rest: at this exact call site, "the broker
answered and disagreed" has parked since 2026-08-17 while "the broker could not be
asked" exited. Both are terminal refusals to resume; both leave the process unable
to trade; both need a human. Nothing justified making one observable and the other
not — only the accident that one was fixed and the other was never looked at.

**What a genuine bug still does is unchanged.** `except BaseException: teardown;
raise` stays, so a defect still exits non-zero and Docker still restarts it — which
[halted-but-alive](2026-08-17-halted-but-alive.md) says explicitly is what that
policy is for. One well-identified failure class moved off that path; nothing else.

## Why the per-day site does NOT halt, when a mismatch there does

A mismatch means **Argus's model of reality is known-wrong.** Permanent: waiting
does not improve it, tomorrow inherits the same wrong state, a restart cannot fix
it. Halting is right.

Unreachable means **unknown.** Transient by nature — that is the premise of the
retry — and tomorrow's session asks again from scratch. Halting on *unknown*
converts a one-day outage into stay-down-until-a-human, for a condition with a good
chance of being gone on its own.

The obvious objection is a real open 0DTE position going unwatched. **It applies
equally to both responses**: neither starts a watchdog (`trading` is `None` either
way), so the position is unmanaged under a halt just as under an abort, and only a
human resolves it. The two differ in exactly one respect that matters — whether the
operator is TOLD — and that is settled by the alert, not by the choice.

**Which is why the alert had to be added.** `session.aborted` fires no Slack alert
at all; it only writes an audit record. So "stop misfiling it as `session.error`,
raise `SessionAborted` instead" would, on its own, have *removed* the only
notification an unreachable broker produced — a regression against the thing this
task exists to improve. The record and the alert therefore live in
`reconcile_with_retry`, next to where the mismatch alert already lives, rather than
depending on either call site's terminal handler.

Aborting cannot leak into an entry: `_reconcile_or_abort` raises before
`session.configured`, before market data, and before the trading side is built.

## The environment gate: same function, second failure class

`mismatch_halts_process` is reused unchanged. Its own rationale carries over — the
split turns on *"shadow submits nothing, so there is no order state to protect"*,
which is a statement about the consequence of resuming **blind**, and resuming
blind is resuming blind whether the answer disagreed or never came.

At process start this is nearly a no-op: shadow never builds a real reconciler
there. It bites only for an injected one (tests, tools), where an unreachable
broker is now lenient exactly as an injected shadow mismatch is. **What it does not
do is change what the function means for a MISMATCH**, which was out of scope.

## Also fixed: `session.error` recorded nothing about the error

Diagnosing 2026-08-26 needed a live `journalctl` dig specifically because the one
place that writes a session failure down wrote only `{"trading_date": ...}`. The
payload now carries `error_type` and a 500-char-truncated `error` on **both** the
alert and the audit record, matching `account.refresh_failed`'s shape. The
handler's "reaching here means a genuine bug" claim is gone with it.

This still matters after the rest of this change: the fix removes the
broker-unreachable case from that handler, so whatever reaches it now is by
definition something nobody predicted — which is exactly when the payload is worth
the most.

## The resource check

Repeated partial startups were checked for accumulating cost. Two are real, and
**this change reduces both** rather than extending them:

1. **An OAuth token refresh per restart.** `tt.open()` forces
   `session.refresh(force=True)`, so five restarts in 33 seconds were five
   `/oauth/token` calls accruing against the IP-block threshold. A retry inside one
   process reuses the open session: one token, N fetch attempts. This is the
   strongest resource argument for the change, and the reason the login leg gets no
   retry of its own.
2. **A full-table `GROUP BY` on `audit_log` per restart.** `AuditLog.__init__` seeds
   its tally with a whole-table scan — the same scan whose *recurrence* was judged
   unacceptable on 2026-08-05 and moved to startup because startup pays it once. A
   restart loop turns "once" back into "per iteration", at a cost that grows with
   the log forever. **Measured at the incident's own size** (472,443 rows, 166 MB):
   **~20 ms**, via `SCAN audit_log USING COVERING INDEX idx_audit_log_category`.
   Genuinely accumulating and genuinely linear, but ~100 ms across five restarts —
   recorded so the next investigation does not re-measure it, not because it was the
   hazard.

Not accumulating: the writer `flock` releases cleanly per cycle, `apply_migrations`
is idempotent, and `_safe_aclose` closes the httpx pool on every path. Not a
resource but worth knowing: each partial startup wrote `process.start` with no
`process.stop`, so anything pairing those categories read a restart loop as
processes that never ended.

## The actual residual, after two false starts describing it

Two claims made in conversation about the park's cost, after this shipped, were
both wrong, and correcting them narrows the residual to something much smaller
than either implied:

- **"An outage longer than 30 minutes at process start costs the entire day and
  the next"** — wrong. `KillSwitch` is constructed bare (no `initially_engaged`)
  and `risk/rehydrate.py` reads only `daily_loss.breach`; a `kill_switch.trip` with
  `source="broker_unreachable"` is never rehydrated. `_halted` is in-memory only.
  **A restart clears the park completely** — nothing to reset, nothing to
  reconcile against a stale trip. And the scheduler resumes the *current* trading
  date if the restart lands inside today's window (`scheduler.py`'s
  `now < window[1]`), not tomorrow's.
- **"A park at 03:00 on a Saturday could sit unnoticed until Monday"** — also
  wrong, for a reason that should have been obvious from the file above this one:
  `MarketCalendar.session_window` returns `None` on a weekend, so a Saturday park
  costs *nothing extra* — there is no session to miss until Monday's open. The
  scenario also needs a process START at 03:00 Saturday to trigger at all
  (crash, droplet reboot, or a manual deploy), not the routine case.

**What is actually true, once both are corrected:** the park trades an
*automatic, invisible* recovery (the old restart loop, unbounded, no record, the
control surface down every cycle) for a *manual, announced* one (a Slack alert on
entry, `/status` reads HALTED with the reason, a journal WARNING every 15
minutes). The residual cost is not lost trading time — it is however long it takes
a human to see the alert and run one restart, which the alert exists to
minimize. That is the 2026-08-17 bargain applied to a second failure class, not a
new tradeoff this task introduced.

The one scenario that survives correction: a park that fires and is not noticed
before the *next scheduled session open* — a notification-reliability question,
not a self-healing one, and it applies equally to every other halt path already in
the system (a mismatch has parked the exact same way since 2026-08-17).

## Left open, deliberately

- **N consecutive unreachable *days* at the per-day site.** The 30-minute intraday
  loop makes one day survivable, not five. No evidence for what N should be, and
  inventing one is how a rail gets a number nobody can defend.
- **`session.aborted` still fires no alert.** Routed around rather than changed:
  every other abort reason (missing halt seam, no quote within 30s, no matching
  account) would start alerting too, and none has been reviewed for it.
- **`KillSwitch` still has no `disengage`, and `_halted` is still terminal.** The
  self-heal was built to avoid needing either. A future task that genuinely needs a
  clearable halt is its own design, and it touches a non-negotiable.
- **The entry guard's identical shape stays a separate task**
  ([prompt](../tasks/argus_entry_guard_unreachable_retry_prompt.md)), and there is a
  concrete reason beyond scope: the placement that would unify all three sites —
  retrying inside the execution adapter — is **forbidden**.
  `Reconciler.reconcile()` and `DuplicateOrderGuard.has_conflicting_order` call the
  *same* port method (`execution.get_live_orders()`), so a retry there would push a
  ~72-second budget into an await on the 5-second signal cadence, which
  [the book-confirm timeout rule](2026-08-21-book-confirm-timeout-bound.md)
  prohibits. Three locally-reasoned fixes, one shared vocabulary:
  `BrokerUnreachable` is importable and carries `.leg`/`.attempts`.
