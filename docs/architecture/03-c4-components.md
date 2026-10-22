# C4 Level 3 — Components of the Argus supervisor

**Audience:** dev leads. Answers "what is inside the one process, and which way do the
dependencies point".

The supervisor decomposes into three **lifetimes**, and that split is the single most
useful thing to understand about the internal structure:

- **Process lifetime** — built once by `Application.startup()` and alive across trading
  days, nights, weekends and halts: the audit log, the kill switch (rehydrated from the
  log), the control server, the status service, the heartbeat, the notifier, the scheduler.
- **Session-day lifetime** — rebuilt for each trading day by
  `Application._run_session_day` and `session.run_session`: the daily-loss breaker and
  day-P&L anchor (rehydrated per trading DATE), the pre-trade gate, both broker sessions,
  the market-data and account streams, the chain, the signal loop.
- **Trading side** (`session.TradingSide`) — exists only when
  `pipeline.trades_for_real(config)` is true, i.e. sandbox and live: the execution stack,
  the entry executor, the position watchdog with its exit executor, the fill consumer and
  the residual-order recheck. **Built together or not at all** — a watchdog without the fill
  consumer would watch a `positions` table nobody writes.

Three corrections to the shape one might expect. First, **price-walking is not the exit
path**: `execution/loop.py` is *one* state machine used by both entry and exit,
parameterized by `WalkPolicy.for_entry` / `for_exit`; the exit-specific logic (when to
close, the flatten, the market-crossing price) lives in `execution/watchdog.py`. Second,
**reconciliation is not restart-only**: the same `reconcile_with_retry` →
`reconcile_and_record` sequence runs at process start and at every session start, and the
two sites differ only in how they refuse. Third, **the kill switch has a deliberately
one-sided blast radius**: it is handed to the entry executor and to nothing on the exit
side, so a halt can never cancel the order closing a position.

The component view is drawn in three diagrams rather than one, because one diagram with
forty boxes renders as noise: **(A)** the process layer and its safety rails, **(B)** one
trading day's pipeline, **(C)** the execution package.

## (A) The process layer and its safety rails

Everything here outlives a trading day. The arrows into `Application` from below are the
**halt seams** — every component that can decide the process must stop trading calls back
into `_halt_process`, which is the one place a halt is graded, latched, announced and (for
`ALL`) turned into a stop.

