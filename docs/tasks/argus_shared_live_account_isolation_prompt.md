# Argus: The Live Account Is Not Dedicated — Every Account-Derived Rail Assumes It Is

> ## ⚠️ SUPERSEDED 2026-09-10 — RESOLVED OPERATIONALLY. DO NOT RUN THIS PASS.
>
> **The premise below is no longer true.** This prompt asked for a design because
> the live tastytrade account was going to be shared. Richard has since decided and
> begun executing the opposite: **the tastytrade account becomes fully dedicated to
> Argus.** Manual trades and the other strategies move to another brokerage account.
>
> **Nothing in "Options" below should be built.** Every option was a way to teach
> Argus's rails to tolerate foreign positions; a dedicated account makes all of them
> unnecessary, and each one would have *weakened* a rail (whole-account NLV, the
> broker-is-the-only-source-of-truth reconciliation, `open_spread_equivalent`) to buy
> tolerance nothing now needs. This is the outcome the prompt itself allowed for:
> *"the correct answer may be operational (open a dedicated account) rather than a
> code change at all."* It was.
>
> **What survives is §"What breaks", and only as the reason for a waiting rule.**
> Those five verified consequences are why the flip must not happen while any
> non-Argus position remains in the account. The one remaining action is tracked as
> **C8 in [`docs/go-live-findings.md`](../go-live-findings.md)**: let the last legacy
> tastytrade positions resolve under normal trade-management policy — do not force
> them for the flip — and confirm the account is flat of everything Argus did not
> open. That is a waiting item, not a design.

## Priority

**RESEARCH + DESIGN ONLY. No code. Do not implement any option without Richard's
explicit sign-off on the recommendation.** This touches four of CLAUDE.md's
non-negotiables at once (broker-is-the-only-source-of-truth, the daily max-loss
breaker, every size rail, `max_concurrent_positions`), and the correct answer may
be operational (open a dedicated account) rather than a code change at all — which
is exactly why this is a design pass and not a ticket.

**This is a live-flip BLOCKER.** As the code stands, pointing `environment = "live"`
at an account that holds any non-Argus position does not degrade gracefully — it
hard-halts at process start and, if it somehow got past that, blocks every entry
forever. See "What breaks" below. Nothing here is theoretical or first-week; it is
step-zero for live.

## The issue

Argus was designed on the assumption that the tastytrade account it trades is
**dedicated to Argus** — nothing else opens positions in it, and the whole account
is the strategy. Richard has confirmed this assumption is false for the live
account: **accounts are not siloed by strategy, and the live account will hold
other positions** (manually-entered trades, other strategies, Treasuries, etc.).

The reconciliation non-negotiable — "the broker is the only source of truth, refuse
to resume on any mismatch" — is built as `broker_positions == argus_expected_positions`.
The moment the account holds a leg Argus did not put there, that equality is false
by construction, and Argus reads a perfectly healthy account as a corrupted-state
emergency.

## What breaks (verified against current source)

Every one of these reads a **whole-account** number and assumes it is an
**Argus-only** number.

### 1. Reconciliation hard-halts at startup — Argus cannot even boot

`reconcile.reconcile_positions(expected, broker)`
([`src/argus/reconcile.py`](../../src/argus/reconcile.py)) walks
`sorted(set(expected) | set(broker))` and classifies any symbol the broker holds
but `expected` does not as `UNEXPECTED_AT_BROKER`. `Reconciler.reconcile` builds
`broker_map` from **every** non-flat position the broker returns
(`reconcile.py`, `broker_map = {p.symbol: p.signed_quantity for p in positions ...}`).

- Every foreign position → one `UNEXPECTED_AT_BROKER` discrepancy.
- `mismatch_halts_process("live")` is `True` → `ReconciliationError` → process-wide
  hard halt at `app.Application._reconcile`, before any session is built.
- `reconcile.halt_grade` returns `HaltGrade.ALL` (a position discrepancy of any
  kind disqualifies `ENTRIES`), so P3's monitor-only path does not apply — the
  watchdog never starts either.
- The 2026-08-25 same-day-expiry and sandbox-reset carve-outs do not help: they
  only ever *remove* rows from the discrepancy set, and neither matches a foreign
  equity/option position.

