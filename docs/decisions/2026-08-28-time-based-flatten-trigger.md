# A clock is the third trigger, and it is the only one nothing can blind (2026-08-28)

*P2 from [the exit-attempt taxonomy pass](2026-08-28-exit-attempt-taxonomy-and-halt-scope.md),
the finding that had no mitigation at all. `TriggerReason` had two members and both
were price-based, on a product that settles in cash at a fixed instant. On 2026-08-28
the credit stop fired at 10:37, the exit was refused, and there was no second
mechanism — the position sat held and triggered for 76 minutes because the only thing
that could close it was the thing that had already failed.*

> Settled and BUILT. Adds a REQUIRED `[watchdog] flatten_before_close_s`, so the
> **config deploys WITH the image**.

## What this is, in one sentence

At a fixed instant before the day's close, every held spread becomes a trigger —
unconditionally, with no debounce, with no market input, and with one attempt of its
own for a position whose ordinary attempts are already spent.

## Why a clock and not a better price rule

The two existing triggers share a dependency the strategy cannot rely on at the one
moment it matters. `CREDIT_MULTIPLE` needs both legs priced. `UNDERLYING_NEAR_SHORT_STRIKE`
needs spot and a strike, which is why it exists — it survives an unpriceable option
market. Neither of them survives the case that actually costs money, which is not
"we could not SEE the loss" but **"we saw it, decided, and could not ACT"**. Both
triggers had already fired on 2026-08-28. The position was still held at 11:54.

A 0DTE vertical held into the last half hour is a different instrument from the one
that was sold at 10:00. Gamma goes vertical into a PM settlement: the same one-point
move in the index that was worth a few cents of the short leg's premium at 10:00 is
worth most of the width at 15:45, and the delta of a short strike the index is
sitting on goes from 0.15 to 0.5 to 1.0 inside minutes. The standard desk control is
a hard time-based flatten, commonly 15:30–15:45 ET, and the reason it is a *clock*
rather than a *price* is exactly the failure recorded above: **the price-based stop
may be unfillable precisely when it matters, and a control whose input is the same
market that has stopped working is not a second mechanism.**

The clock has no input. It cannot go stale, it cannot fail to resolve a symbol, and
it cannot be blinded by a broker outage. That is the whole of its value and it is
also, as it turns out, a structural hazard — see question 1's consequence below.

---

## Question 1 — half-days: minutes before the close, never a wall-clock time

**Settled: the key is `[watchdog] flatten_before_close_s`, an OFFSET, and there is no
absolute-time form.**

A literal 15:30 is wrong on a half-day and wrong in the unsafe direction: the market
closes at 13:00, so the flatten mark falls two and a half hours after settlement and
**the control never fires at all** on the two or three days a year it is most needed
(a compressed session with the same expiry). This is the same defect
[expired-position resolution](2026-08-12-expired-position-resolution.md) fixed on
2026-08-25 — *"never a literal 16:15, which is wrong on a half-day"* — and the same
fix applies, because the same object already knows the answer.

`MarketCalendar` reads `[market] half_days` / `half_day_close` and exposes
`close_time(d)`. It already anchors `settlement_mark(d)` on that rather than on a
literal, for this exact reason. So the flatten mark is a third method on the same
object, deriving from the same `close_time(d)`:

```
flatten_mark(d, before_close_s) = close_datetime(d) - before_close_s
```

16:00 − 1800s = **15:30 ET** on a normal session; 13:00 − 1800s = **12:30 ET** on a
half-day. Half-day correctness is a property of where the number is computed, not a
branch anyone has to remember.

**Why not both forms.** A config that accepted an absolute time *or* an offset would
have to define which wins, and the answer on a half-day is always the offset — so
the absolute form is a key whose only distinct behaviour is the wrong one. Offering
it would be offering a foot-gun and calling it flexibility.

