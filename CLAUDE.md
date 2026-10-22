# Argus

Fully automated 0DTE SPX credit spread trading system, run to make money for its
investors (see Objective). Separate app, separate
repo, separate deploy from TOMIC — no shared runtime, no shared credentials.

| Read this | For |
|---|---|
| `docs/0dte_spx_desk_spec.md` | The full design spec. **Read before any architecture work.** Rollout-plan sections (§6, §8, §10) predate the live cutover and are now historical prose describing a phase already reached — read them as the ORIGINAL plan, not current state; this file and `docs/live-cutover-runbook.md` are current state. |
| `docs/decisions/` | Settled decisions and closed investigations. **Read the relevant file before changing the component it covers** — they are normative, not history. |
| `docs/designs/` | Proposals awaiting or having received sign-off — not normative until a decision record cites them. |
| `docs/architecture/` | C4 + UML: the shape of the system, derived from source. |
| `docs/research/` | External research and Argus's own measurements informing signal/strategy design. **Not a decision log** — nothing there is settled until a decision record in `docs/decisions/` cites it. |
| `README.md` | Day-to-day operation, the daily rhythm, droplet deploy/rollback, the shadow-marking SOP. |
| `docs/argus-deployment-runbook.md` | One-time provisioning, the chunks 1–4 cutover (§16, historical), and the full packaging/deploy reference (§17). |
| `docs/live-cutover-runbook.md` | **The sandbox → live cutover. The only live cutover document** — read it before touching `host.env`, the profiles, or anything the flip depends on. |
| `docs/go-live-findings.md` | What gated the flip (both gates closed), what closed it, and the still-open backlog. |

## Objective

**Argus exists to make money for its investors: consistent, long-term profit from
selling 0DTE SPX premium, net of fees and slippage.** Everything else in this file
(the non-negotiables, the rails, the audit log) is a **constraint** on how that is
pursued, never the objective. Correct, safe execution is necessary and not sufficient.

- **The measure** is net P&L per trading day (the day is the unit of evidence), judged
  against its risk: day SD, the worst day, and drawdown against the loss budget. How
  much mean is worth how much tail is Richard's policy call; state it, never assume it.
- **The standing question for any change:** what does it do to net expected P&L, and to
  that risk? A safe change with no answer has not been evaluated. A profitable idea that
  breaks a rail is not available.
- **On strategy questions, start from the owner's purpose, not a record's framing.** Test
  ideas against the recorded data (read-only), find the mechanism, and propose what would
  make an idea pay, not only why it fails. The evidence standards still hold: day-level
  statistics, pre-registration for anything found after the fact, and every variant
  reported, not just the best one.

## Current phase

**THE SPX BOOK IS KILLED AND STOPPED.**
- The kill switch was engaged on 2026-10-01.
- The service was stopped and disabled on 2026-10-05.
- v0.5.5 was never deployed.

Before that, it was PHASE 2, live at minimum size, from the cutover on 2026-09-15 at 16:30 ET.
`/etc/argus/host.env` reads
`ARGUS_ENV=live`, so the droplet loads `argus.live.toml` (`environment = "live"`),
writes `argus-live.db`, and routes orders to production tastytrade account
`5XX00000`. **Read `host.env` and the loaded profile's `environment` key as the
authority — this section is prose and can lag them**; `/status` carries both as
`environment` and the `(profile, filename, sha12)` fingerprint.

**The 2020–2026 history found no edge in the book (2026-09-30).** Replayed on 1,362 sessions
of ThetaData SPXW quotes, today's book makes −$2.29 a session at the mid on the history set
and −$13.14 on the held-out period, and is below zero after costs in both. That supports
**no size increase**, and every variant tested is closed on that data. Whether to keep
opening trades at minimum size is Richard's open decision.
→ [results](RESULTS.md)

**PREPARED, NOT YET TAGGED OR DEPLOYED: v0.5.5 (name proposed: Alnilam).** A coupled
image + config release, no new required key, no migration. It changes trading in one way:
- **The flatten deferral recalibrated** (`z99` 2.38 → 2.60, `alpha` 2.80 → 2.72; k stays 2):
  the boundary on a 5-wide moves z ≥ 3.05 → z ≥ 3.35, so a 15:30 flatten is deferred less often.

It records one thing (P9: Tier 2 `signal.ladder`, the strike ladder once a minute, never read
by `src/`) and changes `tools.flatten_calibration` to exclude the 8:30 release days by rule
(tool only, not runtime). Expected live config sha `b6fd657063a1`; suite 3,121. **Two
sign-offs are Richard's and open:** the calibration-exclusion decision, and the k decision's
"a rerun ships in either direction" adoption rule.
→ [release notes](docs/releases/v0.5.5.md),
[k decision + amendment](docs/decisions/2026-09-28-flatten-deferral-k-2.md),
[ladder](docs/decisions/2026-09-29-recording-the-strike-ladder.md)

**Deployed: v0.5.4 (Mirzam), 2026-09-28** (read back `version: v0.5.4`, sha `d6639d0d7a1a`).
Coupled image + config + data: k 10 → 2, P11 (the deferral decision on `watchdog.triggered`),
**the 8:30 release days (CPI, NFP, PCE) traded**, first 2026-09-30 (PCE), pre-registered abort
at −$600 cumulative, `signal.max_width` 10 → 5. → [notes](docs/releases/v0.5.4.md),
[k](docs/decisions/2026-09-28-flatten-deferral-k-2.md),
[release days](docs/decisions/2026-09-28-trading-the-8-30-release-days.md)

