# A terminal rung is not a verdict about the spread (2026-09-02)

**DESIGNED, SIGNED OFF AND BUILT the same day. `src/` only, no new config key — the
image deploys alone and does not join the coupled deploy.** Two fixes, shipped
together, because they are one incident seen from two surfaces: a walk that reprices
concluded about one rung, and the page that would have explained it dropped the
broker's own sentence.

> **Superseded in LOCATION, not in substance, on 2026-09-10.** The probe this record
> built moved out of `_resolve` and into `_finish`, gated on the OUTCOME rather than on
> the terminal branch, because the same rule turned out to be applied one branch short
> here too. `_finish_if_filled_elsewhere` no longer exists; its body is
> `loop._finish`'s probe block and every contract below is unweakened. Read this record
> the WHY and → [a rung that never worked is not a verdict
> either](2026-09-10-never-worked-is-not-a-verdict.md) for where the code is now.

Incident: [the second v0.3.13 day](2026-09-01-second-live-fire-day.md).
Design gate: [`argus_reprice_rejection_identifier_wide_design.md`](../tasks/argus_reprice_rejection_identifier_wide_design.md).
Prompt: [`argus_reprice_rejection_identifier_wide_prompt.md`](../tasks/argus_reprice_rejection_identifier_wide_prompt.md).

## The bug

`put-7615-7610`'s exit triggered on `underlying_near_short_strike` at 13:42 ET on
2026-09-01. The walk opened order **1430426**, repriced once to **1430433**, and the
broker refused the replacement:

```
618797 18:42:12 order.event    1430433  reject_reason 'Received a fill on the original order'
618798 18:42:12 order.fill     argus-close-…put-7615-7610  Buy to Close  2  7615  4.50
618800 18:42:12 order.fill     argus-close-…put-7615-7610  Sell to Close 2  7610  3.00
618801 18:42:12 walk.finished  1430433  outcome 'rejected'  filled_contracts 0  steps 1
```

**The exit had succeeded.** 2 contracts, net debit 1.50, spread flat. It was recorded
`REJECTED` → `ExitOutcome.NOT_RESOLVED`, spent the position's one exit attempt, paged
the operator, and wrote `filled_contracts: 0` for a spread that filled 2.

`_reprice` moves `state.broker_order_id` and `state.last` onto the replacement as
soon as the replace is *acknowledged* and never inspects `replaced.status`. The next
`_observe` read the replacement, found it terminal, and `_resolve` took the verdict
**rung-scoped**. `_recover` asks the identifier-wide question and was unreachable:
it is the exception handler, and the replace succeeded.

**Rate: 1 of 3 successful replaces that day, and the two events are correlated by
construction** — a reprice goes out exactly when a fill is imminent.

## The principle, and where it already applied

`CLAUDE.md`, as a non-negotiable, on the market-crossing escalation:

> The FILLED check alone stays identifier-WIDE, deliberately — a fill on any rung
> means the spread is flat.

Argus already implemented that rule in **two** places and needed it in **three**:

| Site | Asks the identifier | Why it was there |
|---|---|---|
| `_await_settled` (`watchdog.py`) | ✔ since 2026-08-21 | authorizing a SECOND closing order |
| `_recover` (`loop.py`) | ✔ since chunk 3 | a mutation failed; what exists? |
| `_resolve`'s terminal branch | ✘ **until now** | concluding what the walk DID |

The gap is stated in one sentence: **a terminal non-fill on a REPLACEMENT is exactly
as much a "the broker may know something this rung does not" moment as a raised
exception is, and only one of the two was asking.**

The entry mirror shares `_reprice`. An entry walk whose original fills as the replace
goes in recorded `outcome='rejected'` for a trade idea that **opened a position**.
Never a position-safety hole — `positions` is written from the fill feed by chunk 2,
independently of the walk (`entry.py:782-788`) — but it mis-scores `desk`/`mark`,
pages `entry_rejected`, and skips the watchdog's look-sooner nudge.

## Fix 1 — the probe

`_resolve`'s terminal-non-fill branch asks `by_identifier` and, if any order under
the key is `FILLED`, concludes `FILLED` about the spread instead of `REJECTED` /
`CANCELLED_ELSEWHERE` about the rung. `_finish_if_filled_elsewhere` carries the
argument in full; the answers to the design questions are below.

**1. Site: `_resolve`, one place.** Both routes to a terminal observation
(`_await_in_market`'s poll and the main cycle's step 3) funnel through it.
`_reprice` inspecting `replaced.status` after the ack is narrower — it catches the
refusal only when the broker returns it *synchronously*, and would need `_resolve`
covered as well, i.e. two sites and two copies of the question. Structurally the
defect is a rung-scoped conclusion about an identifier-wide fact, and `_resolve` is
where the conclusion is drawn.

**`_first_submit`'s own REJECTED branch is deliberately excluded**: both production
callers run `DuplicateOrderGuard` immediately before that submit, the guard asks the
same identifier-wide question, and it BLOCKS on `FILLED`. A second read milliseconds
later can only repeat an answer already acted on.

