# UML state diagrams — orders, positions and the system

**Audience:** engineers implementing or debugging these flows.

Four things have a genuine state machine in Argus, and they are not the same machine:

1. **An order** has the state tastytrade says it has. Argus observes it and drives only
   three transitions (submit, replace, cancel).
2. **A walk** is Argus's own machine layered on one trade idea's order(s). It decides what
   to do about each observation and ends in one of ten explicit outcomes.
3. **A held position** has an exit lifecycle in the watchdog — watched, triggered, exiting,
   retried, spent — and that lifecycle, not the order's, is what decides whether anything
   automated can still close it.
4. **The system** has operating states (starting, serving, halted and parked, monitor-only)
   plus a set of entry-permission overlays that are flags, not phases.

Keeping them apart matters because most of the interesting logic is one machine reacting to
another, and the classification bridging orders to walks (`classify_order`) is deliberately
three-valued rather than "terminal / not terminal".

---

## 1a. Order lifecycle — the broker's states

`OrderStatus` (`execution/models.py`) mirrors tastytrade's enum value-for-value;
`test_order_status_mirrors_the_sdk_exactly` asserts the value sets are **equal**, so an SDK
that gains a status breaks CI rather than falling through a mapping on a live order. An order
whose status cannot be mapped at all is not dropped: `get_live_orders()` returns it in
`LiveOrders.unmappable`, and reconciliation halts on it.

Only three transitions are initiated by Argus (marked **(argus)**). Everything else is the
exchange or the broker acting, and Argus learns about it by polling `get_live_orders()` or by
an `AlertStreamer` push.

```mermaid
%%{init: {"state": {"useMaxWidth": false}}}%%
stateDiagram-v2
    direction LR

    state "Received" as RECEIVED
    state "Routed" as ROUTED
    state "In Flight" as INFLIGHT
    state "Contingent" as CONTINGENT
    state "Live" as LIVE
    state "Replace Requested" as REPLREQ
    state "Cancel Requested" as CANCREQ
    state "Partially Removed" as PARTREM

    state "Filled" as FILLED
    state "Cancelled" as CANCELLED
    state "Rejected" as REJECTED
    state "Expired" as EXPIRED
    state "Removed" as REMOVED

    [*] --> RECEIVED : submit via place_order (argus)
    RECEIVED --> REJECTED : broker refuses, reject_reason carried
    RECEIVED --> LIVE : reaches the exchange
    RECEIVED --> ROUTED : on its way to the exchange
    ROUTED --> LIVE : working at the exchange
    INFLIGHT --> LIVE : in transit then working
    CONTINGENT --> LIVE : condition met

    LIVE --> FILLED : execution, possibly in partial steps
    LIVE --> PARTREM : part of the order removed
    PARTREM --> FILLED : remainder fills
    PARTREM --> CANCELLED : remainder cancelled
    LIVE --> EXPIRED : end of session
    LIVE --> REPLREQ : replace to reprice (argus)
    LIVE --> CANCREQ : cancel to abandon (argus)
    REPLREQ --> CANCELLED : old order superseded, NEW id minted
    REPLREQ --> FILLED : the original fills first, replace refused
    CANCREQ --> CANCELLED : cancel takes effect
    CANCREQ --> FILLED : a fill crosses the cancel
    LIVE --> REMOVED : removed by the broker

    FILLED --> [*]
    CANCELLED --> [*]
    REJECTED --> [*]
    EXPIRED --> [*]
    REMOVED --> [*]

    note right of RECEIVED
      MEASURED against sandbox 2026-07-29 — a pre-market submit
      is accepted, carries tif.next_valid_session and SITS here.
      Received does NOT mean in the market.
    end note

    note right of LIVE
      CONFIRMED_IN_MARKET holds LIVE and nothing else.
      The walk's cadence clock starts only here.
    end note

    note right of PARTREM
      Deliberately NOT terminal (is_terminal excludes it).
      Fails toward "still out there".
    end note
```

