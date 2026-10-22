# A spent exit attempt is visible on `/status` (2026-08-21)

*A2 bounded the SILENCE on the phone and left the PAGE saying nothing: a position
whose one exit attempt is spent had already been popped out of `_impaired`, so
`/status` rendered the worst state the exit path can produce as an ordinary open
position. Fixed with a live read scoped to what the ledger still says is held —
never a re-derivation from the structure the state has left.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

## What prompted it

A2's own recorded residual, taken off the open-decisions list rather than found:

> **`/status` still shows a spent position as watched.** It disappears from the
> "Exit triggers" cell (`_impaired.pop`) and appears nowhere else as degraded, so
> the phone view reads normal for a position nothing is evaluating.

Confirmed unchanged in source before editing. `check_once`:

```python
if key in self._exiting or key in self._attempted:
    self._impaired.pop(key, None)          # correct, and it is the whole defect
    if (spent := self._attempted.get(key)) is not None:
        await self._page_if_spent_and_still_held(spread, spent)
    continue
```

The `pop` is right on its own terms — the position is no longer evaluated, so a
frozen impairment reading would be a lie that only gets older, and
`ImpairedPosition.impaired_for_s` is the field that lie would corrupt. What was
missing is anywhere for the state to go NEXT. The position stayed in `watching`,
kept counting toward `open_spread_count`, and rendered on the page as two ordinary
legs with every trigger evaluable.

**The same shape as the `daily_loss.breach` defect, one field over** (2026-08-20):
a status view derived from a structure the real state has already left, which is
reassuring at exactly the moment it is wrong. That one rendered TRIPPED in red
while the gate allowed entries; this one renders calm while a triggered 0DTE
position has no automated path to close. D1's fix pattern is the template — a live
reference into the source of truth, not a reconstruction — and it is reused here.

## What the operator was actually left with

| Channel | Before | After |
|---|---|---|
| Phone | The `_SPENT` escalation, repeating on `watchdog.exit_unattempted_max_s` | Unchanged |
| Audit trail | `watchdog.exit_spent_still_held`, Tier 1, per bound | Unchanged |
| `/status` | Nothing. "Exit triggers: all evaluable", "Open positions: 2 legs (1 spread)" | An "Exit attempts" row naming the position, the outcome, the size and the age |

One channel, and it was the push one. An operator who opens `/status` — because a
notification was missed, because they are checking on something else, because the
phone is what they reached for after the page and the page is where the answer was
supposed to be — got a page that contradicted the alert they had just received.

## What was built

**`PositionWatchdog.spent_exit_attempts()`**, the companion live read to
`impaired_positions()`, returning `SpentExitAttempt(external_identifier,
candidate_id, contracts, outcome, spent_for_s)`.

**Driven by the SCAN, filtered by `_attempted` — never the reverse.** This is the
load-bearing choice and it is not a style preference. `_attempted` is deliberately
never pruned (A2: clearing a key on scan-exit would hand a second attempt to a
spread that flickered out of the ledger and back, which is the dangerous
direction), so iterating it would report every position that ever ended badly
today, forever, including ones that closed cleanly a second later. Iterating the
scan means the row clears on exactly the condition that stops the page: the ledger
no longer says the position is held.

**A separate `StatusSnapshot` field, not a severity on the existing one.**
`ImpairedPosition`/`PositionEvaluability` carry `unknown_reasons` and `blind`,
which mean something specific and different — *which triggers cannot be evaluated
right now*. A spent position has no reasons: it is not being evaluated at all.
Overloading the row would also mean re-adding the key to `_impaired`, undoing a
`pop` that is correct.

