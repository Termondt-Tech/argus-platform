# The book-confirm REST call was bounded by a rebuild cadence (2026-08-21)

*A broker round trip awaited inside a watchdog evaluation pass was given
`watchdog.refresh_interval_s` — 15.0s — as its timeout. That is the watch-list
REBUILD cadence, not a bound on an HTTP call. The tuned per-attempt bound for this
exact call already existed in config, one section over.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

Finding A4 of the 2026-08-21 pass. Found by reading, not by an incident: no
production stall has been attributed to it, and cert would not surface one
(`fetch_positions()` there is fast and the disagreements it produces resolve on
their own). Closed the same session it was found.

## The defect

`PositionWatchdog._cache_is_over_reporting` is the REST confirm added on
2026-08-19 to stop a stale account cache paging `held_but_unbooked` for a position
the broker no longer holds
([ghost positions](2026-08-19-account-cache-ghost-positions.md)). It wrapped its
round trip like this:

```python
async with asyncio.timeout(self._wd.refresh_interval_s):
    confirmed = await self._confirm_broker_position_count()
```

Every await on that line is inside **one** watchdog evaluation pass:

```
check_once → _refresh_if_due → _check_broker_agreement → _cache_is_over_reporting
           → await fetch_positions()
```

Nothing else runs in that task meanwhile, so the timeout is not a patience budget —
it is a **blind window**, during which no open position is evaluated against its
triggers. At production values that window was up to 15.0s: 15× `watchdog.cadence_s`
(1.0) and 3× the `sustained_s` debounce (5.0), so a trigger could form, satisfy its
full debounce, and still be waiting to be looked at.

It fires on the **paging edge of a book disagreement** — the moment Argus and the
broker are known to disagree about what is held, and therefore the moment the book
is least understood. That is precisely when trigger evaluation should not stop.

The disagreement is a property of the BOOK, not of any position: it compares the
cache's total leg count against the `positions` row count. So the position that
waits is never the position the confirm is about — a phantom leg from something
closed an hour ago (the 2026-08-19 shape exactly) stalls a live, unrelated spread.

**What the wait actually costs, by case:**

| Position state during the stall | Cost |
|---|---|
| Trigger already fired, exit in flight | **None.** `_trigger` hands off via `asyncio.create_task` and later ticks `continue` on `key in self._exiting` before evaluating, so the exit walk keeps running — the confirm's await yields the loop. |
| Trigger condition already accumulating | The clock runs through the stall, so the first post-stall observation sees the window already satisfied and fires at once. The stall is additive to time-to-exit; the debounce does not restart. |
| **Trigger condition becomes true during the stall** | **The bad case.** `SustainTracker.observe` starts the window with `setdefault(slot, now)` — at the first OBSERVATION the reason reads true, not when it became true. Nothing observed it, so the window starts late and still needs a full `sustained_s`: stall + 5.0s, against a ~5.0s baseline. |

That last row is the one the bound is sized against: 20s at the old value, 10s at
the new one.

**Frequency was already bounded; duration was not.** `_book_confirm` caches the
verdict per `(broker, ledger)` shape, so a steady disagreement costs one round trip
rather than one per tick. That is what kept this from being a per-tick 15s stall,
and it is why the finding is a latent hazard rather than an incident. It bounds how
MANY stalls there are, never how long one lasts.

Two refinements on that bound, because it is the reason this was a hazard and not an
outage, and it is weaker than "once per shape" sounds:

- `_book_confirm` is a **single slot**, not a dict. It collapses CONSECUTIVE repeats
  of one shape, so the real rule is one round trip per shape *change*. A book
  alternating between two shapes re-confirms on every alternation. The ledger count
  changes on every fill and fills call `request_refresh()`, so a persistent cache
  ghost re-keys on each entry and exit — `(1,0)` → `(3,2)` → `(1,0)` — and each
  transition is a fresh stall, landing exactly when a position is newly adopted onto
  the watch list.
  → **Closed the same day as A4b**, which makes it a dict with an explicit expiry
  policy: [per-shape cache](2026-08-21-book-confirm-cache-is-per-shape-and-expires.md).
  That record also corrects the entry/exit sequence sketched here — traced against
  the real gates, an entry/exit cycle walks through six DISTINCT shapes rather than
  alternating between two, and the genuine alternation is the account cache's count
  flapping with the ledger steady.
