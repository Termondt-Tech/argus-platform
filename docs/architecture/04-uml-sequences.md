# UML sequence diagrams — how operations actually execute

**Audience:** engineers implementing or debugging these flows. Every participant is a real
class or module and every message is a real call. Anchors are `module::function`, not line
numbers.

**Read this first:** in `environment = "shadow"` only flow 1's first half runs, and it
terminates at `shadow.would_submit`. Flows 1 (from `EntryExecutor` onward) and 2, and the
sandbox/live half of flow 4, require `pipeline.trades_for_real(config)` to be true — which
it is on the droplet since the 2026-09-15 live cutover. A **monitor-only** day (a halt at
`HaltGrade.ENTRIES`, or an entries-ended day) runs flow 2 in full and flow 1 up to the gate.

---

## 1. Entry execution — signal to fill

`SignalLoop.evaluate_once` runs every `[signal] cadence_s`. The terminal action is the only
thing `environment` changes: `submit=None` (shadow, or monitor-only) logs
`shadow.would_submit` or nothing; otherwise `EntryExecutor.submit` is called.

Three boundaries are worth internalising before reading the diagram:

- **The exception boundary is one call.** Everything in `EntryExecutor.submit` is *soft* —
  nothing has been sent, so an unexpected failure ends today's **entries** (not the day,
  not the process: the `EntriesEnded` latch, 2026-09-01). Everything from
  `_walk_and_report` onward is *hard* — an order may exist, so any exception halts the
  process at `HaltGrade.ALL`.
- **The duplicate guard is asked once, above the boundary, in a bounded burst.** A spent
  burst retires the trade idea; only `execution.guard_unreachable_max_streak` spent bursts
  in a row (distinct trade ideas) end the day's entries. The walk is never reached without
  an answered `ConflictVerdict`.
- **The fill that books the position does not come back through this call chain.**
  `WalkResult` is a report. Positions are written by `FillConsumer` off the `AlertStreamer`
  push, independently of who submitted.

