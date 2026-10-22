# A rung that never worked is not a verdict either (2026-09-10)

**FOUND AND DIAGNOSED 2026-09-10 from the 2026-09-09 cert session. BUILT 2026-09-10**
— `src/` only (`loop.py`, plus an additive comment beside `watchdog.py`'s
`_RETRYABLE_EXITS`), no config key, no migration, no new audit category, so the image
deploys alone. **The identifier-wide FILLED read moved from `_resolve` into `_finish`
and is now gated on the OUTCOME**, not on the branch that produced it: `_finish` is
the sole constructor of every `WalkResult` in the module, so the rule is an INVARIANT
there rather than the habit that has now failed three times. See *What was decided*
below; the design is
[`2026-09-10-never-worked-identifier-wide.md`](../designs/2026-09-10-never-worked-identifier-wide.md),
SIGNED OFF and now SHIPPED.

This is the **third instance** of one class. 2026-08-26 closed two of them
(`reconcile._ask_broker`, `entry._check_for_duplicate`); 2026-09-02 closed the
FILLED-check version at `_resolve`'s terminal branch. The rule broken here is the
same rule, and it is a stated non-negotiable:

> The FILLED check alone stays identifier-WIDE, deliberately — a fill on any rung
> means the spread is flat.

Sibling: [a terminal rung is not a verdict](2026-09-02-a-terminal-rung-is-not-a-verdict.md).
Session it was found in: the 2026-09-09 EOD pass (12 trade ideas, 2 fills, 7 refused
on `instrument_validation_failed`).
Design, SIGNED OFF and SHIPPED:
[`docs/designs/2026-09-10-never-worked-identifier-wide.md`](../designs/2026-09-10-never-worked-identifier-wide.md).

## The evidence

`20260909-call-7660-7670`, entered 13:36:42 ET. Two surfaces disagree about whether
it traded.

`tools.desk`, EXECUTION QUALITY — what the **walk** reported:

```
CANDIDATE                        OUTCOME       REQ  FILL  STEPS    LIMIT  TIME
20260909-call-7660-7670          never_worked    1     0      1     0.50  13:36:42
```

`tools.desk`, CLOSED INTRADAY — what the **ledger** holds:

```
CANDIDATE                        CONTR   CREDIT    DEBIT      PTS        P&L $
20260909-call-7660-7670              1     0.55     0.15     0.40        40.00  filled
```

`tools.mark` scores the same candidate `closed_intraday`. Both tools reconstruct
fills from `order_legs` via `external_identifier`, never from the walk, so the
ledger is the authority here and the walk is the surface that is wrong.

**The reconstruction.** `state.sent = next_sent` in `_reprice`, and `_finish` reads
`final_limit_price` off `state.sent` — so the reported `0.50` is the REPLACEMENT's
limit. An entry walk concedes downward (less credit), so rung 0 was `0.55`, which is
exactly the credit the ledger records. **The original rung filled 1 contract while
the replacement was in flight, and the walk concluded about the replacement.**

That last step is an inference across two surfaces, and it is worth confirming
directly the next time the shape appears: `walk.finished` carrying
`filled_contracts: 0` with `steps: 1`, against `order.fill` rows under the same
identifier naming a DIFFERENT `broker_order_id` than the one on `walk.finished` —
and no `walk.filled_under_identifier` record, which is the Tier 2 category
2026-09-02 added precisely so this condition stays countable.

## The mechanism

`_await_in_market` runs **after every reprice**, not only after the first submit —
`_walk`'s own comment says why: *"a replace mints a new order that has to get there
on its own"* ([loop.py:1077](../../src/argus/execution/loop.py#L1077)). So a walk at
step 1 re-enters it holding the replacement, while the original rung is still
resolvable at the broker under the same `external_identifier`.

Inside, there are three exits. One asks the identifier; two do not.

```
while True:
    observed = await self._observe(state)          # the CURRENT rung, by id
    if observed is not None:
        seen = True
        if is_in_market(observed.status) or _reported_fill(observed):
            return None                            # proceed to the hold
        if observed.is_terminal:
            return await self._resolve(state, observed)   # <- probes, since 2026-09-02
    if self._clock() >= deadline:
        break
    ...
if not seen:
    return await self._finish(state, WalkOutcome.UNRESOLVED,      # <- no probe
                              reason="order_not_visible_at_broker")
await self._request_cancel(state)
return await self._finish(state, WalkOutcome.NEVER_WORKED, ...)   # <- no probe
```

`_observe` reads `state.broker_order_id` — the current rung — so a replacement that
sits at an unreadable or merely non-`Live` status until `working_timeout_s` expires
takes the deadline branch, and the deadline branch concludes.

**2026-09-02 did not miss a case it considered; it scoped itself to a different
input.** Its probe hangs off a *terminal broker observation*, and its own docstring
argues from that: the probe *"is strictly ADDITIVE over a positive terminal
observation the broker gave us one call ago."* Here there is no such observation —
the walk reaches a terminal conclusion of its OWN, off a clock, with the broker
having said nothing conclusive. That is the distinction, and it is the bug: **the
trigger for asking should be that the walk is about to conclude, not that the broker
already did.**

