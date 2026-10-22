# Argus: Three Residuals From the 2026-08-20/21 Proactive Review (B2, E1, C1)

## Priority

**DESIGN FIRST for B2 and E1 — do not implement without Richard's sign-off.**
Both looked like small comparison-logic fixes on first read and turned out not
to be: B2's "obvious" fix needs new data threaded through a call chain that
does not carry it today, and E1's current behavior is explicitly a deliberate
choice (there is a comment saying so) that a naive fix would silently reverse
in the dangerous direction. **C1 is a one-line documentation correction — build
directly, no design gate.**

Not yet assigned to a release. Confirm with Richard whether this rides
v0.3.11 alongside the reconcile-broker-unreachable work or lands separately —
none of the three touches the same call paths as that task, so there is no
technical reason they have to move together.

## Context

All three were flagged in the 2026-08-20 proactive defect-class review
(`docs/tasks/argus_proactive_defect_class_review_prompt_template.md`) as
lower-priority findings, left open when the higher-priority items from that
same pass (C3, D1, C2, A3, A3b, A1, A4, A4b, A2, A2b, B1) were resolved and
shipped as v0.3.7. They were never picked up. Re-confirmed against current
`HEAD` (`cd01756`) immediately before writing this prompt — all three are
still present exactly as originally described; nothing about them has moved
since v0.3.7.

Read [`docs/decisions/2026-08-19-account-cache-ghost-positions.md`](../decisions/2026-08-19-account-cache-ghost-positions.md)
and [`docs/decisions/2026-08-20-held-but-unbooked-after-our-own-exit.md`](../decisions/2026-08-20-held-but-unbooked-after-our-own-exit.md)
before touching B2 — it is a residual of exactly that mechanism. Read
[`docs/decisions/2026-08-21-exit-attempt-spent-is-not-the-end.md`](../decisions/2026-08-21-exit-attempt-spent-is-not-the-end.md)
before touching E1 — same reason.

## Item B2 — `held_but_unbooked`'s closing-fill cover compares a COUNT, not the actual legs

**File:** `src/argus/execution/watchdog.py`

`_book_allowance_elapsed` (line 2089) is the gate that decides whether a
`held_but_unbooked` disagreement — the broker's book shows more legs than the
ledger, the one condition deliberately built with **no timer allowance** — may
be suppressed because Argus's own recent closing fills explain it:

```python
# watchdog.py:2129
covered = self._closing_fill_cover()
excess = int(detail["broker_legs"]) - int(detail["ledger_legs"])
if excess > len(covered):
    # Not (or not fully) explained by Argus's own exits. This is the
    # unwatched-position case and it pages on sight.
    self._book_settling_since = None
    return True
```

`_closing_fill_cover()` (line 2150) returns the SET of OCC symbols
`note_fills` (line 1628) recorded as taken to flat by one of Argus's own
closing fills, pruned at `book_settle_s`. But the comparison against it is
`excess > len(covered)` — a bare cardinality check. It asks "are there at
least as many recently-closed symbols as the excess count implies," never
"are the SPECIFIC excess legs among the ones Argus actually just closed."

### Why this matters