```mermaid
%%{init: {"sequence": {"useMaxWidth": false}}}%%
sequenceDiagram
    autonumber
    participant SigLoop as SignalLoop<br/>(orchestrator.py)
    participant Strat as CreditSpreadStrategy
    participant Gate as PreTradeGate
    participant KS as KillSwitch
    participant Entry as EntryExecutor<br/>(execution/entry.py)
    participant Guard as DuplicateOrderGuard
    participant Walk as PriceWalkingLoop<br/>(execution/loop.py)
    participant Stack as Audited → Throttled → Tastytrade
    participant TT as tastytrade /orders
    participant Audit as AuditLog
    participant Resid as ResidualOrderRecheck
    participant Stream as AlertStreamerAccount
    participant Fills as FillConsumer
    participant WD as PositionWatchdog

    SigLoop->>Strat: evaluate(chain_snapshot, et_time, regime)
    note right of Strat: short strike by delta — mid-implied (signal/implied.py, Black-76 on the parity<br/>forward) or the feed's, per signal.short_delta_source, no fallback between them
    alt no signal (incl. outside_entry_window, EVENT_BLACKOUT)
        SigLoop->>Audit: telemetry("signal.none", near-miss incl. raw bid and ask) — Tier 2
    else candidate fired (signal, plus other qualifying sides as alternatives)
        opt risk.side_balance
            SigLoop->>SigLoop: _choose — side_balance.rank over signal + alternatives, most-balancing first, ties by signal.sides, skip a side already attempted today
        end
        SigLoop->>SigLoop: size_contracts — all three size rails plus loss_budget.contracts_within_budget, tightest wins
        SigLoop->>Audit: record("signal.candidate", per-leg bid, ask, mid) — Tier 1
        SigLoop->>Gate: evaluate(proposal, risk_snapshot)
        Gate->>KS: engaged?
        Gate->>Audit: record("gate.decision", reasons, limits, daily_pnl_anchored, loss_budget) — Tier 1
        alt denied (kill switch, breaker, concurrent_limit in SPREADS, size rail, daily_loss_budget_exceeded, side_imbalance_exceeded, stale data)
            Gate-->>SigLoop: allowed=false — candidate abandoned
        else allowed and submit is None (SHADOW or MONITOR-ONLY)
            SigLoop->>Audit: record("shadow.would_submit") — shadow only
        else allowed and submit is EntryExecutor.submit
            SigLoop->>Entry: submit(signal, proposal, risk)

            rect rgb(232, 245, 233)
            note over Entry,Guard: SOFT region — nothing sent yet.<br/>An unexpected exception ends today's ENTRIES only (EntriesEnded).
            Entry->>KS: engaged? — closes the gate-to-submit race
            alt tripped since the gate passed
                Entry->>Audit: record("entry.halted") — Tier 1
            else entries already ended today
                Entry->>Audit: telemetry("entry.entries_ended_today") — Tier 2
            else candidate already attempted this process
                Entry->>Audit: telemetry("entry.already_attempted") — Tier 2, the steady state
            else first attempt
                Entry->>Entry: build_order(OPEN) — one multi-leg limit, floored mid, width-capped
                alt StaleSignalError or unpriceable
                    Entry->>Audit: record("entry.not_built") — Tier 1, session continues
                else built
                    Entry->>Entry: _attempted.add(candidate) — claimed BEFORE anything can be sent
                    loop burst of 3, each under execution.guard_timeout_s
                        Entry->>Guard: has_conflicting_order(external_identifier)
                        Guard->>Stack: get_live_orders()
                        Stack->>TT: GET live orders
                    end
                    alt burst spent, streak below the max
                        Entry->>Audit: record("entry.guard_unreachable_abandoned") — idea retired, day continues
                    else burst spent, streak at the max
                        Entry->>Entry: _end_entries_today("entry_guard_unreachable")
                        Entry->>Audit: record("entry.aborted_day") and record("session.entries_ended")
                        note right of Entry: EntriesEnded latch — NOT the session stop event.<br/>Watchdog, exits and the 15:30 flatten keep running.
                    else blocked
                        Guard->>Audit: record("order.duplicate_blocked") — Tier 1
                    else clear
                        Entry->>Entry: streak reset, go on
                    end
                end
            end
            end

            opt clear, and live submission (dry_run_submissions = false)
                Entry->>Entry: _drift_check_for(signal) — reference fixed at gate time

                rect rgb(255, 235, 238)
                note over Entry,TT: HARD region (_walk_and_report) — an order may exist.<br/>Any exception → Application._halt_process at HaltGrade.ALL.
                Entry->>Walk: run(order, guard_first_submission=False)
                Walk->>Audit: record("walk.started", drift and halt_check armed) — Tier 1
                Walk->>Stack: submit(order, dry_run=False)
                Stack->>Audit: record("order.submit_attempt") — Tier 1, BEFORE the wire
                Stack->>TT: place_order
                TT-->>Stack: OrderAck (broker_order_id, status)
                Stack->>Stack: store.record_order(ack.order) — ledger row
                Stack->>Audit: record("order.submitted")

                loop until resolved
                    Walk->>Stack: get_live_orders() — OrderView.by_id
                    alt not yet Live
                        Walk->>Walk: poll until working_timeout_s, else NEVER_WORKED + cancel
                    else Live
                        Walk->>Walk: hold max(cadence_s, COA settle window)
                        Walk->>KS: halt_check() at the cycle boundary — ENTRY walks only
                        Walk->>Walk: _resolve(observed) — a fill in this cycle wins
                        alt still working
                            Walk->>Entry: drift_check() — spot vs gate-time underlying
                            alt drifted past entry_drift_abort_points, or spot unreadable
                                Walk->>Stack: cancel → DRIFTED
                            else next rung past the walk-away floor
                                Walk->>Stack: cancel → WALKED_AWAY
                            else within budget
                                Walk->>Stack: replace(order at next rung) — mints a NEW id
                            end
                        end
                    end
                end
                Walk->>Walk: _finish — non-fill verdict? ask by_identifier once for a FILLED rung
                Walk->>Resid: register(order) if the last observation was still working
                Walk->>Audit: record("walk.finished") — Tier 1
                Walk-->>Entry: WalkResult(outcome, steps, filled_contracts, reject_reason, mutation_error)

                Entry->>Entry: _report — every WalkOutcome handled explicitly
                alt FILLED
                    Entry->>WD: request_refresh() — books NOTHING
                else REJECTED, NEVER_WORKED, CANCELLED_ELSEWHERE, UNRESOLVED, NO_ORDER_EXISTS
                    Entry->>Entry: alert — the broker's reason rides in the payload
                else WALKED_AWAY, DRIFTED, BLOCKED, ABORTED
                    Entry->>Entry: log only — expected no-trade endings
                end
                Entry->>Audit: record("entry.finished", outcome + drift observation) — Tier 1
                end
            end
        end
    end

    note over Stream,WD: INDEPENDENT PATH — the position is booked here, not by the walk.
    TT-->>Stream: PlacedOrder event (push)
    Stream->>Fills: on_order(order)
    Fills->>Fills: map_placed_order — direction from the MINTED identifier, never the leg labels
    Fills->>Fills: store.apply(order) — cumulative remaining_quantity delta per leg
    alt position moved
        Fills->>Audit: record("order.fill") per leg — Tier 1, AFTER the store commit
        Fills->>WD: note_fills(applied), then request_refresh()
    else status change only
        Fills->>Audit: telemetry("order.event") — Tier 1 on the first terminal sighting of an id
    end
```

