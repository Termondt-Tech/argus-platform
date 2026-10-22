# A spent exit attempt, a blinded watchdog, and a halt that stops watching (2026-08-28)

*The first day v0.3.12's greeks fix worked — 1180 candidates at an 82% rate against
52 for all of the previous day — and a broker outage drove the exit path into the
worst state it can reach. A triggered 0DTE spread went 76 minutes held, unpriced,
and with no automated path to close; a restart intended to recover it instead
parked the process, stopping the watchdog on BOTH live positions. Nothing here is
cert's fault except the trigger. Every mechanism below would behave identically in
live.*

> CLOSED — **all eight findings are answered and P0–P6 are built.** P4, P6 and P3
> landed 2026-08-29 (P3 design-first, then signed off); **P5 landed 2026-08-30**,
> the same way, and ships in `would_cancel` mode — the decision is computed and
> recorded, the outbound cancel waits on a config flip. TWO REQUIRED config keys
> between them (`[watchdog] flatten_before_close_s` from P2 and
> `[watchdog] unmanaged_order_max_s` from P5), so **the config deploys WITH the
> image**; everything else is `src/` only. See the ranked plan at the end.

## What happened

| time (ET) | event |
|---|---|
| 09:50 | session start (late; cert OAuth flaky) |
| 10:00–10:13 | 5 submit failures, 4 unreachable duplicate-guard bursts, all cert 502s |
| 10:09 | order **1424563** (`call-7765-7770`) submitted, reaches `Received`, never confirms in-market |
| 10:10 | walk cancels it → **cancel fails on a 502** → `NEVER_WORKED`, trade idea retired |
| ~10:12 | two spreads fill: `put-7670-7665`, `call-7755-7760` |
| 10:37 | `credit_multiple` stop fires on `call-7755-7760` (cost_to_close 1.60 = 2× credit) |
| 10:38 | exit walk returns `UNRESOLVED: submit failed and no order exists under this identifier` → `NOT_RESOLVED`, **one attempt spent** |
| 10:38–11:54 | position held, **not evaluated**, `watchdog_exit_spent_still_held` paging every 900s |
| 12:15 | operator restarts to clear `_attempted` |
| 12:15:48 | **process-start reconcile finds 1424563 still `working` → `unexpected_working_order` → HALT** |
| 12:15–now | process parked. `phase: idle`. **No session, no watchdog, two live spreads.** |

## Finding 1 — `UNRESOLVED` collapses two provenances that are opposites

`WalkOutcome.UNRESOLVED` is produced from two sites with contradictory epistemic content:

| site | reason | what the broker said |
|---|---|---|
| `loop.py:1113` (`_recover`) | `"submit failed and no order exists under this identifier"` | **Asked and answered: nothing working, nothing filled** |
| `loop.py:828` (`_await_in_market`) | `"order_not_visible_at_broker"` | *"not evidence it never worked — it is no evidence at all"* |

Today's exit hit the first. `_recover`'s own comment states the intent:

> Say so, and **let the caller (which can re-run the guard) decide** — that is the whole reason the guard asks the broker.

The caller does not. `watchdog.py:927` maps every non-`WALKED_AWAY` outcome to
`NOT_RESOLVED`, reasoning that a second order *"would either be refused the same
way or **stack onto something we cannot see**."* For this variant that is false:
there is nothing we cannot see, because the broker was asked and reported nothing
exists. **The one-attempt rule's entire safety argument does not apply to the case
that actually fired.**

Same defect class as A3 (`any()` over an empty list) and the 2026-08-25 relabel
finding: two different conditions compared as though they were one.

## Finding 2 — `check_once` stops EVALUATING, not just stops ACTING

```python
if key in self._exiting or key in self._attempted:
    ...
    continue          # watchdog.py:1768 — skips evaluate_triggers entirely
```

Two separable concerns are conflated:

- **"Do not send another order"** — correct, and worth enforcing.
- **"Do not price it any more"** — justified by nothing, and it is the blind spot.

The position triggered at spot 7742. Spot was 7734 an hour later — eight points
*better*, quite possibly back under its stop. `cost_to_close` went unmeasured for
76 minutes. No Argus surface could answer "what is that position worth."

**Against options-desk practice this is the most serious finding here. A monitor
must never be disabled by the failure of an action it requested.** Marking a held
position is a reporting obligation independent of the ability to trade out of it.

## Finding 3 — no time-based exit, on a 0DTE product

`TriggerReason` (`watchdog.py:370`) has exactly two members, both price-based:
`CREDIT_MULTIPLE`, `UNDERLYING_NEAR_SHORT_STRIKE`. No clock. A2 already recorded
that no EOD flatten exists anywhere in `src/`.

