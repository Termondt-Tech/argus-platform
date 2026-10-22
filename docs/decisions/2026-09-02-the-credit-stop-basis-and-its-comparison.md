# The credit stop was measured against two wrong numbers (2026-09-02)

**DESIGNED, SIGNED OFF AND BUILT the same day, as TWO independent commits.** D3 is
`src/` only. D5 carries **migration 0005** and no config key, so it too deploys with
the image alone — neither joins the coupled `watchdog.exit_walk_min_steps` deploy.

Incident: [the second v0.3.13 day](2026-09-01-second-live-fire-day.md).
Design gate: [`argus_credit_stop_wrong_numbers_design.md`](../tasks/argus_credit_stop_wrong_numbers_design.md).
Prompt: [`argus_credit_stop_wrong_numbers_prompt.md`](../tasks/argus_credit_stop_wrong_numbers_prompt.md).

Two of the five `credit_multiple` stops on 2026-09-01 were not market events.
Neither is a judgement call about the multiple; both are the same decision point
reading a number it should not have been reading.

---

## D3 — the comparison fired on 4.4e-16

`call-7670-7675`, from the 09:34 ET page verbatim:

```
watchdog_triggered [20260901-call-7670-7675]
  {"cost_to_close": 1.6000000000000005, "credit_stop": 1.6,
   "entry_credit": 0.8, "fired": ["credit_multiple"], "spot": 7661.165}
```

SPX settled at 7637.705; the spread was 7670/7675 calls and expired worthless.
Realized **−160.00** on a breach of **4.4e-16** — eleven orders of magnitude below
the 0.05 tick, and below any price difference that can exist.

### Where the precision goes, and why `Decimal` is not the fix

The obvious reading — `credit_stop` is `float(entry_credit) * credit_multiple` and
`credit_multiple` is a plain float from config — **is wrong**, and it matters
because it is the reading that leads to the wrong fix. `float(Decimal("0.8")) * 2.0`
is exactly the double nearest 1.6. The printed `1.6` is 1.6 to the last bit.

The noise is in `cost_to_close`: `spread_mid` over four float quotes, two divisions
and three additions, landing a half-ULP off. So **keeping `Decimal` all the way to
the comparison changes nothing** — `Decimal(str(1.6000000000000005)) >=
Decimal("1.6")` fires just the same. Restoring exactness would have to happen
upstream, in `spread_mid`, which the ENTRY path shares and which prices the exit
order at `watchdog.py`'s `credit=market.cost_to_close`. Three consumers, one of
which is a threshold test.

**So the boundary drawn is: a quantity that is displayed, priced from and compared
against keeps the type its producers use; the comparison that turns it into a
verdict owns its own tolerance.**

### Built

`TriggerThresholds.credit_breached(cost_to_close=, credit_stop=)`, a sibling to
`underlying_breached`, requiring `cost_to_close - credit_stop >= PRICE_EPSILON`.
`PRICE_EPSILON` is a new public constant in `zerobid.py`, **derived from that
module's existing `_INPUT_PRECISION`** so the two cannot drift: one millionth of a
point. The sizing argument is the price grid, not `sys.float_info` —

- a leg mid is `(bid+ask)/2` on a 0.05 tick, so it lands on a **0.025 grid**, and
  `spread_mid` differences two of those;
- the smallest difference `cost_to_close` can express against anything is therefore
  **0.025 points, $2.50 a contract** — 25,000× the tolerance;
- the arithmetic's own error is ~1e-16, ten orders of magnitude below it.

It is the same quantity, with the same argument, that `zerobid._INPUT_PRECISION` and
`tools.mark._NOISE_FLOOR` (2026-08-04) already settled on. A third independently
chosen epsilon would have been a third thing to keep in agreement.

### The direction, sized

The stop now fires **strictly less often** — monotonically. That is a real change to
a safety rail and it is stated as one. It is the removal of an answer that was never
meaningful, and at this magnitude the two are the same thing:

- **Where `credit_stop` is not on the achievable grid** — the common case, and the
  universal one once D5 makes the basis a weighted average — the boundary is a value
  `cost_to_close` can never take, and the tolerance changes **no outcome at all**.
- **Where it IS on the grid** (0.8 × 2.0 = 1.6, the 2026-09-01 case) the old rule's
  answer was decided by rounding: the same market state summed from different quotes
  gives 1.5999999999999996 or 1.6000000000000005. A rail whose verdict at its own
  threshold is a coin flip has no defined behaviour there.
- **Cost of defining it this way**: the position runs to the next achievable price,
  at most 0.025 points — **$2.50 a contract against $500 of defined risk on a
  5-wide, 0.5% of max loss.**

