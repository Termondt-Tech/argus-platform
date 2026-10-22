# Argus: Assess P3–P6 (2026-08-28 taxonomy record) for inclusion in v0.3.13

## Priority and authorization

**Richard has explicitly authorized this work**, weekend of 2026-08-28/29, in these
words: *"assess including the design and development of P3, P4, P5, and P6 into
this same release... it is the weekend, and my preference is not to sit around and
wait all weekend for Monday. I'd rather build a stable, resilient, and properly
functioning product for Monday morning open."* This satisfies
`docs/tasks/README.md`'s non-negotiable ("explicit command from Rich is required")
for this specific batch — P3–P6 only. Do not pick up any other backlog item on the
strength of this authorization.

**But authorization to work is not authorization to deploy.** Nothing here should
be tagged, released, or pushed live without Richard reviewing it. The goal for
Monday is a *reviewable, tested, well-documented* set of changes waiting for a
five-minute look — not a fait accompli. Work on a branch, not `main` (see
"Mechanics" below), so `main` stays exactly as Richard left it (v0.3.13/Tejat draft,
P0+P1+P2, `HEAD` at the time of writing: `dc3eb87` plus the release-doc commit that
follows it) until he says merge it.

**Re-confirm every claim below against current `HEAD` before acting on it.** This
record was written the same evening the code was — normal drift risk, not expected,
but check.

## Priority tiering — this is the part that matters most

The four items are NOT equally risky, and treating them as one undifferentiated
batch is the mistake to avoid. Same posture the 2026-08-20 B2/E1/C1 prompt took:

- **P4 and P6 — build directly, no design gate.** Mechanical, additive, low blast
  radius. Do these first; they are the highest confidence weekend output.
- **P5 — design first, in writing, before touching code.** It is a new *automated
  recovery action* on the exit path, and CLAUDE.md's own open-decisions list
  already warns about this shape: *"never fix an unwatched spent position by
  retrying the exit"* is the standing caution for anything that looks like "detect
  a bad state, then act on it automatically" near a position that can double-close.
  Write the design (a decision-record draft, same depth as
  `docs/decisions/2026-08-28-time-based-flatten-trigger.md`) and get it to a state
  you would stake the "no re-litigating" convention on — but hold off on writing
  the implementation until Richard has seen the design, even if that means it
  waits past this weekend.
- **P3 — design only, explicitly gated on Richard's sign-off before any code.**
  This is the one the record itself called out: *"needs a design decision
  first."* It touches the kill-switch/halt scope, which is a stated non-negotiable
  (`CLAUDE.md`, "Broker is the only source of truth" + the kill-switch scope
  decision, 2026-08-03) and a boundary that has already been gotten wrong once in
  each direction (2026-08-17's halted-but-alive fix; the 2026-08-28 record's
  Finding 7 itself). **Do not build P3 this weekend.** Produce the best design
  document you can — including a concrete recommendation — and stop there. If the
  design is clean enough that implementing it feels like the obvious next hour of
  work, that is exactly the moment to stop and wait for Richard instead of
  continuing on your own judgment; the cost of being wrong here has already been
  measured twice.

If time runs out, the right order to have made progress in is P4, then P6, then
P5's design, then P3's design — not partial progress spread evenly across all four.

## Context to read first, in order

1. `CLAUDE.md` — current phase, the non-negotiables (especially the kill-switch
   scope, "broker is the only source of truth" and its two sanctioned stand-downs,
   and the standing landmines).
2. `docs/decisions/2026-08-28-exit-attempt-taxonomy-and-halt-scope.md` — the
   source record. Findings 3 (closed, →P2), 6, 7, 8 map to P4, P3, P5
   respectively; P6 is Finding 8's sibling ask about diagnosability rather than
   recovery. Read the "Ranked plan" table and the "What P0 and P1 actually are"
   section for the reasoning style expected here.
3. `docs/decisions/2026-08-28-time-based-flatten-trigger.md` — the P2 design
   record from this weekend's earlier pass. Use it as the quality and depth bar:
   named design questions, explicit rejections with reasoning (not just
   conclusions), a "what was NOT done" section, mutation testing, a "still open
   after this" section.