Two spreads exit around the same time — routine at
`max_concurrent_positions = 2` with correlated stop-outs (the exact scenario
[B1's own record](../decisions/2026-08-21-alert-throttle-is-per-position.md)
treats as "the design's normal bad day, not a tail"). Both closing fills land
inside the same `book_settle_s` window, so `_closed_legs` holds four symbols
across two unrelated positions. If a real, unrelated `held_but_unbooked`
excess of 1-2 legs shows up in that same window — a genuinely unwatched
position, nothing to do with either exit — the count comparison can pass
(`excess ≤ len(covered)`) even though NONE of the covered symbols are the
excess legs in question. The page this condition exists specifically to never
suppress on a timer gets suppressed on the wrong evidence instead.

### What to investigate

1. **Is symbol-exact coverage actually available at this call site, and if
   not, what's the smallest change to make it available?** Confirmed while
   scoping this prompt: `AccountSnapshot.positions` (`account/models.py`) is
   already `tuple[Position, ...]`, and `Position` already carries `.symbol` —
   so the CACHE side already has per-symbol identity in memory. But
   `session.py`'s `_broker_position_count` closure (around line 856-878, the
   callable wired into `PositionWatchdog(broker_position_count=...)`) reduces
   it to a bare `int` (`snap.open_position_count`) before the watchdog ever
   sees it, and `_confirm_broker_position_count` does the same reduction on
   the REST side (`sum(1 for p in positions if p.signed_quantity != 0)`).
   **The ledger side is worse: `LedgerState.positions` (`execution/store.py`)
   is a bare count with no accessor to the actual symbols at this call site**
   — `_open_spreads`/`OpenSpreadScan` carries per-spread symbols elsewhere in
   the same file, but nothing threads that into `_check_broker_agreement`.
   Getting to a true symbol-exact comparison likely means widening what these
   three things hand the watchdog (or adding parallel symbol-set accessors
   alongside the existing counts) — decide whether that's worth doing, or
   whether a cheaper, more conservative fix (below) is preferable.
2. **Is there a cheaper fix that doesn't require new plumbing?** E.g.,
   requiring `excess == len(covered)` exactly rather than `≤` narrows the
   false-suppression window (two positions closing at once with an unrelated
   1-leg excess would no longer pass) but does not eliminate it (two
   positions closing with an unrelated 2-leg excess still could, if the
   counts happen to line up). State plainly whether an exact-count fix is a
   real improvement or mostly cosmetic, with the failure case it still
   admits spelled out the way this prompt spells out the current one.
3. **If symbol-exact coverage is the right fix, where does the comparison
   actually happen — the SET the broker reports minus the SET the ledger
   expects, intersected against `_closed_legs`?** Work out the actual set
   arithmetic and state it in the design, not just "compare symbols instead
   of counts." Confirm it degrades safely (fails toward paging, per the
   existing rule stated at line 1877: "a disagreement with no matching
   closing fill pages on the first observation, unconditionally") if any of
   the three symbol-set inputs is unavailable (a `None` account snapshot, a
   ledger read that only returns a count, a REST confirm that fails).
4. **Does this interact with A4b's per-shape confirm cache** (`_book_confirm`,
   keyed on `(broker_count, ledger_count)` — see
   [its record](../decisions/2026-08-21-book-confirm-cache-is-per-shape-and-expires.md))?
   That cache is keyed on the COUNT pair specifically. If this task widens
   what flows through the comparison, confirm whether the cache key needs to
   change too, or whether it can stay count-keyed while the coverage check
   underneath it goes symbol-exact — argue it, don't assume it either way.

### The one invariant that must not move

**`held_but_unbooked` must keep failing toward paging.** Every existing
failure path in this function returns `True` (page) rather than `False`
(suppress) when evidence is missing or ambiguous. Whatever shape the fix
takes, an unresolvable symbol comparison — a `None` snapshot, an
unmappable/unreadable broker read — must still page, never suppress by
default.

## Item E1 — `_attempted` never clears when a position leaves and re-enters the scan

**File:** `src/argus/execution/watchdog.py`

`_attempted` (line 1583, `dict[str, _SpentAttempt]`) tracks positions whose
one exit attempt has been spent — `check_once` (line 1768) skips any key
present in it, permanently, for the life of the process. The existing comment
at line 1692-1693 is explicit that this is deliberate:

> `_attempted` is never pruned (deliberately: clearing a key on scan-exit
> would hand a second attempt to a spread that flickered out of the ledger
> and back — which is the dangerous direction).

That reasoning is sound for the case it was written for: `_refresh_if_due`
forgets `_sustain`/`_escalation`/`_impaired` for anything that drops out of
the scan (a transient `_pair_spreads` divergence — the mid-fill window
`book_settle_s` already absorbs), and if `_attempted` cleared the same way, a
spread that flickered out and back in would look like a brand-new position
with a fresh exit attempt available — precisely the double-close risk
`_await_settled` (A3/A3b) was hardened against.

