# The book-confirm cache is per shape, and it expires (2026-08-21)

*A4's residual. The confirm cache was a single SLOT, so it collapsed CONSECUTIVE
repeats and nothing else — a book returning to a shape it had already asked about
re-paid the round trip, and the blind window with it. Making it a dict is four
lines. The part that had to be designed is what a cached verdict is allowed to
outlive.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

Finding A4b of the 2026-08-21 pass, found while tracing A4's residual. Like A4:
found by reading, not by an incident, and closed the same session.

## What was actually at risk, and what was not

A4 established that `_cache_is_over_reporting`'s round trip is a **blind window** —
awaited inside one `check_once` pass, so no open position is evaluated against its
triggers until it returns — and bounded one such window at
`staleness.account_refresh_timeout_s` (5.0s).

An already-fired trigger is **not** exposed to it. `_trigger` hands off via
`asyncio.create_task` and later ticks `continue` on `key in self._exiting` before
evaluating, so a position already exiting keeps its walk running while the confirm's
await yields the loop. That case was closed by inspection and is not what remains.

The residual is a stop condition that **forms during** the window. `SustainTracker`
starts its window with `setdefault(slot, now)` — at the first OBSERVATION the reason
reads true, not at the condition's true onset. Nothing observes during the stall, so
the clock starts late and still needs a full `sustained_s` afterwards. Stall time and
debounce **stack instead of overlapping**: 5.0 + 5.0 against a 5.0 baseline.

A4 bounded one stall's duration. This is about how many there are.

## The defect

`self._book_confirm` was `tuple[tuple[int, int], bool] | None` — one slot. It
answered "was the LAST confirm about this shape?", so the real rule was **one round
trip per shape CHANGE**, not per shape. A book alternating between two shapes
re-confirmed on every alternation, forever.

That matters because the exposure concentrates in the state this system has spent
the most effort trying to catch. A persistent cache ghost
([ghost positions](2026-08-19-account-cache-ghost-positions.md)) is by definition a
long-lived disagreement, and it is the case where the counts move underneath it.

## What the oscillation actually looks like

Reconstructed by driving `_check_broker_agreement` through a full day against the
real gates, because the shape sequence decides whether a dict can help at all — and
the first guess was wrong.

**An entry/exit cycle does not alternate.** With a two-leg ghost lingering in the
cache, one entry and one exit produce a monotone walk through six DISTINCT shapes:

| step | cache | ledger | confirms |
|---|---|---|---|
| idle, ghost only | 2 | 0 | 1 |
| entry leg 1 books | 2 | 1 | 1 |
| entry leg 2 books | 2 | 2 | — *(numeric agreement)* |
| broker book catches up | 3 → 4 | 2 | 2 |
| held, steady, hours | 4 | 2 | 0 |
| exit fills | 4 | 0 | 1 |
| broker book drains | 3 → 2 | 0 | 1 |

Six round trips, six distinct shapes. **A dict collapses none of it**, and the only
repeat — `(2,0)` before the entry and after the exit — is hours apart, reachable only
by a cache lifetime long enough to be a different bug. Worth stating plainly because
the finding was written expecting "one per shape for the day", and that is not
available.

**The alternation is the account cache's count flapping while the ledger holds
steady** — a stream re-seed churning a ghost leg in and out, with no fills involved.
That is the A → B → A pattern, and it is unbounded: twelve observations over two
shapes cost twelve round trips. Six disconnects in one sandbox session on 2026-08-11
is the precedent for the churn.

This is the case the dict fixes, and it settles the sizing question below: **the
repeats a dict can collapse are seconds to a minute apart, not hours.** The safe TTL
and the useful TTL do not conflict.

## The staleness policy

The dict is the easy half. A cached verdict outliving the state it measured is the
failure keying on shape exists to prevent, and the dangerous direction is specific: a
`True` suppresses `held_but_unbooked`, which is deliberately the ONE condition with
no timer allowance, because a position held with no ledger row has no virtual stop
being evaluated. An unbounded dict would trade A4's compounded delay for silence on
that page — strictly the worse bug.

Three bounds, strongest first. All three live in `_book_confirm_lookup`.

1. **The episode.** The entire cache is cleared the moment `broker == ledger`. Every
   verdict in it describes a disagreeing book; agreement means that book is gone.
   Evidence-based, no clock, and it is the main defence rather than a backstop —
   note from the trace above that an entry passes through numeric agreement, so this
   fires more often than "the disagreement finally resolved" suggests. This existed
   as a single-slot clear and becomes a full clear; a partial clear would leave the
   OTHER shapes' verdicts to be reused across an episode boundary.
2. **The shape.** A verdict answers for one `(broker_legs, ledger_legs)` pair.
   Every fill moves the ledger count and every book update moves the cache count, so
   nearly any change in what is held re-keys on its own.
3. **`watchdog.book_confirm_ttl_s`, from when the verdict was MEASURED.** The
   backstop for what (1) and (2) cannot see: the same counts recurring inside one
   long episode for a DIFFERENT reason. A ghost leg evicted in the same re-seed that
   a real fill goes unbooked leaves the counts identical and the verdict wrong, with
   neither the episode nor the shape having changed. Narrow — but its cost is silence
   on the no-allowance page, so it gets a hard cap rather than an argument about
   likelihood.