"Inclusive at the mark" is the house convention everywhere else (`seconds_to_flatten
<= 0.0`, `underlying_breached`) and it survives wherever the threshold is a value the
input can actually equal. It cannot survive here, and it was never a promise to fire
on an equality that is not observable.

### The sibling comparison does NOT get this, and a test says so

`underlying_breached` keeps its bare comparison. The reason is structural, not a
judgement about magnitude:

- **No equality by construction.** `credit_stop` is derived from `entry_credit` by
  multiplication, so it lands on the same grid the input does and
  `cost_to_close == credit_stop` is a RECURRING EXACT STATE. Here the sides come
  from unrelated places — an integral SPX strike plus a configured buffer, against a
  mid of the index quote that prints at 7661.165. Landing on the boundary is
  measure-zero, not a design feature.
- **The observation moves**, and `sustained_s` requires the condition to hold for
  seconds regardless.
- **At the boundary the answer is not wrong**: spot exactly at `strike − buffer` IS
  at the trigger level.
- **A tolerance would only delay the backstop** — the trigger that still fires when
  the option market cannot be priced, and the one 2026-08-13 went to some trouble to
  keep live when the credit trigger goes blind.

### The suite was firing on 2.2e-16 too

`_triggered_and_priceable`, the helper the whole watchdog suite used to mean
"triggered", set the short leg 2.00/2.20 and the long 0.30/0.50 against a 0.85
credit. `2.10 − 0.40` is `1.7000000000000002` and the stop is `1.7`. **Twenty-three
tests were passing on a 2.2e-16 breach** — a live specimen of the defect, in the
fixture, unnoticed. They now use a genuine tick; the original quotes are kept as the
boundary case.

---

## D5 — `entry_credit` was the LIMIT, not the fill

