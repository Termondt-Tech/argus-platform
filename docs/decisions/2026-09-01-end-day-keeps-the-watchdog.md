# `_end_day` ends the day, and the day includes the watchdog (2026-09-01)

**DESIGNED, SIGNED OFF AND BUILT the same day (2026-09-01).** It changes a
deliberate abort path and contradicts a sentence in that path's own docstring, so
the sign-off was asked for and given explicitly rather than routed around. The
design below is what was signed off; ["What the build found"](#what-the-build-found-that-the-design-did-not)
at the end records the one thing it got wrong.
→ design prompt: [`docs/tasks/argus_end_day_keeps_watchdog_prompt.md`](../tasks/argus_end_day_keeps_watchdog_prompt.md)

---

## What this is, in one sentence

`EntryExecutor._end_day` ends today's ENTRIES by setting the session's `stop`
event, and that event is also what stops the watchdog, the exit executor and the
exit walk — so a soft, entry-side stand-down on a transient cert blip silently
takes a held 0DTE spread's virtual stop, its mark and its 15:30 flatten with it,
for the rest of expiry day; the fix is to stop reaching the watchdog through an
event that means "the day is over" and to end entries directly instead.

## The defect, precisely

`_end_day` ([`entry.py:1124`](../../src/argus/execution/entry.py)) does its
recording, its alert, and then one line ([`entry.py:1164`](../../src/argus/execution/entry.py)):

```python
self._stop.set()
```

`self._stop` is the SESSION stop event. `_build_trading_side`
([`session.py:690`](../../src/argus/session.py)) hands the same object to three
consumers, and the scheduler hands it a fourth meaning:

| Consumer | What `stop` does there | Line |
|---|---|---|
| `PositionWatchdog` | its scan loop is `while not self._stop.is_set()` — triggers stop being evaluated, marks stop updating, `FLATTEN_BEFORE_CLOSE` never fires | [`watchdog.py:2058`](../../src/argus/execution/watchdog.py) |
| `ExitExecutor` → `PriceWalkingLoop` | `_stop_reason()` returns `"stop requested"`, the walk requests a cancel and returns `WalkOutcome.ABORTED` | [`loop.py:1380`](../../src/argus/execution/loop.py) |
| `run_session` body | the `while not stop.is_set()` journal loop exits and the `finally` tears down the watchdog, both streams and both broker sessions | [`session.py:1395`](../../src/argus/session.py), [`session.py:1437`](../../src/argus/session.py) |
| `SessionScheduler` | the day is done; it sleeps to the NEXT trading day's open and never re-enters today | [`scheduler.py:131`](../../src/argus/scheduler.py) |

So the blast radius is not "the session ends". It is three distinct harms, in
ascending order of severity:

1. **A held spread stops being watched.** No virtual stop, no mark, and — since
   P2 — no time-based flatten. The flatten is the one trigger nothing can blind,
   and it is the only mechanism that closes a winning 0DTE spread before
   settlement. Losing it is losing the day's designed exit.
2. **`/status` goes quiet about it.** `session_finished(ended=True)` clears the
   risk, link, evaluability and spent-exit sources, so a position that is still
   genuinely held renders as a session that ended normally.
3. **An exit already walking is cancelled AND spent.** If `_end_day` fires while
   the watchdog is mid-exit, the walk aborts with `ABORTED`, which
   `ExitExecutor` maps to `ExitOutcome.NOT_RESOLVED`
   ([`watchdog.py:1084`](../../src/argus/execution/watchdog.py)) — not in
   `_RETRYABLE_EXITS`, so the position's one exit attempt is spent. The
   sequence is: a working close is cancelled, the attempt is consumed, and the
   monitor that would have paged about it is torn down in the same instant.

Harm 3 is the sharpest and the prompt does not name it. It is not hypothetical
wiring: it is the documented, intended behaviour of the shared event
([`watchdog.py:3263`](../../src/argus/execution/watchdog.py) — "if the SAME
`stop` event is passed to this watchdog and to the exit walks … then setting it
here makes those walks return `ABORTED`. That is the intended behaviour"). It is
intended when the thing setting the event is the close or a SIGTERM. It is not
intended when the thing setting it is the duplicate-order guard failing a REST
call.

### The two call sites

- [`entry.py:518`](../../src/argus/execution/entry.py) —
  `_end_day("entry_build_failed", …)`. The `try` covers the kill-switch read,
  the `_attempted` membership test and `build_order`. Nothing in it touches the
  network; reaching it is a code defect.
- [`entry.py:982`](../../src/argus/execution/entry.py) — `_guard_unreachable`,
  after `execution.guard_unreachable_max_streak` spent bursts back to back
  across DISTINCT trade ideas. A sustained cert outage. This is the one that has
  cost trading days (2026-08-25, 10:00:00 ET, the day's first tick).

The second site is the one that fires in reality, and the condition that fires
it — the broker is unreachable — is uncorrelated with whether a spread is open.
2026-08-28's cert outage ran 76 minutes with a triggered spread held throughout.
Had the streak been spent that day, the position would have lost its watchdog
inside the exact window it needed one.

---

## Question 1 — does the signal loop keep spinning, or stop?

**Settled: it keeps spinning, untouched. `SignalLoop` is not modified at all.**

This is not the compromise option; under the mechanism chosen below it is the
option with no code in it. The loop keeps calling `evaluate_once`, the strategy
keeps proposing, `PreTradeGate` keeps deciding, and the entry seam declines.
Four reasons:

1. **It is shadow's proven path.** `submit=None` has run every shadow session
   and every P3 monitor-only day: full pipeline, nothing routed. Building a
   second "loop stopped but session alive" state is building a runtime shape
   nobody has exercised, on the day the broker is already misbehaving.
2. **The telemetry is the point of the day.** `signal.candidate`,
   `signal.none` and the regime block are the calibration dataset, and a day
   with a cert outage is a day you especially want the market record from. Half
   a day of it is a real loss; the loop is what produces it.
3. **Volume does not change.** On an ordinary day the loop already re-proposes
   every qualifying spread every `signal.cadence_s` and the entry path already
   declines them at `entry.already_attempted` (Tier 2). The steady state after a
   downgrade is the same shape with a different Tier 2 category. No new load, no
   new record class.
4. **Stopping it requires restructuring `run_session`** so "loop stopped" ≠
   "session ended" — a second teardown ordering to reason about, in the function
   whose `finally` ordering is already load-bearing (watchdog after loop, before
   the streams, so an in-flight exit keeps its quotes and its order feed).

---

## Question 2 — the mechanism

**Settled: a latch on `EntryExecutor` itself, read at the top of `submit`.
No new event, no `SignalLoop` change, no new wiring through
`_build_trading_side`, and nothing new handed to the watchdog.**

```python
@dataclass(frozen=True)
class EntriesEnded:
    """Why today's entries are over. Session-scoped; there is no clear() ."""
    kind: str            # entry_guard_unreachable | entry_build_failed
    candidate_id: str
    error_type: str
    since: datetime      # ET, from clock.et_now
```

`EntryExecutor` gains `self._entries_ended: EntriesEnded | None = None` and a
read-only property. `_end_day` sets it in place of `self._stop.set()`. `submit`
refuses on it, immediately after the kill-switch check and before the
`_attempted` test and `build_order`:

```python
if self._killswitch.engaged:
    ...                                    # unchanged, unmoved
if self._entries_ended is not None:
    self._audit.telemetry("entry.entries_ended_today", {...})
    return
```

### Why the latch is a dataclass and NOT an `asyncio.Event`

Deliberate, and it is the whole defence of this design against P3.

P3 ([halt scope split](2026-08-29-halt-scope-split.md), Question 1) considered
and **rejected** splitting `_stop` into `_stop_entries` / `_stop_session`, on
the grounds that "it multiplies states across five consumers, silently … a
second event means every one of those picks a side, and picking wrong fails
silently in exactly one direction — the one where something keeps running that
should have stopped." That reasoning stands and this proposal must not
re-introduce what it refused.

It does not, on three counts:

- **One reader.** The latch is passed to nobody. `PositionWatchdog`,
  `ExitExecutor`, `PriceWalkingLoop`, `SignalLoop` and `_interruptible_sleep`
  never see it and have no side to pick. The only code that reads it in the hot
  path is the method that would otherwise route the order.
- **Nothing can await it.** It is a frozen dataclass, not an Event, so there is
  no `.wait()` for a future consumer to grow. That is not stylistic: it is what
  structurally prevents this from drifting into the second stop event P3
  rejected. An AST test pins it.
- **It narrows the ACTION, not the EVENT.** P3's own conclusion was that "the
  narrowing belongs on the HALT, not on the EVENT" — it put a `grade` on the
  halt rather than a second event under it. This is the same move one level
  down: `_end_day` stops expressing "end entries" as "set the event that stops
  everything", and expresses it directly.

### Why not the two alternatives the prompt lists

- **A thin `submit` wrapper installed by `run_session`.** It protects exactly
  one wiring — the one in `run_session` — while the check inside `submit`
  protects every caller of `EntryExecutor.submit` that will ever exist, and it
  is the same placement the kill-switch refusal already uses two lines above.
  A wrapper is not more structural than the `if` it wraps; it is the same `if`
  moved somewhere with less context, plus a closure whose behaviour changes over
  time. Rejected as duplication that invites drift.
- **A `SignalLoop` method that clears its own `submit` seam.** Gives
  `SignalLoop` a mutable trading-posture state for one caller's benefit, and
  puts the refusal in the module whose docstring's first claim is that the loop
  body is identical for shadow and live. Rejected.

### What the latch does NOT do

It does not reset. It does not expire. There is no `clear()` and no condition
under which entries resume today — the guard is never re-asked, the streak is
never reset, `_attempted` claims stand. It evaporates because `EntryExecutor` is
constructed once per trading day inside `_build_trading_side`, so tomorrow's
session builds a new one with `None`. **The transience is structural, not a
reset call**, which is the property that makes "no durable state" checkable
rather than asserted.

### The rename

`_end_day` becomes `_end_entries_today`. The repo's own rule
([`session.py:1467`](../../src/argus/session.py)): "a function called
`run_shadow_session` doing that is exactly the drift CLAUDE.md exists to
prevent." A method that no longer ends the day must not be called `_end_day`.

**The audit category `entry.aborted_day` is NOT renamed.** It is a wire format
read by `tools.mark`, `tools.desk` and `test_shadow_marker`, and its `kind`,
`candidate_id`, `error_type` and `error` keys keep their exact meanings — the
same additive rule 2026-08-26 followed when it added `extra`. Method names are
documentation; category names are an interface. The asymmetry is on purpose and
is stated here so nobody "tidies" it later.

---

## Question 3 — which call sites

**Settled: both, with identical treatment. There is no per-site branch.**

Neither site has a reason to stop watching an open position, and a branch here
would create a second posture nobody has reasoned about, reachable only from a
code path that by definition only executes when something is already wrong. The
sites already differ where it matters — a distinct `kind` on the record and in
the alert, which is what tells an operator whether to look at cert or at a
stack trace.

One thing the `entry_build_failed` site deserves recorded, because it is the
strongest-looking argument against the change and it does not hold:

**If `build_order` is what raised, the exit path may share the defect** —
`watchdog.py:1048` builds its closing order with the same `build_order`. So the
watchdog we are keeping alive might also fail to construct an order. That is an
argument FOR keeping it, not against: a failing exit lands in the escalation
built for exactly that (`_attempted` spent → repeating `_SPENT` page on
`watchdog.exit_unattempted_max_s` → the flatten's one further send past the
mark), which is loud and visible on `/status`. Tearing the watchdog down
produces silence. **Trying and paging strictly dominates not trying**, and the
comparison is not close.

The noise profile is also unchanged: entries end on the FIRST occurrence, so a
deterministic build defect that would recur every tick hits the latch's Tier 2
refusal from the second tick onward — exactly as today's day-end suppressed it.

---

## Question 4 — no open position when it fires

**Settled: always downgrade. No flat-book branch.**

The branch would have to ask "is anything open?", and every available answer to
that question is a mirror of the broker rather than the broker:
`open_position_count` is a REST-seeded cache that trails a fill by ~4–5s
(2026-08-19), and `positions` is written only from AlertStreamer fill events. A
fill can be in flight at the instant `_end_day` fires — the entry walk that just
filled is the most likely single cause of a candidate list worth ending the day
over. **The failure mode of the branch is precisely the bug being fixed**: "we
thought there was nothing to watch."

The cost of being wrong in the other direction is a watchdog scanning an empty
list every `watchdog.refresh_interval_s` and a signal loop that was going to run
anyway. That is not a trade.

And a conditional day-end is the one thing `_end_day`'s docstring warned
against by name. Keeping it unconditional is what lets this proposal claim it
does not soften the path.

---

## Question 5 — the position resolves after the downgrade

**Settled: run to the real close, unconditionally. No early end.**

Same reasoning, plus two of its own: entries are already off, so the only thing
an early end saves is idle time; and an "everything resolved, end early" branch
would re-introduce the exact `stop`-event coupling being removed, in a new place
and with a new trigger. The session ends when the scheduler's closer sets
`session_stop` at the ET close, exactly as every other day. The scheduler's
"never re-enter a day already run" rule is untouched because the day is still
run exactly once and still ends at its own close.

---

## Questions 6 and 7 — `/status`, and the relationship to P3

**Settled: one seam, two states, never one flag. `/status` must answer "what
clears this", because that is the only actionable question.**

P3's monitor-only day and this one produce the SAME runtime posture — entries
off, watchdog and exits and flatten on — and they are otherwise opposites:

| | P3 `HaltGrade.ENTRIES` | this |
|---|---|---|
| Provenance | `HaltRecord` on the `Application` (process) | `EntriesEnded` on `EntryExecutor` (session) |
| Kill switch | engaged | **not engaged** |
| Durable | yes — and since 2026-09-01 it survives a restart | no — dies with the session object |
| Computed | once, at session build (`session.py:1269`) | mid-session |
| Cleared by | a human: `POST /resume` + restart | tomorrow's rollover, automatically |
| `/status` | HALTED row, `halt_grade="entries"` | its own ENTRIES-ENDED row |

They **share the runtime seam** (both end with the entry hook not routing) and
must **not share a flag**. A single `monitor_only: bool` on `/status` would
answer "are entries off?" and lose "what clears it" — and the operator reading
the page at 15:00 on expiry day already knows entries are off; what they need to
know is whether they have to do something tonight. Collapsing the two is the
same class of error as reading `breaker_tripped` off a `daily_loss.breach` row:
the page renders a true word and the operator draws a false conclusion.

They can co-occur, in one direction only. A P3 `ENTRIES` halt makes
`submit=None`, so `EntryExecutor.submit` is never called and `_end_entries_today`
is unreachable; the reverse — a transient downgrade followed by a halt — is
reachable and renders both rows, HALTED first because it is stronger.

### `SessionPhase` gets no new member

The phase is a lifecycle ladder (idle → logging_in → reconciling → running →
ended/aborted) and the session genuinely IS running. P3 faced the identical
question and answered it the same way: it added a row above the phase rather
than a phase. `_PHASE_CLASS`, the page title and the `<h1>` all key off the
phase; adding a member forks all three for a state that is a modifier on
"running", not a replacement for it.

### The rendering

New `StatusSnapshot` fields, populated from a session-published source exactly
as `evaluability_source` and `spent_exit_source` already are
(`run_session` passes `lambda: trading.entry.entries_ended`, inside the same
`if trading is not None` guard, so shadow gets `None` = "not applicable"):

```
entries_ended: bool
entries_ended_kind: str | None          # entry_guard_unreachable | entry_build_failed
entries_ended_candidate: str | None
entries_ended_error: str | None
entries_ended_since: str | None         # ISO, ET
```

HTML — one row, immediately after the HALTED row when both apply, otherwise
immediately after Phase, for the reason the HALTED row is placed there ("it
changes the meaning of everything below it"):

> **ENTRIES ENDED FOR TODAY — watchdog still running** *(warn badge)*
> `entry_guard_unreachable` (candidate=SPXW-…-PUT-5, since 11:42:07 ET) —
> MONITOR-ONLY for the rest of today: held positions are still watched, closed
> and flattened. **Clears automatically at the next trading day; no action
> required.**

The last sentence is the entire design. HALTED's equivalent clause reads
"restart required once resolved"; this one reads "clears automatically at the
next trading day". Badge severity is `warn`, not `bad` — the two words that
matter are already in the badge text, and reusing HALTED's red for a condition
that resolves itself overnight is how a red badge stops meaning anything.

JSON gets an `entries_ended` block of the same fields with `"clears":
"next_trading_day"` spelled out, so a script does not have to infer it from the
absence of `halted`.

`session_finished` clears the source with the others, on the precedent
`spent_exit_source` set verbatim: it is a handle into a per-session object, and
the EOD "what happened today" question is answered by the counts block and the
audit tail, which both survive.

---

## Question 8 — the reconciliation with "never soften it"

Both `_end_day`'s docstring ("an argument for reaching here rarely … never for
softening what happens once it is reached") and CLAUDE.md ("reach it rarely,
never soften it") say not to do this. Here is exactly what does and does not
change.

**Unchanged — every entry-side property the sentence was defending:**

- Entries end for today, on the first occurrence, immediately.
- No retry of the guard. The broker is never re-asked, in any form.
- The unreachable streak is not reset. `_attempted` claims stand.
- The candidate that triggered it is retired, as is every other.
- No condition resumes entries today. There is no clear, no expiry, no
  operator override.
- No kill switch, no `HaltRecord`, no `HaltGrade`, no durable state, no
  migration, no config key.
- `entry.aborted_day` still fires, Tier 1, with the same `kind` and the same
  four keys carrying the same meanings.
- Tomorrow starts clean, entries enabled, with no human step.
- The day-end is not made conditional on anything (Questions 4 and 5).

**Changed — one thing:** the session body does not tear down. `_end_day` stops
expressing "end today's entries" as "set the event that stops the watchdog, the
exit executor, the exit walk, both streams and both broker sessions."

The sentence was written about the ENTRY posture, and the entry posture is
untouched. The watchdog teardown was never the thing being defended: the
docstring names it as a **cost** ("This is not the only thing it costs") and
uses it to argue for reaching the path rarely. It is an artifact of the
mechanism — the shared `stop` event was the nearest available way to unwind a
session in 2026-07-30 — not a property anyone chose.

Two things postdate that sentence and change the arithmetic behind it:

- **P2, the time-based flatten (2026-08-28).** Before it, a watchdog running to
  the close bought a virtual stop that a given position might never trigger.
  Since it, the watchdog running is the difference between a 0DTE spread being
  closed at 15:30 and being held into PM settlement. On 2026-08-31 the flatten
  fired for the first time and fired on two positions. The cost of the teardown
  went up by a whole trigger, and the trigger it gained is the one that fires on
  a NORMAL day rather than a bad one.
- **P3, the halt scope split (2026-08-29).** "Entries off, watchdog on" stopped
  being a hypothetical runtime shape and became a built, tested, signed-off,
  deployed one. This proposal reaches the same posture; it does not invent it.

So the honest statement of what is being asked for is not a softening of
`_end_day` but a **narrowing of its blast radius to what it was always about**.
The thing it was built to stop — a broker Argus cannot see straight taking on
NEW risk — is stopped exactly as before, and more reliably, because it no longer
depends on an event that four other components read differently.

The docstring sentence and the CLAUDE.md line should be rewritten rather than
deleted, to say what still holds: reach it rarely, never soften the entry
stand-down, and never make it conditional.

---

## What changes, file by file

Small. Five files, no config key, no migration, no new dependency.

| File | Change |
|---|---|
| `execution/entry.py` | `EntriesEnded` dataclass; `self._entries_ended`; a read-only property; `_end_day` → `_end_entries_today`, setting the latch instead of `self._stop.set()` and writing the new Tier 1 record; the refusal at the top of `submit`; module-docstring update on the soft/hard boundary. |
| `session.py` | one line inside the existing `if trading is not None` guard, publishing `lambda: trading.entry.entries_ended` to `status.session_running`. |
| `status.py` | `SessionStatus` source + accessor (mirrors `spent_exit_source`), five `StatusSnapshot` fields, one HTML row, one JSON block, and the clear in `session_finished`. |
| `orchestrator.py` | **nothing.** |
| `execution/watchdog.py`, `execution/loop.py` | **nothing.** |

New audit categories:

- **`session.entries_ended`** — Tier 1, once, on the edge. `{trading_date,
  kind, candidate_id, error_type, entries: "ended", watchdog: "running",
  clears: "next_trading_day"}`. This is the record the prompt asks for: the
  "entries halted, watchdog still running" state made explicit rather than
  inferred from the absence of a `session.ended`.
  **Named `session.*` although it is written from `entry.py`**, deliberately,
  to pair with P3's `session.monitor_only`: the two are the complete answer to
  "why did this session route nothing", and splitting them across prefixes
  would hide one from the query that finds the other. The category names its
  subject, not its writer.
- **`entry.entries_ended_today`** — Tier 2, per declined tick, modelled on
  `entry.already_attempted` and for the same reason (a 5s cadence would
  out-volume every real decision at Tier 1).

### The alert changes its KIND, on purpose

Today `_end_day` alerts under `entry_guard_unreachable` / `entry_build_failed`,
and an operator's learned reading of that on a phone is "the day is over". It no
longer is. `_abandon_guard`'s docstring already settled the governing rule:
"A distinct `kind` from `_end_day`'s, on purpose: read on a phone, 'one spread
could not be checked' and 'the day is over' must not look alike." By that same
rule, "entries are done, the watchdog is still working" must not look like "the
day is over" either.

So the edge alert becomes **`entries_ended_today`**, carrying the original
`kind` in its detail. Nothing machine-readable moves — the audit record's `kind`
field is unchanged, and alert kinds are read by humans and by `SlackNotifier`'s
throttle only. `candidate_id` stays at the TOP level of the detail so B1's
`(kind, scope)` window still scopes it. `entry_guard_unreachable_abandoned`
keeps firing for every burst before the last, so the cause is still audible on
the way up.

## Consequences deliberately accepted

Recorded rather than discovered later:

1. **The session holds its broker sessions, streams and refresher open for the
   rest of the day, through the outage that ended entries.** So
   `account.refresh_failed` volume, `md` reconnect counts and the 15s journal
   line all keep flowing where today they stop. This is the posture a P3
   `ENTRIES` day already runs; `SlackNotifier`'s 900s coalescing already governs
   those kinds and nothing new is added to them. It is a real change in what an
   outage day looks like in the journal, and it is the price of the watchdog.
2. **Keeping the watchdog alive does not guarantee an exit.** On the
   `entry_guard_unreachable` path the trading API is by definition unreachable,
   and a closing order needs it. What is guaranteed is the ATTEMPT, the
   escalation and the page. Worth noting in its favour: **the mark rides a
   different session** — `SessionRole.MARKET_DATA`, `marketdata.source = "live"`
   — so on exactly this failure the triggers keep evaluating and `/status` keeps
   pricing the position while the trading side is down.
3. **The watchdog will poll `get_live_orders()` against an unreachable broker.**
   Already handled and already the right posture: that poll's failure is the one
   swallowed exception in the module (it only gates new entries), and the
   residual recheck's rule is "absence is not a verdict in either direction", so
   an unanswered poll cancels nothing.
4. **`test_only_the_watchdog_steps_the_recheck_so_end_day_stops_it`
   (`tests/test_residual_orders.py:1067`) keeps its assertion and loses its
   rationale.** The invariant — the residual recheck is stepped only from the
   watchdog's refresh — is unchanged and in fact strengthened; the docstring's
   reason ("`_end_day` stops the watchdog … and this goes the same way") becomes
   false and must be rewritten to name the session close instead. Flagged
   because a test whose stated reason has silently expired is how the
   `get_live_orders` comment failure happened (2026-08-20).

---

## Test / mutation list

Each must fail against the mutation named.

**The watchdog survives (the whole point)**

1. `_end_entries_today` fires with a spread open → the watchdog's scan loop runs
   another cycle, `evaluate_triggers` is called for that spread, and the mark
   updates. *Mutation: restore `self._stop.set()`.*
2. Same, with the clock advanced past `flatten_mark` → `FLATTEN_BEFORE_CLOSE`
   fires and `ExitExecutor.close` is called. *Mutation: as above.* This is the
   test that encodes the live-flip concern.
3. An exit walk in flight when it fires → the walk is NOT aborted, does not
   return `ABORTED`, and the position's exit attempt is not spent. *Mutation: as
   above.* Harm 3, which no existing test covers.
4. The session body does not tear down: the streams, the refresher and both
   broker sessions are still open on the next tick. *Mutation: as above.*

**Entries really do end**

5. After it fires, the next `submit` for a DIFFERENT qualifying candidate routes
   nothing — no `build_order`, no guard call, no walk — and writes
   `entry.entries_ended_today` at Tier 2. *Mutation: delete the refusal, or move
   it below `_attempted`/`build_order`.*
6. The guard is never re-asked and the streak is not reset. *Mutation: reset
   `_guard_unreachable_streak` in `_end_entries_today`.*
7. The refusal sits above the `_attempted` test and `build_order`, and below the
   kill-switch check — asserted on the AST, not by behaviour, so a reorder is
   caught even if both orders happen to pass today. *Mutation: swap the
   kill-switch and latch blocks.*

**No durable state**

8. It engages no kill switch, writes no `HaltRecord`, calls no `HaltFn`, and
   `halted_grade()` still reports `None`. *Mutation: route it through
   `_halt_process(grade=ENTRIES)`.*
9. `EntriesEnded` has no `.wait()`, no `clear()`, and `entry.py` contains no
   `self._stop.set()` anywhere — AST-asserted, both. *Mutation: make the latch
   an `asyncio.Event`.* This is the test that stops it drifting into P3's
   rejected second event.

**The day still ends, tomorrow is clean**

10. The scheduler's `session_stop` at the ET close still ends the session
    normally: phase `ended`, `finally` teardown in the existing order, exactly
    one session for that date. *Mutation: have `_end_entries_today` end the
    session early on a flat book (the Question 4 branch).*
11. The next trading day's session builds a fresh `EntryExecutor` with
    `entries_ended is None` and a live `submit`. *Mutation: hoist the latch to a
    class attribute or to the `Application`.*

**Both call sites**

12. `entry_build_failed` produces the identical posture with its own `kind` on
    the record and in the alert detail. *Mutation: branch on `kind` so one site
    still sets `stop`.*

**`/status`**

13. A transient downgrade renders the ENTRIES-ENDED row with "clears
    automatically at the next trading day", phase still `running`, `halted`
    false, and the JSON block present. *Mutation: derive the row from `halted`.*
14. A P3 `ENTRIES` halt and a transient downgrade render DIFFERENTLY, and both
    render when both apply. *Mutation: collapse them into one `monitor_only`
    flag.*
15. After the close, `session_finished` clears the source and the row
    disappears, while the counts block and the audit tail still show
    `entry.aborted_day`. *Mutation: keep the source past teardown.*

**Regression on the untouched interface**

16. `entry.aborted_day` keeps its `kind` / `candidate_id` / `error_type` /
    `error` values byte-for-byte, and `tools.mark` / `tools.desk` still read it.
    (`tests/test_entry.py:1228` already asserts this shape; it must keep
    passing unchanged.)

---

## The live-flip checklist line this closes

> **A soft, entry-side stand-down can no longer leave a held 0DTE spread
> unwatched.** `_end_entries_today` ends entries only; the watchdog, the exit
> executor and `FLATTEN_BEFORE_CLOSE` run to the real close on every path that
> does not halt the process.

It does not close, and does not touch:

- the daily max-loss breaker being structurally inert in sandbox (still a gate,
  still unprovable in cert);
- the shared live account (still a blocker);
- `OpenSpread.entry_credit` being the filled order's LIMIT (still deferred).

## Out of scope, and one note on reuse

Not proposed here: changing WHEN `_end_day` is reached (`guard_timeout_s`,
`guard_unreachable_max_streak`, the retry burst — v0.3.11's decision, not in
question); a confirmation-gated exit re-attempt; `/status` naming a residual
order.

**The seam is deliberately not general.** `EntriesEnded` has one writer, one
enforcement point and no accessor that could set it from outside
`EntryExecutor`. A future caller wanting a mid-session monitor-only downgrade
for a different reason should not reach for it: if the reason is durable and
needs a human, it is P3's `HaltGrade.ENTRIES`; if it is a third thing, it needs
its own design pass rather than a second writer on a latch whose whole safety
argument is that it has one.

---

## What the build found that the design did not

**One thing, and it was in the TESTS rather than in the design.** The two headline
tests — "a triggered spread is still exited" and "the flatten still fires at the
mark" — were first written the way every other watchdog test in the suite is
written: call `check_once()` in a `for` loop. Both passed. Both also passed with
the regression fully restored, because **the gate the old stand-down closed is
`while not self._stop.is_set()` in `run()`, and `check_once` never reads the
event at all.** They were tests of the wrong thing, and they were the two the
whole change exists for.

They now drive the watchdog's real `run()` loop on a 1ms cadence, and they fail
under the mutation. Worth carrying as a rule: **a test about an event must
exercise the thing that reads the event.** The suite's ordinary watchdog tests
call `check_once` directly on purpose — they are about evaluation, not about the
loop — so following the local convention was what produced the hole.

Otherwise the design held. Five files, no config key, no migration, no new
dependency; `orchestrator.py`, `watchdog.py` and `loop.py` untouched as specified.

### As built

- `execution/entry.py` — `EntriesEnded`; `EntryExecutor._entries_ended` and its
  read-only property; `_end_day` → `_end_entries_today`, setting the latch in
  place of `self._stop.set()` and writing `session.entries_ended`; the refusal at
  the top of `submit`. The latch is assigned **last, after both awaited writes** —
  a detail the design did not specify: if either record fails the write raises
  `AuditWriteError`, which halts trading through the audit log's own latch (a
  strictly stronger outcome), and setting the latch first would leave entries
  silently off with no Tier 1 row saying so.
- `session.py` — one closure inside the existing `if trading is not None` guard,
  converting to `status.EntriesEndedToday` the way the evaluability and spent-exit
  closures already convert (so `status.py` keeps its one-way dependency).
- `status.py` — `EntriesEndedToday`, the source/accessor pair, the snapshot field,
  the HTML row, the JSON block, the clear in `session_finished`.
- Doc-comment corrections in `config.py` (`guard_unreachable_max_streak`) and
  `docs/architecture/04-uml-sequences.md`, and expiry stamps on the five decision
  records that state the old behaviour as a standing rule.

### Tests

**21 new** in `tests/test_end_day_keeps_watchdog.py`, plus five existing
`test_entry.py` tests rewritten (they asserted `stop.is_set()`; they now assert
the latch AND that `stop` is clear) and `test_residual_orders.py`'s
`test_only_the_watchdog_steps_the_recheck_*` renamed — its assertion is unchanged
and strengthened, its stated reason had expired. Suite green at **1,809**.

Mutations verified to fail, one run each:

| Mutation | Fails |
|---|---|
| restore `self._stop.set()` | 6 tests, including both headline ones |
| delete the refusal in `submit` | 5 |
| reset the streak on the stand-down | 1 |
| make the latch a class attribute | 1 |

The AST tests (`_entries_ended` between the kill switch and `build_order`; no
`self._stop.set()` anywhere in `entry.py`; `EntriesEnded` has no `wait`/`set`/
`clear`/`is_set` and the property has no setter) are what pin the two properties
prose cannot: the ordering, and that this can never become the second stop event.

---

## The recommendation, as put to Richard

Take it, both call sites, as specified. The change is five files, no config key,
no migration, and it deletes a line rather than adding a subsystem. The property
it buys — a held 0DTE spread keeps its virtual stop, its mark and its 15:30
flatten regardless of what the entry path decides about today — is one CLAUDE.md
already states as a non-negotiable for every other path, and the only reason it
does not hold here is that a 2026-07-30 implementation detail reached the
watchdog through an event that four components read differently.

The sign-off being asked for is narrow and should be given explicitly, because
it re-makes a sentence that was deliberately written: **`_end_day`'s entry
posture is not being softened; its blast radius is being narrowed to entries.**