What has been **observed** on real orders, as opposed to drawn from the enum:
`Received → Live → Filled`; `Routed → Rejected` inside a second (2026-08-27, cert's
"Session offline"); a replace minting a new id and leaving the old one `Cancelled`; a
replacement **refused** with *"Received a fill on the original order"* while the original
filled (2026-09-01 — drawn as `REPLREQ → FILLED`, though whether the original's status passed
through `Replace Requested` first was not recorded); a replace of a partially
filled order carrying the **remainder** (2026-09-01); and a `Cancel Requested` going
terminal inside 1.5s.

> **Unverified:** the transitions *into* `ROUTED`, `IN_FLIGHT` and `CONTINGENT`, and their
> exact meanings at tastytrade, have not been observed during regular hours, which is why
> none of them is in `CONFIRMED_IN_MARKET`. An order that **stays** at `Cancel Requested`
> has never been observed either.

### The disposition overlay — what a status *means* to a caller

`classify_order` (`execution/models.py`) is the one classification, shared by the duplicate
guard, order reconciliation, the walk's identifier probe and the exit's settle check, so
they cannot drift.

```mermaid
%%{init: {"state": {"useMaxWidth": false}}}%%
stateDiagram-v2
    direction LR
    state "Any observed order" as ANY

    state "WORKING" as W
    state "FILLED" as F
    state "DEAD" as D

    ANY --> W : status is None, or is_working — includes Partially Removed
    ANY --> F : status is Filled
    ANY --> F : terminal AND any leg reports a fill — e.g. a 3-lot that filled 1 then cancelled
    ANY --> F : terminal AND per-leg quantities were never reported — fail closed
    ANY --> D : terminal AND every leg reports zero filled

    note right of F
      Blocks a resubmission — a real position exists.
    end note
    note right of D
      Deliberately UNBLOCKED. Re-entering after a
      cancelled attempt is what crash recovery needs.
    end note
```

---

## 1b. The walk — Argus's own machine over one trade idea

`PriceWalkingLoop.run` (`execution/loop.py`) is one state machine used for **both** entry and
exit, parameterized by a `WalkPolicy`. There is no direction branch anywhere in it:
tastytrade's sign convention (positive limit for a credit, negative for a debit) makes
"concede toward the market" a subtraction in both cases. The ladder is arithmetic on the
opening limit — never a re-quote — floored at a percentage (with `exit_walk_min_steps` for
exits) and clamped at the strike width.

Three properties the diagram encodes that the first revision did not:

- **The guard is asked before `walk.started`**, in a bounded burst. A spent burst raises a
  typed `GuardUnreachable` out of `run()` — it is **not an outcome**; the caller sends
  nothing and decides (entry: retire the idea; exit: `NOT_ATTEMPTED`).
- **Every non-fill verdict is probed** (`_finish`, 2026-09-10): before a walk may report
  that a trade idea did not fill, it asks `by_identifier` once, and a FILLED rung anywhere
  under the identifier turns the verdict into `FILLED`. `BLOCKED` and `ABORTED` never ask —
  the latter because a halt must not add a broker round trip. Three sites pass
  `probe=False` because they have just read that exact answer.
- **Every exit from the machine registers a residual** if the last observation of the order
  was still working, so a failed or stuck cancel is rechecked by P5 rather than forgotten.

