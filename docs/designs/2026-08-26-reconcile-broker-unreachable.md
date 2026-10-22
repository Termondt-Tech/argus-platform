# Design — a broker-unreachable reconcile parks; it does not kill the process

**Status: BUILT 2026-08-26 as approved.** The normative record is
[`docs/decisions/2026-08-26-reconcile-broker-unreachable.md`](../decisions/2026-08-26-reconcile-broker-unreachable.md).
This file stays as the record of what was *rejected* and why. Two
implementation-level findings not in this document, both recorded in the decision:
`AuditedExecution.get_live_orders` writes a Tier 1 record INSIDE the port call, so
`AuditWriteError` is a required exclusion from the retry (§3.2 claimed no exception
type would be load-bearing — one is, and it is an exclusion); and the login leg gets
the park but NOT the outer retry, because retrying an OAuth refresh at 60s is five
times more aggressive than `_login_with_retry`'s deliberate 10-minute ban-aware
cadence — so the self-heal covers the fetch legs only, which is where 2026-08-26
actually failed.

---

**Status when written: APPROVED 2026-08-26 (Richard), with three amendments made at
sign-off — D2, D3 and D6 below.** Target v0.3.11. Task prompt:
[`docs/tasks/argus_reconcile_broker_unreachable_prompt.md`](../tasks/argus_reconcile_broker_unreachable_prompt.md).

**Working arrangement, decided at sign-off:** work on `main`, **do not commit, do
not push** — Richard reviews the working tree. (Same as the v0.3.10 sign-off; still
not a standing convention, so it is asked each time.)

The three amendments, each of which changed the shape rather than a number:

- **D3 — the park is now the TERMINAL state of an outer retry loop, not the first
  response.** Richard chose a self-healing park. The literal form of that (halt,
  then clear the halt) would require making `KillSwitch` disengageable and
  `_halted` clearable; the approved form never enters the halted state while it is
  still trying, so nothing needs un-halting. §4.6.
- **D2 — the reconcile fetches get their own per-request bound**, a new config key
  rather than the 30s client-wide cold-start default. §2.2.
- **D6 — split.** "Could not ask" and "asked, and the configuration is wrong" were
  travelling as one exception type; a self-heal loop would spin forever on the
  second. §4.4.