**Where it is computed, and why not `session.py`.** The 2026-08-25 precedent built
its calendar input in ONE place (`session.session_settled`) because `reconcile.py`
deliberately does not import the calendar. That constraint does not exist here, and
the obvious transliteration is impossible anyway: `session.py` imports
`execution/watchdog.py`, so a `session.flatten_mark` the watchdog called would be an
import cycle. **The ONE place is the calendar itself** — `MarketCalendar.flatten_mark`,
sitting next to `settlement_mark`, which is a better home than either caller.

**And the input is not ambient, which is why it is not a required constructor
argument.** `session_settled` had to be threaded because it is a CLOCK READING the
caller takes, and two callers could straddle midnight. The flatten mark is fully
determined by two REQUIRED config sections (`[market]`, `[watchdog]`) plus the
watchdog's own `trading_date` seam, all of which it already holds. There is no way to
construct a `Config` without the key — the loader is the fail-closed rail here, not
the constructor — so the dangerous silent default the 2026-08-25 rule guards against
is unreachable. The watchdog builds the calendar once and computes the distance once
per `check_once` pass.

The one genuinely new seam is `now_et`, defaulting to `clock.et_now` — exchange time,
never host-local, the same discipline every other date question in this codebase
follows.

### The loader rail

`flatten_before_close_s` must be `> 0` (a flatten AT the close is not a flatten: the
session has ended, the watchdog is stopped, and the settlement print is already made)
and must be **strictly less than the SHORTEST session the calendar can produce** —
`half_day_close - session_open`, 12,000s against the current config's 1,800s. A
larger value would put the flatten mark before the open on a half-day, so every
position would be born triggered. It is a cross-section check, so it runs after
`[market]` is parsed rather than inside the `[watchdog]` block.

### What this deliberately does NOT fix

`signal.entry_latest` is an ABSOLUTE 15:00 ET. On a normal day that is 30 minutes
before the flatten and there is no interaction. On a half-day the session ends at
13:00, so entries run until the close and a spread opened at 12:45 is flattened at
12:46 — paying the spread twice for nothing. **That is a pre-existing half-day defect
in the entry window, not one this change introduces** (today `entry_latest` already
means "entries run to the close" on a half-day), and the principled fix is to make
`entry_latest` close-relative too, which is an entry-path strategy change and not
P2. Recorded in "Still open" below; two or three days a year.

---

## Question 2 — pricing: the flatten changes WHEN, never HOW

**Settled: the flatten hands off to `ExitExecutor.close` unchanged. It does not skip
the walk, it does not go straight to the cross, and above all it does not invent a
price.**

### It must not invent a price, and the tempting version is arithmetically wrong

The premise of this trigger is that the legs may be unpriceable, and the obvious move
is to construct a limit from something that survives a dead quote feed. Exactly one
such number exists: the **spread WIDTH**, which comes off the strikes, and the strikes
survive a stale quote by construction (`LegResolution` carries `identity` out of every
unpriced branch — the 2026-08-13 identity/price split). A limit at the width is
bounded, tick-alignable, and needs no market at all.

**It is also strictly dominated by doing nothing.** A vertical can never settle for
more than its width, so paying the width to close guarantees the maximum loss, while
holding to settlement realizes *at most* that and usually less. A width-priced
flatten is not a conservative exit; it is a certain worst case bought at the price of
an uncertain one. It is rejected on the arithmetic, not on taste, and it is written
down here because it is the first idea anyone will have.

So when the legs cannot be priced, the flatten does what a triggered price stop
already does: it takes the existing `_UNPRICEABLE` path, sends nothing, keeps its
attempt, retries every cadence tick, and pages on the repeating
`exit_unattempted_max_s` bound. What changes is the CONTENT of that page, not its
mechanism — it now carries `seconds_to_flatten`, because the operator's action
("leg out by hand") is time-boxed by the close and "triggered, unpriceable" reads
very differently with 25 minutes left than with 25 minutes to go.