**Net: `python -m argus` with `environment = "live"` against a shared account parks
on the first reconcile and never trades.**

### 2. Concurrency cap is permanently tripped

`RiskSnapshot.open_spread_equivalent`
([`src/argus/risk/models.py`](../../src/argus/risk/models.py)) is
`max(ceil(open_position_count / 2), ledger_spread_count) + working_open_spread_count`,
and `open_position_count` is **broker legs** — the whole account's, read off
`AccountStateCache` which is seeded from `/accounts/{n}/positions`.

Foreign legs inflate `ceil(broker_legs / 2)` past `ledger_spread_count`, so
`open_spread_equivalent >= max_concurrent_positions` is true regardless of how many
spreads Argus actually holds → `PreTradeGate` denies every candidate on
`concurrent_limit` ([`src/argus/risk/gate.py`](../../src/argus/risk/gate.py)).

The design comment on this field is explicit that broker-derived-and-rounding-up is
deliberate ("the system degrades toward not trading") — correct for a dedicated
account, actively wrong for a shared one.

### 3. Daily max-loss breaker measures the wrong P&L

`RiskSnapshotBuilder` sets `daily_pnl = DailyPnl.value(nlv) = current_nlv - anchor_nlv`
([`src/argus/risk/snapshot.py`](../../src/argus/risk/snapshot.py)), where `nlv` is
**whole-account** `net_liquidating_value`. The anchor is a whole-account NLV
snapshot (`day_pnl.anchored`, [`src/argus/risk/rehydrate.py`](../../src/argus/risk/rehydrate.py)).

- A Treasury mark, an earnings move on another position, a manual trade → day P&L
  swings for reasons that have nothing to do with Argus.
- The breaker trips on someone else's loss (halts Argus for no reason) **or** masks
  an Argus loss with someone else's gain (the rail silently does not fire).
- The welded invariant `max_concurrent_positions × max_loss_per_position ≤
  daily_max_loss` (2 × 1250 = 2500) becomes meaningless — it bounds Argus's
  contribution, not the number the breaker actually watches.

This rail is *already* the #1 live-flip gate (cert never marks NLV, so it has never
been exercised). A shared account means the first time it runs it is also measuring
the wrong thing.

### 4. The relative size rail sizes against capital Argus does not own

`PreTradeGate`: `proposal.max_loss > position_size_pct * snapshot.account_nlv`
(`gate.py`). `position_size_pct = 0.02` against a shared account's NLV is 2% of a
number that includes capital allocated to everything else. The absolute rails
(`max_loss_per_position`, `max_contracts_per_position`) still govern as the tightest
check, so the practical harm is bounded — but the relative rail is now noise, and
the config comment's reasoning for keeping it ("scales down with a drawdown") no
longer describes an Argus drawdown.

### 5. Order-level reconciliation flags foreign working orders

`reconcile_orders` ([`src/argus/reconcile.py`](../../src/argus/reconcile.py)) takes
`get_live_orders()` — **all** of the account's live orders — and any order whose id
is not in Argus's ledger and whose `classify_order` is `WORKING` or `FILLED`
becomes `UNEXPECTED_WORKING_ORDER` / `UNEXPECTED_FILLED_ORDER` → halt. A manual
limit order sitting in the book, or another strategy's working order, halts the
process. (`halt_grade` can only reach `ENTRIES` if *every* order discrepancy is one
of Argus's own opening orders, so a single foreign order forces `ALL`.)

`DuplicateOrderGuard.has_conflicting_order` keys on `external_identifier`, so a
foreign order with a non-`argus-` identifier will not false-match there — but this
should be confirmed, not assumed, and the SPX/SPXW symbol-collision case (below)
needs specific thought.

### 6. `held_but_unbooked` / ghost-position detection becomes permanent noise

`AccountStateCache` + the watchdog's book-agreement check compare broker leg counts
against Argus's ledger ([`src/argus/account/stream.py`](../../src/argus/account/stream.py),
`execution/watchdog.py`). Foreign legs are a permanent, unclearable "broker holds
more than the ledger" disagreement — the exact shape that pages `held_but_unbooked`,
the one condition built with no timer allowance.