```mermaid
%%{init: {"state": {"useMaxWidth": false}}}%%
stateDiagram-v2
    state "Guard burst (only if guard_first_submission)" as GUARD
    state "Submitting" as SUB
    state "Awaiting Live" as AWAIT
    state "Holding one COA cycle" as HOLD
    state "Observing" as OBS
    state "Drift check (entry only)" as DRIFT
    state "Repricing" as REPRICE
    state "Recovering by identifier" as RECOVER
    state "Identifier probe in _finish" as PROBE

    state "FILLED" as O_FILL
    state "BLOCKED" as O_BLOCK
    state "ABORTED" as O_ABORT
    state "UNRESOLVED (no probe)" as O_UNRES_NP
    state "NO_ORDER_EXISTS" as O_NOE
    state "Non-fill verdict as proposed" as NONFILL

    [*] --> GUARD : run(order)
    GUARD --> [*] : burst spent — raises GuardUnreachable, nothing sent
    GUARD --> O_BLOCK : a conflicting order exists at the broker
    GUARD --> SUB : clear, or the caller already asked — walk.started recorded

    SUB --> PROBE : ack is Rejected — proposes REJECTED
    SUB --> RECOVER : submit raised — outcome unknown
    SUB --> AWAIT : ack carries a broker order id

    AWAIT --> AWAIT : not yet Live — poll every status_poll_interval_s
    AWAIT --> HOLD : Live, or a fill already reported
    AWAIT --> PROBE : seen, never Live by working_timeout_s — cancel, proposes NEVER_WORKED
    AWAIT --> PROBE : never visible at all — proposes UNRESOLVED
    AWAIT --> O_ABORT : session stop, or kill switch via halt_check — cancel

    HOLD --> OBS : waited max(cadence_s, COA settle)
    HOLD --> O_ABORT : session stop, or kill switch via halt_check — cancel

    OBS --> O_FILL : terminal and FILLED, or partial on a working order — remainder cancelled
    OBS --> PROBE : terminal Rejected — proposes REJECTED
    OBS --> PROBE : terminal, nothing filled, not ours — proposes CANCELLED_ELSEWHERE
    OBS --> AWAIT : absent from the answer — blind cycle, no reprice
    OBS --> PROBE : max_recovery_attempts blind cycles — proposes UNRESOLVED
    OBS --> DRIFT : still working

    DRIFT --> PROBE : moved past entry_drift_abort_points, or spot unreadable — cancel, proposes DRIFTED
    DRIFT --> REPRICE : reference holds, or no check armed

    REPRICE --> PROBE : next rung past the floor — cancel, proposes WALKED_AWAY
    REPRICE --> AWAIT : replace accepted — new broker id
    REPRICE --> RECOVER : replace raised

    RECOVER --> O_FILL : a filled order under the identifier
    RECOVER --> AWAIT : exactly one working — resume at ITS limit price
    RECOVER --> O_UNRES_NP : two working under one identifier — all cancelled
    RECOVER --> O_UNRES_NP : broker unreadable after max_recovery_attempts
    RECOVER --> O_NOE : broker answered, nothing exists — mutation_error kept

    PROBE --> O_FILL : a rung filled after all
    PROBE --> NONFILL : nothing filled, or the probe failed — the proposed verdict stands

    O_FILL --> [*]
    O_BLOCK --> [*]
    O_ABORT --> [*]
    O_UNRES_NP --> [*]
    O_NOE --> [*]
    NONFILL --> [*]

    note right of PROBE
      ONE by_identifier read, no retry. FILLED only.
      A failed read falls back to the proposed verdict
      rather than inventing UNRESOLVED.
    end note
    note right of O_NOE
      The broker WAS asked and had nothing. Opposite of
      UNRESOLVED. The exit path may retry it through the guard.
    end note
    note right of NONFILL
      One of WALKED_AWAY, DRIFTED, REJECTED, NEVER_WORKED,
      CANCELLED_ELSEWHERE, UNRESOLVED. An EXIT that walked
      away escalates to the cross, see section 2.
    end note
```

**Every terminal state is a report, not a guarantee.** A cancel request and a fill can
cross, so `positions` (written by `FillConsumer`) and restart-time reconciliation stay
authoritative about what is held. `WalkResult.filled_contracts` is scoped to one rung;
`positions` is what says what a trade idea filled.

---

## 2. A held position's exit lifecycle — the watchdog's view

This is the machine that decides whether anything automated can still close a position, and
it is where most live incidents have been. The rule it enforces is **one EMERGENCY exit
attempt per position per process** (`_attempted`, never pruned), because a second closing
order on top of one that may still fill closes the spread twice — re-opening it inverted on
expiry day. The exceptions are narrow and each is licensed by `DuplicateOrderGuard` re-asking
the broker, never by elapsed time.

