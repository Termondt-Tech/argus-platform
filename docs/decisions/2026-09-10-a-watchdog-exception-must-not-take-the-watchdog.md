# A watchdog exception must not take the watchdog (2026-09-10)

**DESIGNED, SIGNED OFF AND BUILT 2026-09-10** — `src/` only
(`execution/watchdog.py`, `execution/escalation.py`, `status.py`, `session.py`), no
config key (the one number is a module constant), no migration, so **the image
deploys alone**. Suite 2113 passing. Design, with the rejected shapes and the
as-built deviations:
[`2026-09-10-a-watchdog-exception-must-not-take-the-watchdog.md`](../designs/2026-09-10-a-watchdog-exception-must-not-take-the-watchdog.md).

**The rule, and it is the whole record in one line: a watchdog exception now
engages the kill switch at `HaltGrade.ENTRIES` and KEEPS WATCHING.** Entries stop
immediately and permanently, because a watchdog that raised cannot be trusted to
stop out what a new entry would open. Everything else runs on, because the watchdog
is the only thing that can close or flatten what is already held.

This closes the last open halt-scope question. P3 (2026-08-29) graded the
reconciliation halt; 2026-09-01 narrowed `_end_day` the same way one level down;
2026-09-03 removed the *cause* that fired here and explicitly left the class open.
This is that class.

Precedent chain: [halt scope split](2026-08-29-halt-scope-split.md),
[end-day keeps the watchdog](2026-09-01-end-day-keeps-the-watchdog.md),
[exit guard unreachable](2026-09-03-exit-guard-unreachable.md),
[exit attempt taxonomy and halt scope](2026-08-28-exit-attempt-taxonomy-and-halt-scope.md).

## The cost, measured

**≈ $670 on 2026-09-03**, and the day supplied its own control. Two call spreads,
adjacent strikes, five minutes apart, both triggering `credit_multiple` in the same
window:

| Position | Watchdog | Outcome |
|---|---|---|
| `call-7720-7725` | survived | exited at a 1.70 debit — **−190.00** |
| `call-7725-7730` | taken down by the halt | closed after the manual 15:37 restart at the full 5.00 width — **−860.00**, identical to holding it to settlement |

`tools.mark` scores the pair independently at *"exiting early saved 660.00"*, all of
it from the position whose watchdog survived. The halted one was **perfectly
evaluable at the instant it was abandoned** — priced at 1.65 that same second. The
trigger was a single cert 502 on a `get_live_orders()` GET asked *before anything was
sent*.

## The defect, and why the finding understated it

`_halt_on_exception` took the watchdog down **three independent ways**, and the
go-live finding named two:

| # | Mechanism | What it destroyed |
|---|---|---|
| 1 | `self._stop.set()` | the **shared** session stop event — ends the signal loop, aborts any exit walk in flight (`ABORTED`), ends the session body |
| 2 | `await self._halt(...)` with no `grade=` → `ALL` → `request_stop()` | the scheduler, and therefore every remaining session; the process parks |
| 3 | `return` from `run()` | **the watchdog task itself**, independent of both |

**3 is the one that has to be fixed for either of the others to help, and it was in
no write-up of this finding.** Fixing 1 and 2 alone yields a session that runs to the
close with `/status` reporting `running`, no HALTED-`ALL` badge, and nothing watching
a held 0DTE position — strictly worse than the old behaviour, where the hard stop at
least prompted the restart that recovered the 2026-09-03 position in eight seconds.
**A design that "grades the halt" and stops there is a regression.** That is the most
important thing in this record.