### Why this is still worth a design pass

The comment defends NEVER clearing. It does not address whether the KEY
`_attempted` clears against is the right one. It is currently
`spread.external_identifier` — the OPENING order's identifier
(`OpenSpread.external_identifier`, from `execution/store.py`). Two different
questions get collapsed onto that one string:

- "Is this the SAME held position, re-entering the scan after a transient
  divergence?" — must never get a second attempt. This is what the existing
  comment protects.
- "Has this exact opening order's position been fully closed, and has a
  GENUINELY NEW position since been opened under a colliding identifier?" —
  `candidate_id` is deterministic from `(trading_date, side, short_strike,
  long_strike)` (`execution/build.py`), so a same-day re-entry of the exact
  same strikes after a full close reuses the same `external_identifier`. Is
  that reachable? If the strategy can propose the same candidate twice in one
  session after the first one closed (worth confirming against
  `EntryExecutor._attempted`'s own one-attempt-per-candidate-per-session
  rule — a DIFFERENT `_attempted` set, in `execution/entry.py`, not this
  one), a position that closed CLEANLY hours ago could inherit a spent flag
  from an unrelated earlier occupant of the same key.

### What to investigate

1. **Is the same-key-reused-by-a-different-position scenario actually
   reachable**, given `EntryExecutor`'s own one-candidate-per-session limit?
   Trace it before proposing anything — this may turn out to be
   unreachable in practice, in which case the finding is "confirmed, but the
   current behavior is already correct for every case that can occur" and
   the deliverable is a decision record saying so, not a code change. Do
   not assume either way.
2. **If it is reachable**, what actually distinguishes "the same position,
   flickering" from "a closed position's key, now a different position"?
   `positions` rows and `order_legs.applied_quantity` are the durable ledger
   — a position that fully closed and later re-opened under the same
   candidate has a NEW `broker_order_id` for its NEW opening order
   (`OpenSpread.broker_order_id`, read off `head["oid"]`, the newest opening
   order for that key — see `_pair_spreads` in `execution/store.py`). Is
   keying `_attempted` on `(external_identifier, broker_order_id)` instead of
   `external_identifier` alone enough to tell them apart safely, or does that
   introduce a different hazard (e.g. a REPLACED order during the walk
   minting a new `broker_order_id` for the SAME attempt, which must NOT read
   as a different position)? Check `PriceWalkingLoop`'s replace semantics
   before assuming `broker_order_id` is stable across one exit attempt.
3. **Is a growth bound worth adding regardless of the above**, independent of
   correctness? `_attempted` currently grows for the life of the process with
   no eviction — CLAUDE.md's own framing of "a handful of keys per day
   against a process that restarts daily" (from the exit-attempt-spent
   record) sizes this as fine, but confirm that reasoning still holds rather
   than re-asserting it, and say explicitly if it does not need a bound.

### The one invariant that must not move

**A position still genuinely held must never get a second exit attempt.**
Any change here must be provably at least as conservative as today's
never-clear behavior for the flicker case the existing comment protects —
if the design can't state clearly how it tells "the same position, briefly
absent from the scan" apart from "a different position, same key" at the
data level, do not ship a change that clears anything. Confirming the
current behavior is correct and closing this as "checked, no change needed"
is an acceptable outcome of the design pass.

## Item C1 — `desk.py`'s comment still cites the corrected `store.py` language (no design needed, build directly)

**File:** `src/argus/tools/desk.py`, line 31.

```python
# desk.py:30-33
- **`entry_credit` on a `watchdog.check` is the ORDER LIMIT, not the fill**, and is
  documented as at-worst-slightly-low. So the mark's `cost_to_close` is used and its
  `entry_credit` is discarded in favour of the fill-derived one. Mixing them would
  quietly bias every unrealized number by the difference.
```

`store.py`'s own docstring for `OpenSpread` (around line 107-120) was
corrected in the 2026-08-20 pass — *"'Slightly' was wrong. Both occasions this
has been checked against realized fills put the gap at **9-18%**"* — but
`desk.py`'s header comment, which cites that same fact to justify discarding
`watchdog.check`'s `entry_credit` in favor of the fill-derived one, was never
updated and still uses the old "at-worst-slightly-low" wording.

