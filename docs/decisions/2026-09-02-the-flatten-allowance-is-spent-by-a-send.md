# The flatten's override is spent by a SEND, not by a look

**2026-09-02. Finding 1 of [the second v0.3.13 day](2026-09-01-second-live-fire-day.md).
Designed first, signed off, then BUILT the same day. `src/` only, no new config key —
the image deploys ALONE and does not join the coupled `exit_walk_min_steps` deploy.**

Design proposal, including the two shapes rejected:
[`docs/designs/2026-09-02-flatten-allowance-spent-before-the-order.md`](../designs/2026-09-02-flatten-allowance-spent-before-the-order.md).
Task prompt: [`argus_flatten_allowance_spent_early_prompt.md`](../tasks/argus_flatten_allowance_spent_early_prompt.md).

---

## The lifecycle, in the sentence an operator can hold

> **A position's one flatten override is spent when the closing order is handed to the
> walk, and it is given back only when the exit reports that no order was built — which
> is the only report that can be made without anything having been sent.**

## What was broken

`FLATTEN_BEFORE_CLOSE` gives a position whose exit attempts are already SPENT one
further send past the mark. It is the single override on a stated non-negotiable,
granted because a 0DTE spread held into the close with no automated path to exit is the
worst state the system can produce.

That allowance was claimed the moment the legs had QUOTES — `_trigger` spends it just
past the `evaluation.market is None` early return — and an order can still fail to be
built after that. **The way it fails is the flatten's MODAL regime**: `_limit_price`
refuses a net mid that rounds to 0.00 at the 0.05 tick, and a winning 0DTE credit
spread is nearly worthless by 15:30, which is what makes it winning. On 2026-09-01 both
flattened positions burned the override on an order that was never sent, on 2 of 2
flattens.

**It was harmless because of a property of that day, not a property of the mechanism.**
Neither position had spent its ordinary attempt, so both took the normal path and
closed. A position arriving at 15:30 with `_attempted` already claimed — precisely the
position the override exists for — got one unpriceable look and then had nothing left
for the rest of expiry day.

The spend site's own comment said *"only once an order can actually be built"*. **The
reasoning was right and the placement did not implement it**, and the same mistake ran
one field over: `_run_exit`'s `NOT_ATTEMPTED` branch and `close()`'s docstring both say
*"nothing was sent, so the position keeps its one attempt"* — true of `_attempted`,
which is simply not written on that path, and false of `_flatten_attempted`, claimed
several frames earlier and never given back. **Two allowances, one failure mode,
opposite treatment.**

## The fix, and why it is at this end

The spend STAYS at the handoff and `NOT_ATTEMPTED` gives it back
(`self._flatten_attempted.discard(key)`, gated on the same `FLATTEN_BEFORE_CLOSE in
fired` that spent it). One rule now governs both allowances, read from opposite ends:

- **the handoff is the FAIL-CLOSED DEFAULT** — spent unless something positively says
  otherwise, so a crash mid-exit cannot hand out a second override;
- **the restore is on a VALUE that only construction-refusal sites can produce**, so it
  can never describe an order that reached the broker.

**Two shapes were rejected, and the reason is the same one in both cases.** Spending
inside `ExitExecutor.close` after the build succeeds (via a callback) is genuinely
precise, and it re-creates this defect mirrored: it gives the flatten allowance a
second, private signal that `_attempted` does not share, so the next person to change
one again does not know to change the other. **The fix that makes them one rule is the
one where both read the same value, in the same place, at the same time.** A
buildability pre-check at the handoff was rejected on arithmetic: the probe and the real
build call `build_order` with two different `now` readings against a 2.0s staleness
bound, so a probe that passes can be followed by a build that raises
`StaleSignalError` — spend and send do not become one decision, they become two
decisions that usually agree.

**The value-versus-exception boundary is load-bearing here, the same way it was for the
2026-08-26 reconcile work, and it is held STRUCTURALLY rather than by comment.** There
are exactly two `ExitOutcome.NOT_ATTEMPTED` construction sites in `close()` and both are
lexically ABOVE `self._loop_factory().run(...)`; an AST test asserts that, so adding a
third one below the walk fails the suite instead of silently handing back overrides for
orders that reached the exchange. Four ways the exit task can end, and none reaches a
second override:

| How the exit task ends | Allowance | A second send? |
|---|---|---|
| Returns `NOT_ATTEMPTED` | **restored** | Yes, and that is the fix — nothing was sent |
| Returns anything else | spent | No — `_flatten_may_act` refuses |
| Raises | spent — the restore is on the return path | No — `_halt_on_exception` engages the kill switch, requests stop, and the loop ends |
| `CancelledError` | spent | No — nothing cancels an exit task (`stop()` awaits, `_drain_exits` gathers), so this is process teardown |

