# Argus architecture documentation

This directory documents the architecture of **Argus**, the fully automated 0DTE SPX
credit-spread execution system, using the [C4 model](https://c4model.com) for structure
(context → containers → components) plus UML sequence and state diagrams for behaviour.
Every diagram here was derived by reading `src/argus/` — module, class and function names
are the real ones, and arrows correspond to calls or dependencies that exist in source.
Where source could not confirm something (a broker-side transition Argus only observes, a
value recorded as unmeasured in code), it is marked inline as `> **Unverified:**` or
`> **Assumption:**` rather than drawn as fact.

Design *rationale* is not repeated here — `CLAUDE.md` holds the current state and the
non-negotiables, `docs/decisions/` holds the settled decisions, and
`docs/0dte_spx_desk_spec.md` holds the spec. These files answer "what is the shape of the
thing", and link back to the source and the decision that proves it.

## Reading order

| File | Audience | Answers |
|---|---|---|
| [01-c4-context.md](01-c4-context.md) | Traders, compliance, execs | What is Argus and what does it talk to? |
| [02-c4-containers.md](02-c4-containers.md) | Infra/DevOps, architects | What are the deployable pieces and how do they communicate? |
| [03-c4-components.md](03-c4-components.md) | Dev leads | What's the internal structure of the core application? |
| [04-uml-sequences.md](04-uml-sequences.md) | Engineers implementing/debugging flows | How does a specific operation actually execute, step by step? |
| [05-uml-state.md](05-uml-state.md) | Engineers implementing/debugging flows | What states can an order, a held position, and the system be in, and what triggers each transition? |
| [06-whiteboard.md](06-whiteboard.md) | Anyone, first | The whole system on one whiteboard: the major blocks and how they connect. |

## Status of the system these diagrams describe

**Verified against `main` at `f3f0211` (2026-09-27), the tree `v0.5.3` (Pherkad) was
built from.** Argus is **LIVE at minimum size** since the cutover on 2026-09-15 16:30 ET:
`/etc/argus/host.env` reads `ARGUS_ENV=live`, so the droplet loads `argus.live.toml`
(`environment = "live"`, config sha `8322ab49c926`), writes `argus-live.db`, and routes
orders to the dedicated production tastytrade account named in `[broker] account_number`
(`5XX00000`). Size is now **two spreads at a time, one per side**
(`max_concurrent_positions = 2`, `risk.side_balance = true`, `max_side_imbalance = 1`,
`max_loss_per_position = 475`, welded to `daily_max_loss = 1050` — since v0.5.2,
2026-09-26). Neither v0.5.2 nor v0.5.3 had run a live session as of this verification; the
first is 2026-09-28, so every behaviour below dated 2026-09-25 through 2026-09-27 is BUILT
and DEPLOYED but not yet observed live.

All three runtime modes still exist and all three are drawn here, because they share one
code path and differ only at named seams:

| `environment` | What the terminal action is | Where the seam is |
|---|---|---|
| `shadow` | `shadow.would_submit` — nothing routed, no trading side built | `pipeline.trades_for_real` |
| `sandbox` | Real orders to tastytrade cert | same seam, `dry_run_submissions = false` |
| `live` | Real orders to the production account | same seam; the credential slot and endpoint follow `environment` |

A fourth runtime posture is **monitor-only**: a process halted at `HaltGrade.ENTRIES`, or a
day whose entries were ended by `EntryExecutor._end_entries_today`, builds the whole trading
side (fills, watchdog, exits, flatten) and withholds only the entry hook. It is not a
separate mode; it is the same `submit=None` seam shadow uses, reached for a different reason.

## What changed since the previous revision (`d1666be`, 2026-09-18 → `f3f0211`, 2026-09-27)

Nine days, ~60 commits, five tagged releases (v0.4.5 → v0.5.3). Each bullet is a
behaviour change the diagrams did not carry before this revision:

- **The short strike can be selected on mid-implied delta instead of the feed's**
  (`signal.short_delta_source`, `signal/implied.py`, v0.4.6, 2026-09-21) — computed from
  each leg's own raw mid (Black-76 on the parity forward), never the greeks feed.
- **A fourth risk rail bounds the whole DAY, not just what is open**: `risk/loss_budget.py`
  computes `daily_max_loss − W` (today's realized loss plus the worst case of everything
  open) and both the sizer and the gate (`daily_loss_budget_exceeded`) refuse past it
  (v0.4.6, 2026-09-21).
- **A held-to-expiry trade is valued at settlement in the ledger, not left silent**
  (`tools/settlement.py`, v0.5.0, 2026-09-23) — the broker comparison stays fills-only.
- **A fourth trigger, `PROFIT_TARGET`, can harvest a position instead of only defending
  it** (`execution/watchdog.py::at_profit_target`, v0.5.0 built / v0.5.2 still OFF,
  2026-09-23 → 2026-09-26): fires when the net mid rounds to one tick, never crosses, and
  a take that does not fill spends neither `_attempted` nor the flatten's override.
- **A partial close's leftover stub is now closable**: `_maybe_stub_close` sends one
  further close when the ledger holds fewer contracts than a close was sized for, proven
  against the broker's own per-leg fills (`guard.py::CloseQuantity`, v0.5.1 built /
  deployed with v0.5.2, 2026-09-24 → 2026-09-26).
