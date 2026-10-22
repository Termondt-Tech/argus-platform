# Argus: A Walk That Repriced Concludes About One Rung, And The Page Drops The Word That Explains It

## Priority

**Real, observed in cert on 2026-09-01, and it inverted the meaning of an outcome.**
An exit that SUCCEEDED — spread closed, 2 contracts, 1.50 debit, position flat — was
recorded `REJECTED` → `ExitOutcome.NOT_RESOLVED`, spent the position's one exit
attempt, and paged the operator. The broker told Argus exactly what had happened, in
prose, in the same second; that sentence reached the audit log and was dropped on the
way to Slack.

Not a safety breach on the day: the position was already flat, so the spent attempt
bought nothing and `_open_spreads` correctly stopped watching it (which is why A2's
`exit_spent_still_held` escalation properly stayed silent). The cost was a false page,
a spent attempt, and a mis-scored day. **But it hit 1 of 3 replaces**, the two events
are correlated by construction, and the ENTRY mirror shares the same code path.

**These are two fixes and they ship together.** They are one incident seen from two
surfaces, and fixing the read without the page leaves the next occurrence exactly as
illegible as this one was.

Fix 1 touches the walk state machine's terminal verdict (chunk 3), which is
load-bearing. **Investigate and propose a design FIRST for fix 1; do not implement
without Richard's sign-off**, on the same gate P3, P5 and the exit ladder took. Fix 2
is one key on a dict and can be built in the same pass once the design lands.

Background: [the second v0.3.13 day](../decisions/2026-09-01-second-live-fire-day.md).

## The evidence

`put-7615-7610`, 2026-09-01, exit triggered on `underlying_near_short_strike` at
13:42 ET. From the audit chain (times UTC as stored):

```
618781 18:42:04 order.event    broker_order_id 1430426
618782 18:42:04 order.event    broker_order_id 1430426
618797 18:42:12 order.event    broker_order_id 1430433
                               reject_reason 'Received a fill on the original order'
618798 18:42:12 order.fill     argus-close-...put-7615-7610  Buy to Close  2  7615  4.50
618799 18:42:12 order.event    broker_order_id 1430433
                               reject_reason 'Received a fill on the original order'
618800 18:42:12 order.fill     argus-close-...put-7615-7610  Sell to Close 2  7610  3.00
618801 18:42:12 walk.finished  broker_order_id 1430433  outcome 'rejected'
                               last_status 'Rejected'  filled_contracts 0  steps 1
                               reason 'broker rejected the order'
                               reject_reason 'Received a fill on the original order'
```

Two different broker orders. The walk opened at **1430426**, repriced once to
**1430433**, and the broker refused the replacement **because the original had filled**.
Net debit 4.50 − 3.00 = **1.50**, which is what `tools.desk` scored. `desk` also
labels the row `not_resolved`, because that is what the walk said.

The Slack page the operator actually got:

```
watchdog_exit_not_resolved [20260901-put-7615-7610]
  {"reason": "walk ended rejected: broker rejected the order", ...}
```