**Before it: v0.5.3 (Pherkad), 2026-09-27**, image-only: `blind` measured against the
stops that can fire, an unreadable side counted worst-case in the side rule, per-side
counts AST-pinned to trade ideas. → [notes](docs/releases/v0.5.3.md),
[review](docs/reviews/2026-09-27-v0.5.2-mira.md)

**What v0.5.2 (Mira, 2026-09-26) changed and is still true today** — a coupled
image + config release, eight new required `[risk]`/`[watchdog]` keys, prompted by
a −$515 week that came entirely from eight 2×-credit stops:

- **The strike-touch stop.** `watchdog.credit_trigger_enabled = false`: the
  2×-credit mark trigger no longer fires; the emergency exit is spot reaching the
  short strike (`underlying_buffer_points = 0.0`). `credit_multiple = 3.0` is now
  inert — it is the rollback value, not the live multiple.
  → [decision](docs/decisions/2026-09-26-strike-touch-stop.md)
- **One open spread per side.** `risk.side_balance = true`,
  `max_side_imbalance = 1` — chosen in the orchestrator, refused by the gate
  (`side_imbalance_exceeded`); `false` is the pre-v0.5.2 entry path exactly.
  → [decision](docs/decisions/2026-09-26-one-position-per-side.md)
- **Tighter, welded caps.** `max_concurrent_positions = 2` at `max_loss_per_position
  = 475` (was `1 × 950`) against `daily_max_loss = 1050` (was `950`) — the second
  slot has one ordinary stop of slack. See the landmine below; move one, re-check
  the other two.
- **A calibrated far-OTM flatten deferral.** `watchdog.flatten_deferral` defers
  `FLATTEN_BEFORE_CLOSE` only while `k · P_touch(z) · W ≤ one tick` (Pareto tail,
  `tools.flatten_calibration`); it only ever removes the flatten, never for a
  spent/failed-send/blind/degraded position, and a restart after 15:00 disables it
  for the rest of the day. Its first live flatten (2026-09-28, z = 4.42) was not deferred at k = 10; v0.5.4 lowered k to 2.
  → [decision](docs/decisions/2026-09-25-deferring-a-far-otm-flatten.md)

**Built, deployed, and currently switched OFF: the profit target**
(`watchdog.profit_target_enabled = false` in all three profiles, since v0.5.2,
package B). A fourth trigger closes a held spread when `cost_to_close` rounds to
one tick; it never crosses the market, and an unfilled take may never touch
`_attempted`, `_sends` or `_flatten_attempted` (AST-pinned) — it can never spend
the position's one emergency exit or the flatten's one-shot override. Stays off: at two slots a take frees a slot that gets refilled, and the book-level replay
found no robust benefit. → [decision](docs/decisions/2026-09-22-profit-target-and-re-entry.md)

**Built and enabled: the stub-close continuation** (`watchdog.stub_close_enabled =
true`, v0.5.1/v0.5.2). A partial close's stub — the position a fill leaves behind
when `DuplicateOrderGuard` blocks every later close under the shared key — now gets
one further close once the broker's own per-leg fills prove it fits, discharging
the profit target's promotion gate.
→ [decision](docs/decisions/2026-09-23-closing-the-stub-a-partial-close-leaves.md)

**Built and live since v0.4.6: the short strike is selected on mid-implied delta**
(`signal.short_delta_source = "mid"`, required, `"feed"` is the config-only
rollback) and **the day's loss budget bounds entries against `daily_max_loss`
minus today's realized loss and open worst case** — both covered under
Non-negotiables below.

**Built and live since v0.4.7: a nightly trading ledger**, reconciled to the
broker's own `get_history` per order id to the cent, pushed to a Google Sheet as
the last step of `argus-eod.sh`. Read-only, its own container, never gates trading
— an AST test pins that it cannot reach the submission path.
→ [decision](docs/decisions/2026-09-22-ledger-and-sheets.md)

**Still owed: the entry/exit price-walk parameters are the unchanged 2026-08-10
sandbox-entry set**, carried into live deliberately (none is denominated in
account size) but never re-derived from live fill data. The data-backed pass is
gated on the first size increase past live minimum; `entry_drift_abort_points` is
the one parameter whose data is already admissible and the computation is not yet
run. → `execution.walk_source` / `watchdog.trigger_source` in `argus.live.toml`

**What the cutover proved, and what is still unproven.** The first live boot's
`startup_reconcile` closed both go-live gates at once: the live OAuth app
registration authenticated against the production API, and the account
reconciled clean with no position or order discrepancy. **G1 — the daily
max-loss breaker — is live and still not meaningfully pressure-tested.** It has
four parts on different days (the NLV anchor, the live measurement, same-day
rehydration, and the trip); the trip is the part that stays unproven, since at
`2 × 475 ≤ 1050` a day's loss only reaches the cap if a position goes near its
full width — a quiet week proves nothing about it.
→ [live cutover runbook](docs/live-cutover-runbook.md), [go-live findings](docs/go-live-findings.md)

