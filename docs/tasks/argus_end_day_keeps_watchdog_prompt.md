# Argus: `_end_day` Tears Down the Watchdog on an Open Position

## Priority

**DESIGN FIRST — do not implement without Richard's sign-off.** This changes the
behaviour of a deliberate abort path, and it directly contradicts a line in
`_end_day`'s own docstring ("never for softening what happens once it is reached")
and in CLAUDE.md ("reach it rarely, never soften it"). Those lines predate P2 (the
`FLATTEN_BEFORE_CLOSE` trigger) and P3 (the `HaltGrade.ENTRIES` monitor-only
posture), and the argument here is that "keep watching the open position" is not
the kind of softening they were forbidding — but that is Richard's call to
re-make, explicitly, not something to route around.

**This is a live-flip concern.** A held 0DTE spread losing its virtual stop and
its close-of-day flatten — on expiry day, when gamma is worst — because the entry
path hit a transient broker blip is a real-capital exposure.

## The issue

`EntryExecutor._end_day` ([`src/argus/execution/entry.py:1124`](../../src/argus/execution/entry.py))
is the SOFT side of the entry boundary: end today's session, keep the process,
resume automatically tomorrow. Its stated justification is "nothing reached the
broker, so there is no order state to protect, and a latched halt would also cost
tomorrow" — sound reasoning **about entries**.

It ends the day with `self._stop.set()` — the session `stop` event, the same one
the scheduler sets at the market close. That unwinds
[`session.run_session`](../../src/argus/session.py)'s `finally`, which calls
`trading.watchdog.stop()`, and the scheduler never re-enters a day it has already
run. So a spread already open when `_end_day` fires **stops having its virtual stop
evaluated, stops being marked, and loses its `FLATTEN_BEFORE_CLOSE` trigger — for
the rest of expiry day.**

`_end_day`'s docstring already names this cost and calls it an argument for
reaching the path rarely, not for changing what it does. This prompt argues the
opposite: the entry-side reasoning ("nothing was submitted") is correct and should
stay, but it is orthogonal to the open position, and the position must keep its
watchdog.

### Call sites

- [`entry.py:518`](../../src/argus/execution/entry.py) —
  `_end_day("entry_build_failed", ...)`: order construction raised. A code defect;
  nothing in its `try` touches the network.
- [`entry.py:982`](../../src/argus/execution/entry.py) — the `_guard_unreachable`
  path: the duplicate-order guard could not reach the broker for
  `execution.guard_unreachable_max_streak` consecutive **distinct trade ideas**
  (v0.3.11,
  [`docs/decisions/2026-08-26-entry-guard-unreachable-retry.md`](../decisions/2026-08-26-entry-guard-unreachable-retry.md)).
  A transient cert outage. This is the one that has cost trading days.

The design must decide whether both sites get the new treatment or only the
guard-unreachable one — see question 3.

### Why P3's machinery does not already cover it

P3 ([`docs/decisions/2026-08-29-halt-scope-split.md`](../decisions/2026-08-29-halt-scope-split.md))
built exactly the "entries off, watchdog on" posture: `HaltGrade.ENTRIES` engages
the kill switch, skips `request_stop()`, and the session comes up with
`submit=None` so the fill consumer, watchdog and exit executor all run while
nothing enters. But:

- `monitor_only` is computed **once, at session build time**
  ([`session.py`](../../src/argus/session.py), ~line 959–972), and `submit` is
  set once (`submit = None if monitor_only else trading.entry.submit`, ~line 1269)
  and passed to `SignalLoop` at construction. There is **no mid-session transition
  into monitor-only.**
- P3's `ENTRIES` grade is **permanent** — kill switch engaged, needs a human
  (`/resume`, once that exists) — which is deliberately what `_end_day` is *not*.
  `_end_day` is a soft, same-day condition that must auto-resume tomorrow.

So the fix needs a **transient, session-scoped** monitor-only: same runtime shape
as P3's ENTRIES day (watchdog + exits + flatten live, entries denied), but no kill
switch, no `HaltRecord`, no durable state, and it evaporates at the next rollover
because the session object is rebuilt.

## The direction

`_end_day` should **downgrade the running session to monitor-only for the rest of
today** instead of ending it:

- The signal loop stops routing entries (or stops proposing entirely — question 1).
- The watchdog, exit executor and fill consumer keep running untouched.
- The session body stays alive until the scheduler's real close-time `stop`, then
  tears down normally — so the scheduler's "never re-enter a day" rule is
  unaffected and tomorrow starts clean.
- The alert and the `entry.aborted_day` audit record still fire; a new record
  makes the "entries halted, watchdog still running" state explicit.

## Design questions to resolve

1. **Does the signal loop keep spinning or stop?** Keeping it running with the
   entry seam disabled matches shadow's proven `submit=None` path (full pipeline,
   nothing routed) and keeps the audit trail showing a live monitor-only session.
   Stopping the loop entirely saves cycles but needs `run_session` restructured so
   "loop stopped" ≠ "session ended". Recommend one.
2. **Mechanism.** Options to weigh:
   - a session-local `entries_halted_today` event that a thin `submit` wrapper
     checks per tick (`if set: decline; else trading.entry.submit(...)`) — minimal,
     no `SignalLoop` change, watchdog untouched;
   - a `SignalLoop` method that clears its own `submit` seam at runtime, wired to a
     callback `_end_day` calls;
   - restructure so `_end_day` stops the loop and the body `await stop.wait()`s
     with the watchdog running.
   Recommend one, with the seam wiring (`_end_day` is on `EntryExecutor`, which
   does not own the watchdog or the loop — it currently reaches the session only
   through the shared `stop` event).
