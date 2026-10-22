# The live account is not dedicated to Argus (2026-08-31)

**Status: RECOMMENDATION ACCEPTED 2026-09-10 (Richard), and being executed
operationally.** Option A is happening: **the tastytrade account becomes fully
dedicated to Argus.** Manual trades and the other strategies move to another brokerage account. The remaining step is a waiting one: let the last legacy
tastytrade positions resolve under normal trade-management policy, then confirm the
account is flat of everything Argus did not open.

**The D half — the enforcement — is BUILT (2026-09-10).** `[broker]
account_number` exists in all three profiles — `argus.live.toml` pins `5XX00000`;
dev and sandbox ship `""` (unpinned, by decision: a pinned cert account turns a
tastytrade re-provision into a permanent `SessionAborted`). `config.py` refuses
`environment = "live"` without it, and `__main__.py` passes
`config.broker.account_number` through the account filter this record's §3
already had, so the supervisor no longer takes `accounts[0]`. C9 is closed in
[`docs/go-live-findings.md`](../go-live-findings.md); the build record is
[2026-09-10 — pinning the traded account](2026-09-10-pinning-the-traded-account.md).

**The original task prompt is superseded** and carries a banner saying so
(`docs/tasks/argus_shared_live_account_isolation_prompt.md`) — none of its
tolerate-a-shared-account options should be built, because each one weakens a rail
to buy tolerance a dedicated account makes unnecessary.

**Recommendation: A, enforced by D.** Open (or designate) a tastytrade account
that holds nothing but Argus, pin it with a new REQUIRED-under-live
`[broker] account_number`, and make `config.py` refuse `environment = "live"`
without it. The "book is Argus-only" half of D needs **no new rail** — the
process-start reconcile already enforces it, fail-closed, today. Everything else
in the system stays exactly as designed, because the assumption becomes true
instead of being worked around.

**This is a live-flip BLOCKER.** As the code stands, `environment = "live"`
against an account holding one foreign position parks at the first reconcile and
never trades. `argus.live.toml` already exists in the working tree (untracked,
`environment = "live"`) and **cannot name an account** — the key does not exist,
so it would take `accounts[0]`, whichever that is.

---

## The assumption, and where it came from

Argus was designed on "the account is the strategy". Nothing states it; it is
load-bearing in five places at once and visible only as the *absence* of a scope
in each. The clearest fossil is `reconcile.reconcile_positions`, which walks
`sorted(set(expected) | set(broker))` — a whole-account set union against a
table only Argus writes. That is a correct comparison exactly when the two sets
are supposed to be equal, i.e. when nothing else trades the account.

Richard confirmed on 2026-08-31 that this is **false for the live account**:
accounts are not siloed by strategy, and the live account will hold
manually-entered trades, other strategies, and Treasuries.

Two things follow immediately, and they pull in opposite directions:

- Every account-derived rail reads a **whole-account** number and treats it as an
  **Argus-only** number.
- Every *order*-derived and *ledger*-derived path is already correctly scoped,
  because Argus mints its own `external_identifier` and writes its own ledger.
  The exit path, the watch list, the duplicate guard, the residual-order recheck
  and the working-order term of the concurrency cap all filter on
  `is_argus_identifier` or read the ledger. **None of them can touch a foreign
  position or order**, and the non-negotiable that says so is already held by
  mechanism.

So the damage is confined to the four *account-derived* rails, and that is the
whole of the design problem.

---

## What breaks — confirmed against source

Verified 2026-08-31 against `main` at 3be57b5. Each item below was read, not
assumed; corrections to the prompt's framing are marked.

### 1. Reconciliation hard-halts at startup — CONFIRMED

`Reconciler.reconcile` (`src/argus/reconcile.py`) builds
`broker_map = {p.symbol: p.signed_quantity for p in positions if p.signed_quantity != 0}`
from **every** non-flat position the broker returns, and compares it against
`SqlitePositionStore.open_positions()`, which reads
`SELECT symbol, signed_quantity FROM positions WHERE signed_quantity != 0` —
a table `execution/store.py` is the only writer of. Every foreign leg lands in
`reconcile_positions` with `e == 0`, so `kind = UNEXPECTED_AT_BROKER`.

- `mismatch_halts_process("live")` is `True` → `ReconciliationError` →
  process-wide hard halt in `app.Application._reconcile`, before any session
  exists.
- `halt_grade` returns `HaltGrade.ALL` on its **first** test
  (`if result.discrepancies: return HaltGrade.ALL`), so P3's monitor-only path
  is unreachable. The watchdog never starts.
- Neither carve-out helps. `expiry_verdicts` is row-scoped to
  `missing_at_broker` rows whose own OCC expiry is past; `sandbox_reset_decision`
  returns `RESET_NOT_SANDBOX` under live. Both only ever *remove* rows.