### 7. Symbol collision — the case with no clean fix

If another strategy in the same account ever trades **SPX or SPXW options**
(same root, possibly same expiry, conceivably an adjacent strike to an Argus leg),
then `positions` netting by symbol means Argus genuinely cannot tell its own leg
from the other strategy's. This is the `docs/decisions/2026-08-17-overlapping-verticals.md`
problem but across strategies instead of within Argus's own chained book — and
Argus's per-trade-idea attribution (`order_legs.applied_quantity`) only covers legs
*Argus* placed. The research must establish whether this case can be excluded by
policy (Argus is the only thing trading index options in that account) or must be
handled.

### 8. What is NOT affected (confirm, don't assume)

- The **watch list** and the **exit path** (`ExitExecutor`, the virtual stop, the
  `FLATTEN_BEFORE_CLOSE` trigger) are built from Argus's **own order ledger**, not
  from the account position feed — so Argus should not try to close or flatten a
  foreign position. Confirm this holds end to end (especially the book-agreement
  and residual-order paths).
- The **kill switch** and **audit log** are account-agnostic.
- **Buying power / margin**: Argus does not appear to check BP directly (it uses
  NLV-relative + absolute rails). In a shared account, other positions' margin
  usage could cause a broker-side rejection of an Argus order. v0.3.12/P6 now
  records `reject_reason`, so this is visible — but confirm whether a BP rejection
  is handled gracefully or ends the day.

## Research questions

1. **Can tastytrade give Argus a dedicated account under the same customer login?**
   - Does tastytrade support multiple accounts per customer (e.g. a second
     individual/margin account, or an entity/trust account)? What are the limits,
     the application friction, and the funding mechanics (internal transfer between
     own accounts)?
   - Argus **already** supports targeting one: `Application(account_number=...)`
     threads through to `session.py` (`accounts = [a for a in accounts if
     a.account_number == account_number]`), defaulting to `accounts[0]` when unset.
     Where is `account_number` sourced today (env? CLI? unset?), and what is the
     one-line change to pin it?
   - Tax/entity/PDT/assignment implications of siloing the 0DTE strategy into its
     own account (Richard's call, but surface the considerations).
2. **Does the tastytrade API expose any position/order → originating-strategy or
   originating-order linkage** that would let Argus scope reconciliation to "legs I
   placed" without a dedicated account? (Fills carry `order_id`; positions do not
   directly. Account-level tags/notes? Sub-accounting? `ext_group_fill_id`?)
3. **Is there a defensible scoping predicate** for "an Argus position" that does not
   depend on a dedicated account — e.g. "SPXW options expiring today, within the
   strategy's strike window, that Argus's fill ledger accounts for"? What does it
   get wrong (a real Argus fill that never booked; a foreign SPXW position)?
4. **What is the correct P&L basis for the breaker** if account NLV is off the
   table? Argus already has per-position marks (the watchdog's `cost_to_close` /
   `credit_stop` machinery) and its own fill ledger. Cost/benefit of a
   strategy-attributed realized+unrealized P&L figure vs. keeping an NLV delta but
   requiring a dedicated account.
5. **Symbol collision (item 7)**: can it be excluded by written policy, or must the
   design tolerate another strategy trading index options in the same account?
6. Prior art: how do multi-strategy execution frameworks (or tastytrade's own
   ecosystem, or TOMIC — `--add-dir` for pattern lookup only) handle
   strategy-scoped accounting inside a shared brokerage account?

## Solution space to evaluate

Evaluate at least these; add others if the research surfaces them. For each: what
changes (code + operational), which invariants in "What breaks" it fixes vs. leaves
open, effort, residual risk, and reversibility.

- **A — Dedicated tastytrade account (sub-account under the same login).** Pin
  `account_number`, fund it with the strategy's allocation. Preserves *every*
  invariant unchanged; the code assumption becomes true instead of being worked
  around. Cost is operational (open + fund an account) and a capital-siloing
  decision. Likely the recommendation unless research finds a blocker — but the
  prompt must actually evaluate it, including what happens if Richard wants to move
  the allocation later.