3. **Which call sites.** `entry_build_failed` is a code defect (louder? still
   end entries for the day?) vs. `_guard_unreachable` is transient broker. Neither
   has a reason to kill the watchdog on an open position, but the response beyond
   that may differ. Decide per site.
4. **No open position when `_end_day` fires.** Keeping a watchdog alive for hours
   to watch nothing is harmless but wasteful. Is "end the session normally if the
   book is flat, downgrade to monitor-only if anything is open" worth the branch,
   or keep it simple (always run to close)?
5. **Position resolves after the downgrade** (fills, stops out, flattens). End the
   session early, or run to the real close regardless? Simple = run to close.
6. **`/status` and phase.** `SessionPhase` is IDLE/LOGGING_IN/RECONCILING/RUNNING/
   ENDED/ABORTED. A transient monitor-only day needs to read as neither "running"
   (misleading — entries are off) nor "aborted" (misleading — the watchdog is
   working). Reuse P3's `session.monitor_only` record and the HALTED/monitor-only
   rendering, or a distinct phase? And how does it differ visually from P3's
   *permanent* ENTRIES monitor-only — the operator needs to know one clears
   tomorrow and one needs a human.
7. **Relationship to P3's `halted_grade` seam.** Is the transient state a third
   thing, or does it feed the same `monitor_only` flag the loop already reads —
   just now settable mid-session? If it shares the flag, spell out how a
   *permanent* P3 halt and a *transient* `_end_day` downgrade that land on the
   same session are distinguished (for `/status`, for the audit trail, and for
   "what clears this").
8. **The CLAUDE.md / docstring contradiction.** Both say do not soften `_end_day`.
   State precisely what this proposal changes (the watchdog survives; entries still
   end for the day; still no auto-retry of the guard; still no latched halt) and
   why that is consistent with the intent behind "never soften it" (which was
   about not adding retries and not making the day-end conditional), given P2 and
   P3 postdate it.

## Code touchpoints (for the design to map precisely)

- [`src/argus/execution/entry.py`](../../src/argus/execution/entry.py) —
  `_end_day` and its two call sites; the `self._stop` coupling.
- [`src/argus/session.py`](../../src/argus/session.py) — `run_session`: the
  `monitor_only` / `submit` computation (~959–1312), the `finally` teardown
  (~1437–1450), and the `halted_grade` seam. This is where a mid-session downgrade
  has to be expressible.
- [`src/argus/orchestrator.py`](../../src/argus/orchestrator.py) — `SignalLoop` and
  its `submit` seam.
- [`src/argus/status.py`](../../src/argus/status.py) — `SessionPhase`, the
  monitor-only / HALTED rendering, `session.monitor_only`.
- [`src/argus/scheduler.py`](../../src/argus/scheduler.py) — confirm the
  "never re-enter a day already run" rule is untouched (the session still ends at
  the real close).
- Read first:
  [`2026-08-29-halt-scope-split.md`](../decisions/2026-08-29-halt-scope-split.md),
  [`2026-08-28-time-based-flatten-trigger.md`](../decisions/2026-08-28-time-based-flatten-trigger.md),
  [`2026-08-26-entry-guard-unreachable-retry.md`](../decisions/2026-08-26-entry-guard-unreachable-retry.md),
  [`2026-08-03-kill-switch-scope.md`](../decisions/2026-08-03-kill-switch-scope.md),
  [`2026-08-17-halted-but-alive.md`](../decisions/2026-08-17-halted-but-alive.md).

## Deliverables

1. A **decision record** in `docs/decisions/` (dated, added to
   `docs/decisions/README.md`; CLAUDE.md's `_end_day` language and the
   halt-scope-split open-decision row updated): the bug, the answers to every
   question above, the chosen mechanism, and the explicit reconciliation with
   "never soften it".
2. The **mid-session downgrade design** — the seam from `_end_day` to the session,
   what the signal loop does, how the watchdog is left untouched, and how the
   session still ends cleanly at the real close.
3. The **`/status` design** — how a transient monitor-only day renders and how it
   is distinguished from P3's permanent one.
4. A **test / mutation list** — each must fail a test:
   - `_guard_unreachable` fires with a spread open → watchdog keeps evaluating
     triggers, the mark keeps updating, and `FLATTEN_BEFORE_CLOSE` fires at 15:30;
   - `_end_day` fires → no new entry is routed for the rest of the day;
   - the session ends normally at the real close, and the next trading day starts
     un-halted with entries enabled;
   - `entry_build_failed` path (whichever treatment is chosen) behaves as designed;
   - the transient state does **not** engage the kill switch and writes no durable
     halt state.
5. The **live-flip checklist** line this closes.

## Non-negotiables this must respect

- The watchdog, exit executor and `FLATTEN_BEFORE_CLOSE` must run for any open
  position until it resolves or the day closes — that is the whole point.
- Entries still end for the day once `_end_day` is reached — no auto-retry of the
  guard, no re-proposing the same idea, exactly as today.
- No kill switch, no `HaltRecord`, no durable state — this is a soft, same-day,
  auto-resume-tomorrow condition, deliberately unlike P3's permanent `ENTRIES`.
- The scheduler must not re-enter a day it has already run — the session runs to
  the real close and ends normally.
- `NEVER TOUCH THE LIVE DATABASE` for any exploration.

## Out of scope

- Implementing it.
- Changing when `_end_day` is reached (`guard_unreachable_max_streak`, the guard
  retry burst) — that is v0.3.11's decision and is not in question here.
- The kill-switch-persistence and shared-live-account issues (separate prompts),
  though the `/status` monitor-only rendering should stay coherent with the
  kill-switch work if both land.
- Generalising to a full mid-session "downgrade to monitor-only" API for other
  callers — build only what `_end_day` needs; note if the seam is reusable.