**Source anchors:** `orchestrator.py::SignalLoop.evaluate_once` / `_choose`,
`risk/side_balance.py::rank` / `admissible`, `risk/loss_budget.py::contracts_within_budget`,
`signal/implied.py::selection_delta`, `risk/gate.py::PreTradeGate.evaluate`,
`entry.py::EntryExecutor.submit` (soft region), `entry.py::_check_for_duplicate`,
`entry.py::_guard_unreachable`, `entry.py::_end_entries_today`, `entry.py::_walk_and_report`
(hard region), `loop.py::PriceWalkingLoop.run` / `_walk` / `_finish` / `_register_residual`,
`audited.py::AuditedOrderExecution.submit`, `fills.py::FillConsumer.consume`,
`session.py::run_session.on_order`.

> **Unverified:** the mid-implied delta source (`signal.short_delta_source = "mid"`,
> deployed v0.4.6, 2026-09-21) has run no live session as of `f3f0211`. The pre-deploy
> replay gate (`tools.replay_selection`) passed on 10,840 recorded live legs; the first
> live tick on it is unread. → 00-index.md open item 17.

---

## 2. Exit — the position watchdog and the price-walking close

Spec §4 makes this a task independent of the signal loop: "gamma risk in the final hour
doesn't care whether entry logic is running". Each pass (every `[watchdog] cadence_s`):

1. **Refresh, when due** (`refresh_interval_s`, or on request after a fill): rebuild the
   watch list from the ledger per TRADE IDEA (`store.open_spreads`, expiry-aware), poll
   working orders for the gate's concurrency count (P4) and step the residual recheck on the
   same answer (P5), check that the broker's leg count agrees with the ledger, and report
   anything `unattributed`/`malformed`.
2. **Evaluate every held spread — including one whose exit is spent.** FOUR triggers,
   independently: `credit_multiple` (on the REALIZED entry credit, with an observable-margin
   comparison — **inert while `watchdog.credit_trigger_enabled = false`, live and cert since
   v0.5.2, 2026-09-26**), `underlying_near_short_strike` (the sole emergency exit under the
   strike-touch stop, `underlying_buffer_points = 0`), `flatten_before_close` (a clock:
   `watchdog.flatten_before_close_s` before THIS day's close, half-day aware — since v0.5.2
   this can be DEFERRED by `execution/flatten_deferral.py::decide` on a calibrated insurance
   test, never invented, only withheld), and `profit_target` (since v0.5.0, 2026-09-23: the net
   mid rounds to exactly one tick — evaluated always, ACTED on only while
   `watchdog.profit_target_enabled`, currently OFF in all three profiles). The three EMERGENCY
   triggers are debounced by `SustainTracker` (the flatten fires on first observation, unless
   deferred); the profit target's own episode ends only when its condition clears.
3. **Act** on a sustained trigger by handing an exit to its own task, unless the position's
   emergency attempt is spent — where the flatten, once past its mark, and a partial close's
   STUB (`_maybe_stub_close`, since v0.5.1/v0.5.2), each get one further send through the
   guard.

