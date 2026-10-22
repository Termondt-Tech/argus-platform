# Argus: One Unanswered Duplicate-Order Check Ends the Whole Trading Day

## Priority

Not urgent, but real and now observed twice. No incorrect order was ever
submitted — the mechanism worked exactly as designed. This is about
AVAILABILITY, not safety: whether the fail-safe response to a single transient
cert timeout should cost an entire remaining session, given how often cert has
now been shown to have transient timeouts. Investigate and propose a fix;
implementation should not proceed without Richard's sign-off on the actual
design, since this touches the ONE duplicate-submission guard in the system.

## Context

2026-08-25, 10:00:00 ET — the very first tick of the day's entry window. The
signal loop worked correctly (this was right after the `v0.3.8` entry-admission
fix landed): it built a clean 5-wide, 2-contract call spread (7700/7705, credit
0.525), and the pre-trade gate approved it (`gate.decision: allowed=true`).
Before submitting, `execution/entry.py`'s pipeline calls
`DuplicateOrderGuard.has_conflicting_order` — the one required check confirming
nothing is already working under this candidate's identifier before an order can
go out. That REST call timed out against cert (same
`sandbox-nginx.service.consul` backend independently showing 25
`account.refresh_failed` / 6 recovered earlier that same morning — cert was
having a rough day, not yet resolved from the prior session). The exception
handler in `entry.py`:

```python
# entry.py:426-430
try:
    verdict = await self._guard.has_conflicting_order(order.external_identifier)
except Exception as exc:
    if self._config.execution.dry_run_submissions:
        await self._abandon_dry_run("entry_guard_unreachable", candidate, exc)
    else:
        await self._end_day("entry_guard_unreachable", candidate, exc)
    return
```

In live-fire mode (`dry_run_submissions = false`, which is the current phase —
see CLAUDE.md), the LIVE-FIRE branch fires unconditionally on the FIRST failure,
no retry attempted at any level. `_end_day` sets the session's stop event (the
same one the scheduler uses at the actual 16:00 close) and writes Tier 1
`entry.aborted_day`. No kill switch — this is explicitly a soft failure, correct
per its own comment ("nothing reached the broker, so there is no order state to
protect") — but it ends the ENTIRE remainder of the trading day, ~5 hours of
entry window remaining (10:00–15:00 ET) on a single timed-out REST call. This is
not the first occurrence — `entry.aborted_day`'s all-time counter was already 1
before this session.

**The reasoning behind ending the day, not just skipping the candidate, is
explicit in the code** (the comment on the live-fire branch): *"orders may
exist, and being blind to what is already working is a real reason to stand
down for the day."* That's a real concern and this task must not casually
override it — read `execution/guard.py`'s full docstring for
`has_conflicting_order` before proposing anything, including the 2026-08-20
finding it already carries (an unclassifiable order also BLOCKs, for the same
"no safe reading of an unanswerable question" reason). This task is about
whether a SINGLE transient timeout should trigger that same conclusion, not
about weakening the guard itself.

## What to investigate

**1. Is a bounded retry appropriate here, and if so, on what schedule?**
`account/stream.py`'s `_refresh_loop` already solves a structurally similar
problem — a transient REST failure against the same broker, where sleeping the
full interval after one failure wastes most of the staleness budget on a single
blip: *"A failed attempt therefore backs off from 1s, doubling, capped at the
normal interval — several attempts inside the window a single one used to
occupy"* (`_retry_delay`, `_RETRY_BACKOFF_BASE_S`, `_RETRY_BACKOFF_MAX_DOUBLINGS`).
Read that mechanism as the precedent to reuse or adapt, not to reinvent — but
verify it actually fits this call site before assuming it does: `_refresh_loop`
retries in the BACKGROUND on its own loop, outside anything else's critical
path. `has_conflicting_order` is awaited SYNCHRONOUSLY inside the entry pipeline,
which itself runs on `signal.cadence_s` (5.0s, `argus.toml`) — check whether
retrying inline here would stall the signal loop the same way the watchdog's own
book-confirm timeout once did (`docs/decisions/2026-08-21-book-confirm-timeout-bound.md`
— "an await inside a hot loop takes its bound from the thing being awaited,
never from a cadence in scope" is the exact rule that finding established; it
applies here too and must be checked, not assumed safe).

**2. Should ONE failure end the day, or should ending the day require a
SUSTAINED failure signal?** Two real options, not obviously the same choice
— present both with reasoning, do not silently pick one:

   - **Option A — bounded retry, same ultimate fallback.** Retry
     `has_conflicting_order` a handful of times with backoff (per #1) before
     still calling `_end_day` if all attempts fail. This absorbs a single blip
     without changing the underlying "stand down when genuinely blind" logic
     at all — the day only still ends if cert stays unreachable across the
     whole retry window.
   - **Option B — distinguish one candidate's bad luck from sustained
     unreachability.** Treat a single `entry_guard_unreachable` as "abandon
     THIS candidate, let the next loop tick try a fresh one" (the same shape
     as the existing dry-run soft-abandon path, `_abandon_dry_run` — read it;
     it already exists for the OTHER mode and does exactly this), and only
     escalate to `_end_day` after N CONSECUTIVE guard-unreachable failures
     across separate candidates/ticks — a real signal of sustained
     unreachability rather than one bad REST call. This needs new state (a
     consecutive-failure counter), analogous to `AccountStateCache`'s own
     `_consecutive_failures` tracking in `stream.py` — read that as the
     pattern to follow if you go this direction, including how it resets on
     success.

   These are not mutually exclusive — a bounded retry (A) reduces how often a
   single blip reaches the escalation decision at all, and the consecutive-tick
   counter (B) changes what "escalation" means once retries are exhausted.
   Read both existing precedents in full, reason about which combination
   actually fits this call site's constraints (see #1), and present a
   recommendation with the tradeoffs stated plainly — do not just pick A
   because it's simpler unless it's actually sufficient. **Whichever direction
   you land on, do not touch the underlying WORKING/FILLED/DEAD/unclassifiable
   classification logic in `guard.py` — this task is entirely about what
   happens when the classification call itself cannot complete, never about
   loosening what counts as a conflict.**

**3. Does this same pattern exist anywhere else on the entry path?**
`entry.py:401` (`entry_build_failed`) and other exception handlers around it
may have the same one-shot-to-`_end_day` shape for other transient-failure
causes. Check whether the same reasoning (and therefore possibly the same fix)
applies to any of them, or whether `entry_guard_unreachable` is uniquely
sensitive because it's specifically a cert-REST-call failure mode (as opposed
to e.g. a local construction error, which retrying would never fix). Report
what you find; do not expand scope to fix other cases without flagging them
first — this task is scoped to the guard-unreachable path.

