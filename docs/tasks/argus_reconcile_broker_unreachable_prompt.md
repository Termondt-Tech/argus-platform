# Argus: A Broker-Unreachable Reconcile Kills the Process Instead of Parking It

## Priority

**DESIGN FIRST for the two reconcile call sites (below) — do not implement
without Richard's sign-off.** Both touch the process-start and per-day
reconcile paths, which CLAUDE.md's non-negotiables cover directly
(broker-is-source-of-truth, halted-but-alive), and the fix has to reconcile
with an EXISTING deliberate design comment that argues for the current
behavior (quoted below) — this is not a bug nobody thought about, it is a
tradeoff someone made explicitly and that tradeoff needs to be revisited on
purpose, not routed around silently. The third item (`session.error`'s empty
payload) is small, mechanical, and safe to build directly without a design
pass — see its own section.

Target: **v0.3.11**, a stability release. Richard is separately assembling
other items for the same release; this prompt covers exactly the three
defects below and nothing else.

## Context — observed live, 2026-08-26

`v0.3.10` was deployed intraday on a confirmed blackout day (deliberately —
zero position risk, zero entry risk, and it put the just-shipped account-
refresh fix in front of a live cert failure storm). Restarting the service
triggered the process-start reconcile, which hit tastytrade cert returning a
502:

```
File "argus/app.py", line 497, in startup
    await self._reconcile()
File "argus/app.py", line 956, in _reconcile
    result = await startup_reconcile(...)
File "argus/session.py", line 498, in startup_reconcile
    return await reconcile_and_record(...)
File "argus/reconcile.py", line 835, in reconcile
    positions = await self._broker.fetch_positions()
File "argus/account/rest.py", line 28, in fetch_positions
    raw = await self._account.get_positions(self._session)
tastytrade.utils.TastytradeError: Couldn't parse response: <html>
<head><title>502 Bad Gateway</title></head>
...
```