Reproduce with the first command in
[the pass record's reproduction section](../decisions/2026-09-01-second-live-fire-day.md).

## What actually happens, confirmed in source

`_reprice` (`execution/loop.py:1054-1098`) calls `replace`, and on success:

```python
state.sent = next_sent
state.broker_order_id = replaced.broker_order_id
state.steps += 1
state.last = replaced
```

**`replaced.status` is never inspected.** The replace did not raise, so nothing
reaches `_recover`. `_reprice` returns `None` and the loop polls on.

The next `_observe` reads order 1430433, and `_resolve` (`execution/loop.py:974-996`)
takes the verdict from that one order:

```python
if observed.is_terminal:
    disposition = classify_order(observed)
    if disposition is OrderDisposition.FILLED:
        return await self._finish(state, WalkOutcome.FILLED)
    if observed.status is OrderStatus.REJECTED:
        return await self._finish(
            state, WalkOutcome.REJECTED, reason="broker rejected the order"
        )
    ...CANCELLED_ELSEWHERE...
```

`filled_contracts` is `0` because `_finish` reads `state.last.reported_fill_quantity`
and `state.last` is the rejected replacement, which filled nothing. The original
filled 2.

### The rule this violates already exists in this codebase

CLAUDE.md, on the market-crossing escalation:

> The FILLED check alone stays identifier-WIDE, deliberately — a fill on any rung
> means the spread is flat.

That rule is implemented in `_await_settled` on the exit cross path. It is **not**
applied at `_resolve`'s terminal branch, which is where a walk that has repriced
concludes about a spread whose rungs share one `external_identifier`.

**And the machinery is already on this class.** `_recover` (`execution/loop.py:1146-1180`)
asks precisely the right question and already knows what to do with the answer:

```python
candidates = await self._view.by_identifier(identifier)
working = [o for o in candidates if classify_order(o) is OrderDisposition.WORKING]
filled  = [o for o in candidates if classify_order(o) is OrderDisposition.FILLED]

if filled:
    state.last = filled[0]
    state.broker_order_id = filled[0].broker_order_id
    return await self._finish(
        state, WalkOutcome.FILLED, reason=f"discovered filled while recovering from {what}"
    )
```

`_recover` is simply unreachable from this path, because it is the *exception*
handler and the replace succeeded. The gap is that a terminal non-fill on a
**replacement** is exactly as much a "the broker may know something this rung does
not" moment as a raised exception is, and only one of the two asks.

### The entry mirror — verified safe on the position, unsafe on the record

`_reprice` is shared by both policies. An ENTRY walk whose original fills as the
replace goes in records `outcome='rejected'` for a trade idea that **opened a
position**.

Checked, and this is *not* a position-safety hole: `entry.py:782-788` deliberately
does not record positions from the walk's report —

> Position recording is NOT done here. Fills arrive as a push on the `AlertStreamer`
> order feed and are booked by chunk 2's `FillConsumer` regardless of who submitted
> the order […] All this does is ask the watchdog to look sooner.

— so the position books and the watchdog adopts it either way. What breaks is the
record: `entry.finished` says `rejected` for a filled idea, `desk`/`mark` score the
day wrong, `entry_rejected` pages, and the watchdog's "look sooner" nudge is skipped
so adoption waits for the ordinary cadence.

**Consequence to state loudly in the design:** the open decision about backing off on
the **count of consecutive terminal rejections** must never count one of these. A
rejection that means "your order filled" is not a broker refusing to trade.

## Fix 1 — the identifier-wide question at the terminal branch

### Design questions to answer before writing code

1. **Where does the probe belong** — in `_resolve`'s terminal branch (catches every
   route to a terminal non-fill, including a rejection first seen through
   `_await_in_market`), or in `_reprice` immediately after the replace ack (narrower,
   catches only the observed case, no extra broker read on the poll path)? Argue it;
   they are not equivalent, and one of them is one site while the other is two.
2. **Which dispositions get it.** `REJECTED` is observed. `CANCELLED_ELSEWHERE` has
   the identical shape — a terminal non-fill on one rung while another rung filled.
   Does it get the same treatment, and if not, why is its absence safe? The FILLED
   branch already short-circuits when *this* order filled, so it needs nothing.
3. **Is it gated on `state.steps > 0`?** A walk that never repriced has one order
   under the identifier from this walk — but the broker does **not** enforce
   identifier uniqueness (measured, chunk 1), so an identifier-wide read is not
   trivially a read of one order even then. Decide and justify; do not assume.
4. **Reuse or extract?** `_recover`'s filled-branch is the exact logic wanted, but
   `_recover` wholesale is wrong here: it calls `_record_disconnect`, increments
   `state.recovered`, does `_reconnect_and_back_off`, writes `walk.disconnected`
   telemetry, reports reasons in "recovering from {what}" prose, and carries the
   `len(working) > 1` cancel-everything decision — none of which belongs on a path
   where nothing failed. Extract the probe, or write a narrow one, and say which.
