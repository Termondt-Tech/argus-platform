# `/status` says when a day is blacked out (2026-09-18)

**BUILT 2026-09-18, ships in v0.4.4. `src/` + tests + docs. No config key, no
migration, no behaviour change.** One new Tier 1 audit category,
`session.event_blackout`, written only on a blackout day, which nothing reads.

## The finding

On 2026-09-11 (CPI, a date in `blackout_dates.txt`) the 08:45 ET `/status` read:

```
"session":        { "phase": "idle", "next_session_open": "2026-09-11T09:40:00-04:00" }
"event_blackout": { "state": "ok", "days_remaining": 103, ... }
```

Nothing on the page said the day would take no entries. Two separate gaps caused
that:

1. **`event_blackout` described the FILE, not the DAY.** `BlackoutRunway` carried
   `last_date` and `date_count`. `read_blackout_runway` loaded the full set of dates
   and threw it away on the next line. So on the one day a month the list was
   actually suppressing entries, the row read a plain `ok`.
2. **The audit trail said nothing until 10:00 ET.** `CreditSpreadStrategy.evaluate`
   checks the entry window *before* the blackout, so every `signal.none` from the
   session opening at 09:40 until the window opened said `outside_entry_window`. The
   journal logged the file's horizon at session start, and never whether today was
   in it.

Live day 1 (2026-09-16, FOMC) was the same shape, with real money on the account.
On a blackout day the live `daily_pnl` is legitimately `0.0`, and nothing
distinguished that from a broken signal path, a dead feed, a cap-bound day or an
exhausted list.

## What was decided

**`next_session_open` is correct and was left alone.** A blackout day still opens a
session: login, reconcile, chain, the watchdog, exits and the pre-close flatten all
run, and only entries are refused, in `strategy.py`. A blackout-aware
`next_session_open` would misreport when the process next wakes and when the
watchdog goes live, which makes it a worse field. So the field says *when a session
starts*, and a new flag says *what that session will not do*. This is the same
two-questions-two-fields split as `halted` vs `entries_ended`.

**Sessions are not skipped on blackout days.** A position carried into the day
would lose its stop and its flatten.

### A — the page (`status.py`, `app.py`)

- `BlackoutRunway.dates`: the set itself, from the same loader.
  `BlackoutRunway.covers(day)` returns `False` when the blackout is disabled (a
  definite answer: nothing is suppressed) and `None` when the file is unreadable
  (the page cannot say, and the session will abort anyway).
- `StatusSnapshot.blackout_today` / `blackout_next_session` are derived live against
  `generated_at` and against `next_session_open`'s own date, following the same rule
  `blackout_state` already follows. These are separate questions because while a
  session is running, `next_session_open` is tomorrow's open.
- JSON: `event_blackout.today_blacked_out` and `event_blackout.next_session_blacked_out`.
  `state` still reads `ok`, which is correct: it describes the file.
- HTML, in three places:
  - an **`Entries`** row under the phase, `NONE TODAY — event blackout`. It sits
    where the entries-ended stand-down already sits, because `idle`/`running` is
    true but misleading on its own. It is suppressed when the stand-down row is
    present, since that row already says it.
  - an `IN FORCE TODAY` badge leading the Event blackout row.
  - a suffix on **Next session open** when that session's date is a blackout day.
- **Grey, never amber.** The blackout is planned and expected, and there is nothing
  for the operator to do. It is a badge so it cannot be missed, and muted so it does
  not spend a warning colour on a day going exactly as designed. The "silent while
  healthy" rule on the runway is unchanged. An ordinary day adds nothing to the page,
  and a test holds that.

### C — the session's half (`session.py`)

`_announce_blackout_day(audit, trading_date=, blackout_dates=)` runs at session start
with **the same frozenset the strategy is built from**. An AST test pins that both
calls in `run_session` take the one local `blackout_dates`. On a blackout day it
prints a journal line and writes Tier 1 `session.event_blackout {trading_date}`. On
an ordinary day it writes nothing, because this is a row whose *presence* is the
answer.

The strategy's check order (window, then blackout) is correct and **is unchanged**.
This fills the silence ahead of it without moving the rail.

## The one caveat, and why it holds

`BlackoutRunway` is read **once at process start**. For the horizon, that was an
accepted staleness: a warning that outstays its cause. A claim about **today** can be
*wrong*, not merely stale, if the file changes under a running process. It holds for
three reasons:

- `blackout_dates.txt` is a bind mount, so the deploy classifier restarts on any
  change to it.
- There is no hot-reload, by rule.
- The session's own verdict comes from an independent read of the file at session
  start (C), so a hand edit without a restart shows up as the page and the audit
  trail disagreeing.

## Not done (deliberately)

- **B, a "next entry day" field** (the next session that is *not* blacked out) was
  scoped separately and not built. Walking the calendar past blackout days would
  need `SessionScheduler`'s 14-day horizon re-reasoned rather than reused (Dec 23
  plus holidays), and the walk belongs in `app.py`, not in the scheduler, which owns
  no trading state.
## The EOD report names it too (same day, same release)

`tools.eod` reads the **report date's** blackout **off the chain**, from
`session.event_blackout` or from `signal.none` refusals with reason `event_blackout`
(`read_blackout_day`). It never uses `/status`'s `today_blacked_out`. That key answers
for the moment `/status` was polled, and the report may be a next-morning catch-up or
a re-render of a past date.

- The subject block, G1's "no series", and the digest's `entries` and `G1` lines all
  name the blackout. The digest JSON gains `blackout_day`.
- Only the **next** session comes from `/status`
  (`event_blackout.next_session_blacked_out`), because it is about the future. It
  shows as a line in RAILS and as a `next` heads-up line in the digest when tomorrow
  is blacked out.
- **An entry attempt on a blackout day is the one blackout reading that alarms**,
  because by design it means the rail did not hold.

→ [the EOD record's amendment](2026-09-17-automated-eod-report.md)