Since v0.5.0 → v0.5.3 (2026-09-23 → 2026-09-27) this machine gained a **non-emergency**
trigger with its own budget (`PROFIT_TARGET`), a **stub continuation** for a partial close
(`_maybe_stub_close`), and a **deferral seam** that can withhold the clock trigger
(`flatten_deferral.py`). None of the three touches the one-emergency-attempt rule: a profit
take that does not fill spends nothing (`_attempted`/`_sends`/`_flatten_attempted` untouched,
AST-pinned), a stub close spends its OWN allowance (`_stub_spent`), and a deferred flatten is
simply a trigger that has not fired yet.

```mermaid
%%{init: {"state": {"useMaxWidth": false}}}%%
stateDiagram-v2
    state "Unwatchable — held, no watchable spread" as UNW
    state "Watching" as WATCH
    state "Sustaining — true, under sustained_s" as SUST
    state "Deferred — clock trigger withheld" as DEFER
    state "Triggered" as TRIG
    state "Unpriceable — no order can be built" as UNPR
    state "Exiting — exit task in flight" as EXIT
    state "Retry pending — emergency sends below 3" as RETRY
    state "Retry pending — profit-take sends below 2" as PRETRY
    state "Spent — evaluated, never acted on" as SPENT
    state "Flatten override — one send" as FOVR
    state "Stub — booked fewer than the close was sized for" as STUB
    state "Gone from the ledger" as GONE

    [*] --> WATCH : fill booked, open_spreads builds it
    [*] --> UNW : unattributed or malformed — red on /status, no stop, no flatten

    WATCH --> SUST : credit_multiple (if credit_trigger_enabled) or underlying trigger observed true
    SUST --> WATCH : condition goes false — the window is deleted
    SUST --> TRIG : held for sustained_s — unknown readings do not reset the clock
    WATCH --> TRIG : PROFIT_TARGET — mid rounds to one tick, evaluated every tick regardless of profit_target_enabled
    WATCH --> DEFER : FLATTEN_BEFORE_CLOSE fires AND flatten_deferral decide() says defer
    DEFER --> TRIG : the tick is no longer withheld — insurance test flips, or an EMERGENCY reason joins
    WATCH --> TRIG : FLATTEN_BEFORE_CLOSE — a clock, no debounce, when not deferred

    TRIG --> UNPR : legs cannot be priced
    UNPR --> TRIG : quotes return — NOT counted as an attempt
    TRIG --> EXIT : ExitExecutor.close as its own task — an EMERGENCY reason always takes precedence over PROFIT_TARGET alone

    EXIT --> TRIG : NOT_ATTEMPTED — unbuildable, or guard unreachable, nothing sent
    EXIT --> RETRY : emergency NOTHING_AT_BROKER or CROSS_UNPRICEABLE
    RETRY --> EXIT : next tick, through the guard
    EXIT --> PRETRY : profit-only UNFILLED — order confirmed terminal, nothing filled, no cross ever sent
    PRETRY --> EXIT : next tick, through _settle_profit_take
    EXIT --> SPENT : FILLED, FILLED_LATE, CROSSED, CROSS_REFUSED, NOT_RESOLVED
    RETRY --> SPENT : third emergency send spent
    PRETRY --> SPENT : second profit-take send spent, or NOT_RESOLVED (order still live, unconfirmed)
    EXIT --> SPENT : the exit task crashed — spent in memory FIRST

    SPENT --> STUB : FILLED close, ledger still holds fewer contracts than the close was sized for
    STUB --> EXIT : one further close, through the guard's quantity rule (stub_close_enabled)
    STUB --> SPENT : refused — nothing sent, paced by refresh_interval_s unless a new emergency reason joins

    SPENT --> FOVR : past the flatten mark, override unused, stub unspent-or-absent
    FOVR --> EXIT : one further send, through the guard
    FOVR --> SPENT : the override is spent at the send

    WATCH --> GONE : closed by a fill, or expired at settlement
    SPENT --> GONE : the closing fill books, or settlement
    EXIT --> GONE : fill books while the walk is still reporting
    UNW --> GONE : resolved in the ledger
    GONE --> [*]

    note right of SPENT
      Still priced every tick — watchdog.check_spent.
      Pages exit_spent_still_held on a REPEATING bound
      while the ledger says it is held (emergency spend
      only). Nothing automated retries an emergency spend,
      and a broker outage here is eaten to settlement.
    end note
    note right of TRIG
      Rising edge only is Tier 1 plus a page for an
      EMERGENCY reason. Repeats are Tier 2, bounded by
      exit_unattempted_max_s. A PROFIT_TARGET-only episode
      falls when the CONDITION clears, never when a take is
      merely held back.
    end note
    note right of DEFER
      execution/flatten_deferral.py::decide, pure. Every
      missing, stale or unusable input refuses to defer —
      the flatten then runs exactly as before this existed.
      Never withheld for a position whose emergency attempt
      is already spent.
    end note
    note right of STUB
      guard.py::CloseQuantity proves q against the broker's
      own per-leg fills. Never observed live as of v0.5.3
      (2026-09-27) — see docs/architecture/00-index.md open
      item 16.
    end note
```