```mermaid
%%{init: {"c4": {"useMaxWidth": false}}}%%
C4Component
    title (A) Process layer — built once by Application.startup

    Person(operator, "Operator", "phone on tailnet")
    System_Ext(slack_ext, "Slack", "webhook")
    System_Ext(tt, "tastytrade", "trading REST, short-lived at startup")

    Container_Boundary(proc, "Argus supervisor process") {
        Component(cfg, "Config loader", "config.py", "Fail-closed TOML, selected by PROFILE with no default. Refuses a filename-environment mismatch, live without FULL fsync, live without account_number.")
        Component(app, "Application", "app.py", "Startup order, the scheduler drive, the park. Owns _halt_process: engage, latch a HaltRecord with a GRADE, announce once, and request_stop only for ALL.")
        Component(lock, "DatabaseLock", "db/lock.py", "SAFETY. Exclusive flock before the DB opens.")
        Component(audit, "AuditLog", "audit/log.py", "SAFETY. Hash chain. Tier 1 awaited, Tier 2 queued. read/write are THE gateway to the one writer connection. A failed write latches and fires on_failure.")
        Component(killstate, "Kill state reader", "risk/kill_state.py", "SAFETY. Engaged iff the newest trip has no later resume by seq. No date bound. Unreadable fails closed.")
        Component(ks, "KillSwitch", "risk/killswitch.py", "SAFETY. engage flips then alerts then records. disengage records then flips. Owns no stop event.")
        Component(ctl, "ControlServer", "control.py", "SAFETY. POST /kill, POST /resume, GET /status, GET /. Bearer auth before routing. Binds BEFORE reconciliation.")
        Component(stat, "StatusService + SessionStatus", "status.py", "ONE assembly to JSON and HTML. Reads live objects for current state and the DB for history.")
        Component(hb, "LoopHeartbeat", "heartbeat.py", "Event-loop lag warning. Its trip hook is config-gated and ships off.")
        Component(notify, "SlackNotifier + AlertFn", "notify.py, alerts.py", "Enqueue-only, worker POST, never raises. Throttled per kind and per position scope.")
        Component(sched, "SessionScheduler + MarketCalendar", "scheduler.py, marketcalendar.py", "One session per ET trading day. Half-day aware. Bridges the process stop to the session stop.")
        Component(recon, "Reconciler", "reconcile.py", "SAFETY. Positions AND orders vs broker, incl. unmappable orders. Retry on unreachable. mismatch_halts_process and halt_grade are the two boundaries.")
        Component(rehydrate, "Day-risk rehydration", "risk/rehydrate.py", "SAFETY. Reads today's breaker trip and FIRST anchor back, so a same-day restart cannot re-arm the budget.")
        Component(sess, "Session body", "session.py run_session", "One trading day. See diagram B.")
    }

    Rel(operator, ctl, "kill, resume, status", "HTTP + bearer")
    Rel(ctl, ks, "engage, disengage")
    Rel(ctl, stat, "assemble")
    Rel(stat, audit, "read")
    Rel(app, cfg, "loads")
    Rel(app, lock, "acquire first")
    Rel(app, audit, "builds, sets on_failure")
    Rel(app, killstate, "load_kill_switch_state")
    Rel(killstate, audit, "read trip and resume rows")
    Rel(app, ks, "builds engaged or not, engages via _halt_process")
    Rel(app, ctl, "builds and binds")
    Rel(app, hb, "builds")
    Rel(app, notify, "resolves the AlertFn")
    Rel(app, recon, "process-start reconcile, retried then PARKED")
    Rel(recon, tt, "positions + live orders")
    Rel(app, sched, "drives unless halted ALL")
    Rel(sched, app, "_run_session_day")
    Rel(app, rehydrate, "per trading date")
    Rel(app, sess, "runs with halt and halted_grade seams")
    Rel(ks, notify, "alert on every trip")
    Rel(ks, audit, "kill_switch.trip / resume")
    Rel(audit, app, "on_failure: halt ALL")
    Rel(sess, app, "halt seam: mismatch, post-submit entry failure, watchdog degraded")
    Rel(notify, slack_ext, "alerts", "HTTPS")
```

## (B) One trading day's pipeline

`run_session` is THE pipeline body — `tools.shadow` reuses it through
`Application.run_single_session`, so it never lives in two places. The order is the safety
order: log in (ban-aware retry), build the execution stack, **reconcile**, then market data,
the fill consumer (wired before the account stream starts delivering), the account stream,
the chain, the risk snapshot, the trading side, and finally the signal loop.