Five things here are exit-specific. The guard **is** run by the loop
(`guard_first_submission=True`), in a bounded burst whose failure becomes `NOT_ATTEMPTED`
rather than a halt (2026-09-03). A `WALKED_AWAY` walk **escalates** to one market-crossing
limit, only after the broker **positively confirms** the abandoned rung is terminal. Two
outcomes — `NOTHING_AT_BROKER` and `CROSS_UNPRICEABLE` — retry through the guard under a
3-send budget. A **profit take never crosses** — `_settle_profit_take` confirms dead and
retries under its OWN 2-send budget, spending nothing on the emergency side unless it fills.
And **nothing on this path consults the kill switch**.

```mermaid
%%{init: {"sequence": {"useMaxWidth": false}}}%%
sequenceDiagram
    autonumber
    participant WD as PositionWatchdog
    participant Store as SqliteOrderStore
    participant Resid as ResidualOrderRecheck
    participant LQ as LegQuotes
    participant Sus as SustainTracker
    participant Exit as ExitExecutor
    participant Walk as PriceWalkingLoop<br/>(WalkPolicy.for_exit)
    participant Guard as DuplicateOrderGuard
    participant Stack as Audited → Throttled → Tastytrade
    participant TT as tastytrade /orders
    participant Audit as AuditLog
    participant App as Application._halt_process

    loop every [watchdog] cadence_s — run()
        opt refresh due or requested
            WD->>Store: open_spreads(today) — per trade idea, via AuditLog.read
            Store-->>WD: OpenSpreadScan(spreads, unattributed, malformed)
            WD->>Stack: get_live_orders() — failure is swallowed, last count stands
            WD->>WD: working_open_spread_count (P4, feeds the gate)
            WD->>Resid: step(live orders) — re-cancel OPENING residuals, page either kind
            WD->>WD: _check_broker_agreement — confirm by REST before paging held_but_unbooked
            WD->>Audit: record("watchdog.unwatchable") once per episode, if any
        end

        loop each held spread not already exiting
            WD->>LQ: resolve(short and long OCC symbols), spot()
            WD->>WD: evaluate_triggers — credit (if enabled), underlying, flatten, profit_target
            WD->>Audit: record("watchdog.unevaluable") on the rising edge only
            WD->>Sus: observe — unknown never clears a window, flatten bypasses it
            opt flatten fired and this position's attempt is not yet spent
                WD->>WD: flatten_deferral.decide(z, IV/RV, width, k) — pure
                note right of WD: any missing/stale input REFUSES to defer — runs as before
            end
            alt exit attempt already SPENT
                WD->>Audit: telemetry("watchdog.check_spent", mark) — still priced every tick
                WD->>Audit: record("watchdog.exit_spent_still_held") — REPEATING page
                opt FILLED close, ledger holds fewer contracts than the close was sized for
                    WD->>WD: _maybe_stub_close — one further close on the origin's ladder
                    WD->>Audit: record("watchdog.stub_close") on first handoff, Tier 2 after
                end
                opt flatten mark passed, stub unspent-or-absent, override unused
                    WD->>Audit: record("watchdog.flatten_override") — once per position
                    note right of WD: falls through to the trigger below — ONE further send
                end
            else not sustained
                WD->>Audit: telemetry("watchdog.check") — Tier 2, the common case
            end
            opt an emergency reason sustained, the flatten fired (and not deferred), or profit_target sustained
                WD->>Audit: record("watchdog.triggered") on the rising edge — Tier 1 + page (emergency only)
                alt legs unpriceable
                    WD->>Audit: record("watchdog.exit_unpriceable") — NOT an attempt, retried next tick
                else priceable
                    WD->>WD: claim the flatten override if the flatten fired — emergency reasons always precede profit_target
                    WD->>Exit: create_task(close(spread, market, reasons))
                end
            end
        end
        WD->>WD: _settle_pass — any caught failure this pass? see the note below
    end

    note over Exit,TT: The exit runs as its OWN task, so watching does not stop for its duration.
    Exit->>Exit: _build_exit_order — build_order(CLOSE) on fresh quotes
    alt unbuildable (stale, rounds to zero) or candidate mismatch
        Exit-->>WD: ExitReport(NOT_ATTEMPTED) — keeps the attempt, gives the flatten override back
    else built
        Exit->>Audit: record("watchdog.exit_started", crossing_debit, scan_age_s) — Tier 1
        Exit->>Walk: run(order, guard_first_submission=True) — NO halt_check
        Walk->>Guard: burst — has_conflicting_order("argus-close-...")
        alt burst spent — GuardUnreachable
            Exit-->>WD: ExitReport(NOT_ATTEMPTED) — nothing sent, next tick asks again
        else a close already in flight
            Guard-->>Walk: BLOCKED
        else clear
            Walk->>Stack: submit, hold, observe, replace — exit cadence, min-steps floor, width-capped
            Stack->>TT: place, replace, cancel
        end
        Walk-->>Exit: WalkResult — _finish already asked for a FILLED rung

        alt FILLED
            Exit-->>WD: ExitReport(FILLED)
        else profit_target only, and the walk found no fill (rung floors at PROFIT_TARGET_MARK, never crosses)
            Exit->>Stack: _await_settled — the one positive confirmation this branch acts on
            alt any rung FILLED after all
                Exit-->>WD: ExitReport(FILLED_LATE)
            else order confirmed terminal, nothing filled
                Exit-->>WD: ExitReport(UNFILLED) — retryable, profit-only budget 2, emergency attempt untouched
            else not confirmed within exit_cancel_settle_s
                Exit-->>WD: ExitReport(NOT_RESOLVED) — retires the take, emergency exit still has its attempt
            end
        else NO_ORDER_EXISTS
            Exit-->>WD: ExitReport(NOTHING_AT_BROKER) — retryable, send budget 3
        else REJECTED, NEVER_WORKED, CANCELLED_ELSEWHERE, UNRESOLVED, BLOCKED, ABORTED
            Exit-->>WD: ExitReport(NOT_RESOLVED) — never answered by paying more
        else WALKED_AWAY, emergency reason present → escalate
            Exit->>Stack: _await_settled — get_live_orders until exit_cancel_settle_s
            alt any rung FILLED (identifier-wide)
                Exit-->>WD: ExitReport(FILLED_LATE)
            else walk.broker_order_id working, absent, partial or unreadable
                Exit-->>WD: ExitReport(CROSS_REFUSED) — refuses to stack a second close
            else broker NAMED that id and it is terminal
                Exit->>Exit: _cross_limit — fresh natural ask or bid, capped at WIDTH
                alt no usable price
                    Exit-->>WD: ExitReport(CROSS_UNPRICEABLE) — retryable, same budget
                else priced
                    Exit->>Stack: submit(order.with_limit_price(-limit))
                    Stack->>TT: place_order — a crossing LIMIT, never a market order
                    Exit->>Audit: record("watchdog.crossed") — Tier 1 + page
                    Exit-->>WD: ExitReport(CROSSED)
                end
            end
        end
        Exit->>Audit: record("watchdog.exit_finished", reject_reason, mutation_error)
    end
    alt NOT_ATTEMPTED
        WD->>Audit: record("watchdog.exit_unattempted") on the edge, bounded repeat after
    else retryable and sends below 3
        WD->>Audit: record("watchdog.exit_nothing_sent" or "exit_cross_unpriceable")
    else anything else
        WD->>WD: _attempted[key] = outcome — SPENT, one attempt per position per process
    end
    WD->>WD: request_refresh()

    note over WD,App: DEGRADE, DON'T DIE (2026-09-10) — a caught exception in the refresh,<br/>a position or an exit task is scoped to that frame. On the pass's RISING edge:
    WD->>Audit: record("watchdog.degraded") + page
    WD->>App: _halt_entries — kill switch at HaltGrade.ENTRIES, NO request_stop
    note over WD,App: The watchdog keeps watching, exiting and flattening.<br/>Only AuditWriteError takes the old path — _halt_on_exception at ALL.
```