**2. Dispositions: the whole terminal-non-fill branch, above the rejected/cancelled
split.** `CANCELLED_ELSEWHERE` has the identical shape and an adjacent mechanism —
`replace` cancels the ORIGINAL at the broker, so a broker resolving the same race by
cancelling the REPLACEMENT produces `Cancelled` over a filled original, and lands in
the same `NOT_RESOLVED` bucket. `EXPIRED` on a 0DTE rung at the close is a third
arrival. Excluding it would assert that a fill on another rung matters when this rung
says `Rejected` and not when it says `Cancelled`, which nothing downstream believes.
**Not extended to the cancel-and-finish paths** (`WALKED_AWAY`, `DRIFTED`, `ABORTED`,
`NEVER_WORKED`): the probe would then fire on every ordinary walk-away, and on the
exit path `_await_settled` already asks one frame later.

**3. NOT gated on `state.steps`.** The broker does not enforce `external_identifier`
uniqueness (measured, chunk 1), so a walk at step 0 is not a walk with one order
under its key — `_recover` resuming against an order a "failed" submit landed, and
P0's exit retry re-running under the same `argus-close-*` key, both produce exactly
that. And a rung-scoped gate on an identifier-wide question **repeats the bug's own
category error one level up**, defended with the same sentence that was wrong the
first time.

**4. Extract the selection, write the read narrow.** `_recover` wholesale is wrong
here (disconnect record, `state.recovered`, reconnect+backoff, `walk.disconnected`,
"recovering from {what}" prose, the `len(working) > 1` cancel-everything decision).
So: a new narrow method, plus one shared module-level `_first_filled(candidates)`
that `_recover` now uses — behaviour-identical to the `filled[0]` it replaces, so
"which order counts as having left a position" has ONE implementation. **First, not
largest, and never a sum**: the question is a boolean, and the order returned supplies
`broker_order_id` / `filled_contracts` for the record only. The under-count on a walk
that filled across two rungs (2026-09-01 finding 5) is unchanged **deliberately** —
`positions` is what is authoritative about size.

**5. `state.last` and `state.broker_order_id` move; `state.sent` does not.** Moving
the outcome alone would leave `filled_contracts` reporting the rejected rung's 0 under
a `FILLED` headline — half-applied, and harder to spot than the bug. `state.sent`
stays because `_finish` reads `final_limit_price` off it, and relabelling that as the
filled rung's limit would dress one limit up as more fill-like than another — **a
limit is not a fill price**, which is the `entry_credit` lesson from this same pass.

**6. The record.** Outcome `FILLED`, so `_finish` leaves `reject_reason` at `None`:
**its documented contract — present only on `REJECTED` — is not widened.** The
rejection rides in `reason` (`order 1430433 came back Rejected, but 1430426 under
this identifier is filled: Received a fill on the original order`), which reaches
every surface that reads a walk outcome. Plus a **Tier 2 `walk.filled_under_identifier`**
carrying both ids, the status and the prose as fields — because the incident's
headline is a RATE and prose is not countable.