Where the rule is applied and where it is not:

| Site | What makes the walk end there | Asks the identifier |
|---|---|---|
| `_await_settled` (`watchdog.py`) | authorizing a SECOND closing order | ✔ since 2026-08-21 |
| `_recover` (`loop.py`) | a mutation raised | ✔ since chunk 3 |
| `_resolve` terminal branch | the broker returned a terminal status | ✔ since 2026-09-02 |
| `_await_in_market`, seen → `NEVER_WORKED` | **`working_timeout_s` expired** | ✘ |
| `_await_in_market`, not seen → `UNRESOLVED` | **`working_timeout_s` expired** | ✘ |
| `_walk` blind cycles → `UNRESOLVED` | **N unreadable answers in a row** | ✘ |

**So this is two outcomes, arguably three — not one.** `NEVER_WORKED` is the one
observed; `UNRESOLVED: order_not_visible_at_broker` sits on the same deadline, three
lines up, and is the outcome the exit path treats as its headline "a human should
look at this now" verdict. Fixing only the observed one would leave the louder
sibling wrong.

## The codebase already contains the argument for the fix

`watchdog.py`, defending the one-attempt rule
([watchdog.py:3727](../../src/argus/execution/watchdog.py#L3727)):

> Its worst case is measured, not theoretical: `NEVER_WORKED` — the
> `working_timeout_s` path — calls `_request_cancel`, which is best-effort and
> confirms nothing, **so the abandoned order is at `Cancel Requested` at best and can
> still fill.**

That is exactly right, and the corollary was never drawn. *If a `NEVER_WORKED` rung
can still fill, then by the time the walk concludes it may already have* — and the
loop concludes without asking. The same sentence that justifies refusing a retry
also justifies asking the question, and the two are not in tension: one is about
sending, the other is about reading.

## What it costs

**Not position safety.** `positions` is written from the fill feed by chunk 2,
independently of the walk, so the position was adopted, watched, and exited
normally — this one closed for +40.00. That is the same bound the 2026-09-02 record
established for the entry mirror, and it holds here for the same reason.

**Not `entry_credit`.** Since migration 0005 that is the contracts-weighted REALIZED
per-leg fill price, not the walk's limit, so the credit stop reads the right basis
on a position acquired this way.

**Reporting, on both paths, plus one real cost on the exit path:**

- **Entry.** `NEVER_WORKED` is in `entry.py`'s `_ALERTING_OUTCOMES`, so a trade idea
  that opened a position pages as unexplained. `WalkResult.filled_contracts` reads
  `state.last.reported_fill_quantity` — the replacement's 0 — which mis-scores the
  execution funnel (2026-09-09 reads as 1 clean fill of 12; it was 2) and is an input
  to the `partially_filled` comparison that already straddles two scopes.
- **Exit.** `NEVER_WORKED` is not `WALKED_AWAY`, so `ExitExecutor` returns
  `ExitOutcome.NOT_RESOLVED` — **which spends the position's one exit attempt and
  fires the `_SPENT` escalation.** On a spread the exit actually flattened that is
  wasted state and a false page rather than a stranded position, but it is the same
  harm the 2026-09-02 incident was written about, reached by a different branch. It
  has not been observed on the exit path; nothing prevents it.

**Rate.** Unknown, and this is the honest gap. 2026-09-02's version had a measured
1-of-3 on successful replaces that day. This one has n=1, on a session where only two
orders filled at all. The correlation argument transfers unchanged — a reprice goes
out exactly when a fill is imminent — but the branch is narrower (it needs the
replacement to stay unreadable or non-`Live` for a full `working_timeout_s`), so the
rates should not be assumed equal. `walk.filled_under_identifier` already counts the
2026-09-02 shape; the fix should make this shape countable the same way.

## What must not change

- **The one-attempt rule is untouched.** This is not a retry and must never be built
  as one. It changes what the walk CONCLUDES, never what it SENDS. Every sentence in
  `watchdog.py`'s defence of the rule survives the fix intact.
- **The probe stays additive and falls back.** `_finish_if_filled_elsewhere`'s
  contract — a read that cannot be answered returns the verdict the branch already
  had, one attempt, no retry, no reconnect — is the right contract here and for a
  sharper reason: this branch has spent `working_timeout_s` already, and on the exit
  path it is awaited while a triggered 0DTE position is open. *(As built, that
  method's body IS the new gate and the name is gone; the contract is unweakened —
  it is INLINE in `loop._finish`, deliberately: two names for one step is how the
  module ends up with two probe sites again.)*
- **FILLED only.** The identifier-wide question is *"did anything under this key
  fill?"*, never *"what else is out there?"*. Widening it toward working orders
  re-opens the double-send reasoning `_await_settled` exists to hold.

## What was decided, and what was built

The four questions above were put to the design, all four were answered, and the
design was signed off unchanged on 2026-09-10. Built the same day.

1. **One site, not the branch.** The gate is `_finish`'s, keyed on a frozen
   `_ASK_BEFORE_CONCLUDING` set of outcomes: `NEVER_WORKED`, `UNRESOLVED`, `REJECTED`,
   `CANCELLED_ELSEWHERE`, `NO_ORDER_EXISTS`, `WALKED_AWAY`, `DRIFTED`. The membership
   rule decides every member at once — *ask wherever the walk concludes "no fill" and
   an order it sent may still be live at the broker* — and `_resolve`'s own call was
   DELETED rather than left beside it, because two probe sites double-ask every
   `REJECTED` and are how the module ends up needing this fix again. `_NEVER_ASKS`
   holds the other three, each excluded for its own reason rather than by being left
   out: `FILLED` is the answer; `BLOCKED` sent nothing AND was just guarded; `ABORTED`
   is the kill-switch frame, where **a halt must not add a broker round trip** — a
   safety rail, not an optimisation. `test_every_outcome_is_classified` asserts the
   union covers `WalkOutcome` exactly and the two sets are disjoint, so a new member
   fails on both halves until somebody decides. That test is the whole difference
   between an invariant and a habit.
2. **Yes, the blind-cycle `UNRESOLVED` joins**, and it is the least arguable of the
   three rather than the weakest: it concludes from repeated failure of `by_id`, and
   `by_identifier` is a **different question against a different key** — a fill on the
   original rung carries another `broker_order_id` entirely, so every `by_id` read can
   legitimately fail while the identifier-wide answer is sitting there.
3. **`state.last` and `state.broker_order_id` move; `state.sent` and
   `state.cancel_requested` do not**, exactly as 2026-09-02. Moving the outcome without
   moving the first two is the half-applied fix that is harder to spot than the bug.
   `state.sent` stays because `final_limit_price` is read off it and **a limit is not a
   fill price**; `cancel_requested` stays because a cancel *was* requested. **The probe
   runs AFTER the cancel**, which falls out of living in `_finish` and is right on its
   own terms: cancelling the current rung is correct regardless of what a prior rung
   did, probing last makes the answer as fresh as the frame allows, and a cancel cannot
   hide a fill on a different rung. Mutation-tested: reversing the order changes no
   test, so the ordering is not load-bearing in either direction.
4. **`_RETRYABLE_EXITS` needed no change — and the confirmation this asked for turned
   up one thing the design had wrong.** Confirmed: a walk that now reports `FILLED`
   never reaches `ExitOutcome.NOT_RESOLVED`, because `_after_walk` handles `FILLED`
   first. **Not confirmed, and corrected here: it does not "never spend the attempt".**
   `_run_exit`'s terminal branch means *an order reached the broker*, which is true of
   `FILLED` too, so `_attempted` is written and the `_SPENT` episode is armed exactly
   as for any other terminal outcome. What does not happen is the PAGE — the closing
   fill books, the row leaves the ledger, and `_page_if_spent_and_still_held` never
   fires (the property `test_a_clean_fill_that_books_never_pages` already pinned). The
   change is which outcome is recorded there, not whether the slot is written.

   `_RETRYABLE_EXITS` did gain an additive comment, because §3.7's safety argument
   depends on a property of that set: both members mean *nothing filled*, so a second
   walk under one `argus-close-*` key cannot inherit a first walk's fill. A future
   member that can follow a PARTIAL fill would be the only way this gate could produce
   a false `FILLED`, and that reasoning would have to be redone.

## One thing the build overturned

**On the exit path the gate does not merely duplicate `_await_settled`'s read — it
PRE-EMPTS it.** The design (§3.2) called the exit-side `WALKED_AWAY` coverage
"redundant, not wrong" on the grounds that the market-crossing escalation opens with
`_await_settled`, which asks the same identifier-wide FILLED question. It is the same
question, but the walk now answers it a frame earlier, so `_cross` is never reached —
and the cancel/fill-cross case that `_await_settled` reports as
`ExitOutcome.FILLED_LATE` is now reported as plain `FILLED`.

Nothing load-bearing moved: nothing is crossed, exactly one order is sent, and
`ExitOutcome.closed` is true either way. What is lost is a NAME — *the walk gave up and
the order filled anyway* — which now lives one tier down as
`walk.filled_under_identifier` with `suppressed_outcome: walked_away`, and one alert
kind (`watchdog_exit_filled` instead of `watchdog_exit_filled_late`; both page).
`needs_attention` has no callers in `src/`.

It is recorded rather than designed around, because carving `WALKED_AWAY` out on the
exit path is precisely the third option sign-off declined — it would make the exclusion
depend on a downstream caller's behaviour the walk loop cannot see, which is this
defect's own failure mode wearing a different hat. **If `FILLED_LATE` is wanted back it
is a taxonomy change, in the taxonomy's own file, on its own decision.**

## Corollary for the rejection back-off decision

The open decision *"should entry back off on the COUNT of consecutive terminal
rejections?"* already carries the 2026-09-02 caveat: **"your order filled" is not a
broker refusing to trade**, so build that counter on top of the identifier-wide fix,
never before it. This record extends the caveat by one outcome — a counter built over
`_ALERTING_OUTCOMES` rather than over `REJECTED` alone would count a `NEVER_WORKED`
that opened a position, which is the same category error one branch over.