**Source anchors:** `watchdog.py::PositionWatchdog.run` / `check_once` / `_check_one` /
`_refresh_if_due` / `_refresh_working_orders` / `_check_broker_agreement` /
`_flatten_may_act` / `_maybe_stub_close` / `_apply_flatten_deferral` / `_trigger` /
`_run_exit` / `_settle_pass` / `_halt_entries`, `watchdog.py::evaluate_triggers`,
`watchdog.py::at_profit_target`, `watchdog.py::SustainTracker`,
`watchdog.py::ExitExecutor.close` / `_after_walk` / `_cross` / `_settle_profit_take` /
`_await_settled` / `_cross_limit`, `execution/flatten_deferral.py::decide`,
`guard.py::CloseQuantity`, `residual_orders.py::ResidualOrderRecheck.step`.

> **Unverified:** the `profit_target` branch and the stub-close branch are drawn from
> source and their unit tests; neither has fired in a live session as of `f3f0211`
> (2026-09-27) — `profit_target_enabled` is `false` in every profile, and the stub
> precondition has never been observed live. → 00-index.md open items 15–16.

---

## 3. Kill switch, resume, and the internal halts

The kill switch is the operator's rail; `Application._halt_process` is the system's. They
share `KillSwitch.engage` and nothing else, and the difference is the design:

