# Argus: Evening-Restart Expiry Resolution — Same-Day Halt + Orphaned `order_legs`

## Priority

Not urgent. Neither defect below cost a trading day or put a real position at risk —
both are documented, understood, and workable around today (wait past ET midnight
before restarting; the false `watchdog.unwatchable` page is noise, not signal).
Filed because the shape and fix of both are now clear, not because anything is
currently broken. Backlog priority — schedule whenever.

## Context

Both items surfaced from the SAME event: a `v0.3.8` evening deploy on 2026-08-24,
restarting the process at ~21:02 ET on a day that held two 0DTE positions to expiry
(a call spread and a put spread, both settled worthless/full-credit at 16:00 ET,
zero closing fills — normal 0DTE resolution). Read `docs/decisions/2026-08-12-expired-position-resolution.md`
in full before touching either task — it is the existing, settled design both of
these extend, not replace, and it explains the mechanics (why 0DTE expiry never
produces a fill event, why `positions` rows outlive the position, why the rule is
`missing_at_broker` + strictly-past-expiry only). Both tasks below are gaps in that
design's original analysis, not regressions — the design doc's own "Checked and
found not to be a problem" section only verified `/status`'s `open_position_count`;
it never traced the watchdog's own spread-reconstruction scan (Task 2), and it
never considered a restart happening on the SAME evening as the expiry rather than
the next morning (Task 1).

Neither task touches `v0.3.8`'s actual diff (`signal/live_chain.py`,
`signal/strategy.py`, `session.py`'s chain-coverage print sites, `tools/order_smoke.py`)
— these are `reconcile.py` and `execution/store.py`/`execution/watchdog.py`,
respectively.

---

## Task 1: Same-day expiry carve-out blocks evening restarts

**Problem:** `reconcile.py`'s `expiry_verdicts` resolves a `missing_at_broker` row
only when `expiry < today` (`KEPT_NOT_YET_EXPIRED = "expiry_is_today_or_later"`
otherwise) — deliberately, per the design doc: *"Within its own trading day the
position can genuinely still be held, and the cost of being wrong in that direction
is deleting Argus's record of a live position."* Sound reasoning for an intraday
restart. But it also means a restart **any time on the SAME calendar day a 0DTE
position expired** — including well after the 16:00 ET close, evening included —
still hard-halts on `missing_at_broker`, requiring a human to wait until the ET
date rolls over and restart again. Confirmed live 2026-08-24 21:02 ET: 4
discrepancies, all `missing_at_broker`, both of that day's held-to-expiry spreads;
resolved cleanly the next morning (2026-08-25 08:25 ET) with zero further
intervention needed once the date had rolled.

**Why this will keep recurring:** three of the five most recent trading days
(2026-08-24, 08-21, 08-20 — see `docs/reconciliation.txt`) held at least one
position to expiry. An evening release, on any of those days, would have hit this.

