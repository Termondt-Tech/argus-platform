# The flatten's one-shot override is spent before the order can be built

**Status: SHIPPED 2026-09-02.** Approved with §3 (the repeating override page) included
in the same commit, as recommended. §4.5 (`UnpriceableSpreadError`) was approved
separately, on its explained cost, and shipped as a second commit — so **every
recommendation in this proposal was taken, and nothing in it was overruled.** Decision
record:
[the flatten's override is spent by a send](../decisions/2026-09-02-the-flatten-allowance-is-spent-by-a-send.md).

One outcome worth recording here, since this is the file that keeps what was rejected:
**the mutation this design proposed as §8.3 — "drop the latched-trigger behaviour" —
survived.** Clearing the sustain state after a `NOT_ATTEMPTED` changes nothing on this
path, because `FLATTEN_BEFORE_CLOSE` is in `_INSTANT_TRIGGER_REASONS` and re-fires on
one observation: there is no latch to drop for a clock, so the restored retry is not
dependent on the sustain window at all. Substituted with a mutation that latches
ELIGIBILITY instead of the send, which is killed by T1/T2/T3/T6.

Task prompt:
[`docs/tasks/argus_flatten_allowance_spent_early_prompt.md`](../tasks/argus_flatten_allowance_spent_early_prompt.md).
Background: [the second v0.3.13 day](../decisions/2026-09-01-second-live-fire-day.md)
(finding 1), and the trigger's own record,
[a clock is the third trigger](../decisions/2026-08-28-time-based-flatten-trigger.md).

> **Line numbers.** The prompt's citations are stale by roughly +135 lines — D3 and D5
> landed in `watchdog.py` on 2026-09-02 after it was written. Every line number below
> is against `7318180`.

---

## 1. Recommendation in one paragraph

**Keep the spend where it is — at the handoff, in `_trigger` — and give it back on the
one returned value that means the order was never sent.** `ExitOutcome.NOT_ATTEMPTED`
already carries exactly that meaning and is already the signal `_attempted` uses to
stay unspent; the flatten allowance simply does not read it. So the fix is to read the
same signal in the same place at the same time: `_run_exit`'s existing `NOT_ATTEMPTED`
branch does `self._flatten_attempted.discard(key)` alongside the `_attempted` it
already declines to write. One rule then governs both allowances — *nothing was sent,
so nothing was spent* — instead of two rules that disagree about the same failure.
**And the same change makes the `watchdog.flatten_override` page repeat, so it must
ship with the edge-guard that page never had** (§3, measured).

Rejected: moving the spend into `ExitExecutor.close` (§4.1, option A) and a
buildability pre-check at the handoff (option B). Not proposed: any change to what the
flatten does with an unpriceable spread (§4.3) — it keeps refusing.

---

## 2. What is broken, in source

The allowance is spent in `_trigger`, `watchdog.py:3044-3054`:

```python
market = evaluation.market
if TriggerReason.FLATTEN_BEFORE_CLOSE in fired:
    # Spend the flatten allowance HERE — at the handoff, and only once an
    # order can actually be built. ...
    self._flatten_attempted.add(key)
```

`market is not None` is the condition that has just been established (the early return
at `watchdog.py:3010`). That is **"the legs have quotes"**, not **"an order can be
built"**. Between the two sit `_build_exit_order` (`watchdog.py:1086-1129`) and
`build.py`'s `_limit_price` (`build.py:232-250`), which refuses a net mid that rounds
to 0.00 at the tick and comes back as `ExitOutcome.NOT_ATTEMPTED`
(`watchdog.py:1024-1039`).

`_run_exit` then reads that outcome (`watchdog.py:3079-3100`) and restores nothing:

```python
if report.outcome is ExitOutcome.NOT_ATTEMPTED:
    # Nothing was sent, so the position keeps its one attempt and the trigger
    # stays latched. ...
```

Both that comment and `close()`'s own docstring are true of `_attempted` — which is
written only in the `else` branch, `watchdog.py:3144` — and false of
`_flatten_attempted`, claimed several frames earlier and never given back.

**The comment at the spend site is right about what it wants and wrong about where it
is.** Its three clauses each survive this proposal intact (§5.2).

### 2.1 Reproduced

`tests/` harness, `RecordingExecutor` scripted to `NOT_ATTEMPTED`, a position whose
ordinary attempt is already spent, clock past the mark:

```
handoffs                = 2      (one to spend the attempt, one flatten look)
allowance spent         = {'argus-open-20260729-put-x'}
handoffs after the mid recovers = 2      <-- the send that should have happened
```

Four ticks of `NOT_ATTEMPTED`, then four ticks with a perfectly usable market: **no
further order is ever handed off.** That is the state the prompt describes — one
unpriceable look, then nothing for the rest of expiry day.

---

## 3. A second defect at the same site, and it is why this cannot ship alone

`_flatten_may_act` (`watchdog.py:2281-2342`) records `watchdog.flatten_override` at
Tier 1 and pages, documented as *"once per position"*. It is not once per position
today, because the page sits at the **eligibility** site and the spend sits past
`_trigger`'s `market is None` early return. A spent position, past the mark, whose legs
cannot be priced at all never reaches the spend — so the eligibility test is true again
on the next tick, and on every tick after it.

Measured on the same harness (8 ticks, legs aged past `market_data_max_age_s`):

```
tier-1 watchdog.flatten_override records = 8
watchdog_flatten_override alerts         = 8
handoffs                                 = 1
```

One Tier 1 crash-critical record per cadence tick, for a condition that can hold for
the rest of the session. `SlackNotifier`'s 900s per-`(kind, scope)` throttle keeps the
phone to ~1 per 15 minutes, so this was never going to be *seen* as an alert storm — it
is an **audit-trail** storm, and `_Escalation`'s own docstring names the cost: *"a Tier
1 record per tick is not more safety, it is a trail nobody can read (and, at a
sub-second cadence, sustained contention on the writer lock spec §6 protects)."*

On 2026-09-01 this did not fire, because both flattened positions were unpriceable at
the **build**, not at the **legs** — they got past `market is None`, spent the
allowance, and the spend is what silenced the repeat. **The bug in §2 was masking the
bug in §3.** Fixing §2 alone un-masks it and extends it from the rare
legs-cannot-be-priced case to the flatten's modal build-refused case. They ship
together or neither ships.

---

## 4. The design questions

### 4.1 Where should the spend go?

Three shapes, and they differ in what the state is when the exit task dies between the
spend and the report.

| | A — spend inside `close()` after the build succeeds | B — buildability pre-check at the handoff | **C — spend at the handoff, give it back on `NOT_ATTEMPTED`** |
|---|---|---|---|
| Precision on the observed case | exact | exact-ish (see below) | exact |
| Default if the task dies | spent | spent | **spent** |
| Who owns the latch | `ExitExecutor`, via a new callback | watchdog | **watchdog, as today** |
| Same signal as `_attempted`? | no — a second, different signal for the same question | no | **yes — the same report, the same branch** |
| Blast radius | new parameter on the exit path's public method | a second construction call | one `discard`, one branch |

**Option A** — pass an `on_committed` callable into `close()`, fired between the build
succeeding and `self._loop_factory().run(...)`. It is genuinely precise, and it is
still the wrong shape: it hands the watchdog's most load-bearing latch to a callback
fired from inside another class's task, so the write site disappears from the module
that owns the state. More decisively, **it re-creates the exact defect this fix is
about, mirrored.** The finding is "two allowances, one failure mode, opposite
treatment." The version that makes them one rule is the one where both read the *same
value* in the *same place*; A gives the flatten allowance a second, private signal that
`_attempted` does not share, and the next person to change one will again not know to
change the other.

**Option B** — a buildability probe at the handoff so spend and send become one
decision. They do not become one decision. The probe and the real build call
`build_order` with **two different `now` readings**, and `max_signal_age_s`
(`market_data_max_age_s`, 2.0s) is measured against `market.captured_at` — so a probe
that passes can be followed by a build that raises `StaleSignalError`, and the spend is
back to being wrong in exactly the way it is wrong now. Passing the built `Order`
through instead removes the duplication but moves construction and its freshness bound
out of the executor, past the `watchdog.exit_started` record, for one caller's latch.
Rejected on both branches.

**Option C, recommended.** The spend stays at the handoff — which is the fail-closed
default, and the property worth keeping is precisely that *the allowance is spent
unless something positively says otherwise*. `NOT_ATTEMPTED` is that something. It is
a returned value, it is constructed only at sites that provably precede any
submission, and it is already the signal that keeps `_attempted` unwritten.

The lifecycle in one sentence, which is the sentence the decision record will carry:

> **A position's one flatten override is spent when the closing order is handed to the
> walk, and it is given back only when the exit reports that no order was built —
> which is the only report that can be made without anything having been sent.**

### 4.2 If restoring: how is "nothing was sent" distinguished from "the task died"?

By the same value-versus-exception boundary the 2026-08-26 reconcile work leaned on,
and here it is **structural rather than conventional**: `NOT_ATTEMPTED` is a *returned
value*, a crash is an *exception*, and `_run_exit` reaches the restore only on the
return path. There are exactly two `ExitOutcome.NOT_ATTEMPTED` construction sites
(`watchdog.py:1035` and `watchdog.py:1046`) and **both are lexically above
`self._loop_factory().run(...)`** — the build refusal and the candidate-id mismatch.
Nothing that has touched the broker can produce this value. That is pinned by a test
(§7, T5) rather than by this paragraph.

**Is the distinction load-bearing here, or weaker because `_run_exit` catches
`BaseException` and halts?** It is load-bearing, and it is *reinforced* rather than
replaced by the halt. Three windows, none of which reaches a second override:

| How the exit task ends | `_flatten_attempted` | Can a later tick hand out a second? |
|---|---|---|
| Returns `NOT_ATTEMPTED` | **restored** | Yes — and that is the fix; nothing was sent |
| Returns anything else | spent | No — `_flatten_may_act` refuses |
| Raises (not `CancelledError`) | spent — the restore is on the return path and is not reached | No — `_halt_on_exception` (`watchdog.py:3388`) sets `_stop`, engages the kill switch and requests process stop; the loop ends |
| `CancelledError` | spent | No — nothing in the watchdog cancels an exit task (`stop()` *awaits*, `_drain_exits` *gathers*, `_reap_exits` only reaps), so this is loop teardown, i.e. the process is going away |

And a backstop that must be named but must **not** be the argument: `_flatten_attempted`
lives for one process and is never rehydrated, so a process that died mid-exit comes
back with `_attempted` empty too — the position has its ordinary attempt back and the
allowance is not what would authorize anything. **The design rests on the first,
structural ground.** Depending on the halt would make the restore correct by
coincidence of another module's policy, which is how the current comment's reasoning
came apart from its placement in the first place.

### 4.3 Does the flatten need its own answer to "the mid rounds to zero"?

**No.** CLAUDE.md, verbatim: *"it must never invent a price: the width is the one
number that survives a dead feed and paying it GUARANTEES max loss where holding
realizes at most it, so an unpriceable flatten still sends nothing."* Nothing here
touches that, and no part of this proposal needs it relaxed.

Worth stating positively, because "keep refusing" reads as inaction: **the system
already has the right answer to a zero mid, and 2026-09-01 is the evidence it works.**
A net mid that rounds below half a tick is the market saying the spread is worth
approximately nothing, which is the *winning* case; the escalation for "the auction
will not pay" is the width-capped market-crossing limit inside `close()`, and it is
reachable only *after* a build. On 2026-09-01 both positions were refused at the build,
looked again on a later tick when the mid was expressible, walked away, and **crossed
at −0.10 and filled**. Look-again-next-cycle is a working answer. The only thing that
was broken was the accounting that decides whether there is a next cycle.

### 4.4 Should `StaleSignalError` and `ValueError` be distinguished?

**For the allowance: no — deliberately, not by inheritance.** The allowance asks
exactly one question, *was anything sent*, and the answer is "no" for both. Giving one
of them a different lifetime would put a second rule back into the thing this change
exists to reduce to one rule. Same for the candidate-id mismatch `NOT_ATTEMPTED`, which
is a serious condition that also sent nothing: it is bounded by `_UNATTEMPTED`'s
`overdue` repeat, not by silently consuming the override.

**For the operator page: yes** — and that is question 4.5's answer, below. The two mean
different things and imply different next moves: a stale quote resolves itself and a
zero mid may not, and past the flatten mark a zero mid is a statement about the
*position* rather than about the *feed*.

### 4.5 What does the operator page say?

Today, `f"{type(exc).__name__}: {exc}"` — so an operator's phone reads
`ValueError: net mid 0.0 rounds to 0.00 ...` for what is a designed refusal. Proposed,
smallest version that makes the distinction structural rather than string-matched (this
repo already refuses to key behaviour on message text — see the open
consecutive-rejection decision, *"never by matching `reject_reason`'s text"*):

- `build.py` gains `class UnpriceableSpreadError(ValueError)`, raised by `_limit_price`
  for the rounds-to-zero case only. **A `ValueError` subclass, so every existing
  `except (StaleSignalError, ValueError)` catches it unchanged** — no call site moves.
- `close()`'s handler branches on the three for prose only, still returning one
  `NOT_ATTEMPTED`:
  - `UnpriceableSpreadError` → *"refused to price the exit: the spread's net mid rounds
    to 0.00 at the 0.05 tick, so there is no limit to send and neither the walk nor the
    flatten will invent one; nothing was sent, and the next tick looks again"*
  - `StaleSignalError` → *"refused to price the exit: quotes aged {age}s past the
    {bound}s bound between the trigger and the build; nothing was sent"*
  - any other `ValueError` → *"refused to build the exit order: {exc}"* — kept
    deliberately distinct, because a `_assert_credit_matches_legs` failure here is a
    defect signature and should not read like the routine refusal above it.

**One cross-path side effect, flagged for sign-off rather than buried.** `entry.py:588`
records `"reason": type(exc).__name__` in `entry.not_built`, so an entry-path zero mid
would record `UnpriceableSpreadError` instead of `ValueError`. That is strictly more
specific and the case is not reachable in practice (`signal.min_credit = 0.50`, per the
2026-08-31 ladder record), but it is a change to an existing record's value on a path
this task does not own. Cut it and the fix is unaffected — the allowance change stands
alone; only the page prose loses its discriminator.

**Explicitly not proposed:** a structured `ExitReport.refusal` field. It would be
better for the next pass's measurement than prose, and it is scope this task did not
ask for. Raise it as a follow-up if the prose proves hard to count.

### 4.6 Why `_flatten_attempted` is not cleared by the scan-forget path

Confirmed, and **unchanged by this proposal.** `watchdog.py:2714-2726` drops
`_sustain`, `_escalation`, `_impaired` and `_spent_marks` for a spread that leaves the
scan, and pointedly not `_attempted`, `_sends` or `_flatten_attempted`: *"those are what
stop a spread that flickers out and back from coming back with a fresh budget."* A
`positions` row can vanish and return for reasons that are not a closed position — a
partial close, a booking lag, a scan that could not attribute a leg — and a budget that
resets on that is a budget that resets on noise.

The restore proposed here is the opposite kind of event and does not weaken that rule:
it is keyed on a **positive report from the exit path** that no order was built, not on
the position's presence or absence in a scan. A test pins that the forget path still
does not touch it (§7, T7).

---

## 5. The change

### 5.1 `watchdog.py` — `_run_exit`, the `NOT_ATTEMPTED` branch (`3079`)

```python
if report.outcome is ExitOutcome.NOT_ATTEMPTED:
    # Nothing was sent, so the position keeps BOTH allowances and the trigger
    # stays latched. `_attempted` keeps its by not being written below; the
    # flatten's is given back here, because it is claimed at the handoff —
    # several frames before this — and this is the report that says the handoff
    # never became an order (2026-09-02).
    #
    # Gated on the same condition that spent it, so the pair reads as one rule
    # in one place. Safe on the return path specifically: `NOT_ATTEMPTED` is a
    # VALUE, constructed only at sites above the walk, so it cannot describe an
    # order that reached the broker — while a crash is an exception, never
    # reaches here, and leaves the allowance spent.
    if TriggerReason.FLATTEN_BEFORE_CLOSE in fired:
        self._flatten_attempted.discard(key)
    ...
```

The `fired` gate is defensive rather than load-bearing — every state in which an order
*was* sent under the allowance is one that cannot reach `_trigger` again — but it keeps
the spend and the un-spend readable as a single pairing on one condition.

### 5.2 `watchdog.py` — the spend site's comment (`3044`) and the field doc (`1918`)

No code change at the spend. The comment's three clauses all survive and the third
becomes true for the first time:

- *"spending it above would consume the position's last action on an order that was
  never sent"* — still the reason it is not at the `market is None` branch;
- *"spending it on the REPORT would let a crash mid-exit hand out a second one"* —
  still the reason the **spend** is not moved to `_run_exit`. Note the asymmetry that
  makes both true at once: a crash must not be able to *grant*, and only a positive
  value may *restore*;
- *"only once an order can actually be built"* — currently aspirational; the restore is
  what implements it, and the comment should say so rather than claim the placement
  does.

`_flatten_attempted`'s field doc gains the lifecycle sentence from §4.1.

### 5.3 `watchdog.py` — `_flatten_may_act`'s page becomes edge-triggered (`2317-2342`)

A new tag beside the existing family:

```python
#: A SPENT position, past the day's flatten mark, is eligible for its one override.
#: Tier 1 on the rising edge and never cleared: this is a "we have told you" edge, not
#: a window that re-arms — the override is granted once per position per process, and
#: an operator who has read it once does not need it again every cadence tick.
_FLATTEN_OVERRIDE = "flatten_override"
```

`_flatten_may_act` keeps its verdict and its payload; only the reporting changes —
`self._escalation.rising(key, _FLATTEN_OVERRIDE)` gates the Tier 1 record and the page,
with `watchdog.flatten_override_still` at Tier 2 on the repeats. **No `falling` call for
this tag anywhere**, which is what makes it once per position. The repeats' bound on
silence is already carried by `_UNATTEMPTED` / `_UNPRICEABLE` and their `overdue`
repeat, which is the correct owner: the interesting recurring sentence is *"still not
in the market"*, not *"still eligible"*.

The page's meaning is unchanged and remains the honest one: **the override is granted at
eligibility, not at the send.** With the restore in place a position can be handed off
more than once under one override — each handoff is a genuine attempt to build the
order, and at most one becomes an order — so pinning the page to a send would either
under-report (silent on the ticks that refused) or over-report (one page per refused
build, which is the storm §3 measures).

### 5.4 `build.py` — `UnpriceableSpreadError`, and `close()`'s prose

Per §4.5. `ValueError` subclass; no catch site changes.

---

## 6. What is deliberately NOT changed

- **The refusal to price an unpriceable spread.** No width fallback, no one-tick
  substitute, no flatten-specific pricing branch. §4.3.
- **The one-shot nature of the override.** One *send*, still. What is being fixed is
  that a look which never became a send was being counted as one. The enforcement is
  not intention: the restore has exactly one call site, inside the `NOT_ATTEMPTED`
  branch, and the mutation list (§8) includes moving it out.
- **`_attempted`'s restore-on-`NOT_ATTEMPTED`.** Untouched. It is correct today and is
  what kept 2026-09-01 harmless.
- **The one-attempt rule for anything that reached the broker.** Untouched.
- **The guarded walk and the width-capped cross.** `ExitExecutor.close`, its
  `guard_first_submission=True`, `_await_settled`'s positive confirmation scoped to
  `walk.broker_order_id`, and `_cross_limit`'s width cap are all unmodified. The
  flatten still changes WHEN, never HOW.
- **`DuplicateOrderGuard` as the licence.** The override is safe because the guard
  re-asks the broker at the moment of the send and permits only DEAD — not because time
  passed, and not because of anything in this change. Restoring an unspent allowance
  cannot weaken that: every send it permits still goes through the same guard.
- **`_ALL_TRIGGER_REASONS` / `_MARKET_TRIGGER_REASONS`.** The flatten stays out of
  `blind`'s reason set.
- **`_flatten_attempted`'s exclusion from the scan-forget path.** §4.6.
- **`_sends` / `_EXIT_SEND_ATTEMPTS`.** The P0 budget is a different mechanism for a
  different question and is not consulted or altered here.
- **`staleness.market_data_max_age_s`.** Not raised, not special-cased for the exit
  path, not read differently past the mark.

---

## 7. Tests

Deliverable 4's four, plus three the design added.

- **T1 — the reported defect.** Spent position, past the mark, exit returns
  `NOT_ATTEMPTED`: the allowance is **unspent** after the tick and the trigger stays
  latched. (Fails today: §2.1.)
- **T2 — spent once and only once.** Following T1, the mid becomes usable: exactly one
  further handoff, and no more however many ticks follow.
- **T3 — end to end, the 2026-09-01 shape.** A position whose ordinary attempts are
  already spent, unpriceable at the first flatten tick and for several after, **still
  gets its send when quotes return.** The regression this whole change is for.
- **T4 — a crash hands out no second override.** The exit task raises: the allowance
  stays spent, `_halt_on_exception` fires, and no later tick hands off again.
- **T5 — structural, `NOT_ATTEMPTED` cannot describe a sent order.** An AST assertion
  over `ExitExecutor.close` that every `ExitOutcome.NOT_ATTEMPTED` construction site
  precedes the `_loop_factory().run(` call. This is the premise the restore rests on
  (§4.2), so it is pinned rather than trusted — the same treatment the `reject_reason`
  rule got on 2026-09-02.
- **T6 — the override page is once per position** even when the legs stay unpriceable
  for many ticks, and even when several builds are refused. (Fails today: §3, 8 records
  for 8 ticks.)
- **T7 — the forget path still does not clear `_flatten_attempted`.** A spread that
  leaves the scan and returns does not come back with a fresh override.

Existing tests that must keep passing unchanged, because they are the properties this
must not disturb: `test_a_spent_position_is_not_flattened_before_the_mark`,
`test_a_spent_position_gets_one_flatten_send_past_the_mark`,
`test_a_flatten_that_cannot_price_its_legs_spends_nothing_and_retries`,
`test_a_position_flattened_on_the_ordinary_path_gets_no_second_send`.

## 8. Mutations to verify

Deliverable 5's three, plus two the design added. Each must break at least one named
test.

1. **Move the spend back to the handoff only** (i.e. drop the `discard`) → T1, T2, T3.
2. **Restore unconditionally rather than on `NOT_ATTEMPTED` only** — put the `discard`
   above the `if`, so every outcome restores → T2 (a `NOT_RESOLVED` exit is followed by
   a second flatten send).
3. **Drop the latched-trigger behaviour** — clear the sustained flatten reason after a
   `NOT_ATTEMPTED` → T3 (no send when quotes return).
4. **Restore on the exception path too** — `discard` in `_run_exit`'s `except` → T4.
5. **Drop the `rising` guard on the override page** → T6.

## 9. Deploy shape and risk

**`src/` only. No config key, so the image deploys alone** — it does not join the
coupled deploy `watchdog.exit_walk_min_steps` is waiting on, and it does not add to it.

The change can only ever *permit* a send that today does not happen, and only for a
position that is (a) held, (b) past the day's flatten mark, (c) already spent, and
(d) whose previous flatten look built no order. Every such send still goes out through
`DuplicateOrderGuard`, which permits only DEAD. The direction of the risk is therefore
"one more closing order on a spread nothing else will close", against today's "no
closing order at all", and the trap this could have fallen into — inventing a price to
get an order out — is untouched (§4.3).

Not fixed here, and named so it is not read as fixed: `_flatten_attempted` still does
not survive a restart (moot, since `_attempted` does not either); the flatten's own
`NOT_ATTEMPTED` still cannot distinguish "worthless" from "unpriceable" in anything but
prose; and the 571s an exit can spend unpriceable under
`staleness.market_data_max_age_s = 2.0` is unchanged and must not be fixed by raising
that key.

## 10. On sign-off

Ships as one commit, then a decision record dated 2026-09-02 in `docs/decisions/`, a
row in `docs/decisions/README.md`, a CLAUDE.md paragraph, this file marked SHIPPED with
its outcome, and item 5 of the 2026-09-01 pass record's "Recommended order" struck
through.

**Two things to confirm explicitly**, because they are the places where this proposal
does more than the prompt asked:

1. **§3, the repeating override page** — a second, measured defect at the same site,
   which the fix in §2 would otherwise un-mask and widen. Proposed to ship together.
2. **§4.5, `UnpriceableSpreadError`** — touches `build.py`, which is on the entry path,
   and changes one field's value in an entry-path audit record for a case that is not
   reachable there today. Cut it and the fix stands.