| A phone `/kill`… | …and why |
| --- | --- |
| **Denies new entries at the gate**, immediately | `PreTradeGate.evaluate` reads `engaged`. |
| **Refuses a candidate the gate already approved** | `EntryExecutor.submit` re-checks; records `entry.halted`. |
| **Abandons an ENTRY walk in flight and cancels its order** | The walk's `halt_check`, at the cycle boundary, bounded by one `cycle_wait_s`. `ABORTED` never adds a broker round trip. |
| **Leaves the cancel's outcome to P5** | If the cancel fails or sticks, the order is a registered residual and is re-cancelled until terminal. |
| **Alerts Slack, once, for every trip** | Inside `KillSwitch.engage`, the one funnel every trip source passes through. |
| **Survives a restart** | Rebuilt from the audit log at startup (`risk/kill_state.py`); no expiry, no date bound. |
| **Does NOT touch exits, the watchdog, the flatten, or the process** | `ExitExecutor` holds no kill-switch reference. `/status` stays up. |

`POST /resume?confirm=yes` is the only way back, and it is the only route in the system that
*enables* risk — so it records before it flips, and reports honestly when a `HaltRecord`
means the process will not trade again until restarted anyway.

```mermaid
%%{init: {"sequence": {"useMaxWidth": false}}}%%
sequenceDiagram
    autonumber
    actor Phone as Operator's phone<br/>(iOS Shortcut over Tailscale)
    participant Ctl as ControlServer<br/>(control.py)
    participant KS as KillSwitch
    participant Audit as AuditLog
    participant Slack as SlackNotifier
    participant Gate as PreTradeGate
    participant Entry as EntryExecutor
    participant Walk as PriceWalkingLoop (entry)
    participant TT as tastytrade
    participant Resid as ResidualOrderRecheck
    participant App as Application
    participant WD as Watchdog + exits

    rect rgb(232, 240, 254)
    note over Phone,WD: PATH A — POST /kill. Stops NEW RISK. Exits, watchdog and process untouched.
    Phone->>Ctl: POST /kill with Authorization Bearer token
    Ctl->>Ctl: _authorized — hmac.compare_digest, BEFORE any routing
    alt bad or missing token
        Ctl->>Audit: telemetry("control.auth_rejected", peer and route — never the token)
        Ctl-->>Phone: 401
    else already engaged
        Ctl-->>Phone: 200 status already_tripped
    else first trip
        Ctl->>KS: engage(reason, source="http_endpoint")
        KS->>KS: _engaged = True — FIRST, the halt never waits on I/O
        KS->>Slack: alert("kill_switch_engaged")
        KS->>Audit: record("kill_switch.trip") — Tier 1, awaited
        alt trip write failed
            Ctl-->>Phone: 500 status tripped, audit write_failed — halt stands
        else durable
            Ctl-->>Phone: 200 status tripped
        end
    end
    Gate->>KS: engaged? — every later candidate DENIED
    Entry->>KS: engaged? — a gate-approved candidate refused, entry.halted
    Walk->>KS: halt_check() at the next cycle boundary
    Walk->>TT: cancel(broker_order_id) — best effort
    Walk->>Resid: register if the order was still working
    Walk->>Audit: record("walk.finished", ABORTED, reason kill switch engaged)
    note over WD: NOT affected — keeps watching, exiting and flattening.
    end

    rect rgb(240, 250, 240)
    note over Phone,WD: PATH B — POST /resume?confirm=yes. The ONE clear. Records BEFORE it flips.
    Phone->>Ctl: POST /resume?confirm=yes
    alt confirm missing, repeated or not exactly yes
        Ctl-->>Phone: 400
    else nothing engaged
        Ctl-->>Phone: 200 status not_engaged — NO resume row written
    else engaged
        Ctl->>App: halt_state() — is a HaltRecord latched?
        Ctl->>KS: disengage(source, detail incl. restart_required)
        KS->>Audit: record("kill_switch.resume") — if this fails, still ENGAGED, 500
        KS->>KS: _engaged = False
        KS->>Slack: alert("kill_switch_disengaged")
        Ctl-->>Phone: 200 status resumed, restart_required, halt_grade
    end
    end

    rect rgb(255, 243, 224)
    note over App,WD: PATH C — Application._halt_process. The system's halt, GRADED.
    App->>App: _halt_process(reason, source, grade)
    App->>KS: engage(reason, source, detail)
    App->>App: _mark_halted — reason first-writer-wins, grade strictest-wins
    App->>Audit: record("process.halted") + alert — once, on the edge
    alt grade ALL — audit failure, post-submit entry failure, broker unreachable at start, rehydrate failure, most mismatches
        App->>App: request_stop() — process stop → scheduler bridge → SESSION stop
        WD->>WD: exits in flight see the session stop and ABORT, the watchdog loop ends
        App->>App: session teardown, then PARK — serving /status and /kill, no sessions
    else grade ENTRIES — watchdog degraded, or an order-only mismatch of our own opening orders
        App->>App: NO request_stop — the scheduler keeps running
        WD->>WD: sessions run MONITOR-ONLY — watch, mark, exit, flatten, no entries
    end
    end
```