`put-7630-7625` recorded `entry_credit: 0.45` (the walk's final limit) and stopped at
`cost_to_close: 0.925` against `credit_stop: 0.90`. Both contracts collected **0.50**
— proven from fill prices, not reconstruction (2.05/1.55 and 2.15/1.65). On that
basis the stop is 1.00 and **it is never stopped out at all**.

### The correction to `store.py`'s stale premise — required by the prompt

`OpenSpread`'s docstring said `positions.average_open_price` is *"0.0 whenever the
broker reported no per-fill prices, **which is every case chunk 1 actually
observed**"*, and concluded that capturing realized prices meant new machinery. **That
sentence was true when written and is now false, and it is the one that made the fix
look expensive.** Real per-fill prices have been arriving throughout:

- `models._map_fills` maps `PlacedOrder` leg fills into `LegFill`;
- `OrderLeg.fill_price(start, end)` averages the cumulative slice being booked;
- `store._apply_leg` has been calling it on **every applied fill** and passing the
  result into `_apply_position` and `AppliedFill.price`;
- `fills.py` writes that price onto the Tier 1 `order.fill` record — which is where
  the pass record read 2.05 / 1.55 / 2.15 / 1.65, and 4.50 / 3.00.

**Only the durability at spread level was missing.** The docstring is rewritten.

### Built

**Migration 0005** adds `order_legs.priced_quantity` and `priced_notional`,
accumulated in `_apply_leg` inside the same transaction as `applied_quantity` and
`positions`, from the `fill_price` that call already computes. Idempotent for free:
the write rides behind the existing `filled <= already` return.

The pair written is **exactly the pair the same call already emits on `AppliedFill`**
— `contracts` and `price` — so the columns are the durable running total of the
`order.fill` records. That is what makes the ledger's realized credit and
`shadow.outcome`'s reconstruction of it the same arithmetic over the same numbers,
including `fill_price`'s extrapolation where the broker priced part of a slice.

`_realized_credit` aggregates them **per LEG across every OPENING order in the trade
idea**, contracts-weighted, cash-signed by `LegAction.sells`.

### Why every other home loses

- **`positions`** is keyed by SYMBOL, and chained verticals net to zero at a shared
  strike — `signal.width = 5.0` on SPX's 5-point ladder makes that the common case.
  The same landmine as the concurrency cap's, and the reason the price column already
  there is documented as safety-irrelevant.
- **`orders`** is one row per BROKER ORDER, which is one rung of a walk. **1429812
  filled 1 contract and 1429813 filled 1**, so a per-order price still misses rung
  one. Both rungs happened to net 0.50 that day, which is what made newest-only
  right by luck; rungs at 0.55 and 0.45 give 0.45 against a truth of 0.50.
- **A new table** buys nothing: `order_legs` is already keyed `(broker_order_id,
  symbol)`, already the per-trade-idea idempotency ledger, already written in the
  same transaction as `positions`, and already selected by the one query
  `_open_spreads` runs.

**The cash sign is `LegAction.sells`** — the single existing definition, the one
`signed_leg_delta` reads for the position sign and `shadow.marker` reads for the fill
sign. It is the half of a leg action **the broker does not relabel**: an opening BUY
that comes back `Buy to Close` on a chained book is still a buy (2026-08-25). Nothing
in this path reads `opens`/`closes` or a direction.

### The one case this makes worse, stated rather than slipped in

An idea closed and re-entered the same day would now BLEND the closed entry's credit
into the live one, where newest-only got it right. It is prevented upstream —
`_attempted` claims a candidate before `DuplicateOrderGuard` runs, and a re-entry
would mint the same deterministic `external_identifier` — and it has never been
observed, while the walk-rung case is the normal result of any walk that reprices and
was wrong. A test pins the blend by name, so if it ever becomes reachable there is a
test that says what to go and fix.

### Provenance travels with the number

`entry_credit` CHANGED MEANING, so a payload carrying it without its basis is a
number an operator will read as the other one. `OpenSpread` gains
`entry_credit_basis`, `entry_limit`, `fills_credit` (what the fills said, **whether
or not it was used**) and `priced_contracts`, exposed as one block by
`credit_payload()` and splatted into all six hand-built watchdog dicts.

**An AST sweep asserts no module outside `store.py` writes an `"entry_credit"` dict
key by hand**, plus a second one asserting the six splats are present — absence of
the bad shape is not presence of the good one. This is the same shape that dropped
`reject_reason` from an exit page one week earlier, held the same way, because a
convention is what failed there.

### A non-positive realized basis keeps the position WATCHED

The first draft sent it to `malformed`, matching the existing limit guard. **That was
wrong, and the gate caught it.** A malformed spread is not "watched with a caveat" —
it leaves `spreads` entirely, so it gets no credit trigger, no underlying trigger,
**no `FLATTEN_BEFORE_CLOSE`**, no `/status` row (the known-unbuilt visibility gap),
and no concurrency slot.

And the two cases differ in kind. A non-positive **limit** is a corrupt record —
`_limit_price` signs OPEN positive, so `build_order` cannot produce one. A
non-positive **realized** basis leaves the symbols, quantities, actions and limit all
coherent. So the guards split:

| condition | verdict |
|---|---|
| `entry_limit <= 0` | **malformed**, unchanged, and asked FIRST — what the legs filled at says nothing useful about an order whose own limit is corrupt |
| both legs priced, realized `<= 0` | **watched on the limit**, `CreditBasis.LIMIT_FILLS_INCOHERENT` |
| either leg unpriced | **watched on the limit**, `CreditBasis.LIMIT` |

Falling back to the limit is fail-CLOSED in the trading sense: the limit basis is
LOW, so the stop it leaves behind is TIGHT.

**A new state is worse than useless if nothing surfaces it**, which is the only thing
that makes this better than the malformed routing it replaced. So it pages once per
position (`watchdog.entry_credit_incoherent`, Tier 1 + alert), carrying
`external_identifier` and `candidate_id` at the TOP level — nested inside `position`
the 2026-08-21 per-position throttle folds it in with the next unrelated `watchdog_*`
alert — and carrying `fills_credit`, because a page saying "incoherent" without the
number is a page nobody can act on.

### Sizing the loosening — required, and smaller than the percentage suggests

| Date | Candidate | limit | realized | gap | % |
|---|---|---|---|---|---|
| 2026-08-19 | — | 0.55 | 0.60 | **0.05** | +9.1% |
| 2026-08-20 | — | — | — | — | +18% |
| 2026-09-01 | `put-7630-7625` | 0.45 | 0.50 | **0.05** | +11.1% |
| 2026-09-01 | `put-7615-7610` | 0.75 | 0.775 | **0.025** | +3.3% |
| 2026-09-01 | `put-7595-7590` | 0.55 | 0.60 | **0.05** | +9.1% |

**In absolute terms the gap is one half-tick to one tick, every time.** That is what
price improvement on a resting limit order looks like; the 3-18% range is that same
absolute gap divided by a small credit, which is why the percentage looks alarming
and moves around. Characterizing it as a percentage was the misleading part.

So the stop level moves later by `credit_multiple × gap` = **0.05 to 0.10 points,
$5 to $10 per contract** at the current 2.0 multiple.

**What that does to `max_loss_per_position` exposure: nothing.** The credit stop does
not bound loss and never has — the loss is bounded by the spread WIDTH, defined risk,
enforced in `orchestrator.size_contracts` and re-checked in `PreTradeGate` (every
size rail enforced twice, the tightest governing). Moving the stop later raises the
EXPECTED loss on a position that does stop out; it cannot move the MAXIMUM, which
stays `width × contracts × 100 − credit`. On a 5-wide that is $500 a contract, so
$5-$10 is **1-2% of max loss**. The welded invariant
`max_concurrent_positions × max_loss_per_position ≤ daily_max_loss` is untouched:
none of its three terms is a function of the stop.

### What the `credit_multiple` prohibition becomes

It narrows rather than lifts:

> Conclusions about `credit_multiple` may be drawn only from positions whose
> `entry_credit_basis` is `REALIZED`. A `LIMIT` or `LIMIT_FILLS_INCOHERENT` position
> is **excluded**, never corrected — its basis is low by an unknown amount, and
> reconstructing it after the fact would be manufacturing the very number the column
> exists to record.

**Still not possible, and unchanged by this: sizing `credit_multiple` off cert.**
Fixing the basis removes a KNOWN bias; it does not make sandbox fills realistic —
orders match inside tastytrade's own sandbox and quotes are delayed. **A number
measured in cert still cannot size a change on its own.** What this buys in cert is
that the mechanism can be validated there and the bias will not contaminate the live
dataset when one exists.

Immediately: `tools.desk`'s note that it discards the watchdog's `entry_credit` in
favour of the fill-derived one becomes a **cross-check** rather than a correction —
on a `realized` record the two are the same arithmetic down two different pipes, so a
disagreement is a bug worth chasing, which is the posture `desk` already takes toward
`mark`.

### No backfill

Pre-0005 rows take the column defaults, `priced_quantity = 0`, which reads back as
`LIMIT` — bit for bit the behaviour they already had, now labelled. Sufficient
because Argus trades **0DTE only**, so every position predating the deploy settles
within its own session; and right because a backfill would have to reconstruct ledger
state from the audit log, making a second writer of the thing the ledger is
authoritative over. **A mid-session deploy leaves that session's already-open
positions on the limit basis** — they keep the slightly-tight stop, and nothing
regresses.

---

## Not one shared function with `shadow.outcome`, deliberately

`_net_legs` already reconstructs a realized credit and `desk` reuses it rather than
scoring a second way. The runtime path is not made to call it, and the reason is that
they are **not the same dataset**: `outcome` reads the AUDIT LOG (a record, written
after the fact); `store` reads the LEDGER (the transactional state written in the same
transaction as `positions`, and what every other runtime answer about a held spread
comes from). Retrofitting `_net_legs` onto a `Decimal`-returning helper would also
change the numbers `mark` and `desk` print — a reporting change smuggled inside a rail
fix.

What IS shared is the thing that actually drifts: `LegAction.sells`, one definition.
The weighting rule is pinned by an **equivalence test** — the same fills through both
paths must produce the same credit — which is strictly stronger than sharing the last
step, because it also catches a divergence in the accumulation.

## What is explicitly NOT changed

- `credit_multiple` itself. This makes that analysis possible; it does not perform it.
- What closes a position: the trigger set, the debounce, the one-attempt rule,
  `ExitExecutor.close`, the walk, the width-capped cross.
- `staleness.*`. Nothing here reads or moves a staleness bound.
- `WalkResult.filled_contracts`' across-rungs under-count — a different instance of
  the walk-versus-identifier distinction, on the reporting side.
- `infer_direction` and the relabelled closing legs. Direction is never read from a
  leg action anywhere in this change.
- `/status`, which carries no held spread's `entry_credit` for any position. The page
  and the Tier 1 record are the channel. (Noted in passing and raised separately: the
  spent-exit row on `/status` drops its mark entirely — `session.py` never copies
  `cost_to_close`/`credit_stop`/`still_triggered` out of `SpentExitAttempt`, so it
  always renders "mark: unpriced".)

## Ship order

D3 shipped first and alone; D5 behind it. The argument is **risk isolation, not
attribution** — an earlier draft claimed the latter and it was wrong, because both
effects are reconstructible per position from records that already exist
(`cost_to_close`, `credit_stop`, and now `entry_limit` and `entry_credit_basis`). D3
is three lines in a pure comparison and cannot break anything; D5 touches the write
path of the only writer of `positions`, adds a migration, and changes the read path
that decides which spreads are watched at all.

## Coverage

24 tests. Twelve mutations verified, each failing the suite: removing the tolerance,
widening it to a half-tick, flipping its sign, giving it to `underlying_breached`;
using the newest rung's price, weighting by rung instead of contracts, taking the
cumulative filled instead of the delta, blending a `None` price as 0.0, sending a
non-positive realized basis to `malformed`, aggregating across closing orders,
deriving the cash sign from `.opens`, and writing `entry_credit` by hand in one alert
dict.

The cumulative-vs-delta mutation **survived the first pass** and exposed a real gap:
nothing exercised a leg filling across two events on the same order — the proven
partial-fill case (2026-08-31, four leg movements over 3.07s). The test that closes it
is the one that catches it.