`ExitOutcome.closed` is true only for `FILLED` and `FILLED_LATE`; `CROSSED` means a crossing
limit was *sent*, and the position leaves the watch list when its fill books. `UNFILLED` exists
only on the profit path — `_settle_profit_take` never crosses, so a profit take that finds no
fill simply reports and the next tick may try again, under its own two-send budget
(`_PROFIT_SEND_ATTEMPTS = 2`) separate from the emergency exit's three
(`_EXIT_SEND_ATTEMPTS`). Every other `ExitOutcome` spends the attempt it was reached under —
`ExitReport` is a report too, and the ledger decides when a position is gone.

> **Unverified:** `PROFIT_TARGET` has fired in no live session as of 2026-09-27 (shipped OFF
> at v0.5.0 and v0.5.2); the `STUB` state, the `DEFER` state, and the strike-touch stop's
> `SUST`/`TRIG` path with `credit_trigger_enabled = false` are all drawn from the source and
> its tests, not from an observed live transition. → 00-index.md open items 14–16.

---

## 3. System operational state

The process has three nested lifetimes: the process itself, the scheduler's day loop, and the
session inside a trading day. The halts sit across all three, and **they no longer end the
process**: since 2026-08-17 a halt parks it (still serving `/status`, `/kill` and `/resume`),
and since 2026-08-29 a halt has a width.

The `SessionPhase` values are the real ones published to `/status` (`status.py`); a halt is
reported by `StatusSnapshot.halted`, which sits above the phase and changes what it means.

```mermaid
%%{init: {"state": {"useMaxWidth": false}}}%%
stateDiagram-v2
    state "Starting" as Starting
    state "Refused to start" as Refused
    state "Serving — /status, /kill, /resume bound" as Serving
    state "Reconciling at start — retried on the reconcile budget" as StartRecon
    state "Parked — halted ALL, no sessions" as Parked
    state "Stopped" as Stopped

    [*] --> Starting

    Starting --> Refused : no ARGUS_KILL_TOKEN
    Starting --> Refused : config refuses — no profile, filename mismatch, live rails
    Starting --> Refused : writer flock held by another process
    Starting --> Refused : control server cannot bind the Tailscale address
    Refused --> [*]

    Starting --> Serving : bound, process.start recorded, kill switch restored if in force
    Serving --> StartRecon : sandbox or live
    Serving --> Running : shadow — reconcile.skipped
    StartRecon --> Running : clean
    StartRecon --> Running : order-only mismatch graded ENTRIES — monitor-only
    StartRecon --> Parked : mismatch graded ALL, broker unreachable, no matching account
    StartRecon --> Stopped : unknown exception — teardown, exit non-zero, Docker restarts

    state Running {
        state "idle" as Idle
        state "logging_in — ban-aware retry" as LoggingIn
        state "reconciling" as Reconciling
        state "running — trading" as Trading
        state "running — MONITOR-ONLY" as Monitor
        state "ended" as Ended
        state "aborted" as Aborted

        [*] --> Idle
        Idle --> LoggingIn : ET session open (MarketCalendar)
        LoggingIn --> Reconciling : both sessions open
        LoggingIn --> Aborted : login attempts exhausted, or credentials missing

        Reconciling --> Trading : clean and not halted
        Reconciling --> Monitor : clean but the process is already halted
        Reconciling --> Monitor : mismatch graded ENTRIES
        Reconciling --> Aborted : broker unreachable — this day only
        Reconciling --> Aborted : shadow mismatch — this day only

        Trading --> Monitor : watchdog degraded — halt ENTRIES, the gate denies from here
        Trading --> Ended : ET close
        Monitor --> Ended : ET close

        Ended --> Idle : to the next trading day
        Aborted --> Idle : process survives
    }

    Running --> Parked : _halt_process at ALL — audit failure, post-submit entry failure, mismatch, rehydrate failure
    Parked --> Stopped : SIGINT or SIGTERM
    Running --> Stopped : SIGINT or SIGTERM
    Stopped --> [*]

    note right of Parked
      Serving, HALTED on /status, repeating a journal line
      every 15 minutes. One process.halted record and one
      page. A restart re-runs the rail, and /resume clears
      only the kill switch, never the HaltRecord.
    end note
```

