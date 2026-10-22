# A cancel that was requested is not a cancel that landed (2026-08-29)

*P5 from [the exit-attempt taxonomy pass](2026-08-28-exit-attempt-taxonomy-and-halt-scope.md),
finding 8. At 10:10 ET on 2026-08-28 the walk decided order 1424563 must not
stand, asked the broker to cancel it, and the DELETE came back a 502. The walk
finished anyway — correctly, by design — and nothing in the process ever asked
about that order again. It was still `working` at 12:15 when a restart's
reconciliation found it and halted the process. Between those two clock readings
Argus had one live order at the exchange, at a price it had stopped standing
behind, that it believed it had cancelled.*

> **BUILT 2026-08-30, in `would_cancel` mode.** Written 2026-08-29 under the
> weekend P3–P6 authorization, whose gate for this item was explicit: *"hold off on
> writing the implementation until Richard has seen the design, even if that means
> it waits past this weekend."* He read and approved it 2026-08-30 and the
> implementation followed; everything below is as designed, with one clarification
> and one addition, both marked **AS BUILT** in place:
>
> - **the rollout stage is a config default, not a code fork** — recommendation 2's
>   `would_cancel` is what shipped, selected by `watchdog.unmanaged_order_cancel`
>   (optional, defaults `false`), so promoting sends the first real cancel without
>   a new image;
> - **registration is the mechanism rule, not the outcome list.** "The last
>   observation of this order was not terminal" is what decides, so a `FILLED` walk
>   whose PARTIAL fill left a still-working remainder does register — the
>   remainder's cancel is best-effort like every other, and a remainder that fills
>   is a bigger spread than the one the gate approved.
>
> The `/status` row under "Also proposed, and separable" is deliberately NOT built;
> it stays a named follow-up.

## What this is, in one sentence

Every walk that ends leaves at most one order behind; that order is registered
until the broker is seen to say it is terminal, and while it is registered and
positively confirmed still WORKING, an **opening** one gets its cancel re-requested
on a backing-off schedule and both kinds page on a repeating bound.

## Where it goes and why it is its own file

Standalone, not an addendum to the taxonomy record. That record is a multi-finding
incident write-up whose findings each point OUT to their own decision
([P2](2026-08-28-time-based-flatten-trigger.md),
[P3](2026-08-29-halt-scope-split.md),
[P4](2026-08-29-count-working-orders-toward-concurrency-cap.md)); folding the one
remaining finding back INTO it would make that file the only place with two shapes
in it. P5 also has to be readable by someone changing `loop.py` who has never heard
of 2026-08-28, which is what `docs/decisions/`'s "read the file that covers the
component" convention is for.

---

## The framing correction that has to come first

**Finding 8 is stated as a diagnosability problem and it is a broken safety rail.**
The finding says a failed cancel "left a working order live for two hours, and it is
what eventually halted the process" — which reads as: the cost was the halt. The halt
was P3's problem and P3 is built. The cost that remains is this, from `CLAUDE.md`'s
non-negotiables, first bullet:

> **Kill switch** reachable in one action, from a phone. Halts new entries
> immediately regardless of what any other task is doing, **including an entry order
> already working at the exchange.**

That guarantee is implemented by `HaltCheckFn` → `_abort` → `_request_cancel`, and
`_request_cancel` is best-effort with no follow-up. So a 502 on the DELETE means the
kill switch's stated behaviour silently did not happen, the operator is told nothing
beyond one `walk_cancel_failed` alert, and the only mechanism that would ever notice
is a restart. **On the one rail `CLAUDE.md` calls non-negotiable, "cancelled" is
today an intention, not an outcome.**

The same is true one level down of the walk's own contract. `loop.py`'s module
docstring, property 3:

> The result is a REPORT, not a guarantee. `cancel()` returning means the broker
> accepted a cancel REQUEST — the order goes to `Cancel Requested`, not gone.

Both halves of that sentence are honest and only the first half has a backstop. An
order whose cancel RAISED and an order whose cancel was ACCEPTED and never went
terminal are, from the outside, the same object: a live order nobody is walking. The
design below therefore triggers on the **state**, never on the error — which is the
same correction A3/A3b made to `_await_settled` and the same one the `_Escalation`
docstring makes about rate versus events.

Two consequences of that framing, stated up front because they are the parts most
likely to be argued with:

1. **This is not only about the 2026-08-28 failure mode.** A cancel that was accepted
   and stuck at `Cancel Requested` for an hour is in scope, and that condition sits
   on the open-questions list already (*"what does a real `Cancel Requested` take to
   go terminal?"* — chunk 4). This design does not answer that question; it stops the
   answer from mattering silently.
2. **The order is a residual of a decision already taken.** Nothing here decides to
   cancel anything. `_request_cancel` already decided, `order.cancel_attempt` already
   recorded it at Tier 1, and `walk.finished` already reported the outcome that
   depended on it. The recheck only asks whether the decision took effect.

---

## What happens today, exactly

`_request_cancel` (`loop.py:1035`) swallows every exception, logs, and fires the
alert `walk_cancel_failed`. Three things about the current state are worth having
straight before designing on top of it:

- **`state.cancel_requested` stays `False`**, so `walk.finished` carries
  `cancel_requested: false` — which is the durable trace, and it is a field nobody
  reads for anything.
- **The audit record is NOT missing.** `AuditedOrderExecution.cancel` writes
  `order.cancel_attempt` before the call and `order.cancel_failed` after a raise,
  both Tier 1. So the intent and its failure are already durable. What is missing is
  anything that acts on them. (The walk's own `_notify` writes no record — it is
  alert-only — but the layer underneath it covers the same event.)
- **Every `_request_cancel` is immediately followed by `_finish`.** That is stated in
  the code (`loop.py:906`, in service of a different conclusion) and it is true at all
  six call sites: `_await_in_market`'s never-worked path, the partial-fill remainder,
  `_check_drift`, `_reprice`'s walk-away, `_recover`'s duplicate branch, and `_abort`.
  **This property does the single most useful piece of work in the design** — see
  question 3.

And what happens to the order afterwards, if it fills: `FillConsumer` books it
(`is_argus_identifier` passes), `store._apply` upserts the order and position rows,
and the watchdog adopts the position on the next `request_refresh`. So an abandoned
opening order that fills does NOT produce an unwatched position. It produces a
position Argus decided not to take, with a virtual stop on it, counted against the
cap, at a price the walk had already rejected. That is a materially better outcome
than the worst case people reach for, and it is worth saying because it bounds how
much this design is allowed to spend on safety margin.

---

## Question 1 — is this the same decision as the exit RE-ATTEMPT open question?

**Settled: no, and the reason is the only axis that matters — what the action can do
at its worst. They should not share a mechanism and should not be decided together.**

The brief asks this directly, and `docs/decisions/README.md`'s open-decisions table
carries the row (*"should a confirmation-gated exit RE-ATTEMPT exist?"*). The two do
look alike: both are "notice a bad state after the fact, then act on it
automatically", both sit near a position that can double-close, and both are exactly
the shape `CLAUDE.md` cautions about with *"never fix an unwatched spent position by
retrying the exit."*

They differ in the worst case of the action:

| | exit re-attempt | cancel recheck |
|---|---|---|
| What is sent | a new **closing order** | a **cancel** for an order already sent |
| Worst case | a second close fills on top of the first → the position **inverts** on expiry day | the cancel crosses a fill (already possible today), or is rejected as a no-op |
| Can it create a position? | yes | **no** |
| Can it increase size? | yes | **no** |
| Is the decision new? | yes — "close this again" | no — "the close/cancel I already decided did not land" |

A cancel is monotone in the direction of less exposure. That is not a slogan; it is
the property the whole design rests on, and it is why the licence a re-attempt needs
(`DuplicateOrderGuard` re-asking the broker and permitting only DEAD) has no analogue
here: there is nothing to guard against duplicating. Sending a second cancel for an
order that is already dead is an error return. Sending a second cancel for an order
that is alive is what was wanted.

**One exception, and it is the whole reason this question is not trivial:** cancelling
a **closing** order is not monotone. It strands an open position — the exact harm
`loop.py`'s `HaltCheckFn` docstring names when it explains why the kill switch is
wired for entry walks only:

> A kill press must stop the system taking on NEW risk, so an entry walk is abandoned
> and its order cancelled. It must NOT strand an open position by cancelling the order
> that is closing it.

So the recheck inherits that asymmetry verbatim rather than inventing one. **Opening
orders get the automatic cancel; closing orders get the page and nothing else.** That
single rule keeps this design entirely out of the exit-re-attempt decision's
territory — it never sends anything on the exit path, and it never removes anything
the exit path sent.

The two decisions should stay separate in the record as well as in the code. The
re-attempt row in the open-decisions table is not advanced or narrowed by this.

---

## Question 2 — what is watched, and where the registry is populated

**Settled: every finished walk's residual order, registered at `_finish`, in process
memory only.**

### The candidate designs

**(a) Register at the audited layer's cancel failure.** `AuditedOrderExecution.cancel`
is the chokepoint every cancel in the process passes through, so hooking its `except`
branch covers every caller automatically — the same argument `ratelimit.py` makes for
being a decorator rather than a helper ("a helper the loop had to remember to call
would protect only the paths that remembered"). **Rejected as the registry, kept as
an argument.** It sees exactly one of the three ways an order is left working: the
cancel that raised. It cannot see the cancel that was accepted and never settled, and
it cannot see the walk that abandoned an order it could not even see
(`UNRESOLVED: order_not_visible_at_broker`, `loop.py:862`, which requests no cancel at
all). Building the narrow version first and widening later means changing where the
registry lives, which is the expensive half.

**(b) Register at `_finish`.** One funnel, already the single place every walk ends,
already holding `state.broker_order_id`, `state.sent` (with Argus's own minted
direction), `state.last` (the last observation), `state.cancel_requested`, and the
outcome. Registration is: *if the last observation of this order was not terminal,
this walk leaves a residual.* **Chosen.**

**(c) Derive the set from `get_live_orders()` alone — every Argus opening order that
is working and that no walk owns.** This is the honest invariant and it is the wider
target. **Rejected for now**, on two counts: "no walk owns it" requires either a
registry of in-flight walks (shared mutable state between `SignalLoop` and the
watchdog, which are independent by construction and whose independence `CLAUDE.md`
states as architecture) or a time heuristic; and it would act on orders no walk in
this process ever sent, which is a different and much larger claim. (c) is where this
goes if it is ever wanted; (b) is a strict subset of it and does not have to be
unbuilt first.

### What the registry holds

Per residual, keyed on `broker_order_id`:

- `external_identifier` — for the cross-check below, and for the alert scope B1's
  throttle reads;
- `direction`, taken from `state.sent.direction`, i.e. **the direction Argus minted
  when it built the order** (question 5);
- `candidate_id`;
- the walk outcome and whether a cancel was requested — for the record and the page,
  never for the decision;
- the clock reading at registration, so the settle allowance is measurable;
- attempt count and next-attempt time.

**In process memory, not durable, and deliberately.** A registry rehydrated from the
audit log would let a restart pick up the previous process's orphan and cancel it —
which sounds like the fix for 2026-08-28 and is the one automatic action this design
must not take. That order's provenance after a restart is exactly what reconciliation
exists to adjudicate, and P3's record says so in terms: *"Nothing cancels the
unaccounted order. That is P5, it is a new automated action on an order whose
provenance is by construction unclear, and it must not be smuggled in here."* The
same sentence binds P5. **P5 prevents 2026-08-28's state from arising; it does not
resolve it after a restart.** Say that plainly rather than letting the record imply
otherwise.

---

## Question 3 — what the recheck actually does

**Settled: it acts on a positively-confirmed WORKING sighting of the registered
`broker_order_id`, and on nothing else.**

Each poll, for each registered residual, look it up in the `LiveOrders` answer the
watchdog already fetched:

| What the broker's answer says about **this** order id | What happens |
|---|---|
| Named, terminal (`classify_order` → `DEAD`) | Resolved. Drop it, Tier 2 `order.residual_resolved`. |
| Named, `FILLED` | Resolved. Drop it. The fill books through `FillConsumer` exactly as it does today; nothing here touches positions. Tier 1, because an order the walk walked away from filling is a decision that got reversed by the market. |
| Named, `WORKING`, and registered longer ago than `watchdog.exit_cancel_settle_s` | **Act** (below). |
| Named, `WORKING`, inside the settle allowance | Wait. |
| **Absent** from the answer | **Not a verdict.** Keep watching, page distinguishably, act on nothing. |
| Present as an **unmappable** order under this identifier | Same as absent: cannot read, cannot conclude. Page, do not act. |

The absent row is A3/A3b applied symmetrically, and it is the row most likely to be
"tidied" later. `_await_settled`'s docstring already carries the argument at the one
call site where absence authorized a second closing order:

> Present-and-working, and absent from an answer of any size, both refuse.

Here the conclusion runs the other way — absence must not authorize a **cancel** —
but the rule is identical: **an answer that does not name this order is a read gap,
not a state.** The page distinguishes the two (`still_working` vs `not_visible`)
because the operator's next move differs: chase a live order, or chase a read.

### Acting, per direction

- **`OrderDirection.OPEN`** — re-request the cancel through the same
  `AuditedOrderExecution.cancel` every other cancel goes through (so
  `order.cancel_attempt` / `order.cancel_failed` / `order.cancel_requested` are
  written by the existing layer, at the existing tier, with no new record types), and
  escalate.
- **`OrderDirection.CLOSE`** — escalate only. Never cancel. Question 1's asymmetry.
- **Direction unknown or disagreeing with the identifier** — escalate only, and name
  the disagreement. Question 5.

### The settle allowance, and why it reuses an existing key

`watchdog.exit_cancel_settle_s` (3.0) is *how long to wait for a requested cancel to
be confirmed terminal before concluding it has not landed*. That is precisely the
quantity wanted here, it is the same physical thing on both paths, and it is the one
number the open question *"what does a real `Cancel Requested` take to go terminal?"*
is about. Reusing it means someone tuning it moves both — which is correct, because
both are estimates of the same broker behaviour, and it is the opposite of the
`market_data_max_age_s` landmine where one key governed two genuinely different
things (entry pricing and exit staleness).

It matters that the allowance is measured explicitly rather than assumed from the
cadence. The watchdog's refresh is normally 15s — five times the allowance — but
`request_refresh()` fires it early on any fill, so the first sighting of a
just-registered residual can be under a second old.

---

## Question 4 — where it runs

**Settled: driven by the watchdog's existing refresh, implemented in its own module.**

The order outlived the walk that abandoned it by two hours, so anything living inside
the walk is disqualified before it starts. The real choice is between a new task in
`session.py` and the watchdog's existing cadence.

**Chosen: the watchdog's cadence, a separate module.** `_refresh_if_due` already
fetches `LiveOrders` every `refresh_interval_s` for P4's working-order count
(`_refresh_working_orders`, built 2026-08-29). Reusing that fetch costs **zero
additional broker requests and zero new task lifecycle**, and the answer is already in
hand at exactly the moment it is wanted. A separate task would duplicate the poll at
the same cadence for no gain.

The policy does not belong *in* `watchdog.py`, which is a position-monitoring module
whose docstring earns its keep on that framing. It goes in its own module (working
name `execution/residual_orders.py`) with its own seams — audit, alert, clock, and a
cancel callable — and the watchdog contributes one line: `await
self._residuals.step(live)` inside `_refresh_if_due`, next to `_refresh_working_orders`.
That keeps the policy unit-testable with no watchdog at all, which is how P4's
`open_working_spread_equivalents` is testable and why it is a free function in its own
file.

**Failure posture: identical to `_refresh_working_orders`, and for the same stated
reason.** A raise inside the step must never halt the watchdog. That module's
docstring is already explicit that it is the one place in the file that swallows an
exception, because *"the count feeds ... NEW entries; position monitoring does not
read it and must not be taken down by a broker-read blip."* The same applies here with
more force: a cancel failing is this module's expected input, not an exception, and
taking the watchdog down over one would trade a live position's stop for an unfilled
order's tidiness.

**Two things it does not gate on:**

- **Not the kill switch.** Cancelling an abandoned opening order while halted is
  precisely what the operator asked for. A recheck disabled by a halt would be
  disabled exactly when it matters most.
- **Not `dry_run_submissions`.** Dry runs route nothing, so there is never a residual
  to register — the registry is empty by construction and no branch is needed. Worth a
  test rather than a branch.

**What it inherits, and this is the honest limitation:** it only runs while a session
runs. ~~`_end_day` stops the watchdog and takes this with it, the same way it takes
the virtual stop and the 15:30 flatten.~~ **Narrowed 2026-09-01**: the entry path's
stand-down takes none of the three any more — what ends the session is the close, a
SIGTERM or an `ALL`-graded halt. → [end-day keeps the watchdog](2026-09-01-end-day-keeps-the-watchdog.md)
Under a P3 `HaltGrade.ENTRIES` halt it keeps
running (the monitor-only session builds the watchdog) — which is correct, and note
again that the halting order itself is *not* in this process's registry, so there is
no interaction where P5 quietly cancels the thing P3 refused to.

---

## Question 5 — direction comes from the identifier, never from the broker

**Settled, and this is the finding in this design most likely to be got wrong by
someone implementing it from the summary.**

The recheck's one dangerous mistake is cancelling a CLOSING order it believed was an
opening one. `Order.direction` as it arrives from `get_live_orders()` is produced by
`infer_direction`, which reads the leg actions the broker echoed — and **tastytrade
labels each leg relative to the ACCOUNT's position, not to Argus's intent**
([broker relabels leg actions](2026-08-25-broker-relabels-leg-actions.md)). The open
decision [relabelled closing legs](2026-08-25-relabelled-closing-legs-open.md) records
the exact failing case: selling to close a strike the account is net flat on comes
back `Sell to Open`, and `infer_direction` calls a **closing** order OPEN. On a
chained book — routine at `signal.width = 5.0` — that is not a tail case.

So the recheck must never read `Order.direction`. Two sources that are immune:

1. `state.sent.direction` at registration — the direction Argus passed to
   `build_order`, before anything round-tripped through a broker. This is the primary.
2. `build.parse_external_identifier(identifier)` — pure, local, reading a string Argus
   minted. This is P3's `halt_grade` condition (3) and it is chosen there for exactly
   this reason.

**Use (1), cross-check against (2), and on disagreement refuse to cancel and page.**
Disagreement should be unreachable (both derive from the same construction), which is
what makes it a good tripwire for a future change that breaks the invariant.

Note the deliberate difference from P4. `working_orders.py` *does* read
`order.direction`, and its docstring says why that is safe there: a mislabelled
closing order inflates a count that blocks an entry, which is the fail-closed
direction. **Here the same mistake cancels a live close.** Same field, opposite
consequence — so a later "consistency" pass that unifies the two would be a
regression, and the module comment should say so.

**Second tripwire, same shape:** act only when the broker's answer names an order
whose `broker_order_id` **and** `external_identifier` both match the registration.
More than one match for an id is a broker reusing ids, which `_await_settled` already
treats as fail-closed rather than picking one; this does the same.

---

## Question 6 — the bounds, and the one new config key

**Settled: bound the RATE, not the COUNT — the opposite of P0's budget, for a stated
reason.**

P0 (2026-08-28) capped its exit retry at `_EXIT_SEND_ATTEMPTS = 3`, a constant, and
its record explains why a hard count rather than a rate: *"each retry costs a
SUBMISSION"*, and an unbounded loop against a broker failing every send would have
been ~1,500 order requests in 76 minutes. That reasoning does not transfer. A cancel
is not a submission; the volume argument is the only part that survives, and a rate
bound answers it completely.

A hard count is also **wrong on the facts of the incident it is for.** Cert was
502-ing from 10:00 to 10:13 and the order sat working until 12:15. A budget of three
attempts would have been spent inside the outage, on the one stretch where nothing
could have worked, and the recheck would then have watched the broker recover at
10:15 and never tried again. A rate bound self-heals.

**Proposed schedule:** re-cancel on the first eligible sighting, then back off
geometrically (≈15s, 30s, 60s, 120s, 240s, 480s) to a floor of
`watchdog.unmanaged_order_max_s`, and from then on **one attempt and one page per
bound**, for as long as the order is confirmed working. At 900s that is ~30 cancel
requests over a full session — against a documented ceiling of 40 per *second*, and
against `ratelimit.py`'s process-wide floor which covers it regardless.

**The page rides the same schedule**, using `_Escalation`'s existing `overdue(key,
tag, after_s)` — the mechanism A2 built for exactly this ("what the 2026-08-05 storm
argues for is a bounded RATE, not a single event"). One record per bound, carrying
`external_identifier` **at the top level of the detail** so B1's `(kind, scope)`
throttle scopes it per order rather than collapsing two orders into one alert
([alert throttle](2026-08-21-alert-throttle-is-per-position.md)). Rising edge is Tier
1 and immediate; the repeats are the bound.

### The new key

```toml
[watchdog]
# How long an order Argus has stopped managing may remain confirmed-working at the
# broker before it pages again — and the floor on how often its cancel is re-requested.
unmanaged_order_max_s = 900.0
```

REQUIRED, refused if absent, validated `> refresh_interval_s` (a bound below the poll
cadence fires every tick). **Adding it means the config deploys WITH the image**,
which is a real cost and is why the alternative was considered seriously:

- **Reuse `exit_unattempted_max_s` (30.0).** Rejected on the number, not the name.
  That key means "a triggered position has not been closed", which is a genuinely more
  urgent condition than an unfilled OTM opening order. Applied here it produces ~600
  Tier 1 escalation records over one afternoon for one unchanging fact — the volume
  the `_Escalation` docstring exists to prevent — while Slack sees ~20 because B1
  coalesces. The Tier 1 trail is the part that would be ruined.
- **A constant instead of a key.** Rejected: this is an alert cadence, which is the
  category the operator actually tunes, and the repo's line is that the burst's SHAPE
  is code and the numbers are config.

---

## Interactions, each stated because each is a way to be wrong

- **`DuplicateOrderGuard`** — no race, and the reason is structural: the guard governs
  SUBMISSIONS and the recheck never submits. There is a real interaction in the other
  direction and it is benign: a working order under an Argus identifier *blocks*
  re-entry of that identifier, so cancelling it removes a block. Nothing re-enters
  anyway, because `EntryExecutor._attempted` holds the candidate for the process
  lifetime. **The recheck can never widen what may be entered**, and `_attempted` is
  the reason — which is one more thing that must not be "tidied".
- **P4's concurrency count** — a confirmed-terminal residual drops out of
  `open_working_spread_equivalents` on the same poll, freeing a slot. That is correct
  (the exposure is genuinely gone) and it is the one path by which this design
  indirectly authorizes an entry. It rests on a positive terminal confirmation from
  the broker, which is the right basis, and it is a reason the "absent is not a
  verdict" rule has to hold on both sides.
- **P3's halt grade** — no collision, by construction: the halting order came from a
  previous process and this registry is in-memory. Under `ENTRIES` the recheck runs
  and manages *this* process's residuals only.
- **The kill switch** — the recheck is the missing second half of an existing
  guarantee, not a new behaviour near it. It never cancels a closing order, so it
  cannot violate the 2026-08-03 decision's "a trip does not stop exits".
- **`_end_day`** (renamed `_end_entries_today` 2026-09-01, and since then it stops
  NEITHER — the watchdog and this recheck both survive it; what stops them is the
  close, a SIGTERM or an `ALL`-graded halt) — Same blast radius as the
  flatten's, unchanged, and named again in "what this does not fix".

---

## What must NOT change

- **A cancel is never re-requested for a CLOSING order.** Automatically, ever, under
  any grade or halt. Cancelling a close strands a position.
- **Direction never comes from the broker's echoed leg actions on this path.** See
  question 5; the same field is safe in `working_orders.py` and unsafe here.
- **Absence is never a verdict**, in either direction: it does not resolve a residual
  and it does not authorize a cancel.
- **The registry stays in-process.** No rehydration, no audit-log-derived recovery of
  a previous process's orders. That is reconciliation's job and P3's boundary.
- **A failure inside the step never halts the watchdog** and never propagates into
  position monitoring.
- **Nothing here submits, replaces, or prices anything.** The module's only outbound
  call is `cancel`, and its only inputs are a `LiveOrders` answer and its own registry.
- **`_request_cancel` keeps its current contract** — best-effort, non-fatal, decision
  stands either way. This adds a follow-up; it must not make the walk's cancel fatal,
  which would turn a decided outcome into an exception on the hard side of `entry.py`'s
  boundary.

---

## What this does NOT fix

- **Orders from a previous process.** In-memory registry, deliberately. 2026-08-28's
  order would still have halted the 12:15 restart — graded `ENTRIES` now, so with a
  watchdog. The recovery is still: cancel it by hand, restart.
- **Orders left by a walk that died with the process.** Same reason.
- **`ALL`-graded halts and the close** take the recheck down with the watchdog.
  (`_end_day` did too until 2026-09-01; it no longer stops the watchdog at all.)
- **Whether a re-requested cancel actually works** on an order stuck at
  `Cancel Requested`. Unknown, and it is the open question this design sits next to
  rather than answers. The page is what covers the case where it does not.
- **The wider invariant** — *every* Argus opening order working at the broker that no
  walk owns — which is design (c) in question 2. This handles residuals of walks this
  process ran.

---

## What it would have done on 2026-08-28

10:10:0x — `_request_cancel` raises on the 502; `_finish` returns `NEVER_WORKED` and
registers residual 1424563 (`argus-open-20260828-call-7765-7770`, OPEN,
`cancel_requested=False`).

10:10:1x — first watchdog refresh after registration. The order is named and working,
past the 3s allowance. Cancel re-requested — **against a broker that is still 502-ing,
so it fails.** Tier 1 rising-edge page: *"order 1424563 still working after a failed
cancel"*.

10:10–10:13 — two or three more attempts on the backoff, all failing, one page.

~10:13 onward — cert recovers (the two spreads fill at ~10:12, so it is answering
again). The next scheduled attempt succeeds; `order.cancel_requested` is written; the
following poll sees the order terminal and resolves the residual.

12:15 — the restart's reconciliation finds nothing. No halt. Both spreads watched,
`_attempted` cleared, `call-7755-7760` gets the fresh guarded exit attempt the restart
was for.

**Stated honestly:** if cert had stayed down through 12:15, the recheck would have
changed nothing except that the operator would have been paged every 15 minutes
holding the order id, instead of finding it in a halt three hours later. That is worth
building for on its own, but it is not the same claim as "the incident does not
happen."

---

## Sequencing — why this rides with P3, not inside v0.3.13

**The config coupling is not the reason, and the first draft of this record was wrong
to say it was.** v0.3.13 already forces a config-with-image deploy for P2's
`watchdog.flatten_before_close_s`. A second REQUIRED key riding the same cutover would
cost nothing extra — the coupled deploy is already paid for. That argument is void.

Three real ones remain, in ascending order of weight:

**1. Attribution.** v0.3.13 already carries five changes across the exit and order
paths. The repo's own evidence for why this matters is the 2026-08-26 account-refresh
fix, which is *provable* only because one trading day split across two code versions
under identical broker conditions — hence the standing rule to split a journal by PID
when reading a day that spans a deploy. Six changes in one image is not six times the
risk; it is one bundle you cannot decompose after a bad Monday. Note the release's own
scope section already draws this line correctly for a different reason: P0/P1/P2 ship
together **because they are not independently useful**, which is an argument for
coupling those three specifically, not a licence to add independent things to them.

**2. Retracted — "this is the only one of the six that SENDS something new" is not,
on its own, a reason to spend a calendar day.** The earlier draft of this section
argued that shipping a new outbound cancel alongside five other changes is "a bad
sentence to have to say during an incident review" — which smuggled in live-capital
stakes that do not exist yet. **We are in cert. There is no capital at risk and no
incident review of that kind.** A misfired cancel here is a bug to notice and fix,
not a loss to explain. This reasoning becomes real again as the live flip
approaches — see "Sequencing and the live flip", below — but it does not argue for
delay today.

**3. The one reason that survives on its own technical merit: sensor before
actuator.** This design hangs off P4's `_refresh_working_orders` — the same
`get_live_orders()` poll, on the same cadence, built 2026-08-29 and not yet run
against a real cert session. Wiring an action to a poll's output in the same image
as the poll's first exposure means learning whether the sensor is trustworthy and
whether the actuator behaves correctly at the same time, from the same data. That is
real regardless of stakes — it is a debugging-hygiene argument, not a safety one, and
it does not need an *artificial* wait: the time it naturally takes to review and build
P5 already gives P4's poll a live cert session or several before anything rides on
top of it. No extra calendar padding is needed on top of that.

**Net: hold P5 out of v0.3.13's image (already tested, already ready), but do not
treat it as needing its own separate release day after that.** Once the design is
approved and P5 is built and tested, it can ship in the SAME follow-on cert release
as P3 — which is already built and signed off, and is itself only waiting on a
deploy slot for reasons about the halt-scope rail's correctness (see
[halt scope split](2026-08-29-halt-scope-split.md)), not calendar pacing. Attribution
(reason 1) argues for keeping that follow-on release's own change count sane — two
items, not five — not for spacing it away from v0.3.13 by days.

**What must not happen is the inverse trade: holding a finished, tested v0.3.13 to
wait for P3 or P5.** P2 is the finding the taxonomy record called out as having *no
mitigation at all*, and it is the only control that works when everything else has
stopped. Delaying it to wait on unbuilt or unreleased items is the one sequencing
choice here with a clearly wrong answer.

### Sequencing and the live flip

Reason 2 above is not gone, only deferred to where it actually applies.
`execution.walk_source` / `watchdog.trigger_source` are already flagged in
`CLAUDE.md` as carrying SANDBOX-ENTRY values needing "a second, data-backed pass
... before size is scaled" — i.e. there is already a planned gate before
live-min-size. **P5 should be included in whatever that pass reviews**: by the time
size is scaled, P5 should either be running cleanly in cert with a track record, or
explicitly still absent and named as a gap in that review, not something that rode
in on a compressed schedule and was never separately looked at again. That is a
different, later checkpoint from this one, and it costs nothing to write down now.

---

## How it would be tested

The shape P2 and P3 set: real behaviour end to end, and a mutation per decision.

- **Registration**: each of the six `_request_cancel` sites leaves exactly one
  residual when the last observation was non-terminal, and none when it was terminal;
  `UNRESOLVED: order_not_visible` registers one although no cancel was requested;
  `FILLED` registers none.
- **The state table, exhaustively**: terminal resolves; `FILLED` resolves and pages
  Tier 1; working-inside-allowance waits; working-past-allowance cancels once;
  **absent does not resolve and does not cancel**; unmappable-under-the-identifier
  does not resolve and does not cancel.
- **Direction**: a CLOSE residual is never cancelled however long it works — asserted
  on the call, and with an AST assertion that the module's decision path reads no
  `.direction` attribute off an order that came from `LiveOrders`, so a future
  "consistency" edit that swaps the minted direction for the broker's fails a test
  rather than a position. A residual whose minted direction disagrees with
  `parse_external_identifier` is not cancelled and pages the disagreement.
- **The relabelling case as a real test, not a unit stub**: a chained pair where the
  broker returns `Sell to Open` for a closing order, proving the recheck still calls
  it CLOSE.
- **Rate**: attempts follow the backoff and floor at `unmanaged_order_max_s`; a
  four-hour stuck order produces a bounded, counted number of cancel calls and one
  page per bound; a broker that fails for ten minutes and then recovers **is still
  tried after recovery** (the test that a hard count would fail).
- **Scope**: two simultaneous residuals produce two independently-scoped alerts with
  `external_identifier` at the top level of the detail (B1).
- **Isolation**: a `step` that raises does not stop the watchdog's `check_once`, and a
  position's triggers are still evaluated on the same tick.
- **Wiring**: dry-run mode registers nothing; a monitor-only (P3 `ENTRIES`) session
  runs the recheck; the session close stops it (`_end_day` did too until
  2026-09-01, when it stopped reaching the watchdog).

**Mutations, each of which must fail at least one test**: absence treated as
resolution; absence treated as licence to cancel; the CLOSE guard dropped; direction
read from the broker's order; the settle allowance dropped; the backoff floor removed;
the identifier cross-check dropped; the step's exception swallow removed (the watchdog
halts); the registry made durable; the recheck gated on the kill switch.

---

## Also proposed, and separable

**`/status` should name a residual.** A live order Argus has stopped managing is the
third instance of the pattern the open-decisions table already names twice
(`daily_loss.breach` reading TRIPPED off a log row, and a spent exit attempt rendering
as an ordinary open position): a page that reads calm for a state the operator needs
to act on. One row — identifier, id, how long it has been working, attempts made, last
broker status — read **live off the registry**, never off an audit row, which is the
rule `kill_switch.since` and `breaker_tripped` both had to learn.

This is cheap and it is genuinely separable: if the recommendation below is accepted
in part, the `/status` row is worth having even with the automatic cancel left out.

---

## The recommendation

1. **Build it, as specified**: registration at `_finish`, the recheck driven by the
   watchdog's existing `LiveOrders` poll in its own module, positive-confirmation-only
   action, automatic re-cancel for **opening** orders on a rate bound, page-only for
   closing orders and for every unreadable state.
2. **The softest call in this document is the automatic cancel itself**, and the
   fallback is not "page-only" but **`would_cancel`** — the same code, the same
   registry, the same decision computed and recorded at Tier 2, with the outbound
   call not made. That is `shadow.would_submit`'s exact shape and it is idiomatic
   here; "build the observer, decide about the action later" is not, because it
   defers the decision without collecting anything that informs it.

   The distinction matters because **a staged rollout only earns its cost if stage
   one produces evidence that de-risks stage two.** `would_cancel` does: every
   residual it registers exercises the direction classification (minted vs
   `parse_external_identifier`), the OPEN/CLOSE split, the identifier cross-check,
   and the working / terminal / absent reading — on real cert data, with no
   outbound call. What it cannot exercise is the trigger itself, because the
   condition it fires on is rare: most residuals resolve on the first poll. So
   expect it to validate the inputs thoroughly and the decision almost never, and
   size the stage accordingly — a week, not a month, and do not wait for a
   `would_cancel` record to appear before promoting.
3. **One new REQUIRED key**, `watchdog.unmanaged_order_max_s = 900.0`, so **the config
   deploys WITH the image**. If that coupling is unwanted this release, the manual
   variant in (2) still needs it — the bound is on the page, not on the cancel.
4. **Not in v0.3.13's image, but not held for a separate calendar-spaced release
   either.** We are in cert, not live trading — the earlier draft of this
   recommendation leaned on live-capital stakes that do not apply yet, and that
   reasoning has been corrected below ("Sequencing"). Once you approve the design
   and it is built and tested, it can ship in the SAME follow-on cert release as
   P3 — which is already built and signed off and only waiting on a deploy slot.
   The one real reason for keeping it out of v0.3.13's own image is sensor-before-
   actuator hygiene (P4's poll should see at least a live cert session before an
   action rides on its output), and the natural time to review and build this
   design already provides that. → see "Sequencing", below.
5. **The exit RE-ATTEMPT open decision is untouched by this** and should stay open on
   its own terms.

**I am not building this without your review.** The design is complete enough that
implementing it would be an obvious next few hours, and per the authorization's own
gate that is exactly the point to stop.