**This is the honest limit of P2 and it should not be dressed up.** A flatten that
cannot price its legs cannot send an order, and no design that respects "no
broker-side stop-market orders" and "never an unbounded market order" can change
that. What the flatten guarantees is that the attempt is *made*, loudly and at a
known time — not that the market will take it.

### It must not skip the walk either

The record's question 2 asks whether the flatten should go straight to the
market-crossing limit. No, for three reasons:

1. **The walk already ends there.** `WALKED_AWAY` → `_cross`, at the natural
   ask/bid, capped at the width. The escalation the question asks for is the one
   already built, already confirmation-gated, and already the only path that has
   been reasoned about. The flatten inherits it whole.
2. **There is no time pressure to buy.** `exit_cadence_s = 1.0`,
   `exit_step_ticks = 1`, `exit_walk_away_pct = 0.20`: the walk from mid to
   walk-away is a handful of COA cycles, seconds, against 1,800s of runway. Skipping
   it saves nothing measurable and forfeits every fill the auction would have paid
   for. The walk is not what made 2026-08-28 slow; a broker that answered nothing
   was.
3. **A direct cross would be a SECOND submission path on the exit side**, with its
   own guard semantics (`_cross` deliberately submits *unguarded*, because the walk
   already guarded and `_await_settled` already confirmed the abandoned order dead —
   neither of which is true with no walk in front of it). Building that to save five
   seconds is how the exit path acquires a second way to double-close.

The flatten's whole content is therefore **a new reason to fire**, plus the attempt
allowance in question 4. Nothing about pricing, construction, routing, the width cap,
or the crossing rail changes, and the `guard_first_submission=True` on the exit walk
is untouched — which is what makes question 4 safe.

---

## Question 3 — the debounce is bypassed, explicitly

**Settled: `FLATTEN_BEFORE_CLOSE` fires on the first observation. `SustainTracker`
still opens a window for it, and still never gates on it.**

`sustained_s` exists for one stated reason (spec §7): *"a 0DTE vertical's mid can
print through 2x credit on one wide quote and come straight back; acting on that
single observation converts a quote artifact into a realized loss."* Every word of
that is about a MARKET observation. A clock reading has no artifact to filter: it is
monotonic, it cannot come straight back, and a sustain requirement can only delay the
control by `sustained_s` and never improve it.

Leaving the debounce to apply "harmlessly" was rejected because it would be
**incidental rather than decided**. A five-second delay that nobody chose is a
five-second delay nobody will re-derive when `sustained_s` is next re-tuned — and the
tuning note in `argus.toml` already contemplates lengthening it. So the bypass is
explicit: `_INSTANT_TRIGGER_REASONS`, one member, read by `SustainTracker.observe`.

The window is still OPENED, deliberately, because `held_s` then measures **how long
the flatten has been due and the position is still held**, which is exactly the
number the repeating page and the trail want. Recording the window and gating on it
are separate; only the second one was ever argued for.

---

## Question 3b — the consequence nobody asked for: `blind` becomes unreachable

Not in the brief, found while writing the trigger, and it would have silently retired
a page.

```python
_ALL_TRIGGER_REASONS = frozenset(TriggerReason)
...
@property
def blind(self) -> bool:
    return self.unknown_reasons >= _ALL_TRIGGER_REASONS
```

`blind` means "NO trigger could be evaluated — nothing at all is watching this
position", and it is the one evaluability state that PAGES (`watchdog.unevaluable`;
`degraded` is Tier 2 and never wakes anyone). **The flatten trigger can never be
unevaluable** — it needs no quote, no chain lookup, no feed, and its inputs are a
config value and a calendar. So the moment it joins the enum, `unknown_reasons` can
never again cover `_ALL_TRIGGER_REASONS`, `blind` is `False` for every possible
input, and the page retires with no test failing.

The comment on that constant anticipated the opposite mistake:

> *a third trigger added without updating a literal here would make `blind`
> unreachable and silently retire the page*

— and the defence it prescribes (derive from the enum, never a literal) is precisely
what makes it unreachable in this case. The comment was right about the failure and
wrong about the direction.

