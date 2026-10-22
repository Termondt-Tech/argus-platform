# Argus: The Flatten's One-Shot Override Is Spent Before The Order Can Be Built

## Priority

**Real, observed on 2026-09-01 on 2 of 2 flattens, and harmless only because of a
property of that day rather than a property of the mechanism.**

`FLATTEN_BEFORE_CLOSE` gives a position whose exit attempts are already SPENT one
further send past the mark — the single override on a stated non-negotiable, granted
because a 0DTE spread held into the close with no automated path to exit is the worst
state the system can produce. That allowance is claimed the moment the legs have
QUOTES. But an order can still fail to be built after that, and the way it fails is
the flatten's **modal** regime: a net mid that rounds to 0.00 at the tick.

On 2026-09-01 both flattened positions burned the override on an order that was never
sent. Neither had spent its ordinary attempt, so both took the normal path and closed.
**A position that arrives at 15:30 with `_attempted` already claimed — which is
precisely the position the override exists for — gets one unpriceable look and then
has nothing left for the rest of expiry day.**

Small fix, load-bearing surface: it touches the exit handoff and an override on a
non-negotiable. **Design first; do not implement without Richard's sign-off**, on the
gate P3, P5 and the exit ladder took.

Background: [the second v0.3.13 day](../decisions/2026-09-01-second-live-fire-day.md),
and the trigger's own record,
[a clock is the third trigger](../decisions/2026-08-28-time-based-flatten-trigger.md).

## The evidence

14:30 ET, both held spreads, verbatim:

```
watchdog_exit_unattempted [20260901-put-7600-7595]
  {"outcome": "not_attempted", "trigger_reasons": ["flatten_before_close"],
   "reason": "ValueError: net mid 0.0 rounds to 0.00 at a 0.05 tick
              — there is no valid limit price for this spread", "walk": null}

watchdog_exit_unattempted [20260901-put-7595-7590]   (same reason)
```

Then `exit_still_unplaced` at 37.8s and 43.8s, `exit_unpriceable` a minute later, and
finally both crossed the market at −0.10 and filled. The day ended flat. The override,
however, was gone from the first tick.

## The mechanism, confirmed in source

`_maybe_exit` spends the allowance at `execution/watchdog.py:2910-2919`:

```python
market = evaluation.market
if TriggerReason.FLATTEN_BEFORE_CLOSE in fired:
    # Spend the flatten allowance HERE — at the handoff, and only once an
    # order can actually be built. Spending it above (at the `market is None`
    # branch) would consume the position's last action on an order that was
    # never sent, which is the exact failure `NOT_ATTEMPTED` exists to avoid;
    # spending it on the REPORT instead would let a crash mid-exit hand out a
    # second one. Unconditional rather than gated on `_attempted`: ...
    self._flatten_attempted.add(key)
```

**The comment's reasoning is right and its placement does not implement it.** The
spend sits just past the `evaluation.market is None` guard (`watchdog.py:2875`) — that
is "the legs have quotes", not "an order can be built". The build happens later, in
`ExitExecutor.close` → `_build_exit_order` → `build.py`'s `_limit_price`, which refuses
a mid rounding to zero (`execution/build.py:241-249`) and hands back
`ExitOutcome.NOT_ATTEMPTED` (`watchdog.py:951-964`).

**The two allowances are then treated in opposite ways for the same failure.**
`_run_exit` on `NOT_ATTEMPTED` (`watchdog.py:2944-2947`):

> Nothing was sent, so the position keeps its one attempt and the trigger stays
> latched.

And `close()`'s own docstring on that same path (`watchdog.py:951-956`):

> nothing was sent, so the position keeps its one attempt

Both sentences are true of `_attempted` and false of `_flatten_attempted`, which was
claimed several frames earlier and is never restored.

## Why this is the flatten's normal case, not its tail

A winning 0DTE credit spread is nearly worthless by 15:30 — that is what makes it
winning. A mid that rounds to 0.00 at a 0.05 tick is therefore the EXPECTED first look
for the exact positions the flatten most often fires on, and it was the observed result
on 2 of 2 flattens on 2026-09-01. This is the same regime finding that motivated
`exit_walk_min_steps` (the ladder having zero range below a 0.10 debit), one step
earlier in the same path.

## Design questions

1. **Where should the spend go?** Candidates: after `_build_exit_order` succeeds
   (but `close()` does not know the allowance exists); a buildability pre-check at the
   handoff so the spend and the send become one decision; or restoring it on
   `NOT_ATTEMPTED`. Argue the choice — they differ in what happens when the exit task
   dies between the spend and the report.
2. **If restoring: how is "nothing was sent" distinguished from "the task died"?**
   The existing comment rejects spending on the report precisely because *"a crash
   mid-exit would hand out a second one."* A restore has the same hazard from the other
   side. `NOT_ATTEMPTED` is a returned VALUE and a crash is an exception — the same
   value-versus-exception boundary the 2026-08-26 reconcile work leaned on — so say
   whether that distinction is load-bearing here too, or whether it is weaker because
   `_run_exit` catches `BaseException` and halts.