4. `docs/decisions/README.md` — the open-questions and open-decisions tables at
   the bottom. P5 in particular overlaps the recorded open decision *"should a
   confirmation-gated exit RE-ATTEMPT exist?"* — read that row before designing
   P5; do not treat P5 as unrelated to it.
5. `docs/releases/v0.3.13.md` (DRAFT) — the release these might join. Read its
   "Scope" section, which already anticipates P3/P4/P5/P8-numbering as open.
6. `docs/tasks/argus_b2_e1_c1_stability_residuals_prompt.md` — not because its
   content is relevant, but because its STRUCTURE (priority tiering, explicit
   sign-off gate on the risky items, direct-build permission on the safe one) is
   the template this document follows and the new session should follow in turn.

## Item P4 — count working orders toward `max_concurrent_positions`

**Files:** `src/argus/risk/models.py` (`RiskSnapshot.open_spread_equivalent`),
`src/argus/risk/gate.py`, wherever the snapshot is assembled (`orchestrator.py` /
`session.py` — confirm at HEAD).

**Finding 6, verbatim:** *"the concurrency cap counts POSITIONS, not ORDERS...
Between submit and fill an order is uncounted exposure; normally the walk owns
that window for seconds. When the walk abandons an order whose death it cannot
confirm, the window runs to the close. Had [the abandoned order] filled, it would
have been a third spread against a cap of two."*

Confirmed still open at HEAD (checked 2026-08-28 evening,
`open_spread_equivalent` still `max(ceil(broker_legs/2), ledger_spread_count)`,
no working-order term).

Design questions to settle, briefly — this one does not need a full record, but
does need these stated somewhere in the commit/PR description:

- **Where does "working order count" come from?** The entry path's
  `DuplicateOrderGuard` / `get_live_orders()` already classifies orders as
  WORKING; the exit path's abandoned-order tracking is the scenario that
  motivated this finding. Prefer reading it from the same broker-derived source
  the existing gate already trusts, rather than adding a second one.
- **Does this apply to exit-side working orders too, or only entry-side?** An
  exit's own market-crossing order is already closing a position that is
  currently counted; double-counting it would UNDER-count available concurrency
  for new entries at exactly the wrong moment. Read `CLAUDE.md`'s welded
  invariant (`max_concurrent_positions × max_loss_per_position ≤ daily_max_loss`)
  before changing anything here — a change to what the gate counts is exactly the
  kind of change that invariant's own comment warns must be re-checked.
- **What test proves this?** At minimum: an abandoned working order that would
  push a NEW entry over the cap is refused; a working order that is one of the
  spread's OWN exit legs does not double-count against a re-entry check that
  should not exist anyway (entries and exits are different call sites).

## Item P5 — mid-session re-query after a failed cancel (DESIGN ONLY this weekend)

**File:** `src/argus/execution/loop.py` (`_request_cancel`, confirmed at HEAD:
still documented as best-effort with restart-time reconciliation as the only
stated backstop — no mid-session re-query found).

**Finding 8, verbatim:** *"`_request_cancel` is best-effort by design and says the
backstop is restart-time order reconciliation. Nothing re-queries mid-session.
Today that left a working order live for two hours, and it is what eventually
halted the process."*

This is a new automated action — "if a cancel request may not have landed, check
again later and do something about it" — on the path CLAUDE.md's non-negotiables
say the least about touching casually ("no auto-retry" is stated elsewhere for the
audit writer specifically, but the SPIRIT — an unconfirmed state must not be
silently re-acted upon — applies here too). Design questions the record itself
leaves open and this document does not answer for you:

- **What does "re-query" DO on finding the order still working?** Re-request the
  cancel (idempotent, presumably harmless)? Escalate to a human? Both? Re-check
  against `_await_settled`'s existing confirmation logic — a re-query that reaches
  a positive "still working" answer is exactly the state that already routes to
  `CROSS_REFUSED` on the exit path today; a re-query on the ENTRY path (this
  finding's actual scenario — the walk's own abandoned order) may need different
  handling because there is no "cross" concept on entry.
- **Where does the re-query run?** Inside the walk loop (extending its own
  lifetime past what `working_timeout_s` already bounds), or as a separate
  watchdog-style task that outlives the walk? The 2026-08-28 incident's order
  outlived the walk that abandoned it by roughly two hours — a re-query living
  only inside the walk's own loop would not have caught it.