- **A far-OTM flatten can be DEFERRED by a calibrated insurance test**
  (`execution/flatten_deferral.py`, config `[watchdog] flatten_deferral`, v0.5.2,
  2026-09-25 → 2026-09-26): `k · P_touch(z) · W ≤ one tick` against a Pareto tail fit by
  `tools/flatten_calibration.py`. It only ever removes the flatten, never adds one, and the
  loader refuses `"on"` unless the welded size invariant holds.
- **The emergency exit is now the strike-touch stop, not the 2×-credit mark**
  (`watchdog.credit_trigger_enabled = false`, `underlying_buffer_points = 0`, v0.5.2,
  2026-09-26) — `credit_multiple` is amended, not repealed, and is the rollback value.
  `page_view` strips `credit_stop` from every page while the trigger is off.
- **One open spread per side, welded to a symmetric imbalance band**
  (`risk/side_balance.py`, `risk.side_balance` / `max_side_imbalance`, v0.5.2, 2026-09-26 →
  reviewed 2026-09-27): counted by `open_spread_equivalent`'s own per-side formula, an
  unreadable side counted as its own worst-case entry (`occ.py::occ_right`, v0.5.3,
  2026-09-27).
- **`blind` is measured against the stops that can fire, not every stop that exists**
  (`TriggerThresholds.acting_stops`, v0.5.3, 2026-09-27) — under the strike-touch stop a
  dead SPX feed alone now pages instead of reading degraded.
- **A nightly trading ledger, reconciled to the cent against the broker and pushed to a
  Google Sheet**, joined the EOD job (`tools/ledger.py`, `tools/broker_ledger.py`,
  `tools/sheets.py`, `tools/rs256.py`, v0.4.7, 2026-09-22): read-only, its own container
  step in `argus-eod.sh`, a Google **service account** key mounted only into the push, and
  an AST test pins that it can never reach the submission path.
- **Concurrency doubled and the size rail moved with it**: `max_concurrent_positions` 1 → 2,
  `max_loss_per_position` 950 → 475, `daily_max_loss` held at 1050 (was 950) — the welded
  invariant `max_concurrent_positions × max_loss_per_position ≤ daily_max_loss` now reads
  `2 × 475 = 950 ≤ 1050` (v0.5.2, 2026-09-26).