```mermaid
%%{init: {"c4": {"useMaxWidth": false}}}%%
C4Component
    title (B) Session-day pipeline — run_session

    System_Ext(tt_md, "tastytrade market data", "DXLink, production, read-only")
    System_Ext(tt_tr, "tastytrade trading", "REST + AlertStreamer")

    Container_Boundary(day, "One trading day") {
        Component(md, "DXLinkMarketData", "marketdata/dxlink.py", "Quote and greeks adapter behind MarketDataPort. One task owns the streamer.")
        Component(chain, "DXLinkChainSource", "signal/live_chain.py", "0DTE SPXW chain in a strike window. Leg admission is ROLE-SCOPED: fresh quote to price, fresh greeks to be the SHORT leg (short_delta_source = feed only; mid needs the quote alone).")
        Component(strat, "CreditSpreadStrategy", "signal/strategy.py", "Entry window, then the LIVE event blackout, then a short strike selected by delta (mid-implied via signal/implied.py, or the feed's: signal.short_delta_source) and a width-selected long strike. Baseline, not a validated edge.")
        Component(regime, "Regime telemetry", "signal/regime.py", "ATM IV and skew per tick, recorded and never acted on. No [regime] section ships.")
        Component(acct, "AlertStreamerAccount + cache", "account/stream.py, account/cache.py", "Positions and balances mirror with eviction of absent legs, plus the on_order hook.")
        Component(refresh, "ThreadedAccountRefresher", "account/refresher.py", "Liveness re-read OFF the event loop, on its own single-worker thread. Bounds itself.")
        Component(snap, "RiskSnapshotBuilder + DailyPnl", "risk/snapshot.py", "NLV, day P&L vs the anchor, data ages, and open_spread_equivalent from broker legs, ledger ideas and working orders, now split per SIDE (occ_right; an unreadable side counts as its own worst-case entry, v0.5.3).")
        Component(gate, "PreTradeGate", "risk/gate.py", "SAFETY. Kill switch, breaker, concurrency in SPREADS, three size rails, a fourth DAY loss budget, side balance, fail-closed staleness. Records gate.decision.")
        Component(brk, "DailyLossBreaker", "risk/breaker.py", "SAFETY. Latching, per trading date.")
        Component(budget, "LossBudget", "risk/loss_budget.py", "SAFETY, since v0.4.6 (2026-09-21). Pure: daily_max_loss minus today's realized loss and every open position's worst case. Non-latching; every unreadable input is ZERO budget.")
        Component(sidebal, "side_balance", "risk/side_balance.py", "Since v0.5.2 (2026-09-26). Pure: admissible(side) refuses an entry that would leave |puts - calls| > max_side_imbalance; rank() breaks ties toward the more-balanced side.")
        Component(loop_sig, "SignalLoop", "orchestrator.py", "Cadence loop: chain, strategy, size_contracts, gate, terminal action. submit None means shadow or monitor-only.")
        Component(entry, "EntryExecutor", "execution/entry.py", "The SubmitFn. See diagram C.")
        Component(wd, "PositionWatchdog", "execution/watchdog.py", "Separate task. See diagram C.")
        Component(fills, "FillConsumer", "execution/fills.py", "Books fills from the stream into positions. Never adopts a foreign order.")
    }

    Rel(md, tt_md, "subscribe", "DXLink")
    Rel(chain, md, "quotes + greeks")
    Rel(strat, chain, "ChainSnapshot")
    Rel(loop_sig, strat, "evaluate")
    Rel(loop_sig, regime, "compute_regime, telemetry only")
    Rel(strat, regime, "time basis + parity forward for the mid-implied delta (never its readings)")
    Rel(loop_sig, snap, "build")
    Rel(loop_sig, gate, "evaluate")
    Rel(gate, brk, "evaluate day P&L")
    Rel(gate, budget, "compute_loss_budget, refuse past it")
    Rel(loop_sig, sidebal, "orchestrator ranks the side to try first")
    Rel(gate, sidebal, "admissible — refuse side_imbalance_exceeded")
    Rel(loop_sig, entry, "submit — sandbox and live, not monitor-only")
    Rel(snap, md, "market data age")
    Rel(snap, acct, "NLV, legs, stream age")
    Rel(snap, wd, "ledger spread count + working opening orders, late-bound")
    Rel(acct, tt_tr, "AlertStreamer", "WebSocket")
    Rel(refresh, tt_tr, "positions + balances", "REST, off-loop")
    Rel(acct, refresh, "periodic liveness floor")
    Rel(acct, fills, "on_order")
    Rel(fills, wd, "note_fills, request_refresh")
    Rel(entry, wd, "on_filled: request_refresh")
    Rel(wd, acct, "broker leg count for the book-agreement check")
```

## (C) The execution package

The order path is a decorator stack, and the nesting is load-bearing: **audit outermost,
throttle inside, adapter innermost** (`pipeline.build_execution_stack`, pinned by a test).
Audit outside means the Tier 1 "about to send" record is durable regardless of how long the
wire waits for a rate-limit slot; throttle inside means the one process-wide governor covers
every order request, including the guard's and the watchdog's reads.