Gamma goes vertical into a PM settlement, and the standard control is a hard
time-based flatten (commonly 15:30–15:45 ET) **precisely because the price-based
stop may be unfillable exactly when it matters.** Today was that scenario: the
price stop fired, the exit was refused, and there is no second mechanism.

> **CLOSED the same evening (P2).** `TriggerReason.FLATTEN_BEFORE_CLOSE`, at
> `watchdog.flatten_before_close_s` before *that day's* close — 15:30 ET normally,
> 12:30 on a half-day. → [a clock is the third trigger](2026-08-28-time-based-flatten-trigger.md)

## Finding 4 — the retry budget is allocated backwards

v0.3.11 gave the **entry** duplicate-guard a bounded burst (3 attempts, 1s/2s)
because one unanswered call was ending trading days. The **exit** gets one attempt
per position for the process lifetime — no burst, no backoff.

A failed entry costs opportunity. A failed exit costs money. The path carrying real
downside has strictly less resilience than the path carrying opportunity cost.

## Finding 5 — the correct protection already exists, and `_attempted` duplicates it badly

`watchdog.py:855`:

> `guard_first_submission=True`, and it is NOT a formality... **The scenario it
> covers is a crash mid-exit: on restart the position is still open, the watchdog
> triggers again, and without the guard a second closing order would stack on the
> first.**

Deterministic identifier + broker-checked `DuplicateOrderGuard` is the right
mechanism, it is already wired into the exit path, and it is **already relied upon
for the retry-an-incomplete-exit case**. `_attempted` is a second, cruder guard
layered on top that cannot distinguish confirmed-safe from unknown — and it is the
one that fired.

## Finding 6 — the concurrency cap counts POSITIONS, not ORDERS

`RiskSnapshot.open_spread_equivalent` takes `max(ceil(broker_legs / 2),
ledger_spread_count)`. Both count filled positions. **Neither counts a working
order.** Between submit and fill an order is uncounted exposure; normally the walk
owns that window for seconds. When the walk abandons an order whose death it
cannot confirm, the window runs to the close. Had 1424563 filled, it would have
been a third spread against a cap of two, ~$2,550 against `daily_max_loss = 2500`.

## Finding 7 (NEW) — a reconciliation halt stops the watchdog on healthy positions

The restart did clear `_attempted` as intended. It never got that far:

```json
{"broker_order_id": 1424563, "disposition": "working",
 "external_identifier": "argus-open-20260828-call-7765-7770",
 "kind": "unexpected_working_order", "status": "Received"}
```

**The rail is correct.** There genuinely was a working order nobody was managing —
exactly the condition Finding 6 describes — and restart-time reconciliation is
documented as the backstop for a failed cancel. It fired as designed.

**The response is disproportionate.** A process-start mismatch parks before any
session exists, so `phase: idle`, `trading_date: null`, and **the watchdog never
starts**. Two real spreads on expiry day went from "one watched, one spent" to
**neither watched**, over one unfilled order 30 points OTM.

This also sits in tension with the kill-switch scope decision (2026-08-03), which
deliberately does NOT stop exits — but `_halt_process` requests session stop, and
no session means no watchdog, so in practice a halt does stop exits.

**The design question for next session:** should an ORDER-level discrepancy, with
no position implication, stop POSITION monitoring? A defensible split is: halt new
entries always, but let the watchdog run in read-and-exit-only mode when the
discrepancy does not concern the positions it holds.

> **ANSWERED AND BUILT 2026-08-29 (P3), and this finding's framing was wrong in
> one load-bearing way.** A reconciliation halt never *stops* a running watchdog:
> at both reconcile call sites the watchdog does not exist yet, so the halt
> prevents one from being BUILT. The fix is a `HaltGrade` on the halt, not a third
> watchdog mode and not a second stop event — `ENTRIES` keeps the kill switch
> engaged and the halt permanent while letting the scheduler run a monitor-only
> session, so held positions keep their virtual stops, their marks and the 15:30
> flatten. Earned only when there is no position discrepancy of any kind and every
> order discrepancy is one of Argus's own working OPENING orders.
> → [halt scope split](2026-08-29-halt-scope-split.md)

## Finding 8 — no mid-session follow-up on a failed cancel

`_request_cancel` (`loop.py:1002`) is best-effort by design and says the backstop is
*restart-time* order reconciliation. Nothing re-queries mid-session. Today that
left a working order live for two hours, and it is what eventually halted the
process.