**The halt is a backstop and deliberately NOT the argument.** Resting on it would make
the restore correct by coincidence of another module's policy — which is exactly how the
spend site's comment came apart from its placement in the first place.

## The second defect, found by design and measured

`_flatten_may_act` records `watchdog.flatten_override` at Tier 1 and pages, documented
as *"once per position"*. **It was once per TICK**, because the page sits at the
ELIGIBILITY site while the spend sits one frame further on, past `_trigger`'s
`market is None` early return — so a spent position past the mark whose legs cannot be
priced at all never reaches the spend and re-qualifies on every cadence tick. Measured
on the harness: **8 Tier 1 records and 8 alerts over 8 ticks, with 1 handoff.**

It never surfaced in live fire because 2026-09-01's flattens were refused at the BUILD
rather than at the legs, so they spent the allowance and the spend silenced the repeat:
**the allowance bug was masking this one, and giving the allowance back un-masks it and
widens it from the rare case to the modal one.** They therefore shipped together, which
was the release-scope question put to Richard and answered yes.

`SlackNotifier`'s 900s per-`(kind, scope)` throttle would have kept the phone to ~1 per
15 minutes, so the cost was never going to read as an alert storm — it is an **audit
trail** cost, and `_Escalation`'s own docstring names it: *"a Tier 1 record per tick is
not more safety, it is a trail nobody can read (and, at a sub-second cadence, sustained
contention on the writer lock spec §6 protects)."* Fixed with a `_FLATTEN_OVERRIDE`
escalation tag on the rising edge, `watchdog.flatten_override_still` at Tier 2 for the
repeats, and **no `falling` call anywhere** — it is a "we have told you" edge, not a
window that re-arms.

**The page stays on ELIGIBILITY rather than moving to the send, and that is deliberate.**
Since the allowance is now returned when no order was built, one override can span
several handoffs — each a real attempt to build the closing order, at most one of which
becomes one. Pinning the page to the send would either go silent on the ticks that
refused or fire once per refused build, which is the storm the edge exists to stop. What
an operator needs to read once is that the non-negotiable is now overridable for this
position; what happens on each tick after that is `_UNATTEMPTED`'s and `_UNPRICEABLE`'s
to report, and their `overdue` repeat already bounds it.

## Deliberately NOT changed

- **The refusal to price an unpriceable spread.** No width fallback, no one-tick
  substitute, no flatten-specific pricing branch. CLAUDE.md's rule is untouched: *"the
  width is the one number that survives a dead feed and paying it GUARANTEES max loss
  where holding realizes at most it, so an unpriceable flatten still sends nothing."*
  **And the system already had the right answer** — a zero mid means the market says the
  spread is worth approximately nothing; the escalation for "the auction will not pay"
  is the width-capped crossing limit inside `close()`, reachable only AFTER a build. On
  2026-09-01 both positions were refused at the build, looked again later, walked away,
  and **crossed at −0.10 and filled**. Look-again-next-cycle works. Only the accounting
  that decides whether there IS a next cycle was broken.
- **The one-shot nature of the override.** One SEND, still. Restoring a look that never
  became an order is not granting a second send, and the difference is enforced rather
  than intended: the restore has exactly one call site and moving it out of the
  `NOT_ATTEMPTED` branch is a verified mutation.
- **`_attempted`'s restore-on-`NOT_ATTEMPTED`.** Correct today, and what kept
  2026-09-01 harmless.
- **The one-attempt rule for anything that reached the broker.**
- **`DuplicateOrderGuard` as the licence.** The override is safe because the guard
  re-asks the BROKER at the moment of the send and permits only DEAD — not because time
  passed, and not because of anything here. Every send this permits still goes through
  it.
- **The guarded walk and the width-capped cross.** The flatten still changes WHEN, never
  HOW: same `ExitExecutor.close`, same `guard_first_submission=True`, same
  `_await_settled` confirmation scoped to `walk.broker_order_id`, same width cap.
- **`_flatten_attempted`'s exclusion from the scan-forget path.** A `positions` row can
  vanish and return for reasons that are not a closed position, and a budget that resets
  on that is a budget that resets on noise. The restore does not weaken it: it is keyed
  on a report from the exit path, never on a spread's presence in a scan.
- **`_sends` / `_EXIT_SEND_ATTEMPTS`.** A different mechanism for a different question.
- **`staleness.market_data_max_age_s`.** Not raised, not special-cased for the exit
  path, not read differently past the mark.
