# Order execution engine — chunk 5: the wiring (spec §7/§10)

*Wiring chunks 1-4 into the running process, the sandbox reset, dry-run mode and the size rails.*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


`execution/entry.py` is the entry path, `execution/pipeline.py` assembles the one
order stack, and the changes in `session.py` / `app.py` / `reconcile.py` are what turn
chunks 1–4 from tested-in-isolation into a running system. Chunk 5 owns no execution
primitive: it builds with chunk 1, checks with chunk 2, walks with chunk 3, watches
with chunk 4. What it genuinely owns is WHEN the pipeline is entered, what happens for
each way a walk can end, and where the failure boundary falls.

Decisions that are settled — do not relitigate:

- **`environment` is the ONLY switch, and shadow is untouched.** `pipeline.trades_for_real`
  is the single boundary (`environment != "shadow"`); everything above the terminal
  action is the same code in every environment. Shadow still passes `submit=None` and
  logs `shadow.would_submit`, so weeks of accumulated candidate data stay comparable,
  and NO code in `entry.py` runs. Every pre-existing shadow test passes unmodified. A
  test asserts by AST that `_build_trading_side` is called exactly once and inside a
  conditional — hoisting it out of that branch is the one edit that would make shadow
  submit.
- **The exception-policy boundary is ONE `try`, not a condition.** Signed off
  explicitly. `EntryExecutor.submit` is the SOFT region and
  `EntryExecutor._walk_and_report` is the HARD region, and the line is the call
  boundary between them so it cannot drift:
    - **Soft** (before any submit): end the DAY. Tier 1 `entry.aborted_day`, alert,
      and set the session `stop` event — the same event the scheduler sets at the
      close, so the body unwinds cleanly and the supervisor idles to the next trading
      day. No kill switch: nothing reached the broker, and a latched halt would also
      cost tomorrow.
    - **Hard** (from the first submit onward): halt the PROCESS via the same
      `Application._halt_process` pairing the audit-failure, reconcile-mismatch and
      watchdog paths use, `source="entry_execution_exception"`. Past that point an
      order may exist, and a live order with nobody walking it is what the watchdog's
      posture exists for. Deferring to the next reconciliation would leave it
      unattended for however long that is.
  `AuditWriteError` is never converted into a soft day-end — it propagates, so a
  broken black box cannot hide behind a tidy-looking session.
- **Not every pre-submission stop is a failure.** A stale signal, a credit with no
  expressible limit, an already-attempted candidate, or a BLOCKED guard verdict are
  normal outcomes of a system that refuses to trade unless everything lines up:
  recorded (`entry.not_built`, Tier 1), candidate abandoned, session continues.
  Conflating those with a fault would mean one 1.1s-stale candidate costs a day.
- **The guard is called in `submit`, and the loop is told `guard_first_submission=False`.**
  False means "already done", not "skipped". Delegating it to the loop with True was
  the first shape of this and it was WRONG: a guard failure propagates out of `run()`
  (chunk 3, deliberately — "I could not check" must not become an outcome), which
  would land it inside the hard-halt region and take the process down for a question
  asked before anything was sent. Running it here puts it on the side of the boundary
  its own semantics call for, and still satisfies both halves of `guard.py`'s rule:
  once per brand-new trade idea, never before a replace (the loop does not check at
  all, so a replace re-sending the same identifier cannot deadlock the loop against
  itself). An unreachable broker at the guard is therefore SOFT — end the day rather
  than keep re-proposing every 5s while blind to what is already working.