> **DESIGNED 2026-08-29 (P5), signed off, and BUILT 2026-08-30.** The design
> also corrects this finding's framing: the cost is not the halt (P3 answered that)
> but a stated non-negotiable being best-effort — the kill switch promises to reach
> *"an entry order already working at the exchange"* and reaches it through this
> function. The recheck triggers on the STATE, never on the error, so a cancel that
> was accepted and stuck at `Cancel Requested` is in scope too. Opening orders get
> an automatic re-cancel on a rate bound; closing orders get a page and nothing
> else, because cancelling a close strands a position. **As shipped it registers,
> classifies and records but sends nothing** — `watchdog.unmanaged_order_cancel`
> defaults to `false`, and promoting it is a config flip.
> → [a cancel that was requested is not a cancel that landed](2026-08-29-mid-session-cancel-recheck.md)

## Immediate recovery (operational, before any code change)

1. **Cancel 1424563** — this is the discrepancy. Argus already decided to cancel it
   (`order.cancel_attempt`, 14:09:51Z); only the DELETE failed on a 502.
2. **Restart.** With the order gone, reconcile should be clean, the session starts,
   and the watchdog adopts both spreads — which also clears `_attempted` and gives
   `call-7755-7760` the fresh, guarded exit attempt the restart was for.
3. If the cancel fails, the halt persists and both positions stay unwatched. There
   is no in-process way to clear a reconciliation halt by design.

## Ranked plan

| # | Change | Effect | Status |
|---|---|---|---|
| **P0** | Split `UNRESOLVED` by provenance: broker-confirmed "no order exists" → retryable; unknown stays `NOT_RESOLVED`. | Smallest change, largest effect. The guard remains the double-close protection. | **BUILT** |
| **P1** | Keep evaluating triggers on a spent position; suppress only the submission. | Removes the blind spot. Monitoring and action become separable. | **BUILT** |
| **P2** | Time-based flatten trigger (`[watchdog] flatten_before_close_s`, 1800s = 15:30 ET / 12:30 on a half-day). | The only structural answer to "the stop fired and could not be filled." | **BUILT** — [decision](2026-08-28-time-based-flatten-trigger.md) |
| **P3** | Scope the halt: order-level discrepancies with no position implication should stop ENTRIES, not the watchdog. | Held positions keep their stop during a halt. | **BUILT** 2026-08-29 — [decision](2026-08-29-halt-scope-split.md) |
| **P4** | Count working orders toward `max_concurrent_positions`. | Closes the uncounted-exposure window. | **BUILT** — [decision](2026-08-29-count-working-orders-toward-concurrency-cap.md) |
| **P5** | Mid-session re-query after a failed cancel. | Catches Finding 8 before it becomes tomorrow's halt. | **BUILT** 2026-08-30, in `would_cancel` mode — [decision](2026-08-29-mid-session-cancel-recheck.md) |
| **P6** | Thread `reject_reason` onto `WalkResult`/alerts; give `TimeoutError` a non-empty message. | Diagnosis from the phone rather than a DB query. | **BUILT** 2026-08-29 — see below |

## What P0 and P1 actually are

Both are `src/` only. **No new config key** — the image deploys alone, unlike the
last three passes.

### P0 — `WalkOutcome.NO_ORDER_EXISTS`

The one site that produced it is `_recover`'s final return (`loop.py`), the branch
whose own comment already said the caller should decide. It now says so in the type
rather than in a reason string, and `_after_walk` maps it to a new
`ExitOutcome.NOTHING_AT_BROKER` **ahead of** the `is not WALKED_AWAY` catch-all.

Three things were deliberately NOT done:

- **It is not folded into `NOT_ATTEMPTED`**, which the plan above suggested. That
  would have been the smaller edit and it collapses the same kind of distinction
  this finding is about: `NOT_ATTEMPTED` never touched the broker and may repeat
  every cadence tick forever, while this one costs a SUBMISSION each time. A broker
  failing every send while answering every read would have turned an unbounded retry
  into ~1,500 order requests over the 76 minutes this actually ran — the runaway
  `ratelimit.py` exists to cap, not the volume it is designed for.
- **So the retry is budgeted**: `_EXIT_SEND_ATTEMPTS = 3`, per position, per
  process. Not config, on `entry.py`'s `_GUARD_ATTEMPTS` precedent — the burst's
  SHAPE mirrors v0.3.11's entry guard, and the numbers an operator tunes are config.
  Three rather than one is finding 4 answered directly.
- **The one-attempt rule is untouched for everything else.** Nothing that reached
  the broker gained an attempt. The retry's licence is `guard_first_submission=True`
  re-asking the broker, never this outcome's own answer — and the exhausted budget
  lands in `_attempted` exactly as before.

