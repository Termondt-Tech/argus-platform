# Argus: `/status` Go-Live Gaps — Build Version, Blackout Expiry, Held-but-Unmanaged Positions

## Context

`/status` observability gaps that stand between "live-fire sandbox" and real
money. Tasks 0, 1 and 2 are small and buildable directly (surface an
existing-but-invisible fact; same code paths — `/status` JSON+HTML assembly,
startup log block). **Task 3 is larger and DESIGN FIRST** — it closes two open
decisions in `docs/decisions/README.md` and needs Richard's sign-off on the shape
before any code. Treat all four as independent — don't let one influence
another's design, with **one exception stated in Task 0: it must land before
Task 3 is designed**, because Task 3's design explicitly follows the seam Task 0
repairs.

Before writing any code, read the actual `/status` assembly function
([`src/argus/status.py`](../../src/argus/status.py) — `StatusService.assemble`,
`StatusSnapshot`, `SessionStatus`), the existing staleness/heartbeat warning
pattern, the startup logging block, the watchdog's existing live-read accessors
(`impaired_positions()`, `spent_exit_attempts()` — the precedent Task 3 follows),
and `docs/argus-deployment-runbook.md`'s current build step. Match their existing
conventions (naming, log format, JSON key style, HTML styling, the
DERIVED-never-stored / None-vs-`()` three-state contract the watchdog rows already
use) rather than inventing new ones. If anything below doesn't match what you find
in the repo — a function that doesn't exist as described, a convention that
differs, a premise that's stale — report it explicitly rather than silently
working around it or silently adopting my assumption.

**Coordination note.** Two other pending prompts also change `/status` rendering —
[`argus_kill_switch_persistence_prompt.md`](argus_kill_switch_persistence_prompt.md)
(a rehydrated kill must show `engaged: true` after a restart, closing the
`trip = db.kill_trip if ks.engaged else None` misreport) and
[`argus_end_day_keeps_watchdog_prompt.md`](argus_end_day_keeps_watchdog_prompt.md)
(a transient monitor-only day). Neither is in scope here, but whichever lands
first should keep the monitor-only / HALTED / degraded-position rendering
coherent — flag any collision rather than forking the layout.

---

## Task 0: the spent-exit row on `/status` has never shown its mark

**Found 2026-09-02** while shipping the credit-stop fixes
([record](../decisions/2026-09-02-the-credit-stop-basis-and-its-comparison.md)),
out of scope there and raised here because this is the `/status` prompt.

**Do this one FIRST**, before Task 3 is designed. Task 3's design question 2 names
`spent_exit_attempts()` as *the precedent* its two new live-read seams should
follow — and that precedent is currently the one seam in the file that silently
drops fields. Fixing it first means Task 3 copies a working example and inherits
the regression test rather than the bug.

**Problem.** `/status` renders **`mark: unpriced` for every spent exit, always.**
`_fmt_spent_mark` (`src/argus/status.py`, ~line 1495) takes its
`p.cost_to_close is None` branch on every render, because the session seam that
builds the row never copies the mark across.

`src/argus/session.py` (~line 1375, the `spent_exits` closure) constructs
`UnclosablePosition` from `wd.spent_exit_attempts()` with five fields —
`external_identifier`, `candidate_id`, `contracts`, `outcome`, `spent_for_s`. The
source `SpentExitAttempt` carries **eight**: it also has `cost_to_close`,
`credit_stop` and `still_triggered`, and `UnclosablePosition`
(`status.py:224-263`) declares all three with `None` defaults, so the omission is
silent at every layer.

**Introduced by omission, not by a change.** The seam was written 2026-08-21
(commit `f0b5e81`, A2b) with the five fields that existed then. The three mark
fields were added to `SpentExitAttempt` on 2026-08-28 by the exit-taxonomy pass —
the change whose entire point was that a held, triggered spread went 76 minutes
with no surface able to say what it was worth. The pages got the mark; `/status`
did not.

**Why the suite is green.** `tests/test_status.py` (~line 378) constructs
`UnclosablePosition` directly with the fields populated and asserts on the
rendering. Nothing exercises the session seam, so the drop is invisible to every
test that exists. **The test is the deliverable here, not the three lines** —
assert through the seam (watchdog → `spent_exits()` → `StatusSnapshot` → rendered
row), never on a hand-built `UnclosablePosition`, since building one by hand is
exactly what missed it.

**What to build:**
- Copy `cost_to_close`, `credit_stop` and `still_triggered` through the
  `spent_exits` closure in `session.py`.