- Working the other way, three gates sit in front of the round trip and are strong:
  the refresh must be due, `booked_but_absent` never consults the confirm at all, and
  a `held_but_unbooked` excess covered by Argus's own closing fills returns from
  `_book_allowance_elapsed` before it. Ordinary entry lag and ordinary exit lag are
  therefore both free.

## Was 15s ever deliberate?

No. The line was introduced whole in `1569643` ("fixed: ghost positions in the
account cache", 2026-08-19); `refresh_interval_s` is used four lines away in
`_refresh_if_due` and referenced throughout the surrounding docstrings, and neither
that commit nor its decision record mentions a timeout at all. The value has not
been touched since. This was reaching for the nearest number in scope, not a
tradeoff someone made.

Worth checking rather than assuming, because the opposite case exists in this
codebase and reads similarly: `watchdog.book_settle_s` deliberately governs two
different windows, and its config docstring says so explicitly, precisely so nobody
"fixes" the sharing later.

## The fix

Use the bound that already exists:

```python
async with asyncio.timeout(self._config.staleness.account_refresh_timeout_s):
```

`staleness.account_refresh_timeout_s` (5.0) is **THE tuned per-attempt bound for a
`fetch_positions()` round trip against this broker** — sized for the cert
environment's documented 502s and hangs, and already governing the account liveness
refresher's copy of the same call. This is not a new bound being designed; it is the
existing one being applied at a call site that missed it.

No new config key, so **the image deploys alone** — no coupled config change.
The loader's existing invariant (`account_refresh_timeout_s < account_refresh_s`)
is untouched and still the only constraint on the value.

The failure posture is unchanged and was already correct: a timeout is caught by the
same `except Exception` as every other confirm failure, logs, caches `False`, and
**pages**. "Could not check" must never be the reason a genuinely unwatched position
goes unreported. A tighter timeout makes that fail-closed path fire sooner, not
differently.

## The rule this leaves behind

**An await inside `check_once` is a blind window, and its bound belongs to the thing
being awaited — never to a loop cadence that happens to be in scope.** A cadence
answers "how often should this run?"; a timeout answers "how long may this
particular call take before it is presumed dead?". They are numerically similar in
this file and mean unrelated things, which is exactly why one substituted for the
other without looking wrong.

Same shape as the rule the 2026-08-13 pass left behind — never fix an exit-path
staleness problem by raising `market_data_max_age_s` — and the note is now in
`StalenessConfig`'s docstring where the value is defined: `account_refresh_timeout_s`
has **two** call sites since this change, and raising it raises the watchdog's
worst-case blind window by the same amount.

## Coverage

Two tests in `tests/test_watchdog.py`, both using **real wall time** deliberately —
a `FakeClock` cannot observe an `asyncio.timeout`, which runs on the event loop's
clock. The suite's `account_refresh_timeout_s` is scaled to 0.05s while
`refresh_interval_s` is left alone at 5.0s, so a test that finishes fast can only
have used the timeout:

- `test_a_hanging_confirm_is_bounded_by_the_account_timeout` — a confirm that sleeps
  30s must return control in under 1s, and must still page.
- `test_a_hanging_confirm_still_stalls_once_per_shape_not_once_per_tick` — four ticks
  on a steady shape make ONE round trip; a changed shape makes a second. The
  per-shape cache is what bounds frequency, and it must survive the bound change.

Both were mutation-checked against the pre-fix line: they fail at 5.00s and 10.00s
respectively (one stall per shape, at the old bound), and the call-count assertion
holds in both versions — confirming the caching behaviour is genuinely independent
of the timeout value rather than incidentally passing.