**Both renderings, checked rather than assumed to agree.** This page has a
documented history of the JSON being right while the HTML silently truncated
(`ImpairedPosition`'s own docstring is about exactly that), so the JSON key and the
HTML row are asserted separately in the tests, and the HTML carries the meaning in
WORDS — `NO EXIT LEFT (not_resolved, 2x) for 15m 0s — needs a human` — not only in
a colour class, because a colour does not survive a screenshot description or a
narrow phone column.

**No severity gradient in the HTML.** `_impaired_cell` styles `degraded` amber
because the underlying backstop is still live and it is genuinely not an emergency.
There is no equivalent here: every entry means the stop already fired, the position
is still held, nothing is evaluating it, nothing automated will try again, and it
expires today. All red.

### The duration, and where it is measured from

`_attempted` becomes `dict[str, _SpentAttempt]` — the outcome **and the instant**.
Both channels read `spent_for_s` / `held_for_s` from that instant.

This corrects a small inconsistency inside A2 rather than introducing one. A2 armed
the `_SPENT` escalation in `_run_exit` explicitly so that "the elapsed time the page
reports is measured from the exit attempt itself" — and then added a defensive
re-arm in `_page_if_spent_and_still_held`, which it had to (a spread that flickers
out of the ledger and back returns with the slot cleared, and `overdue` answers
`None` for an inactive slot — silent forever). The re-arm restarted the reported
duration as a side effect. The episode is a **cadence**: it decides when the next
page fires and must keep doing so. It is not the answer to "how long has this
position had no way to close", which is asked once and answered from one instant.

So `overdue` still gates the page, and the number in the page is now
`clock() - spent.since`. After a flicker those diverge, and the divergence is the
point: the episode reads 30s, the attempt was spent 45 minutes ago, and 45 minutes
is what the operator acts on. It also means the page and `/status` cannot disagree
with each other about the same fact — the rule this repo keeps re-learning.

### Session scoping

Three layers, and the first two are structural:

1. The `PositionWatchdog` is rebuilt per trading day, so `_attempted` cannot
   outlive a session.
2. `SessionStatus.session_finished` clears `_spent_exit_source` with the risk and
   link sources, so the field reads `None` — "no watchdog", not `()` — between the
   close and the next morning.
3. The scan filter above clears the row intraday, the moment the position closes.

`None` and `()` stay distinct here for the same reason they do one row up: `()`
asserts that every held position still has its exit attempt, which is a claim no
process without a watchdog is entitled to make.

## The adjacent check, and what it found

A2b's brief asked for a quick confirmation that no OTHER state reached by falling
out of `_impaired` vanishes the same way. Three call sites pop it:

- **`_note_evaluability`, fully evaluable** — the position is fine. Correct.
- **`check_once`, `key in self._exiting`** — an exit is in flight. Bounded by the
  walk's own timeouts and self-resolving: it ends either with the position leaving
  the ledger (correctly gone) or in `_attempted` (now visible). Not a silent state.
- **`_refresh_if_due`, gone from the scan** — either genuinely closed (correct), or
  **still held and no longer attributable to a spread** (`scan.unattributed` /
  `scan.malformed`).

**That last case is the same shape and is NOT fixed here.** An unwatchable position
is an open position with no virtual stop being evaluated — the exact gap the module
exists to close — and `_report_unwatchable` pages it Tier 1, once per episode
signature. There is no `/status` field for it: the page shows the legs under "Open
positions" and nothing names them as unwatched. It is the still-unbuilt
`degraded_positions` proposal from the Fix C follow-up, now with a second instance
of its pattern behind it. Recorded under *Open decisions*, deliberately not widened
into this change.

## Tests

`tests/test_watchdog.py` (the live read, over the real `check_once`):

- `test_a_spent_attempt_is_reported_live_while_the_position_is_still_held` — it has
  left `impaired_positions()` AND is in `spent_exit_attempts()`, with the outcome,
  the contract count and a duration that grows.
- `test_a_clean_fill_that_books_is_not_reported_as_spent` — `FILLED` spends the
  attempt like everything else and nothing excludes it by name; it does not need
  excluding, because the closing fill takes the row out of the scan this read is
  driven by. Its deliberate corollary (a `FILLED` whose row never clears) still
  shows, and still pages.
- `test_a_closed_position_does_not_stay_on_the_page_forever` — an hour after the
  position leaves the ledger the row is empty while `_attempted` still holds its
  key, which is what proves the direction of the filter.
- `test_the_spent_duration_is_measured_from_the_attempt_not_the_episode` — a
  ten-minute flicker out of the ledger and back; both the live read and
  `watchdog.exit_spent_still_held` report 660s, not the re-armed episode's 30s.

`tests/test_status.py` (both renderings, separately):

- The three states — no watchdog (`null`, and NOT "all intact"), live and clean
  (`[]`, "all intact"), and spent — asserted in JSON and HTML independently.
- `test_a_spent_position_is_distinguishable_from_an_ordinary_watched_one` — the
  defect itself: a snapshot that is calm everywhere else, where the exit-triggers
  row correctly says "all evaluable" (a position nobody evaluates is not an
  *impaired* one), and the spent position still has to be tellable apart at a
  glance.
- `test_session_end_clears_the_spent_row_rather_than_tallying_it` — the D2 failure
  mode: after `session_finished` the field is `None` and the HTML carries no
  `NO EXIT LEFT`, and the next `session_starting` is clean.

`tests/test_session_trading_side.py`: the AST test that already guards
`evaluability_source` now has a twin for `spent_exit_source` — published to
`session_running`, read off `PositionWatchdog.spent_exit_attempts()`, and built
inside a branch testing `trading`, since an empty tuple in shadow would assert that
every held position still has its exit attempt, about positions that do not exist.

## Deploy

**Image only.** No new config key, no migration, no `argus.toml` change. It can
ride the coupled deploy `watchdog.exit_unattempted_max_s` and
`watchdog.book_confirm_ttl_s` already require, or go alone.