- **The entry walk's drift breaker is armed HERE** (follow-on, 2026-07-30), because
  this is where the reference lives. `UnderlyingDriftCheck` compares the CURRENT
  underlying against `SpreadSignal.underlying_price` — the arrival price, carried
  from `ChainSnapshot.underlying_price` the same way and for the same reason
  `captured_at` is — every cycle, FIXED, never rolling: a rolling comparison lets the
  market walk any distance one sub-threshold step at a time. It reuses chunk 4's
  quote path exactly (`session.py` passes `LegQuotes.spot` by reference, so `entry.py`
  imports nothing from `watchdog.py` and there is one OCC/streamer-symbol-aware
  resolver, not two — CONFIRMED compatible, not assumed: `ChainSnapshot.underlying_price`
  and `LegQuotes.spot()` are both the mid of `market.latest(<underlying>)` on the same
  feed, and `LegQuotes` adds a staleness gate the chain source does not). **Unreadable
  is abandon, not continue** — no spot, a stale spot, or a non-finite reference all end
  the walk, because "I cannot tell whether the market moved" is not evidence that it
  did not, and `PreTradeGate` would refuse this same candidate on this same feed right
  now. `DRIFTED` is not alerted (same reasoning as `WALKED_AWAY`), and the observation
  — reference, reading, threshold, drift — rides in `entry.finished` on EVERY outcome,
  since a near-miss is what the threshold gets tuned from. NB `SpreadSignal.underlying_price`
  defaults to NaN meaning "not recorded" (chunk 4's synthetic exit signals carry it),
  and both `to_payload()` and the check's own record emit None rather than NaN — the
  audit log refuses what `json` cannot encode, and a refused Tier 1 write halts trading.
- **Entries never escalate to crossing the market.** `WALKED_AWAY` is the end of the
  idea, and the asymmetry with chunk 4's exits is deliberate: an exit crosses because
  the alternative is holding a position that is already hurting, while a missed entry
  costs only the trade. Paying up to open a position nobody has to open inverts the
  edge. It is also not alerted — on an unproven strategy with a 10% concession limit
  it is expected to be common, and paging for it trains the operator to ignore the
  channel.
- **One entry attempt per `candidate_id` per process.** `SignalLoop` re-proposes a
  qualifying spread every `signal.cadence_s` (one shadow session: 456 records, 10
  distinct spreads), and NOTHING else stops a re-entry: the duplicate guard allows it
  (a terminal-unfilled order is `DEAD`, deliberately unblocked so crash recovery can
  re-enter) and the concurrent-position limit does not apply (nothing filled, nothing
  held). Without this, a `WALKED_AWAY` entry is re-entered seconds later, all day, at
  the strikes it just conceded 10% on and failed to fill. The candidate is claimed
  BEFORE the submit can raise, because after a failed submit an order may exist.
  Whether a conceded entry is worth re-attempting is a strategy decision nobody has
  made. Re-proposals after that are Tier 2 (`entry.already_attempted`) and cost no
  broker round trip.
- **Every walk outcome is handled explicitly, and the coverage is MECHANICAL.**
  `test_entry.py`'s `OUTCOME_EXPECTATIONS` table drives the parameterization AND is
  asserted equal to `set(WalkOutcome)` — the same set-equality shape
  `test_execution_models.py` uses for SDK enum drift. That assertion is the load-
  bearing half: the table alone was just a list that happened to be current, and
  until 2026-08-03 the "whole enum" claim was a comment, not a check. `_report`'s
  `else` is kept as an UNREACHABLE alert-and-record catch-all rather than a raise,
  deliberately — it sits inside `_walk_and_report`, so raising would engage the kill
  switch, stop the process, and skip the `entry.finished` write, discarding the
  record of a walk that really happened while an order may exist. The outcome is not
  unknown to the system (it is a valid enum member with defined semantics in
  `loop.py`, and the walk has already requested any cancel); only the dispatch table
  would be stale, and CI now catches that first. `FILLED` (→ `watchdog.request_refresh()`,
  and it books NOTHING — the fill consumer does that off the stream regardless of who
  submitted, so booking from the walk's report too would double-book);
  `WALKED_AWAY`/`BLOCKED`/`ABORTED` recorded quietly; `REJECTED`, `NEVER_WORKED`,
  `CANCELLED_ELSEWHERE` and `UNRESOLVED` alert. A non-zero `recovered_disconnects`
  alerts on ANY outcome — a disconnect while a real order was working is worth knowing
  even when the walk recovered.
- **Layering is asserted, not documented.** `pipeline.build_execution_stack` is the
  one place the stack is assembled — `AuditedOrderExecution(ThrottledOrderExecution(
  TastytradeOrderExecution))` — and a test checks the nesting, because both ways of
  getting it wrong fail silently (throttle outside audit delays the Tier 1 record with
  the wire; a missing decorator puts unrecorded orders at the exchange and hard-halts
  the next reconcile). ONE stack per process, shared by the entry path, the walk, the
  guard and the watchdog's exit executor — the rate limiter must be process-wide to
  mean anything, since two limiters at 40 req/s are an 80 req/s ceiling.
- **The trading side is built together or not at all** (`session.TradingSide`). The
  dependency runs one way: `FillConsumer` writes `positions`, `store.open_spreads()`
  reads it back. A watchdog without the fill consumer watches a table nobody writes,
  finds nothing, and reports nothing wrong — the one failure it cannot detect about
  itself; order reconciliation without the fill consumer hard-halts the morning after
  the first fill. Hence one switch and no partial states. The fill consumer itself is
  built BEFORE the account stream (which starts delivering as soon as `start()`
  returns) while the watchdog cannot exist until the chain is loaded — so the
  refresh HINT is the only late-bound part, harmless because the watchdog reads the
  whole ledger on its first cycle.
- **A submitting environment with no `halt` seam REFUSES.** Both the entry executor
  and the watchdog escalate through it; an executor with nowhere to halt is a rail
  that silently does nothing. Checked at wiring time, not at 10:42.
- **Teardown order is loop → watchdog → streams.** After the loop so no new entry can
  land while positions go unwatched; before the streams because `watchdog.stop()`
  waits for an exit already in flight, and that exit is still walking an order — it
  needs quotes and the order feed until it returns.
- **Credential-free boot is now shadow-only.** Sandbox/live open a short-lived trading
  session at startup (`session.startup_reconcile`) for the full position+order check
  and close it again, so nothing broker-related is held for the process lifetime — but
  a sandbox/live process that cannot reach the broker refuses to start rather than
  starting blind. Verified: shadow boots and shuts down clean; sandbox without
  credentials refuses and releases the writer lock.
- **`run_shadow_session` is now `run_session`**, with the old name kept as an alias
  (`tools.shadow`, the supervisor and existing tests all use it). A function called
  `run_shadow_session` that submits real orders in sandbox is exactly the drift this
  file exists to prevent.

Audit categories added: `entry.finished`, `entry.not_built`, `entry.aborted_day`,
`entry.dry_run`, `reconcile.sandbox_reset` (Tier 1); `entry.already_attempted`,
`reconcile.reset_check` (Tier 2 — a re-proposal and a "checked, nothing to do" are the
common cases by volume and would otherwise drown the Tier 1 trail).

## The sandbox 24h account reset

Confirmed against tastytrade's documentation (2026-07-30), not assumed: a SANDBOX
account's trades, transactions, positions and balances are wiped every 24 hours (the
user/customer/account entities survive). So sandbox shows a flat broker every morning
against a ledger on a persistent volume — a `missing_at_broker` discrepancy, and
therefore a HARD HALT, every single day, with the rail firing correctly on something
that is not a fault.

`reconcile.sandbox_reset_decision` is the pure decision, and it clears the ledger only
when BOTH hold:

1. **The trading day has changed since the last reconciliation CONFIRMED CLEAN.** The
   operator's resolution: reuse Argus's existing day-rollover notion (an ET trading
   date vs. another ET trading date, `clock.py`), never a new elapsed-time threshold —
   that would mean guessing at a reset schedule tastytrade does not publish and would
   drift from the rest of the system's idea of a day. The marker is the `trading_date`
   now stamped on every `reconcile.result`, read back by `last_clean_reconciliation`
   (one row, on migration 0003's `(category, ts)` index).
2. **Nothing in the ledger was written on today's trading date.** Not redundant with
   (1), and it is why a same-day crash-restart still halts: a process whose last CLEAN
   reconcile was yesterday, that submitted an order today and then crashed, would be
   told "the day rolled over, clear it" by (1) alone — silently discarding today's real
   order, which is precisely the masking that ruled out the unconditional-clear
   alternative.

Also settled:

- **Only sandbox is ever cleared** — the gate is `environment == "sandbox"`, NOT
  `mismatch_halts_process`. A live account is not wiped nightly, so "broker empty,
  ledger non-empty" there is always real.
- **The last result being a MISMATCH reads as "no clean marker"**, which refuses the
  clear. Deliberately not a backwards search for the last clean one: after a non-ok
  result sandbox/live hard-halt and refuse to start until it is fixed, so a mismatch on
  record means an operator is mid-investigation — the worst possible moment to stand
  this rail down.
- **The residual risk of a false clear is a halt, not a silent resume**, which is what
  makes automating it acceptable at all: clearing when it was not a reset loses Argus's
  record of a position the broker still holds, and that surfaces immediately as
  `unexpected_at_broker` on the very reconcile that follows.
- **`reconcile_and_record` is the ONE sequence both refusal sites run** — reset check,
  compare, `trading_date`-stamped Tier 1 record, mismatch alert — so process-start
  (`app._reconcile`) and per-session (`session._reconcile_or_abort`) differ only in HOW
  they refuse. Before chunk 5 those two already had duplicate record-and-alert code;
  adding the reset logic would have made it three copies of everything.
- The ET conversion in `store._trading_date_of` is load-bearing, not cosmetic: a
  01:30 UTC write is still the PREVIOUS ET day, and a naive UTC date would call it
  "today", conclude the ledger was written today, and refuse a legitimate clear every
  morning. A test pins exactly that timestamp.

## What a clean sandbox run does and does not prove

Sandbox quotes are always 15 minutes delayed, and sandbox orders are matched entirely
inside tastytrade's own sandbox — they never reach a real market. So a clean sandbox
run proves the API contracts and response schemas (field presence, status semantics,
the shape of things) and that the WIRING is correct. It proves nothing about realistic
fill behaviour, price-walking effectiveness, or timing under real market conditions.
Read every sandbox result from here on with that distinction in mind.

## The dry-run activation mode

`[execution] dry_run_submissions` runs the ENTIRE path — gate → duplicate guard →
`build.build_order` → the audited/throttled stack → the broker — with the broker's own
`dry_run=True`, so the first end-to-end exercise of chunks 1–5 together carries no
order risk. It **stops after the submit rather than walking**, and that is not a
shortcut: a dry run mints NO order id (measured against sandbox, chunk 1), so there is
nothing to poll, reprice or cancel, and driving the state machine against that would
be testing the loop against a fiction. It proves construction, decorator layering, the
guard call and that the broker accepts the payload Argus builds — and nothing about
fills, repricing or timing.

**It defaults to TRUE when the key is absent**, the one defaulting key in an otherwise
fail-closed loader. The fail-closed direction for an order-routing switch is "do not
route": flipping `environment` to sandbox against a config that predates chunk 5 must
not start firing real orders by omission. A present value must be a real bool —
`"false"` is a `ConfigError`, not a truthy string.

## Position sizing and the loss caps — CHECKED, and they were placeholders

The chunk brief asked whether sizing already read the decided values. It did not, and
the gap was not only a stale number:

- `risk.daily_max_loss` was **1000.0**, not the decided **1740.0**.
- Sizing was purely `position_size_pct = 0.02`, a RELATIVE bound. 2% of NLV is $1,250
  only at an NLV of exactly $62,500 — so the decided per-trade cap was a coincidence
  of account size that moved every time the account did.
- There was **no absolute dollar cap and no per-trade contract cap anywhere**.
  `max_concurrent_positions = 2` counts positions, not contracts — a different rail.

Now (operator's decision, 2026-07-30): `daily_max_loss = 1740.0`, plus
`max_loss_per_position = 1250.0` and `max_contracts_per_position = 2` as **independent
absolute rails**. Independent because a dollar cap and a contract cap disagree about
which trade is too big as soon as credit or strike distance moves, so whichever is more
restrictive for the trade in hand governs. `position_size_pct` is KEPT as an additional
non-binding sanity bound — it is the only one that shrinks with a drawdown — so the
size is the MINIMUM of all three rather than a replacement of the percentage.

Enforced in BOTH places, and the duplication is the design: `orchestrator.size_contracts`
applies all three when choosing a size, and `PreTradeGate` re-checks the resulting
proposal (`position_max_loss_exceeded`, `contract_cap_exceeded`). The sizer chooses;
the gate REFUSES. The two absolute checks are deliberately NOT chained onto the
invalid-NLV branch — an unreadable account snapshot must not excuse an oversized
position, which is the entire point of having an absolute cap. `size_contracts` keeps
its `max(1, ...)` floor so a candidate that fits no rail is still evaluated and
recorded; the gate is what denies it. The loader refuses
`max_loss_per_position > daily_max_loss` (a per-trade cap above the day's cap can never
bind before the breaker does), and `gate.decision` now carries a `limits` block so a
post-mortem does not have to reconstruct what the config held that day.