- A test through the seam, per above.
- Check the sibling seams in that same block for the same class of drop. **Already
  checked as of 2026-09-02: `evaluability` copies all five of `ImpairedPosition`'s
  fields and is complete, and `entries_ended` builds a different type — so
  `spent_exits` is the only one wrong today.** Re-check rather than trusting this
  sentence if either source type has gained a field since.
- Consider whether the shape is worth pinning structurally the way the
  `credit_payload()` / `reject_reason` sweeps are (a test asserting each seam's
  closure names every field of its source type). A silent under-copy between two
  dataclasses with `None` defaults is a repeatable defect, and Task 3 adds two
  more seams of exactly this shape. **Use judgment — if it is more machinery than
  it is worth, say so rather than omitting it silently.**

**Constraints:**
- `session.py` and a test only. Nothing in `execution/` changes; `SpentExitAttempt`
  and `UnclosablePosition` already carry the fields and the renderer already
  handles all three of its states.
- The three-state rendering is already correct and must stay: `None` means "that
  tick could not price it" and renders as `unpriced` — never as a placeholder
  number, on a row whose whole purpose is telling a human what to do by hand.
  This task makes the *other two* states reachable; it does not change what `None`
  means or how it reads.

---

## Task 1: Blackout-list expiration warning

**Backlog source:** Todoist task `6hCX9wc8Jfv3HCJ8` ("Argus: surface blackout-list expiration in /status or a startup warning").

