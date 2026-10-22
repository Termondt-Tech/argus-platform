# A spent exit attempt is not the end of the story (2026-08-21)

*`WalkPolicy.for_exit` shares `working_timeout_s` and `max_recovery_attempts` with
the entry policy on entry-shaped reasoning. Re-derived for the exit path, the
numbers hold — but the state they land in does not: `ExitOutcome.NOT_RESOLVED`
spends the position's one attempt, `check_once` skips it for the rest of the
process, and the only thing ever said about it was one alert at the moment it
happened. Fixed by bounding that silence, not by relaxing the one-attempt rule.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

## What prompted it

Not an incident — a read, in the same pass as A1/A3/A3b/A4. `WalkPolicy.for_exit`
justified sharing three operational knobs with the entry policy like this:

> The operational knobs (`working_timeout_s`, `status_poll_interval_s`,
> `max_recovery_attempts`) are deliberately shared with the entry policy: they trade
> request volume against reaction time and touch no price, so an exit has no reason
> to want different ones.

That is an argument about *price*, offered where the question is about *risk*. And
the value it defends is documented entirely in entry terms — `working_timeout_s`'s
config comment describes one scenario, a pre-market ENTRY accepted with
`tif.next_valid_session` and sitting at `Received` outside the market:

> Such an order MUST be cancelled — left alone it goes live at the next open,
> unattended, at a price nobody is walking.

The watchdog cannot be in that state. It only runs against a position that has
already filled, which means RTH, which means the scenario the number was chosen for
does not occur on the path that reuses it.

## The path, end to end

```
_await_in_market: working_timeout_s elapses, order seen but never in-market
  -> _request_cancel               (best-effort; NOT a confirmation)
  -> WalkOutcome.NEVER_WORKED
  -> ExitExecutor._after_walk      -> ExitOutcome.NOT_RESOLVED
  -> PositionWatchdog._run_exit    -> self._attempted.add(key)
  -> check_once                    -> skips _attempted keys, forever
```

Three failed `_observe` reads reach the same place via `BrokerUnavailable` ->
`UNRESOLVED`, as do three consecutive blind cycles.

**What that leaves.** A triggered 0DTE position — the stop-loss condition already
fired and sustained — with:

- **no trigger being evaluated.** `check_once` `continue`s before
  `evaluate_triggers`, and pops the position out of `_impaired` on the way past, so
  it also stops appearing in `/status`'s "Exit triggers" cell.
- **no automated path to close.** `_attempted` is never cleared inside a process.
  There is no time-based trigger and no end-of-day flatten anywhere in `src/` —
  `TriggerReason` has exactly two members and neither is a clock. The position rides
  to expiry unless a human intervenes.
- **one alert, at the moment it happened.** `_record_exit` writes Tier 1
  `watchdog.exit_finished` and pages `watchdog_exit_not_resolved`, once. Then
  nothing.

The rest of the system behaves correctly around it: `open_spread_count` still counts
the position, so the concurrency cap and the `max_concurrent_positions ×
max_loss_per_position ≤ daily_max_loss` invariant still hold. Nothing new is added
on top. The position is simply unmanaged and unmentioned.

## Were the shared values wrong?

**No — and shortening them would have made this worse.** Re-derived against the
exit's risk profile rather than the entry's:

| Knob | Value | Why it holds for an exit |
|---|---|---|
| `working_timeout_s` | 30.0 | Not a passive wait. A fill or a terminal status short-circuits `_await_in_market` on the poll that sees it (`_reported_fill`, `is_terminal`), so the full bound only elapses on an order that is persistently neither in-market nor terminal. **Shortening it is the wrong direction**: the bound's exit is `NEVER_WORKED`, which is the terminal state above. A tighter bound reaches it sooner, and reaches it on orders that were about to work. |
| `max_recovery_attempts` | 3 (~1.5s of `_RECOVERY_BACKOFF_S`) | Exhausts into `UNRESOLVED` — "nobody read this order". More attempts against an unreadable broker buy an exit nothing it may act on: an unread order never authorizes a second closing order (`_await_settled`, A3/A3b), so the extra patience ends at the same refusal. |
| `status_poll_interval_s` | 1.0 | Genuinely operational, genuinely price-free. Unchanged for both paths. |

So no exit-specific keys were added, and the image deploys without a config change.

**The lever was never the seconds.** It was the state at the end of them.

## Why the fix is not a retry

The obvious reading of "permanently unwatched" is "so retry it". That is the
double-close, under a different name.

`NEVER_WORKED` — the very outcome this started from — calls `_request_cancel`, which
is best-effort by design and confirms nothing:

> `cancel_requested` records that we ASKED; the order is at `Cancel Requested` at
> best, and may still fill.

A second closing order on top of one that can still fill does not flatten the
spread. It re-opens it inverted, unmanaged, on expiry day. `UNRESOLVED` is worse:
nobody read the order at all. Both are precisely the states `_await_settled` was
hardened (A3, A3b) to refuse to cross against, and a "retry" is the same second
closing order the cross would have been.

The outcomes are also not uniform, which rules out a blanket rule even if one were
wanted — `BLOCKED` means a close is already in flight, and `CANCELLED_ELSEWHERE`
means a human cancelled Argus's exit order, which a retry would override. See the
residual below.

## What was done

**The silence is bounded; the one-attempt rule is untouched.**

`_attempted` becomes `dict[str, ExitOutcome]` — the outcome is kept, not just the
key, because "the walk timed out" and "the cross was refused" are different next
moves for whoever gets the page. `PositionWatchdog._page_if_spent_and_still_held`
is the third member of the family `_page_if_no_exit_yet` established on 2026-08-20,
under a new `_Escalation` tag `_SPENT`:

- **Repeating, per `watchdog.exit_unattempted_max_s`** (30.0 live, 20.0 in tests).
  Reused rather than given a key of its own, and honestly so — the key's own comment
  describes its class as *"long enough that an ordinary few-second quote gap resolves
  inside it, short enough that a triggered position nobody can close is on a phone
  while the trading day is still running"*, which is this condition verbatim. Reusing
  it also keeps this an image-only deploy, matching A1 and A4.
- **Armed in `_run_exit`, not on the next tick**, so the elapsed time the page
  reports is measured from the exit attempt itself.
- **Re-armed defensively** in the escalation method. `_refresh_if_due` calls
  `_escalation.forget` for anything that leaves the scan, and `_attempted`
  deliberately keeps its key across that; a spread that flickers out of the ledger
  and back would otherwise return with the slot cleared, and `overdue` answers `None`
  for an inactive slot — silent forever, which is the one outcome this exists to make
  impossible. `rising` is idempotent, so the re-arm cannot shorten a running window.
- **Not a halt**, for the same reason `_page_if_no_exit_yet` is not: halting sets the
  stop event, which ends the watchdog and stops the *other* position being watched.
- **Rate-bounded twice**, unchanged from the 2026-08-20 design: `overdue` fires per
  interval rather than per tick, and `SlackNotifier` throttles at 900s. Both bounds
  are per POSITION — the notifier's since B1, later the same day, which found that
  it keyed on `kind` alone and therefore folded a second position's page into the
  first's suppressed count. → [the alert
  throttle](2026-08-21-alert-throttle-is-per-position.md)

New records: Tier 1 `watchdog.exit_spent_still_held`, alert kind
`watchdog_exit_spent_still_held`, carrying `exit_outcome`, `held_for_s` and
`bound_s`.

### A clean fill does not page, and is not excluded by name

`FILLED` also spends the attempt, and nothing filters it out. It does not need to
be: `positions` loses the row on the closing fill, the next refresh drops the spread
from `_scan`, and `_escalation.forget` takes the slot with it. The bound is what
makes that safe — booking lag was 4-5s in live fire, well inside 30s.

**The corollary is deliberate.** A `FILLED` exit whose row is *still* there past the
bound DOES page, because a fill that never booked or a partial close that left a stub
is a position carrying real risk with no trigger being evaluated — and no other rail
says that at this granularity. `booked_but_absent` compares LEG COUNTS across the
whole book; it cannot name a position, and it cannot tell a stub from a lag.

## Tests

In `tests/test_watchdog_exit.py`, driving the REAL `ExitExecutor` over the REAL
`PriceWalkingLoop` INTO a real `PositionWatchdog` (`build_watchdog` grew injectable
`audit=`/`alerts=` so both sides share one trail):

- `test_an_exit_whose_status_never_confirms_does_not_go_silent` — the exact
  reproduction. Order accepted, sits at `Received` forever, `working_timeout_s`
  burns, cancel requested and unconfirmed, `NOT_RESOLVED`, attempt spent; then the
  page fires past the bound, naming the outcome, and does not halt.
- `test_the_spent_attempt_page_repeats_and_never_re_sends_an_order` — ten minutes
  held: ~30 pages, monotonically increasing durations, and
  `len(broker.submitted) == 1` throughout. **That count is against the TEST
  harness's `exit_unattempted_max_s = 20.0`** (`tests/support.py`, unchanged since
  2026-08-20), not production's 30.0 — 600 / 20 = 30 rather than 600 / 30 = 20. The
  property under test is one page per interval, never one per 0.5s cadence tick and
  never once-only; the asserted window is `25 <= pages <= 35`.

In `tests/test_watchdog.py`: a clean fill that books never pages; a fill whose row
never clears does; and a spent position that leaves the ledger and returns still
pages (the defensive re-arm).

`test_an_order_that_never_reaches_the_market_is_not_crossed` is **unmodified** and
passing — this changes nothing about the "do not cross" half of `NEVER_WORKED`.

## Residuals, deliberately not fixed here

- **`/status` still shows a spent position as watched.** It disappears from the
  "Exit triggers" cell (`_impaired.pop`) and appears nowhere else as degraded, so the
  phone view reads normal for a position nothing is evaluating. Same family as the
  `daily_loss.breach` bug: the reassuring reading at the one moment it is wrong.
  Fixing it needs a new field on `StatusSnapshot` and its renderers; the page is the
  primary channel and is now correct.
- **A confirmation-gated retry is possible and was not built.** `_await_settled`
  already produces exactly the positive confirmation a safe re-attempt would need,
  and the outcomes split cleanly: `CROSS_UNPRICEABLE` in particular reaches
  `_attempted` having *already* confirmed the abandoned order terminal, so nothing is
  live and it is structurally the same "no order exists" case `NOT_ATTEMPTED` is
  allowed to retry. `BLOCKED` and `CANCELLED_ELSEWHERE` must never retry. A per-
  outcome rule is a change to a stated non-negotiable and needs sandbox exercise of
  paths CLAUDE.md lists as NOT YET EXERCISED (rejections, `Cancel Requested` going
  terminal). → recorded under *Open decisions*.
- **`_attempted` is never pruned** when a position leaves the ledger. Left that way
  on purpose: clearing it on scan-exit would hand a second attempt to a spread that
  flickered, which is the dangerous direction. Growth is a handful of keys per day
  against a process that restarts daily.
