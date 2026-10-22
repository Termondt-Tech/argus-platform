# Argus: Gate Decision Audit Tiering — Edge-Triggered Tier 1, Level Tier 2

## Priority

Not urgent. At 0.20 Tier 1 records/sec the current cost is negligible and there's no
defect in gate.py today. This is backlog-priority, safe to schedule whenever — filed
now because the shape of the problem and its fix are both clear, not because anything
is broken.

## Context

`gate.decision` is written at Tier 1 unconditionally — every tick, every outcome. On
2026-08-13, both position slots were filled at 10:00:30 and every tick from then to
15:00 proposed a candidate and got refused. Correct behavior. But each of those refusals
generated its own Tier 1 record: 2,315 `concurrent_limit` denials + 11 `staleness`
denials + 2 approvals = 2,328 Tier 1 `gate.decision` writes, almost exactly 50% of the
day's entire Tier 1 volume (4,651, from the evaluability-run baseline).

Structurally this is the same shape as the evaluability flap closed in Fix C: a Tier 1
record per tick for a condition that hasn't changed since the previous tick. The
difference is that a gate denial isn't noise — it's the risk rail's decision trail, so
this is **not** a candidate for wholesale downgrade. The fix has to preserve full
forensic detail on every single decision; only the durability tier changes.

## Precedent already in the codebase

Two existing splits do exactly this pattern — cite both as the model to follow, don't
invent a third:

- **Fix C** (`watchdog.degraded` vs `watchdog.unevaluable`): `TriggerEvaluation.blind`
  still pages via Tier 1 `watchdog.unevaluable`. Single-trigger-dark is Tier 2
  `watchdog.degraded`, logged on the rising edge only, no page — explicitly to avoid
  audit-writer-lock contention from 1s-cadence stale/fresh flapping. Recovery
  (degraded → evaluable again) still writes Tier 1.
- **Item 1** (`reconcile.expired_positions` vs `reconcile.expiry_check`): Tier 1
  per-row for an actual resolution action; Tier 2 for the check that finds nothing to
  excuse.

Both follow the same rule: **Tier 1 on the edge (state transition), Tier 2 on the level
(state persists unchanged).** Apply that rule to `gate.decision` denials.

## Proposed approach

For `gate.decision`:

- **Approvals: always Tier 1, unconditionally.** No change. These are rare (2/day),
  each one is a real state change with real consequence, and none of the volume problem
  comes from here.
- **First denial in a streak: Tier 1.** "First" means the immediately preceding
  `gate.decision` was an approval, was a denial with a *different* reason, or there is
  no preceding decision (process start).
- **Repeated denial, same reason as the immediately preceding decision: Tier 2.**
  Same category (`gate.decision`), same full payload (candidate proposed, reason, any
  relevant snapshot fields — open position count, staleness threshold values, whatever
  the current Tier 1 payload carries). Only the durability tier changes.
- **Reason changes mid-streak** (e.g. `concurrent_limit` → `staleness`): Tier 1 — this
  is an edge too.

State needed: just the previous decision's reason, tracked in-memory, scoped to the
running process. Restart resets it, so the first denial after any restart is Tier 1 —
that's the correct conservative default (same posture as `compute_regime` returning
NaN rather than None so a bad chain can't silently disarm a check), not a gap to
engineer around. Don't add cross-restart persistence for this.

## Non-goals — must hold

- No change to the gate's actual deny/approve logic, thresholds, or the
  `max_concurrent_positions=1` behavior from Fix A. This is an audit-tiering change
  only.
- No change to approval tiering.
- No payload reduction at Tier 2 — every denial record, Tier 1 or Tier 2, keeps the
  same fields it has today. Don't sample, batch, or truncate.
- No change to what the gate returns to the caller or to any paging/Slack behavior tied
  to the decision itself — only the audit record's durability characteristics move.

## Before implementing

Confirm against actual source rather than this brief's assumptions, and report any
correction rather than building around it — this repo's established pattern (see the
`candidate_id` join-key correction, the OCC-parser gap, and the ITM-assignment
inversion from prior sessions):

- Locate the actual `gate.decision` write site (likely `gate.py`, given the earlier
  `gate.py:68` kill-switch reference) and confirm it fires once per tick for the whole
  system, not once per slot.
- Confirm the denial-reason field name/enum and that it's the right granularity for the
  "unchanged" comparison (reason only, not candidate identity — a different candidate
  proposed on consecutive ticks that both get refused for `concurrent_limit` should
  still collapse to Tier 2, per the observation as written).
- Confirm current Tier 1 payload shape for `gate.decision` so Tier 2 can match it
  exactly.

## Testing requirements

Mirror the test patterns already established for the two precedents above:

- Streak collapse: N identical-reason denials in a row → first is Tier 1, remainder
  Tier 2.
- Edge on reason change: denial reason A → denial reason B → the B record is Tier 1.
- Edge on recovery: denial streak → approval → approval is Tier 1 (already true by the
  "approvals always Tier 1" rule; test it explicitly for the transition case too).
- Process-start edge: first `gate.decision` of a fresh process, if a denial, is Tier 1.
- Distinguishability test: `gate.decision` Tier 1 vs Tier 2 records differentiable by
  category + tier + payload shape, same style as the existing tests for
  `watchdog.degraded`/`watchdog.unevaluable` and `reconcile.expired_positions`/
  `reconcile.expiry_check`.
- Payload-completeness test: a Tier 2 denial record carries the same field set as a
  Tier 1 denial record for the same reason — nothing dropped.

## Deliverables

- Code change scoped to the `gate.decision` write path only.
- CLAUDE.md updated (required every session).
- Decision doc added under `docs/decisions/`, following the existing naming and content
  convention (see `2026-08-12-expired-position-resolution.md` for shape).
- Test suite green, new/total count reported.