On the ENTRY path the split changes nothing: `NO_ORDER_EXISTS` was added to
`_ALERTING_OUTCOMES` so the page still fires, with a truer name. Leaving it out
would have silently dropped an alert entry has always sent.

### P1 — evaluate, never act

`check_once` now skips only `_exiting`. A position in `_attempted` is evaluated,
recorded (`watchdog.check_spent`, Tier 2), and reported — it simply never reaches
`_trigger`, so no exit task is ever created. Refusing to act and refusing to look
were the same branch; only the first was ever argued for.

The visible half is the **mark**. `_SpentMark` holds the last evaluation, written
by one method from the same `TriggerEvaluation` that drives the telemetry, and both
readers take it from there:

- `watchdog.exit_spent_still_held` (the repeating page) now carries `cost_to_close`,
  `credit_stop`, `spot` and `still_triggered`.
- `/status`'s exit-attempts row gains a second line — `mark: 0.70 vs stop 1.70 —
  back inside the stop`, or `mark: unpriced`. Never a placeholder number: on a row
  whose purpose is telling a human whether to leg out by hand, a 0.0 cost-to-close
  is not a missing value but a wrong one, and it is the kind that gets acted on.

`still_triggered=False` is the state worth having. The position that went 76 minutes
unpriced had spot come back eight points in its favour, quite possibly back inside
its stop, and no surface could say so.

**One consequence to expect in the trail:** a spent position is evaluated again, so
it can now also raise `watchdog.unevaluable` / `watchdog_position_unevaluable`. That
is a second page on a position already paging `exit_spent_still_held`, and it is
correct — they say different things ("nothing will close this" and "nothing can even
price this"), the escalation is rising-edge, and B1's throttle scopes both per
position.

### What is still true after both

The state that produced this record is now recoverable rather than terminal, but
**nothing in P0/P1 closes findings 3, 6, 7 or 8.** In particular: had the exit still
failed after three sends, the position would sit exactly where it sat — held,
triggered, no automated path to flat — only visible. **P2 was the finding with no
mitigation at all**, and it is the one that does not depend on the broker
cooperating.

### P2 — the clock, and the one place the spent-attempt rule bends

Finding 3, closed the same evening and recorded in full at
[a clock is the third trigger](2026-08-28-time-based-flatten-trigger.md).
`TriggerReason.FLATTEN_BEFORE_CLOSE` fires `watchdog.flatten_before_close_s` before
**that day's** close — the offset is applied to `MarketCalendar.close_time(d)`, so
15:30 ET normally and 12:30 on a half-day, because a literal 15:30 would fall after a
half-day's settlement and never fire at all.

Four things in it are decisions rather than mechanics, and the last is the one that
touches this record's findings 4 and 5 directly:

- **It changes WHEN, never HOW.** The flatten hands off to the same
  `ExitExecutor.close`, the same guarded walk, the same walk-away escalation and the
  same width-capped cross. Going straight to the cross was rejected: `_cross`
  deliberately submits unguarded because a walk ran in front of it, and the walk costs
  seconds against 1,800s of runway.
- **It does not invent a price.** The one number that survives a dead quote feed is
  the spread WIDTH, and paying the width guarantees the maximum loss where holding
  realizes at most it — so an unpriceable flatten sends nothing, keeps its allowance
  and retries, exactly like any other unpriceable trigger. That is P2's honest limit.
- **It bypasses the debounce, explicitly.** `sustained_s` filters a quote artifact; a
  clock reading has none.
- **A position whose attempts are spent gets ONE further send, and only past the
  mark.** This is finding 4's imbalance and finding 5's observation applied to one
  cell rather than uniformly: `_attempted` answers "does a conflicting order exist?"
  from memory and never re-asks, while `DuplicateOrderGuard` — which the exit walk
  already runs with — asks the broker at the moment of the send and blocks on
  WORKING, on FILLED, and on an order it cannot read, raising if it cannot ask. It
  permits only DEAD, which is the one state where a second closing order is provably
  not a double close, and the only state this unblocks. Tier 1 as
  `watchdog.flatten_override`.

**It does not close findings 6, 7 or 8**, and finding 7 in particular now costs more:
a process-start halt parks before any session exists, so no watchdog — and therefore
no flatten either. A control that only exists while a session runs inherits every way
a session can fail to exist, which is a further argument for P3.

## P6 — the rejection reason, on the surface that already carries reasons (2026-08-29)

`src/` only, no new config key, no deploy coupling. Built directly under the weekend
P3–P6 authorization alongside P4.

**The gap.** v0.3.12 (Eltanin) already mapped the SDK's `PlacedOrder.reject_reason`
onto `Order.reject_reason` and promoted `order.event` to Tier 1 on an order's first
terminal `dead` sighting — so the broker's prose *is* recorded. But `WalkResult`,
which is what `_report` (`entry.py`) and `ExitExecutor` (`watchdog.py`) actually
hand to `entry.finished` / `entry_rejected` / `ExitReport`, never carried it. Every
other way a walk can end puts its explanation in `WalkResult.reason`; a rejection
put a fixed string there (`"broker rejected the order"` on a mid-walk sighting,
`ack.errors` on the first submit) and left the broker's own words in a *different*
record an operator would have to find and join by hand.

**The change.** `WalkResult` gains `reject_reason: str | None`. `_finish` populates
it from `state.last.reject_reason` **only when `outcome is WalkOutcome.REJECTED`**,
`None` otherwise — the same None-vs-`""` / None-vs-`0.0` discipline the P2 record
used for `cost_to_close`. `to_payload()` carries it, so it reaches both alert
payloads and both Tier 1 records with no change at either call site (`entry.py`'s
`_report` already spreads `**result.to_payload()`; `ExitReport.to_payload()` already
nests `self.walk.to_payload()`). Nothing decides on the string — it is broker prose,
it changes wording without notice, and `Order.reject_reason`'s own docstring already
bans a rail built on it.

**Which outcomes carry it.** Only `REJECTED`. Considered and rejected: also
populating it on `CANCELLED_ELSEWHERE` (a terminal-at-broker sighting that is not a
rejection) — but that order was not rejected, its `reject_reason` is `None` anyway,
and widening the guard invites a future `Order` state that sets both `status` and
`reject_reason` to leak the field onto a non-rejection outcome. The `is REJECTED`
guard is the cheap correct scope.

**The `TimeoutError` half.** `grep TimeoutError src/argus/execution/loop.py` is still
empty — the walk state machine raises `BrokerUnavailable` with a populated message on
every give-up path. The messageless one on this path is in `entry.py`'s
`_check_for_duplicate`: `async with asyncio.timeout(timeout_s)` raises a bare
`TimeoutError` (empty `str()`), which the burst loop catches under `except Exception`
and reports through `_note_guard_attempt` and `_GuardUnreachable.last` — so
`entry.guard_unreachable_attempt` / `entry.guard_unreachable_abandoned` logged
`error_type: "TimeoutError"` with `error: ""`. Now a dedicated `except TimeoutError`
re-labels it — **same type**, because `error_type` readers (every existing audit
query for this condition) match on the string `"TimeoutError"` and nothing matches
on the instance — with `f"duplicate-guard check for {identifier} did not complete
within {timeout_s:g}s"`. A `TimeoutError` that already carries a message (an httpx
one that somehow reached here as the builtin) is passed through untouched.