**No functional impact.** The CODE in `desk.py` is already doing the right
thing (using the fill-derived `entry_credit`, not the mark's) — this is a
stale citation, not a bug. `desk.py`'s own `Mark.credit_stop` field is still
populated straight off `watchdog.check` and therefore still carries the same
9-18% bias `store.py` documents, but it is not currently rendered anywhere in
`desk.py`'s output (confirmed: `credit_stop` is read into the dataclass but
never printed) — so there is nothing live to fix beyond the comment.

**Fix:** Update the comment to match `store.py`'s corrected language — cite
the measured 9-18% figure (or point at `store.py`'s docstring by reference
rather than duplicating the number, whichever reads better in context) —
and, while touching the paragraph, note explicitly that `Mark.credit_stop`
inherits the same bias and is unrendered today, so a future change that
starts printing it inherits this comment's warning rather than rediscovering
the bias from scratch.

## Constraints (all three)

- No new runtime dependencies for any of the three.
- B2 and E1: existing tests in `tests/test_watchdog.py` covering
  `held_but_unbooked`/`booked_but_absent` suppression and the spent-attempt
  escalation must continue to pass unchanged unless the design explicitly
  changes their asserted behavior — and if it does, say which tests changed
  and why in the design write-up, not just in the diff.
- C1: comment-only, `desk.py`'s actual scoring logic must not change.

## Testing

Fake clock, no real sleeps, matching every other timing-dependent test in
this suite.

- **B2**: at minimum, a test reproducing the false-suppression scenario
  above (two positions' closing fills covering an unrelated third position's
  excess by count alone) that currently passes (wrongly) and must fail once
  the fix lands; plus the existing correct-suppression case (one position's
  own closing fill correctly covering its own excess) must still pass.
- **E1**: if a code change lands, a test for the specific case it fixes,
  plus a regression test proving the original flicker case (the one the
  existing comment protects) still gets no second attempt. If the outcome is
  "no change, current behavior confirmed correct," no new test is required —
  a decision record explaining why is the deliverable instead.
- **C1**: no test required for a comment-only change.

## Deferred / explicitly out of scope

- Any change to `A4b`'s book-confirm cache TTL policy itself
  (`watchdog.book_confirm_ttl_s`) — untouched unless B2's design concludes
  the cache key must change, per Item B2 question 4.
- `EntryExecutor._attempted` (the entry-side, one-attempt-per-candidate-per-
  session set in `execution/entry.py`) — a different set from this task's
  `PositionWatchdog._attempted`, referenced only to check reachability for
  E1, not touched.
- The confirmation-gated exit re-attempt open decision
  ([spent exit attempt](../decisions/2026-08-21-exit-attempt-spent-is-not-the-end.md))
  — related to E1 in spirit (both are about `_attempted`'s permanence) but a
  separate, larger, already-tracked open decision. Do not fold it into this
  task.
- D2 (the heartbeat block's process-lifetime counters on `/status`) — a
  fourth item from the same review, deliberately not included here; a
  separate prompt if Richard wants it picked up.

## Deliverable

For B2 and E1: a written investigation and recommendation FIRST
(`docs/designs/`, following the lifecycle in `docs/designs/README.md`) —
covering the questions posed in each item's "What to investigate" section,
with the stated invariant explicitly addressed for each. Get Richard's
sign-off before writing implementation code. Once approved: the code change
(if any — "no change needed" is a valid outcome for either), test coverage
per above, and a decision record in `docs/decisions/` (add its row to
`docs/decisions/README.md`).

For C1: build directly, no design gate. Can land in the same PR as B2/E1 if
they land together, or ship alone first — Richard's call.

Confirm with Richard whether to work on a branch or directly on `main`, and
whether to commit — no standing convention is recorded for this repo either
way; do not assume. Do not push regardless, unless explicitly told to.