**Never extended by a cache hit.** A last-used TTL would make a shape the book sits
on for hours carry an immortal verdict — the unbounded trust this exists to end,
re-entering through the back door, in exactly the steady case that hides it longest.

Every one of the three fails toward asking REST again, and asking again can only cost
a round trip. **Nothing in the policy can produce a suppression a fresh confirm would
not have produced.**

## Why it is a new key

`watchdog.book_confirm_ttl_s`, required, `90.0`. Three existing values were
considered and each failed on the change test — would the right TTL follow if
somebody retuned it for its own reasons?

- `watchdog.refresh_interval_s` (15.0) — a rebuild cadence. This is the substitution
  A4 just removed from this exact call site.
- `watchdog.book_settle_s` (20.0) — already governs two windows and says so in its
  own docstring. A third meaning is drift, and 1.3× the refresh cadence is too tight
  to reuse (below).
- `staleness.account_position_evict_after_s` (60.0) — the closest fit, and the one
  worth naming because it is genuinely apt: it bounds how long the account cache may
  be wrong in the over-reporting direction, which is exactly what a `True` verdict
  claims. **Rejected on the repo's own stated rule**, which that key states about
  itself: *"ITS OWN KEY, never a reuse of `watchdog.book_settle_s`. Both are sized
  against the same propagation lag, which is exactly why sharing one number would
  weld two unrelated conditions together."* One governs when a stale cache entry is
  DROPPED; this one governs how long a suppression may stand on an old reading.
  Lowering the first toward `account_refresh_s` — legal today — would silently drag
  this TTL under the refresh cadence and destroy the frequency bound.

The deploy is already config-coupled: `watchdog.exit_unattempted_max_s` is a REQUIRED
key added by the 2026-08-20 exit-path fixes and still undeployed, so this rides the
same coupled deploy at no additional operational cost. **The config must deploy WITH
the image.**

### Why 90.0

Bounded on both sides, and both bounds are real:

- **Floor, enforced by the loader.** Must exceed `refresh_interval_s` (15.0). The
  confirm is only consulted from `_refresh_if_due`, so a TTL at or below that cadence
  expires every verdict before it can ever be reused — restoring a REST round trip,
  and its blind window, on every observation. That is the frequency bound A4 credits
  with keeping this a latent hazard rather than an outage, and it must not be
  configurable away. `ConfigError`, with the reason.
- **The account cache's own repair path, end to end.** A genuine ghost is evicted at
  `account_position_evict_after_s` (60) + one `account_refresh_s` (10), and seen by
  the watchdog within one `refresh_interval_s` (15) — about 85s for the counts to
  move and the key to change on their own. Expiring sooner re-confirms verdicts the
  eviction was still on track to make irrelevant.
- **Ceiling: 90s is the cap on how long a suppression may stand on an old reading.**
  Before this key there was no cap at all — one verdict held for the entire episode,
  which on 2026-08-19 was over an hour.

Cost on a long steady disagreement is one confirm per 90s, ~160 over four hours: at a
normal round trip, ~30s of blind time spread across an afternoon. At the
`account_refresh_timeout_s` worst case it is a ~5.5% duty cycle — and in that state
the liveness refresher is failing on the same call, entries are already blocked on
`account_stream_max_age_s`, and every one of those confirms is fail-closed.

Re-tune from `watchdog.book_cache_over_reporting` frequency, not from a guess.

### The size cap is not a knob

`_BOOK_CONFIRM_CACHE_MAX = 16`, a module constant. The TTL bounds lifetime; this
bounds size for a churn inventing shapes faster than the TTL retires them. Eviction
is oldest-measured-first and costs a round trip, never a suppressed page — safety
neutral, so it does not deserve config surface.

## What was deliberately not touched

The three gates in front of the round trip, which are why ordinary lag never reaches
it at all: the refresh must be due; `booked_but_absent` never consults the confirm;
and a `held_but_unbooked` excess covered by Argus's own closing fills returns from
`_book_allowance_elapsed` first. A full entry/hold/exit cycle with no ghost costs
**zero** round trips, and that is now asserted end to end rather than inferred.

## Coverage

Eight tests in `tests/test_watchdog.py`, two in `tests/test_config.py`. Each of the
behavioural ones was mutation-checked against the change it claims to pin:

| Mutation | Caught by |
|---|---|
| back to a single slot | `test_an_alternating_shape_does_not_re_confirm_on_every_alternation` (12 round trips instead of 2) |
| no expiry at all | `..._not_trusted_past_its_ttl`, `..._not_kept_alive_by_being_read` |
| TTL refreshed on use | the same two |
| agreement clears only the current key | `test_book_agreement_clears_every_cached_shape_not_just_the_last` |
| the direction gate removed | `test_booked_but_absent_past_its_settle_window_still_does_not_confirm` |
| the closing-fill evidence never admitted | `test_ordinary_entry_and_exit_lag_never_reaches_the_round_trip` |

The direction-gate mutation is worth calling out: it was survived by the *existing*
`test_the_confirm_is_not_consulted_for_booked_but_absent`, which only ever reaches
the settle allowance and so never exercises the direction check itself. The gap
predates this change; the new test advances the clock past `book_settle_s` so the
disagreement genuinely escalates, and then asserts the confirm still was not called.

`test_a_cached_suppression_is_not_trusted_past_its_ttl` is the one the policy exists
for: same shape, same episode — so neither stronger invalidation can fire — the truth
underneath changes, and the page must arrive.
