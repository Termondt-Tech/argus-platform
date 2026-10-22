# Design — a terminal rung is not a verdict about the spread

**Status: SIGNED OFF AND BUILT, 2026-09-02.** Implemented as designed, with one
addition the design did not anticipate: the fix-2 AST sweep found a THIRD alert
surface with the same gap (`watchdog_flatten_override`, which names `spent_outcome`),
and it carries the reason too. Outcome record:
[a terminal rung is not a verdict](../decisions/2026-09-02-a-terminal-rung-is-not-a-verdict.md).
Prompt: [`argus_reprice_rejection_identifier_wide_prompt.md`](argus_reprice_rejection_identifier_wide_prompt.md).
Incident: [the second v0.3.13 day](../decisions/2026-09-01-second-live-fire-day.md).

## Recommendation in one line

**A walk that concludes about a SPREAD must ask about the IDENTIFIER, not about the
rung it happens to be holding** — so `_resolve`'s terminal-non-fill branch asks the
broker what exists under `external_identifier` and whether any of it FILLED, exactly
as `_await_settled` and `_recover` already do, before it concludes `REJECTED` or
`CANCELLED_ELSEWHERE`.

**No new config key.** `src/` only; the image deploys alone. It does not ride the
coupled `watchdog.exit_walk_min_steps` deploy and does not create a second one.

**Nothing new is submitted.** The change adds exactly one broker READ, on a path that
was already terminating, and re-labels a verdict. `pipeline.trades_for_real` is
untouched, the one-exit-attempt rule is untouched, `_await_settled`'s deliberate
FILLED-wide / settle-narrow asymmetry is untouched.

---

## The eight design questions

### 1. Where does the probe belong? — `_resolve`'s terminal branch. One site.

The two candidate sites are not equivalent, and the asymmetry is coverage.

**`_resolve`'s terminal branch is ONE site that catches every route.** There are
exactly two ways a walk observes a terminal order, and both funnel through
`_resolve(state, observed)`:

- `_await_in_market` (`loop.py:930-932`) — `if observed.is_terminal: return await
  self._resolve(state, observed)`. This is the route the incident most likely took:
  a replacement that comes back `Received` and is refused a moment later is seen by
  the poll that is waiting for it to reach the market.
- the main cycle's step (3) — `observed = await self._observe(state)` then
  `resolved = await self._resolve(state, observed)`.

**`_reprice`, inspecting `replaced.status` after the ack, is narrower and would need
a second site anyway.** It only catches the rejection when the broker returns it
*synchronously in the replace response*. If the replace acks `Received` and the
refusal lands on the next poll — which is what the sequencing of the 18:42:12 records
is consistent with, and is in any case a shape the broker is free to choose from one
day to the next — `_reprice` sees a clean ack and the bug survives untouched. Covering
the incident from `_reprice` therefore means covering `_resolve` as well: two sites,
two copies of the same question, and the copy in `_reprice` firing on the ack path is
the one that would rot first because it is the one with no other reason to exist.

There is also a structural argument, and it is the stronger one. The defect is a
**rung-scoped conclusion about an identifier-wide fact**. `_resolve` is where the
conclusion is drawn; `_reprice` is where a rung is minted. Putting the correction at
the mint site fixes one way of arriving at the wrong conclusion and leaves the
conclusion itself as wrong as it was.

**Read cost is bounded at one probe per `run()`** — see question 8.

`_first_submit`'s own `REJECTED` branch (`loop.py:898-904`) is deliberately NOT
included, and the reason is not squeamishness about cost: **`DuplicateOrderGuard` has
just asked the broker that exact identifier-wide question and permitted the send.**
Both production callers run it — `entry.submit` immediately above the boundary
(`entry.py:104-117`), `ExitExecutor.close` via `guard_first_submission=True`
(`watchdog.py:1005`) — and the guard BLOCKS on `FILLED` (`guard.py:122-124`). A second
identifier-wide read milliseconds later can only repeat an answer the walk already
acted on.

### 2. Which dispositions get it? — the whole terminal-non-fill branch, `CANCELLED_ELSEWHERE` included.

The probe goes **above** the `REJECTED` / `CANCELLED_ELSEWHERE` split, not inside
either arm:

```python
if observed.is_terminal:
    if classify_order(observed) is OrderDisposition.FILLED:
        return await self._finish(state, WalkOutcome.FILLED)     # unchanged
    <probe here>
    if observed.status is OrderStatus.REJECTED: ...
    ...CANCELLED_ELSEWHERE...
```