3. **Does the flatten need its own answer to "the mid rounds to zero"?** `build.py`
   deliberately defers that policy question to chunk 4 (*"whether it is worth paying a
   tick to close a near-worthless spread is an exit-policy decision"*), and chunk 4
   currently answers "not by guessing." For the FLATTEN specifically the answer may
   differ — but see the first trap before proposing it.
4. **Should `StaleSignalError` and `ValueError` be distinguished?** `close()` catches
   them together into one `NOT_ATTEMPTED`. One is a freshness problem that resolves on
   its own; the other is a market-shape problem that may not. They may deserve the same
   treatment; decide rather than inherit.
5. **What does the operator page say?** The `reason` today is a stringified
   `ValueError`. It is accurate and it reads as a crash rather than as the deliberate
   refusal it is. Cheap to improve while the code is open; not the point of the fix.
6. **`_flatten_attempted` is deliberately not cleared by the scan-forget path**
   (`watchdog.py:2617` names it alongside `_attempted` and `_sends`). Confirm why
   before changing anything about its lifetime.

## Traps

- **The flatten must NEVER invent a price.** CLAUDE.md, on the third trigger: *"it
  must never invent a price: the width is the one number that survives a dead feed and
  paying it GUARANTEES max loss where holding realizes at most it, so an unpriceable
  flatten still sends nothing."* The likely correct answer to question 3 is therefore
  **no** — keep refusing, and simply stop burning the allowance on the refusal. If the
  design proposes otherwise it needs to reconcile with that sentence explicitly.
- **Do not turn a one-shot into a retry.** The whole content of the allowance is that
  it is ONE. Restoring it on a send that never happened is not the same as granting a
  second send, and the design must show the difference is enforced, not just intended.
- **Do not touch `_attempted`'s restore-on-`NOT_ATTEMPTED`.** That behaviour is
  correct today and is what kept 2026-09-01 harmless.
- **Do not weaken the guarded-walk / width-capped-cross path.** The flatten changes
  WHEN, never HOW: same `ExitExecutor.close`, same walk, same cap. That stays.
- **`DuplicateOrderGuard` is the licence, not the allowance.** The override is safe
  because the guard re-asks the broker and permits only DEAD — not because time passed.
  Any change to when the allowance is spent must leave that intact.
- **Never re-add the flatten to `_ALL_TRIGGER_REASONS`' role in `blind`.** A clock can
  never be unevaluable, so measuring `blind` over every reason makes it `False` for
  every input and silently retires `watchdog.unevaluable`. `_MARKET_TRIGGER_REASONS` is
  the set that page means.

## Deliverables

1. **A design document first**, answering every question above, with the site chosen
   and the crash-window reasoning written out.
2. After sign-off: the implementation.
3. **A decision record** in `docs/decisions/` — dated, added to
   `docs/decisions/README.md`, `CLAUDE.md` updated — stating the allowance's new
   lifecycle in one sentence an operator can hold, and what was deliberately NOT
   changed (the refusal to price an unpriceable spread, the one-shot nature of the
   override, `_attempted`'s existing behaviour).
4. **Tests.** At minimum: a flatten on a spread whose mid rounds to zero leaves the
   override UNSPENT and the trigger latched; the next tick with a usable mid spends it
   once and only once; a position whose ordinary attempts are already spent and which
   is unpriceable at the first flatten tick still gets its send when quotes return; and
   an exit task that raises does NOT hand out a second override.
5. **Verified mutations**, including: move the spend back to the handoff; restore the
   allowance unconditionally rather than on `NOT_ATTEMPTED` only; drop the
   latched-trigger behaviour.
6. Update the pass record's "Recommended order" to reflect what shipped.

## Non-negotiables this must respect

- **NEVER TOUCH THE LIVE DATABASE.** Read-only URI for exploration; `argus.dev.toml`
  for any dev run.
- No broker-side stops, no unbounded market orders. The escalation stays a single
  market-crossing LIMIT capped at the spread width.
- The second (crossing) order still requires a POSITIVE confirmation the first is
  dead, scoped to `walk.broker_order_id` and never to the identifier.
- Nothing submits outside `pipeline.trades_for_real`.
- The one-attempt rule for everything that reached the broker is untouched.

## Out of scope

- **D1/D2 — the reprice-rejection read and the dropped `reject_reason`**
  (`argus_reprice_rejection_identifier_wide_prompt.md`).
- **D3/D5 — the float-epsilon credit stop and the `entry_credit` basis**
  (`argus_credit_stop_wrong_numbers_prompt.md`).
- Deploying `watchdog.exit_walk_min_steps` — built, waiting, separate; it addresses the
  ladder's range in this same regime and is complementary, not a substitute.
- Any move toward a confirmation-gated exit RE-ATTEMPT. Explicitly not this.
- `staleness.market_data_max_age_s = 2.0` governing the whole exit path. Real, known,
  and **must not be fixed by raising that key**.
