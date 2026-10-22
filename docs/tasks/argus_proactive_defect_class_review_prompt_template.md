# Argus — proactive defect-class review (reusable template)

## Purpose

Every fix so far has come from something breaking live in shadow, sandbox,
or cert. That's partly unavoidable — some behavior only shows up under real
market data and real broker timing, and this review can't replace that. But
several of the bugs found this way turn out, in hindsight, to be instances
of a small number of recurring shapes. This prompt asks Claude Code to hunt
for other latent instances of those *specific* shapes, before they get
exercised — instead of a generic "review the code for bugs" pass, which
tends to be noisy and low-signal.

## Mode: report-only, always

This prompt never authorizes a code change. Output is a single written
report: one section per defect class, each finding rated by confidence
(confirmed / suspected / speculative) and priority, with the exact
file:line and a one-paragraph explanation of why it matches the pattern.
No diffs. No "while I was in there" fixes. A finding that turns out to be
intentional and already-reasoned-about is still worth reporting as
"checked, no issue" so it doesn't get re-audited next pass — say so
explicitly rather than omitting it.

## When to run this

Not continuously — after a release has held clean for a stretch (the
quiet-day pattern, like today after v0.3.5) is exactly when a proactive
pass has the best signal-to-noise, since the sharpest issues already
surfaced reactively. Also run it as a pre-flight, scoped to the classes
below, before any environment-gating flip (dry-run → live, sandbox →
live-fire) — several past incidents (the 8/13 concurrent-risk gap, the
0.2.0 alert storm) surfaced exactly at those transitions.

## Defect classes to search for

### Class A — an invariant built for one call path, silently reused on a sibling path where its precondition doesn't hold

Pattern: a safety check (staleness gate, identity requirement, a "must be
fresh" assumption) is written for entry, then exit code reuses the same
function or config value without re-deriving whether the invariant still
makes sense in the other direction. Today's Item 1 (`signal_max_age_s`
gating exit orders) is this. Fix C's `.leg`-vs-`.contract` distinction
(watchdog.py:1013, "the one call site needing an actual price... correctly
left refusing") is the same class caught correctly.

Search for: every staleness/freshness/identity check shared between
entry.py and the exit/walk path; every place a config value's docstring or
comment describes it in terms of one direction ("gate", "construction",
"entry") but is read from both.

### Class B — a threshold or allowance calibrated against data from one condition, applied to both

Pattern: a value gets tuned or added using a specific day's/scenario's data
(e.g., an entry-only day) and the code applies it symmetrically without the
tuning ever covering the untested direction. Today's Item 2
(`book_settle_s` on `booked_but_absent` but not `held_but_unbooked`) is
this, and it's explicitly the same class as the 2026-08-05 alert storm —
two confirmed instances of this exact shape already.

Search for: every timing/count/rate allowance in watchdog.py and
reconcile.py — for each, confirm whether its calibration comment or commit
message names the condition it was derived under, and whether the code
applies it to conditions outside that scope.

### Class C — a code comment describes a bound that was reasoned about, not measured, and the measured value has since diverged

Pattern: "at worst slightly X" or similar hedged language in a comment,
written when the actual gap wasn't yet known. Today's Item 3
(`store.py:106`, "at worst slightly LOW" vs. a measured 18%) is this.

Search for: comments containing hedging language ("slightly", "roughly",
"should", "at worst", "in practice") near any P&L-adjacent or
threshold-adjacent computation — flag ones with no adjacent test or logged
measurement backing the claim.

### Class D — status/metadata fields set once, never refreshed, misleading on read even though underlying state is correct

Pattern: `kill_switch.since`, and the already-known GET/POST `/kill`
vs. `/status` dispatch inconsistency. Low-severity individually, but worth
sweeping once rather than re-discovering one at a time.

Search for: every field surfaced in `/status` (JSON and HTML) that's
written at one point in time and never updated on subsequent reads of the
same logical state.

### Class E — a branch that discards identity or state on early return, silently blinding a downstream consumer that assumed it was still populated

Pattern: Fix C's original bug — a not-in-chain branch dropped the resolved
contract, blinding the underlying backstop trigger. Caught and fixed once;
worth checking whether the same shape (early return that drops something a
caller two frames up assumes survives) exists elsewhere.

Search for: early-return branches in resolution/lookup functions
(LegQuotes.resolve and anything structurally similar) — for each, confirm
every caller's assumptions about what's still set on that branch.

## Explicitly out of scope for this pass

Don't re-derive whether any *specific* already-closed finding was fixed
correctly — that's what the closed decision docs are for. This pass is
for *new* instances of the same shapes elsewhere in the codebase, not a
re-audit of prior fixes.