```mermaid
%%{init: {"c4": {"useMaxWidth": false}}}%%
C4Component
    title (C) Execution package — src/argus/execution

    System_Ext(tt_orders, "tastytrade /orders", "place, replace, delete, get_live_orders")
    ComponentDb(sqlite, "audit DB", "SQLite", "orders, order_legs, positions, audit_log")
    Component_Ext(ks, "KillSwitch", "risk/killswitch.py", "entry side ONLY")

    Container_Boundary(ex, "execution") {
        Component(pl, "pipeline.py", "chunk 5", "build_execution_stack and trades_for_real. THE assembly and THE submission boundary.")
        Component(aud, "audited.py", "chunk 2", "Tier 1 record before the wire, ledger row after the ack. Direction disagreement is Tier 2 telemetry.")
        Component(rl, "ratelimit.py", "chunk 3", "Process-wide minimum-interval governor, 40 req/s ceiling.")
        Component(bro, "broker.py", "chunk 1", "Plain place_order, never the complex-order API. get_live_orders returns LiveOrders incl. unmappable.")
        Component(build, "build.py + zerobid.py", "chunk 1, pure", "One multi-leg order from a floored mid. Signed tick-aligned limit, capped at the strike WIDTH. Deterministic external_identifier.")
        Component(mdl, "models.py", "chunk 1-3", "Order, OrderStatus mirror, classify_order 3-way, CONFIRMED_IN_MARKET holds LIVE only, direction from the minted identifier.")
        Component(gd, "guard.py", "chunk 2", "DuplicateOrderGuard. Asks the BROKER, raises when it cannot. Since v0.5.1/v0.5.2 (2026-09-24): CloseQuantity lets a close of q contracts through when the broker's own per-leg fills prove q still fits what is held.")
        Component(lp, "loop.py", "chunk 3", "PriceWalkingLoop. Guard burst, submit, await Live, hold a COA cycle, observe, drift, reprice, recover. _finish probes the identifier before any non-fill verdict.")
        Component(en, "entry.py", "chunk 5", "EntryExecutor. Kill re-check, EntriesEnded latch, guard burst and streak, soft and hard exception boundary, drift breaker.")
        Component(wdg, "watchdog.py", "chunk 4", "PositionWatchdog + ExitExecutor + LegQuotes. FOUR triggers since v0.5.0 (credit_multiple, underlying-near-short-strike, flatten_before_close, profit_target), debounce, the exit walk, cross escalation, send budget, degrade-not-die, the flatten-deferral seam, the stub-close continuation.")
        Component(fdef, "flatten_deferral.py", "pure, v0.5.2", "decide(). k x P_touch(z) x width <= one tick, Pareto tail from tools.flatten_calibration. Only ever REMOVES a flatten send; every missing/stale input refuses to defer.")
        Component(st, "store.py", "chunk 2", "SqliteOrderStore. ONLY writer of positions. open_spreads per trade idea, realized entry credit, expiry-aware.")
        Component(fl, "fills.py", "chunk 2", "FillConsumer.")
        Component(wo, "working_orders.py", "P4", "Opening orders still working, as spread equivalents for the gate.")
        Component(rr, "residual_orders.py", "P5", "Orders a walk left working. Rechecked until terminal. Re-cancels OPENING only.")
        Component(esc, "escalation.py", "shared", "Rising, falling, overdue. One bound on noise, one on silence.")
    }

    Rel(pl, aud, "outermost")
    Rel(aud, rl, "delegates")
    Rel(rl, bro, "delegates")
    Rel(bro, tt_orders, "REST")
    Rel(aud, st, "record_order")
    Rel(st, sqlite, "SQL via AuditLog.write")
    Rel(fl, st, "apply")
    Rel(gd, aud, "get_live_orders")
    Rel(en, ks, "engaged, and halt_check for the walk")
    Rel(en, gd, "once, before the first submit")
    Rel(en, build, "build_order OPEN")
    Rel(en, lp, "run, guard_first_submission False")
    Rel(wdg, st, "open_spreads")
    Rel(wdg, build, "build_order CLOSE")
    Rel(wdg, lp, "run, guard_first_submission True, no halt_check")
    Rel(wdg, aud, "crossing submit")
    Rel(wdg, wo, "count from get_live_orders")
    Rel(wdg, rr, "step on the same answer")
    Rel(wdg, fdef, "gather IV/RV/spot -> decide, own every side effect")
    Rel(wdg, gd, "CloseQuantity on a stub close's guarded ask")
    Rel(lp, rr, "register at _finish")
    Rel(lp, aud, "submit, replace, cancel, get_live_orders")
    Rel(lp, mdl, "classify_order, is_in_market")
    Rel(rr, aud, "cancel, opening orders only")
    Rel(rr, esc, "pages")
    Rel(wdg, esc, "pages")
```

## Safety-critical path vs. signal/execution logic

The distinction that matters when changing code here: a bug in the left column can cost a
trade; a bug in the right column can cost the account.