## Constraints

- **This is THE only duplicate-submission protection in the system**
  (`guard.py`'s own docstring, and CLAUDE.md's non-negotiables section:
  *"Broker is the only source of truth... the unreadable ones are a required
  argument... never suppressed"*). Any change here needs to preserve the
  invariant that an order is never submitted while Argus is blind to whether
  one is already working under the same identifier. If your proposed design
  cannot preserve that with full confidence, say so explicitly and stop rather
  than shipping something uncertain.
- **Live-fire vs. dry-run stays split exactly as today** — `_abandon_dry_run`
  is already correct for `dry_run_submissions = true` (per its own comment:
  nothing has ever routed an order in that mode, so there's genuinely nothing
  to protect). Don't touch that branch; this task is about the live-fire branch
  only.
- **No change to `has_conflicting_order`'s classification semantics**
  (WORKING/FILLED block, DEAD allows, unclassifiable blocks) — restated from
  above because it's the single easiest thing to accidentally weaken while
  chasing availability.
- Check whether a bounded retry budget could conflict with `signal.cadence_s`
  or `execution.signal_max_age_s` (the candidate's own staleness bound between
  gate-pass and order construction) — a long retry here could make the
  candidate itself stale by the time it's finally submitted or abandoned.
  Verify, don't assume either way.

## Testing

Extend `tests/test_entry.py` (existing coverage for `entry_guard_unreachable` /
`_end_day` is already there — don't fork a new file). At minimum:

- A single transient failure that recovers on retry (if Option A) results in
  the candidate proceeding normally, session continues, `entry.aborted_day` is
  NOT written.
- Sustained/exhausted failures still end the day exactly as today —
  `entry.aborted_day` written, session stop set, no kill switch. The existing
  test for this shape should still pass unchanged if the fallback behavior is
  preserved; if it needs to change, explain why.
- If Option B: N-1 consecutive failures across separate candidates do NOT end
  the day; the Nth does. A success anywhere in between resets the counter,
  matching `AccountStateCache`'s own reset-on-success pattern.
- Dry-run mode (`_abandon_dry_run`) is unaffected by any of this — add a test
  asserting so if one doesn't already exist.

Use a fake clock / injectable delay, never real sleeps, matching the house
convention for every other timing-dependent test in this suite.

## Deferred / explicitly out of scope

- Any change to `entry_build_failed` or other `_end_day` call sites, beyond
  reporting whether the same reasoning applies (see investigation item #3).
- `guard.py`'s classification logic itself (WORKING/FILLED/DEAD/unclassifiable).
- The market-crossing exit escalation's own broker-unreachable handling
  (`CROSS_REFUSED`) — a different, already-settled mechanism
  (`docs/decisions/2026-08-21-empty-broker-answer-is-not-a-verdict.md`), not in
  scope here even though it rhymes with this problem.
- Any change to `[staleness] account_refresh_timeout_s` or the account-refresh
  retry loop itself — those are working as intended and are the PRECEDENT for
  this task, not something this task modifies.

## Deliverable

A written investigation and recommendation FIRST — this is explicitly not a
"read the prompt, implement immediately" task given the safety weight of the
code involved. Present the findings from the three investigation items above,
a concrete recommendation (A, B, or both, with reasoning), and get Richard's
sign-off on the design before writing the implementation. Once approved: the
code change, test coverage per above, and a short decision-record note
(`docs/decisions/`) capturing why the chosen design still preserves the
duplicate-order invariant — matching this codebase's existing convention of
documenting reasoning for safety-rail changes, not just the change itself.
Confirm with Richard whether to work on a branch or directly on `main`, and
whether to commit — no standing convention is recorded for this repo either
way; do not assume. Do not push regardless, unless explicitly told to.