**Fixed by splitting the set, not by exempting the trigger.**
`_MARKET_TRIGGER_REASONS = _ALL_TRIGGER_REASONS - {FLATTEN_BEFORE_CLOSE}` is what
`blind` and `ImpairedPosition.blind` compare against. The semantics are the ones the
page always had: *no MARKET-driven condition can be seen*. A position blind to both
price triggers at 11:00 is still an emergency, and the fact that a clock will fire on
it at 15:30 is not a substitute for watching it for the six hours in between.

Still derived, never a literal — a fourth, market-dependent trigger joins
`_MARKET_TRIGGER_REASONS` automatically. A second clock-shaped trigger would have to
be excluded by hand, which is the correct place for a human decision.

---

## Question 4 — the attempt allowance. The sharp one.

**Settled: a position whose ordinary exit attempts are spent gets exactly ONE further
send, and only after the flatten mark. It does not reopen the double-close hazard,
and the reason is `DuplicateOrderGuard`, not the passage of time.**

### The problem, stated precisely

P0 added `_EXIT_SEND_ATTEMPTS = 3`, but `_attempted` is unchanged for everything that
reached the broker: `check_once` evaluates such a position on every tick (P1) and
never reaches `_trigger`. So a spread that spent its attempts at 10:38 **cannot be
flattened at 15:30**. Without an allowance, P2 would be inert in exactly the incident
that motivated it — a control that fires only for positions that never needed it.

### Why the allowance is safe, exhaustively

The one-attempt rule's safety argument, verbatim from
`_page_if_spent_and_still_held`, is about an order that *may exist and cannot be
seen*: `_request_cancel` is best-effort, so a `NEVER_WORKED` exit leaves an order at
`Cancel Requested` that can still fill, and a second closing order on top of it closes
the spread twice — re-opening it inverted, unmanaged, on expiry day.

That argument is entirely about **whether a conflicting order exists at the broker
right now**. `_attempted` answers that question from in-process memory, once, and
never re-asks. `DuplicateOrderGuard` answers it by asking the broker, at the moment of
the send — and the exit walk already runs with `guard_first_submission=True`
specifically so that it does (finding 5 of the parent record: the correct protection
already exists and `_attempted` duplicates it badly).

The guard's classification is exhaustive, and every branch is the right answer for a
flatten:

| What the broker holds under `argus-close-<candidate>` | Guard | Right for the flatten? |
|---|---|---|
| `WORKING` | BLOCK | **Yes** — this is the double-close hazard, and it is refused. |
| `FILLED` | BLOCK | **Yes** — the exit already happened; a second send would re-open inverted. |
| unmappable | BLOCK | **Yes** — "the broker holds something under your key and Argus cannot say what" is `WORKING`-or-worse. |
| broker unreachable | RAISES | **Yes** — an unanswerable question is never permission; it halts the process. |
| `DEAD` (terminal, nothing filled) | ALLOW | **Yes, and this is the whole licence.** |

The last row is the point. `DEAD` is the state `_attempted` also blocks, and it is the
one state in which a second closing order is provably not a double close — nothing is
live to stack onto. **The flatten's allowance does not weaken any of the four
blocking branches; it removes a memory-based refusal in the one branch a live broker
read declares safe.** This is P0's licence applied one step further: *the guard is
the licence, never the outcome's own answer* — and here, never the outcome's own
memory either.

### Why one, and why only after the mark

- **One, not three.** `_EXIT_SEND_ATTEMPTS = 3` is a burst absorbing a transient
  ("the broker confirmed nothing landed, ask again"). The flatten is not a burst; it
  is a last action at a known time, and each send costs a real submission on a path
  already known to be failing. One send, one page.
- **Only after the mark.** The allowance is not granted by the flatten trigger
  *existing*; it is granted by the flatten trigger *firing*. Before 15:30 a spent
  position is exactly as spent as it was, and the one-attempt rule is untouched for
  the whole entry window and most of the session.