| Signal / execution mechanics | Safety-critical (fail-closed, refuses rather than guesses) |
|---|---|
| `signal/strategy.py` + `signal/implied.py` — candidate selection (mid-implied short-strike delta) | `risk/killswitch.py` + `risk/kill_state.py` — halts new entries; durable; cleared only by `/resume` |
| `signal/live_chain.py`, `signal/contracts.py` — chain assembly | `risk/breaker.py` + `risk/rehydrate.py` — latching daily max-loss, per trading date |
| `signal/regime.py`, `signal/realized.py` — telemetry only | `risk/gate.py` — kill switch, breaker, spread-counted concurrency, three size rails, staleness |
| `marketdata/dxlink.py`, `marketdata/book.py` — quote ingest | `staleness.py` — the ONE fail-closed staleness rule (NaN and negative ages are stale) |
| `orchestrator.py` — cadence and sizing arithmetic | `execution/guard.py` — the only duplicate-submission backstop; raises rather than allowing |
| `execution/build.py`, `execution/zerobid.py` — construction and tick floor | `execution/watchdog.py` — the virtual stop, the flatten, degrade-not-die |
| `execution/loop.py` — cadence, sequencing, the reprice ladder | `execution/residual_orders.py` — the "already working" half of the kill switch's promise |
| `execution/ratelimit.py` — request governor (insurance, not cadence) | `reconcile.py` — broker-is-truth; refuses to resume on mismatch or on an unreadable order |
| `status.py`, `notify.py` — read-only rendering, best-effort alerting | `audit/log.py` + `db/lock.py` — one writer, and a failed write halts trading |
| `account/refresher.py` — liveness plumbing | `control.py` — authenticated halt surface; 200 only after the trip is durable |
| `occ.py` — the one OCC symbol parser (`None` means cannot say, and every caller fails closed; `occ_right` added v0.5.3 for the side count) | `config.py` — no default path, profile/filename check, live-only refusals, refuses `flatten_deferral = "on"` without the welded invariant |
| `execution/flatten_deferral.py` — pure arithmetic, no I/O | `risk/side_balance.py`, `risk/loss_budget.py` — refuse an entry that would unbalance the book or exceed the day's remaining budget |
| `tools/ledger.py`, `tools/sheets.py`, `tools/settlement.py` — nightly reporting, read-only, own container | (not safety-critical: an AST test pins that the ledger/Sheets path cannot reach `pipeline.trades_for_real`) |

A heuristic that holds across the codebase: **safety-critical components turn "I cannot
tell" into a refusal, not into permission.** `DuplicateOrderGuard` raises when the broker is
unreachable; `LegQuotes` returns a resolution stating *why* it failed rather than a bare
`None`; `classify_order` treats "the broker never reported quantities" as FILLED; `is_stale`
treats NaN as stale; an unreadable kill-switch state restores ENGAGED; an unreadable
day-risk state halts; a broker answer that does not name the abandoned order refuses the
cross.

## Structural rules enforced by tests, not convention

- `loop.py` takes **no `MarketDataPort`** and imports only tick arithmetic from `zerobid`:
  the reprice ladder is deterministic from the gate-approved mid and never re-quotes. Its
  floor is clamped to the strike width read off the order's own OCC symbols
  (`Order.width`), so the walk stays market-free.
- `loop.py` **never imports `watchdog.py`** (`tests/test_watchdog.py`); the exit is a
  `WalkPolicy`, not a second implementation.
- `ExitExecutor` arms **no `halt_check`** and holds **no kill-switch reference**
  (`tests/test_kill_scope.py`).
- `KillSwitch.disengage` has exactly **one caller** in `src/` — `POST /resume`.
- Nothing submits outside `pipeline.trades_for_real`; the complex-order API appears nowhere
  in `src/`.
- `_ASK_BEFORE_CONCLUDING | _NEVER_ASKS == set(WalkOutcome)`, disjoint
  (`test_every_outcome_is_classified`); `EntryExecutor._report`'s table covers the whole
  enum (`test_the_outcome_table_is_the_whole_enum`).
- The `/status` seam converters in `session.py` name every field of their source type
  (`tests/test_session_status_seams.py`), because an under-copy between two dataclasses with
  `None` defaults is silent at every layer.
- `flatten_deferral.py` and `side_balance.py` are pure — no clock read, no config read, no
  I/O — the same contract `signal/regime.py` and `signal/realized.py` already hold, so their
  decision logic is testable without a watchdog or a gate.
- A profit-target take that does not FILL may never touch `_attempted`, `_sends` or
  `_flatten_attempted` (AST-pinned) — a failed take must cost the position nothing.
- The per-side counts in `risk/snapshot.py` are AST-pinned to come from trade ideas, never a
  bare broker-leg symbol count (CLAUDE.md's fourth landmine, extended to the side rule).