- **How does this interact with `DuplicateOrderGuard`?** If the re-query itself
  is not careful about single-writer / no-double-cancel semantics, it becomes a
  second thing racing `_request_cancel`'s own best-effort call.
- **Read the "should a confirmation-gated exit RE-ATTEMPT exist?" open decision**
  (`docs/decisions/README.md`) before designing this — it is very plausibly the
  SAME decision wearing a different trigger (a failed cancel vs. a spent exit
  attempt), and designing them independently risks two mechanisms that disagree
  about when a re-attempt is safe.

**Deliverable for this weekend:** a design document at
`docs/decisions/2026-08-2X-mid-session-cancel-recheck.md` (or folded as an
addendum into the taxonomy record, your call, but pick one and say why) at the
depth of the P2 record — decisions with reasoning, explicit rejections, a
recommendation. **No implementation PR for this item until Richard has read it.**

## Item P6 — thread `reject_reason` onto `WalkResult`/alerts; give `TimeoutError` a message

**Files:** `src/argus/execution/loop.py` (`WalkResult`, wherever a rejection
outcome is built and wherever `TimeoutError` is raised/caught on this path).

**Confirm scope against v0.3.12 first — this may be partially done.**
v0.3.12 (Eltanin) already added `Order.reject_reason` mapped from the SDK's
`PlacedOrder.reject_reason` and promoted `order.event` to Tier 1 on an order's
first terminal `dead` sighting
(`docs/decisions/2026-08-27-greeks-selector-bound-and-order-rejection-visibility.md`).
Grep confirms `reject_reason` does NOT currently appear in `loop.py` or
`watchdog.py` (checked 2026-08-28 evening) — so the `order.event` record has it,
but `WalkResult` (what the entry/exit escalation paths and alerts actually see)
does not. Verify this distinction still holds before building: the value of P6 is
specifically getting the reason onto the SAME surface that already reads
`WalkResult.reason` for every other outcome, so an operator does not have to
correlate a walk's `reason` string against a separate `order.event` record by
hand.

Second half: `grep -n "TimeoutError" src/argus/execution/loop.py` currently
returns nothing — find where a bare `TimeoutError()` (no message) is raised or
where `asyncio.TimeoutError` is caught and re-raised/reported without a message,
and give it one naming what was waited for and how long.

This is additive, low-risk, no new config key expected. **Build directly** —
design questions are "which existing outcome-reason strings should carry it" and
"does an empty `reject_reason` (order not yet rejected, or a non-order outcome)
render as `None` rather than an empty string" (same discipline the P2 record used
for `cost_to_close: None` vs `0.0`). Test coverage: a rejected order's `WalkResult`
carries the broker's `reject_reason`; a non-rejected outcome carries `None`; the
`TimeoutError` message names what timed out.

## Item P3 — halt scope split (DESIGN ONLY, sign-off required before ANY code)