**Problem:** `blackout_dates.txt` / `fomc_dates.txt` run out 2026-12-23. No 2027 FOMC/CPI/NFP/PCE dates are seeded yet (source hasn't published them). From inside the process, an exhausted blackout list is indistinguishable from a genuinely quiet day — no internal signal fires when the list runs dry. Decision on 2026-08-04 was to rely on a manual calendar reminder for now; this task is the actual fix.

**What to build:**
- Compute the blackout list's max date (across whichever files/sources feed the event blackout — check how `EventConfig`/the blackout wiring actually loads dates, don't assume a single file).
- When that max date is within N days of "now," surface a warning:
  - A `/status` field (JSON + HTML, same assembly function as everything else — see Task 1's overlap with Task 2's requirement 4 below).
  - AND/OR a startup log warning — match whatever the existing staleness/heartbeat observability convention actually does (if that convention is "log at startup only," do that; if it's "field + log," do both). Look at how existing staleness/heartbeat warnings are implemented and mirror that pattern exactly rather than picking a new one.
- Pick N by checking existing threshold conventions in the repo (staleness/heartbeat timeouts, etc.) for a consistent order of magnitude. Absent a clear precedent, 30 days is a reasonable default — but confirm against what's actually there and note your choice.
- No warning at all when the list has healthy runway — this should be silent in the common case, not a permanent banner.

**Constraints:**
- Read-only against the blackout data — this doesn't seed 2027 dates, doesn't change blackout logic, doesn't touch `EventConfig` wiring itself.
- Don't build calendar-fetching or auto-refresh — the deferred fix here is detection/visibility, not auto-repopulation.

---

## Task 2: Surface running build version in `/status`

**Goal:** Surface the running build's git version in `/status` (and at boot in the logs), computed at Docker build time via `git describe --tags --always --dirty`, so "what's actually deployed" is answerable at a glance instead of git archaeology — directly addressing the branch-mismatch class of problem hit during the chunk 1-4 cutover (main didn't have the chunks; caught by luck of a pre-flight check, not by anything visible at runtime).

**Requirements:**

1. **Build-time, not runtime.** The container shouldn't need `.git` present. Accept a build ARG (`ARGUS_VERSION`) and bake it into the image — write a small `src/argus/_version.py` module (or equivalent) rather than relying on an env var set at `docker compose up` time, since that's one more thing to forget to set correctly on any given run.
2. **Automate the build step.** Update the actual build invocation so the version is computed automatically, not manually typed each time — e.g. `docker compose build --build-arg ARGUS_VERSION=$(git describe --tags --always --dirty)`. Update `docs/argus-deployment-runbook.md`'s build step to reflect this exact command, not just the plain `docker compose build` documented today.
3. **`argus.service` — confirmed no build coupling, no unit changes needed.** `ExecStart=/usr/bin/docker compose -f /opt/argus/docker-compose.yml up` / `ExecStop=... down` — no `build`, no `--build` flag. The unit only starts/stops whatever image is already built; it never builds it. So: don't touch `argus.service`, don't copy it anywhere, no `daemon-reload`. Instead, add an explicit line to `docs/argus-deployment-runbook.md` stating that the build step (with the new build-arg command) must run *before* any `systemctl restart argus` / service (re)start — a restart with no fresh build just reattaches to whichever image was last built, silently, with no error. That's a stale-but-not-crashing version label — exactly the "what's running doesn't match what I expect" gap this feature exists to close, so the runbook should make the ordering explicit rather than assumed.
4. **Graceful fallback.** If the image was built without the build arg (e.g. an ad hoc local dev build), show something explicit like `"unknown"` — never crash, never silently show a blank or misleading value.
5. **`/status` JSON and HTML.** Add a `version` field next to environment/uptime in both renderings. They already share one assembly function — keep that intact, don't special-case one output.
6. **Startup log.** Log the version once at process startup, alongside existing startup logging, so it's visible in `journalctl` without needing to hit `/status`.
7. **Testing.** Don't require an actual git-tagged checkout to test this — inject/monkeypatch the version value in tests the same way other config is tested. Add a test for the fallback (`"unknown"` when unset) and a test that `/status` actually surfaces whatever value is present.
8. **Nice-to-have, not required.** If the version string contains `-dirty` (uncommitted changes at build time), consider visually flagging it in the HTML view — that's precisely the "what's running doesn't match what I expect" signal this feature exists to make visible. Use your judgment on whether it's worth the extra code; skip if it adds meaningful complexity for a monitoring nicety. If you skip it, say so — don't silently omit it without a note.

---

## Task 3: `/status` shows held-but-unmanaged positions (DESIGN FIRST)

**Backlog source:** two open decisions in
[`docs/decisions/README.md`](../decisions/README.md) — "Should `/status` name an
UNWATCHABLE position?" and "Should `/status` name a residual order?" — plus the
recurring pattern CLAUDE.md names three times: `daily_loss.breach` rendered
TRIPPED off a stale row (fixed), a spent exit attempt rendering as an ordinary
open position (fixed, `spent_exit_attempts`), and now these two.

**Problem.** There are held positions and abandoned orders that Argus is **not
managing** — no virtual stop evaluated, or an order nobody is walking — and
`/status` renders calm for every one of them. Each pages Tier 1 once per episode
and then appears **nowhere on the page**:

1. **UNWATCHABLE positions.** `OpenSpreadScan.unattributed` (a `positions` row no
   order explains) and `.malformed` (an order whose legs are in `positions` but
   which cannot be reassembled into a spread) — see
   `PositionWatchdog._report_evaluability` and the "UNWATCHABLE POSITION" log line
   in [`src/argus/execution/watchdog.py`](../../src/argus/execution/watchdog.py)
   (~line 2705–2744). A held position with no stop being evaluated is the worst
   state the system can put a position in, and it is the one state
   `spent_exit_attempts` (A2b) deliberately did **not** widen into. If the
   position was impaired first, it also vanishes out of "Exit triggers" on the
   refresh that drops it from the scan.
2. **Residual orders.** P5 (2026-08-30,
   [`docs/decisions/2026-08-29-mid-session-cancel-recheck.md`](../decisions/2026-08-29-mid-session-cancel-recheck.md))
   built `ResidualOrderRecheck` with an `outstanding()` accessor
   ([`src/argus/execution/residual_orders.py`](../../src/argus/execution/residual_orders.py),
   ~line 416) but deliberately left the rendering out of that pass. One live row —
   identifier, broker order id, how long it has been working, attempts made, last
   broker status — read off the registry, **never off an audit row**. This is
   *more* useful, not less, while `watchdog.unmanaged_order_cancel = false`
   (`would_cancel` mode): the row and `order.residual_would_cancel` telemetry are
   the only surfaces showing what the recheck would have done.

**What to build** — a single unified section, not two one-off rows. The open
decision is explicit that this "wants one decision about what the page owes for
held-but-unmanaged positions generally." Design it as `degraded_positions` (or a
name that fits the existing field vocabulary) covering both categories, and leave
room for the two pending prompts' states (rehydrated-kill monitor-only, transient
`_end_day` monitor-only) to slot in rather than each adding a row later.

**Design questions to resolve (get sign-off before coding):**

1. One combined section vs. distinct rows per category. What does an operator on a
   phone at 15:00 on expiry day actually need to see first?
2. The live-read seam. `impaired_positions()` / `spent_exit_attempts()` are the
   precedent: DERIVED, never stored, built BY the session from a
   `PositionWatchdog` accessor, defined in `status.py` (not imported from
   `execution/`) to keep the one-way dependency. **Read Task 0 before following
   that precedent** — the `spent_exit_attempts` seam silently drops three of its
   source's eight fields, and this section adds two more seams of the same shape.
   Does the watchdog need a new
   `unwatchable_positions()` accessor (there is none today — `unattributed` /
   `malformed` are only in the `_report_evaluability` payload), and does the
   residual row come straight off `ResidualOrderRecheck.outstanding()` threaded
   through `SessionStatus` the same way?