**One correction to the prompt.** It says the process "parks on the first
reconcile and never trades" — that is right, and the park is the *designed*
behaviour (2026-08-17, halted-but-alive): `/status` and `/kill` stay bound and
served. The failure is loud and safe. It is still a total blocker.

### 2. Concurrency cap is permanently tripped — CONFIRMED

`RiskSnapshot.open_spread_equivalent` (`src/argus/risk/models.py`) is
`max(ceil(open_position_count / 2), ledger_spread_count) + working_open_spread_count`.
`open_position_count` is `AccountSnapshot.open_position_count` —
`sum(1 for p in self.positions if p.signed_quantity != 0)` over the cache seeded
from an unfiltered `GET /accounts/{n}/positions` (`account/refresher.py`, no
query params). Four foreign legs alone make the first term 2, and
`PreTradeGate` denies on `concurrent_limit` at
`open_spread_equivalent >= max_concurrent_positions`.

The design comment is explicit that broker-derived-and-rounding-up is
deliberate. It is right for a dedicated account and inverted for a shared one:
"degrade toward not trading" becomes "never trade".

**Note what is already scoped**, because it is the shape of the fix if B were
ever chosen: `working_open_spread_count` comes from
`execution/working_orders.py`, which filters `is_argus_identifier` — the third
term of this same expression is already Argus-only.

### 3. Daily max-loss breaker measures the wrong P&L — CONFIRMED, and worse than stated

`RiskSnapshotBuilder.build` sets `daily = self._day_pnl.value(nlv)` where `nlv`
is whole-account `net_liquidating_value`; `DailyPnl.value` is
`current_nlv - anchor`, and the anchor is a whole-account NLV reading captured
at session start (or rehydrated per trading date by `risk/rehydrate.py`).

Everything the prompt says holds. Two additions found in review:

- **A deposit, withdrawal or internal transfer is indistinguishable from P&L.**
  `DailyPnl` has no notion of external cash flow. Moving $20k into the account at
  11:00 reads as a $20,000 gain; moving it out reads as a $20,000 loss and trips
  a $2,500 breaker instantly. This is true on a **dedicated** account too, so it
  becomes an operating rule under the recommendation, not an argument against it.
- The welded invariant `max_concurrent_positions × max_loss_per_position ≤
  daily_max_loss` (2 × 1250 = 2500, exactly) bounds *Argus's* worst case. Against
  a whole-account P&L figure the two sides of that inequality are denominated in
  different things and the equality stops meaning anything.

This rail is already the #1 live-flip gate (cert never marks NLV, so it has never
fired). A shared account means the first time it runs, it also measures the wrong
quantity.

### 4. The relative size rail sizes against capital Argus does not own — CONFIRMED, low harm

`PreTradeGate`: `proposal.max_loss > self._position_size_pct * snapshot.account_nlv`,
and `orchestrator.size_contracts` applies the same bound when choosing size
(`by_pct = int((pct * regime_multiplier * nlv) // max_loss_per_contract)`).

The absolute rails (`max_loss_per_position = 1250`,
`max_contracts_per_position = 2`) are re-checked independently and are **not**
`elif`-chained onto the NLV branch, so an inflated NLV cannot widen them. At
2% the relative rail binds only below an NLV of $62,500, so on a larger shared
account it simply stops being a rail. Practical harm is bounded; the *stated
reasoning* for keeping it ("it scales down with a drawdown") stops describing an
Argus drawdown.

### 5. Order-level reconciliation flags foreign working orders — CONFIRMED; the guard is clean

`reconcile_orders` takes `LiveOrders` — everything working, plus anything
cancelled or filled in the past 24 hours — and any order not in
`known_order_ids` whose `classify_order` is `WORKING` or `FILLED` becomes a
discrepancy. A manual limit order in the book halts the process. `halt_grade`
can only reach `ENTRIES` if **every** order discrepancy is one of Argus's own
opening orders by `parse_external_identifier`, so a single foreign order forces
`ALL`.

**`DuplicateOrderGuard` is confirmed clean, not assumed.**
`has_conflicting_order` filters `o.external_identifier == external_identifier`
(exact equality) and `live.unmappable_by_identifier(...)`, and it raises on an
empty identifier rather than matching everything. A foreign order carrying no
identifier, or any identifier other than the exact
`argus-{direction}-{candidate}` under test, cannot false-match. The SPX/SPXW
collision case (item 7) does not reach this code — the guard keys on the
identifier, never on symbols.

### 6. `held_but_unbooked` becomes permanent, unclearable noise — CONFIRMED

