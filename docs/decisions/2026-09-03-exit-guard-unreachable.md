# The exit's duplicate guard, unreachable (2026-09-03)

**Status: BUILT.** `src/` only — `execution/loop.py` and `execution/watchdog.py`.
**No new config key**: the per-attempt bound is the existing
`[execution] guard_timeout_s`, so the image deploys alone. Raised by v0.3.15's
first live-fire day, which ended at 11:04:05 ET with the process halted, the
watchdog dead, and a triggered 0DTE spread held unmanaged to settlement.

## What happened

v0.3.15 booted at 07:45:17 ET and traded normally into the window: 701 candidates,
7 `entry.finished`, 10 submit attempts. Cert then began failing — 90
`account.refresh_failed` against 27 recoveries, and **7 of 10 submits refused**.

One spread was open: `argus-open-20260903-call-7725-7730`, 2 contracts,
`entry_credit 0.70` on the new realized basis (`entry_limit 0.65` — v0.3.15's
one-tick understatement, working as designed). At ~11:03:14 `credit_multiple`
began holding at `cost_to_close 1.65` against `credit_stop 1.40`: a real breach,
0.25 wide, far past `PRICE_EPSILON`.

| ET | seq | |
|---|---|---|
| 11:04:03.15 | 682285 | Exit episode 1: submit failed, identifier probe named nothing → `NOTHING_AT_BROKER`, send 1 of 3 |
| 11:04:04.01 | 682289 | `watchdog.working_orders_unread` — 502 on the same endpoint |
| 11:04:05.02 | 682292 | Exit episode 2 (P0's retry). `walk.started`, then `DuplicateOrderGuard` → `get_live_orders()` |
| 11:04:05.04 | 682293 | 502. Uncaught. `watchdog.fatal` → kill switch → `HaltGrade.ALL` → `request_stop()` |

```
internal_server_error: Failed request to get
http://sandbox-nginx.service.consul:8080/customers/me/permitted
?resource-subject=%2Faccounts%2F3AD79010%2Forders%2Flive&resource-action=GET - 502
```

**The process died over a GET, asked before anything was sent.**

## What it cost

`_halt_on_exception` sets the SHARED stop event, so `HaltGrade.ALL` ended the
session. From 11:04 ET the held spread had **no virtual stop, no mark, no
`/status` row, and — the one that matters on 0DTE — no 15:30
`FLATTEN_BEFORE_CLOSE`.** Every session-scoped field on `/status` read `null`
while a live position sat 8 points OTM of its short strike.

The position was **perfectly evaluable at the instant of the halt**. It had been
priced at 1.65 that same second, by the very tick that triggered the exit. What
failed was one REST read about orders; what was disabled was the monitor.

That is A2's shape at process scope — *a monitor disabled by the failure of an
action it requested* — and P2/P3's harm exactly: the flatten is the last-resort
control, and a halt that parks takes the day's last-resort control with it.

### The cost is ≈ $670, and the day supplied its own control

The day ran **two** call spreads, adjacent strikes, entered five minutes apart,
both triggering `credit_multiple` in the same window. One had a working watchdog
and one did not.

| | entered | credit (realized) | exit | debit | P&L | held to settlement |
|---|---|---|---|---|---|---|
| `call-7720-7725` | 10:10:18 | 0.75 | before the halt | **1.70** | **−190.00** | −850.00 |
| `call-7725-7730` | 10:05:25 | 0.70 | 15:37+ (after restart) | **5.00** | **−860.00** | −860.00 |

SPX ran from 7716.76 at the halt to a 7737.5 settle — 12.5 points through the
short strike and 7.5 through the long. By the time a restarted watchdog could act, the
position was deep ITM and worth the full width; the flatten closed it at 5.00,
which was fair value, not a bad fill. **The exit mechanism was never the problem
and was not what failed.**

The counterfactual is not the 1.65 mark on the halting tick — *`cost_to_close` is
a MARK, never a fillable price* — it is the **sibling's realized 1.70**, five
strikes away, in the same minutes, from the same trigger. On that basis the
blocked exit was worth about −190, and **the halt cost ≈ $670 on one 2-lot
spread.**

`tools.mark` scores the same pair independently: *"exiting early saved 660.00"* —
and every dollar of that came from the position whose watchdog survived. The one
the halt disarmed saved exactly zero, because paying 5.00 and letting it settle
are the same number.

**Two spreads, one variable.** This is the cleanest measurement of the credit
stop's value the system has produced, and — because both positions carry
`entry_credit_basis = realized` — the first pair admissible under the
`credit_multiple` prohibition at all. It is n=2 in cert and settles nothing about
the threshold; it is recorded because the *next* pass on `credit_multiple` should
start from it, and because it is the exact opposite regime from 2026-09-01's
trending-then-reverting day, where the stop looked maximally wrong.

## The gap, and why it is the third of three

The exit calls the loop with `guard_first_submission=True`
(`watchdog.py`, `ExitExecutor.close`), and the guard branch inside `run()` was a
**bare await — no timeout, no retry** — propagating by design into the watchdog's
hard-halt region. `close`'s own docstring recorded that as the intended posture.

This is one class of defect, and 2026-08-26 closed two of its three instances:

| call site | verdict on unreachable | fixed |
|---|---|---|
| `reconcile._ask_broker` | bounded retry, then park/abort the day | 2026-08-26 |
| `entry._check_for_duplicate` | bounded burst, then retire the trade idea | 2026-08-26 |
| **the exit's guard, via `loop.run`** | **halt the process** | **this record** |

The invariant the other two hold — *a mismatch is a VALUE, unreachable is an
EXCEPTION, and unreachable is not a verdict* — was simply not applied here.

**The entry record names this exact configuration as wrong**, in `entry.py`'s own
module docstring:

> Delegating it to the loop with `guard_first_submission=True` was the first shape
> of this and it was WRONG, for a reason worth recording: a guard failure
> propagates out of `run()` (chunk 3, deliberately — "I could not check" must not
> become an outcome), which would land it inside the hard-halt region and take the
> whole process down for a question that was asked before anything was sent.

That reasoning was written from the entry path's seat. Its "Deliberately not done"
section says *"nothing else on the entry path has this shape"* — true, and scoped
to the entry path. Nobody looked one module over. **`guard_first_submission=True`
has exactly one caller in `src/`, and it is the exit.**

### The same day ran the A/B itself

Same broker, same hour, same endpoint:

- **Entry side**: `entry.guard_unreachable_attempt: 7`,
  `entry.guard_unreachable_abandoned: 2`. Seven failed round trips, two trade
  ideas retired, the day continued.
- **Exit side**: **one** failed round trip, process halted, position unmanaged.

## The fix

**The guard is asked under a bounded burst; a spent burst is a refusal to send,
not a failure.**

- `loop._ask_guard` — 3 attempts, 1s/2s backoff, each under
  `[execution] guard_timeout_s`. On a spent burst it raises `GuardUnreachable`,
  a **typed** exception carrying `attempts` and `last`.
- `ExitExecutor.close` catches it and returns
  **`ExitOutcome.NOT_ATTEMPTED`** — nothing was sent, the position keeps its one
  exit attempt AND its flatten override, the trigger stays latched, and the next
  cadence tick rebuilds and asks again.

### Six decisions inside that

**1. The exception stays an exception; only its type and its bound change.**
Chunk 3's contract — *"I could not check" must never become a `WalkOutcome`* — is
untouched, because a caller that reads it as one more not-filled case sends
anyway. What changed is that the raise is now typed, so `close` can catch exactly
this and nothing else, and the hard-halt region no longer swallows a transport
error by default.

**2. `NOT_ATTEMPTED`, not a new outcome.** The rule is 2026-09-02's, verbatim:
*the allowance downstream asks exactly one question — was anything sent — and
splitting the outcome would put a second rule back into the thing `_run_exit`'s
restore exists to reduce to one.* This is a fourth cause joining three
construction refusals, and it gets a fourth **sentence**, not a fourth outcome and
not a fourth escalation tag. The operator's next move differs ("the broker cannot
be reached" vs "the feed is thin") and `report.reason` is where that belongs.

**3. The bound is on RATE, not COUNT — the opposite of `_EXIT_SEND_ATTEMPTS`.**
A guard check is a GET; it costs no submission. Bounding it by count would mean a
broker down for three ticks at 11:04 leaves the position unmanaged at 15:30 even
though the broker recovered at 15:00 — today's harm re-created in a smaller box.
Three things bound the rate instead, and none of them is a counter:

- one exit episode at a time per position (`check_once` skips a key in
  `_exiting`), so bursts cannot overlap;
- each burst is ≤ 3 GETs and ~12s wall clock against a timing-out broker, which is
  **≤ 20 GETs/minute** — against an endpoint the watchdog already polls 4×/minute;
- `ThrottledOrderExecution` wraps every order request in the process, *including
  the duplicate guard's `get_live_orders()`*, so the structural ceiling is already
  in place and was designed for exactly this ("the runaway half").

**4. `guard_timeout_s` is REUSED, not duplicated.** It bounds the same call, to the
same endpoint, through the same port method as the entry burst. A second key would
be a coupled deploy for a number that has one right value — and this fix wants to
ship as an image alone.

**5. The attempt COUNT is a separate constant from entry's, deliberately.**
`loop._GUARD_ATTEMPTS` is 3 and `entry._GUARD_ATTEMPTS` is 3, and they are not
shared: the two bursts have different consequences when spent (an entry retires a
trade idea and can eventually end the day; **an exit must never do either**), so
one number would carry two decisions. Same split `_EXIT_SEND_ATTEMPTS` already
makes.

**6. The scope is drawn by POSITION, not by exception type**, with one
exclusion. Only the guard await sits inside the `try`, so no type list has to keep
up with whichever transport error the SDK raises this month — 2026-08-26's was an
unwrapped `httpcore2.ReadTimeout`, 2026-09-03's a `TastytradeError` wrapping an
nginx 502 page. **`AuditWriteError` is re-raised untouched, ahead of everything
else**: `has_conflicting_order` writes Tier 1 `order.duplicate_blocked` from
inside itself, so a dead recorder surfaces from what looks like a pure read, and a
dead recorder must halt and must never be re-attempted.

### `walk.started` moved below the guard

A question asked before anything is sent did not start a walk. The 2026-09-03
trail carries the proof of what that costs a reader — **`walk.started: 11` against
`walk.finished: 10`**, the missing one being the walk that never happened. The
record now sits below the guard and **above the `blocked` return**, so:

- guard unreachable → no walk records at all (the exit episode is still visible as
  `watchdog.exit_started`);
- `BLOCKED` → the pair is intact, which is the regression this reordering could
  otherwise have caused and which has its own test.

Nothing depends on `walk.started` preceding broker contact: spec §6's
"durable before the wire" rule is `order.submit_attempt`'s, inside
`AuditedOrderExecution`, and the guard writes its own Tier 1 row.

## What this does NOT change

- **The duplicate invariant.** No submit is reachable without an answered
  `ConflictVerdict`. The retry is on the QUESTION, never the verdict, and
  `guard.py`'s classification is untouched — WORKING/FILLED block, DEAD allows,
  unclassifiable blocks.
- **The one-attempt rule**, for anything that reached the broker.
- **`_EXIT_SEND_ATTEMPTS`.** A spent guard burst is not a send and charges no
  budget.
- **The watchdog's halt policy.** A programming error and a dead recorder still
  halt. What is removed from that set is one condition that was never a defect.
- **Where the retry lives.** Not inside `guard.py` (CLAUDE.md), not widened past
  the port await, not inside the execution adapter.

## Known consequence, accepted

A burst that succeeds on attempt 2 or 3 submits a closing order at a limit priced
1–3s earlier, and the exit walk has **no drift check on its first rung** (armed for
entry walks only). This is why the ladder is the fast one (1s, 2s) rather than
`reconcile.py`'s: every second is unchecked price staleness on the one rung
`UnderlyingDriftCheck` never sees. The entry path accepted the identical exposure
in the worse direction (opening at a stale price); on the exit the walk itself is
the correction — a limit that is too low simply does not fill and steps toward the
market.

## The manual recovery, and what it argues

Richard restarted at 15:37:49 ET. The session was running by 15:37:57 — **eight
seconds** — and within two ticks (the chain subscription warming produced one
`watchdog.exit_unpriceable` and one `_still`) the watchdog had adopted the spread,
fired, walked and closed it: one `walk.started`/`walk.finished`, one
`order.submitted`, four `order.fill`s, book flat with two minutes to settlement.

Three things that recovery confirms, none of them by design of this fix:

- **The scheduler re-enters the same trading day.** `plan()` gates on
  `window[0] <= now < window[1]`, so a restart inside the session window resumes
  it. Past 15:30 `seconds_to_flatten <= 0`, so the flatten fires on the first
  evaluable tick.
- **Kill-switch persistence worked exactly as specified** (2026-09-01): the switch
  came back `engaged`, `restored: true`, showing the ORIGINAL 15:04:05 press time,
  with `halted: false` beside it — rehydration restores the switch and never the
  `HaltRecord`. Entries stayed blocked; exits and the flatten were untouched, which
  is the three-point entry scope doing its job.
- **The halt did not need to be process-wide.** Everything the halt destroyed was
  restored by a human doing nothing but starting the process again, under the same
  broker conditions, in eight seconds. That is the strongest available argument for
  the still-open `HaltGrade` question below: if a restart is a sufficient response,
  the halt was never protecting anything that a narrower grade would have exposed.

## Not fixed here, and still owed

- **`HaltGrade` for a watchdog exception is still `ALL`**, and
  `_halt_on_exception` still sets the shared stop event. This record removes the
  cause that fired on 2026-09-03; it does not answer whether a *genuine* watchdog
  defect should keep taking the flatten down with it. That is P3's question one
  source further along, and the 2026-09-01 `EntriesEnded` split is its precedent.
- **A submit-time refusal still reports as an absence, and it is now the day's
  dominant failure mode.** `tools.desk` scores **5 of 7 entry attempts (71%) as
  `no_order_exists`** with `reject_reason: null`, where the truth was a 502 or an
  instrument-validation refusal; the exit path burned one of three sends the same
  way. Already on v0.3.15's not-in-this-release list, and no longer only a
  reporting gap: it spends budget on the exit and retires trade ideas on the
  entry, and an operator cannot tell a transient from a permanent one.
- **`working_orders_unread`'s blind spot**, unchanged.

## Tests

Six behavioural (`tests/test_watchdog_exit.py`, G1–G6) plus the `walk.started`
pairing assertion in `tests/test_execution_loop.py`, and T5's AST sweep extended.

`test_an_unreachable_broker_at_the_guard_propagates_and_halts` — the test that
pinned the old posture — is **replaced**, not deleted, by
`..._sends_nothing_and_does_not_halt`. Its one load-bearing assertion
(`broker.submitted == []`) survives verbatim.

T5 needed extending rather than exempting. Every other `NOT_ATTEMPTED` in `close`
sits lexically above the walk, which is how the flatten restore proves it can
never describe a sent order — and **an `except` clause cannot sit above what it
wraps**. So the report is built in `_guard_unreachable_report`, and the test now
asserts that helper contains no submit path and no `await` at all, plus that the
set of helpers called below the walk in `close` is exactly the three reviewed ones.

**Seven mutations run, all seven caught:** production not opting into the burst;
`_GUARD_ATTEMPTS = 1`; `AuditWriteError` retried like any other; `walk.started`
moved back above the guard; `close` re-raising instead of refusing; the `blocked`
return moved above `walk.started`; and a submit path added to the report helper
(caught twice — by T5 and by the flatten's own no-submission-path sweep).

Suite: **1949 passing, up from 1927 at v0.3.15.**
