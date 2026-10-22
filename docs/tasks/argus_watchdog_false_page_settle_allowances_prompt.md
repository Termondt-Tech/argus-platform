# Argus: Two Watchdog Pages That Fire and Immediately Self-Clear

## Priority

Not urgent. Neither page reflects an impaired rail — `degraded` and the credit/underlying
triggers keep working throughout both episodes below. This is alert-noise cleanup, safe to
schedule whenever. Filed now because both fixes were sized and scoped during the
2026-09-02 EOD review and the shape of each is already clear.

## Context

Two independent false/noisy Tier 1 pages, both observed on 2026-09-02, both already
described in [the third live-fire day](../decisions/2026-09-02-third-live-fire-day.md)
(findings 4 and 5). Bundled here because they're the same file and the same class of fix
(a settle allowance or evidence check before a page fires), not because they share a root
cause — they don't, and should land as two separate commits.

### Fix 1 — `watchdog.leg_imbalance` has no settle allowance

`PositionWatchdog`'s scan loop pages `watchdog.leg_imbalance` unconditionally whenever
`spread.imbalanced` is true (currently around `watchdog.py:2844` — **confirm the line
against current source; `main` has moved during this review**). Observed false positive:
at 15:30 ET, `put-7625-7620` reported `long_quantity: 2 / short_quantity: 1` — a snapshot
taken mid-way through **its own closing fill** — and `watchdog_exit_filled` with
`contracts: 2` landed inside the same minute. Nothing was actually impaired; the page
fired on a transition, not a fault.

This is the last of three book-shape pages without evidence-based suppression. The other
two already have it:

- `watchdog.unwatchable` is suppressed inside `watchdog.book_settle_s` (a wall-clock
  allowance from first observation — see the check pattern in `_report_unwatchable`).
- `held_but_unbooked` is suppressed by `_closing_fill_cover()` (`watchdog.py:2999` as of
  this review), an evidence set built from `note_fills()` naming exactly which symbols
  Argus's own recent closing fills took to flat.

### Fix 2 — `watchdog.unevaluable` pages twice per episode, even for sub-second flickers

`_report_evaluability` (`watchdog.py:3227` as of this review) is edge-triggered: Tier 1 on
the rising edge (`watchdog.unevaluable`) and Tier 1 again on recovery
(`watchdog.evaluable_again`). Measured via `tools.evaluability` on 2026-09-02: 545
distinct episodes in one session, p50 duration 2.0s, p90 5.0s, max 21.9s. Most of those 545
episodes therefore produced **two** Tier 1 Slack pages for a condition that resolved in
about two seconds.

This is "Fix D" from
[the 2026-08-13 live-fire pass](../decisions/2026-08-13-live-fire-sandbox-pass.md) (one of
several lettered fixes in that record), re-measured and re-sized in the 2026-09-02 record
above. The design was already stated there:

- A new `[watchdog]` key — do not reuse `book_settle_s`, different condition, different
  urgency — TIME-based on wall time since the condition was first observed.
- While inside the allowance: suppress the Tier 1 edge (both directions), but keep writing
  a Tier 2 telemetry record so the trail exists for re-tuning.
- Escalate to Tier 1 only if the condition is still live once the allowance elapses.
- `0` disables it (today's immediate-page behavior).
- Sizing already worked out: **~20.0s** (4x the measured p90 of 5.0s, floored at 4x
  `cadence_s`).

## Proposed approach

**Fix 1**: gate the `leg_imbalance` page through `_closing_fill_cover()` the same way
`held_but_unbooked` already is — suppress the Tier 1 page (keep a Tier 2 line) when the
imbalanced leg is covered by Argus's own recent closing fill. Only fall back to a new
timer-based allowance if there's a real case the evidence check can't reach; don't build
one preemptively.

**Fix 2**: add the settle-allowance key (suggest `watchdog.unevaluable_settle_s`) and apply
it in `_report_evaluability` exactly mirroring the existing `book_settle_s` pattern.

**Config shape — decide explicitly, don't default silently**: make the new key OPTIONAL
with a default of `0`, not REQUIRED. A required key forces a coupled config+image deploy
(this repo's pattern for behavior-changing watchdog keys, e.g. `flatten_before_close_s`,
`book_confirm_ttl_s`); those were required because their off-state would have been unsafe.
Here the off-state (`0`) is simply today's behavior, which is safe — so ship this PR
uncoupled (image-only), and let Richard flip it to `20.0` afterward as a pure config
change. State this reasoning in the PR description since it's a deliberate departure from
some of this repo's past required-key choices.

## Non-goals — must hold

- Do not touch `_Escalation`'s `blind`/`degraded` severity split (`_report_evaluability`'s
  docstring explains why they're separate) — this only changes *when* the `blind` edge is
  allowed to page, never what counts as blind.
- Do not change what triggers can or cannot be evaluated, or any trading-path decision.
  Both fixes are purely about alert timing/suppression.
- Do not invent a new evidence mechanism for Fix 1 if `_closing_fill_cover()` already
  covers the observed case.

## Before implementing

Confirm against actual source rather than this brief's line numbers — `main` has been
under active parallel development and several of the referenced functions have moved
during recent sessions. If a referenced function no longer exists or has a materially
different shape, stop and report the discrepancy rather than building around a guess.

## Testing requirements

**Fix 1**: a test reproducing the 2026-09-02 pattern — an imbalanced spread mid-way
through its own closing fill must not page Tier 1.

**Fix 2**:
- An episode shorter than the allowance produces no Tier 1 record (neither rising nor
  falling edge), but does write Tier 2.
- An episode longer than the allowance still pages Tier 1 on both edges.
- The allowance is per-episode (a new episode starting after a settled one gets its own
  full allowance), not a global cooldown.
- `0` reproduces exactly today's immediate-page behavior — add this as an explicit
  regression case, not just an implicit default.

## Deliverables

- Code changes, as two separate commits (different mechanisms, same file).
- CLAUDE.md updated (required every session).
- A decision-record note — Fix 1 as a resolution line added to the third-live-fire-day
  record's finding 5; Fix 2 as a resolution line added to the 2026-08-13 record's Fix D
  and cross-referenced from the third-live-fire-day record's finding 4.
- Test suite green, new/total count reported.

Confirm with Richard whether to work on a branch or on `main`, and whether to commit. Do
not push unless explicitly told to.