- **Spent at HANDOFF, not at report.** `_flatten_attempted` is marked immediately
  before the exit task is created — after the unpriceable early return, so a flatten
  that cannot build an order has not spent anything and retries every tick. Marking
  at handoff rather than on the report means a crash mid-exit cannot hand out a
  second one.
- **Never pruned**, for the same reason `_attempted` and `_sends` are not: a spread
  that flickers out of the ledger and back must not return with a fresh budget.
- **Tier 1 and paged.** Overriding a stated non-negotiable, even narrowly, must be
  in the trail as its own event and on a phone: `watchdog.flatten_override` /
  `watchdog_flatten_override`, once per position, carrying the outcome that had
  spent the attempt and how long ago.

### What was NOT done

- **`_attempted` was not deleted**, though finding 5 argues it is redundant with the
  guard. That is a bigger change than P2, it touches every spent outcome rather than
  one, and it is the open decision "should a confirmation-gated exit RE-ATTEMPT
  exist?" — whose recorded rule is *do not ship it as a uniform retry; the taxonomy
  is the decision*. This ships one narrowly-scoped, time-gated exception, which is
  the taxonomy answer for one cell.
- **The one-attempt rule is untouched before the mark**, and untouched entirely for
  any position that has already had its flatten send.

---

## Question 5 — ordering against everything that stops the watchdog

**Settled: the mark is 1,800s before the close, and it is comfortably ahead of every
SCHEDULED path. It is provably not ahead of the unscheduled ones, and that is stated
rather than papered over.**

The traced ordering, on a normal day:

| ET | What happens |
|---|---|
| 09:40 | `Scheduler._run_one_session` opens; watchdog starts inside `run_session`. |
| 10:00–15:00 | `signal.entry_earliest` .. `entry_latest`. |
| **15:30** | **flatten mark.** Trigger fires, exit walk hands off, ~seconds to the cross. |
| 16:00 | scheduler's `closer()` sets `session_stop`; `run_session`'s `finally` calls `watchdog.stop()`, which drains any exit in flight. |

Thirty minutes of runway, all of it inside the most liquid part of the SPX day, and
`watchdog.stop()` waits for a walk that is already running rather than cutting it off.

**Why 30 minutes and not 15.** Desk practice puts the flatten between 15:30 and
15:45; the earlier end is chosen deliberately, on three grounds that are all about
what happens when the flatten *fails*: an unpriceable flatten pages every
`exit_unattempted_max_s` (30s), so 15:30 gives ~60 escalations before the close
rather than ~30; the walk-plus-cross escalation needs room to run twice if the first
order has to be abandoned; and an operator who has to leg out by hand needs the
liquid window, not the last five minutes of it. The cost of being early is a few
basis points of theta forgone on a position the desk has already decided to be flat
in. It is a strategy-shaped number and it belongs to `trigger_source` like the other
three.

**Two paths reach the watchdog before 15:30 and neither is fixable here:**