`REJECTED` is what was observed. `CANCELLED_ELSEWHERE` has the identical shape and a
plausible mechanism sitting right next to the observed one: `replace` **cancels the
original at the broker** (measured, chunk 1 — 1253729 → 1253731, old order left
`Cancelled`), so a broker that resolves the same race by cancelling the *replacement*
instead of rejecting it produces `Cancelled` on the current rung with a filled
original underneath. Same spread, same flat position, same spent exit attempt, same
false page — and `CANCELLED_ELSEWHERE` on the exit path lands in exactly the same
`ExitOutcome.NOT_RESOLVED` bucket (`watchdog.py:1084-1096`). `EXPIRED` on a 0DTE rung
at the close is a third arrival at the same place.

Excluding it would mean asserting that a fill on another rung matters when this rung
says `Rejected` and does not matter when it says `Cancelled`, which is not a
distinction anything downstream draws. **The FILLED branch needs nothing** — it
already short-circuits when *this* rung filled, and a rung that filled is the answer
the probe would go looking for.

**Not extended to the cancel-and-finish paths** (`WALKED_AWAY`, `DRIFTED`, `ABORTED`,
`NEVER_WORKED`). Those are non-terminal observations, the probe would then fire on
every ordinary walk-away, and on the exit path `_await_settled` already asks precisely
this question one frame later with the identifier-wide FILLED check intact. Stated as
a considered non-extension, not an omission.

### 3. Gated on `state.steps > 0`? — **No.** Deliberately ungated.

Three reasons, in ascending order of weight.

1. **The premise of the gate is false.** The broker does **not** enforce
   `external_identifier` uniqueness (measured, chunk 1, and the fake deliberately
   mirrors that — `fake.py:15-19`). A walk at step 0 is not a walk with one order
   under its key.
2. **There are live paths that produce exactly that.** `_recover` resuming against an
   order a "failed" submit actually landed leaves `steps == 0` with a second row under
   the key; P0's exit retry (2026-08-28) re-runs a whole walk under the *same*
   `argus-close-<candidate>` identifier, so an earlier walk's rungs are still reported
   under it for the rest of the 24h window `get_live_orders()` covers.