- **B — Strategy-scoped accounting layer.** Keep the shared account; make every
  account-derived rail read an Argus-attributed figure instead of a whole-account
  one:
  - reconciliation compares only symbols in Argus's scope (predicate from Q3),
    treating out-of-scope positions/orders as *invisible* rather than *unexpected*;
  - the breaker runs on strategy-attributed realized+unrealized P&L, with its own
    durable anchor;
  - the concurrency cap drops the broker-leg term (or filters it to in-scope
    symbols) and trusts `ledger_spread_count` + `working_open_spread_count`;
  - the relative size rail is dropped or repointed at a configured Argus allocation
    notional.
  This is a real design change across `reconcile.py`, `risk/snapshot.py`,
  `risk/models.py`, `risk/gate.py`, `risk/breaker.py`, `risk/rehydrate.py`, and the
  account feed — and it *weakens* the "broker is the only source of truth" rail
  into "the broker is the only source of truth about the positions I claim," which
  needs its own careful argument and a new failure-mode analysis (a real
  unattributed Argus fill now reads as someone else's position and is ignored).
- **C — Hybrid.** Dedicated account for the capital and the breaker/NLV rails;
  scoped accounting only where a dedicated account still isn't enough (e.g. if the
  same account must briefly hold a manual hedge). Probably only relevant if A has a
  partial blocker.
- **D — Do not run live on a shared account.** Make it a hard, enforced
  precondition: `config.py` refuses `environment = "live"` unless `account_number`
  is set AND the first reconcile sees an empty-or-Argus-only book, with a clear
  error. Cheapest to build, forces A operationally, and is a legitimate answer if B
  is judged too risky for the rail it weakens.

## Evaluation criteria

- Does it keep the daily max-loss breaker meaningful (the rail welded to
  `max_concurrent_positions × max_loss_per_position`)?
- Does it keep "refuse to resume on a mismatch" a real rail, or does it blind Argus
  to a genuine unattributed fill? Spell out the new worst case.
- Blast radius of the change (files, tests, config keys, migrations).
- Reversibility — if Argus later gets its own account, does the change become dead
  weight?
- Operator burden (another account to fund and monitor vs. more code to trust).

## Deliverables

1. A **decision record** in `docs/decisions/` (dated, added to
   `docs/decisions/README.md`) covering: the assumption and where it came from, the
   full "what breaks" list confirmed against source, the options evaluated, and a
   **single recommendation** with its reasoning and its residual risks stated
   plainly.
2. If the recommendation is **A or D**: the exact config/wiring change (where
   `account_number` comes from, the `config.py` guard, the runbook step) and the
   `CLAUDE.md` non-negotiable wording update — as a design, not a patch.
3. If the recommendation is **B or C**: an itemized change list per file from "What
   breaks," the new scoping predicate with its failure modes, the breaker's new
   P&L basis and anchor design, any new config keys / migration, and an explicit
   rewrite of the "broker is the only source of truth" non-negotiable to match what
   the rail would actually guarantee.
4. Either way: what the **live-flip checklist** gains as a line item.

## Non-negotiables this must respect

- No option may leave Argus able to close, flatten, or cancel a **non-Argus**
  position or order. The OPENING/CLOSING asymmetry from
  `docs/decisions/2026-08-29-mid-session-cancel-recheck.md` is the precedent:
  cancelling something that isn't yours is worse than missing it.
- The breaker must stay a real automatic rail — "measure a different number"
  cannot become "measure nothing."
- `NEVER TOUCH THE LIVE DATABASE`. Any exploratory analysis of the live account's
  contents is read-only and out-of-band (`argus.dev.toml` for anything that writes;
  `mode=ro` for reads).
- Design-first. This document's output is a recommendation for Richard to approve,
  not a merged change.

## Out of scope

- Implementing the chosen option.
- The daily max-loss breaker's cert-inertness (separate live-flip gate; this prompt
  only intersects it where a shared account changes the P&L basis).
- Position sizing / strategy parameter tuning.
- Multi-strategy *orchestration* (running other strategies through Argus) — Argus
  stays single-strategy; this is only about coexisting in one brokerage account.