Issue 3 (`session.error`'s empty payload) is **already built** — it carries no
design gate. It is described in §10 for completeness and was not part of the
sign-off.

> Read [a halted process parks; it does not exit](../decisions/2026-08-17-halted-but-alive.md)
> first. This proposal does not modify that fix; it argues that one call site was
> left outside it, and that the comment which kept it outside was made obsolete by
> that very change and never revisited.

---

## 0. What this changes, in one paragraph

A reconcile that **could not ask the broker** currently ends the process at
process start (raw exception → `except BaseException` → teardown → exit → Docker
restart → repeat) and is misfiled as a bug at the per-day site (raw exception →
`session.error`, under a comment saying "reaching here means a genuine bug").
This proposal adds one bounded retry at the **port boundary** — three attempts,
`min(2.0 × attempt, 5.0)` backoff, the existing `_fetch_accounts` numbers — so a
single 502 or timeout never surfaces at all; and on exhaustion converts the
process-start response from **exit** to **park** (the existing `_halt_process`
seam, `source="broker_unreachable"`) and the per-day response from
**`session.error`** to a clean, alerted `SessionAborted`. The
"never retry past a real mismatch" invariant is held **structurally, not by
discipline**: a mismatch is a return VALUE and the retry only ever wraps calls
that return broker data.

---

## 1. Verification of the prompt's reading of the code

Everything the prompt asserts about the current code was checked against the
source. Four confirmations, and **three things it did not catch** — two of which
change the shape of the fix.

### 1.1 The two call sites and their failure paths — CONFIRMED

| | process start | per trading day |
|---|---|---|
| entry point | `app.Application._reconcile` ([app.py:948](../../src/argus/app.py)) | `session._reconcile_or_abort` ([session.py:362](../../src/argus/session.py)) |
| opens its own broker session? | yes, short-lived (`startup_reconcile`) | no, reuses the day's |
| mismatch response | `ReconciliationError` → **parks** (2026-08-17) | `halt(...)` + `SessionAborted` |
| **unreachable response today** | **raw exception → teardown → process exit** | **raw exception → `session.error`** |

Both go through `reconcile_and_record` → `Reconciler.reconcile()`, which is where
the unhandled fetch lives.

### 1.2 `startup_reconcile`'s docstring is wrong — CONFIRMED, and wrong in a second way

The prompt notes the docstring's *"Raises `SessionAborted` if the broker cannot be
reached"* is false for the reconcile-comparison step. Correct. But the fix it
implies — "make the reconcile step raise `SessionAborted` too" — would not have
saved the process either, because **`SessionAborted` out of `startup()` also
exits today.** It is not a `ReconciliationError`, so it lands on the same
`except BaseException: await self._teardown(); raise`.

So the account-fetch leg, which already has the retry the prompt holds up as the
precedent and already raises the domain exception, **still kills the process on
exhaustion.** The docstring describes intended behaviour that was never wired to a
consequence. Any fix that only handles the new exception type leaves this leg
broken.

### 1.3 A third unretried, unhandled broker call at process start — NOT IN THE PROMPT

`startup_reconcile` opens its own session:

```python
# session.py:474
tt = TastytradeSession(config, trading_creds, role=SessionRole.TRADING)
await tt.open()          # forces an OAuth token refresh — no retry, no wrapping
```

`open()` forces `session.refresh(force=True)`, so a cert outage on
`/oauth/token` at process start raises a raw SDK/httpx error before the reconcile
is even attempted, and exits exactly as the 502 did. The per-day path has
`_login_with_retry` for this; the process-start path has nothing.

**This one must NOT get the same retry**, and that is not an omission —
`_login_with_retry`'s docstring makes ban risk the binding constraint: *"each
failed OAuth refresh accrues toward tastytrade's undocumented IP-block
threshold."* Three tight retries against a sick token endpoint is precisely the
shape that costs a day-long IP block. The right treatment for this leg is the
**park**, with no retry at all (§4.4).

### 1.4 `session.aborted` does not alert — NOT IN THE PROMPT, and it inverts Issue 2

`run_session`'s handler:

```python
# session.py:1186
except SessionAborted as exc:
    print(f"session aborted: {exc}")
    await audit.record("session.aborted", {"reason": str(exc)})
```

No `alert(...)`. Meanwhile `session.error` — the path the prompt is moving *away*
from — **does** fire `self._alert("session_error", ...)`.

So the obvious form of the Issue 2 fix ("stop misfiling it as `session.error`,
raise `SessionAborted` instead") would, on its own, **remove the only Slack
notification an unreachable broker currently produces.** Richard learned about
2026-08-26 partly *because* `session_error` alerted. A fix whose net effect is
"the day silently does not happen" is a regression against the thing this task
exists to improve.

The design therefore puts the alert in the shared function (§3.3), where the
mismatch alert already lives, rather than relying on either call site's own
terminal handler.

### 1.5 The retry cannot live in the execution port — NOT IN THE PROMPT, and it is decisive

The prompt's "Related" section asks whether all three sites (process-start
reconcile, per-day reconcile, entry guard) should share one bounded-retry
primitive. There is a concrete reason one specific placement is forbidden, and it
is worth stating before any of the design questions:

`Reconciler.reconcile()` and `DuplicateOrderGuard.has_conflicting_order` call the
**same port method** — `execution.get_live_orders()`
([reconcile.py:847](../../src/argus/reconcile.py),
[guard.py:156](../../src/argus/execution/guard.py)). Putting the retry inside the
execution adapter would give both sites the retry "for free" and would silently
hand a **~96-second worst-case budget to a call awaited inline in the entry
pipeline, on a 5-second signal cadence** — the exact defect
[the book-confirm timeout finding](../decisions/2026-08-21-book-confirm-timeout-bound.md)
established a rule against ("an await inside `check_once` takes its bound from
the thing being awaited, never from a loop cadence in scope").

So the retry goes at the **reconcile** boundary, not the port adapter. §9 answers
the shared-primitive question in full.

---

## 2. Design question 1 — should an unreachable broker be retried?

**Yes, at both sites, and this is the least controversial part of the proposal.**

The observed incident is the argument. Cert returned a 502 — which comes back
*immediately*, not at the 30s timeout — and recovered on the fifth process start,
~33 seconds later. Three attempts with 2s and 4s backoff would have covered that
episode inside a single process, with a single OAuth token, and **nothing would
have surfaced at all**: no halt, no alert, no restart, one clean
`reconcile.result`.

### 2.1 The numbers: reuse `_fetch_accounts`'s, unchanged

3 attempts, `min(2.0 × attempt, 5.0)` backoff → sleeps of 2s then 4s. The
constraint in the prompt says reuse unless there is a specific reason not to;
there is not one. Same broker, same failure class, same "transient cert" premise,
and reusing them means the process-start path has ONE retry cadence across its
account fetch and its reconcile rather than two numbers to keep in step.

### 2.2 The worst case, and the per-request bound — AMENDED AT SIGN-OFF (D2)

The budget is **per broker await**, and `reconcile()` makes two
(`fetch_positions`, `get_live_orders`). Only one failure shape makes the
per-request timeout matter at all:

| failure shape | one attempt | 3 attempts |
|---|---|---|
| 502 from nginx (observed at process start) | ~ms | **~6s** — just the 2s + 4s sleeps |
| connection refused / DNS | ~ms | ~6s |
| **socket accepted, no response** (`httpcore2.ReadTimeout`, observed at the per-day site) | the full timeout | dominated by it |

Both shapes were observed on 2026-08-26, so the hang is not hypothetical here.

**Approved: the reconcile fetches get their own per-request bound rather than the
client-wide `broker.http_timeout_s = 30.0`.** That 30s was sized on 2026-07-23 for
a specific event — the droplet's first-ever cold `GET /customers/me/accounts` from
a new IP dying at httpx's silent 5s default — and the config's own comment
anticipates this case: *"This is the client-wide DEFAULT. Callers needing a
tighter bound still set their own."* By the time the reconcile fetches run, that
cold-start allowance is largely spent: at process start they are the second and
third authenticated calls of the process, after `tt.open()`'s token refresh and
`_fetch_accounts`.

The number is **10.0s**, anchored rather than invented: twice the already-tuned
`staleness.account_refresh_timeout_s = 5.0`, on the reasoning that a
once-per-session reconcile can afford more patience than a 10s-cadence liveness
refresh but does not need the cold-start allowance.

```
per await:  3 × 10s (hang) + 2s + 4s  =  36s
whole reconcile, both awaits hanging  =  72s
```

**Per-await budgets rather than one shared deadline**, deliberately: the two awaits
are independent questions to different backends behind one nginx, and a positions
endpoint that hangs should not spend the orders endpoint's budget and turn a
one-sided outage into a giving-up. The cost is that the worst case doubles; at 10s
per request that is 72s rather than the 192s the 30s default would have produced.

**This is the inner budget.** It is now wrapped by the outer retry loop of §4.6, so
72s is what one *attempt at reconciling* costs, not what the process gives up
after.

### 2.3 Config — a new `[reconcile]` section, three REQUIRED keys

```toml
[reconcile]
request_timeout_s = 10.0   # per-request bound for the reconcile fetches (§2.2)
retry_interval_s  = 60.0   # outer loop cadence (§4.6)
retry_budget_s    = 1800.0 # outer loop total, 30 min, then a terminal halt
```

Required, not defaulted — the fail-closed loader's existing discipline, and the
house rule that every timing value is a deliberate auditable number rather than a
literal. Inner `attempts = 3` stays a code default, matching `_fetch_accounts`'s
own `attempts: int = 3`.

**This means the config deploys WITH the image** — the same coupled deploy
`watchdog.exit_unattempted_max_s` already needs. It must be called out in the
release notes.

### 2.4 Visibility during the retry

Each failed attempt writes Tier 2 telemetry (`reconcile.unreachable_attempt`:
`leg`, `attempt`, `attempts`, `error_type`, `error`), matching
`session.login_attempt_failed`. A silent multi-minute gap at startup is its own
small version of this task's complaint — and §4.6's outer loop makes the window
longer still, so this is not optional decoration.

---

## 3. Design question 2 — where the retry goes, and how the invariant is held

> **The one invariant that must not move:** never let a retry blur "the broker is
> unreachable" into "the broker answered and disagrees."

### 3.1 The structural answer: a mismatch is a VALUE, not an exception

This is the fact the whole design rests on, and it is already true of the code
today:

```python
# reconcile.py:834 — Reconciler.reconcile()
positions = await self._broker.fetch_positions()      # can fail: could not ask
...
result = reconcile_positions(expected_map, broker_map) # cannot fail: pure compare
return ReconciliationResult(ok=False, ...)             # a mismatch RETURNS
```

`ReconciliationError` is never raised here. It is constructed by the *caller*
(`app._apply_reconcile_verdict`) after the value comes back. So a retry loop
wrapping a call that returns broker data is **structurally incapable** of
retrying past a mismatch: a mismatch does not raise, so it exits the loop on the
first iteration and reaches the same refusal it reaches today, having consumed
zero attempts.

That guarantee holds regardless of which exception types the retry catches. It is
not a rule someone has to remember.

### 3.2 The second guarantee: a bug is not retried either

A weaker but still important property: the retry must not swallow a genuine
defect (a `KeyError` in the compare, a sqlite error from `open_positions()`, an
`AuditWriteError`). Catching by *exception type* is a losing game here — the
2026-08-26 traceback shows a raw `httpcore2.ReadTimeout` escaping unwrapped
rather than the `httpx2` type a list would name, and
[`account/port.py`](../../src/argus/account/port.py) already documents why
third-party class names are the wrong thing to build a rail on (*"every query
written against it stops matching the day the SDK or httpx renames one"*).

So the boundary is drawn by **position in the call graph, not by exception
type**: the retry wraps *exactly one broker await at a time and nothing else*.
Everything that can only fail because of a bug — the compare, the ledger read,
the audit write — sits outside every `try`, so no exception type list is
load-bearing.

### 3.3 The shape

One new exception and one small helper in `reconcile.py`:

```python
class BrokerUnreachable(RuntimeError):
    """The broker could not be ASKED. Structurally distinct from a mismatch.

    A mismatch is a ReconciliationResult with ok=False — a VALUE, returned by a
    broker that answered. This is raised only when a broker call itself failed,
    after its bounded retry was exhausted, and it is raised from a wrapper that
    contains no comparison logic at all. The two can never be confused because
    they do not travel by the same mechanism.
    """
    def __init__(self, leg: str, attempts: int, last: BaseException) -> None: ...
    # .leg ("positions" | "orders"), .attempts, .__cause__ = last


async def _ask_broker(leg, fn, *, attempts, on_attempt_failed):
    """Await ONE broker call, retrying transient failures. Contains no comparison."""
```

`Reconciler.reconcile()` routes its two awaits through it, and gains an optional
`on_attempt_failed` callback (default: none) so the telemetry is written by the
caller that owns the audit log — `Reconciler` stays a module that "does not write
audit itself", as its docstring promises.

`reconcile_and_record` — the function that exists so the two call sites cannot
drift — catches `BrokerUnreachable`, records **Tier 1** `reconcile.unreachable`
and fires `alert("reconcile_unreachable", ...)`, then re-raises:

```python
    if order_store is not None:
        await _clear_ledger_if_reset(...)     # OUTSIDE the retry — see below
    try:
        result = await reconciler.reconcile()
    except BrokerUnreachable as exc:
        await audit.record("reconcile.unreachable", {...})   # Tier 1
        alert("reconcile_unreachable", {...})
        raise
```

This gives the unreachable case exactly the record-and-alert symmetry the
mismatch already has, in the same function, for the same reason — and it is what
closes §1.4's silent-abort regression.

**`_clear_ledger_if_reset` stays outside the retry, and that is load-bearing.**
It writes Tier 1 `reconcile.sandbox_reset` and then *deletes position rows*. It
must be evaluated exactly once per reconcile; a retry that re-ran it could record
two resets, and its "record before delete" crash-ordering is written on the
assumption of a single pass. Same for `_resolve_expired_positions` and the
`reconcile.result` write, both of which are after the compare.

### 3.4 What this means at the type level, stated plainly

| what happened | how it travels | retried? |
|---|---|---|
| broker answered, comparison disagrees | `ReconciliationResult(ok=False)` — a value | **never** — it is not an exception |
| broker call failed, retries left | exception, inside `_ask_broker` | yes |
| broker call failed, retries exhausted | `BrokerUnreachable` | no — terminal |
| bug in the compare / ledger / audit | its own exception, outside every `try` | **never** — not inside the retry's scope |

---

## 4. Design question 3 — park or exit, at process start

**Recommendation: PARK,** via the existing `_halt_process` seam,
`source="broker_unreachable"`.

### 4.1 The existing comment, addressed head-on

```python
# app.py:952
if mismatch_halts_process(self._config.environment):
    # Build one. Any failure here — unreachable broker, missing credentials,
    # no matching account — propagates out of `startup()` and tears the
    # half-built process down: sandbox/live must not start without having
    # ASKED the broker, and "could not ask" is not "nothing to report".
```

**Its premise survives untouched, and this proposal strengthens it.** Sandbox/live
still must not resume without having asked the broker, and "could not ask" is
still not "nothing to report" — under the park it is reported *more*: a Tier 1
`reconcile.unreachable` record, a Slack alert, an engaged kill switch attributed
to `broker_unreachable`, `/status` rendering HALTED with the reason, and a
journal WARNING every 15 minutes. Under the exit it was reported *not at all* —
no `process.stop`, no halt record, no `/status` to read one from.

**Its consequence is what is overturned, and it was overturned by a change that
had already landed.** The comment says the failure "tears the *half-built*
process down". That was literally true when it was written, because `_reconcile()`
then ran **before** the control bind. On 2026-08-17 the bind and the
`process.start` record moved ahead of it, with a comment of their own eleven
lines above saying so:

```python
# app.py:481
# Tier 1: the process starting is a crash-relevant boundary in the trail.
# Written BEFORE reconciliation, because by here the process genuinely HAS
# started — it is serving /status and /kill.
```

Two comments in one function now disagree about whether the process is built. The
second one is the accurate one. The reconcile comment was not revisited when the
ordering under it changed — the same oversight class as
[the expired comment naming its safe callers](../decisions/2026-08-20-unmappable-orders-reach-reconciliation.md).

### 4.2 The symmetry argument

At **this exact call site**, "the broker answered and disagreed" already parks
(2026-08-17). "The broker could not be asked" exits. Both are terminal refusals to
resume; both leave the process unable to trade; both need a human. There is no
reasoning anywhere in the repo for why one should be observable and the other
should not — only the chronological accident that one was fixed and the other was
not looked at.

### 4.3 The strongest argument AGAINST parking, and why it does not win

**A park is not self-healing, and cert 502s are transient.** The restart loop,
ugly as it was, *did* recover — on the fifth attempt, ~33 seconds in. A park would
have stayed parked until Richard restarted it, on a day when the broker came back
almost immediately. This is a real cost and it should be weighed, not waved past.

Three things answer it:

1. **The retry is what covers the transient case, and it covers this one.** Three
   attempts against a fast-failing 502 span the observed episode inside one
   process. The park is reached only by a failure the retry did *not* cover —
   i.e. one that has already outlasted the window a blind restart would have
   needed anyway.
2. **A restart loop is not a recovery strategy; it is an unbounded retry with no
   diagnosis.** It has no cap (`unless-stopped`; systemd's `StartLimitBurst`
   guards the compose process, not the container —
   [halted-but-alive](../decisions/2026-08-17-halted-but-alive.md)), it produces
   no record of why, and it drops the control surface on every cycle. Its
   "recovery" is indistinguishable from the failure until it happens to work.
3. **A parked process is still the better place to be wrong from.** If cert comes
   back two minutes later, the park costs a restart the operator performs
   knowingly, from a page that says exactly what happened. The exit costs an
   invisible loop that might resolve itself and might run all weekend.

**Should the park self-heal — retry the reconcile on a timer while parked?**
Deliberately not, and not in this task. That would be the first automated
halted → trading transition in the system, and every halt in the repo is
"restart once the cause is resolved". Filed as an open question (§12), not built.

### 4.4 What is retried, what parks, and what still exits — AMENDED AT SIGN-OFF (D6)

Per §1.2 and §1.3, handling only `BrokerUnreachable` fixes one third of the
process-start problem. The original proposal added `SessionAborted` alongside it.
**That was wrong, and the self-heal decision is what exposed it:** `SessionAborted`
carries two unlike conditions.

| condition | raised by | meaning | treatment |
|---|---|---|---|
| reconcile fetch exhausted | `_ask_broker` | could not ask | `BrokerUnreachable` → **outer retry**, then park |
| account fetch exhausted | `_fetch_accounts` | could not ask | **re-raised as `BrokerUnreachable`** → outer retry, then park |
| `tt.open()` / OAuth failure | the SDK | could not ask | **wrapped as `BrokerUnreachable`, with NO inner retry** (ban risk, §1.3) → outer retry, then park |
| **no matching account** | `startup_reconcile` | asked; the CONFIG is wrong | `SessionAborted` → **terminal park immediately, never retried** |

The last row is the amendment. A wrong account number in `argus.toml` is not
transient, and an outer loop that retried it would spend 30 minutes re-proving a
permanent fact and then halt with a reason a minute of reading would have given.
Same exception type, opposite correct response — so they must stop sharing one.

**Everything else keeps `except BaseException: await self._teardown(); raise`,
unchanged.** A genuine bug must still exit non-zero so Docker restarts it —
[halted-but-alive](../decisions/2026-08-17-halted-but-alive.md) says so
explicitly (*"a process that dies from a genuine bug still exits non-zero and
still gets restarted, which is what that policy is for. Only deliberate halts
stop looping"*). This change moves one specific, well-identified failure class
from that path, and nothing else.

### 4.5 The halted-but-alive non-negotiable is satisfied by construction

The constraint requires the surface be bound before any rail that can refuse, and
that a halt never become an exit. Both hold **without reordering anything**: the
bind is already ahead of `_reconcile()` (2026-08-17), and this change only
replaces a `raise` with a call to the seam every other halt already uses. It adds
no new ordering requirement. `run()`'s "startup-halted" branch already exists and
already handles a `startup()` that returns with `_halted` latched.

### 4.6 The self-heal loop — AMENDED AT SIGN-OFF (D3)

§4.3 argued the park should not self-heal, and listed it as an open question.
**Richard chose the self-healing version.** What follows is the shape that
delivers it, and why it is not the shape §4.3 was worried about.

**What the literal version would have cost.** "Halt, then clear the halt on a
timer" requires three changes to primitives this system deliberately made
one-way:

- `KillSwitch` has **no `disengage`** (`risk/killswitch.py`) — engaging is a latch,
  and a restart is the only thing that clears it. Adding one makes the halt control
  clearable by something other than an operator.
- `_halted` is **first-writer-wins and never cleared**, precisely so a cascade
  cannot overwrite the cause.
- `_await_exit_while_halted` would have to be able to hand control back to the
  scheduler — the first automated halted → trading transition in the system.

**The approved shape avoids all three by never entering the halted state while it
is still trying.** The control server binds *before* the reconcile (2026-08-17), so
during the outer loop the process is:

- **alive and bound** — `/status` and `/kill` answer throughout;
- **not trading** — the scheduler has not started; `startup()` has not returned;
- **not halted** — `_halted` is `None`, the kill switch is disengaged, so a success
  needs nothing undone. It simply proceeds.

```
for each outer attempt, every `reconcile.retry_interval_s`, until `retry_budget_s`:
    try the full reconcile (which contains §2.2's inner 3-attempt retry)
      -> succeeded          : status back to IDLE, startup proceeds, day runs
      -> BrokerUnreachable  : Tier 2 telemetry, wait the interval, try again
      -> SessionAborted     : terminal park NOW (config error, §4.4)
      -> _stop set          : operator override -> terminal park
budget exhausted             : terminal park (`_halt_process`, source=broker_unreachable)
```

Three properties worth naming because they are what make it safe:

1. **The kill switch is an operator override, and it works during the loop.** A
   `POST /kill` mid-retry engages the switch; the loop sees `_stop` and stops
   trying, then parks. "Kill" reading as "stop trying to trade" is the correct
   meaning here, and it costs nothing to honour because the loop is checking an
   event it already has.
2. **The terminal halt is unchanged.** When the budget runs out, this is exactly
   §4's park — `_halt_process`, `source="broker_unreachable"`, a Tier 1 record, a
   Slack alert, and a restart is what clears it. Nothing about the halted state
   became softer; a *different, earlier* state was added in front of it.
3. **`/status` must not lie during it.** `app._reconcile()` today never touches the
   status publisher, so a process-start reconcile reads phase `idle`. Under a
   30-minute loop that is a multi-minute misreport of a process blocked on the
   broker. `set_reconciling()` around the process-start reconcile, back to `IDLE`
   after — one line, and it is load-bearing now in a way it was not before.

**Budget: every 60s for 30 minutes.** Long enough to cover a cert outage of
realistic length (2026-08-26's was ~33 seconds) and short enough that a
permanently-broken deployment still reaches a loud terminal state the same
morning rather than retrying into the weekend.

---

## 5. Design question 4 — the environment gate at process start

**Recommendation: route it through `mismatch_halts_process`, unchanged, and it is
almost a no-op at this site.**

Shadow does not reach the real-reconcile branch at all — `_reconcile()` builds a
reconciler only when `mismatch_halts_process(env)` is true, and otherwise records
`reconcile.skipped`. So the unreachable case at process start is **already
sandbox/live-only by construction.** The gate matters only for an **injected**
reconciler (tests, tools) in shadow, which goes down the `reconcile_and_record`
branch above it. There, a `BrokerUnreachable` should be lenient — logged and
proceeded past — exactly as an injected shadow *mismatch* is today.

**Is the same boundary correct for a different failure class?** Yes, and the
docstring's own rationale is why: the split turns on *"shadow submits nothing, so
there is no order state to protect"*. That is a statement about the consequence of
resuming blind, and resuming blind is resuming blind whether the answer disagreed
or never came. Nothing in the reasoning depends on which of the two it was.

This **extends what feeds into** `mismatch_halts_process` and does not touch what
it means for a mismatch — within the prompt's constraint. The one thing the design
must say out loud, and does: the two failure classes differ in **permanence**
(§6.2), and that difference is absorbed by the retry, not by the environment gate.

---

## 6. Design question 5 — the per-day site's response on exhaustion

**Recommendation: abort the day, alert loudly, and do NOT halt the process — in
every environment, including sandbox/live.** This is the one place where this
proposal deliberately treats unreachable *differently* from a mismatch, so the
argument is made in full.

### 6.1 The shape — with the same outer retry (approved at sign-off)

The outer loop of §4.6 applies here too, and for the same reason it was wanted at
process start: the reconcile runs at session build, well before market data and
the trading side, so a failure at 09:40 that clears at 09:50 would otherwise cost
the entire 10:00–15:00 entry window. Retrying for up to 30 minutes still leaves
most of it.

```python
    try:
        result = await _reconcile_with_retry(...)  # same outer loop, same keys
    except BrokerUnreachable as exc:
        raise SessionAborted(
            f"could not reach the broker to reconcile ({exc}); no session today"
        ) from exc
    finally:
        store_conn.close()
```

Where the two sites differ is only in what the exhausted loop *does*: process
start parks (§4.6), the per-day site aborts the day. The loop itself is one
function, taking the same two config keys, so the cadence cannot drift.

`run_session` already catches `SessionAborted` and records `session.aborted`. The
Slack alert comes from `reconcile_and_record` (§3.3), which is what keeps §1.4's
silence from being the net result.

The loop is `stop`-aware at this site too — the session's own stop event, so an
operator `POST /kill` or the ET close ends the retry rather than having it run on
past the point a session could still be useful.

### 6.2 Why not halt, when a mismatch at this same site does

A mismatch means **Argus's model of reality is known-wrong.** That is permanent:
waiting does not improve it, tomorrow's session inherits the same wrong state, and
a restart cannot fix it. Halting is right — 2026-08-17 makes the same point about
why the restart loop was pointless.

Unreachable means **unknown.** It is transient by nature (that is the premise of
the whole retry), tomorrow's session re-asks from scratch, and cert being sick at
09:40 says nothing about cert at 09:40 tomorrow. Halting on *unknown* converts a
one-day outage into a stay-down-until-a-human-restarts outage, for a condition
with a good chance of being gone on its own.

### 6.3 The counter-argument, and why it does not change the answer

*"But there may be a real open position, and aborting the day means the watchdog
never starts."* True — and **it is equally true of the halt.** Neither response
starts a watchdog; `trading` is `None` in both. The position is unmanaged either
way, and only a human resolves it. The two responses therefore differ in exactly
one respect that matters — whether the operator is *told* — and that is settled by
the alert in §3.3, not by which of the two is chosen.

*"Does aborting leave a path to entering?"* No. `_reconcile_or_abort` raises before
`session.configured`, before the market-data streams, and before the trading side
is built. The session returns; `_run_session_day` returns; the scheduler idles to
the next trading day, which reconciles again from the top. Aborting is safe with
respect to entries by construction.

### 6.4 Consistency with the "no drift" discipline

The two call sites end up with **different** responses to the same failure class:
park at process start, abort per day. That is not drift, and the reason is already
written into the shared function's docstring: *"Both refusal sites call this and
differ only in HOW they refuse."* What is single-sourced is the verdict, the
record and the alert — all three now cover unreachable too. The refusal mechanics
have always differed, because the sites' *next best option* differs: at process
start there is no day to abort (returning normally would mean starting up having
never asked the broker, which §4.1's premise forbids), so the choice is
park-vs-exit; per day the process is already up and serving, so the choice is
abort-vs-halt. Each site picks the least-destructive refusal available to it.

---

## 7. The resource-cost check the prompt asked for

*"Confirm there's no accumulating resource cost to a longer unreachable-broker
window than today's — a check, not an expected finding."*

Checked. **Two real per-restart costs, both of which this change reduces rather
than extends**, and one that is larger than expected but not a hazard.

1. **A forced OAuth token refresh per restart.** `tt.open()` calls
   `session.refresh(force=True)` ([broker/session.py:196](../../src/argus/broker/session.py)).
   Today's five restarts in 33 seconds were **five `/oauth/token` calls**, each
   accruing against the undocumented IP-block threshold `_login_with_retry` is
   built around. A retry *inside* one process reuses the already-open session:
   one token, N fetch attempts. **This is the strongest resource argument for the
   change**, and it is why §4.4 gives the login leg the park and not a retry —
   the fix must not trade a restart loop for a token-refresh loop.

2. **A full-table `GROUP BY` on `audit_log` per restart.** `AuditLog.__init__`
   seeds its per-category tally with a whole-table scan
   ([audit/log.py:291](../../src/argus/audit/log.py)) — the same scan whose
   *recurrence* was judged unacceptable on 2026-08-05 and moved to startup on the
   grounds that startup pays it once. A restart loop turns "once" back into "per
   iteration", at a cost that grows with the log forever. Measured on a synthetic
   `audit_log` built to the incident's own size (472,443 rows, 166 MB):
   **~20 ms**, via `SCAN audit_log USING COVERING INDEX idx_audit_log_category`.
   So it is genuinely accumulating and genuinely linear, but at ~100 ms across
   five restarts it is not the hazard — worth recording because the next
   investigation should not have to re-measure it.

3. **Not accumulating:** the writer `flock` is acquired and released cleanly per
   cycle (`_teardown` releases after closing the connection, so no lock is
   orphaned — the existing test at
   [test_app.py:102](../../tests/test_app.py) pins that a second attempt is not
   blocked); `apply_migrations` is idempotent; `_safe_aclose` closes the httpx
   pool in `startup_reconcile`'s `finally` on every path.

4. **A cost that is not a resource:** each partial startup writes `process.start`
   with no `process.stop` — seven orphan starts across the incident. Anything
   pairing those two categories (an uptime or restart-count query) reads a
   restart loop as a set of processes that never ended. Parking removes the
   source.

Longer unreachable windows are therefore **cheaper** under this design than under
today's, not more expensive: one process holding one token and one lock, instead
of N processes each re-paying all three.

---

## 8. Test plan

Fake clock, no real sleeps — `monkeypatch.setattr("argus.reconcile.asyncio.sleep", ...)`,
the same convention `tests/test_shadow_tool.py`'s `_fetch_accounts` tests already
use.

| # | Test | Asserts |
|---|---|---|
| T1 | transient failure recovers within budget, process start | `reconcile.result` written, `_halted is None`, no alert, startup completes; attempt count == 2 |
| T2 | transient failure recovers within budget, per day | session proceeds to `session.configured`; no `session.aborted`, no `session.error` |
| T3 | **exhausted at process start → PARKS** | `_halted.source == "broker_unreachable"`, killswitch engaged, `control.bound_address is not None`, `StatusSnapshot.halted is True` with the reason, `process.start` on the log, **no exception out of `startup()`**, and `_run_session_day` refuses. Same shape as `test_reconciliation_mismatch_HALTS_startup_but_keeps_the_process_alive` |
| T4 | exhausted at per day | `session.aborted` recorded with the cause in `reason`; `reconcile_unreachable` alert fired; **`session.error` NOT written**; `_halted is None`; the scheduler still reaches the next trading day |
| T5 | **a real mismatch still refuses with ZERO retry** | fake broker counts calls; `fetch_positions` called **exactly once**; parks (process start) / halts+aborts (per day) exactly as today. *This is the test that proves the retry cannot leak into the mismatch path* |
| T6 | a bug in the compare is not retried | `open_positions()` raises `KeyError`; called once; propagates as today (teardown + raise at process start) |
| T7 | `_clear_ledger_if_reset` runs once across a retried reconcile | `reconcile.sandbox_reset` appears at most once with the fetch failing twice first |
| T8 | shadow + injected reconciler, unreachable | lenient: logged, startup completes, no halt (mirrors `test_shadow_startup_reconcile_mismatch_proceeds_but_loudly`) |
| T9 | `_fetch_accounts` exhaustion at process start parks | the §1.2 leg: `SessionAborted` out of `startup_reconcile` → park, not exit |
| T10 | *(built)* `session.error` carries `error_type` + truncated `error` | §10 — already passing |

T5 and T6 are the two that make the invariant a property of the test suite rather
than of this document.

---

## 9. The shared-primitive question (three call sites)

The prompt asks whether process-start reconcile, per-day reconcile, and
`DuplicateOrderGuard.has_conflicting_order` should share one
bounded-retry-then-escalate primitive. **Posed as asked, with a recommendation,
and not merged into this PR.**

**Two of the three share one already, for free.** Both reconcile sites call
`Reconciler.reconcile()`, so putting the retry at the port boundary inside it
gives them one implementation with no wrapper and no duplication — which also
answers the prompt's Issue-2 question about whether a shared wrapper fits both
shapes. It does, and better than a wrapper would: the retry sits *below* the
difference between the two sites (one opens a short-lived session, the other
reuses the day's), so it re-uses whichever session is already open and never
re-authenticates. No ban-risk exposure, at either site.

**The third should not join them**, on evidence rather than taste:

- The placement that would unify all three — retrying inside the execution
  adapter — is **forbidden** (§1.5): it would push a 96-second budget into an
  await on a 5-second signal cadence.
- The escalation targets are not the same kind of thing: park the process / abort
  the day / end the trading day's entries. Nothing generalizes across them except
  the word "escalate".
- The entry guard's own prompt raises a *different* question this one does not
  have — whether one candidate's bad luck should escalate at all, or only N
  consecutive failures should (its Option B). That is about a hot loop's
  statistics, and it has no analogue at a once-per-session call site.

**Recommendation: three locally-reasoned fixes, one shared *vocabulary*.** If the
entry-guard task wants the classification, `BrokerUnreachable` is importable and
carries `.leg`/`.attempts`; if it wants the cadence, the numbers are
`_fetch_accounts`'s and stated in one place. Neither should force a shared
escalation policy. **Richard's call — §11, D5.**

---

## 10. Issue 3 — already built (no design gate)

`app._run_session_day`'s generic handler now binds the exception and writes it to
both sinks:

```python
except Exception as exc:
    failure = {"trading_date": trading_date.isoformat(),
               "error_type": type(exc).__name__, "error": str(exc)[:500]}
    self._alert("session_error", failure)
    with contextlib.suppress(AuditWriteError):
        await self._audit.record("session.error", failure)
```

Same two fields and the same 500-char bound as `account.refresh_failed`. The
handler's comment also drops its "reaching here means a genuine bug" claim, which
2026-08-26 disproved. `tests/test_app.py::test_unexpected_session_error_does_not_stop_the_supervisor`
now pins the payload on both sinks, including that a 900-char message is
truncated to 500 rather than dropped. No behaviour change.

**This still applies after Issues 1/2 land.** Their fix removes the
broker-unreachable case from this handler; whatever else reaches it is by
definition something nobody predicted, which is exactly when the payload matters
most.

---

## 11. Decisions — SETTLED 2026-08-26

| | Decision | Outcome |
|---|---|---|
| **D1** | Bounded retry at the reconcile port boundary, 3 attempts / `min(2·n, 5)` | **APPROVED as proposed** — §2 |
| **D2** | Per-await budget, with the reconcile fetches taking their own per-request bound rather than the 30s client-wide default | **APPROVED, AMENDED** — new `[reconcile] request_timeout_s = 10.0`, a required config key. §2.2–2.3 |
| **D3** | Process start on exhaustion: park instead of exit, overturning `app.py:952`'s consequence while keeping its premise | **APPROVED, AMENDED** — park is now the TERMINAL state of a self-healing outer retry loop that never enters the halted state while it is still trying. §4.6 |
| **D4** | Per day on exhaustion: abort the day + alert, no process halt, in all environments | **APPROVED**, and extended with the same outer retry before aborting. §6.1 |
| **D5** | Entry-guard task stays separate (shared vocabulary, not a shared primitive) | **APPROVED as proposed** — §9 |
| **D6** | Route the other could-not-ask legs into the same treatment | **APPROVED, AMENDED** — split: could-not-ask becomes `BrokerUnreachable` and is retried; "no matching account" stays `SessionAborted` and parks terminally without retry. §4.4 |
| **D7** | Working arrangement | **`main`, no commit, no push.** Richard reviews the working tree |

## 12. Open questions, deliberately not built

- **Should N consecutive unreachable *days* at the per-day site escalate to a
  halt?** One missed day is a bad day; five in a row is a broken deployment. The
  30-minute intraday loop does not answer this — it makes one day survivable, not
  five. No evidence yet on what N should be, and inventing one is how a rail gets
  a number nobody can defend.
- **`session.aborted` fires no alert at all** (§1.4). This design routes the
  unreachable case around that rather than changing it, because every *other*
  abort reason (no matching account, missing halt seam, no quote within 30s) would
  start alerting too and none of them has been reviewed for it. Worth its own
  look.
- **`KillSwitch` still has no `disengage`, and `_halted` is still terminal.** The
  self-heal in §4.6 was built to avoid needing either. If a future task genuinely
  needs a clearable halt, that is its own design and it touches a non-negotiable.

## 13. Build order

1. `config.py` + `argus.toml` + `argus.dev.toml`: the `[reconcile]` section
   (§2.3), and `tests/support.py`'s config builder.
2. `reconcile.py`: `BrokerUnreachable`, `_ask_broker`, wire into
   `Reconciler.reconcile()`, `on_attempt_failed` seam, per-request timeout.
3. `reconcile.py`: `reconcile_and_record` catches → Tier 1 record + alert →
   re-raise.
4. `reconcile.py`: `reconcile_with_retry` — the ONE outer loop both call sites
   take (§4.6), `stop`-aware, `SessionAborted` passing straight through.
5. `session.py`: wrap `tt.open()` and `_fetch_accounts` exhaustion as
   `BrokerUnreachable` in `startup_reconcile`; keep "no matching account" as
   `SessionAborted`; correct the docstring.
6. `session.py`: `_reconcile_or_abort` goes through the outer loop, then
   `SessionAborted` on exhaustion.
7. `app.py`: `_reconcile` goes through the outer loop; on exhaustion
   `_halt_process(source="broker_unreachable")`; `set_reconciling()` around it;
   the `except BaseException` path is untouched.
8. Tests T1–T9, plus the self-heal and operator-override cases.
9. `README.md` audit-category table: `reconcile.unreachable`,
   `reconcile.unreachable_attempt`.
10. Decision record in `docs/decisions/` + its row in `docs/decisions/README.md`,
    stating explicitly that `app.py:952`'s **premise was preserved and its
    consequence overturned**, and why — and that the config deploys WITH the
    image.
