# One unanswered duplicate check is not a blind day (2026-08-26)

*The duplicate-order guard raises when it cannot reach the broker, and in live-fire
that raise ended the entire trading day — on the FIRST failure, with no retry at
any level. Twice observed, most recently at 10:00:00 ET on 2026-08-25, the day's
very first tick, costing ~5 hours of entry window to one timed-out REST call. No
order was ever at risk; the mechanism did exactly what it was built to do. Fixed
in two stages — a bounded retry, then a consecutive-idea streak — neither of
which touches what counts as a conflict, and neither of which can reach a submit
without a verdict.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

## What prompted it

2026-08-25, 10:00:00 ET. The signal loop built a clean 5-wide 2-contract call
spread (7700/7705, credit 0.525) and `PreTradeGate` approved it
(`gate.decision: allowed=true`). Before submitting, `execution/entry.py` called
`DuplicateOrderGuard.has_conflicting_order` — the one required check that nothing
is already working under this candidate's identifier. That REST call timed out
against cert, which was having a rough morning on the same
`sandbox-nginx.service.consul` backend that showed 25 `account.refresh_failed` /
6 recovered earlier the same session.

The handler took the live-fire branch and called `_end_day`: Tier 1
`entry.aborted_day`, an alert, and the session's `stop` event set — the same event
the scheduler sets at 16:00. No kill switch, correctly: nothing had reached the
broker. But `Scheduler.run` advances from *now* to the next session open and
**never re-enters a day it has already run**, so 10:00–15:00 was gone. The
all-time `entry.aborted_day` counter was already 1 before this session.

The reasoning being applied was explicit in the code, and it is a real one:

> orders may exist, and being blind to what is already working is a real reason to
> stand down for the day.

The defect is not that conclusion. It is the evidence it was drawn from: **one
timed-out round trip is not blindness.**

## What the investigation turned up that the prompt did not anticipate

**The guard call had no bound of its own, and the one it inherited was sized for
something else.** `has_conflicting_order` → `get_live_orders()` → the SDK's httpx
client, built in `broker/session.py` with `timeout=broker.http_timeout_s` = 30.0,
a **scalar**. Two consequences, and the second is the one that matters:

- httpx reads a scalar `timeout=` as connect/read/write/pool **independently** —
  the exact trap `account/refresher.py` was fixed for the same evening
  ([off the event loop](2026-08-26-account-refresh-off-the-event-loop.md)). One
  guard call could spend 30s connecting and 30s again reading.
- 30s is sized, per that key's own config comment, for *"a cold FIRST
  authenticated call from a new host."* `[reconcile] request_timeout_s`
  explicitly refused to inherit it for precisely that reason and chose 10.0. The
  entry guard — the hottest path in the system, on a candidate built against a
  1.0s freshness bound — inherited it silently.

So the status quo already blocked the signal loop for up to ~30–60s before ending
the day. **A retry without its own per-attempt bound would have made that three
times worse**, which is why `execution.guard_timeout_s` is part of this change
rather than a follow-up.

**`_end_day` costs more than the entry window.** Setting the session stop unwinds
`run_session`'s `finally`, which calls `trading.watchdog.stop()`. A spread already
open when this fires stops having its virtual stop evaluated — on expiry day, for
the rest of the day. `max_concurrent_positions = 2` permits exactly that state. On
2026-08-25 it fired on the first tick with nothing open, which was luck, not
structure. **This is an argument for reaching `_end_day` rarely, never for
softening what happens once it is reached** — and the escalation below is
unchanged from what it always was.

> **SUPERSEDED 2026-09-01, in one half only.** The paragraph above is the finding
> that this record could not act on, and it was acted on: `_end_entries_today` no
> longer sets the session stop, so the watchdog, the exits and the 15:30 flatten
> survive it. Everything else here still holds verbatim — entries still end on the
> first occurrence, the guard is still never re-asked, and the day-end is still
> unconditional. The narrowing is of the BLAST RADIUS, never of the entry posture.
> → [end-day keeps the watchdog](2026-09-01-end-day-keeps-the-watchdog.md)

**A blip costs the trade idea either way.** `_attempted.add(candidate)` runs
BEFORE the guard, and `candidate_id` is `date-side-short-long`, so the next
`signal.cadence_s` tick re-proposing the same strikes returns at
`entry.already_attempted` without ever reaching the guard. The prompt's Option B
described this as "let the next loop tick try a fresh one"; it is not. A failed
candidate is retired for the process lifetime. Two things follow:

