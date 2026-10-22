# A halt has one blast radius and needs two (2026-08-29)

*P3 from [the exit-attempt taxonomy pass](2026-08-28-exit-attempt-taxonomy-and-halt-scope.md),
finding 7. On 2026-08-28 at 12:15 ET a process-start reconciliation found one
unfilled working order 30 points OTM — correctly — and parked the process. Two
live 0DTE spreads went from one-watched-one-spent to **neither watched**, three
and three-quarter hours from settlement, because a parked process runs no session
and the watchdog only exists inside one. The rail's verdict was right. Its
response was the whole system.*

> **SIGNED OFF and BUILT (2026-08-29).** The design below was written first and
> reviewed before any code, per the weekend P3–P6 authorization's explicit gate;
> Richard signed off on the grade-split design and implementation followed it.
> `src/` only — **no new config key**, so the image deploys alone. What changed
> against the design as written is in "What the build found that the design did
> not". **Correction (2026-08-29, later the same day): P3 IS part of v0.3.13.**
> Point 4 of "the recommendation, as signed off" below read the sign-off as also
> covering a release-scope call — hold this out of v0.3.13 for its own deploy day —
> that was never Richard's actual intent; he confirmed P0/P1/P2/P3/P4/P6 together
> were always meant to be v0.3.13's scope. The technical recommendation (the grade
> split itself, automatic not operator-gated) is unchanged and stands as signed
> off; only the release-sequencing clause is corrected. See
> [v0.3.13's scope](../releases/v0.3.13.md#scope) for what actually ships.

## What this is, in one sentence

A reconciliation halt gains a **grade**, derived by a pure function from the
discrepancy set alone: everything halts the process as it does today, except an
order-level discrepancy carrying no position implication, which halts **entries**
and lets the day's session come up in a monitor-only mode where the watchdog runs
and nothing can enter.

---

## The correction that has to come first

**The brief's framing is wrong about mechanism, and the difference decides the
whole design.** The scope prompt proposes:

> the halt should stop `SignalLoop` (new entries) and leave `PositionWatchdog`
> untouched, i.e. **decouple `_halt_process`'s effect on the watchdog from its
> effect on entries** rather than inventing a third watchdog mode. Verify this
> framing against how `_halt_process` and `run_session`'s teardown are actually
> wired before committing to it.

Verified, and it does not hold for the incident. **A reconciliation halt never
stops a running watchdog, because at both reconcile call sites the watchdog does
not exist yet.**

- `app.Application._reconcile` runs inside `startup()`, before the scheduler has
  ever run. There is no session and no watchdog to stop.
- `session._reconcile_or_abort` runs from `run_session` long before the trading
  side exists: at the time of writing, `session.py:842` against
  `_build_trading_side` / `watchdog.start()` at `1106` / `1129` — after market
  data, after the account stream, after the chain. Two hundred and sixty lines and
  four network dependencies later. (Those line numbers moved when this shipped;
  the ORDER is the load-bearing part and it is unchanged.)

So the defect is not "the halt tears down a watchdog." It is **"the halt prevents
one from ever starting."** Decoupling a teardown fixes nothing here, because
there is no teardown on this path. What is needed is the ability to **build** a
session after a halt.

That is a materially larger change than the brief anticipates, and naming it is
the most useful thing this design does. Everything below follows from it.

For completeness, the halt paths where a running watchdog *is* torn down, and
what each deserves:

| Halt source | Watchdog running? | Should it survive? |
|---|---|---|
| Process-start reconcile mismatch | No — never started | **This is the finding.** |
| Per-session reconcile mismatch | No — halts 260 lines earlier | **This is the finding, in a costume.** |
| `_halt_on_audit_failure` | Yes | **No.** A dead recorder means no action may proceed; the watchdog's own `run()` would halt on its first `record()` anyway. Acting without a durable trail is the thing the non-negotiable forbids. |
| `PositionWatchdog._halt_on_exception` | It is the thing that died | Moot. |
| `entry.py`'s hard halt (past the submission boundary) | Yes | Arguable, and **out of scope** — see "What must NOT change". |
| `entry.py`'s `_end_day` (soft, no kill switch, no halt record) | Yes | Also out of scope, also real. Unchanged. |

Two of six are the finding, and both are "the watchdog never starts."

---

## What a halt actually does today

`_halt_process` is three statements, and they have four separable effects:

```python
await self._killswitch.engage(reason, source=source, detail=detail)
self._mark_halted(reason, source=source)
self.request_stop()                      # -> self._stop.set()
```

| # | Effect | Mechanism | Right for an order-only discrepancy? |
|---|---|---|---|
| a | New entries refused | `KillSwitch` → `PreTradeGate`, `EntryExecutor.submit`, the entry walk's `halt_check` | **Yes, always.** |
| b | No further sessions, ever | `run()`'s `if self._halted is None`, and `_run_session_day`'s outright refusal | No |
| c | The CURRENT session ends | `_stop` → `scheduler.stop_bridge` → `session_stop` → `run_session`'s `finally` → `watchdog.stop()` | No |
| d | The process parks rather than exits | `_await_exit_while_halted` | **Yes, always.** (2026-08-17) |

(a) and (d) are correct for every halt there is. (b) and (c) are the blast
radius, and they are welded to (a) by one `Event`.

**The kill switch is not the problem and does not need touching.** It owns no
`asyncio.Event` — a test asserts that — and its scope
([2026-08-03](2026-08-03-kill-switch-scope.md)) deliberately excludes exits.
`_halt_process`'s extra reach comes entirely from `request_stop()`, i.e. from the
application's `_stop`. The record's "in tension with the kill-switch decision" is
accurate as an observation and misleading as a diagnosis: the two mechanisms are
already separate, and the internal one is simply wider.

---

## Question 1 — should `_halt_process` stop using the same event the kill switch does?

**Settled: it already does not, and no new event should be introduced. The
narrowing belongs on the HALT, not on the EVENT.**

This is the crux the brief asks for most words on, so here is the alternative
stated fairly before it is rejected.

### The rejected design: split `_stop` into `_stop_entries` and `_stop_session`

`_halt_process` would set only `_stop_entries`; the session would keep running,
and the watchdog with it. Rejected on three counts, in ascending order of
seriousness:

1. **It multiplies states across five consumers, silently.** `_stop` is read by
   `SessionScheduler.run`, bridged into `session_stop`, and `session_stop` is
   then handed to `EntryExecutor`, `ExitExecutor`, `PositionWatchdog`,
   `SignalLoop` and `_interruptible_sleep`. A second event means every one of
   those picks a side, and picking wrong fails silently in exactly one direction
   — the one where something keeps running that should have stopped. This repo
   has one event with one meaning ("stop TRADING") and a second one with a
   sharply different meaning ("leave the PROCESS"), and the clarity of that pair
   is load-bearing (2026-08-17). A third would not be a third meaning; it would
   be a second version of the first.

2. **"Do not stop the watchdog" is not a thing an event can express.** The
   watchdog reads its quotes from `LegQuotes`, which reads `DXLinkMarketData` and
   the chain source; it books fills through `FillConsumer` on the `AlertStreamer`;
   it submits through `ExecutionStack`; it writes to the audit log. All of those
   are torn down by `run_session`'s `finally`, in a fixed order chosen so an exit
   in flight keeps its quotes and its order feed to the end. Keeping the watchdog
   alive means keeping the session alive. So the decision is a **session-level**
   one, and pushing it down into an event puts it at the wrong altitude.

3. **It does not touch the incident at all.** No event is set at process start.
   `run()` never reaches the scheduler because `_halted` is not None, and
   `_run_session_day` refuses for the same reason. Both are `is None` checks on a
   latched record, not event reads. A narrower event would have changed nothing on
   2026-08-28.

### What is proposed instead

`_stop` keeps its exact current meaning. `_halt_process` gains a `grade`, and the
grade decides whether it calls `request_stop()` at all:

```python
async def _halt_process(self, reason, *, source, grade=HaltGrade.ALL, detail=None):
    await self._killswitch.engage(reason, source=source, detail=detail)
    self._mark_halted(reason, source=source, grade=grade)
    if grade is HaltGrade.ALL:
        self.request_stop()
```

`grade` **defaults to the strictest value**, so every existing caller — the audit
hook, the watchdog's exception path, `entry.py`, `BrokerUnreachable`,
`SessionAborted`, day-risk rehydration — keeps today's behaviour without being
edited, and a future caller that forgets the argument gets the safe answer. Only
the two reconciliation sites ever pass anything else, and only when a pure
classifier says so.

The two consumers of `_halted` then read the grade rather than `is not None`:

- `run()` enters the scheduler unless the grade is `ALL`.
- `_run_session_day` refuses outright unless the grade is `ALL`.

Nothing else in the halt machinery changes. One enum, one default, two comparisons.

---

## Question 2 — what counts as "no position implication"

**Settled: it is a property of the DISCREPANCY KIND and the order's own
identifier. It is never a symbol comparison, and every position discrepancy is
disqualifying.**

A pure function in `reconcile.py`, next to `mismatch_halts_process` and subject
to the same rule — one source of truth, both call sites:

```python
def halt_grade(result: ReconciliationResult) -> HaltGrade:
    """ENTRIES only when EVERY discrepancy is one of Argus's own working
    OPENING orders. ALL otherwise, including on an empty-but-not-ok result."""
```

Eligible for `ENTRIES` if and only if **all** of:

1. `result.discrepancies` is empty — **no position discrepancy of any kind**;
2. `result.order_discrepancies` is non-empty and every member's `kind` is
   `UNEXPECTED_WORKING_ORDER`;
3. every member's `external_identifier` parses via
   `build.parse_external_identifier` to `OrderDirection.OPEN`.

Anything else — including a `not ok` result with no discrepancies at all, which
should be impossible and therefore must fail closed — is `ALL`.

### Why every position discrepancy disqualifies

- **`missing_at_broker`** — Argus's ledger holds a leg the broker does not, and
  the expiry carve-out has already run and declined to explain it
  ([expired positions](2026-08-12-expired-position-resolution.md) is applied
  *inside* `reconcile_and_record`, between the compare and the record, so
  anything still standing survived it). The watchdog would then be watching a
  spread that is not there, and a triggered stop — or the 15:30 flatten — would
  send a *closing* order for a leg the account does not hold. That is not a
  close; it is an **opening** trade, unmanaged, on expiry day. This is the single
  worst outcome reachable from any version of this design, and the rule that
  prevents it is "a position discrepancy halts everything."
- **`quantity_mismatch`** — the ledger's contract count is wrong, so a close is
  over- or under-sized. An over-close inverts the position.
- **`unexpected_at_broker`** — the broker holds a leg Argus does not expect. It
  says nothing directly about the rows Argus *does* hold, which is what makes it
  tempting. It is still disqualifying, because the leg has to have come from
  somewhere: either a fill Argus never booked (so the ledger is behind reality
  and the next fill event may correct it in a way the watchdog is mid-flight
  through), or a third party trading the account. Neither is a state in which to
  start routing closing orders off a local table.

### Why `unexpected_filled_order` and `unmappable_broker_order` disqualify

- **`unexpected_filled_order`** is a position claim wearing an order's clothes.
  It usually shows up in the position half too, but not always — a fill that nets
  against an existing expectation, or a positions endpoint trailing the orders
  endpoint, hides it. Treating it as order-level would be trusting the position
  check to have caught something it demonstrably can miss.
- **`unmappable_broker_order`** is the one kind that answers *no* question. It
  exists precisely because "Argus cannot read this" is different from "Argus did
  not place this"
  ([unmappable orders](2026-08-20-unmappable-orders-reach-reconciliation.md)). It
  could be working, it could be filled. An unanswerable question is never
  permission — the same rule `DuplicateOrderGuard` and `_await_settled` already
  hold.

### Why the identifier test, and why it is the SAFE half of an open question

Condition (3) is not decoration. Two orders that are both
`unexpected_working_order` differ enormously:

- `argus-open-20260828-call-7765-7770` — Argus's own opening order, ledger row
  lost. This is the 2026-08-28 case verbatim. It can only ever ADD a position,
  and if it fills, `FillConsumer` books it (`is_argus_identifier` passes,
  `store._apply` upserts the order row and the position rows in one transaction)
  and the watchdog adopts it on the next `request_refresh`. The system's response
  to it filling is *correct by existing mechanism*.
- **A foreign identifier** means somebody is trading the account by hand. There is
  no bound on what they do next, and no basis for the claim "this carries no
  position implication."
- **`argus-close-<candidate>`** means an exit Argus placed and lost track of. If
  it is for a candidate the watchdog holds, `DuplicateOrderGuard` would block a
  second close on that identifier — but that is the guard doing its job on the
  one case that happens to be covered, not a reason to authorize the class. An
  unaccounted closing order is a position changing hands.

Note what condition (3) reads: **the identifier Argus minted, never the leg
actions the broker echoed.** That is deliberate, and it is the settled half of the
open decision *"where should an order's DIRECTION come from?"*
([relabelled closing legs](2026-08-25-relabelled-closing-legs-open.md)):
tastytrade relabels leg actions relative to the account's net position, so
`infer_direction` can call a closing order OPEN. `parse_external_identifier` is
pure, local, and immune to that — it reads a string Argus generated. A future fix
to `infer_direction` must not "unify" these two; this call site wants the minted
answer specifically.

### Why there is no symbol-overlap test, and why that is the point

The obvious-looking check — *does this order touch a strike a watched spread
holds?* — is **forbidden here**, and the reason is a standing landmine:

> **Spreads CHAIN, and nothing symbol-keyed can count them.** `signal.width = 5.0`
> against SPX's 5-point ladder makes consecutive candidates share a strike... a
> shared strike nets to ZERO in `positions` AND at the broker.
> → [overlapping verticals](2026-08-17-overlapping-verticals.md)

`SqlitePositionStore.open_positions()` reads `WHERE signed_quantity != 0`, so a
strike two chained spreads share is **absent from the expected map entirely**. A
symbol-overlap test would therefore return "no overlap" for precisely the strike
that two live spreads are both standing on. It has already cost live positions
with no stop evaluated; it must not be reintroduced as a safety check.

So the eligibility test never looks at strikes at all. It is strictly coarser
than a symbol test, and coarser is the correct direction: it can only refuse
things a symbol test would have allowed.

### Validated against the one real instance

The 2026-08-28 halt payload:

```json
{"broker_order_id": 1424563, "disposition": "working",
 "external_identifier": "argus-open-20260828-call-7765-7770",
 "kind": "unexpected_working_order", "status": "Received"}
```

`unexpected_working_order`, Argus's own identifier, `open`. Eligible under all
three conditions. That is one data point, not a validation set, and the rule is
written to be right rather than to fit it — but a rule that failed its only
observed instance would be worth noticing.

---

## Question 3 — the grade must be RE-DERIVED, never carried

**Settled: the latched `HaltRecord` carries a grade for reporting; the decision to
run a session is re-derived from a live broker answer every session, and the
grade may only ever tighten.**

A halt is process-lifetime. A discrepancy is not: the unaccounted order can fill,
be cancelled by a human, or expire. If the monitor-only session's licence came
from the latched record, a halt graded `ENTRIES` at 12:15 on Friday would still
be running sessions on Tuesday against a book that had moved three times.

So the per-session reconcile is **not skipped** in monitor-only mode. It runs in
full — including the sandbox-reset handling and expired-position resolution,
which are the mechanisms that stop a stale row being watched at all — and its
verdict re-decides the grade:

- verdict clean → still halted, still no entries (the halt is latched and there is
  deliberately no path back), but the session proceeds and the watchdog runs;
- verdict `ENTRIES` → proceed, as above;
- verdict `ALL` → `_halt_process(..., grade=ALL)`, which sets `_stop`, unwinds the
  session, exits the scheduler loop, and lands in the existing park. The process
  is then exactly as parked as it is today.

**Tightening is one-way, and the rule is the opposite of `HaltRecord`'s.**
`_mark_halted` latches the REASON first-writer-wins, because a cascade's first
cause is the useful one and a downstream symptom must not overwrite it
(2026-08-17). The GRADE needs strictest-wins: an `ENTRIES` halt followed by an
`ALL` halt is `ALL`, and never the reverse. Two fields on one record with
opposite update rules is the kind of thing a later tidy-up unifies by accident,
so it wants stating in the code rather than only here.

---

## Question 4 — what a monitor-only session actually is

**Settled: it is the existing session body with `submit=None`. There is no third
watchdog mode, no second pipeline, and no new submission path.**

The brief's instinct is right and is adopted: the watchdog's job on a healthy
position is unchanged. It should keep evaluating both price triggers, keep
producing the mark P1 added, keep firing the 15:30 flatten, and keep routing exits
through `ExitExecutor.close` with `guard_first_submission=True`. A "read-and-exit
only" mode would be a second code path through the exit logic with no behavioural
difference, which is how a second way to double-close gets built.

What changes is one line: `submit` stays `None`, exactly as it is in shadow, so
`SignalLoop` records `shadow.would_submit` and routes nothing. The
`trades_for_real` boundary is untouched — this is still sandbox/live, and the
watchdog still submits closing orders inside it. Only the ENTRY hook is unwired.

**Belt and braces, deliberately.** The kill switch is engaged, so `PreTradeGate`
already refuses every candidate and `EntryExecutor.submit`'s pre-build re-check
already refuses anything that slipped through. `submit=None` adds a *structural*
refusal on top of two behavioural ones, because "nothing submits an order outside
`pipeline.trades_for_real`" is asserted by an AST test rather than trusted, and
this deserves the same posture.

`TradingSide`'s four members still travel together. Its docstring warns about the
watchdog built WITHOUT the fill consumer — a watchdog watching a table nobody
writes. That combination is not what this creates: fills and the watchdog are both
built, and it is the entry executor whose output goes nowhere. `EntryExecutor` is
still constructed (it is cheap, it holds the `on_filled` hook the watchdog wants,
and not constructing it would fork `_build_trading_side`); its `submit` is simply
not handed to the loop.

### What the incident would have looked like

At 12:15 on 2026-08-28, with this built: the process starts, reconcile finds
1424563, grades `ENTRIES`, engages the kill switch, records and pages the halt,
and enters the scheduler. The 12:15 session comes up, re-reconciles to the same
verdict, and starts the watchdog with `submit=None`. Both spreads are adopted.
`_attempted` is empty — it is a fresh process — so `call-7755-7760`'s spent exit
attempt is restored, which is **the exact thing the operator restarted to do**.
At 15:30 the flatten fires on whatever is still held.

Stated honestly: this restores the CONTROLS. It does not promise a fill. The
broker was 502-ing, and P2's own limit applies — an unpriceable flatten still
sends nothing. What it removes is the state where nothing was even trying.

---

## Question 5 — automatic, or an operator flag?

**Settled, and this is the recommendation I would most want overruled if any is:
automatic. The operator-gated variant is a strictly smaller first step and costs
nothing to upgrade to later, because the expensive half is identical.**

The alternative deserves a fair statement. A `--monitor-only` flag (or an env
var) would leave every automatic behaviour exactly as it is today: a halt parks,
full stop, and an operator who has read the alert restarts with the flag to get
the watchdog back. The human is the adjudicator the non-negotiable demands, no
classifier decides anything unattended, and the recovery in the 2026-08-28
runbook — *cancel the order, restart* — gains a second rung for when the cancel
fails.

Three arguments carry it the other way:

1. **P2 changed the stakes, and this is the sentence the brief asks for
   plainly.** The time-based flatten is the one control that needs no market, no
   quote, no feed and no broker cooperation — it exists *for* the case where
   everything else has stopped working. It lives on the watchdog. So a halt that
   prevents a session now removes the day's last-resort control, and making that
   control's existence conditional on an operator being awake, reachable, and
   knowing to pass a flag reintroduces exactly the dependency the flatten was
   built to remove.
2. **The window is expiry day.** A halt at 12:15 leaves 3h45m of accelerating
   gamma. The cost of the automatic version being wrong is bounded by the
   classifier's narrowness; the cost of the manual version being slow is
   unbounded and lands on a 0DTE vertical.
3. **The classifier is narrow enough to inspect.** Three conditions over an
   enumerated set of five discrepancy kinds, re-derived from a live broker answer
   every session, failing to `ALL` on anything unrecognized.

The reason to record this as the soft recommendation rather than the confident
one: every argument above is about *consequences*, and the argument against is
about *epistemics* — "Argus's model of reality is wrong somewhere" is the premise
of the whole state, and an automatic relaxation is a rail standing itself down.
That is the objection I cannot fully answer, only bound.

**The practical point for the sign-off: the decision is one line.** The
monitor-only session, the grade enum, the classifier and every test are identical
in both variants. Only the trigger differs — a classifier verdict, or a flag the
operator passes. It can be built as the manual version and promoted later without
rework, and that is a legitimate way to take this in two bites.

---

## What must NOT change

Stated the same way the P2 record states it, because these are the parts a later
refactor will read as incidental:

- **A position discrepancy halts everything.** Every kind, every environment
  where `mismatch_halts_process` is true, no exceptions, no carve-out. "Broker is
  the only source of truth" is a non-negotiable, not a cost to be traded against
  watchdog availability.
- **The environment boundary is untouched.** `mismatch_halts_process` still
  decides shadow-vs-rest; the grade decides scope *within* a halting environment.
  Two orthogonal questions, two functions, and shadow never reaches the new one
  (it wires no process-start reconciler and has no order state).
- **The kill switch is engaged in BOTH grades.** A halted process enters nothing,
  ever, regardless of grade. `KillSwitch` gains nothing and owns no event.
- **`_stop` keeps one meaning.** "Stop TRADING." No third event.
- **No auto-clear.** There is deliberately no path from `ENTRIES` back to
  un-halted without a restart. `/status` still reports HALTED, first and in red,
  and the word must not be softened to "degraded" — the process still requires a
  human, and the only thing that changed is what it does while waiting.
- **The default grade is `ALL`.** Every halt source that is not a reconciliation
  verdict gets it without being edited, and a new caller that forgets the argument
  gets it too.
- **Nothing cancels the unaccounted order.** That is P5, it is a new automated
  action on an order whose provenance is by construction unclear, and it must not
  be smuggled in here because the process happens to be running. The monitor
  session's licence is to watch what Argus knows, not to tidy what it does not.

---

## What the design found that the brief did not

Two things, both about the announcement rather than the halt. **Both are built** —
`_announce_halt` and `_repeat_halted_line`, split out of `_await_exit_while_halted`
— and both are covered by tests.

**1. `process.halted` and the `process_halted` alert would go missing.** Both are
written from exactly one place — `_await_exit_while_halted` — and that is
deliberate: it is the funnel every halting path converges on, and "exactly one" is
the property that stopped the 2026-08-14 storm (34 restarts, ~68 Slack messages).
A grade-`ENTRIES` halt **never reaches that function**, because `run()` goes into
the scheduler instead. So the halt would engage the kill switch, latch, and
suppress every entry — with no `process.halted` record and no page.

The fix is not to add a second announcement site. It is to lift the
record-and-alert out of `_await_exit_while_halted` into a helper called on the
halt EDGE (guarded by the same `_halted is None` latch that already makes
`_mark_halted` first-writer-wins), which both grades then reach exactly once.
`_await_exit_while_halted` keeps the parking loop and the periodic line; it stops
owning the announcement. That is a small edit and it is the one most likely to be
got wrong by building the grade first and noticing the gap in production.

**2. The 15-minute journal reminder has no home in the new grade.** It exists
because "a parked process is `active (running)` in `systemctl status`,
indistinguishable at a glance from a working one" (2026-08-17) — and a
monitor-only process is *worse* on that axis, because it genuinely is running
sessions, printing the 15-second status line, and looking busy. The reminder
should move out of the parking loop to a task that runs in both grades, and the
session's own periodic line should name the grade. This is the tradeoff that
record accepted knowingly, made one notch sharper.

---

## What was built

`src/` only. **No new config key** — the grade is derived, never configured, and a
config key here would be a switch that changes a safety rail's scope, which is the
`[regime]`-section landmine in a different costume.

**`reconcile.py`** — `HaltGrade` (`ALL` / `ENTRIES`) with `tighten`, and
`halt_grade(result)`, pure, sitting beside `mismatch_halts_process` for the same
reason it does: one source of truth for a boundary two call sites must not drift on.
It imports `parse_external_identifier` from `execution.build`, which pulls in no SDK
— the property this module's docstring protects is unchanged.

**`app.py`** — `HaltRecord.grade` (defaulting to `ALL`, so a record built
positionally by a test or a tool is the wide halt); `_halt_process(..., grade=
HaltGrade.ALL)` calling `request_stop()` for `ALL` alone; `_mark_halted` returning
whether it latched, keeping the first reason and taking the strictest grade;
`halt_grade()` as THE seam every "is this halted, and what does that stop?" question
reads; `run()` and `_run_session_day` gating on it; `_announce_halt` and
`_repeat_halted_line` split out of `_await_exit_while_halted`, which is now only a
wait.

**`session.py`** — `_reconcile_or_abort` returns `bool` (True = keep going,
monitor-only), refusing by grade and reading the decision back from the LATCHED
state; `HaltedGradeFn`, the late-bound seam, on the `halt_state` precedent;
`run_session` withholding `submit` whenever any halt is in force.

**`status.py`** — `StatusSnapshot.halt_grade`, in the JSON `halted` block and in the
HALTED row. The badge changes wording, never severity: `NO ENTRIES — watchdog still
running` and an explicit MONITOR-ONLY note, with "HALTED" and "restart required"
both intact. The grade is read with `getattr`, because `halt_state` is `Any`-typed
by design — `status.py` deliberately does not import `app` — so a test double
predating the field reads as `None` rather than raising.

**Records and alerts** — `process.halted` gains `"grade"`. A monitor-only day writes
Tier 1 `session.monitor_only` and pages `session_monitor_only`. That alert carries no
scope field, deliberately: B1's throttle keys on `(kind, alert_scope(detail))` and
`SCOPE_FIELDS` is `candidate_id`/`external_identifier` — this one is about the
PROCESS, not a position, so the kind's single unscoped window is the correct home
([alert throttle](2026-08-21-alert-throttle-is-per-position.md)).

## What the build found that the design did not

**One, and it would have shipped a hole that only the kill switch was covering.**

The design has the monitor-only mode as "a CONSEQUENCE of the grade, decided in one
pure function" — the per-session reconcile grades the verdict, and the session
continues or aborts on that. **That is insufficient when the verdict CHANGES.** A
process can start halted-`ENTRIES` at 09:38, an operator can cancel the unaccounted
order at 09:39, and the 09:40 session's own reconcile then comes back **clean** —
returning at `if result.ok` long before any grading happens. The session would have
wired `trading.entry.submit` on a permanently halted process.

Nothing would actually have been submitted: the kill switch is engaged on every halt,
so `PreTradeGate` refuses each candidate and `EntryExecutor.submit` re-checks. So this
is a defence-in-depth hole rather than a live hazard, and it is recorded as that
rather than dressed up. But the whole point of question 4's "belt and braces" is that
the structural refusal exists precisely so the behavioural ones are not the only thing
standing there.

Fixed by making the monitor-only decision the OR of two questions that are not the
same question: *did this verdict earn it* (the return value) and *is this process
halted at all* (the seam). Either alone withholds the hook. The seam is read live at
the decision point rather than captured at the top of the body, for the same reason
`StatusService.halt_state` is.

Two smaller things, both recorded because they go stale silently rather than failing:

- **The reminder task must be cancelled FIRST in `_teardown`.** It is a bare log
  loop with no resources, so nothing forces an ordering — and left running it keeps
  writing "still halted" through the shutdown it is describing.
- **`HaltGrade.tighten` is a method with one call site**, which normally argues for
  inlining it. It stays a method because the rule it encodes is the OPPOSITE of the
  one two lines above it in `_mark_halted`, and an inline `if` there reads as an
  arbitrary choice rather than as a stated invariant.

## How it was tested

`tests/test_halt_scope.py`, 35 tests. The shape the P2 record set: real behaviour end
to end, and a mutation for every decision.

- **The classifier, exhaustively**: every position kind alone and alongside an
  eligible order → `ALL`; `unexpected_filled_order` and `unmappable_broker_order` →
  `ALL`; `argus-close-`, a foreign identifier, an empty one and an unparseable
  direction → `ALL`; one ineligible order mixed into an eligible set → `ALL`;
  `ok=False` with an empty discrepancy set → `ALL`. And the 2026-08-28 order
  verbatim → `ENTRIES`.
- **The chained-strike case is a real test**, plus an AST assertion that
  `halt_grade`'s body reads no `.symbol`, `.legs` or `.strike` attribute at all — so
  a future "tightening" that adds a symbol comparison fails a test rather than a
  position. On the AST rather than the text, because the docstring correctly
  discusses symbols at length and counting those would assert nothing.
- **End to end at process start, as a PAIR**: an eligible verdict runs sessions,
  reports `halted: true` with `grade: entries` and renders MONITOR-ONLY; a verdict
  with one position discrepancy alongside the same eligible order runs NOTHING and
  parks. Either half alone proves nothing.
- **Escalation**: a monitor-only day that hits an audit-write halt sets `_stop`,
  runs no second day, and parks without exiting. And the reverse asserted
  impossible.
- **The announcement**: exactly one record and one alert in BOTH grades, and in the
  cascade — with the grade in the payload.
- **The entry hook is gated structurally**, asserted on the AST: `submit` must be
  assigned through a conditional naming `monitor_only`, so a mutation that wires it
  under a halt fails here rather than relying on the kill switch to catch it.
- **The per-session refusal through the REAL comparison**: a stub execution port
  returning one unaccounted working order, an empty ledger, a flat broker. The same
  order relabelled `argus-close-` aborts the day; a verdict that grades `ENTRIES`
  while an `ALL` halt is already latched also aborts, which is the only test that
  can tell the local grade from the latched one apart.

**Eleven mutations, each failing at least one test**: the default grade flipped to
`ENTRIES`; `request_stop()` for both grades (P3 inert); the position-discrepancy
condition dropped; the identifier-direction condition dropped; the decision read from
the local grade instead of the latched one; `tighten` allowed to relax; the entry
hook wired under a halt; the announcement dropped from the halt edge; the
announcement not guarded by the first-writer latch; `run()` gating on `is not None`;
`_run_session_day` refusing on any halt. Full suite green at **1,667**.

## Still open after this

- ~~**`_end_day` still stops the watchdog.**~~ **CLOSED 2026-09-01, and this
  record is where the answer came from.** It is not a halt — no kill switch, no
  `HaltRecord`, no grade — and it could fire at any hour, taking the virtual stop
  and the flatten with it. It now ends ENTRIES alone, through a session-scoped
  latch. **That is deliberately not the second stop event Question 1 rejected**:
  the latch has ONE reader (the entry seam), is handed to none of the five
  consumers that would have to pick a side, and is a frozen dataclass so nothing
  can await it. It narrows the ACTION, which is this record's own conclusion —
  "the narrowing belongs on the HALT, not on the EVENT" — applied one level down.
  → [end-day keeps the watchdog](2026-09-01-end-day-keeps-the-watchdog.md)
- **An audit-write halt still stops everything**, correctly. There is no version
  of "keep watching" that respects "everything writes to the audit log before the
  next action proceeds."
- **The unaccounted working order is still unmanaged.** P5.
- **A monitor-only process runs sessions indefinitely.** Every day, until a human
  restarts it — watching a book that expiry resolution will empty within a day, and
  looking healthier than a parked process does. Correct in that it still refuses
  to trade; worth naming as the visibility cost, since it is the 2026-08-17
  tradeoff taken one step further.
- **`/status` still does not name the flatten mark** (P2's own open item). That
  matters more in this mode than outside it, since the flatten may be the only
  control left and the page cannot say when it fires.
- **Nothing here has run against cert.** The classifier is exhaustively unit-tested
  and the wiring is covered end to end in-process, but the one thing sandbox could
  prove — a real `unexpected_working_order` surviving a failed cancel, then a
  restart coming up monitor-only and adopting the live spreads — has not been
  exercised. It is reachable in cert, unlike the daily-loss breaker.

---

## The recommendation, as signed off

1. **The grade split, with the classifier exactly as specified in question 2.** It
   is the only version of P3 that reaches the incident, and P2 made the cost of
   leaving it undone higher than it was when the finding was written.
2. **Automatic, not operator-gated** — with question 5's caveat standing: that is
   the softest call in this document, and the manual variant remains a legitimate
   fallback that costs nothing to switch to, since the expensive half is identical.
3. **`_end_day`, the audit halt and the entry-path hard halt are untouched.** The
   classifier's input is a reconciliation verdict; every other halt source keeps
   today's behaviour by default rather than by edit.
4. ~~Its own release and its own deploy day.~~ **Corrected: part of v0.3.13.**
   `src/`-only and no config coupling meant folding it in was free mechanically;
   the caution here was real (it changes what a stated non-negotiable does, on a
   boundary that has been got wrong once in each direction) but the call to hold
   it out for a separate deploy day was mine, not Richard's — his actual intent,
   confirmed 2026-08-29, was P0/P1/P2/P3/P4/P6 together as v0.3.13's scope.
   Preserved struck through because the caution itself is worth keeping visible:
   whoever cuts the v0.3.13 release should still read this file before doing so,
   the same as if it had shipped on its own.
