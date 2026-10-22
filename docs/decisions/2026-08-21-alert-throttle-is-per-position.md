# A per-kind alert throttle suppressed the second position (2026-08-21)

*The 900s coalescing window was calibrated against one condition flapping on one
process-wide signal, then applied to every alert kind — including the ones where a
repeat inside the window is a DIFFERENT open position. Two correlated stop-outs is
the documented expected case here, not a tail, and the second one arrived as a bare
`(+1 suppressed)` up to fifteen minutes late.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

Finding B1 of the 2026-08-21 pass. Found by reading, in the course of the same
session's escalation work (A2), which is what made it worth doing immediately:
`_page_if_no_exit_yet`'s phone-volume argument leans directly on this throttle as
its outer rate bound. Closed the same session.

**This was a notification-latency defect and never a record defect.** Every
suppressed alert was still written Tier 1 to the audit log by its own call site,
and every one still reached the journal through `log_alert` — `combine()` fans out
to both sinks and only the Slack one throttles. What was late was the phone.

## The defect

`SlackNotifier.__call__` keyed its throttle on `kind` alone:

```python
last = self._last_enqueued_at.get(kind)
if last is not None and (now - last) < self._throttle_s:
    self._suppressed_by_kind[kind] = self._suppressed_by_kind.get(kind, 0) + 1
    return
```

That window was added on 2026-08-05 against `loop_lag_breach`
([the alert storm](2026-08-05-slack-alert-storm.md)): one condition, flapping every
ten seconds, on a process-wide signal. There, a repeat inside the window genuinely
IS the same condition continuing, and folding it into a count is exactly right —
`(+58 suppressed)` says "continuously" where 58 messages say nothing at all.

The rule was then applied to every kind, and **half the callers are not shaped like
that.** A stop-out pages as `watchdog_triggered` and then `watchdog_exit_filled`.
Neither kind carries anything per-position; the position is in the payload. So two
positions stopping out inside one window are, to this throttle, one condition
repeating.

### Why that is the expected case, not a tail

- `risk.max_concurrent_positions = 2`, and the gate counts SPREADS
  ([overlapping verticals](2026-08-17-overlapping-verticals.md)).
- Both spreads are the same underlying, the same expiry, and frequently the same
  side — `signal.width = 5.0` against SPX's 5-point ladder makes consecutive
  candidates share a strike, which is the *common* shape for two same-side spreads.
- The virtual stop's triggers are `CREDIT_MULTIPLE` and
  `UNDERLYING_NEAR_SHORT_STRIKE`. A move that breaches one spread's short strike is
  routinely the same move that breaches the other's.

So the correlated double stop-out is the design's normal bad day. The operator saw
a full page for the first spread and, for the second, nothing until the first
spread's next message — up to 900s later, and then only as a count.

### The escalation interaction, confirmed

The same session's A2 work bounded the silence around a position that cannot be
closed, with `_Escalation.overdue` repeating per
`watchdog.exit_unattempted_max_s`. Its docstring defends the repeat by pointing at
this throttle:

> the rate is bounded twice over … and `SlackNotifier` throttles per kind at 900s

Correct as a rate argument about one condition on one position — and it does not
transfer. `overdue` is per `(position, condition)`: `_Escalation` keys on the
spread's identifier, so two positions each cross their own bound and each call
`_notify("watchdog_exit_still_unplaced", …)`. Same kind. **Confirmed against
source, then reproduced**: `test_two_positions_crossing_the_bound_together_both_
reach_the_phone` drives two chained put verticals through one move that leaves both
triggered and unpriceable, with a real `SlackNotifier` behind the alert seam. Before
the fix it delivered one page; both positions were in the audit trail throughout.

This is the worst place for the defect to land. The three conditions A2 and its
predecessor bound — `_UNATTEMPTED`, `_UNPRICEABLE`, `_SPENT` — all mean "the
watchdog has decided this position must be closed and it is not closed". Two of
those at once is precisely when the operator needs to know there are two.

## The fix

The throttle key is `(kind, scope)`. `scope` comes from the detail payload:

```python
SCOPE_FIELDS = ("candidate_id", "external_identifier")

def alert_scope(detail: dict[str, Any]) -> str:
    for field in SCOPE_FIELDS:
        value = detail.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:MAX_SCOPE_CHARS]
    return ""
```

A kind that names no subject gets `""` and behaves exactly as it did before. **No
config key, no `AlertFn` signature change** — the image deploys alone.

Four things about this are decisions rather than implementation.

**1. Generic, not an allowlist of per-position kinds.** An allowlist would have to
be maintained alongside every new alert, and the cost of forgetting an entry is
silent: the kind quietly folds two positions together again, invisible until the
day it matters. Reading the subject out of the payload means an alert that names
its position gets its own window *because it named it*.