**The fix — extend `expiry_verdicts`' eligibility, don't loosen its floor:**
SPXW is PM-settled at 16:00 ET, so after settlement a 0DTE position **definitively
cannot still be held** — the "might genuinely still be open" concern the current
rule protects against doesn't apply once the market has closed and settled. The
codebase already has this exact boundary: `tools/mark.py`'s `_EARLIEST_FEED_MARK_ET
= time(16, 15)`, with the reasoning *"a pre-close quote is not a settlement price"*
(15 minutes of margin past the literal close). Use the same boundary and the same
reasoning here.

Concretely: a `missing_at_broker` row should resolve when EITHER
`expiry < today` (unchanged, the existing safe case) OR
(`expiry == today` AND the current ET wall-clock time is at or past 16:15).
`expiry_verdicts(discrepancies, today)` currently takes only a `date` — you'll need
to thread through the current ET time (or a pre-computed "is today's session
settled" boolean) from the caller, `reconcile_and_record`, which itself only
receives `today: date` right now. Trace the FULL call chain
(`app.Application._reconcile` → `reconcile_and_record`; `session._reconcile_or_abort`
→ `reconcile_and_record`) before deciding the cleanest signature change — both call
sites need the same new input, and it should come from the same clock source
(`argus.clock`) every other ET-time decision in this codebase uses, never
host-local time.

**Constraints — read `docs/decisions/2026-08-12-expired-position-resolution.md`'s
"The rule" section before writing a line of code, and preserve every one of its
existing invariants:**

- **Broker-confirmed, never clock-assumed** stays absolute — only `missing_at_broker`
  rows are eligible, exactly as today. This task does not touch that half.
- **Unreadable expiry keeps the halt** — unchanged.
- **It can only ever REMOVE rows from the discrepancy set, never add one** — unchanged.
- The NEW time-based half must be at least as conservative as the existing
  date-based half in the one dimension that matters: never resolve a row for a
  position that could genuinely still be open. 16:15 ET (not 16:00, not a
  narrower margin) is the existing, already-reasoned-about number in this
  codebase — use it, don't invent a new one, unless you find a reason it's wrong
  for THIS use (say so explicitly if you do).
- **Half-days.** `tools/mark.py`'s own docstring flags that `--from-feed` "assumes
  a normal 16:00 session... on a half-day... pass `--settlement`" — check whether
  `argus.marketcalendar` exposes a half-day / early-close calendar Argus can read
  at reconcile time. If it does, use it (16:15 ET is wrong on a day that closed at
  13:00). If it doesn't, that's a real gap this task should surface explicitly
  rather than quietly hardcode around — flag it as a residual open question in the
  decision-record update below, do not attempt to build calendar data as part of
  this task.
- Live is unaffected either way — this only ever changes when a HALT is refused
  vs. resolved automatically; a live-environment mismatch was never eligible for
  auto-resolution in the first place (the rule fires regardless of environment,
  but `mismatch_halts_process` and the surrounding halt behavior are unchanged by
  this task).

**Testing:** extend `tests/test_expired_positions.py`, don't fork a new file. The
existing suite's load-bearing negative-direction tests ("today's expiry still
halts, a future expiry still halts") need a NEW positive-direction case alongside
them: today's expiry, past 16:15 ET, resolves; today's expiry, BEFORE 16:15 ET,
still halts (the boundary itself needs a test, not just the two sides of it —
check inclusive/exclusive the same way the existing staleness-bound tests in this
codebase do, e.g. `tests/test_chain_source.py`'s inclusive-bound parametrization,
for the house convention). A fake/injectable clock, not real wall-clock time, the
same way every other ET-time-dependent test in this suite already works.

---

## Task 2: Expiry-resolved positions leave `order_legs` behind, producing a
permanent false `watchdog.unwatchable`

**Problem:** `execution/store.py`'s `_open_spreads` reconstructs "what's currently
open" purely from `orders`/`order_legs` attribution — summed opens minus closes,
per trade idea — with **no awareness of option expiry**. A 0DTE position held to
expiry has no closing fill (same root cause as Task 1), so its legs are permanently
"outstanding" by this arithmetic, forever. The 2026-08-12 design deliberately
leaves `order_legs` untouched when it resolves a `positions` row (*"orders/order_legs
are the durable record of what was traded... nothing to gain, a P&L history to
lose"*) — so once that resolution runs, `_open_spreads` disagrees with `positions`
permanently for that symbol: `attributed != held` (`_pair_spreads`'s `divergent`
check), reported as `malformed` with `"problem": "ledger holdings disagree with
the positions table"`, which trips `watchdog.unwatchable` (one Tier 1 alert/page)
and then repeats as Tier 2 `watchdog.unwatchable_still` on every refresh cycle
(`watchdog.refresh_interval_s`, 15s) **indefinitely** — confirmed firing every ~15s
for at least several minutes straight on 2026-08-25's post-restart session, for
both of the prior day's fully-settled, zero-risk positions.