**7. A probe that raises FALLS BACK to the verdict the branch already had**, with the
scope named in `reason` (`…the identifier-wide fill check could not be answered (…),
so this verdict is about the rung alone`). The probe is **strictly additive** over a
positive terminal observation the broker gave one call earlier, so a read that does
not happen discovers nothing; manufacturing `UNRESOLVED` out of it would DESTROY
information the broker positively supplied and land in the same `NOT_RESOLVED` bucket
on the exit path anyway. Both directions of being wrong are safe: flat → the attempt
is spent on a position that no longer exists (zero cost, today's behaviour); still
open → the refusal was real and A2's repeating escalation carries it.
`_request_cancel`'s rule holds verbatim: **a call that raises must not turn a decided
outcome into an exception.** Recorded, never swallowed —
`_record_disconnect(call="terminal_probe")` at Tier 1 plus `state.recovered += 1`.

**One attempt, no retry, no reconnect** — the one read in this module that is
deliberately not retried, and the module docstring now says so. `_observe`'s ladder
exists for reads the loop cannot continue without; retrying here would put up to
`sum(_RECOVERY_BACKOFF_S)` of sleeping on the EXIT path's terminating branch, awaited
by `_run_exit` while a triggered 0DTE position is open. The `walk.disconnected`
record makes the failure rate a measurement rather than a guess.

**8. Read cost: at most ONE extra `get_live_orders()` per `run()`.** It fires only
from the terminal branch, which ends the walk on the next statement, and both routes
return that result immediately. It goes through `LiveOrderView` →
`AuditedOrderExecution` → `ThrottledOrderExecution`, the same governed stack the
guard and `_recover` use. On 2026-09-01 it would have been one read.

## Fix 2 — the page owed the broker's own words

`_record_exit` hand-builds a six-key alert dict and `walk` was not among them, so P6's
`WalkResult.reject_reason` reached the Tier 1 record and never the phone. The first
exit rejection in the system's history paged with `loop.py`'s hardcoded fallback while
`'Received a fill on the original order'` — the sentence saying the exit had
succeeded — sat in the audit log.

**What a per-position alert owes, decided once: an alert that names the outcome of an
exit attempt carries the broker's reason for that outcome.** One helper,
`_alert_reject_reason`, is where the two hand-built pages read it from. Three surfaces
now carry it:

| Surface | Why |
|---|---|
| `_record_exit` | the one-shot that names the outcome — the incident |
| `_page_if_spent_and_still_held` | the REPEATING page, so it survives a missed or throttled notification; `_SpentAttempt` gained an optional field to carry it |
| `_flatten_may_act` | **found by the structural test, not by the design pass** — it names `spent_outcome` and announces a non-negotiable being overridden, where "the previous close was refused because it had already filled" reads very differently from "…because the account is not entitled" |

The rest already carried it: `watchdog_exit_unattempted`, the `_NOTHING_SENT` /
`_CROSS_UNPRICEABLE` retry pages and `watchdog_exit_still_unplaced` all spread
`**report.to_payload()` (→ `walk` → `reject_reason`); every entry-side page spreads
`**result.to_payload()`, which is why P6 was already right there.
`watchdog_cross_refused` and `watchdog_crossed_market` report no walk outcome.

**`candidate_id` stays at the TOP level** of every one of these — `notify.alert_scope`
(B1, 2026-08-21) reads it there to give each position its own throttle window.
**`report.to_payload()` is not spread wholesale into the notify**: it carries the
nested `walk` dict and the caller adds `position`, and this goes to a phone.

**The rule is held structurally, because a convention is exactly what failed.**
`test_every_alert_that_names_an_outcome_carries_the_brokers_reason` parses
`watchdog.py` and `entry.py` and requires `reject_reason` on any `self._notify` /
`self._alert` whose kind or whose detail keys name an outcome — literally, or via a
payload that carries it, which a companion runtime test pins so the escape hatch
cannot rot. It caught `watchdog_flatten_override` on its first run.

## What was NOT changed

- **The one-attempt rule.** This changes what a walk *concludes*, not whether an exit
  *retries*. A walk that concludes `FILLED` never needed a second attempt because the
  spread is flat. `_RETRYABLE_EXITS`, `_EXIT_SEND_ATTEMPTS` and `_attempted` are
  untouched, and **this is not a step toward the open confirmation-gated re-attempt
  decision** and must not be cited as one.
- **`_await_settled`'s deliberate asymmetry** — FILLED identifier-wide, the settle
  confirmation scoped to `walk.broker_order_id`. Untouched. This adds the wide FILLED
  question at a *different* site; it widens no settle confirmation anywhere.
- **`DuplicateOrderGuard`**, `classify_order`'s fail-closed posture,
  `WalkResult.reject_reason`'s "REJECTED only" contract, `_recover`'s behaviour, and
  `/status`.
- **`filled_contracts` across rungs** (finding 5), `FillInfo.fill_price`, the
  float-epsilon credit stop, the flatten allowance, `infer_direction`.
- **Nothing new is submitted.** One extra READ and a re-labelled verdict.

## Consequence to carry

**The open decision about backing off on the COUNT of consecutive terminal rejections
must never count one of these.** "Your order filled" is not a broker refusing to
trade, and before this it was recorded as one — on the entry path, for a trade idea
that had opened a position. After this such a walk records `FILLED` and never reaches
a rejection counter at all. That is the second reason this shipped before that
decision is built rather than after.

## Tests and mutations

**16 new tests** — 11 in `tests/test_execution_loop.py`, 5 in
`tests/test_watchdog_exit.py`. Suite: 1809 → 1825, all passing.

Covered: the incident exactly (outcome, contracts, broker order id, last status,
`reject_reason is None`, the reason's prose, and the Tier 2 record); the ENTRY mirror;
a real refusal with nothing filled still refusing; `CANCELLED_ELSEWHERE` both ways;
the ungated step-0 case; the probe's own failure path and the `UnmappableOrderError`
variant; the one-read-and-only-on-a-terminal-rung cost bound; `_first_filled` as one
shared implementation including "cannot tell"; an AST test that the probe decides on
STATE and never on the prose; the rejected exit's page carrying the words, `None`
never `""`, the repeating page carrying it, the payload escape hatch, and the alert
sweep.

**Eleven mutations run, every one caught:** remove the probe; invert the
filled/empty branch; drop the `state.last` reassignment; drop the
`state.broker_order_id` reassignment; let the probe's exception propagate; re-add a
`steps > 0` gate; drop `reject_reason` from each of the three alert dicts; stop
carrying `walk` in `ExitReport.to_payload`; and never put the reason on
`_SpentAttempt`.

## Still unproven in cert

The probe's own failure path, and the `CANCELLED_ELSEWHERE`-over-a-filled-original
variant — neither has been observed live. The rejection variant is the one with a
measured 33% rate behind it. Cert proves API contracts and wiring, never fill
behaviour, so **the value of this fix is in the RECORD and the spent attempt, not in
a number cert can produce.**