5. **`state.last` and `state.broker_order_id` must move to the filled order**, the way
   `_recover` does. Otherwise the outcome is corrected and `filled_contracts` still
   reports `0` — the fix would be half-applied and harder to spot than the bug.
6. **What does `walk.finished` say afterwards?** The outcome becomes `FILLED`, but the
   record must not lose that a replacement was rejected and why. Note that
   `order.event` already captured the rejection independently (seq 618797 above), so
   decide whether `reason` carries it or the correlation is left to the reader.
7. **Failure mode of the probe itself.** If `by_identifier` raises while probing, what
   is the verdict? The pre-existing answer (`REJECTED`) spends an attempt on a
   possibly-flat position; a raise on the exit path reaches `_halt_on_exception`.
   Pick one deliberately and write down why — this is a fail-closed decision, not an
   implementation detail.
8. **Read cost.** Confirm the probe cannot fire per poll — only on a terminal
   observation, which ends the walk anyway — and that it goes through the same
   throttled/audited stack `LiveOrderView` already uses.

### Traps — each of these has bitten this repo before

- **NEVER match on `reject_reason` text.** Not `'Received a fill'`, not a substring,
  not a regex. The rule is already written down in the open consecutive-rejection
  decision: broker prose changes wording without notice, and a rail matching on it
  fails open the day it does. `Order.reject_reason`'s own docstring says *"Nothing
  decides on it."* Keep it that way. **The fix is state-based: ask the broker what
  exists under the identifier and whether any of it FILLED.** The prose stays
  diagnostic.
- **Do not touch the one-attempt rule.** This changes what a walk *concludes*, not
  whether an exit *retries*. It is not the open confirmation-gated-re-attempt
  decision and must not be built as a step toward it. A walk that concludes `FILLED`
  never needed a second attempt because the position is flat.
- **Do not widen the identifier-wide read anywhere else** — not into
  `DuplicateOrderGuard`, not into `_await_settled` (which already has it, scoped
  deliberately: FILLED identifier-wide, the *settle* confirmation scoped to
  `walk.broker_order_id` specifically). That asymmetry is load-bearing and documented;
  leave it exactly as it is.
- **`classify_order`'s fail-closed posture is not to be relaxed** to make this
  tidier. An unknown status counts as WORKING, on purpose.
- The probe must not turn a decided outcome into an exception on a best-effort path;
  mind `_request_cancel`'s existing "a cancel that raises must not turn a decided
  outcome into an exception" reasoning as the local precedent for tone.

## Fix 2 — the exit page drops the broker's own explanation

`_record_exit` (`execution/watchdog.py:1344-1370`) writes the Tier 1 record from
`report.to_payload()` — which includes `"walk": self.walk.to_payload()` and therefore
carries `reject_reason` — and then hand-builds a **separate six-key dict** for the
alert:

```python
self._notify(
    f"watchdog_exit_{report.outcome}",
    {
        "external_identifier": ..., "candidate_id": ..., "contracts": ...,
        "entry_credit": ..., "trigger_reasons": ..., "reason": report.reason,
    },
)
```

`walk` is not one of the six. P6 (2026-08-29) added `WalkResult.reject_reason`
specifically so *"an operator need not correlate against a separate `order.event`
record by hand"* — it landed on the entry path (which alerts the walk's own payload)
and silently missed the exit path.

On 2026-09-01 the dropped string was `'Received a fill on the original order'`: the
one sentence that would have told the operator the exit had succeeded. Fix 1 makes
this occurrence stop happening; fix 2 is what makes the *next* unfamiliar rejection
legible.

### Traps