`TastytradeError` is not `ReconciliationError`, so it fell to
[`app.py`'s generic handler](../../src/argus/app.py):

```python
# app.py:509
except BaseException:
    await self._teardown()
    raise
```

`_teardown()` does not write `process.stop`. The process exited; Docker's
`restart: unless-stopped` restarted it; cert 502'd again; repeat. **Five
`process.start` records in 33 seconds**, confirmed from the audit log
(`seq 472443` through `472451`, seven `process.start` categories total across
the whole incident), no `process.stop` between any of them, and no
`reconcile.result` — the process died before the comparison itself, every
time, until cert answered on the fifth attempt.

A second, separate failure followed once the process stayed up: the
PER-TRADING-DAY reconcile (a different call site — `session.py`'s
`_reconcile_or_abort`, not `app.py`'s process-start one) hit a `ReadTimeout`
against the same cert instability:

```
File "argus/app.py", line 801, in _run_session_day
    await body(...)
File "argus/session.py", line 759, in run_session
    await _reconcile_or_abort(...)
httpcore2.ReadTimeout
```

This one did NOT crash the process (it's inside `_run_session_day`'s
`except Exception` in `app.py`, which alerts and idles to the next trading
day rather than tearing down) — but it was recorded as `session.error` with
payload `{"trading_date": "2026-08-26"}` and nothing else, and the comment on
that handler reads: *"Login, reconcile, and account failures are already
handled gracefully inside the body... reaching here means a genuine bug."*
That comment is now demonstrably wrong for this exact case — a `ReadTimeout`
reaching the broker is not a bug, it's the network doing what cert has been
documented (repeatedly, in this codebase's own comments) to do.

## Issue 1 — process-start reconcile: unreachable broker kills the process

**This is a documented, deliberate design decision, not an oversight — read
it before proposing anything.** `app.py`, the comment directly above the
process-start reconcile call:

```python
# app.py:952
if mismatch_halts_process(self._config.environment):
    # Build one. Any failure here — unreachable broker, missing credentials,
    # no matching account — propagates out of `startup()` and tears the
    # half-built process down: sandbox/live must not start without having
    # ASKED the broker, and "could not ask" is not "nothing to report".
    result = await startup_reconcile(...)
```

That reasoning is real — a process that has never once proven it can reach
the broker arguably should not sit around pretending to be healthy. But
today's incident is evidence against the CONSEQUENCE, not the premise: by the
time this reconcile runs, `control.start()` has already bound and
`process.start` is already on the audit log — the process genuinely IS
serving `/status` and `/kill` (see `app.py`'s own comment on that ordering:
*"by here the process genuinely HAS started"*). Tearing down and exiting
drops that reachability, repeatedly, for as long as cert stays unreachable —
and Docker's blind restart gives the operator no visibility into WHY (no halt
reason, no detail, nothing on `/status` — because there IS no `/status` while
the process is down) where a `reconciliation_mismatch` halt gives all three.

**Also worth noting: `startup_reconcile`'s own docstring is already wrong
about this.** It claims (`session.py:468`): *"Raises `SessionAborted` if the
broker cannot be reached."* That is true for the internal `_fetch_accounts`
step (which has a real 3-attempt bounded retry, see below) but NOT true for
the reconcile-comparison step itself (`reconciler.reconcile()` →
`fetch_positions()`), which is exactly what raised the raw `TastytradeError`
above with no retry and no `SessionAborted` wrapping. The docstring describes
the behavior someone intended, not the behavior that exists.

### What to investigate

1. **Should an unreachable broker at process start retry before anything
   else happens?** `session.py`'s `_fetch_accounts` (used by both reconcile
   call sites) already has exactly this shape — 3 attempts, backoff
   `min(2.0 * attempt, 5.0)`, raises `SessionAborted` (not a bare exception)
   on exhaustion. The reconcile step itself has none. Should
   `Reconciler.reconcile()` — or its caller — get the same bounded retry
   before any decision is made about halting vs. exiting? This absorbs a
   single transient 502/timeout without ever surfacing a halt, which is
   almost certainly right regardless of what you decide for #2.

2. **On exhaustion, should the process PARK (halted, serving `/status` and
   `/kill`) instead of exiting?** This is the real design question and the
   one that revisits the comment quoted above. If yes: it can almost
   certainly reuse the EXACT SAME `halt` seam
   (`self._halt_process(...)` / `source=...`) that a `ReconciliationError`
   already uses — same non-negotiable text applies (*"an unrecoverable halt
   PARKS the process... instead of exiting"*), same operator recovery path
   (a restart re-reconciles). If the answer is "no, process-start really
   should differ from mid-session," the design must say explicitly WHY the
   premise in the existing comment still holds even after weighing today's
   observed cost (five gaps in control-surface reachability) — do not
   silently keep the current behavior without addressing the comment's own
   reasoning head-on.

3. **Does the environment split (`mismatch_halts_process`: shadow aborts the
   day, sandbox/live halts the process) apply here too, or is
   "unreachable" categorically different from "reached and found a
   mismatch"?** Read `mismatch_halts_process`'s full docstring
   (`reconcile.py`) before deciding — it was deliberately built as ONE
   function BOTH call sites share so they can never drift, and the same
   discipline should probably extend to whatever this task adds, but verify
   rather than assume the same boundary is correct for a different failure
   class.

## Issue 2 — per-day reconcile: unreachable broker misclassified as a bug

`_reconcile_or_abort` (`session.py`) wraps the mismatch VERDICT correctly
(returns a `ReconciliationResult`, branches on `mismatch_halts_process`,
raises `SessionAborted` either way once resolved) but has **no handling at
all** for failing to reach the broker in the first place — the call to
`reconcile_and_record` sits inside a bare `try/finally` that only closes the
store connection. A `ReadTimeout` there propagates straight out of
`_reconcile_or_abort`, past `run_session`'s own `except SessionAborted`
handler (it isn't one), and lands on `app.py`'s generic
`except Exception → session.error` — the "reaching here means a genuine bug"
path, for something that is not a bug.

### What to investigate

1. **Same bounded-retry question as Issue 1**, at this call site. Consider
   whether the fix belongs in ONE shared place both reconcile call sites use
   (extending `reconcile_and_record` itself, or a wrapper both call it
   through) rather than two independent retry loops that can drift apart —
   `mismatch_halts_process` was built single-sourced for exactly this reason
   and the same argument likely applies here. Verify it fits both call
   sites' actual shapes (one opens its own short-lived broker session,
   the other reuses the day's) before assuming a shared wrapper is clean.

2. **On exhaustion, this one is more settled but still needs a decision
   written down**: unlike process start, this path already knows how to end
   a session cleanly (`raise SessionAborted(...)`, caught and recorded as
   `session.aborted` by `run_session`'s own handler) — so "retry, then treat
   exhaustion the same way `Reconciler` returning `ok=False` is treated" is
   the natural shape. Confirm whether that should also go through
   `mismatch_halts_process`'s environment gate (i.e., should sandbox/live
   also HALT — not just abort the day — on a broker it could never reach,
   the same as it halts on a broker that answered with a mismatch)? Argue
   it, don't assume it.

### The one invariant that must not move, in either issue

**Never let a retry blur "the broker is unreachable" into "the broker
answered and disagrees."** A `ReconciliationError` (mismatch found) must
NEVER be retried past — that's the whole rail. Whatever exception-handling
shape you land on has to keep those two cases structurally distinct: retry
logic wraps ONLY the broker-unreachable branch (a `TastytradeError`,
`ReadTimeout`, connection failure — the fetch itself failing), never the
"fetch succeeded, comparison found a discrepancy" branch. If the design
can't state clearly how it keeps those apart at the type/exception level,
stop and say so rather than shipping something uncertain — this is the same
bar the entry-guard task below was held to.

## Issue 3 — `session.error` records no error detail (no design needed, build directly)

`app.py`'s generic session-failure handler:

```python
# app.py:826
self._alert("session_error", {"trading_date": trading_date.isoformat()})
with contextlib.suppress(AuditWriteError):
    await self._audit.record(
        "session.error", {"trading_date": trading_date.isoformat()}
    )
```

Diagnosing today's incident required a live journal dig
(`sudo journalctl -u argus.service`) specifically because this payload
carries nothing about what failed — no `error_type`, no message. Compare
`account.refresh_failed`'s payload (`stream.py`), which already carries
`error_type`, a truncated `error` string, and (since v0.3.10) `leg` /
`error_class`. Match that shape here: add `error_type: type(exc).__name__`
and a truncated `error: str(exc)[:500]` (same 500-char bound
`account.refresh_failed` uses) to both the `session_error` alert payload and
the `session.error` audit record. No behavior change, no new exception
handling — purely what gets written down about an exception that already
propagates exactly as it does today. Build this regardless of what Issues 1
and 2 land on; if their fix changes what reaches this handler, this still
applies to whatever residual case still does.

## Related, already-filed, still unresolved

[`argus_entry_guard_unreachable_retry_prompt.md`](argus_entry_guard_unreachable_retry_prompt.md)
is the same shape of problem — a broker-unreachable failure mid-critical-path
escalating to a costly response with no retry — at a THIRD call site
(`DuplicateOrderGuard.has_conflicting_order` in the entry pipeline), also
still open. Read it; do not duplicate its reasoning from scratch. Worth
asking explicitly in the design write-up: **should a bounded-retry-then-
escalate pattern become one shared primitive across all three sites**
(process-start reconcile, per-day reconcile, entry guard), or are the three
call sites different enough (synchronous-in-a-hot-loop vs. once-per-session,
different escalation targets) that three independent, locally-reasoned fixes
are actually correct? Pose this as a question in the write-up — do not
merge the three tasks into one PR without Richard agreeing that's the right
scope, and do not let this task block on the entry-guard task's own
resolution; they can land independently.

## Constraints

- **`mismatch_halts_process`'s existing shadow-vs-rest boundary is not up for
  redefinition here** — reuse it, extend what feeds into it, or justify a
  parallel boundary for the unreachable-broker case; do not change what it
  means for a MISMATCH.
- **The halted-but-alive non-negotiable governs any "park" behavior you add**
  (`docs/decisions/2026-08-17-halted-but-alive.md`): the surface must stay
  bound before any rail that can refuse, and a halt must never become an
  exit. If Issue 1's fix introduces a new halt path, it needs the same
  scrutiny that non-negotiable already demands of every existing one.
- **`_fetch_accounts`'s retry/backoff numbers (3 attempts,
  `min(2.0*attempt, 5.0)`) are the existing precedent** — reuse them unless
  you have a specific reason this call site needs different ones, and state
  that reason if so.
- Repeated partial-startup cycles (today's five) each acquire and release the
  writer `flock` and reopen the audit DB. Confirm there's no accumulating
  resource cost to a longer unreachable-broker window than today's — a check,
  not an expected finding.

## Testing

Fake clock, no real sleeps, matching every other timing-dependent test in
this suite. At minimum:

- A transient broker-unreachable failure that recovers within the retry
  budget: reconcile proceeds normally at both call sites, no halt, no
  `session.error`/process exit.
- Exhausted retries at process start: process PARKS (if that's the design
  landed on) — `/status` and `/kill` remain reachable, `halted.halted` is
  true with a reason naming the cause, no process exit. Assert this with the
  same shape of test `docs/decisions/2026-08-17-halted-but-alive.md`'s fix
  used.
- Exhausted retries at per-day reconcile: `session.aborted` recorded (and, if
  the design extends the environment gate, a process-wide halt in
  sandbox/live) — never a bare `session.error`.
- A REAL mismatch (broker reachable, comparison disagrees) still halts/aborts
  exactly as today, with ZERO retry — this is the test that proves Issue 1/2's
  retry logic cannot leak into the mismatch path.
- `session.error`'s payload (wherever it still fires) carries `error_type`
  and `error`, matching `account.refresh_failed`'s shape.

## Deferred / explicitly out of scope

- `account/refresher.py`'s own bounded-budget model (`account_refresh_timeout_s`)
  — working as intended, not touched by this task.
- The entry-guard task's own resolution — related, not merged into this one
  unless Richard says otherwise (see "Related" above).
- Any change to `mismatch_halts_process`'s shadow/sandbox/live boundary
  itself.
- `docs/releases/v0.3.10.md`'s "DEPLOYED, NOT PROVEN" line for the
  account-refresh change — separate housekeeping, not part of this task.

## Deliverable

For Issues 1 and 2: a written investigation and recommendation FIRST
(`docs/designs/`, following the existing lifecycle in
`docs/designs/README.md`) — covering the retry question, the park-vs-exit
question, and the environment-gating question for each call site, with the
invariant above (never retry past a real mismatch) explicitly addressed. Get
Richard's sign-off before writing implementation code. Once approved: the
code change, test coverage per above, and a decision record in
`docs/decisions/` (add its row to `docs/decisions/README.md`) — including
whether the existing `app.py:952` comment's reasoning was overturned or
preserved, since that comment is the thing this task exists to either
confirm or revise on purpose.

For Issue 3: build directly, no design gate. Include it in the same
decision record as 1/2 if they land together for v0.3.11, or ship it alone
first if 1/2 need more time — Richard's call.

Confirm with Richard whether to work on a branch or directly on `main`, and
whether to commit — no standing convention is recorded for this repo either
way; do not assume. Do not push regardless, unless explicitly told to.