`PositionWatchdog._check_broker_agreement` compares
`_broker_position_count()` (the account cache's whole-account leg count) against
`LedgerState.positions` (a row count of Argus's `positions` table). Foreign legs
make `broker > ledger` permanently → `direction = "held_but_unbooked"`, the one
condition built with **no timer allowance**.

The two suppressions both fail to help, and it is worth being precise about why:

- `_book_allowance_elapsed` suppresses `held_but_unbooked` only while Argus's own
  recent **closing fills** account for the excess legs. Foreign legs have no such
  fill. No suppression.
- `_cache_is_over_reporting` asks REST and suppresses only when
  `confirmed == ledger`. REST reports the foreign legs too, because they are
  real. `over_reporting` is `False`. **It pages.**

So: one Tier 1 page per episode plus a repeating escalation, forever, for a
condition no operator action can clear. The 2026-08-05 alert-storm shape exactly
— a real signal at a threshold that is wrong for the input.

### 7. Symbol collision — CONFIRMED as the case with no clean fix

If anything else in the account trades SPX or SPXW options, `positions` netting
by symbol means Argus cannot distinguish its own leg from the other strategy's.
This is `2026-08-17-overlapping-verticals.md` across strategies rather than
within Argus's own chained book, and Argus's per-trade-idea attribution
(`order_legs.applied_quantity`) only covers legs **Argus placed**.

Concretely: another strategy short one SPXW 7750 put and Argus long one as a
spread's long leg net to **zero** in the broker's book. Every symbol-keyed
predicate answers "not held" for the strike an Argus spread is standing on. This
is the same mechanism `halt_grade` deliberately refuses to build a symbol test
on, and its docstring says so.

**Verdict: this case cannot be handled, only excluded by policy.** Any design
that tolerates it is guessing about which leg is whose at the moment a guess
sends an order.

### 8. What is NOT affected — CONFIRMED end to end

- **The watch list and the exit path are safe by construction.**
  `store._open_spreads` reassembles spreads from `orders JOIN order_legs`, keyed
  per trade idea by `_spread_key` (`candidate_id`, else the minted
  `external_identifier`). `positions` is read only for the `unattributed`
  check — and `positions` is written **only** by `execution/store.py` from
  Argus's own fills, so it never contains a foreign leg. `ExitExecutor`, the
  virtual stop, and `FLATTEN_BEFORE_CLOSE` all act on entries from that scan.
  **Argus cannot close or flatten a foreign position.**
- **The residual-order recheck is safe by construction.**
  `ResidualOrderRecheck` only ever holds orders its own walks registered at
  `_finish`, keyed on `broker_order_id`, with `minted_direction` taken from
  `state.sent.direction`. `read_sighting` matches on that id. A foreign order can
  never enter the registry, so it can never be cancelled. **Argus cannot cancel a
  foreign order.**
- **Kill switch and audit log** are account-agnostic. Confirmed.
- **Buying power.** Argus checks no BP figure anywhere; the rails are NLV-relative
  plus two absolute caps. A margin rejection from another position's usage surfaces
  as `WalkOutcome.REJECTED` → `entry_rejected` alert + `WalkResult.reject_reason`
  (v0.3.12 / P6). **It does not end the day** — `entry.py`'s `REJECTED` branch
  alerts and returns. But the candidate is already in `_attempted`, so it is
  retired for the process lifetime and the next tick proposes a *different*
  strike pair into the same wall. That is precisely the open decision "should
  entry back off on the COUNT of consecutive terminal rejections", and a shared
  account makes it reachable by a second, more ordinary cause than a dead venue.
  **Not a blocker; it raises that open decision's priority.**

---

## Research findings

### Q1 — Can tastytrade give Argus a dedicated account under the same login?

**Yes, mechanically.** tastytrade supports multiple accounts per customer:
signing in at `my.tastytrade.com` exposes **OPEN ANOTHER ACCOUNT**, and account
types include individual cash, individual margin, joint, entity and IRAs. The
API is already multi-account: `GET /customers/me/accounts` returns every account
on the login, `Account.get(session)` returns a list, and every balances/positions/
orders call is `/accounts/{account_number}/...`.

**Not confirmed by public documentation, and Richard must verify with
tastytrade:** whether a *second individual margin* account is permitted for the
same person (some brokers allow one individual cash + one individual margin and
require an entity for further siloing), and the funding mechanics of an internal
transfer between two of one's own accounts. Neither changes the recommendation —
if a second individual is refused, an entity/trust account is the fallback, at
higher application friction.

**Argus's support for targeting one is real but incomplete, and the prompt
overstates it.** `Application(account_number=...)` exists (`app.py:282`) and
threads to `session.startup_reconcile` / `run_session`, which filter
`accounts = [a for a in accounts if a.account_number == account_number]` and
otherwise take `accounts[0]`. **But `__main__.main()` constructs
`Application(config)` with no `account_number`** (`__main__.py:37`), and there is
no config key and no environment variable feeding it. Today only
`tools/shadow.py` can pass one. So this is **not** a one-line change: it needs a
new config key, a live-guard, and one line in `__main__`. Design in
"The change" below.

A second gap found in the same pass: **`tools/account.py` refuses to run against
a live config** (`if config.environment == "live": raise SystemExit(...)`), even
though it places no orders. It is the only in-repo way to enumerate accounts and
print the book, so today there is no Argus-side read-only path to obtain the live
account number or inspect the live account. Read both off `my.tastytrade.com`
instead; relaxing that refusal is optional and is *not* recommended as part of
this change (a blanket sandbox-first posture on a tool is cheap to keep).

**Tax / entity / PDT / assignment considerations (Richard's call, surfaced not
decided):** a second individual account under the same SSN does not change tax
treatment (1256 treatment for SPX index options is per-instrument, not per
account) but does split the 1099s. PDT is an equities rule and does not bind
index-option spreads, but a small dedicated account is where a PDT-adjacent
restriction would bite first if the strategy ever changed shape. SPXW is
**cash-settled** — there is no assignment of stock, which is why siloing costs
nothing on that axis; the settlement debit/credit lands in whichever account
holds the position. Capital siloed into the Argus account is capital not
available as margin elsewhere: that is the real cost, and it is a portfolio
decision, not an engineering one.

### Q2 — Does the API expose a position → originating-order link?

**No.** `CurrentPosition` (tastytrade SDK 12.x, read from the installed package)
carries: `account_number, symbol, instrument_type, underlying_symbol, quantity,
quantity_direction, close_price, average_open_price, multiplier, cost_effect,
is_suppressed, is_frozen, realized_day_gain, realized_today, created_at,
updated_at, mark, mark_price, restricted_quantity, expires_at, fixing_price,
deliverable_type, average_yearly/daily_market_close_price,
realized_day_gain_date, realized_today_date`.

There is **no order id, no tag, no note, no strategy label**. A position is a
netted symbol-level fact and nothing more — which is the same reason Argus's own
`positions` table cannot represent chained verticals.

What *does* exist, and is the only attribution primitive the broker offers:

- `Transaction.order_id` (nullable int), plus `ext_group_id`,
  `ext_group_fill_id`, `ext_exec_id`, `lots: list[Lot]` with
  `Lot.transaction_id`. So **fills** link back to orders, and Argus's ledger
  holds its own order ids. A transactions-based *realized* P&L attributable to
  Argus is therefore constructible (and would also pick up `commission` /
  `regulatory_fees` / `proprietary_index_option_fees`, which Argus records
  nowhere today).
- `get_positions(underlying_symbols=..., instrument_type=..., partition_keys=...)`
  supports **server-side filtering**, so a product-scoped position fetch is one
  parameter, not a client-side filter. `partition_keys` is tastytrade's account
  *partition* concept, not a strategy silo, and is not a substitute for a
  separate account.

**Net: there is no per-position provenance. Any scoping predicate must be
inferred, and inference is what item 7 defeats.**

### Q3 — Is there a defensible scoping predicate?

Two candidates, and they fail differently. This is the crux of option B.

**(a) Ledger-scoped — "symbols Argus's ledger claims".** Cheap, and
**structurally circular**: it makes `UNEXPECTED_AT_BROKER` unreachable. That
discrepancy kind is the *only* thing that catches a fill Argus placed and never
booked — the exact 2026-08-11 unwatched-position gap, and the reason
`open_spread_equivalent` is broker-derived at all. Under (a), an Argus fill with
no ledger row is out of scope **by definition**, reads as somebody else's
position, and reconciliation reports CLEAN. That is a live 0DTE spread with no
`positions` row, no watch-list entry, no virtual stop, and a rail actively saying
everything is fine. **This predicate must not be built.**

**(b) Product-scoped — "SPXW options expiring today, within the strategy's
strike window".** Not ledger-derived, so it keeps catching unbooked Argus fills.
Its failure modes:

- A **foreign SPXW leg** is claimed as Argus's: it halts reconciliation anyway
  (so B does not deliver coexistence for the case it exists for) *and* it
  inflates `open_spread_equivalent`, and under a P&L rewrite it would be marked
  into Argus's attributed P&L. Item 7, unfixable.
- A **real Argus position that is not 0DTE-today** — an overnight leg from a
  failed close, or any future non-0DTE structure — falls out of scope and becomes
  invisible to the rail. The predicate encodes today's strategy into a safety
  rail.
- It needs `underlying_symbol` to be a reliable discriminator for SPXW.
  `map_position` already captures it (`Position.underlying`), but **what
  tastytrade reports as `underlying_symbol` for an SPXW contract is unverified
  in this repo** — nothing reads that field today. It would have to be measured
  before anything depended on it.

**The decisive observation:** (b) is only sound given the written policy
"nothing else in this account trades SPX/SPXW index options". If that policy can
be written, then "nothing else trades this account at all" can be written too —
and that is option A, which costs no code and weakens no rail. **B buys exactly
one thing over A: it lets non-index-option positions (Treasuries, equities,
other-underlying options) share the account.** That is a real benefit, and it is
the only one; it has to be weighed against a rewrite of four rails and a
permanent weakening of the reconciliation guarantee.

### Q4 — What is the correct P&L basis for the breaker if NLV is off the table?

An Argus-attributed realized + unrealized figure, anchored at 0.0 each trading
date rather than to an NLV reading. The parts mostly exist:

- **Unrealized** — the watchdog already computes `cost_to_close` per open spread
  from live leg quotes; unrealized is `entry_credit − cost_to_close` per spread.
- **Realized** — Argus's own fills, entry credit minus exit debit, per trade
  idea; `tools/mark.py` and `tools/desk.py` already reconstruct exactly this.
- **Anchor** — trivially 0.0 per trading date, which is *simpler* than the NLV
  anchor and immune to the deposit/withdrawal contamination noted in item 3.

Three costs, and the first is disqualifying on its own **today**:

1. **The basis is known-wrong by 9–18%.** `OpenSpread.entry_credit` is the filled
   order's LIMIT, not its fill price (open finding, 2026-08-20 pass, n=2). An
   attributed P&L built now inherits that error straight into the rail that
   decides when to stop trading for the day — and the fix
   (`FillInfo.fill_price` captured durably, column + migration) is **DEFERRED**.
   So B's breaker has a deferred prerequisite.
2. **Fees and slippage are absent.** Argus records no commissions or exchange
   fees. A transactions-based realized figure could pick them up
   (`Transaction.commission`, `regulatory_fees`,
   `proprietary_index_option_fees`), at the cost of a new broker read and a new
   attribution path on the rail's critical input.
3. **It is blind to what it does not know.** NLV delta catches *everything* —
   an unbooked fill, a settlement debit, a fee, an assignment surprise. An
   attributed figure catches only what Argus already believes. That is the wrong
   direction for a breaker: the cases most in need of one are the cases where
   Argus's own picture is wrong.

On a dedicated account, NLV delta is a *complete* measure of the strategy's day
by construction, needs no new code, and is what the welded invariant is stated
against. **That is a strong argument for A independent of everything else.**

### Q5 — Can the symbol collision be excluded by policy?

**It must be, and A excludes it as a side effect.** Under a dedicated account
the policy is "nothing else trades this account", which is enforceable at the
one place it matters (a human's own discipline) and *verified continuously* by
the existing reconciliation rail — a violation halts loudly on the next restart
and pages via `held_but_unbooked` within a cadence tick.

Under B the policy narrows to "nothing else trades SPX/SPXW here", which is
strictly harder to remember and, crucially, **no longer verified**: B's whole
purpose is to stop reconciliation objecting to foreign positions, so the
mechanism that would catch a violation is the mechanism B removes. A policy with
no enforcement, protecting a rail that has already cost live positions when got
wrong, is not a control.

### Q6 — Prior art

- **TOMIC** (read-only lookup, `~/workspace/tomic`) faces the same problem and
  solves it by *not needing to be right*. `monitor_engine.evaluate_position_group`
  takes `groups: dict[str, list[dict]]` keyed by **underlying symbol** and
  evaluates each group's aggregate credit and P&L — coarse grouping that would be
  wrong the moment two strategies shared an underlying. It is tolerable because
  TOMIC **takes no automated action**: it renders advice. Separately,
  `utils/trade_id.build_trade_id` identifies a trade by the *shape* of its leg
  set (`ticker::sorted OCC legs`) — a leg-set identity, not a per-order one,
  which cannot distinguish two identical spreads and cannot survive netting.
  **The lesson is the boundary, not the technique:** shape-based attribution is
  fine for a monitor and is not a foundation for a rail that routes orders.
- The general multi-strategy pattern (per-strategy sub-accounts, or an
  order-management layer that owns *all* strategies in one account and allocates
  fills to them) both point the same way: either silo the account, or centralize
  every strategy behind one allocator. Argus is explicitly single-strategy and
  multi-strategy orchestration is out of scope, which leaves siloing.

---

## Options evaluated

### A — Dedicated tastytrade account

**What changes.** Operationally: open (or designate) an account, fund it with the
strategy's allocation, and adopt one standing rule — nothing else trades it,
including by hand. In code: one new config key, one live-guard, one line in
`__main__`. Nothing else in `src/` is touched.

**Invariants.** Fixes **all eight** items. Reconciliation, the concurrency cap,
the breaker's P&L basis, the relative size rail, order reconciliation, the
book-agreement page and the symbol collision are all resolved by the same fact:
the assumption becomes true. No rail is weakened, reworded, or re-argued.

**Effort.** Roughly a day including tests and the runbook, most of it the config
key's validation and the live-guard test. Blast radius: `config.py`,
`__main__.py`, `argus.live.toml`, `CLAUDE.md`, runbook. No migration, no schema
change, no new failure mode.

**Residual risk.** (i) The account must *stay* dedicated — a hand-placed trade
in it hard-halts the next restart. That is loud and correct, but it is an
operator discipline. (ii) Cash movements into or out of the account during a
trading day contaminate the NLV-delta P&L (item 3). (iii) Capital is siloed;
moving the allocation later is an internal transfer, which changes NLV (and
therefore the relative rail's bound, but neither absolute rail nor the welded
invariant, both of which are in dollars) — and must be done **outside a trading
day**, not merely outside a session, because a same-day restart deliberately
rehydrates the day's original anchor.

**Reversibility.** The config key is not dead weight if the account situation
changes — pinning the account is correct on any multi-account login, and it
removes the `accounts[0]` lottery regardless.

### B — Strategy-scoped accounting layer

**What changes.** Reconciliation compares only in-scope symbols and treats the
rest as *invisible* rather than *unexpected*; the breaker runs on attributed
realized+unrealized P&L with a 0.0 anchor; the concurrency cap drops or filters
its broker-leg term; the relative size rail is dropped or repointed at a
configured allocation notional; the book-agreement check compares in-scope legs
only. Files: `reconcile.py`, `risk/models.py`, `risk/snapshot.py`, `risk/gate.py`,
`risk/breaker.py`, `risk/rehydrate.py`, `account/cache.py`, `account/refresher.py`,
`execution/watchdog.py`, `config.py`, plus new keys and probably a migration for
`FillInfo.fill_price` (the deferred prerequisite from Q4).

**Invariants.** Fixes 1, 2, 4, 6 and *partly* 3. Does **not** fix 5 (a foreign
working order still halts unless order reconciliation is scoped too, which is
easier — the identifier is minted — but is one more rail scoped by Argus's own
belief). Does **not** fix 7 at all.

**Effort.** Weeks, not days — and the expensive part is not the code, it is that
each of four rails needs a new failure-mode argument written to the standard the
existing records are written to.

**Residual risk — the new worst case, stated plainly.** With a ledger-scoped
predicate: **a real Argus fill that never books reads as somebody else's
position, and reconciliation reports CLEAN** — a live 0DTE spread with no stop,
no page, and a green `/status`. With a product-scoped predicate: a foreign SPXW
leg is claimed as Argus's and marked into the breaker's P&L, while the halt it
was supposed to avoid fires anyway. And the reconciliation non-negotiable would
have to be rewritten from "the broker is the only source of truth" to "the broker
is the only source of truth about the positions Argus claims" — which is a
materially weaker sentence, because *what Argus claims* is exactly the thing the
rail exists to check.

**Reversibility.** Poor. If Argus later gets its own account, every scoped
predicate becomes dead weight that still has to be maintained and reasoned about
on every subsequent change, and the weakened non-negotiable does not
automatically re-strengthen.

### C — Hybrid

Dedicated account for capital, breaker and NLV rails; scoped accounting only
where a dedicated account is still not enough (e.g. the account must
occasionally hold a manual hedge). **Only relevant if A has a partial blocker** —
for instance, tastytrade refusing a second individual account and an entity
account being unacceptable. It inherits B's rail-weakening for whatever it
scopes, so it should scope as little as possible: realistically, only
reconciliation's `unexpected_at_broker` for a named, configured allow-list of
symbols, never a predicate.

**Not recommended unless A is blocked.** If it becomes necessary, the allow-list
form (explicit symbols an operator typed) is strongly preferable to a predicate:
it is auditable, it cannot silently widen, and it fails closed on anything not
named.

### D — Do not run live on a shared account (enforced precondition)

**What changes.** `config.py` refuses `environment = "live"` unless
`broker.account_number` is set. The "empty-or-Argus-only book" half needs **no
new code**: the process-start reconcile already refuses to resume on any
`unexpected_at_broker` row, in live, fail-closed, with the kill switch engaged
and the process parked and serving. D is therefore mostly *making an existing
implicit precondition explicit and legible*.

**Invariants.** Fixes nothing by itself — it converts a confusing failure into a
stated requirement, and forces A operationally.

**Effort.** Hours. **Reversibility:** total.

**Residual risk.** The error message is the whole product. A live halt that says
"reconciliation failed with 7 discrepancies" when the cause is "this account is
not dedicated to Argus" costs an operator an hour at exactly the wrong time.

---

## Recommendation

**A, enforced by D.** Open or designate a dedicated tastytrade account, pin it
with `[broker] account_number`, and make `config.py` refuse live without it.

The reasoning, in the order it actually decides the question:

1. **Item 7 has no fix.** If anything else in the account can trade SPX or SPXW,
   Argus cannot tell its own leg from the other's, at the strike where two
   spreads meet, which is the *common* case at `signal.width = 5.0`. Every option
   except A must exclude that by policy. Once a policy is being written anyway, A
   is the strictly stronger policy and the only one that stays **verified** —
   because the rail that checks it is the rail B removes.
2. **B's breaker has a deferred prerequisite.** Its P&L basis is
   `entry_credit`, measured 9–18% low, and the fix is deferred. Building the
   day's stop-trading rail on a known-wrong number, on the account where money is
   real, for the first time it has ever run, is not a trade worth making.
3. **NLV delta is a complete measure on a dedicated account and an incomplete
   one everywhere else.** It catches fees, settlements and Argus's own unbooked
   fills; an attributed figure catches only what Argus already believes.
4. **Cost asymmetry.** A is one config key against four rewritten rails and one
   permanently weakened non-negotiable. The operational cost of A — a second
   account to fund and monitor — is a bounded, reversible, well-understood cost.
   B's cost is unbounded reasoning debt on the safety surface.
5. **A is reversible; B is not.** If the account picture changes, A's key is
   still correct. B's predicates are permanent surface area.

**What A explicitly does not solve, and must become operating rules:**

- **Nothing else trades the Argus account, including by hand.** A violation
  hard-halts the next restart (loud, correct, and a lost trading day).
- **No deposits, withdrawals or internal transfers while a trading day is
  live** — they read as P&L to the breaker, and a same-day restart deliberately
  re-uses the day's original anchor, so the contamination is not restart-clearable.
  Move capital on a non-trading day.
- **Buying power in the dedicated account must cover two spreads' margin** with
  room, or a BP rejection retires candidates one at a time against the open
  "back off on consecutive terminal rejections" decision. Sizing the account is
  Richard's call; naming the coupling is this record's.

**Residual risks accepted by recommending A:**

- tastytrade may refuse a second individual margin account (unverified). Fallback
  is an entity/trust account at higher friction, or option C.
- A dedicated account starts with a small NLV, which makes the relative size rail
  (`position_size_pct = 0.02`) bind *hard*: at 2%, a $1,250 max loss needs an NLV
  of $62,500. Below that, `size_contracts`' `by_pct` term goes to 0 and the
  `max(1, ...)` floor sizes one contract, which `PreTradeGate` then **denies** on
  `size_cap_exceeded` if the single-lot max loss still exceeds 2% of NLV. **This
  is a real, foreseeable "funded the account and nothing trades" failure.** It is
  a *funding* decision, not a code change — but it must be on the checklist.
- The dedicated account is a new operational surface: its own statements, its own
  1099, its own funding, its own thing to remember to monitor.

---

## The change, as a design (not a patch)

### 1. A new config key: `[broker] account_number`

```toml
[broker]
# WHICH tastytrade account this process trades. Required under
# environment = "live"; optional elsewhere, where absent means accounts[0].
account_number = "5WX00000"
```

- **Type** `str | None` on `BrokerConfig`; absent/None keeps today's
  `accounts[0]` behaviour for shadow and sandbox.
- **Live guard** in `Config.load`, in the same seam and the same style as the two
  that already exist — `config.py:970` (`account_stream_max_age_s` under live)
  and `config.py:1036` (`audit.synchronous` under live):

  > `environment='live'` requires `broker.account_number`: Argus's account-derived
  > rails (reconciliation, the concurrency cap, the daily-loss breaker's NLV
  > anchor) all read WHOLE-ACCOUNT numbers and treat them as Argus-only, so the
  > account must be dedicated to Argus and must be named rather than inherited
  > from whichever account the login lists first.

  **Do not fold this guard in with either existing one.** The three refuse live
  for three unrelated reasons; sharing a branch is the same mistake the
  `account_stream_max_age_s` comment already warns about ("never let the two
  staleness bounds share a guard; only one of them is a cert artifact").
- **Why config, not env.** It is not a secret, and it must be reviewable in the
  bind-mounted file next to `server.bind_host` — the deployed artifact should
  state which account it trades. Credentials stay in env; this is not one.
- **Why `[broker]`, not `[execution]`.** It selects which account the broker
  *session* addresses. Both `startup_reconcile` and `run_session` need it before
  any execution object exists.

### 2. One line of wiring

`__main__.main()`:

```python
app = Application(config, account_number=config.broker.account_number)
```

Nothing downstream changes: `Application._account_number` already threads to
`session.startup_reconcile(account_number=...)` (`session.py:618-619`) and to the
per-day `run_session` (`session.py:916-917`), both of which filter the account
list and raise `SessionAborted` — permanent, never retried on the reconcile
budget — when no account matches. `test_reconcile_unreachable.py` already covers
that path (`test_a_wrong_account_number_stays_a_permanent_session_aborted`).

### 3. Tests the change owes

- `environment = "live"` without `broker.account_number` raises `ConfigError`;
  shadow and sandbox without it still load.
- The three live guards are independent: each fires on its own with the other two
  satisfied.
- `__main__` passes the config's account number through to `Application` (a
  structural/AST-style test in the spirit of the existing ones, so a future tidy
  cannot silently drop the argument and restore the `accounts[0]` lottery).

### 4. Diagnostics — optional, recommended, changes no verdict

When a live reconcile refuses and **every** position discrepancy is
`unexpected_at_broker` on a symbol with no Argus ledger row, add one line to the
alert and the audit payload naming the likely cause:

> N broker position(s) Argus has no record of. If this account is shared, it is
> not supported: see `docs/decisions/2026-08-31-live-account-is-not-dedicated.md`.

Diagnostic only. It must not change the verdict, the grade, or the halt.

### 5. `CLAUDE.md` non-negotiable wording

Under **Broker is the only source of truth**, add — as a stated premise, before
the existing text, since the existing text is only sound given it:

> **The traded account is DEDICATED to Argus, and that is a premise of this
> rail, not a convenience.** Every account-derived number Argus reads is a
> whole-account number treated as an Argus-only number: reconciliation compares
> the broker's entire position map against a table only Argus writes, the
> concurrency cap counts the account's legs, the daily-loss breaker measures
> whole-account NLV delta, and the relative size rail is a percentage of
> whole-account NLV. A single foreign position makes all four wrong at once and
> hard-halts the process at the first reconcile. `[broker] account_number` names
> the account and `config.py` REFUSES `environment = "live"` without it; the
> account must hold nothing Argus did not place, and no cash may move into or out
> of it during a trading day (`DailyPnl` reads a transfer as P&L, and a same-day
> restart re-uses the day's original anchor).
> → the account is not dedicated: `docs/decisions/2026-08-31-live-account-is-not-dedicated.md`

Everything already in that section stays verbatim.

### 6. Runbook

`docs/argus-deployment-runbook.md`, in the cutover section:

1. Read the dedicated account's number from `my.tastytrade.com` (`tools/account`
   refuses live configs by design and is not the path here).
2. Set `[broker] account_number` in `argus.live.toml`. **The config deploys WITH
   the image** — it is a new REQUIRED-under-live key, the same coupled deploy
   `[reconcile]`, `watchdog.exit_unattempted_max_s`, `book_confirm_ttl_s`,
   `flatten_before_close_s` and `unmanaged_order_max_s` already force.
3. Confirm in the tastytrade UI that the account holds **nothing**, and that no
   working orders sit in it.
4. Confirm the account's NLV against `position_size_pct` — at 0.02, an NLV under
   $62,500 makes the relative rail deny a single-lot $1,250 position.
5. Start. A clean `reconcile.clean` at process start is the confirmation; a halt
   naming `unexpected_at_broker` rows means step 3 was not true.

---

## Live-flip checklist — what this adds

Three line items, all verified the same day as the flip:

- [ ] **`[broker] account_number` is set in `argus.live.toml` and names an
      account dedicated to Argus.** Config refuses live without it; the value is
      not inherited from `accounts[0]`.
- [ ] **That account holds no position and no working order that Argus did not
      place** — checked in the tastytrade UI immediately before the flip, and
      confirmed by a clean `reconcile.clean` at process start.
- [ ] **The account's NLV clears `max_loss_per_position / position_size_pct`**
      ($62,500 at today's 1250 / 0.02), or the relative size rail denies every
      entry.

And two standing operating rules for `README.md`'s daily rhythm:

- Nothing else trades this account, **including by hand**. A manual trade in it
  hard-halts Argus at the next restart.
- No deposits, withdrawals or internal transfers on a trading day. `DailyPnl`
  cannot distinguish a transfer from P&L, and a same-day restart re-uses the
  day's original anchor rather than re-reading NLV.

---

## What this record does NOT settle

- **Whether tastytrade will grant a second individual margin account.**
  Unverified; Richard to confirm. An entity/trust account is the fallback, and
  option C is the fallback if neither is available.
- **How the account is funded, and with how much.** A capital-allocation
  decision. This record only names the coupling to `position_size_pct` and to
  two spreads' margin.
- **The breaker's cert-inertness.** Still a separate live-flip gate. A dedicated
  account makes the NLV basis *correct*; it does not make it *exercised*.
- **`OpenSpread.entry_credit` being the limit rather than the fill price.** Still
  open, still deferred. Under recommendation A it does not reach the breaker
  (which reads NLV), which is one more reason the recommendation is A.
- **Whether entry should back off on consecutive terminal rejections.** Still an
  open decision. A shared account would have raised its priority; a dedicated one
  leaves it where it is, with margin-driven rejections one more way to reach it.