3. **The gate repeats the shape of the bug.** The defect is that a rung-scoped fact
   was used to answer an identifier-wide question. Gating the identifier-wide question
   on a rung-scoped counter re-introduces the same category error one level up, and it
   would be defended with the same sentence that was wrong the first time ("there is
   only one order here").

The cost of not gating is one broker read on a walk that ended terminal-and-unfilled
at step 0 — a case that barely exists, because a refusal at the *first* submit is
handled in `_first_submit` and never reaches `_resolve` at all.

### 4. Reuse or extract? — extract the SELECTION, write the READ narrow.

`_recover` wholesale is wrong here for every reason the prompt lists — it records a
disconnect, increments `state.recovered`, reconnects and backs off, writes
`walk.disconnected`, phrases its reasons as "recovering from {what}", and carries the
`len(working) > 1` cancel-everything decision. None of that belongs on a path where
nothing failed.

So: a new narrow probe method, plus **one extracted module-level helper** shared with
`_recover`:

```python
def _first_filled(candidates: list[Order]) -> Order | None:
    for order in candidates:
        if classify_order(order) is OrderDisposition.FILLED:
            return order
    return None
```

`_recover`'s `filled[0]` becomes `_first_filled(candidates)` — **behaviour-identical**,
same order, same choice — so "which order counts as having left a position" has one
implementation and cannot drift between the two paths that ask it. `_recover` keeps
its own `working` list and its own decisions; nothing else about it changes.

**Why "first" and not "the largest fill" or a sum.** The question this asks is a
boolean — *did this trade idea leave a position?* — and the chosen order supplies only
`broker_order_id` and `filled_contracts` **for the record**. It is not authoritative
about size and never was: `WalkResult` is a report, not a statement about what is held
(module docstring, property 3), and `positions` — written by `FillConsumer` off the
fill feed — is what is authoritative. A walk that filled 1 on the original and 1 on
the replacement still reports one rung's count; that is finding 5 of the 2026-09-01
pass, it is unchanged by this fix, and it is unchanged **on purpose** rather than
quietly half-fixed here.

### 5. `state.last` and `state.broker_order_id` move to the filled order. `state.sent` does NOT.

The two that move, exactly as `_recover` does it:

```python
state.last = filled
state.broker_order_id = filled.broker_order_id
```

Without both, the outcome would read `FILLED` while `filled_contracts` stayed `0` and
`broker_order_id` still named the rejected rung — the half-applied failure the prompt
names, and harder to spot than the original bug because the headline field is right.
A mutation covers it (§Mutations).

**`state.sent` deliberately does not move**, and the reason is the defect one door
over. `_finish` reads `final_limit_price` off `state.sent`, so moving it would relabel
the walk's final limit as the *filled rung's* limit — dressing one limit up as more
fill-like than another. **A limit is not a fill price.** That is precisely the
`entry_credit`-is-the-limit finding this same pass sharpened to n=8 with a realized
consequence, and the honest reading is the one already in force: the walk stood behind
0.45 last, the rung that transacted is named by `broker_order_id`, and what it
actually cost is `order.fill`'s business. It also keeps `_recover`'s filled branch and
this one byte-identical in what they touch.

### 6. What `walk.finished` says afterwards — the prose rides in `reason`, and a Tier 2 record makes the rate countable.

The outcome becomes `FILLED`, so `_finish` correctly leaves `reject_reason` at `None`:
**`WalkResult.reject_reason`'s documented contract — "present only on a `REJECTED`
outcome and `None` otherwise (never `""`)" — is not widened.** A field whose whole job
is to be present on one outcome must not start appearing on another.

The rejection is carried two ways instead:

- **`reason`**, composed at the probe, e.g.
  `order 1430433 came back Rejected, but 1430426 under this identifier is FILLED: Received a fill on the original order`.
  `reason` reaches every surface that reads a walk outcome — `walk.finished`,
  `entry.finished`, `entry_*` pages, `ExitReport.reason`, `desk` — so the correlation
  the prompt worries about being left to the reader is written into the sentence the
  reader already gets.
- **A Tier 2 `walk.filled_under_identifier` record** carrying both broker order ids,
  the terminal status, and the broker's prose as fields. `reason` is prose and prose
  is not countable; the headline number in the incident is a RATE ("1 of 3 replaces,
  33%, correlated by construction") and after this ships the only way to keep
  measuring it is a structured record. Tier 2 because it is telemetry about a walk
  that is about to write its Tier 1 `walk.finished` in the next statement, and
  `walk.step` / `walk.holding` are the precedent.

`order.event` keeps recording the rejection independently, unchanged.

### 7. Probe failure — **fall back to the pre-existing verdict**, loudly. Fail-closed by construction.

If `by_identifier` raises (including `UnmappableOrderError`, which is how an
unreadable order under this key arrives), the walk concludes what it would have
concluded before this change: `REJECTED` / `CANCELLED_ELSEWHERE`, with a note appended
to `reason` saying the identifier-wide check could not be answered.

**Why that is the fail-closed direction and not a soft option.** The probe is
*strictly additive*. Before it runs, the walk already holds a **positive terminal
observation the broker gave it** for this rung, obtained one call earlier. The probe
can only ever improve that verdict by discovering a fill. A read failure discovers
nothing — so falling back is not "guessing", it is declining to manufacture a new
epistemic state out of a read that did not happen. The alternative (escalate to
`BrokerUnavailable` → `UNRESOLVED`) would *destroy* information the broker positively
supplied and, on the exit path, lands in the identical `ExitOutcome.NOT_RESOLVED`
bucket anyway — the same spent attempt, with a worse record.

Check both directions of being wrong:

- **The position really is flat and we fall back to `REJECTED`.** The attempt is spent
  on a position that no longer exists; the ledger drops the row and nothing pages.
  Cost: zero, and it is today's behaviour.
- **The position really is open and we fall back to `REJECTED`.** Correct — nothing
  filled, the refusal is real, and A2's repeating `exit_spent_still_held` escalation is
  what carries it from there.

`_request_cancel`'s rule is the local precedent and it applies verbatim: *a call that
raises must not turn a decided outcome into an exception.*

**Recorded, not swallowed.** `_record_disconnect(what="terminal_probe")` — the existing
Tier 1 shape for "a broker call failed mid-walk", so no new category — plus
`state.recovered += 1`, which is the honest accounting and lights the existing
`entry_recovered_disconnects` page.

**One attempt, no retry, no reconnect.** `_observe`'s retry ladder exists for reads the
loop **cannot continue without**; this one's absence is a no-op. Adding
`max_recovery_attempts` here would put up to 1.5s of `_RECOVERY_BACKOFF_S` sleeping on
the *exit* path's terminating branch — a path `_run_exit` is awaiting while a
triggered 0DTE position is open — to buy a second look at a question whose failure
costs nothing new. The `walk.disconnected` record with `call="terminal_probe"` makes
the failure rate a one-line query, so this is a number to measure rather than a guess
to defend; if it turns out to be common, adding the ladder is one line with evidence
behind it.

### 8. Read cost — at most ONE extra `get_live_orders()` per `run()`, through the same governed stack.

- It fires **only** from the terminal branch, which unconditionally ends the walk on
  the next statement. It cannot fire per poll or per cycle.
- Both routes into that branch (`_await_in_market`, the main cycle) return the
  `_resolve` result immediately, so there is no path that probes twice in one `run()`.
- It goes through `self._view` → `LiveOrderView.by_identifier` →
  `AuditedOrderExecution.get_live_orders` → `ThrottledOrderExecution` → the adapter
  (`pipeline.py:98-99`) — the same throttled, audited stack the duplicate guard and
  `_recover` already use, under `execution/ratelimit.py`'s process-wide governor.
- Upper bound on the day: one read per walk that ends terminal-and-unfilled. On
  2026-09-01 that would have been **one**.

---

## The traps, checked

- **No `reject_reason` matching, anywhere.** The probe's entire input is
  `classify_order` over what the broker says exists under the identifier. The prose is
  read only to be *quoted into* `reason`. `Order.reject_reason`'s docstring — *"Nothing
  decides on it"* — still holds after this change, and a test pins that the probe's
  call graph touches no string comparison against it.
- **The one-attempt rule is untouched.** This changes what a walk *concludes*, not
  whether an exit *retries*. A walk that concludes `FILLED` never needed a second
  attempt because the spread is flat. `_RETRYABLE_EXITS`, `_EXIT_SEND_ATTEMPTS` and
  `_attempted` are not edited.
- **`_await_settled` is not touched.** Its FILLED-wide / settle-narrow-to-
  `walk.broker_order_id` split stays exactly as documented. This change adds the wide
  FILLED question at a *different* site; it does not widen the settle confirmation
  anywhere.
- **`DuplicateOrderGuard` is not touched.**
- **`classify_order`'s fail-closed posture is not relaxed.** The probe consumes it
  as-is: unknown status is `WORKING`, "cannot tell" is `FILLED`, and a `FILLED` with an
  unreported quantity yields `filled_contracts=None` — which `WalkResult` already
  documents as distinct from 0.
- **No decided outcome becomes an exception.** Question 7.

## What this is NOT

- Not a step toward the open confirmation-gated exit **re-attempt** decision, and it
  must not be cited as one.
- Not a fix for `filled_contracts` under-counting a walk that filled across two rungs
  (finding 5). Unchanged, deliberately, and stated in the record.
- Not `FillInfo.fill_price`, not the float-epsilon credit stop, not the flatten
  allowance, not `infer_direction`.

## Consequence to state loudly

**The open decision about backing off on the COUNT of consecutive terminal rejections
must never count one of these.** "Your order filled" is not a broker refusing to
trade, and before this change it was recorded as one — on the entry path, for a trade
idea that opened a position. After this change such a walk records `FILLED` and never
reaches a rejection counter at all. That is the second reason this fix goes in before
that decision is built, not after.

---

## Fix 2 — the page drops the broker's own explanation

One key on `_record_exit`'s hand-built dict, plus one sibling, plus a structural test.

**What a per-position alert owes, decided once:** *an alert that names the outcome of
an exit attempt carries the broker's reason for that outcome.* The kind already names
the outcome (`watchdog_exit_not_resolved`); without the reason the operator gets a
category and a constant.

- **`_record_exit`** (`watchdog.py:1359-1370`) gains `"reject_reason"`, read from
  `report.walk.reject_reason` (`None` when there is no walk or no rejection). One key,
  as finding 2 says. `report.to_payload()` is **not** spread in — it carries the nested
  `walk` dict and this goes to a phone.
- **`_page_if_spent_and_still_held`** (`watchdog.py:3200-3217`) gains the same key. It
  is the *repeating* page for a position nothing will close, it names
  `exit_outcome`, and it is the page most likely to be the one an operator actually
  reads — the first one can be throttled or missed. `_SpentAttempt` gains an optional
  `reject_reason` field to carry it (constructed in one place, `watchdog.py:3009`).
- **`candidate_id` stays at the top level** of both dicts. B1's throttle scope
  (`notify.alert_scope`) reads it there; it is correct today and nothing reshapes it.
- **The siblings that already carry it need no change** and the sweep says why:
  `watchdog_exit_unattempted`, the `_NOTHING_SENT` / `_CROSS_UNPRICEABLE` retry pages
  and `watchdog_exit_still_unplaced` all build from `**report.to_payload()`, which
  carries `walk` → `reject_reason`. `watchdog_cross_refused` and
  `watchdog_crossed_market` report no walk outcome. Every entry-side page passes
  `payload`, which spreads `**result.to_payload()` — `reject_reason` at the top level,
  which is where P6 put it and why the entry path was already right.
- **`/status` is not changed.** Naming a residual or widening the spent-attempt row is
  the separately named follow-up; this is the alert surface.

### The structural test, because a convention is what failed here

P6 added the field and missed a surface silently. The test parses `watchdog.py` and
`entry.py` and asserts: **for every `self._notify(...)` / `self._alert(...)` whose
alert kind or whose detail-dict keys name an outcome, the detail carries
`reject_reason`** — literally, or by `**`-unpacking a payload that does (resolving a
local `detail = {...}` assignment to its dict). Plus a runtime assertion that
`ExitReport.to_payload()["walk"]` really does contain `reject_reason`, so the `**`
escape hatch cannot rot into a false pass.

A new hand-built per-position alert that names an outcome and forgets the reason fails
CI instead of shipping.

---

## Tests

In `tests/test_execution_loop.py` unless noted.

1. **The incident, exactly.** An exit walk reprices once; the ORIGINAL fills 2; the
   replacement comes back `Rejected` with `'Received a fill on the original order'`.
   Asserts `FILLED`, `filled_contracts == 2`, `broker_order_id ==` the original's,
   `last_status is FILLED`, `reject_reason is None`, and that `reason` and the Tier 2
   record both name the rejected rung and quote the prose.
2. **The ENTRY mirror**, same shape on the entry policy — `outcome='rejected'` for a
   trade idea that opened is the record-corruption half.
3. **A real refusal still refuses.** Replacement rejected, **nothing** filled under the
   identifier → still `REJECTED`, `filled_contracts == 0`, `reject_reason` present.
   The fix must not swallow refusals.
4. **`CANCELLED_ELSEWHERE` gets the same treatment** — replacement `Cancelled`,
   original filled → `FILLED`; replacement `Expired`, nothing filled →
   `CANCELLED_ELSEWHERE`.
5. **Probe failure falls back.** `by_identifier` raises → `REJECTED`, with the note in
   `reason`, `walk.disconnected` at Tier 1 with `call="terminal_probe"`,
   `recovered_disconnects == 1`, and **no** exception out of `run()`.
6. **`UnmappableOrderError` under the identifier** takes the same fallback.
7. **Ungated at step 0** — a terminal-unfilled rung at step 0 with a filled sibling
   under the identifier still concludes `FILLED`.
8. **One read, not a poll.** The probe adds exactly one `get_live_orders()` call to a
   walk, and zero to a walk that fills or walks away normally.
9. **`_recover` is unchanged** — the existing
   `test_recovery_reports_a_fill_discovered_while_disconnected` and the two
   working-order tests still pass with `_first_filled` extracted.
10. **Exit surface** (`tests/test_watchdog_exit.py`): a rejected exit's alert carries
    the broker's prose; the repeating spent page carries it too; plus the AST test.

## Mutations to verify (each must fail the suite)

1. Remove the probe entirely → today's behaviour → tests 1, 2, 4, 7 fail.
2. Invert the branch (conclude `FILLED` when the identifier has **no** fill) → test 3
   fails.
3. Drop the `state.last` reassignment, keep the outcome → test 1 fails on
   `filled_contracts`/`last_status` (the half-applied failure of question 5).
4. Drop the `state.broker_order_id` reassignment → test 1 fails on the id.
5. Let the probe's exception propagate instead of falling back → test 5 fails.
6. Re-add a `state.steps > 0` gate → test 7 fails.
7. Drop `reject_reason` from `_record_exit`'s dict → the AST test and test 10 fail.

## Deliverables after sign-off

Implementation of both fixes together; a decision record in `docs/decisions/` dated
2026-09-02, added to `docs/decisions/README.md`; the `CLAUDE.md` entry; and the pass
record's "Recommended order" updated to say what shipped.