**2. The subject is the CANDIDATE, not the order identifier.** One position has at
least two `external_identifier`s over its life — the opening order's, which
`OpenSpread` carries, and the closing order's, which `ExitReport` carries — and
watchdog payloads about one spread contain both. Keying on the order splits one
position's episode across two windows. Harmless in direction (more messages, never
fewer) but incoherent, and it would stop matching `_Escalation`, which is per
position. `external_identifier` stays as the fallback because the price-walking
loop's alerts carry it and no candidate at all.

**`broker_order_id` is deliberately not a scope field**, though several payloads
carry it. A walk cancels and replaces, so one exit produces a new broker order id
per rung — the fact CLAUDE.md's no-broker-side-stops rule turns on. Scoping on it
would give one unthrottled message per reprice: the 2026-08-05 storm rebuilt out of
the fix for it.

**3. A cardinality cap, because an open-ended key is an open-ended message rate.**
N distinct scopes inside one window is N delivered messages, and the whole reason
this class throttles is that a caller nobody predicted turned out to be chatty. Past
`MAX_SCOPES_PER_KIND` (8 — the real working set is 2 spreads × entry and exit
identifiers), new scopes of that kind fall back to the kind's single unscoped
window. **The degradation is to the older, safer behaviour; noisier is not one of
the options.** A scope already tracked keeps its window regardless of the cap, so a
position mid-episode cannot be demoted by unrelated traffic on the same kind and go
silent for the rest of the window.

**4. Windows expire at `2 × throttle_s`.** The state is now per-detail in a process
that runs all day, every day, and the previous comment's bound — "the number of
distinct alert kinds in the codebase" — no longer holds it. A window stops
suppressing at `throttle_s` and is kept for one more so a condition flapping just
outside the window still gets its count on the next message. Past that a pending
count is not a deferred summary any more: the message it would ride on says "in the
last 900s", which would be false. It is not silently lost either — every one of
those repeats is in the journal, and the discard is logged with its count.

### The subject is on the first line

`[argus:sandbox] watchdog_exit_still_unplaced [20260821-put-6300.0-6295.0]`

A phone's notification preview is one line, and two correlated stop-outs are now two
messages whose kinds are IDENTICAL. Without the subject in the header the operator
cannot tell the second position's page from a redelivery of the first's — which
would deliver the message and keep the ambiguity that made the old behaviour bad.
Same reasoning that put the suppressed count there in 2026-08-05.

### Payloads had to name their subject

Two watchdog escalation payloads carried the position only inside a nested
`position` dict, so `alert_scope` could not see it: `_trigger`'s `_UNPRICEABLE`
payload and `_run_exit`'s `NOT_ATTEMPTED` payload — which between them feed
`watchdog_exit_unpriceable`, `watchdog_exit_unattempted` and, through
`_page_if_no_exit_yet`, the repeating `watchdog_exit_still_unplaced`. Both now carry
`candidate_id` at the top level.

**Burying it again silently reverts this**, which is why `_Escalation`'s docstring
and `_page_if_no_exit_yet`'s now say so at the point where someone would tidy it
away. `alert_scope` reading one level down into `position` was rejected: a notifier
that knows the shape of a watchdog payload is a worse coupling than a payload that
names its own subject.

## What this does not change

- **The 2026-08-05 fix.** One position's condition continuing is still one message
  per window with the count on the next. `test_the_same_position_repeating_still_
  throttles` and `test_a_kind_with_no_subject_keeps_exactly_the_old_behaviour` pin
  it, and both survive the mutation that disables scoping — which is the point of
  having them.
- **`throttle_s` stays 900s and stays a module constant**, for the reasons in the
  2026-08-05 record: it shapes notification cadence and nothing about trading
  behaviour, so it does not belong in the fail-closed loader, and adding a REQUIRED
  key would couple this to a config deploy.
- **Any audit record.** The Tier 1 trail was complete before this change and is
  unchanged by it, apart from two payloads gaining a `candidate_id` they should
  always have had at the top level.

## Verification

Six new tests in `tests/test_notify.py` and one end-to-end test in
`tests/test_watchdog.py`. The watchdog one is the only test in that file with a real
`SlackNotifier` behind the alert seam, deliberately: the defect lives in the seam,
so with a recording list on the other end every version of this passes.

Both halves were mutation-checked — disabling the scope key fails six tests
including the end-to-end one, and un-lifting `candidate_id` from the unpriceable
payload fails the end-to-end one on its own.

## What is still open

**`watchdog_book_disagreement` stays unscoped, correctly.** Its detail carries leg
counts and a direction, no identifier, because the condition is about the BOOK and
not about a spread — `_Escalation` keys it on the `__book__` sentinel for the same
reason. It gets `""` and coalesces per kind, which is what it should do.

**Nothing here has been exercised in live fire.** The correlated double stop-out is
argued from the configuration and the trigger shapes, not measured: the sandbox has
never produced two simultaneous stop-outs, and cert's delayed quotes make its
trigger timing unrepresentative anyway. What is measured is the mechanism, in tests.