**Halt sources and their grades**, as wired today:

| Source (`source=`) | Caller | Grade |
|---|---|---|
| `http_endpoint` | `control.py::_kill` | none — the switch alone, no `HaltRecord` |
| `heartbeat` | `app.py::_trip_on_stall` (config-gated, ships off) | none — the switch alone |
| `audit_log` | `app.py::_halt_on_audit_failure` | `ALL` |
| `reconciliation_mismatch` | `app.py::startup`, `session.py::_reconcile_or_abort` | `reconcile.halt_grade(result)` — `ENTRIES` only for our own working opening orders with no position discrepancy |
| `broker_unreachable` | `app.py::startup` after the `[reconcile]` budget | `ALL` (parks) |
| `day_risk_rehydrate` | `app.py::_run_session_day` | `ALL` |
| `entry_execution_exception` | `entry.py::_walk_and_report` | `ALL` |
| `watchdog_exception` | `watchdog.py::_halt_entries` (degrade) | `ENTRIES` |
| `watchdog_exception` | `watchdog.py::_halt_on_exception` (`AuditWriteError` only) | `ALL` |

**Source anchors:** `control.py::ControlServer._kill` / `_resume` / `_authorized`,
`killswitch.py::KillSwitch.engage` / `disengage`, `risk/kill_state.py::load_kill_switch_state`,
`gate.py::PreTradeGate.evaluate`, `entry.py::EntryExecutor.submit` / `_build_loop`,
`loop.py::_stop_reason` / `_abort`, `app.py::_halt_process` / `_mark_halted` /
`_announce_halt` / `run`, `scheduler.py::_run_one_session` (the stop bridge).
**Tests:** `tests/test_kill_scope.py` drives Path A over a real TCP socket, including the
negative control that an exit walk is untouched; `tests/test_halt_scope.py` covers the grades.

---

## 4. Startup and reconciliation

Spec §6: "on every restart, reconcile broker state before resuming automated logic; refuse
to resume on any mismatch". In Argus this happens **twice** — at process start and at the
start of every trading day — and both sites run one shared sequence
(`reconcile_with_retry` → `reconcile_and_record`).

**The order of startup changed in a way that matters** (2026-08-17): the control surface
binds and `process.start` is recorded **before** the reconciliation rail, and a rail that
refuses **parks** the process rather than exiting it. A process that died on a mismatch
dropped `/status` and `/kill` for exactly the failure an operator most needs to see; on
2026-08-14 that was 34 restarts with the listener never up once. What still refuses to
start is only what makes the surface itself unsafe or impossible: no kill token, a held
writer lock, a failed bind — plus an unknown-state bug, which must still exit non-zero.