**Confirmed harmless to trading, but a real observability defect:**
`PositionWatchdog.open_spread_count` (fed to the pre-trade gate) counts only
`self._scan.spreads`, never `malformed` — so this does NOT block new entries or
inflate the concurrent-position count. But it IS a standing false alarm: the
Tier 1 page reads as "UNWATCHABLE POSITION" for something that isn't a position at
all anymore, and — confirmed via `PositionWatchdog.impaired_positions()`, which is
what `/status` surfaces — **this state is invisible on `/status`**, matching a gap
CLAUDE.md's own "Current phase" section already names as deliberately unbuilt
(`scan.unattributed`/`malformed` — "pages once per episode and appears NOWHERE on
`/status`"). This task closes that specific instance of the gap, not the general
`/status`-visibility question, which stays a separate open item.

**Why this is a NEW finding and hasn't shown up before:** `last_clean_reconciliation`
reads only the SINGLE LATEST `reconcile.result` row (deliberately, per its own
docstring — fail-closed if the latest reconcile was a mismatch). An evening halt
(Task 1's scenario) makes that latest row a failure, which correctly blocks the
BROADER sandbox-reset path (`sandbox_reset_decision`) on the next restart — and
that broader reset, unlike the narrow expiry-verdicts path, clears `order_legs`
AND `positions` together (`_clear_reset_holdings`), which would have masked this
defect completely. Every prior restart apparently had either a clean reconcile
still standing (letting the reset fire and wipe both tables) or no intervening
halt. This exact combination — evening halt, then expiry-only resolution with no
reset — looks like it had never happened until 2026-08-24 evening → 2026-08-25
morning.

**The fix — make the scan expiry-aware, don't touch `order_legs` data:**
Two directions exist; read the code and pick — my read, not yet verified against
everything downstream, is that the first is architecturally cleaner:

1. **`_open_spreads`/`_pair_spreads` become expiry-aware.** When computing
   `outstanding`/`attributed` per leg, exclude (or otherwise stop counting as
   "still open") any leg whose OCC-derived expiry (`argus.occ.occ_expiry`) is
   strictly before the ledger read's own "today" — the same predicate Task 1 uses,
   reused rather than reinvented. `_open_spreads(conn)` currently takes no date
   argument; it will need one, threaded from wherever the watchdog rebuilds its
   scan. **Verify this doesn't collide with `tools.mark`/`tools.desk`'s own P&L
   reconstruction before assuming it's safe** — confirmed from the codebase's own
   documentation that those tools use `shadow.marker.read_executions`, a SEPARATE
   reconstruction path, never `_open_spreads` — but confirm this yourself by
   reading rather than trusting this prompt, since a mistake here would be a
   silent P&L-reporting regression, not a loud one.
2. **The alternative — mark/scrub `order_legs` when the expiry-verdicts path
   resolves a row** — is explicitly NOT recommended without a strong reason to
   prefer it: it works against the 2026-08-12 design's own stated rationale for
   leaving `order_legs` alone (*"a P&L history to lose"*), and risks reintroducing
   exactly the hazard that decision was written to avoid, in a narrower form. If
   you find a reason option 1 doesn't work, explain it before falling back to
   this.

**Constraints:**

- `_pair_spreads`'s core invariant — *"the sum of every spread's outstanding legs
  has to equal `positions` symbol by symbol"* — is the mechanism that catches a
  GENUINE ledger/broker disagreement (its whole reason for existing, per its own
  comment, is catching a sandbox-reset-retained order ledger describing spreads
  whose `positions` rows are actually gone for real reasons). This fix must narrow
  what counts as "outstanding" for an EXPIRED leg specifically — it must not
  weaken the divergence check for any other cause of disagreement. A leg that's
  missing from `positions` for a reason OTHER than its own past expiry (a real
  bug, a real ghost) must still be caught and still reported as `malformed`,
  exactly as today.
- Don't touch `unattributed` (`positions` rows with no order explaining them) —
  that's the opposite direction and outside this task's scope.
- `watchdog.unwatchable`'s paging/escalation logic itself (`_report_unwatchable`,
  the signature-keyed episode tracking) is almost certainly fine as-is and
  shouldn't need to change — the fix is upstream, in what the scan reports in the
  first place, not in how the watchdog reacts to what it's told. Confirm this
  reading before changing anything in `execution/watchdog.py`; if the scan-level
  fix turns out to be insufficient on its own, say so rather than reaching for a
  watchdog-side suppression as a substitute.

**Testing:** find and extend the existing test coverage for `_open_spreads` /
`_pair_spreads` (`execution/store.py`'s own test file — check for
`test_open_spreads.py`, `test_store.py`, or similar; don't fork a new one if one
exists). Add a case reproducing the EXACT reported shape: an opening order with no
closing fill, whose expiry is strictly in the past, with a matching `positions`
row already absent — must NOT appear in `malformed`, must NOT appear in
`unattributed` (it was legitimately claimed and legitimately closed by expiry, not
missing an explanation), and must simply be absent from the scan's output
entirely (same treatment as a normal fully-closed spread). Add a second case
proving the divergence check STILL fires for a leg missing from `positions` for a
reason OTHER than expiry (a genuine bug/ghost scenario) — this is the test that
proves the fix narrowed correctly rather than broadly.

---

## Shared requirements (both tasks)

- **Read `docs/decisions/2026-08-12-expired-position-resolution.md` in full
  first.** Both tasks extend it; neither should silently duplicate or contradict
  its stated reasoning. If either task's design conflicts with something that
  document asserts, resolve the conflict explicitly (update the document,
  documented below) rather than leaving two contradictory explanations in the
  repo.
- **Update the decision record, don't leave it stale.** Per CLAUDE.md's own
  convention for `docs/decisions/`, add a dated addendum section to
  `2026-08-12-expired-position-resolution.md` (matching the existing residual-fix
  pattern used elsewhere in this codebase — e.g. how A2b/A3b/A4b each got their
  own dated section referencing the original finding) covering: what was found on
  2026-08-24/08-25, why the original document's "Checked and found not to be a
  problem" section missed Task 2's mechanism, and both fixes. Add a row to
  `docs/decisions/README.md` per its own stated rule ("a resolved investigation
  gets ONE current-state sentence... don't let it drift orphaned").
- **`CLAUDE.md`'s "Current phase" section** currently references the
  `scan.unattributed`/`malformed` gap as unbuilt (search for
  "deliberately unbuilt" in that file). Update that reference once Task 2 lands —
  read the surrounding paragraph carefully and match its terse style; this is a
  one-or-two-sentence edit pointing at the new decision-record addendum, not a
  rewrite.
- **Never touch `argus.db`.** Every dev/test run uses `argus.dev.toml`
  (`ARGUS_CONFIG=argus.dev.toml`) per CLAUDE.md's non-negotiable rule. Any manual
  verification against a real reconcile/scan needs the dev config, never the bare
  command from the repo root.
- Run the FULL test suite (`./.venv/bin/python -m pytest`), not just the touched
  files. Confirm the existing AST guards (`test_no_leaks.py`,
  `test_session_trading_side.py`, `test_order_build.py`) stay untouched and green
  — neither task should be anywhere near the single-legging or `trades_for_real`
  boundaries, but confirm rather than assume.

## Deferred / explicitly out of scope

- **The general `/status` visibility of `scan.unattributed`/`malformed`** for
  cases OTHER than resolved-by-expiry (a genuinely unexplained ledger
  disagreement) — CLAUDE.md already names this as a known, deliberately-unbuilt
  gap. Task 2 makes the expiry case stop occurring at all, which is a different
  (better) outcome than surfacing it — but the general visibility question for
  OTHER causes of `malformed`/`unattributed` stays open and unaddressed here.
- **Half-day / early-close calendar data**, if Task 1 finds it's missing — flag
  it, don't build it.
- Anything in `signal/`, `execution/build.py`, `execution/loop.py`, or the
  price-walking / order-submission path — neither task should need to go near
  entry or exit execution logic at all.
- Re-tuning `watchdog.refresh_interval_s` or any other unrelated timing constant.

## Deliverable

Two independent, separately reviewable changes (own commits or a small stack —
your call, but don't mix the two tasks' code changes into one commit given they
touch different files for different reasons). Each with its own test coverage and
its own slice of the decision-record addendum. Work directly on `main`, do not commit changes. Do not push. Summarize at the end: the exact signature
change Task 1 required and why, which of Task 2's two options you took and why,
and confirmation the full test suite passes.
