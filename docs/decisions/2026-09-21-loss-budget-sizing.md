# Loss-budget sizing: the day's max loss is a bound on the whole day

**Status: BUILT 2026-09-21; DEPLOYED in v0.4.6 (Alshain), 2026-09-21** (`src/` + `tests/`
only, no config key, no migration; it shipped inside v0.4.6's coupled deploy rather
than alone, because that release also carried the mid-delta config key). Built as designed, with the open questions answered as
recommended and three deviations, each in the conservative direction — see
[As built](#as-built) at the end.

## The defect

`daily_max_loss` is supposed to be the most a trading day can lose. It is not. The
rail that was meant to guarantee it, the welded invariant

> `max_concurrent_positions × max_loss_per_position ≤ daily_max_loss` (`1 × 950 = 950`)

bounds only what is **open at the same time**, checked once, against a day on which
nothing has been realized yet. It says nothing about positions opened **one after
another**. The daily-loss breaker does not close the gap: it blocks new entries at
−950 and cannot close what is already open.

So the real ceiling is: the day realizes losses up to just above −950, the breaker
has not yet tripped, one more full-size entry is allowed, and that position goes to
full width. At 2 contracts on a 5-wide that is about **−940 − 910 ≈ −1,850, roughly
twice the stated cap** (4.7% of the account).

This was found on 2026-09-21, the first live day with repeated stop-outs: five call
spreads in a rising market, each re-entered 2 to 25 seconds after the previous exit,
four stopped out for about −570 realized, with a fifth open whose full-width loss
(860) would have taken the day to about −1,460 with nothing tripping first. The kill
switch was pressed by hand. Every component did what it was specified to do; the
specification of "max loss" was wrong, and it was written and reviewed without this
being noticed.

→ [research note](../../RESULTS.md) §1 and §4.3

## The invariant that should hold

Define, at any moment during the trading day:

> **W = realized day loss so far + Σ remaining worst-case loss of every open position**

1. **The day's final loss can never exceed W at any moment.** Realized loss is
   final, and no open position can lose more than its worst case.
2. **Only an entry raises W.** An exit turns a position's worst case into a realized
   loss that is no larger. A mark moving changes neither term. Expiry settles a
   position at or inside its worst case.
3. **So if every entry is refused whenever it would take W past `daily_max_loss`,
   then W ≤ `daily_max_loss` holds after every event of the day**, by induction over
   the day's events, and the day's loss is bounded by `daily_max_loss` on **every**
   path: stops that overshoot, stops that fail, a broker outage, a position held to
   full width.

The bound needs no model of the market and no working exit. That is the property
that decides this design over the alternatives (below). The welded invariant is the
special case of this rule at the first entry of the day, when nothing is realized
and every slot is empty.

## Design

### Quantities

Each is computed for the current ET trading date.

**Premium cash today, from Argus's own ledger.** Every leg movement is a durable
Tier 1 `order.fill` record carrying `signed_delta` (+ long, − short) and `price`
(`execution/store.py:63`, written by `execution/fills.py:191`):

```
cash_today = Σ over today's order.fill rows of  −signed_delta × price × 100
```

For a closed trade idea this is its realized P&L before fees; for an open one it is
the credit collected. `signed_delta` is the sign the broker cannot relabel, so this
is immune to the leg-action relabelling recorded 2026-08-25.

**Worst case of what is open**, from the watchdog's scan, the same late-bound seam
`ledger_spread_count` already uses:

```
open_worst = Σ over open spreads of  width × 100 × contracts
```

That is the cost to buy each open spread back at full width. The credit collected
is already inside `cash_today`, so the two combine exactly.

**Two estimates of W, and the worse one governs.** This is the pattern the
concurrency gate already uses: take the higher of the broker-derived and
ledger-derived spread counts.

```
W_ledger = open_worst − cash_today
W_broker = −daily_pnl + Σ over open spreads of (width − cost_to_close) × 100 × contracts
W        = max(W_ledger, W_broker)        # W_broker only when daily_pnl_anchored
loss_budget = daily_max_loss − W
```

- **`W_ledger` is mark-free and exact** apart from fees, and it is the only estimate
  that works in cert, where `daily_pnl` is structurally zero. That makes this the
  first loss rail cert can actually exercise (once cert's entry path works again).
- **`W_broker` includes fees and the broker's marks.** It also guards against a
  ledger that is short a row.
- **An open spread with no current mark** uses `cost_to_close = 0` in `W_broker`,
  which overstates its remaining worst case: the conservative direction.
- **At `max_concurrent_positions = 1`, entries only happen when flat**, so both
  open-position terms are zero and the rule reduces to
  `loss_budget = daily_max_loss − max(−cash_today, −daily_pnl)`. The general form is
  still what gets built and tested, so raising the concurrency cap later cannot
  silently break it.

### Enforcement, twice

The non-negotiables require every size rail to be enforced twice, by the sizer and
by the gate.

- **Sizer** (`orchestrator.size_contracts`, `orchestrator.py:42`) gains a
  keyword-only `loss_budget` with **no default**, the same convention as the
  existing caps:
  ```python
  by_budget = int(loss_budget // max_loss_per_contract) if loss_budget > 0 else 0
  return max(1, min(by_pct, by_dollars, max_contracts_per_position, by_budget))
  ```
  A non-finite `loss_budget` is treated as 0. The `max(1, …)` floor is unchanged:
  the candidate is still sized to one lot and recorded, and the gate refuses it.
- **Gate** (`PreTradeGate.evaluate`, `risk/gate.py:67`) gains the reason
  **`daily_loss_budget_exceeded`** when `proposal.max_loss > loss_budget`. It is
  checked beside `position_max_loss_exceeded`, independently of whether NLV is
  readable. The absolute rails are never excused by an unreadable NLV, and neither
  is this one; `W_ledger` carries it.
- **`RiskSnapshot`** gains `loss_budget`, `worst_case_ledger`, `worst_case_broker`
  and `ledger_complete`, so every Tier 1 `gate.decision` records which estimate
  bound and by how much.

### Surviving a restart

`cash_today` is an in-memory accumulator, incremented where `order.fill` is written.
At `_run_session_day` it is **rebuilt from today's `order.fill` rows**, on the
`risk/rehydrate.py` pattern that fixed the breaker's same-day-restart defect
(2026-08-20). It reads the audit log, never the `positions` or `order_legs` tables,
which the sandbox reset wipes. An unreadable read fails the way the breaker's
rehydration does.

### Failure modes: every one fails closed

| Condition | Behaviour |
|---|---|
| `daily_pnl` unanchored | `W = W_ledger` alone |
| A today fill has `price = None` | `ledger_complete = false`; use `W_broker` if anchored, otherwise `loss_budget = 0` (no entries) |
| Ledger rehydration unreadable | Same as the breaker's rehydration failure |
| A held leg is unattributed or malformed (`/status` `unmanaged`) | `loss_budget = 0` |
| A working opening order (only reachable when `max_concurrent > 1`) | Counted at `width × 100 × contracts`, i.e. as if it filled at zero credit |
| Crash between the store commit and the `order.fill` write | The ledger is short one row. For a closing fill that understates `W_ledger`; `W_broker` covers it, which is why both exist |

### What it is not

- **Not a latch.** It is recomputed on every candidate and clears at the trading-date
  rollover with no human step. **It must never trip the kill switch**: kill-switch
  trips are durable since 2026-09-01, which would turn a daily limit into a
  permanent halt (CLAUDE.md, first standing landmine).
- **Not a replacement for the breaker.** The breaker is unchanged: it still latches
  at `daily_pnl ≤ −daily_max_loss`, still records `daily_loss.breach`, still pages.
  The budget refuses the entries that would make the breaker's number unreachable
  as a bound. Do not merge them.
- **No new config key.** It uses `daily_max_loss`, so the image deploys alone.
- **The welded-invariant config test stays.** It is no longer what keeps the day
  safe, but without it `max_concurrent_positions` would be a cap the budget
  silently overrides at 10:00.

## What it would have done on 2026-09-21

Rounded; per-contract max loss is `(5 − credit) × 100`.

| Before entry | W (≈) | Budget | Contracts allowed |
|---|---|---|---|
| Trade 1 | 0 | 950 | 2 (870 ≤ 950) |
| Trade 2 | 180 | 770 | **1** (2 × 435 = 870 > 770) |
| Trade 3 | 260 | 690 | **1** |
| Trade 4 | 330 | 620 | **1** |
| Trade 5 | 395 | 555 | **1** |
| After a 5th stop | ~480 | ~470 | 1 |
| After a 6th stop | ~560 | ~390 | **0: entries end** |

Realized through trade 4 would have been about −395 instead of −600, with 430 at risk
on trade 5 instead of 860, and **no path from there ends past −950** (to within fees
and the last entry's walk concession; see [As built](#as-built), open question 1).

In the 2026-09-21 simulation (six market models, 20,000 paired days each), the rule
moved expected day P&L by −2.4 to +1.8 dollars in every model with a premium and cut
the worst simulated day from about −1,100 to about −600. The simulation's paths are
continuous, so no simulated stop failed; the real benefit is in the failure cases it
could not show.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Status quo plus a manual `/kill` | Human latency. Argus re-entered 2–25 seconds after each exit on 2026-09-21. |
| `max_contracts_per_position = 1` | The interim measure. Halves EV on every trade, and a 10-wide spread (ladder gap) at 1 contract is still ~950 of worst case. Reduces the gap, does not close it. |
| A cap on stop-outs per day | Does not bound the failure tail: one −170 stop plus one unclosable 2-lot spread is ~−1,040. |
| The breaker flattens everything when it trips | Needs a working exit, which is exactly what fails in the outage cases (2026-09-03, 2026-09-14), and forces realization at the worst moment. The budget is decided before the risk is taken. |
| Halve `daily_max_loss` | Still twice the new cap. Changes the number, not the definition. |
| Cooldown or same-side lockout | Their EV effect changes sign with unmeasured market parameters (research note §4.2). A different question. |

## Testing

- **The invariant, directly.** A property-based test drives random sequences of
  entries (sized and gated through the real `size_contracts` and `PreTradeGate`),
  stops at random debits up to width, full-width failures and flattens, and asserts
  `W ≤ daily_max_loss` after every step.
- **Enforced twice.** Mutation checks: remove the sizer bound, or the gate reason,
  and the property test must fail.
- **2026-09-21 replay.** The table above, as a test.
- **Restart.** Rehydrating `cash_today` from audit rows reproduces the in-memory
  value, in the same restart-simulation style as the breaker's rehydration test.
- **Fail-closed paths.** Unanchored, unreadable, `price = None` and unattributed
  inputs each produce the behaviour in the failure-mode table.
- **Seam coverage.** New `RiskSnapshot` fields go through the gate payload's `_num`,
  and a new `/status` seam, if built, joins the converter sweep in
  `tests/test_session_status_seams.py`.

## Open questions for sign-off

1. **Fees.** `max_loss_per_contract` excludes fees, as the existing rails do, so the
   bound holds to within the fees of the day's trades (about $10 per 2-lot round
   trip). Accept that, or add a per-contract fee allowance? The latter needs either a
   constant or a REQUIRED config key, and a key forces a coupled deploy.
2. **`/status`.** Recommended: a "Loss budget" row showing the remaining budget and
   which estimate binds. Otherwise the rule's only observable is entries not
   happening, which is the problem the blackout row was built to fix.
3. **Notification.** Recommended: one Tier 1 record and one Slack notice on the
   first refusal of the day, on a rising edge like the breaker's breach. No repeats.
4. **`max_contracts_per_position` after it ships.** The budget makes 1-lot
   re-entries after a loss automatic, so 2 contracts becomes safe again. That is its
   own config decision, made after this is built and observed.

## As built

**Where it lives.** `risk/loss_budget.py` holds the whole rule: `DayLedger` (cash and
per-idea open legs from today's fills), `compute_loss_budget` (pure), `LossBudget`
(the result, carried on `RiskSnapshot.loss_budget` — a REQUIRED field, no default),
and the sizer/gate halves `contracts_within_budget` / `exceeds_budget`, which share one
half-cent `BUDGET_EPSILON` so a lot that fits exactly is never refused on float noise.
`FillConsumer` feeds the day's ledger after each `order.fill` write;
`risk.rehydrate.load_day_risk_state` rebuilds it from today's `order.fill` rows in the
same round trip as the breaker and the anchor; `SessionState.day_ledger` is required.
The watchdog exposes `open_exposure()` (off its scan) and `working_open_exposure()`
(off the same `get_live_orders()` poll the concurrency count uses).

**Open questions, answered.**
1. **Fees: accepted.** The bound holds to within the day's fees on the ledger
   estimate; in live the broker estimate carries them. No fee key.
   **A second tolerance, of the same size, accepted in the v0.4.6 pre-tag review
   (2026-09-21): the last entry's walk concession.** The sizer and the gate price a
   proposal at the signal's MID credit, and the entry walk may fill down to its
   floor (`entry_walk_away_pct`, 10%, tick-rounded). So an opened position can put
   up to (mid − fill) × 100 × contracts more at risk than the gate approved: about
   0.05–0.10 points, **≤ ~$10 per contract**, ≤ ~$20 at 2 lots. It does not
   compound: the ledger books every fill at its real price, so the next gate reads
   the true W and only the most recent entry's concession is ever uncovered. **So the
   bound is `daily_max_loss` to within the day's fees and the last entry's walk
   concession**, and it bites only when an entry nearly fills the remaining budget
   and then goes to full width. The invariant simulation fills at the proposal's
   credit and so does not model it. **The owed fix (next release, its own review):**
   price the BUDGET half alone at the walk's floor credit, through
   `PriceWalk.from_policy` / `WalkPolicy.for_entry` so the arithmetic cannot drift
   from the walk, in both the sizer and the gate, and never by changing
   `EntryProposal.max_loss`, which the per-position rails, `gate.decision` and the
   marker's records all read. The simulation should then fill anywhere in
   [floor, opening limit].
2. **`/status`: built.** A "Loss budget" row directly under the breaker (remaining of
   the cap, which estimate binds, both estimates) and a `loss_budget` JSON block. Red
   at zero or below, and "UNREADABLE — no entries" with the reason when W cannot be
   established.
3. **Notification: built.** `daily_loss.budget_exhausted` (Tier 1) and one
   `daily_loss_budget_exhausted` page on the first refusal of the day on a MEASURED
   budget. Refusals on an unreadable budget are not paged — they are transient by
   nature (the mid-fill window after every exit) and each is on its own
   `gate.decision` with `unknown_reason`. **Narrowed 2026-09-22:** the page now fires
   only when the proposed lot would not fit **even flat**, i.e. `remaining +
   open_worst_case + working_worst_case` is still short (`gate._ends_entries_for_the_day`).
   As first shipped it fired on the first live day Argus held a full-size position:
   a 2-lot 5-wide at 0.50 committed 900 of 950, and the candidate five seconds later
   was refused on the budget (correctly, and by `concurrent_limit` too) and paged
   "exhausted" (wrongly), because a budget tied up in an open position comes back
   when that position closes. The refusal is unchanged; only the page moved.
   **Narrowed again 2026-09-25 (DEPLOYED in v0.5.2, 2026-09-26; the first page to be read under it is still to come):** the flat check now asks about ONE ordinary lot,
   the smaller of the proposal's own per-lot max loss and `(signal.width −
   signal.min_credit) × 100` (450 in live), instead of the proposal as proposed.
   On 2026-09-24 at 14:25 ET (seq 159828–159830) it paged on a put 7680/7670.
   That candidate was 10 wide because a stale 7675 quote pushed the long leg out
   to `max_width` for one tick, so its lot cost 922.5 against 866.8 flat. A 5-wide
   lot still fit almost twice over, so entries had not ended, and the page used up
   the day's only notice on a ladder gap. The check now also measures per lot
   rather than at the proposal's full contract count, which is what the page's own
   definition always said. No config key; the refusal is unchanged. Pinned by
   `test_2026_09_24_a_ladder_gap_candidate_does_not_spend_the_days_page` in
   `tests/test_loss_budget.py`.
4. `max_contracts_per_position` stays open, as the record says.

**Deviations from the design, all conservative.**
- **`W_broker` uses a zero cost-to-close for every open spread, not the watchdog's
  mark.** `daily_pnl` carries the unrealized loss at the BROKER's mark, so the
  remaining worst case must be measured from that same mark; a watchdog mark from a
  different instant can sit above it and then UNDERSTATE W. Zero overstates W by the
  broker's mark — moot at `max_concurrent_positions = 1`, where entries happen flat,
  and worth revisiting (with a consistent mark) before the cap is raised.
- **`W_broker` is counted only in live**, not merely when anchored: cert's NLV is
  frozen, so an anchored cert `daily_pnl` is a structural zero, and "use `W_broker`
  when the ledger is incomplete" would have read as "nothing lost" there.
- **Open exposure is the per-idea UNION of the scan and the fill ledger**, the larger
  winning. The scan alone lags a new fill by up to one refresh while `cash_today`
  already holds its credit, which reads the credit as a gain — the fill ledger is
  updated in the same step as the cash, so that window cannot open. The ledger's own
  unpairable states (a half-booked multi-leg fill, a naked leg) are "cannot say",
  i.e. zero budget, as is a scan that has not run yet, an imbalanced spread, and an
  unmappable or unreadable working opening order.
- **The union has one exception: an idea the fill ledger has SEEN CLOSE today
  (every leg nets to zero, `Exposure.closed`) is not re-opened by the scan.** The
  scan lags in both directions. Right after an exit it still lists the spread while
  the ledger already holds the closing debit, and the larger-wins union counted the
  loss plus the whole width on a budget that read as known. That caused a false
  refusal, and it used up the day's one `budget_exhausted` page (found in the v0.4.6
  pre-tag review). Safe because the scan reads the positions table the same
  `store.apply` writes before each `order.fill`, so the two can disagree about a
  watched-closed idea only during that lag. An idea the ledger does not know, or
  holds open, is still taken from the scan / at the larger value.
- A single unparseable `order.fill` row is NOT a process halt: it makes the ledger
  unable to bound what is open, which zeroes the budget for the day. A database that
  cannot be read still halts, on the breaker's path.

**Testing, as built** (`tests/test_loss_budget.py`). The invariant is a seeded
randomized property (no new dependency) over 150 simulated days per mode — entries
through the real `size_contracts` and `PreTradeGate`, stops at any debit up to width,
full-width failures, a scan that lags the newest position, and in live a broker P&L
with fees — asserting the TRUE W never passes the cap after any event, with a hostile
sizer as well as the real one. Mutation: removing the gate's check breaks the
invariant (the sizer's one-lot floor opens past the cap); removing the sizer's bound
makes it propose lots the budget refuses while one would fit. Plus the 2026-09-21
replay table, restart rehydration through a real `FillConsumer` and audit log, every
row of the failure-mode table, the watchdog reads, and AST pins that the session
feeds the day's ledger and nothing in the rule can engage the kill switch.