- The streak counts **DISTINCT TRADE IDEAS, not ticks.** N=3 means "three
  different spreads in a row could not be checked", which is a real
  sustained-unreachability signal — evidence-shaped rather than a timer, matching
  the rule [held-but-unbooked](2026-08-20-held-but-unbooked-after-our-own-exit.md)
  already established. It is also slow, which is why every single spent burst
  pages on its own.
- **Releasing the `_attempted` claim on this path was considered and refused.** It
  would be defensible — the guard is a GET and cannot have created an order — but
  it converts an unconditional *"claimed before anything can reach the broker"*
  into a conditional one, in the single arc that prevents a double entry, to buy
  back one spread out of the ~10 a session proposes. Not worth it.

## What was built

Two stages, in `execution/entry.py` only.

**Stage 1 — `_check_for_duplicate`: retry the QUESTION.** Three attempts, 1s then
2s backoff, each bounded by `asyncio.timeout(execution.guard_timeout_s)`.

- **Scope by POSITION in the call graph, never by exception type.** Only the guard
  await sits inside the `try`, so nothing else can be retried and no type list has
  to keep up with whichever transport error the SDK raises this month — a raw
  `httpcore2.ReadTimeout` escaped unwrapped in the 2026-08-26 traceback. Same rule
  `reconcile._ask_broker` established.
- **One type IS load-bearing, and it is the same one.** `has_conflicting_order`
  writes Tier 1 `order.duplicate_blocked` from inside itself, so an
  `AuditWriteError` can surface from what looks like a pure read. A dead recorder
  halts the process and must never be re-attempted (CLAUDE.md); it is re-raised
  untouched, before anything else.
- **The timeout is applied by the CALLER**, exactly as `ReconcileRetry.request_timeout_s`
  is, for the same reason (the execution port is narrow by design; a timeout
  argument on every method is the wrong seam) — and it is safe here in the way it
  explicitly is not for the account refresher: this is an async httpx call on the
  loop, so cancelling it really cancels the request rather than abandoning a
  running worker thread.
- **The backoff is interruptible.** A bare sleep would sit on an operator's
  `/kill` for the length of the ladder. Same mechanism as `reconcile._sleep_or_stop`.
  A stop mid-burst abandons the candidate and — deliberately — does **not**
  increment the streak or write `entry.aborted_day`: a scheduled close is not
  evidence about the broker.

A spent burst retires that trade idea and pages, exactly as the dry-run path has
done since 2026-08-11.

**Stage 2 — `_guard_unreachable`: the day ends on a STREAK.** The
`guard_unreachable_max_streak`-th consecutive spent burst, with no answered check
between them, takes the original posture: `_end_day`, no kill switch, the same
`entry.aborted_day` record with the same `kind` and the same `error_type` (the
raw exception, not the wrapper), plus two additive fields. **The streak resets on
any ANSWER, blocked or not** — the same reset-on-success rule
`AccountStateCache._note_refresh_success` uses, because "consecutive" is the whole
of what the number means.

## Why the duplicate invariant survives, with full confidence

It survives by the same mechanism it always did, and the mechanism was not
touched: **`_walk_and_report` is unreachable without a returned `ConflictVerdict`.**
`_check_for_duplicate` has exactly two outcomes — a verdict, or
`_GuardUnreachable` — and every path out of the second returns from `submit`
before the submit boundary. No branch converts "could not ask" into "no conflict".

`guard.py` is untouched: WORKING and FILLED block, DEAD allows, unclassifiable
blocks. **This change retries the QUESTION, never the verdict.** A blocked verdict
returns on the first attempt having spent no retries at all, exactly as a
reconciliation mismatch does — the structural property recorded in
[broker unreachable](2026-08-26-reconcile-broker-unreachable.md).

`test_no_retry_can_reach_a_submit_without_a_verdict` asserts this as a property of
the module rather than of one path, and four independent mutations (attempts=1,
escalate-on-1, no per-attempt timeout, streak never resets) each fail at least one
test.

## The numbers, and what is unmeasured in them

| | | |
|---|---|---|
| `execution.guard_timeout_s` | 3.0 | Config. **UNMEASURED** — a healthy `GET /orders` on a warm session should be well under it, and a fast 502 or refused connection returns in milliseconds and never touches it. Tighter than `[reconcile]`'s 10.0 because the reconcile runs once a session while this runs per trade idea. |
| `execution.guard_unreachable_max_streak` | 3 | Config. Distinct trade ideas, not ticks. Set to 1 to restore the pre-2026-08-26 behaviour exactly. |
| `_GUARD_ATTEMPTS` | 3 | Module constant, mirroring `reconcile._INNER_ATTEMPTS`. |
| backoff | 1s, 2s | Module constant. `account/stream.py`'s ladder, not `reconcile.py`'s `min(2n, 5)` — this burst is on the entry's critical path, where that module's own reasoning ("several attempts inside the window a single one used to occupy") applies most directly. |