**Not yet run live as of this verification**: v0.5.2 and v0.5.3 were both deployed on a
Saturday with no session since; the first is 2026-09-28. Every item above dated
2026-09-25 through 2026-09-27 is code and config in production but behaviour still
unobserved under real fills.

The prior revision's own summary (first revision, 2026-07-31, → `d1666be`, 2026-09-18 —
control-surface-before-reconcile, kill-switch persistence, halt grades, the watchdog's
degrade-not-die, `EntriesEnded`, reconcile retry, the flatten trigger, the walk's
`NO_ORDER_EXISTS`/`GuardUnreachable`/width cap, and config-by-profile) still holds; it is
not repeated here.

## Living document

Regenerate or update these files whenever containers, components, or execution flows
change materially. **Drift between these docs and the code is a bug**, in the same way
`CLAUDE.md` drifting from what is built is a bug. In particular:

- a new module under `src/argus/` that is more than a helper → update `03-c4-components.md`
- a new HTTP route, volume, bind mount, timer or deployable unit → update `02-c4-containers.md`
- a change to the walk state machine, the exception boundaries, a halt grade, or the
  reconcile path → update `04-uml-sequences.md` / `05-uml-state.md`
- any of the above → check whether the one-page `06-whiteboard.md` still tells the truth

**Anchors are by NAME, not line number** (`watchdog.py::PositionWatchdog.check_once`). The
first revision cited line numbers; every one of them had moved within a month.

## Viewing and printing

Markdown preview shrinks a wide diagram to the pane and the text becomes unreadable, so
**every diagram here pins `useMaxWidth: false`** on its first line. It renders at natural
size and the pane scrolls instead. Keep that directive on any diagram you add — the
sequence diagrams are ~3,800px wide and are illegible without it.

| Want to… | Do this |
|---|---|
| Read on GitHub | Nothing — GitHub renders Mermaid and gives each diagram pan, zoom and fullscreen buttons. The best option for following a long arrow. |
| Read in VS Code | The *Markdown Preview Enhanced* or *Mermaid Chart* extension. Both honour the directive above, and both can export a doc to HTML or PDF. |
| Trace one diagram in detail, or print it | `./scripts/render-architecture.sh`, then open `build/architecture/index.html`. One SVG (zooms losslessly) and one PDF (page sized per diagram, `--pdfFit`) per diagram, plus the `.mmd` source. The output directory is gitignored. |
| Experiment with a diagram | Paste its `.mmd` into <https://mermaid.live>. It renders in the browser, but the diagram travels in the URL, so treat a shared link as published. |

Print the big sequence diagrams landscape on 11x17 / A3 at actual size — on Letter, a
14-participant diagram is too dense to read.

All diagrams are verified to render (mermaid-cli 11). Three Mermaid gotchas bit this doc
set and will bite the next edit: **`Loop` is a reserved word** in `sequenceDiagram`, so the
`SignalLoop` participant is aliased `SigLoop`; **`;` is a statement separator**, so it must
not appear in message or note text; and **braces inside a C4 label string** confuse the C4
parser. Check a change with:

```bash
npx -y -p @mermaid-js/mermaid-cli@11 mmdc -i docs/architecture/04-uml-sequences.md -o /tmp/out.md
```

## Open questions