**Rollback is one line of `host.env` back to `sandbox` plus a re-deploy — but
NEVER while a live position is open**, which under `sandbox` would leave that
spread with no watchdog, no virtual stop and no 15:30 flatten. Mid-session the
control is **`POST /kill`**, which stops entries and keeps the watchdog.
→ [runbook §7](docs/live-cutover-runbook.md)

All five execution chunks are built and wired (`docs/decisions/`, chunks 1–5).
The full release-by-release history — every tagged version back to v0.3.1, what
each one fixed, what config it moved — is in
[`docs/releases/README.md`](docs/releases/README.md); the design rationale behind
any of it is in [`docs/decisions/README.md`](docs/decisions/README.md), which is
normative. Six things remain **recorded as unmeasured in code rather than
guessed at**, and several **open decisions** are tracked with their consequences
in [`docs/decisions/README.md#open-questions`](docs/decisions/README.md#open-questions).

## NEVER TOUCH THE LIVE DATABASE

**There are now TWO production databases, and since 2026-09-15 the one that matters
most is the NEW one.** `argus-live.db` is the LIVE audit log — real orders, real
fills, real money, started from empty at the cutover and never reset (the sandbox
24 h wipe keys on `environment == "sandbox"`, so nothing clears it and every row in
its `positions` table is a fact about a real account). `argus.db` remains production
data too — the finished cert hash chain and the accumulating shadow/sandbox signal
record, intact beside it on the same volume. **No development, testing, debugging,
benchmarking, or analysis may write to EITHER. Ever.**

- Every dev/tool/experimental run uses `argus.dev.toml`
  (`ARGUS_ENV=dev`, or `--config argus.dev.toml` for `tools.*`), which
  writes to the gitignored `dev/` directory. `argus.sandbox.toml` is reserved for
  real cert data-collection sessions; `argus.live.toml` is the traded profile and
  is never a target for anything but the droplet's own process.
- Reads for analysis must be read-only:
  `sqlite3.connect("file:argus-live.db?mode=ro", uri=True)` (and the same for
  `argus.db`).
- **Never run `python -m argus` or any `argus.tools.*` from the repo root without
  a config selector** — `db_path` is CWD-relative, so the bare command used to
  target the live DB. **Since 2026-09-04 it cannot: there is no default config
  path.** `python -m argus` needs `ARGUS_ENV` (the profile) or `ARGUS_CONFIG`, and
  every `argus.tools.*` entry point REQUIRES `--config`. Never re-add a default to
  either — the default WAS the mechanism below.
  → [config profiles](docs/decisions/2026-09-04-config-profiles.md)

**Why this is a rule and not a preference.** On 2026-07-21 a bare `python -m argus`
from the repo root opened the live DB alongside a running shadow session, took `seq`
4128–4130, and wedged that session's audit writer for ~80 minutes — ~2,000 records of
Phase 0 signal data lost (never written; nothing corrupted). Two enforcement layers
now exist (`db/lock.py`, `argus.dev.toml`), but the rule stands on its own.

## Non-negotiables

These hold regardless of what else changes as the codebase grows.

- **Kill switch** reachable in one action, from a phone. Halts new entries
  immediately regardless of what any other task is doing, **including an entry
  order already working at the exchange**. Backed by the always-on `POST /kill`
  surface (`control.py`, spec §6); the bind stays Tailscale-only and
  bearer-authenticated, and the token never lives in config/code and never lands
  in a log or audit record. **"Always-on" includes while halted**: the surface
  binds BEFORE the reconciliation rail, and an unrecoverable halt PARKS the
  process instead of exiting — `_stop` = stop trading, `_exit` = leave the
  process, and only SIGINT/SIGTERM sets it. **There is deliberately NO third
  event** — a halt that must reach less narrows the HALT (`HaltGrade`), never the
  event. **Never reorder the bind after a rail that can refuse, and never make a
  halt exit.** **The "already working at the exchange" clause is reached through
  `_request_cancel`, which is best-effort**, so an abandoned entry order is
  REGISTERED and rechecked until the broker confirms it terminal (opening side
  only — cancelling a close strands a position; never rehydrates across a
  restart). **It SENDS in live since v0.4.5** — measured press-to-cancel is
  ~4–5s, and a fill inside that window is not a failure. What it cannot reach — a
  restart after a kill, an unreachable `/kill`, an order whose id was never
  learned — is a kill-side SOP in `README.md`.
  **The halt is STATE OF THE WORLD, not state of a process**: `KillSwitch` is
  built from the audit log — engaged iff the latest `kill_switch.trip` has no
  `kill_switch.resume` at a higher `seq` (never `ts`, no date bound). It never
  expires, and **only `POST /resume?confirm=yes` clears it** — human,
  authenticated, audited, and the sole caller of `KillSwitch.disengage` anywhere.
  `disengage` **records BEFORE it flips** — the ordering follows the SAFETY
  DIRECTION, not the method names, so a failed clear is a no-op. Rehydration
  restores the SWITCH and never `HaltRecord`; an unreadable state fails closed to
  ENGAGED.
  → [scope](docs/decisions/2026-08-03-kill-switch-scope.md),
  [persistence](docs/decisions/2026-09-01-kill-switch-persistence.md),
  [halted-but-alive](docs/decisions/2026-08-17-halted-but-alive.md),
  [halt scope split](docs/decisions/2026-08-29-halt-scope-split.md),
  [cancel recheck](docs/decisions/2026-08-29-mid-session-cancel-recheck.md)
- **One writer per audit database, enforced.** `startup()` takes an exclusive
  `flock` (`db/lock.py`) before opening the DB and refuses to start if another
  process holds it — the chain assigns `seq` from in-memory state, so two writers
  silently collide.
- **A failed audit write halts trading.** A write failure latches
  `AuditLog.failed`, engages the kill switch, and raises `AuditWriteError`. **No
  auto-retry** — with the writer lock in place a `seq` collision means an
  invariant is genuinely broken. The halt is UNIFORM across paths
  (`AuditLog.on_failure` both engages the switch AND requests process stop),
  because a write can fail *outside* a session, where no loop exists to
  propagate the error.
- **Daily max-loss circuit breaker** halts new entries automatically on
  threshold breach — no human step required — and **the latch is per TRADING
  DATE, not per process.** `_run_session_day` rehydrates today's trip AND
  today's original P&L anchor from the audit log (`risk/rehydrate.py`) before
  building the day's state: a same-day restart stays tripped, a new day starts
  clean, and an unreadable state halts. **Never construct
  `DailyLossBreaker`/`DailyPnl` bare there, and never let
  `anchor_day_pnl_if_possible` re-anchor a day that already has an anchor** —
  either one silently hands back a spent loss budget. **`/status` reports a live
  rail by reading the RAIL, never a `daily_loss.breach` row** — a row outlives
  the process that wrote it. An unanchored day P&L is a placeholder `0.0`, and
  the records say so (`RiskSnapshot.daily_pnl_anchored`) — recorded, never
  decided on. → [restart rehydration](docs/decisions/2026-08-20-daily-loss-breaker-restart-rehydration.md)
- **Every size rail is enforced twice, and the tightest one governs.** Three
  rails (`position_size_pct` relative; `max_loss_per_position` and
  `max_contracts_per_position` absolute), applied in
  `orchestrator.size_contracts` when a size is chosen AND re-checked in
  `PreTradeGate` against the resulting proposal — the sizer chooses, the gate
  refuses. **The absolute rails are never gated on a readable NLV**: an
  unreadable account snapshot must not excuse an oversized position.
  → [chunk 5](docs/decisions/execution-chunk-5-wiring.md)
  **A fourth bound, on the DAY, live since v0.4.6: the loss budget.** Both the
  sizer and the gate (`daily_loss_budget_exceeded`) hold `daily_max_loss − W`, W
  = today's realized loss + the worst case (full width) of everything open, the
  worse of a ledger estimate and — live only — a broker one. Every unreadable
  input is ZERO budget. It is NOT a latch and must never trip the kill switch
  (durable → permanent). → [loss-budget sizing](docs/decisions/2026-09-21-loss-budget-sizing.md)
- **The traded account is DEDICATED to Argus, and that is a premise of this
  rail, not a convenience.** Every account-derived number Argus reads is a
  whole-account number treated as an Argus-only number: reconciliation, the
  concurrency cap, the daily-loss breaker and the relative size rail all read
  whole-account state. A single foreign position makes all four wrong at once
  and hard-halts the process at the first reconcile. `[broker] account_number`
  names the account — `config.py` refuses `environment = "live"` without it —
  so the account must hold nothing Argus did not place, and no cash may move
  into or out of it during a trading day.
  → [the account is not dedicated](docs/decisions/2026-08-31-live-account-is-not-dedicated.md)
- **Broker is the only source of truth**, always. On every restart, reconcile
  broker state before resuming automated logic; refuse to resume on any
  mismatch and alert instead of guessing. **That includes an order Argus cannot
  READ:** `get_live_orders()` returns `LiveOrders` (mappable + `unmappable`),
  never a filtered list, and the unreadable ones are a required argument to
  `reconcile_orders` that halts — never suppressed by a ledger row. **Never
  re-narrow that return type**, and treat a comment naming the callers it is
  safe for as expired the moment one is added.
  → [unmappable orders](docs/decisions/2026-08-20-unmappable-orders-reach-reconciliation.md)
  In **sandbox/live** a mismatch is a process-wide hard halt
  (`source="reconciliation_mismatch"`); **shadow** alone is lenient (abort the
  day, keep running) because it holds no order state — the boundary is
  **shadow-vs-rest, not live-vs-rest**. **How WIDE that halt reaches is a
  second, orthogonal question** (`reconcile.halt_grade`): `HaltGrade.ENTRIES`
  still engages the kill switch and still refuses to trade again without a
  human, but skips `request_stop()` so the watchdog keeps its held positions —
  earned ONLY by an order-level discrepancy confined to Argus's own working
  OPENING orders with no position discrepancy of any kind. `ALL` is the
  DEFAULT for every other halt source. **The two functions must never be
  merged**: one asks "process or day?", the other "given a halt, how wide?".
  → [halt scope split](docs/decisions/2026-08-29-halt-scope-split.md),
  [a watchdog exception must not take the watchdog](docs/decisions/2026-09-10-a-watchdog-exception-must-not-take-the-watchdog.md)

  Exactly TWO sanctioned stand-downs, neither a relaxation:
  - **Sandbox 24h wipe** — sandbox only, rolled-over trading day only. Clears
    **POSITIONS ONLY, never `orders`/`order_legs`** — cert's order-reporting
    window is a rolling 24 HOURS, a DIFFERENT window from the trading-day
    rollover, and deleting order rows inside that overlap turns Argus's own
    fills into `unexpected_filled_order`. **Never conflate those two windows.**
    → [sandbox reset](docs/decisions/2026-08-17-sandbox-reset-order-ledger.md)
  - **Expired positions** — a `missing_at_broker` row whose OWN OCC expiry is
    strictly before today, **or today once today's session has SETTLED**
    (close + 15 min, half-day aware, broker-confirmed never clock-assumed). A
    FUTURE expiry and an unreadable symbol both still halt.
    → [expired positions](docs/decisions/2026-08-12-expired-position-resolution.md)

  **`open_position_count` is a REST-seeded MIRROR of the broker, never the
  broker.** A closed position is OMITTED from a REST snapshot, not reported
  flat, so `evict_absent` must subtract — but **never on absence alone**: it is
  AGE-GATED (`staleness.account_position_evict_after_s`), because the broker's
  book trails a fill by ~4–5s. The `held_but_unbooked` page confirms against
  REST first and is suppressed only while Argus's OWN closing fill explains the
  excess legs — **evidence, never a timer**.
  → [ghost positions](docs/decisions/2026-08-19-account-cache-ghost-positions.md)
- **Nothing submits an order outside `pipeline.trades_for_real`.** Shadow's
  terminal action is `shadow.would_submit` and nothing else — not a dry run,
  not a validation call. One boundary, one branch, asserted by an AST test.
- **Single-legging guardrail:** spreads are always submitted as one multi-leg
  order. Never route legs separately. Enforced structurally in
  `execution/build.py`. NB this means tastytrade's PLAIN `place_order`, **not**
  its Complex Order API, which the no-broker-side-stops rail rules out; an AST
  test asserts it appears nowhere in `src/`.
- **No broker-side stop-market orders.** Exits use a virtual stop (sustained
  threshold evaluated in-process against live quotes, debounced) handed to the
  price-walking loop — never a stop order sent to the broker, never an
  unbounded market order. **The time-based flatten changes nothing here**: a
  third reason to fire, routed through the SAME `ExitExecutor.close`, never
  constructing a price of its own; an unpriceable flatten sends nothing. If the
  auction will not pay, the escalation is a single market-crossing **limit** at
  the natural ask/bid, capped at the spread width. **That second order requires
  a POSITIVE confirmation the first one is dead**, on `walk.broker_order_id`
  **never the identifier** (a walk reprices; several rungs share one). Still
  working, absent, or unreadable all refuse (`CROSS_REFUSED`) — `any()` over an
  empty list is `False`, and an order nobody read once passed for one confirmed
  cancelled. **Never re-derive "settled" from the ABSENCE of a working order.**
  The FILLED check alone stays identifier-WIDE, deliberately — a fill on any
  rung means the spread is flat.
  → [chunk 4](docs/decisions/execution-chunk-4-watchdog-exit-notifier.md),
  [empty answer](docs/decisions/2026-08-21-empty-broker-answer-is-not-a-verdict.md),
  [partial answer](docs/decisions/2026-08-21-partial-broker-answer-is-not-a-verdict.md)
- **Never hardcode the COA auction timing.** A Cboe-configurable parameter that
  has changed repeatedly — pull it from `[coa]`, not a literal. The current
  values are SETTLED (SR-CBOE-2025-074, no sunset) but stay config, and they
  are TWO separate keys because Cboe publishes and changes them separately. The
  loop waits for BOTH combined before concluding "not filled".
- **Sandbox before live, always.** No phase skips shadow → sandbox →
  live-min-size → scale.
- **Live requires durable audit writes.** `audit.synchronous` is config, never
  a literal, and `environment = "live"` is REFUSED unless it is `FULL`/`EXTRA`
  — the audit DB also holds the `positions` table reconciliation reads back as
  expected state, so the tail is precisely what must not be lost.

**Standing landmines**, each one word of config away from firing:

- **`[heartbeat] trip_on_sustained_stall` must stay `false`.** Chain
  subscription at session start stalls the loop ~0.5s, reaching
  `sustained_breach_count` every morning — and a trip is DURABLE, so flipping
  this halts entries every day until a human presses `POST /resume`; a restart
  no longer clears it. **The same warning governs any per-day trip source,
  including a breaker→kill-switch coupling**: persistence turns a daily latch
  into a permanent one.
  → [detail](docs/decisions/2026-08-05-slack-alert-storm.md)
- **Never fix an exit-path staleness problem by raising
  `staleness.market_data_max_age_s`.** It also governs entry pricing and the
  gate's staleness rail; a fix scoped to the exit path belongs on the exit
  path's own bound instead.
  → [exit freshness](docs/decisions/2026-08-20-exit-order-freshness-bound.md)
- **Adding a `[regime]` section to any config is a LIVE behaviour change.** The
  skew/IV gate is deliberately inert (telemetry only, uncalibrated); the event
  blackout is separately LIVE and needs no such section. Don't add one to
  "complete" the file. → [detail](docs/decisions/regime-telemetry-and-event-blackout.md)
- **`max_concurrent_positions` is SPREADS and is welded to `daily_max_loss`.**
  Currently `2 × max_loss_per_position (475) = 950 ≤ daily_max_loss (1050)`
  since v0.5.2 (was `1 × 950 = 950`, exactly, before it). The gate reads
  `RiskSnapshot.open_spread_equivalent`, never a leg count. **The invariant to
  preserve is `max_concurrent_positions × max_loss_per_position ≤
  daily_max_loss`** — the breaker only stops NEW entries and cannot close what
  is open, so breaking it lets the breaker trip having already authorized more
  than it caps. Move one, re-check the other two. **It bounds what is open AT
  ONCE, not the day** — the loss budget (above) bounds the day; the equality
  stays so the cap is not a number the budget silently overrides at 10:00.
  → [live-fire pass](docs/decisions/2026-08-13-live-fire-sandbox-pass.md)
- **Spreads CHAIN, and nothing symbol-keyed can count them.** `signal.width =
  5.0` against SPX's 5-point ladder makes consecutive candidates share a
  strike, and a shared strike nets to ZERO in `positions` AND at the broker, so
  k chained spreads present as 2 legs however large k is. Holdings are
  attributed **per TRADE IDEA** from `order_legs.applied_quantity`, never by
  net symbol, and the gate takes the **HIGHER** of the broker- and
  ledger-derived spread counts. **Never reintroduce a symbol-keyed count on
  either path** — it has already cost live positions with no stop evaluated
  and a spread opened past the cap.
  → [overlapping verticals](docs/decisions/2026-08-17-overlapping-verticals.md)

## Architecture at a glance

Single persistent `asyncio` process (not request/response — this needs to be
always-on during market hours). Signal engine → pre-trade risk gate → order
execution engine → tastytrade API. Position risk watchdog runs as a fully separate
task, independent of the signal engine, watching open positions continuously.
Everything writes to a hash-chained audit log before the next action proceeds. Full
diagram: spec §4 and `docs/architecture/`; reused patterns: spec §5's table.

**Ports for pluggable backends:** market data and the broker are `Protocol`s, so
`DXLinkMarketData`/`FakeMarketData` (and a future `SimBroker`) are interchangeable;
the deterministic fake seeds the planned tastytrade simulator. The pre-trade gate
consumes an already-fetched `RiskSnapshot` rather than fetching from the broker
itself, so it stays a pure decision function.

**0DTE lives under the `SPXW` root, not `SPX`.** `chain.chain_symbol` (SPXW) fetches
the chain; `chain.underlying_symbol` (SPX) quotes spot. Fetching the chain from SPX
returns only monthly third-Friday expirations — an empty 0DTE chain and a loop that
logs `signal.skip` forever. Strike ladders have gaps, so the long leg is the nearest
strike within `[signal.width, signal.max_width]` and all economics use the ACTUAL
strike distance.

## Build / test / lint

Python 3.12, `src/` layout. The runtime is **stdlib-only apart from the tastytrade
SDK** (sqlite3, tomllib, asyncio) — deliberately, including the HTTP control surface
(`control.py` is `asyncio.start_server`, not a web framework). Revisit only if a
chunk needs real HTTP surface area; the full reasoning is in
[monitoring surface](docs/decisions/monitoring-surface.md).

```
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[dev]"
./.venv/bin/python -m pytest        # asyncio_mode=auto, see pyproject.toml
./.venv/bin/python -m argus         # run the supervisor (Ctrl-C / SIGTERM to stop)
```

Config lives in a per-PROFILE file (COA timing, risk caps, staleness thresholds —
none hardcoded). **`ARGUS_ENV` names the FILE** (`dev` -> `argus.dev.toml`, resolved
against `ARGUS_CONFIG_DIR`); `ARGUS_CONFIG` gives an explicit path and wins. Neither
set is a `ConfigError` — **there is no default**. The file's own `environment` key
remains the sole authority on trading behaviour and the loader REFUSES when the
filename and the key disagree. Only `python -m argus` reads those variables; every
`argus.tools.*` REQUIRES `--config`. **A config release is still a restart and
must stay one — never add hot-reload.** The image carries neither an
`ARGUS_CONFIG` nor a baked config — **never re-add either**; a drifted bind mount
falling back to a baked copy is a failure the config fingerprint cannot see, since
both would come from the same checkout. Lint/format tooling: TBD. The `tools.*` smoke checks against
sandbox — quotes, account/reconciliation, chain, order primitives — are the manual
steps that validate the adapters and can't be unit-tested; see `README.md`.

### Credentials

**Never in config or code.** All from env, via `.env` at the repo root
(gitignored); entry points call `argus.bootstrap.load_env()` and real env vars win
over the file. See `.env.example`. Tastytrade OAuth is Argus's OWN app
registration, never TOMIC's. Sandbox-first is enforced at the session boundary —
`TastytradeSession.is_test` derives from `environment`, so only
`environment = "live"` can reach the live tastytrade API.

**Credentials sit on a (ROLE, WORLD) grid and the CREDENTIAL is keyed on the same
authority as the ENDPOINT.** `TASTYTRADE_<ROLE>_<WORLD>_<FIELD>` —
`TASTYTRADE_TRADING_CERT_*` (always), `TASTYTRADE_TRADING_LIVE_*` (only under
`environment = "live"`), `TASTYTRADE_MARKETDATA_LIVE_*` (whenever
`[marketdata] source = "live"`, i.e. always in practice). Role first, so
`grep TASTYTRADE_TRADING_` is a complete audit of what can reach an order. **Both
trading worlds live in `.env` at once and the cutover does not touch it.**
`trading_credentials(config)` / `market_data_credentials(config)` are the ONLY ways
to build a credential (an AST sweep bans a bare `from_env()`); a `live` profile
refuses to start with no live slot or with one holding the cert values; and the
refusal lives in `broker/session.py`, **never `config.py`** — the fail-closed
loader has no business reading the environment. `/status` names the slot in use.
**Changing `.env` is a manual, per-host step no image carries** — add the new names
before deploying, delete the old ones after.
→ [environment-keyed trading credentials](docs/decisions/2026-09-09-trading-credentials-are-environment-keyed.md)

- `ARGUS_KILL_TOKEN` — **REQUIRED at process start**, unlike the broker
  credentials: `startup()` refuses to boot without it, because an unauthenticated
  halt control is a broken safety rail.
- `ARGUS_SLACK_WEBHOOK_URL` — optional: a missing webhook is a missing notification
  channel, not a broken rail, so it must never refuse startup.
- `ARGUS_FRED_API_KEY` — operational only (blackout-dates generator).

**Two scoped sessions (`SessionRole`).** `[marketdata] source = "live"` reads
quotes/greeks/instruments READ-ONLY from production while orders keep routing per
`environment` (cert) — sandbox quotes are 15-min delayed, fine for mechanics and
useless for judging signal quality, which is why this exists. Live market data needs
its OWN registration (`TASTYTRADE_MARKETDATA_LIVE_*`). The split is enforced structurally:
`tt.trading_session` RAISES on a market-data session, so nothing that can move money
can hold the (possibly live) one. Use `tt.market_data_session` for quotes/chain,
`tt.trading_session` for account/orders. `open()` forces the initial token refresh
(the SDK does NOT authenticate on construction); `validate()`/`refresh()` are async.

## File structure

Condensed. Full per-file notes — including the SDK streamer task-ownership hazard and
the hardened liveness refresh — are in
[`docs/decisions/component-notes.md`](docs/decisions/component-notes.md).

```
src/argus/
  __main__.py app.py scheduler.py marketcalendar.py session.py
        # supervisor + one session per trading day + run_session, THE pipeline body
        # (login -> reconcile -> chain -> loop -> terminal action). app._halt_process
        # is THE kill-switch+request_stop pairing every internal halt reuses
  control.py status.py    # POST /kill + read-only GET /status, GET / on ONE listener
                          # (Tailscale bind, bearer auth) -> one StatusSnapshot assembly
  config.py heartbeat.py  # fail-closed loader; event-loop-lag early warning
  alerts.py notify.py     # AlertFn seam -> SlackNotifier (never raises, never blocks)
  orchestrator.py         # SignalLoop; size_contracts applies ALL THREE rails
  reconcile.py            # pure compare + mismatch_halts_process(env) + order recon
                          # + reconcile_and_record + sandbox-reset + expiry verdicts
  occ.py                  # occ_expiry ONLY; the sole OCC symbol parser. None means
                          # "cannot say" and every caller must fail closed on it
  clock.py staleness.py   # ET exchange time (never host-local); THE fail-closed
                          # staleness rule. NB account state needs a LIVENESS FLOOR —
                          # AlertStreamer pushes only on change, so a flat account
                          # reads "stale" and would block every entry
  signal/                 # models chain contracts live_chain strategy regime implied
                          # ladder = `signal.ladder`, the strike ladder once a minute
                          # (telemetry; never read in src/, never costs a decision)
                          # strategy holds the LIVE blackout; regime is telemetry only;
                          # implied = the mid-implied short-strike delta (pure, stdlib)
  db/ audit/              # WAL + flock(one writer) + immutable migrations; sha256
                          # chain, Tier 1 awaited / Tier 2 queued, read(fn)/write(fn)
                          # = THE single-writer-connection gateway
  risk/                   # killswitch(owns no stop event) breaker gate models snapshot
                          # rehydrate = the day's trip + P&L anchor read back per
                          # TRADING DATE, so a same-day restart cannot re-arm
  marketdata/ account/    # ports + adapters + fakes. ONE task owns each SDK streamer.
                          # account/refresher.py = the liveness re-read, on its OWN
                          # single-worker executor (NEVER the default one, which is
                          # the audit log's) with a sync client and a per-dispatch
                          # token. It bounds ITSELF; the caller only backstops it
  broker/session.py       # OAuth wrapper; sandbox-first enforced at the boundary
  broker/ratelimit.py     # THE request governor (SPX's, extracted 2026-10-08) + paced_read
  execution/              # __init__ re-exports NOTHING (would cycle via zerobid)
                          # zerobid models build broker fake                       ch1
                          # store(ONLY writer of positions) fills guard audited    ch2
                          # loop(THE walk state machine) ratelimit                 ch3
                          # watchdog(virtual stop, LegQuotes, ExitExecutor)        ch4
                          # entry pipeline(THE submission boundary + stack)        ch5
                          # working_orders(the cap's 3rd term) escalation(rising/
                          # falling/overdue, shared) residual_orders(orders a walk
                          # left working: registered at _finish, rechecked on the
                          # watchdog's poll, minted_direction NEVER the broker's)
                          # flatten_deferral(the far-OTM flatten's calibrated
                          # insurance test; pure — tools.flatten_calibration fits it)
  shadow/ tools/          # settlement math + dedup + marking; manual smoke tools.
                          # marker reads proposal categories as a union keyed on
                          # CATEGORY PRESENCE, never on environment; fills attribute
                          # via external_identifier, never a join to the mutable
                          # orders table. tools.desk (read-only desk view) REUSES
                          # marker's fill reconstruction rather than scoring a second
                          # way; settlement stays in tools.mark. tools.evaluability
                          # is the blind-episode + write-rate instrument;
                          # tools.loop_lag measures on-loop vs off-loop lag against
                          # real TLS (local stub, or --live-cert); tools.regime
                          # reads the regime telemetry back — the unit is a DAY at a
                          # FIXED ET clock, never a pooled tick, because skew CROSSES
                          # ZERO inside the entry window on the clock alone; it also
                          # reports signal/realized.py's RV and IV/RV off the recorded
                          # index path (annualization basis IMPORTED from regime.py)
                          # -> docs/decisions/2026-08-12-markable-dry-run-records.md
tests/ Dockerfile docker-compose.yml scripts/ requirements.txt  # hash-pinned lock
argus.sandbox.toml        # cert sessions. NO LONGER THE DEPLOYED PROFILE since the
                          # 2026-09-15 cutover, and compose mounts only the ACTIVE
                          # profile's file — so reading cert history now needs an
                          # explicit -v mount (runbook section 8). NAMED BY PROFILE,
                          # like all three: `argus.toml` no longer exists and is
                          # gitignored, because a profile-less name arms no
                          # filename<->environment check
argus.dev.toml            # dev/tools -> dev/
argus.live.toml           # LIVE, AND THE DEPLOYED PROFILE SINCE 2026-09-15. Its own
                          # db_path (argus-live.db) so it can never continue the cert
                          # hash chain. Pins broker.account_number
                          # = "5XX00000" (REQUIRED under live, config.py refuses
                          # without it). A SIBLING of the file above, not a fork:
                          # tests/test_config_profiles.py pins identical key sets
                          # + an allowlist of intended drift
/etc/argus/host.env       # NOT IN GIT, per host, root:600, one line ARGUS_ENV=<p>.
                          # argus.service reads it (EnvironmentFile, no `-`);
                          # compose interpolates ${ARGUS_ENV:?}. THE CUTOVER WAS
                          # THIS FILE CHANGING, 2026-09-15: it now reads
                          # ARGUS_ENV=live, and the rollback is the same line
blackout_dates.txt        # GENERATED. Currently ENDS 2026-12-24 — regenerate with
fomc_dates.txt            # `tools.blackout_dates --omit CPI --omit NFP --omit PCE`
                          # (the 8:30 releases are TRADED since 2026-09-28; the
                          # file's header names the flags, a test pins them) when
                          # 2027 calendars publish. Deploy
                          # WITH the config; a missing file is a hard ConfigError.
                          # FOMC days are hand-maintained (no FRED release id exists)
discretionary_blackout_dates.txt  # hand-maintained, OPTIONAL — one-off avoidance
                          # dates (a distrusted news day, an operator's day off),
                          # same format as fomc_dates.txt, merged in on every
                          # `tools.blackout_dates` regen. Empty is the normal
                          # state and never refuses a regen (unlike FOMC/CPI/NFP/
                          # PCE). NEVER add these to `[calendar] holidays`
                          # instead — that means the exchange is closed and
                          # drives session scheduling + 0DTE expiry resolution;
                          # this means "market's open, we choose not to enter"
                          # and leaves the watchdog/exits/flatten untouched
argus.service             # THE SPX book's always-on unit (STOPPED + DISABLED since
                          # 2026-10-05; the deploy never starts it). argus-shadow.timer
                          # is RETIRED — do not enable it; both take the writer lock.
                          # Carries EnvironmentFile=/etc/argus/host.env: the profile
```

## Convention

Living doc — update it each session as real decisions get made, same as TOMIC, and
don't let it drift from what's actually built. **But it is loaded into every session
regardless of task, so every line competes for attention on work it may have nothing
to do with.** It has been re-audited more than once for exactly this reason (most
recently 2026-09-27, from ~1,800 lines). Keep it that way:

- **A resolved investigation gets ONE current-state sentence here plus a pointer to
  `docs/decisions/`.** The narrative — what was measured, what was ruled out, what
  the mechanism was — goes there. Add it to `docs/decisions/README.md` so it isn't
  orphaned.
- **Day-to-day operating detail goes to `README.md`**; packaging, provisioning and
  cutovers to `docs/argus-deployment-runbook.md`. Don't re-duplicate the split
  already declared at the top of this file.
- **What earns a place here:** current phase/state, the non-negotiables, build/test
  commands, the file map, standing landmines. Anything about *how a past problem got
  solved* rather than *what's needed to work on this repo today* goes to `docs/`.
- **Re-audit this file if it crosses ~300 lines.** As of the 2026-09-27 pass it
  sits well above that on the strength of the non-negotiables and file map alone —
  both earn their place under the rule above, so the honest ceiling for a system
  with this many live safety rails is higher than 300; treat the number as a
  prompt to re-audit, not a hard cap to force.