```mermaid
%%{init: {"sequence": {"useMaxWidth": false}}}%%
sequenceDiagram
    autonumber
    participant Main as python -m argus
    participant App as Application.startup()
    participant Lock as DatabaseLock
    participant Audit as AuditLog
    participant KState as kill_state
    participant Ctl as ControlServer
    participant SR as session.startup_reconcile
    participant Rec as Reconciler
    participant Store as SqliteOrderStore
    participant TT as tastytrade
    participant Slack as SlackNotifier

    Main->>App: Config.load — ARGUS_ENV names the profile, no default
    App->>App: log "argus build VERSION config PROFILE/FILE @SHA" — first line, always
    App->>App: load_kill_token() — refuses to boot without ARGUS_KILL_TOKEN
    App->>App: build_alert_fn — Slack optional, never refuses startup
    App->>Lock: acquire() — BEFORE opening the DB, refuses if held
    App->>Audit: connect(WAL), apply_migrations, start()
    App->>KState: load_kill_switch_state — newest trip vs newest resume by seq
    App->>App: KillSwitch(initially_engaged=...), audit.on_failure = halt ALL
    App->>App: LoopHeartbeat, SessionScheduler, blackout runway, StatusService
    App->>Ctl: start() — bind the Tailscale address FIRST — a bad bind refuses startup
    App->>Audit: record("process.start", version, kill_endpoint) — Tier 1
    opt a kill-switch trip was never resumed
        App->>Audit: record("kill_switch.restored")
        App->>Slack: alert("kill_switch_restored") — once, no second trip record
    end

    App->>App: _reconcile() — status phase reconciling
    alt shadow
        App->>Audit: record("reconcile.skipped", shadow_no_broker)
    else sandbox or live
        App->>SR: startup_reconcile(config, account_number, stop)
        SR->>TT: open trading session ONCE — a login failure is BrokerUnreachable at once
        SR->>TT: Account.get — bounded retry, then filter by account_number
        alt no matching account
            SR-->>App: SessionAborted — permanent, never retried
        end
        loop reconcile_with_retry — every retry_interval_s within retry_budget_s, interruptible by stop
            SR->>Rec: reconcile_and_record(today, session_settled)
            Rec->>Store: sandbox reset check — sandbox only, clears POSITIONS, never orders
            Rec->>TT: fetch_positions — inner burst of 3
            Rec->>Store: open_positions (expected)
            Rec->>Rec: reconcile_positions — unexpected, missing, quantity
            Rec->>TT: get_live_orders — its own inner burst, LiveOrders incl. unmappable
            Rec->>Store: known_orders(ids)
            Rec->>Rec: reconcile_orders — unknown WORKING, FILLED or UNMAPPABLE, DEAD is fine
            Rec->>Rec: resolve expired positions — own OCC expiry past, or today and settled
            Rec->>Audit: record("reconcile.result", trading_date) — Tier 1
            opt mismatch
                Rec->>Slack: alert("reconciliation_mismatch")
            end
        end
        SR->>TT: close the session — no broker credentials held for the process lifetime
    end

    alt clean
        App->>App: set idle, tune_gc, started
    else ReconciliationError (sandbox or live mismatch)
        App->>App: _halt_process(grade = halt_grade(result)) — PARK, or monitor-only if ENTRIES
    else BrokerUnreachable (budget spent, or stop)
        note right of SR: reconcile_with_retry already wrote reconcile.unreachable<br/>and alerted, once for the whole episode
        App->>App: _halt_process(broker_unreachable, ALL) — PARK
    else SessionAborted (no matching account)
        App->>App: _halt_process(broker_unreachable, ALL) — PARK
    else any other exception
        App->>App: _teardown and re-raise — a bug must exit non-zero
    end
    App-->>Main: run() — scheduler unless halted ALL, else wait for a signal

    note over App,TT: PER TRADING DAY the same sequence runs inside session.run_session, after login:
    note over App,TT: mismatch → halt(reconciliation_mismatch, graded) then SessionAborted, or continue MONITOR-ONLY<br/>unreachable → SessionAborted for today only, no halt (unknown is not known-wrong)
```

**Source anchors:** `app.py::Application.startup` / `_reconcile` / `_apply_reconcile_verdict` /
`_announce_restored_kill` / `run`, `session.py::startup_reconcile` /
`_reconcile_or_abort`, `reconcile.py::reconcile_with_retry` / `reconcile_and_record` /
`Reconciler.reconcile` / `reconcile_orders` / `_clear_ledger_if_reset` /
`_resolve_expired_positions` / `mismatch_halts_process` / `halt_grade`,
`risk/kill_state.py::load_kill_switch_state`.