**What was NOT done.** The `BrokerUnavailable` messages in `_observe` / `_recover`
already name the attempt count and the order/identifier but interpolate `{exc}`,
which is empty for a bare `TimeoutError` — left alone, because those paths raise
`BrokerUnavailable`, not `TimeoutError`, the message is not empty, and the exception
that lands there is an SDK transport error with its own `str()` far more often than
it is a bare builtin.

**Tests.** `test_a_rejection_carries_the_brokers_reason_onto_the_walk_result` (the
reason reaches `WalkResult` and `walk.finished`),
`test_a_non_rejected_outcome_carries_no_reject_reason` (a filled walk carries
`None`, not `""`), and `test_the_guard_check_is_bounded_by_its_OWN_timeout` gained an
assertion that the abandoned-burst record's `error` names the wait. `FakeOrderExecution.reject_next`
gained an optional `reason` argument mirroring `map_placed_order`.

## What did work, and should not be lost in the noise

- **v0.3.12's greeks fix is validated**: 1180 candidates / 82% rate, `greeks_age_s`
  of 43–48s on strikes that filled, versus 52 candidates the whole previous day.
- **A2/A2b** are the only reason the spent position was visible at all — the
  repeating page and the `/status` field both worked exactly as designed.
- **v0.3.11's guard retry** absorbed 13 unreachable bursts, abandoning only 3, never
  reaching the streak limit that ends the day.
- **B1's per-position throttle** kept 151 `exit_spent_still_held` records from
  collapsing into one alert.
- **The reconciliation rail caught a real unmanaged working order.** Its verdict was
  right; only its blast radius is in question.