**Worst case added latency before the first submit is 12.0s** (3 × 3.0 + 3.0 of
backoff) — but that is precisely the path on which **nothing is submitted**. The
worst case on a path that *does* submit is ~9s; the common fast-fail-then-recover
path is ~1s. Against the status quo's ~30–60s single-shot, this is faster in the
worst case and bounded for the first time.

**That latency is real price staleness and it is the binding constraint here, not
`signal.cadence_s`.** `build_order` enforces `signal_max_age_s = 1.0` BEFORE the
guard and nothing re-checks after; the walk's ladder is arithmetic on the gate-time
mid and never re-quotes; and `UnderlyingDriftCheck` runs at cycle boundaries
**after** `_first_submit`, so **the first rung is the one price in the whole walk
with no drift check at all**. Every second of retry lands on exactly that rung.
That is why the ladder is the fast one and why `guard_timeout_s` is the single
knob that scales the whole burst.

## Why blocking the signal loop is acceptable here, and was not in the watchdog

The rule from [book-confirm timeout](2026-08-21-book-confirm-timeout-bound.md) —
*an await takes its bound from the thing being awaited, never from a cadence in
scope* — applies with full force, and the status quo was breaking it. The
CONSEQUENCE is different, though, and the difference is why an inline retry is
right here and was wrong there:

- `SignalLoop.run` awaits `evaluate_once()` and *then* sleeps `cadence_s`, so the
  cadence is a floor between iterations, not a schedule. `submit` already blocks it
  for the entire price walk — tens of seconds — and `entry.py`'s docstring says
  that is **wanted**, because it serializes entries so the concurrent-position
  limit cannot be raced.
- Nothing safety-critical rides that task. The watchdog, the account refresher, the
  heartbeat and the control surface are all separate tasks, and the await is async
  I/O, so the event loop itself is never blocked. **The cost of blocking here is
  delayed entries. There, it was a window with no position evaluated against its
  triggers.**

## Deliberately not done

- **`entry_build_failed` is untouched, and should be.** It is the only other
  `_end_day` call site. Its `try` covers `killswitch.engaged` (a property read),
  two audit writes (`AuditWriteError` is re-raised; the sole remaining raiser is
  `_reject_unserializable`, i.e. a serialization bug), `_trading_date()`, and
  `build_order` (pure — `StaleSignalError`/`ValueError` already handled
  separately). **Nothing in it touches the network**, so a retry would spin on a
  permanent defect. `entry_guard_unreachable` is uniquely a REST failure mode, and
  nothing else on the entry path has this shape.
- **The dry-run branch is byte-identical**: one attempt, no added bound, the raw
  exception in the record. That mode routes nothing, so there is nothing for
  patience to protect, and its soft-abandon has been correct since 2026-08-11. The
  30s it inherits from `broker.http_timeout_s` is a pre-existing condition of a
  transitional activation mode.
- **`guard.py`'s classification is untouched.** Restated because it is the single
  easiest thing to weaken by accident while chasing availability.
- **N consecutive unreachable DAYS still escalate to nothing.** Same open item the
  reconcile fix left: one bad day is now survivable, five in a row is not
  addressed, and there is no evidence yet for what N should be.

## Files

`execution/entry.py` (`_check_for_duplicate`, `_guard_unreachable`,
`_abandon_guard`, `_backoff`, `_note_guard_attempt`, `_GuardUnreachable`;
`_end_day` gained an additive `extra`), `config.py`, `argus.toml`,
`argus.dev.toml`, `tests/test_entry.py`, `tests/support.py`.

**New records:** `entry.guard_unreachable_attempt` (Tier 2, per failed attempt,
mirroring `reconcile.unreachable_attempt`) and `entry.guard_unreachable_abandoned`
(Tier 1 + alert, once per spent burst). The abandon alert carries `candidate_id`
at the **top level** of its detail — `SlackNotifier` scopes its 900s throttle on
`(kind, scope)` and reads the scope from there, so nested, two candidates' pages
would coalesce and the second would arrive as a bare `(+1 suppressed)` up to 15
minutes late ([alert throttle](2026-08-21-alert-throttle-is-per-position.md)).

**Deploy:** adds two REQUIRED `[execution]` keys, so **the config deploys WITH the
image** — the same coupling `[reconcile]` already forces in v0.3.11.