Architectural ambiguities and unmeasured behaviours found while tracing the source. Each is
already acknowledged in the code, in `CLAUDE.md`, or in
[`docs/decisions/README.md`](../decisions/README.md#open-questions); they are collected here
so a reader of the diagrams knows where the drawn lines are provisional. Items resolved since
the previous revision are listed once at the bottom and then dropped.

1. **Which `OrderStatus` values other than `LIVE` mean "in the market" is unmeasured.**
   `CONFIRMED_IN_MARKET = {LIVE}` (`execution/models.py`), with an explicit note that
   widening it is a measurement, not a judgement call. `ROUTED`, `IN_FLIGHT` and `CONTINGENT`
   are drawn in `05-uml-state.md` from the enum's own naming, not from observed transitions.
2. **Whether a REJECTED order reports per-leg `remaining_quantity` is unmeasured.** If it
   does not, `classify_order`'s fail-closed branch makes a rejected `external_identifier`
   unsubmittable for 24h — safe, but noisier than needed.
3. **An order STUCK at `Cancel Requested` has never been observed.** The benign fast case
   has (two P5 residuals confirmed dead inside 1.5s); `watchdog.exit_cancel_settle_s` still
   bounds a wait nobody has seen run long. This is also the live C5 risk: the broker will not
   take a closing order, which carries a manual-close SOP rather than a code path.
4. **A broker outage during a triggered stop is eaten to settlement.** Once the exit's sends
   and the flatten's one override are spent (`_attempted`), nothing automated can act; the
   repeating `exit_spent_still_held` page is the whole response
   ([2026-09-14](../decisions/2026-09-14-the-desk-view-hides-a-spent-exit.md)). Whether a
   confirmation-gated exit RE-ATTEMPT should exist is an open decision.
5. **The daily-loss breaker is still unproven in live (G1).** Cert's day P&L is structurally
   zero; the first real read is the first day Argus actually holds a position. An unanchored
   day is now visible (`RiskSnapshot.daily_pnl_anchored`) but not re-anchored.
6. **`OrderView` is designed for a push it does not yet receive.** The walk's seam exists so
   it can be backed by the `AlertStreamer` order feed; `LiveOrderView` still polls
   `get_live_orders()` on every observation, which is what the rate governor bounds.
7. **One failed `get_live_orders()` poll blinds TWO consumers** — P4's working-order count
   (the last good count stands) and P5's residual recheck (it does not step) — so the
   residual row on `/status` goes quiet with it.
8. **Residuals and unwatchable positions have no automated resolution across a restart.**
   The residual registry is deliberately never rehydrated, and an `unattributed`/`malformed`
   position gets no flatten (`ExitExecutor.close` needs the `OpenSpread` that could not be
   built). Both are named on `/status`; neither is acted on.
9. **The provenance-gated walk/trigger values are recorded decisions, not data-backed
   tuning.** The exit-walk half was re-derived 2026-09-17 (`exit_walk_min_steps = 3`, width
   cap); the entry-walk half (`entry_*`, including `entry_drift_abort_points`) is still the
   2026-08-10 set carried as a live-minimum-size decision, owed before size is scaled.
10. **`watchdog.unmanaged_order_cancel` is `true` in cert and live since v0.4.5**
    (deployed 2026-09-20), so the P5 recheck re-sends the cancel on the traded account. The one
    case still unexercised is an order stuck at `Cancel Requested`; the flag makes the
    system keep asking while it lasts, and does not prove it.
    → [the 2026-09-19 record](../decisions/2026-09-19-live-cancel-recheck-promotion.md)
11. **The drift breaker is armed for entries only.** `ExitExecutor._build_loop` passes no
    `drift_check`, so an exit can never report `DRIFTED`. Deliberate, and undecided whether
    exits should ever arm it.
12. **Litestream is deferred past go-live by decision (2026-09-04).** The named volume, the
    nightly `argus-snapshot.sh` and the NAS pull are the whole durability story in live, not
    an interim one. The deciding argument: a lost tail cannot cause a wrong trade, because the
    broker is the only source of truth and a restored tail-gap hard-halts.
13. **The DigitalOcean host-monitoring alerts exist only in the DO control panel.** Drawn in
    `01-c4-context.md`, unverifiable from this repo.
14. **Five behaviours are BUILT and DEPLOYED (v0.5.2/v0.5.3, 2026-09-26/27) but have not run
    a single live session as of this verification** — the first is 2026-09-28: the
    strike-touch stop (`credit_trigger_enabled = false`) as the sole emergency exit; the
    one-open-spread-per-side rule and its unreadable-side count (`occ.py::occ_right`); the
    far-OTM flatten deferral (`execution/flatten_deferral.py`) — no flatten has fired under it
    yet; `blind` measured against `acting_stops` rather than every trigger; and the widened
    concurrency cap (`2 × 475 ≤ 1050`). None of these has a live-fire observation to check the
    drawn transitions against.
15. **The profit target (`TriggerReason.PROFIT_TARGET`) has never fired live.** Built
    2026-09-23, shipped OFF at v0.5.0 and v0.5.2 (a replay found no robust book-level
    benefit at two slots) — every path in `05-uml-state.md` for it (`_settle_profit_take`,
    the retry-then-retire budget, a partial take's stub) is drawn from the walk-away/cross
    machinery it reuses, not from an observed take.
16. **The quantity-aware close guard (`guard.py::CloseQuantity`, `_maybe_stub_close`) has
    never closed a live stub.** The precondition — a close whose fills leave the ledger
    holding fewer contracts than the close was sized for — has still never been observed live
    (CLAUDE.md, 2026-08-25 record); the mechanism is exercised only by the 2026-09-24 design's
    replay, not by a real partial.
17. **The mid-implied short-strike delta (`signal.short_delta_source = "mid"`) has run no
    live session yet as of `d1666be`'s successor read** — deployed 2026-09-21 (v0.4.6); the
    pre-deploy gate (`tools.replay_selection`) passed on 10,840 recorded legs, but the first
    live tick on the new source is still unread.
**Resolved since the previous revision** — kept for one revision so a reader of the old
set can find what happened: whether `FLATTEN_BEFORE_CLOSE` should be conditional on
moneyness is now BUILT as the calibrated deferral above (item 14), not merely raised
(2026-09-25); `Application._halt_process`'s docstring no longer lists the watchdog's
exception path among the `HaltGrade.ALL` defaulters (2026-09-19); the phone kill now
reaches an entry walk in flight (2026-08-03); `replace_order` carries the REMAINDER of a
partial fill (measured 2026-09-01); the `WalkOutcome` exhaustiveness guarantee is
mechanical; the kill switch survives a restart (2026-09-01); `/status` names spent,
unwatchable and residual positions (2026-08-21, 2026-09-02).

## Verification notes (this revision, `d1666be` → `f3f0211`)

**Drift where the code looks wrong rather than the doc:** none found. The modules read in
full or by every public symbol for this pass — `execution/flatten_deferral.py`,
`risk/side_balance.py`, `risk/loss_budget.py`, `signal/implied.py`, `occ.py`, the
`TriggerReason`/`ExitOutcome` enums and the `_maybe_stub_close`/`check_once`/`_check_one`
methods in `execution/watchdog.py`, `guard.py`'s `CloseQuantity` addition, and
`config.py`'s new required keys — are internally consistent with their own docstrings, the
decision records they cite, and their tests (spot-checked by name: `test_side_balance.py`,
`test_profit_target.py`, `test_strike_touch_stop.py`, `test_flatten_deferral.py`,
`test_ledger.py`, `test_sheets.py`). No contradiction between what a docstring claims and
what the code does was found in this set.

**Could not verify (beyond what is already marked inline as Unverified/Assumption):**
`execution/watchdog.py` grew to ~5,700 lines in this range (was traced by symbol and by the
specific call paths this revision's diagrams needed, not read start to end); the full diffs
of `status.py` (+442), `guard.py` (+299) and `session.py` (+184/-10) were read for their new
public symbols and wiring, not line-by-line; and the calibration/replay tooling
(`tools/flatten_calibration.py`, `tools/replay_selection.py`) was read for its public
contract and CLAUDE.md's summary of its result, not independently re-derived from the
recorded SPX path. None of the five architecture files' new content depends on an
unread portion of those files, but a second pass focused specifically on `status.py` and
`guard.py` would be the next place to look if a discrepancy turns up.