- **`candidate_id` must stay at the TOP level of the detail dict.** `notify.alert_scope`
  (B1, 2026-08-21) reads it there to give each position its own throttle window; nested
  inside `position`, two correlated stop-outs fold into one `(+1 suppressed)`. It is
  correct today — do not reshape the dict in a way that loses it.
- **Do not spread `report.to_payload()` wholesale into the notify.** It carries the
  nested `walk` dict and the caller adds `position`; this alert goes to a phone. Add
  the field(s) an operator acts on, deliberately chosen.
- **Sweep the siblings.** `_record_exit` is not the only hand-built notify dict in
  `watchdog.py` (`watchdog_exit_unattempted`, `exit_still_unplaced`,
  `exit_spent_still_held`, `exit_unpriceable`, and the entry-side pages). Check which
  of them report a walk outcome without the broker's reason and decide, once, what a
  per-position alert owes.
- **Prefer a structural test over a convention.** This repo already pins invariants
  with AST tests; a test asserting that any alert surface reporting a walk outcome
  also carries its `reject_reason` is what stops P6's silent miss recurring on the
  next surface someone adds.

## Deliverables

1. **A design document first for fix 1** (`docs/tasks/` or inline for review),
   answering every numbered question above, with the site chosen and justified, before
   any `src/` change. Sign-off gate.
2. After sign-off: the implementation of both fixes, shipped together.
3. **A decision record** in `docs/decisions/` — dated, added to
   `docs/decisions/README.md`, with the `CLAUDE.md` entry updated: the bug, the
   identifier-versus-rung principle and where else it already applies, the answers to
   the design questions, and an explicit statement of what was NOT changed (the
   one-attempt rule, `_await_settled`'s deliberate asymmetry, the re-attempt decision).
4. **Tests**, including at minimum: a walk that reprices where the ORIGINAL fills and
   the replacement is rejected concludes `FILLED` with the right `filled_contracts`
   and the right `broker_order_id`; the same on the ENTRY policy; a replacement
   rejected with **nothing** filled under the identifier still concludes `REJECTED`
   (the fix must not swallow real refusals); the probe's own failure path; and the
   alert-surface test from fix 2.
5. **Verified mutations** — the repo's standard. At least: remove the probe, invert
   the filled/empty branch, and drop the `state.last` reassignment (this last one is
   the half-applied failure mode from design question 5 and a test must catch it).
6. Update the pass record's "Recommended order" to reflect what shipped.

## Non-negotiables this must respect

- **NEVER TOUCH THE LIVE DATABASE.** Any exploration is read-only via
  `sqlite3.connect("file:argus.db?mode=ro", uri=True)`, or through the droplet's
  `docker compose run --rm -T argus python -` pattern in the pass record. Dev runs use
  `argus.dev.toml`.
- Nothing submits an order outside `pipeline.trades_for_real`. This change sends
  nothing new — it only reads and re-labels — and it must stay that way.
- The single-legging guardrail, the no-broker-side-stops rail, and the one-exit-attempt
  rule are all untouched.
- `_await_settled`'s split stays: FILLED identifier-wide, the settle confirmation
  scoped to `walk.broker_order_id`.
- No new config key if it can be avoided; if one is unavoidable, say so early — it
  makes the deploy coupled, and there is already a coupled config deploy pending
  (`watchdog.exit_walk_min_steps`).

## Out of scope

- **The float-epsilon credit stop** and **`FillInfo.fill_price`** — same day's pass,
  separate defects, separate prompts. Do not fold them in.
- **The flatten allowance** being spent before `build_order` can succeed (finding 1 of
  the pass record).
- **`infer_direction` and the relabelled closing legs.** 2026-09-01 proved the
  condition fires and that tastytrade relabels rather than rejects; whether to change
  where direction comes from is still an open decision and is not this change.
- Deploying `watchdog.exit_walk_min_steps` — already built, waiting, separate.
- Any move toward a confirmation-gated exit RE-ATTEMPT. Explicitly not this.