The stop event was shared with `ExitExecutor` and `EntryExecutor` (all three take
`session.py`'s `stop`), so `self._stop.set()` never meant "stop the watchdog": it
meant *end the trading day, and abort the exit walk currently trying to close the
position this exception was raised about.* `_end_day` had exactly this defect and
2026-09-01 fixed it by not using the shared event. **The rule that record states —
narrow the ACTION, never widen the event — applied here unchanged.**

The old posture was signed off on reasoning the module still carried verbatim:

> the failure mode is a real position sitting open with nothing watching it, which is
> the precise gap this module was built to close.

The response to that failure mode **guaranteed it**. This is A2's shape at process
scope, and 2026-08-28's rule one level up: *a monitor must not be disabled by the
failure of an action it requested.*

## What was decided

Ten questions, all resolved in the design before any code. The five that matter
afterwards:

**The grade is `ENTRIES`, and the watchdog passes it.** P3's own rule is that the
grade is a property of the failure, not of the call site. `HaltGrade` is imported
into `watchdog.py` from `reconcile.py` — no cycle, since `reconcile` imports only
`execution.models` and `execution.store`. Moving `HaltGrade` to its own module was
rejected as churn; if a second `execution/` module ever needs it, that move becomes
right. **`HaltGrade.tighten` makes this safe for free**: an `ENTRIES` halt arriving
after an `ALL` halt cannot relax it.

**The halt fires on the FIRST exception, unchanged from the old posture.** A bound
was considered and rejected on three grounds. It needs a number — a config key (a
coupled deploy this change otherwise avoids entirely) or a fourth magic constant. Its
false-negative window is entries taken while blind, and at `cadence_s` even three
passes is time enough for the gate to authorise a position nothing can stop out. And
it buys nothing on the cost side: **the old posture already tripped the kill switch on
the first exception, and since 2026-09-01 that trip is DURABLE** — it survives the
restart and needs `POST /resume?confirm=yes`. A bound only delays that. Nothing
regressed by keeping the trip immediate; what changed is *what else* the trip took
with it.

**Why an immediate halt is defensible here and a bounded retry was right elsewhere.**
The codebase's settled split is *a failure to REACH the broker gets a bound; a broken
invariant halts immediately* — and every broker round trip on the check path already
swallows: `_refresh_working_orders` (52 `working_orders_unread` in one cert session),
`_recheck_residual_orders`, and `_cache_is_over_reporting` (every failure path returns
False). **The transient class those bounds were built for is already gone from this
path.** What is left is `AuditWriteError`, a local sqlite failure, or a genuine
defect. The exit path is the exception and retains real transient risk, which is
where the one observed instance in the system's life came from.

**The catch is scoped per POSITION as well as per pass.** `_scan.spreads` ordering is
stable, so a pass-level catch alone fails the same first position on every tick
forever — the 2026-09-03 harm reproduced by a different route, and the *routine* case
for a two-position, same-underlying book. The loop body became `_check_one`, and a
caught failure spends NOTHING: no order was sent, so the position keeps both
allowances and the trigger stays latched, the same rule `NOT_ATTEMPTED` follows. The
refresh has its own catch because `_last_refresh` is advanced *before* the await, so a
failed rebuild costs freshness for one interval and must not cost the evaluation pass.

**`AuditWriteError` is the one load-bearing exclusion and keeps `ALL` verbatim.** Same
shape as `_ask_guard`'s exclusion (2026-09-03) and for a stronger reason: by the time
it reaches this module, `AuditLog.on_failure` has already halted the process `ALL` and
set this very stop event. There is nothing left for a watchdog to protect, and
continuing would loop once more and exit on `while not self._stop.is_set()`. **The
non-negotiable — a failed audit write halts trading — is untouched.** No other
exception type is load-bearing anywhere in this change.

## Two hazards that exist ONLY because the watchdog now survives

Both were found in the design pass, both are closed in the same change, and **either
one alone would have made this fix worse than the defect.**

### A crashed exit task never spent the attempt

`_run_exit` wrote `self._attempted[key]` only on the normal return path. On the
`except` branch it halted and returned — `_attempted` unwritten, `_exiting[key]`
reaped on the next pass. Harmless while the process stopped. **Once the watchdog
survives, the next cadence tick re-evaluates the position, the trigger is still
sustained, and `_trigger` hands off a SECOND closing order** — for a position whose
first order may or may not have reached the broker, because an exception is precisely
the state in which we cannot know. That is the double-close `_attempted` exists to
prevent, and it is worse than any case the one-attempt rule was written for.

`_spend_on_crash` now spends it, **in memory first, before anything that can fail** —
the ordering rule `KillSwitch.engage` already follows, so a dead recorder cannot skip
the spend.

**The verdict is `NOT_RESOLVED` and no new `ExitOutcome` member was added.** The
question that enum answers for the retry machinery is *may this position get another
automated send?*, and for a crash the answer is the absolute NO that `NOT_RESOLVED`,
`BLOCKED` and `CANCELLED_ELSEWHERE` already carry. A new member would force a
classification decision in `_RETRYABLE_EXITS`, `outcome.closed` and every reader for a
distinction none of them acts on. **This is 2026-09-03's rule verbatim: a fourth
sentence, deliberately not a fourth outcome.**

**The SENTENCE is a new field, `crash_error`**, third beside `reject_reason` (an order
the broker accepted then rejected) and `mutation_error` (one it would not accept at
all). A crash is neither, and overloading either would break the AST rule that
*nothing decides on them* (2026-09-04). It rides `_SpentAttempt`, both spent pages,
and — deliberately — `/status`'s Exit-attempts row, because that row's whole job is
telling a human which *kind* of manual intervention this is, and `not_resolved` with
no broker reason anywhere is the reassuring-when-wrong reading that page has been
fixed for twice already.

**`_flatten_attempted` is NOT restored on a crash**, and the discard still has exactly
one site (the `NOT_ATTEMPTED` return). 2026-09-02's rule is exact: *a crash is an
EXCEPTION; it never reaches the restore. A crash must not be able to GRANT; only a
positive value may RESTORE.* So a crash during the ordinary attempt leaves the flatten
override available — at 15:30 that position still gets ONE further send, through
`DuplicateOrderGuard`, which re-asks the broker and permits only DEAD. **This design
needed no retry of its own because P2's override already is one, and it is
confirmation-gated.**

### An aborted pass never reaped

`self._reap_exits()` was the last statement of `check_once`. A pass that raised
anywhere above it — once, and then the process halted — left FINISHED exit tasks in
`_exiting`, and `check_once` skips any key in `_exiting` as "an exit is in flight".
Under the new posture that is a **permanent, silent** removal of that position from
the watch list, with `_impaired` popped every tick so even the evaluability row goes
quiet.

It is now in a `finally` covering the **whole** pass, refresh included. The design's
first cut wrapped the evaluation loop alone, which left the `AuditWriteError` refresh
path skipping the reap — exactly the harm the `finally` exists to close. **The test
written for the hazard is what caught the design's own sketch.**

## Visibility, and why the `/status` row is not optional

A watchdog that parks the process is loud. A watchdog that keeps running while failing
every tick is by construction **the quietest failure this system can produce**: `phase:
running`, sessions scheduled, the 15-second status line printing. After this change the
HALTED row reads `NO ENTRIES — watchdog still running`, which is the one sentence on
that page that can now be false.

So `/status` gained a **Watchdog** row, between "Open positions" and "Unwatched
positions" — that block is one escalating story about the same held spreads, and *is
the watcher itself alive?* is the step before all of them. Not above HALTED, which is
the stronger claim and owns the "changes the meaning of everything below" slot. Three
states:

- **WATCHING** (green) — N positions, last pass Ns ago;
- **DEGRADED** (red) — `where`, `error_type`, the error, how long it has been failing,
  how long since the last clean pass, the affected identifiers, and the sentence *"the
  rows below are as of the last clean pass"*;
- **RECOVERED** (amber) — N failures, the last one's type and age, and *"entries remain
  halted — POST /resume once resolved"*.

**The third state is why the row exists at all.** The halt is permanent and the failure
is not, so without it an operator sees a healthy watchdog beside a HALTED badge whose
reason string is the only surviving trace of why. Like the other five live reads, the
accessor is **not wrapped in a try/except**: a swallowing reader would render a genuine
bug as the calm WATCHING, which is the failure the row removes.

Records: `watchdog.degraded` (Tier 1, rising edge), `watchdog.degraded_still` (Tier 2),
`watchdog.recovered` (Tier 1, falling edge), `watchdog.exit_crashed` (Tier 1, one-shot
per position). `watchdog.fatal` survives for the `AuditWriteError` path. The repeat page
rides `_Escalation.overdue` on `_DEGRADED_PAGE_INTERVAL_S` (300s) — a **module
constant, not a config key**, because a new REQUIRED key forces a coupled config+image
deploy and this is a paging cadence, not a rail. Reusing
`watchdog.exit_unattempted_max_s` was rejected: it is the bound on *one position*
having no automated path to close, and widening a tuned number's meaning silently is
how it stops meaning anything.

## The risk this deliberately accepts

**A degraded watchdog keeps SENDING.** The one way a surviving watchdog can lose money
that a dead one cannot: if `open_spreads` fails repeatedly, `_scan` goes stale and the
watchdog could trigger an exit on a spread that has since closed, opening the inverted
vertical. Gating sends on scan freshness was **rejected** — the failure mode requires
the ledger *read* to keep failing while the audit log keeps *writing* (a read failure
that is really a sqlite failure becomes `AuditWriteError` and takes the exclusion
path), the harm it would prevent is bounded by the spread width and requires that
compounding, and the harm it would cause is certain and is this record's subject: a
0DTE position that cannot be flattened at 15:30 because a ledger read was flaking.
**Suppressing the send because a read failed is the same self-defeating move as halting
the watchdog because the watchdog failed.**

Recorded rather than silenced: `watchdog.exit_started` carries `scan_age_s` (seconds
since the last *successful* scan; `None` before the first), so the one number that
would evidence this is in the trail from day one. **Nothing decides on it.**

Two consequences to know rather than to fix:

- **A defect that fails every pass now runs to the close instead of parking the
  process.** That is the intent — entries halted, exits and the flatten still trying
  every tick — but the operator's cue is the `/status` row and the Slack page rather
  than a dead process.
- **Deciding whether to restart is now the operator's call.** A transient is already
  recovered and a restart buys nothing; a defect that repeats every tick repeats after
  the restart too. The one thing a restart does buy is a clean watchdog state, which
  matters when a position's exit attempt was spent by a crash. README §9 carries this.

## What was deliberately NOT changed

- **The one-attempt rule.** A crash spends; it does not retry. The confirmation-gated
  exit re-attempt is untouched and stays an open decision — this is not a step toward
  it.
- **`stop()` still sets the shared event.** It is the session's teardown path and the
  sharing is correct there.
- **The kill switch's blast radius.** Still entry-only, still not passed to
  `ExitExecutor`. Nothing here lets a halt cancel a closing order.
- **No new stop event and no new latch.** P3 rejected a second stop event and
  2026-09-01 rejected it again; this narrows an existing halt's grade instead.
- **`_refresh_working_orders` / `_recheck_residual_orders` keep swallowing.** They
  already answer this question correctly for their own reads and must not be folded
  into `_degrade`, which halts.
- **The AST alert sweep keeps its two fields.** Extending it to require `crash_error`
  would break `entry.py`'s sites, which splat a `WalkResult` — a crash is a
  watchdog-level fact, not a walk-level one. The crash page carries all three
  literally (`reject_reason: None`, `mutation_error: None`), which is the truthful
  answer and what the sweep enforces; `/status` carries it because the seam sweep
  forces it.

## What the build found that the design did not

Two of them correct the design's own §4.2, and **both were forced by the design's own
test table**, which is the outcome that table exists for:

1. **The `finally` had to cover the refresh too** — see above. T12 failed against the
   sketch.
2. **`_pass_failures` resets at the top of `check_once`, not in `run()`.** Every test
   in the table drives `check_once` directly, and a `run()`-owned reset makes each
   direct pass inherit the previous count: recovery unreachable, every clean pass
   re-halting. **The pass counter belongs where the pass begins.**

Shape deviations, none behavioural: `WatchdogHealth` carries `watching` and
`last_failure_for_s` beyond the design's field list (the cells need both, and the seam
sweep forces the twins to match); `scan_age_s` reaches the record through a new
`ExitExecutor.bind_scan_age` seam on the existing `bind_quotes` precedent, because the
record lives inside `ExitExecutor.close`; `_note_exit_crash`'s detail carries `"where"`
explicitly; and the status twin is imported **bare** in `session.py` with the source
type annotated through a module alias — `_target_keywords` requires a bare `ast.Name`
call and correctly failed on `status.WatchdogHealth(...)` on first run.

Two tests could not survive verbatim, each keeping its load-bearing assertion.
`a_dead_audit_log_still_halts` is adapted, not kept — `await watchdog.run()` now loops
forever on a generic failure, which *is* the change — and asserts the same thing: the
halt does not depend on the recorder. And **T3 drives the real `run()` task**, not
`check_once`, because a `return` re-added after the degrade is invisible to a
directly-driven pass; the loop itself has to be under test for that mutation to be
caught.

Mutations 1, 2, 5 and 8 were applied during the build and caught by their named tests;
the rest are covered structurally by the two sweeps.