**Files:** likely `app.py` (`_halt_process`), `reconcile.py`
(`mismatch_halts_process`), `session.py` (`run_session`'s teardown), possibly
`execution/watchdog.py` if the split needs the watchdog to survive a halt it
previously would not have.

**Finding 7, verbatim:** *"a reconciliation halt stops the watchdog on healthy
positions... The rail is correct... The response is disproportionate... The
design question for next session: should an ORDER-level discrepancy, with no
position implication, stop POSITION monitoring? A defensible split is: halt new
entries always, but let the watchdog run in read-and-exit-only mode when the
discrepancy does not concern the positions it holds."*

**This is now more consequential than when the record was written**, because P2
(built this session) put the time-based flatten trigger on the watchdog — so a
halt that stops the watchdog no longer only removes the two price triggers, it
removes the one control that needs no market at all and exists specifically for
the failure mode where everything else has stopped working. Say this plainly in
the design document; it changes the cost-benefit of leaving P3 undone, even
though it does not change the recommendation to hold off building it this
weekend.

Design questions, none of which have an obvious safe default:

- **What exactly counts as "no position implication"?** The record's own example
  (`unexpected_working_order` on a symbol/strike Argus never intended to hold, 30
  points OTM, unfilled) is close to the easy case. A `quantity_mismatch` on a
  symbol that OVERLAPS a currently-watched spread's own legs is the hard case —
  and CLAUDE.md's chained-verticals rule (`2026-08-17-overlapping-verticals.md`)
  is exactly the reason a "does this touch a held position" check cannot be a
  naive symbol match.
- **Does the watchdog need a genuinely new mode ("exits only, no new stop
  evaluation")?** — no, that seems wrong on reflection: the watchdog's job on a
  healthy position is unchanged, it should keep evaluating triggers and closing
  positions normally. The narrower and probably correct framing: the halt should
  stop `SignalLoop` (new entries) and leave `PositionWatchdog` untouched, i.e.
  **decouple `_halt_process`'s effect on the watchdog from its effect on entries**
  rather than inventing a third watchdog mode. Verify this framing against how
  `_halt_process` and `run_session`'s teardown are actually wired before
  committing to it in the design doc — it may be that simple, or there may be a
  reason (shared `stop` event) that it is not.
- **Does the kill-switch scope decision (2026-08-03) already answer half of
  this?** It deliberately does NOT stop exits. `_halt_process` is a DIFFERENT
  seam (internal halts: audit-write failure, reconcile mismatch, watchdog
  exception) that happens to reuse the same `stop` event and therefore, as a
  side effect, also stops exits — which the record calls "in tension with" the
  kill-switch decision. Resolve whether `_halt_process` should stop using the
  SAME event the kill switch does at all, or a narrower one. This is the crux of
  the design and deserves the most words in the write-up.
- **What is unaffected?** State plainly, the same way the P2 record does, what
  this must NOT change: a genuine POSITION-implicating mismatch (broker holds
  something Argus does not expect on a symbol a watched spread's legs occupy, or
  vice versa) must still halt everything, watchdog included — "broker is the
  only source of truth" is a non-negotiable, not a preference to be traded off
  for convenience.

**Deliverable for this weekend:** a design document at
`docs/decisions/2026-08-2X-halt-scope-split.md`, to the same depth as the P2
record, ending with an explicit recommendation and an explicit "NOT building
this without your review" line. No code.

## Mechanics

- **Work on a branch, never `main`.** `git checkout -b p3-p6-scope-assessment` (or
  a name of your choosing) as your first action, before any edit. This repo's
  harness appears to auto-commit file changes as they are made (observed:
  `dc3eb87` and `dad2bd0` landed without an explicit `git commit` invocation in
  the session that produced them) — confirm this behavior still holds, and if so,
  commit checkpoints after each completed item (P4 done+tested, P6 done+tested,
  P5 design written, P3 design written) rather than one giant commit, so partial
  weekend progress is inspectable and nothing is lost if the run is interrupted.
- **Never touch `argus.db`.** Any dev/tool run uses `ARGUS_CONFIG=argus.dev.toml`
  or `--config argus.dev.toml`. This is a repo non-negotiable, not a suggestion.
- **Run the full suite after each change**, not just the new tests:
  `./.venv/bin/python -m pytest`. 1,610 passing at the time of writing (P0+P1+P2 +
  this weekend's release-doc pass); note the count in your final summary so drift
  is visible.
- **Update `docs/decisions/README.md` and `docs/releases/v0.3.13.md`** for
  whatever actually lands (P4, P6, and design records for P5/P3), following the
  same convention this file's own references demonstrate. If P4/P6 are ready and
  P5/P3 are design-only, say explicitly in `v0.3.13.md`'s Scope section that the
  release is P0+P1+P2+P4+P6, with P5 and P3 recorded as designed-but-deferred —
  do not silently fold a design doc into "done."
- **End with a written recommendation to Richard**, not just a diff: which items
  are ready to merge as-is, which need his sign-off before implementation (P3
  always; P5 unless he explicitly clears it after reading the design), and
  whether v0.3.13's scope should be P0+P1+P2 alone (ship what's proven), or
  P0+P1+P2+P4+P6 (ship the additive, low-risk pair too). Do not recommend
  shipping P3 or P5 this weekend under any circumstance — that recommendation
  is fixed by this document, not yours to revisit.