3. The None-vs-`()` three-state contract. None = "no watchdog publishing"
   (shadow / no session); `()` = "watchdog running, nothing degraded". Same as
   the existing rows.
4. Severity / ordering relative to the existing "Exit triggers" and
   "Exit attempts" rows — an UNWATCHABLE position is strictly worse than an
   impaired-but-not-blind one.
5. What each row shows for a residual: is `would_cancel` vs `cancel` state
   surfaced (so the operator can tell whether P5 is in observer mode)?
6. HTML truncation. Audit payloads truncate at 240 chars in the tail; this section
   must read fully on a phone the way `impaired_positions` was built to
   (live read, not a payload string).
7. Does this section change how `SessionPhase` / the HALTED row read, or sit
   below them unchanged?

**Constraints:**
- Read-only. This surfaces state the watchdog and the residual registry already
  hold; it does not change scan logic, the recheck, or any halt behaviour.
- No new audit category needed for the render itself — but if the design wants one
  (e.g. a Tier 2 "degraded position still unresolved" restatement), justify it
  against the existing `_report_evaluability` / `order.residual_*` records rather
  than duplicating them.
- The watchdog accessor must not be able to take down the scan or the page — same
  discipline as `read_rss_bytes` returning None on any failure.

---

## Constraints (all tasks)

- No new runtime dependencies.
- Don't bundle `.git` or git itself into the runtime image — version is a build-time input, not something computed inside the running container.
- Existing `/status` and `/kill` behavior/tests unaffected — purely additive.

## Deferred (out of scope for this prompt)

- Order execution engine, position watchdog, macro-event blackout days themselves — unchanged.
- Seeding 2027 FOMC/CPI/NFP/PCE dates — separate task, blocked on source publication.
- Any broader release/CI automation beyond this one build-arg change — this is about visibility, not building a release pipeline.
- Auto-refresh or auto-fetch of blackout calendar data.
- **Task 3:** changing the watchdog scan, the residual recheck, or any halt/flatten behaviour — Task 3 only *renders* state those already hold. The kill-switch-persistence and `_end_day` monitor-only states are their own prompts; Task 3 should leave a slot for them, not build them.
- **Task 0:** anything in `execution/`. `SpentExitAttempt` already carries the mark and `_fmt_spent_mark` already renders all three of its states correctly — the defect is entirely in the session seam between them. In particular this is not the place to change what a spent exit *is*, when it clears, or the one-attempt rule.

## Definition of Done

### Tasks 0, 1 & 2 (build directly)

- The spent-exit row on `/status` shows its mark — `cost_to_close`, `credit_stop`
  and all three `still_triggered` states — instead of always reading
  `mark: unpriced`.
- That is covered by a test that goes THROUGH the session seam, not one that
  hand-builds an `UnclosablePosition`; and the other live-read seams in the same
  block are confirmed complete (or fixed).
- `/status` (JSON and HTML) shows the actual git-described version of the running build.
- A build without the version arg shows `"unknown"`, not a crash or blank field.
- `journalctl` shows the version at startup.
- `docs/argus-deployment-runbook.md`'s build step updated to compute and pass the version automatically.
- Runbook includes an explicit build-before-restart ordering note (`argus.service` itself confirmed unaffected — no build coupling, no changes needed).
- `/status` (and/or startup log, per whatever the existing staleness/heartbeat convention does) surfaces a warning when the blackout list's max date is within N days.
- No warning shown when the blackout list has healthy runway.
- All existing tests still pass; new tests cover both features per the testing notes above.
- CLAUDE.md updated to reflect both changes, per this repo's usual convention.
- Any premise mismatch, scope question, or judgment call (N's value, whether the `-dirty` HTML flag was built) reported back explicitly rather than resolved silently.

### Task 3 (design first, then build once signed off)

- A written design for the `degraded_positions` section answering every design
  question above, delivered for Richard's sign-off before code.
- Once signed off: `/status` (JSON and HTML) names every held-but-unmanaged
  position — UNWATCHABLE (`unattributed` / `malformed`) and residual orders —
  as a live read off the watchdog / residual registry, never off an audit row.
- The three-state None-vs-`()` contract matches the existing watchdog rows.
- A residual row shows whether P5 is in `would_cancel` or `cancel` mode.
- The section reads fully on a phone (no 240-char truncation).
- The watchdog accessor(s) cannot take down the scan or the page.
- New tests: an UNWATCHABLE position appears on `/status`; a residual order
  appears; both clear when the underlying state does; shadow / no-session renders
  None not `()`.
- The two open decisions in `docs/decisions/README.md` are marked resolved (or a
  decision record added), and CLAUDE.md's A2b / P5 follow-up notes updated.