- **The OUTCOME for `StaleSignalError` vs `UnpriceableSpreadError` vs any other
  `ValueError`.** Still one `NOT_ATTEMPTED` for all three, deliberately: the allowance
  asks exactly one question, *was anything sent*, and the answer is "no" for every
  branch. Giving one of them a different lifetime would put a second rule back into the
  thing this change exists to reduce to one. **Only the PAGE distinguishes them** — see
  the last section; the verdict does not.
- **Not a step toward the open confirmation-gated exit RE-ATTEMPT decision, and must not
  be cited as one.**

## Tests and mutations

Eleven tests. Four are the deliverable's: an unbuildable flatten leaves the override
unspent with the trigger latched; the next usable tick spends it once and only once; a
position spent-and-unpriceable at the mark still gets its send when quotes return; and
an exit that raises hands out no second override. Three the design added: the AST
assertion that `NOT_ATTEMPTED` cannot describe a sent order, the override paging once
per position across both refusal shapes, and a spread that leaves the scan and returns
getting no fresh override. Four more cover the refusal prose: the new error's dual nature (its own type AND a `ValueError`), the three sentences reading differently with the type prefix intact on each, the end-to-end page on the path that produces it, and the entry path keeping the refusal on the soft side of its exception boundary.

**Ten mutations, nine killed.** Dropping the restore (the pre-fix behaviour); restoring
unconditionally rather than on `NOT_ATTEMPTED` only — which breaks two PRE-EXISTING
tests, not just the new ones; restoring on the exception path; dropping the rising-edge
guard; spending at the `market is None` branch (the rejected placement); and latching
ELIGIBILITY instead of the send.

**One survived, and the finding is worth more than the mutation was.** Clearing the
sustain state after a `NOT_ATTEMPTED` — the deliverable's "drop the latched-trigger
behaviour" — changes nothing on this path, because **`FLATTEN_BEFORE_CLOSE` is in
`_INSTANT_TRIGGER_REASONS` and re-fires on one observation.** There is no latch to drop
for a clock. So the retry that this fix restores does not depend on the sustain window
at all, which is a stronger property than the deliverable assumed. A side observation
from the same run, pre-existing and out of scope: **no test covers the latch for the
MARKET triggers either** — clearing it there costs a `sustained_s` re-accumulation
before the retry, which is a delay rather than a correctness break.

## The page names the refusal instead of reading as a crash

Shipped in the same change, on a second sign-off. The `NOT_ATTEMPTED` page read
`ValueError: net mid 0.0 rounds to 0.00 ...` — an accurate description of a **designed
refusal** that reads as a crash, and the only way to tell it apart from a genuine
builder defect (`_assert_credit_matches_legs` failing, a non-positive contract count)
was matching the message text. **This codebase already refuses to key behaviour on
prose** — `Order.reject_reason`'s own rule is that broker wording changes without notice
and a rail matching on it fails open the day it does. Our own prose is more stable than
a broker's and not stable enough to branch on.

`build.py` gains `UnpriceableSpreadError`, raised by `_limit_price` for the
rounds-to-zero case alone, and `_refusal_prose` in `watchdog.py` branches on TYPE to
produce three sentences:

- **the zero mid** — the market says this spread is worth approximately nothing, which
  near the close is what a WINNING position looks like; nothing was sent, neither the
  walk nor the flatten will invent a price, and the next cycle looks again;
- **a stale quote** — latency between the trigger's evaluation and the build, on the
  same bound at both ends, which clears on its own;
- **anything else** — a defect signature, worded so it does not borrow the
  self-resolving language of the other two, because it repeats every cadence tick and
  never clears.

**Still ONE `ExitOutcome.NOT_ATTEMPTED` for all three.** The allowance asks exactly one
question — *was anything sent* — and the answer is "no" for every branch; splitting the
outcome would put a second rule back into the thing the restore exists to reduce to one.
This is the page, not the verdict. The type prefix is kept on every branch, so
`error_type`-shaped readers and existing log filters are unaffected.

**It is a `ValueError` SUBCLASS, and that is the load-bearing half.** Every caller
already catches `(StaleSignalError, ValueError)` around `build_order`, so no call site
moved and no catch widened. The one cost, accepted at sign-off rather than discovered:
`entry.py` records `type(exc).__name__` in `entry.not_built`, so an entry-path zero mid
now records `UnpriceableSpreadError` instead of `ValueError` — strictly more specific,
and not reachable in practice, since `signal.min_credit = 0.50` keeps an entry credit
two orders of magnitude above the tick. **What must never change is the CATEGORY**: stop
subclassing `ValueError` and the entry refusal falls through to `entry_build_failed` and
ends the day's entries over a price that rounded down. A mutation confirms it — breaking
the subclassing fails the entry test, not only the exit ones.

Naming the refusal does not soften it. Chunk 4's answer to "is it worth paying a tick to
close a near-worthless spread" is still *not by guessing*.