- ~~**`_end_day`** (the entry path's soft abort) sets the session stop, which
  unwinds `run_session`'s `finally` and stops the watchdog — so a spread open when
  it fires loses its virtual stop AND its flatten, on expiry day.~~ **CLOSED
  2026-09-01**: the stand-down is now a session-scoped latch read only by the entry
  seam, so the flatten survives it. This paragraph is what the fix was argued from —
  P2 is precisely what raised the cost of the teardown, because before the flatten
  existed a surviving watchdog bought only a stop the position might never trigger.
  → [end-day keeps the watchdog](2026-09-01-end-day-keeps-the-watchdog.md)
- **A reconciliation halt at process start** (finding 7) parks before any session
  exists, so the watchdog never starts and the flatten never arms. **P2 does not
  close finding 7**, and the flatten is a further reason P3 matters: a control that
  only exists while a session is running inherits every way a session can fail to
  exist. **P3 closed it on 2026-08-29** for the narrow case that produced it: an
  order-level discrepancy with no position implication now halts ENTRIES and lets
  the session — and therefore this trigger — run.
  → [halt scope split](2026-08-29-halt-scope-split.md)

Both are arguments for P3, and both are recorded there rather than worked around
here.

---

## Question 6 — it survives a kill-switch trip, by construction

**Settled, and it required no new decision — only a check that the existing one still
holds.**

[Kill-switch scope](2026-08-03-kill-switch-scope.md) deliberately does not stop
exits, and `ExitExecutor._build_loop` passes **no `halt_check`** for exactly that
reason (a test pins the omission). The watchdog holds no kill-switch reference at
all. The flatten reaches the market through `ExitExecutor.close` unchanged, so it
inherits that property whole: a `/kill` at 15:00 stops new entries and does not stop
the 15:30 flatten — which is the moment it matters most, because an operator who has
just halted the system is very likely one who wants to be flat.

The distinction worth keeping straight, because it is easy to conflate: a `/kill`
trip engages the switch and does NOT request process stop, so exits (and the flatten)
run on. `_halt_process` — the internal pairing used for an audit-write failure, a
reconcile mismatch, or a watchdog exception — engages the switch AND sets `stop`, and
`stop` does end the watchdog. So an internal halt stops the flatten and the operator's
button does not. That asymmetry is the existing design, restated here because a
flatten is the first control for which someone will ask.

---

## What was built

`src/` plus one required config key. No new module, no new submission path, no change
to `build.py`, `loop.py`, `guard.py`, or `_cross`.

**`marketcalendar.py`** — `close_datetime(d)` (extracted; `settlement_mark` now
composes it) and `flatten_mark(d, before_close_s)`. THE one place the mark is built,
half-day-correct because `close_time(d)` is.

**`config.py`** — `WatchdogConfig.flatten_before_close_s`, required, `> 0`, and
refused at or above the shortest possible session (`half_day_close - session_open`).

**`execution/watchdog.py`**

- `TriggerReason.FLATTEN_BEFORE_CLOSE`, and `_MARKET_TRIGGER_REASONS` behind `blind`.
- `_INSTANT_TRIGGER_REASONS`, read by `SustainTracker.observe`: the window opens, the
  gate does not apply.
- `evaluate_triggers(..., *, seconds_to_flatten: float)` — required keyword-only, on
  the `session_settled` / `unmappable` / `today` precedent, because both directions of
  a default are silent. A pre-REDUCED distance rather than a clock or a calendar, so
  the evaluation stays pure: the flatten's threshold is an instant on the exchange
  calendar, and the calendar belongs to the watchdog.
- `TriggerEvaluation.seconds_to_flatten`, carried into `to_payload` so every
  `watchdog.check` tick records the runway.
- `PositionWatchdog`: a `now_et` seam (default `clock.et_now`), a `MarketCalendar`
  built once, `_seconds_to_flatten()` computed once per pass, and
  `_flatten_attempted` — the one-shot allowance, marked at handoff, never pruned.
- `check_once`: a spent position falls through to `_trigger` only when the flatten
  has fired and its allowance is unspent, and that fall-through writes
  `watchdog.flatten_override` (Tier 1) and pages.

**`status.py`** — comments only. `PositionEvaluability.blind`'s two docstrings said
"NO trigger evaluable", which is now wrong by one trigger; both say MARKET now. No
behaviour change: `/status` reads `ImpairedPosition.blind`, which was fixed at
source.

**`docs/architecture/04-uml-sequences.md`** — "two triggers" → three, and the
sequence note that `blind` spans the market triggers only.

## What the build found that the design did not

One thing, and it is the kind that goes stale silently rather than failing.

**`_SpentAttempt.since` now moves, and its docstring said it never could.** That
docstring — written for A2b — reads *"the attempt was spent once"*, and warns that the
defensive re-arm in `_page_if_spent_and_still_held` must never restart the duration
the page reports. The flatten allowance genuinely spends a SECOND attempt, so
`_run_exit` overwrites the record with the newer outcome and instant. That is the
truthful reading (the operator's "how long has this had no automated path to close"
should date from the newer attempt) and it is a different thing from the re-arm the
warning is about, which invents nothing. Both halves are now stated in the docstring
rather than one of them being quietly false — the same failure mode as C2's expired
comment and the `_open_spreads` sentence in the expired-position record.

## Deploying it

**Config-with-image, and the coupling is one-directional.**
`watchdog.flatten_before_close_s` is REQUIRED with no default, so a new image against
an old config refuses to boot with a `ConfigError` naming the key — loud, immediate,
and the fail-closed direction. The reverse is harmless: the loader reads named keys,
so the new config on an old image is simply ignored. If the two have to be sequenced,
config first.

## Testing

`tests/test_watchdog.py`, and the half-day case is a real test rather than an
assertion in a comment:

- **The calendar**: 15:30 on a normal close, **12:30 on a half-day**, and the offset
  applied to `close_time(d)` rather than to a literal — asserted through
  `MarketCalendar` directly and again end to end through the watchdog, where the SAME
  wall-clock instant (12:35 ET) flattens on a half-day and does not on a normal day.
  That pair is the test; either half alone proves nothing.
- **The loader** refuses `0`, refuses a value that would put the mark before a
  half-day's open, and accepts the production value.
- **The debounce is bypassed**: one observation fires the flatten where one
  observation never fires a credit stop, and `held_s` still measures the episode.
- **`blind` is still reachable**: both market triggers unknown while the flatten is
  evaluable is `blind=True`, and `FLATTEN_BEFORE_CLOSE` is asserted never to appear in
  `unknown_reasons` under any input.
- **The allowance**: a position spent at 10:38 is NOT triggered at 15:00 and IS
  triggered at 15:30; it gets exactly one send however many ticks pass; a flatten that
  cannot price its legs spends nothing and retries; and a position that was never
  spent takes the ordinary path with no override record.
- **The guard is still in front of it**: the flatten's send goes through
  `ExitExecutor.close` with `guard_first_submission=True` — asserted structurally, so
  a future refactor that routes the flatten around the guard fails a test rather than
  a position. On the AST rather than the text, because several docstrings correctly
  discuss the flag and counting those would assert nothing.

**Seven mutations, each failing at least one test**: the debounce gate applied to the
flatten; `blind` measured over the full enum; `flatten_mark` anchored on
`session_close` rather than `close_time(d)`; the allowance spent on the unpriceable
branch; the allowance not one-shot; no fall-through at all (P2 inert, the state this
exists to fix); and the mark exclusive rather than inclusive. Full suite green at
1,610.

## Still open after this

- **A flatten that cannot price its legs still sends nothing.** Bounded by the
  repeating page and by the close; there is no version of this that respects the
  no-market-orders rail and does better.
- ~~**Finding 7 is untouched.** No session, no watchdog, no flatten.~~ **Closed
  2026-08-29 (P3)** for an order-level halt carrying no position implication; a
  position-level mismatch still parks with no session, no watchdog and no flatten,
  and that is deliberate. → [halt scope split](2026-08-29-halt-scope-split.md)
- ~~**`_end_day` still stops the watchdog**, so it can still remove the flatten from
  a day it was meant to protect.~~ **CLOSED 2026-09-01.** → [end-day keeps the watchdog](2026-09-01-end-day-keeps-the-watchdog.md)
- **`signal.entry_latest` is absolute**, so on a half-day the entry window runs past
  the flatten mark. Two or three days a year; the fix belongs to the entry path.
- **`/status` does not name the flatten mark.** The fired trigger is visible
  everywhere a trigger is (`watchdog.triggered`, the page, the spent-exit row), but
  "the flatten is at 15:30 and it is 15:12" is not on the page. That belongs with the
  still-unbuilt `degraded_positions` proposal rather than as a fourth one-off row —
  the same call the A2b record deferred.