### Entry-permission overlays

These are not phases — they are flags that decide whether a candidate may become an order
while the system stays in whatever phase it is in. Each has a **different clearing rule**,
which is exactly what `/status` renders them separately for: the operator already knows
entries are off; what they need is what clears it.

```mermaid
%%{init: {"state": {"useMaxWidth": false}}}%%
stateDiagram-v2
    direction LR

    state "Armed — entries permitted" as Armed
    state "Fail-closed — this candidate denied" as FailClosed
    state "Breaker tripped — rest of this trading DATE" as HaltedDay
    state "Entries ended today — EntriesEnded latch" as Ended
    state "Kill switch engaged — durable" as Killed
    state "Halted ENTRIES — rest of this process" as HaltedEnt
    state "Halted ALL — parked" as HaltedAll

    Armed --> FailClosed : stale market or account data, size rail, concurrent_limit in spreads, NLV unreadable, side_imbalance_exceeded, daily_loss_budget_exceeded
    FailClosed --> Armed : the condition clears — evaluated fresh on every gate call

    Armed --> HaltedDay : day P&L breaches risk.daily_max_loss
    HaltedDay --> Armed : next trading date — a same-day restart REHYDRATES the trip

    Armed --> Ended : guard unreachable streak, or an entry build crash
    Ended --> Armed : next trading day — a fresh EntryExecutor, no human step

    Armed --> Killed : POST /kill, or any halt
    Killed --> Armed : POST /resume?confirm=yes — a restart does NOT clear it

    Killed --> HaltedEnt : halt graded ENTRIES
    Killed --> HaltedAll : halt graded ALL
    HaltedEnt --> HaltedAll : a later halt graded ALL — grade only tightens
    HaltedEnt --> [*] : process restart, once the cause is resolved
    HaltedAll --> [*] : process restart, once the cause is resolved

    note right of Ended
      Frozen dataclass, ONE reader. No kill switch, no
      HaltRecord. Watchdog, exits, flatten keep running.
    end note
    note right of Killed
      Engaged iff the newest kill_switch.trip has no later
      kill_switch.resume by seq. Unreadable restores ENGAGED.
      Exits and the watchdog are never gated by it.
    end note
```

> **Verified:** the kill switch has three functional readers, all on the ENTRY side —
> `PreTradeGate.evaluate`, `EntryExecutor.submit`'s pre-build re-check, and the `halt_check`
> the entry executor arms on `PriceWalkingLoop`. `KillSwitch.check()` and `HaltedError`
> still exist but nothing in `src/` calls `check()`. `ExitExecutor` holds no reference to
> the switch (`tests/test_kill_scope.py`), and the residual recheck is deliberately not given
> one either — cancelling an abandoned OPENING order while halted is what the operator asked
> for.
