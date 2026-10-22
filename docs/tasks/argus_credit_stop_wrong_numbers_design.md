# Design — the credit stop is measured against two wrong numbers

Answers [`argus_credit_stop_wrong_numbers_prompt.md`](argus_credit_stop_wrong_numbers_prompt.md).
Background: [the second v0.3.13 day](../decisions/2026-09-01-second-live-fire-day.md).

**Status: SIGNED OFF 2026-09-02.** Two questions were raised at the gate and both
changed the design; see [Sign-off](#sign-off--what-the-gate-changed) at the end.

---

## Recommendation in one line

**D3 ships first and alone** — a comparison that requires the breach to be
*observable* (`cost_to_close - credit_stop >= PRICE_EPSILON`, one millionth of a
point, the quantity `zerobid` already owns), `src/` only, no key, no migration —
and **D5 ships behind it** as a separate deploy carrying migration 0005, because
it is the one that changes what a safety rail does with money.

Both were caused by the same line and both are read at the same decision point,
but they have nothing in common operationally: D3 removes an answer that was
decided by float rounding, D5 changes the number the rail is aimed at. Shipping
them together means one deploy where a stop that fires later cannot be attributed
to either change. Shipping D3 first also costs nothing to wait on — it is
unconditionally right and it closed a position yesterday.

---

## Ship order, explicitly

| | Change | Blast radius | Config | Migration | Deploy |
|---|---|---|---|---|---|
| **D3** | the comparison requires an observable margin | `watchdog.py` + one constant in `zerobid.py` | none | none | **image alone, immediately** |
| **D5** | the stop's basis becomes the realized credit | `store.py`, `models.py` (nothing), 6 alert dicts, `OpenSpread` | none | **0005** | image alone (migration is additive and runs at startup) |

Neither adds a config key, so neither forces a coupled config deploy. The
`watchdog.exit_walk_min_steps` coupling that is already outstanding is untouched
and unaffected — D5's migration runs from the image, and `apply_migrations` is
already called at startup.

**Why not one deploy — corrected at sign-off.** The first draft argued this on
ATTRIBUTION: D3 removes `credit_multiple` fires and so does D5, so one deploy
makes them indistinguishable in the first dataset ever measured on a correct
basis. **That argument is mostly wrong and does not carry the recommendation.**
Both effects are deterministic and reconstructible from records that already
exist: every `watchdog.check` carries `cost_to_close` and `credit_stop`, and
after D5 it also carries `entry_limit` and `entry_credit_basis` — so "would this
have fired on `entry_limit × credit_multiple`?" (D5) and "was the margin under
`PRICE_EPSILON`?" (D3) are both answerable per position, after the fact,
whichever way they ship.

The argument that does carry it is **risk isolation**. D3 is three lines in a
pure comparison and cannot break anything. D5 touches the write path of the only
writer of `positions`, adds a migration, changes the read path that decides
*which spreads are watched at all*, and edits six alert dicts. Staggering means a
surprise in the next session's watchdog points at one change without needing the
analysis. Cost: one more session where every credit stop fires 0.05-0.10 points
early — the thing that closed `put-7630-7625`.

**This does not gate the build.** The two are independent commits — D3 touches
`watchdog.py` and `zerobid.py` only, D5 carries the migration — so which image
they go out in stays a deploy-time call.

---

# D3 — a bare `>=` on two floats

## The fix

`TriggerThresholds` gains a sibling to `underlying_breached`:

```python
def credit_breached(self, *, cost_to_close: float, credit_stop: float) -> bool:
    """Has the cost to close reached the stop by an amount a price can express?"""
    return cost_to_close - credit_stop >= PRICE_EPSILON
```

`evaluate_triggers`' one call site (`watchdog.py:577`) becomes
`elif thresholds.credit_breached(cost_to_close=..., credit_stop=...)`. The
`is_finite` guard in front of it is untouched, and there is exactly one such
comparison in the tree (verified: `credit_stop` is read in `status.py` and
`tools/desk.py` for DISPLAY only; `still_triggered` is derived from
`true_reasons`, not recomputed).

`PRICE_EPSILON` is a new public constant in `zerobid.py`, derived from the
`_INPUT_PRECISION` that module already carries so the two can never drift:

```python
#: Below this, two prices are the same price. Same quantity, same argument as
#: `_INPUT_PRECISION` above: six decimals is far finer than any real price
#: increment (a tick is 0.05, a mid lands on 0.025) and far coarser than the
#: float noise a net mid accumulates.
PRICE_EPSILON = float(_INPUT_PRECISION)
```

### 1. Fix the comparison, not the quantities

`cost_to_close` has three consumers and only one of them is a threshold test:

- the trigger (this comparison),
- the MARK on `/status`, on the spent-attempt pages, on `watchdog.check`, and in
  `tools/desk.py`'s held-position valuation,
- the exit order's price (`watchdog.py:1069`, `credit=market.cost_to_close`),
  which is then `round_to_tick`ed by `build_order`.

Changing its type reaches all three, plus `zerobid.spread_mid`, which the ENTRY
path shares. Changing the comparison reaches one, in one file, with one call
site. The boundary is: **a quantity that is displayed, priced from and compared
against keeps the type its producers use; the comparison that turns it into a
verdict owns its own tolerance.**

There is also a correctness reason, not only a blast-radius one — see question 5.

### 2. The unit, and why

Points, absolute, `1e-6`. The argument is the price grid, not `sys.float_info`:

- SPX options quote on a **0.05 tick**. A leg mid is `(bid+ask)/2`, so it lands on
  a **0.025 grid**; `spread_mid` differences two of those, so `cost_to_close` lands
  on the 0.025 grid too.
- The smallest *observable* difference between `cost_to_close` and anything is
  therefore **0.025 points = $2.50 per contract**.
- `1e-6` points is **25,000× smaller** than that and **10 orders of magnitude
  larger** than the 4.4e-16 that fired the trigger on 2026-09-01. Nothing
  representable as a price can hide inside it, and no float error at this
  magnitude can escape it.
- It is the quantity this repo has already reasoned to twice and written down
  twice — `zerobid._INPUT_PRECISION` ("six decimals is far finer than any real
  price increment ... and far coarser than the noise") and `tools/mark._NOISE_FLOOR`
  (2026-08-04, the half-tick credit that broke ties at 2dp). Making it one constant
  with one argument is the point; a third independently-chosen epsilon would be a
  third thing to keep in agreement.

Not a *half-tick* tolerance (0.025): that is the size of a real price move, and a
tolerance the size of a real move would silently retire a genuine breach at the
grid point immediately above the stop. The tolerance must be unable to mask
anything, which means it must be far below the grid, not on it.

### 3. Direction, and why it is removal rather than loosening

The change makes the stop fire **strictly less often** — monotonically: every
`(cost_to_close, credit_stop)` pair that fires under the new rule fired under the
old. That is a real behaviour change on a safety rail and it is stated as one.

It is the removal of an answer that was never meaningful, and the two are the same
thing here because of where the boundary falls:

- **When `credit_stop` is not on the achievable grid** — the common case, and the
  universal case once D5 makes the basis a contracts-weighted average — the
  boundary is a value `cost_to_close` can never take. The nearest achievable
  values are ≥ 0.0125 away on either side. The tolerance changes **no outcome at
  all**; it is pure noise removal with zero observable effect.
- **When `credit_stop` lands exactly on the grid** — `entry_credit = 0.8`,
  `credit_multiple = 2.0`, stop = 1.6, the 2026-09-01 case — the old rule's answer
  at that grid point was **decided by float rounding, not by the market.** The same
  four quotes summed in a different order give 1.5999999999999996 (no fire) or
  1.6000000000000005 (fire). A rail whose verdict at its own threshold is a coin
  flip decided by ULPs has no defined behaviour there. Defining it is the fix.
- **The cost of defining it in this direction** is bounded and small: the position
  runs on to the next achievable price above the stop, **at most 0.025 points =
  $2.50 per contract of additional adverse mark.** On a 5-wide spread whose defined
  risk is $500 per contract, that is 0.5% of max loss.
- **The cost of leaving it** is the observed one: −160.00 realized on
  `call-7670-7675`, a spread that expired worthless, closed on 4.4e-16.

"Inclusive at the mark" is the house convention for every other bound here
(`seconds_to_flatten <= 0.0`, `underlying_breached`). It survives everywhere the
threshold is a quantity the input can actually equal. It cannot survive here,
because in float arithmetic `cost_to_close` cannot reliably *be* the mark — and
the convention was never a promise to fire on an equality that is not observable.

### 4. The sibling comparison — `underlying_breached` does NOT get this

**Answer: no, and the reason is structural, not a judgement about magnitude.**

`spot <= short_strike + buffer` / `spot >= short_strike - buffer` is the same
shape and a different problem:

- **No equality-by-construction.** In the credit case, `credit_stop` is derived
  from `entry_credit` — a quantity that lives on the *same grid* `cost_to_close`
  lands on — by multiplication. `2.0 × 0.8 = 1.6` is exactly an achievable
  `cost_to_close`, so `cost_to_close == credit_stop` is a **recurring exact state**,
  not a coincidence. In the underlying case the two sides come from unrelated
  sources: a strike from the chain (integral on SPX) plus a configured buffer in
  points, against a *mid of the index quote* which prints at 7661.165. There is no
  arithmetic identity making them equal; landing on the boundary is a measure-zero
  event, not a design feature.
- **The observation moves.** Spot is a continuously-updating quantity; if it did
  land exactly on the boundary, the next tick resolves it either way, and
  `sustained_s` requires the condition to hold for seconds regardless. The credit
  condition, by contrast, fires *deterministically and repeatedly* at the same grid
  point for as long as the market sits there — which is what turned one ULP into a
  closed position.
- **At the boundary the answer is not wrong.** Spot exactly at `strike - buffer`
  IS at the trigger level; firing there is the trigger doing its job. Firing at
  `cost_to_close == credit_stop` is also the trigger doing its job — the defect was
  never "it fired at the mark", it was "the mark is not a value this arithmetic can
  express".
- **Adding a tolerance there would delay the backstop** — the one trigger that
  keeps working when the option market cannot be priced, and the one the
  2026-08-13 pass went to some trouble to keep live when the credit trigger goes
  blind. There is no float-noise defect to trade against that.

This gets a comment on `underlying_breached` saying so, so the asymmetry is a
recorded decision rather than an oversight the next reader "tidies".

### 5. Where precision is lost — and why `Decimal` all the way does not fix it

`credit_multiple` is a plain `float` from `[watchdog]` config (`config.py`), and
`credit_stop` is `float(entry_credit) * credit_multiple`. That multiplication is
**not where the precision goes.** `float(Decimal("0.8")) * 2.0` is exactly the
double nearest 1.6 — the same double `Decimal("1.6")` would convert to. The
observed `credit_stop` printed as `1.6` because it *is* 1.6 to the last bit.

The noise is in `cost_to_close`: `spread_mid(short_bid, short_ask, long_bid,
long_ask)` = `floored_mid(...) - floored_mid(...)`, four float quotes through two
divisions and three additions, which lands a half-ULP off. **Keeping `Decimal` all
the way to the comparison therefore fixes nothing** — `Decimal(str(1.6000000000000005))`
is `Decimal("1.6000000000000005")`, and `>= Decimal("1.6")` still fires. Exactness
would have to be restored *upstream*, in `spread_mid`, on the shared entry path,
by quantizing the mid — which is the type change question 1 rules out, and which
would change the price the exit order is built at.

So: the tolerance is not the cheap alternative to `Decimal`. It is the **only**
one of the two that addresses the defect at all. `zerobid` already reached the
same conclusion for the same reason and solved it the same way — it normalizes to
`_INPUT_PRECISION` before rounding, precisely because "a net mid accumulated in
binary floats lands at 1.1249999999999998 when the arithmetic value is exactly
1.125".

---

# D5 — `entry_credit` is the limit, not the fill

## The premise correction, verified

`store.py:107-128` says `positions.average_open_price` is *"0.0 whenever the
broker reported no per-fill prices, which is every case chunk 1 actually
observed."* **That was true when written and is now false**, and it is the premise
that made this fix look expensive.

Verified against source and against the 2026-09-01 chain extract:

- `models._map_fills` maps `PlacedOrder` leg fills into `LegFill(quantity, price, …)`,
  dropping only unparseable ones.
- `OrderLeg.fill_price(start, end)` returns the quantity-weighted average over the
  cumulative slice `[start, end)` — precisely the contracts a given event newly
  books — or `None` when the broker sent no covering detail.
- `store._apply_leg:501` already calls it on every applied fill and passes the
  result into `_apply_position` and into `AppliedFill.price`.
- `fills.py:192` writes that price onto the Tier 1 `order.fill` record — which is
  where the pass record read `2.05 / 1.55 / 2.15 / 1.65` on 2026-09-01 (seq
  585450, 585451, 585474, 585475), and 4.50 / 3.00 elsewhere in the day.

So real per-fill prices arrive and are already computed, per leg, per booked
slice, inside the one transaction that writes `positions`. **The only thing
missing is durability at spread level.** The docstring's correction is a required
part of the decision record.

*(The local `argus.db` in this checkout is the Phase 0 shadow database and ends
2026-07-23; it holds no `order.*` records at all. The live chain is on the droplet
and was not accessed. The verification above is source-level plus the pass
record's own chain extract, which is the evidence the prompt cites.)*

## 1. Where the realized credit lives, and where it aggregates

**Storage: two new columns on `order_legs`, accumulated at WRITE time.**
**Aggregation into a spread-level credit: at READ time, in `_pair_spreads`.**

Migration `0005_order_leg_fill_prices.sql` (new file; 0004 is untouched):

```sql
ALTER TABLE order_legs ADD COLUMN priced_quantity INTEGER NOT NULL DEFAULT 0;
ALTER TABLE order_legs ADD COLUMN priced_notional REAL    NOT NULL DEFAULT 0.0;
```

`order_legs` is the right home and the alternatives lose:

- **`positions`** cannot hold it — keyed by SYMBOL, and chained verticals net to
  zero at a shared strike. `signal.width = 5.0` on SPX's 5-point ladder makes that
  the common case. Same landmine as the concurrency cap's; this is the reason the
  column already there (`average_open_price`) is documented as safety-irrelevant.
- **`orders`** (a spread-level `fill_price` column) is one row per BROKER ORDER,
  which is one rung of a walk. That is the trap the prompt names: 1429812 filled 1
  contract and 1429813 filled 1, and a per-order price still misses rung 1.
- **A new table** buys nothing `order_legs` does not already give: it is already
  keyed `(broker_order_id, symbol)`, already the per-trade-idea idempotency ledger
  via `applied_quantity`, already written in the same transaction as `positions`,
  and already selected by `_open_spreads`' one query. Two columns beside
  `applied_quantity` need no new join, no new writer, and no new consistency story.

**Write side** (`store._apply_leg`, inside the existing transaction — `store.py`
remains the only writer of `positions` and now of these columns):

```python
price = leg.fill_price(already, filled)      # already computed today
if price is None:
    UPDATE order_legs SET applied_quantity = ?              …
else:
    UPDATE order_legs SET applied_quantity      = ?,
                          priced_quantity  = priced_quantity  + <contracts>,
                          priced_notional  = priced_notional  + <price × contracts>  …
```

Idempotent for free: it rides the existing `filled <= already` early return, so a
re-delivered or replayed event contributes nothing here for the same reason it
contributes nothing to `applied_quantity`. `priced_quantity` is the priced SUBSET
of `applied_quantity` and may be less than it — that is the honest state when the
broker priced some fills and not others.

**Read side** (`_pair_spreads`, pure, so every branch is testable without a DB).
The query gains `l.priced_quantity, l.priced_notional`; for each trade-idea group,
over its **OPENING** orders only, aggregate per symbol and compute:

```
realized = Σ over {short_symbol, long_symbol} of  cash_sign(action) × (notional / quantity)
```

`cash_sign` is `+1` if `LegAction.sells` else `-1` — the **single existing
definition** of a leg's cash direction, the one `signed_leg_delta` already reads
for the position sign and `shadow.marker` already reads for the fill sign. It is
the field the broker does **not** relabel: an opening BUY that comes back
`Buy to Close` on a chained book is still a buy, so the relabelling hazard
(2026-08-25) cannot reach this arithmetic. Nothing here reads `direction` off a
leg action.

Aggregating across every opening order in the group is the whole point: rung 1
(2.05 / 1.55) and rung 2 (2.15 / 1.65) give short avg 2.10, long avg 1.60, realized
**0.50** — contracts-weighted, not newest-only. Rungs at 0.55 and 0.45 give 0.50,
where newest-only gives 0.45.

**The one thing this changes for the worse, stated:** the docstring's contemplated
re-entry case ("2 opened + 2 closed + 2 opened is one open spread of 2, priced
from the NEWEST opening order") would now BLEND the closed entry's credit into the
re-entered one. That case is prevented upstream — `_attempted` claims a candidate
before `DuplicateOrderGuard` even runs, so a re-proposed strike pair never reaches
submission, and a re-entry would in any case mint the *same* `external_identifier`
(`{prefix}{direction}-{candidate}`, deterministic). It has never been observed. The
walk-rung case, by contrast, is proven, is the normal result of any walk that
reprices, and was wrong under newest-only. Newest-only was right for the
hypothetical by accident and wrong for the real case by construction; the trade is
worth making and is recorded rather than slipped in.

## 2. `entry_credit` becomes realized-with-limit-fallback, and the LIMIT stays visible

`OpenSpread` gains two fields and keeps one meaning:

| field | meaning |
|---|---|
| `entry_credit: Decimal` | **the basis the stop is priced from** — realized when available, the limit otherwise. Name and role unchanged; only the value improves. |
| `entry_credit_basis: CreditBasis` | `REALIZED` \| `LIMIT` \| `LIMIT_FILLS_INCOHERENT`. A `StrEnum`, so it prints itself. |
| `entry_limit: Decimal` | the filled opening order's limit — always present, always the same number as today. |
| `priced_contracts: int` | how many contracts the realized basis actually covers (0 when the basis is the limit). |

One number governs; the operator can see both. The prompt is right that a stop
computed from one number while an operator reads another is its own defect — so
the answer is not a second field the stop ignores, it is **provenance on the one
field that governs.**

**And the provenance must travel with it.** `float(spread.entry_credit)` appears in
**six hand-built alert/record dicts** in `watchdog.py` (1401, 2174, 2195, 2266,
2919, 3256). This is the exact shape the 2026-09-02 pass just pinned for
`reject_reason`: *a hand-built alert dict is still a hand-built alert dict.* So:

- `OpenSpread.credit_payload()` returns
  `{"entry_credit", "entry_credit_basis", "entry_limit", "priced_contracts"}`,
  `to_payload()` splats it, and all six sites splat it instead of writing the key;
- an **AST sweep** asserts no module under `src/argus/` outside `store.py` contains
  a dict literal with the key `"entry_credit"`. A convention is what failed here
  last week; this one is held by a test.

## 3. Not a third implementation — a cross-check instead, deliberately

`shadow.outcome._net_legs` already reconstructs a realized credit: average over
priced fills per symbol, then `Σ cash_sign × avg`. `tools/mark` scores from it and
`tools/desk` reuses it rather than scoring a second way. The new runtime path
computes the same arithmetic from different inputs.

**It does not become a shared function, and here is the argument.** The repo's rule
is against scoring the same dataset two ways. These are not the same dataset:

- `outcome` reads the **audit log** — a record, written Tier 1 but written *after*
  the fact, and the wrong thing for a live rail to price a stop from.
- `store` reads the **ledger** — `orders`/`order_legs`, the transactional record
  written in the same transaction as `positions`, which is what every other runtime
  answer about a held spread already comes from and is what makes the divergence
  invariant at the bottom of `_pair_spreads` meaningful.

Retrofitting `_net_legs` onto a `Decimal`-returning helper would also change the
numbers `mark` and `desk` print — a reporting change smuggled inside a rail fix.

What IS shared is the thing that actually drifts: the cash-sign convention, which
is `LegAction.sells` and has exactly one definition. The weighting rule is pinned
instead by **an equivalence test**: the same fills, fed through both paths, must
produce the same credit. That is strictly stronger than sharing the final
arithmetic, because it also catches a divergence in the *accumulation* — which a
shared last-step function would not.

This is stated in the decision record so the next reader does not "fix" the
duplication.

## 4. What this unblocks, concretely

**Becomes possible:** any analysis of `credit_multiple` measured against
`entry_credit_basis == REALIZED` positions. The standing prohibition —
*"do not read any conclusion about `credit_multiple` off a dataset measured against
this basis"* — becomes:

> Conclusions about `credit_multiple` may be drawn only from positions whose
> `entry_credit_basis` is `REALIZED`. A `LIMIT` or `LIMIT_FILLS_INCOHERENT`
> position is not corrected, it is **excluded** — its basis is low by an unknown
> amount and correcting it after the fact would be reconstructing the very number
> the column exists to record.

**Also becomes possible immediately:** `desk`'s note that it discards the
watchdog's `entry_credit` in favour of the fill-derived one becomes a *cross-check*
rather than a correction — the two numbers should now agree on every position, and
a disagreement is a bug worth chasing, exactly the posture `desk` already takes
toward `mark`.

**Still NOT possible, and unchanged by this:** sizing `credit_multiple` off cert.
Fixing the basis removes a KNOWN bias; it does not make sandbox fills realistic —
orders match inside tastytrade's own sandbox and quotes are delayed. A number
measured in cert still cannot size a change on its own. What this buys in cert is
that the *mechanism* can be validated there and the bias no longer contaminates
the live dataset when it exists.

## 5. No backfill

Positions opened before the migration get `priced_quantity = 0` on their legs
(the column default), which falls through to `entry_credit_basis = LIMIT` — bit for
bit today's behaviour, with the basis now labelled. That is sufficient, and it is
the right answer for three reasons:

- Argus trades **0DTE only**. Every position opened before the deploy is settled or
  closed within the same session; there is no multi-day book to migrate.
- A backfill would have to reconstruct per-leg prices from `order.fill` audit
  records into the ledger — a **second writer of ledger state**, from a source the
  ledger is supposed to be authoritative over, to correct positions that no longer
  exist.
- The transitional state is *labelled*, not silent: a position spanning the deploy
  reads `LIMIT` and is excluded from any `credit_multiple` dataset by the rule
  above, which is exactly what it should be.

Consequence worth saying out loud: **a mid-session deploy leaves that session's
already-open positions on the limit basis.** They keep today's slightly-tight stop.
Deploying between sessions avoids it entirely; either way nothing regresses.

## Sizing the loosening — required, and it is smaller than the percentage suggests

Five observations of the gap now exist:

| Date | Candidate | limit | realized | gap | % |
|---|---|---|---|---|---|
| 2026-08-19 | — | 0.55 | 0.60 | **0.05** | +9.1% |
| 2026-08-20 | — | — | — | — | +18% |
| 2026-09-01 | `put-7630-7625` | 0.45 | 0.50 | **0.05** | +11.1% |
| 2026-09-01 | `put-7615-7610` | 0.75 | 0.775 | **0.025** | +3.3% |
| 2026-09-01 | `put-7595-7590` | 0.55 | 0.60 | **0.05** | +9.1% |

**In absolute terms the gap is one half-tick to one tick.** That is what price
improvement on a resting limit order looks like, and it is the honest
characterization; the 3-18% range is that same absolute gap divided by a small
credit, which is why the percentage looks alarming and moves around.

So the stop level moves LATER by `credit_multiple × gap` = **0.05 to 0.10 points,
i.e. $5 to $10 per contract** at the current 2.0 multiple.

**What that does to `max_loss_per_position` exposure: nothing.** The credit stop
does not bound loss and never has — the loss is bounded by the spread WIDTH, which
is defined risk, enforced in `orchestrator.size_contracts` and re-checked in
`PreTradeGate` against the resulting proposal (every size rail enforced twice, the
tightest governing). Moving the stop later increases the EXPECTED loss on a
position that does stop out; it cannot move the MAXIMUM, which stays
`width × contracts × 100 − credit`. On a 5-wide spread that is $500 per contract,
so $5-$10 is **1-2% of max loss per contract**. The welded invariant
`max_concurrent_positions × max_loss_per_position ≤ daily_max_loss` (2 × 1250 =
2500) is untouched, because none of its three terms is a function of the stop.

**And the direction is the point, not a side effect.** On 2026-09-01,
`put-7630-7625` stopped at 0.925 against a stop of 0.90 computed from 0.45. On the
true 0.50 basis the stop is 1.00 and **it is never stopped out at all** — robust
even to a pessimistic blend: the walk repriced once, so even 0.475 across two rungs
puts the stop at 0.95, still above 0.925.

## The `entry_credit <= 0` guard — split, not moved (changed at sign-off)

The first draft moved the single guard onto "whichever basis will price the
stop", which sent a non-positive REALIZED basis to `malformed`. **That was wrong,
and the reason is what `malformed` actually costs.** A malformed spread is not
"watched with a caveat" — it is removed from `spreads` entirely, so:

- it gets **no credit trigger, no underlying trigger, and no
  `FLATTEN_BEFORE_CLOSE`** — and that last one is the trigger nothing can blind.
  The position rides to expiry with no automated exit at all.
- it appears **nowhere on `/status`** (the known-unbuilt `unattributed`/`malformed`
  visibility gap).
- `open_spread_count` ignores it, so it does not even hold a concurrency slot.

And the two cases are different in kind. A non-positive **limit** means the record
is corrupt — `_limit_price` signs OPEN positive, so `build_order` cannot produce
one. A non-positive **realized** basis leaves the symbols, quantities, actions and
limit all coherent; only the fill-price aggregate is nonsense, and the spread is
perfectly watchable on the basis it has been watched on for the entire life of the
system. Routing a well-formed live vertical into "no stop, no flatten, invisible"
over a price-arithmetic anomaly is strictly the worse trade. Falling back to the
limit is also fail-CLOSED in the trading sense: the limit basis is LOW, so the
stop is TIGHTER than the truth — it errs toward exiting.

So the guards split:

| condition | verdict |
|---|---|
| `entry_limit <= 0` | **malformed**, unchanged — record integrity, and unconditional (it does not matter what the fills say about an order whose limit cannot be trusted) |
| both legs priced, realized `<= 0` | **watched on the limit**, `entry_credit_basis = LIMIT_FILLS_INCOHERENT` |
| either leg unpriced | **watched on the limit**, `entry_credit_basis = LIMIT` |

**A new state is worse than useless if nothing surfaces it**, so
`LIMIT_FILLS_INCOHERENT` is not merely a quieter label:

- it rides in `credit_payload()`, and therefore onto all six watchdog pages and
  `watchdog.check` — every surface that names a held spread's credit at all;
- the watchdog **pages once per position on first sighting**, carrying
  `candidate_id` at the TOP level of the alert detail — or the 2026-08-21
  per-position throttle folds it in with the next unrelated `watchdog_*` alert;
- it is excluded from any `credit_multiple` dataset by the same rule that excludes
  `LIMIT` (§4).

**`/status` is NOT among those surfaces, and that is a stated gap rather than
coverage.** `/status` carries no held spread's `entry_credit` for any position —
only `cost_to_close` / `credit_stop` on the spent-exit row — so there is nowhere to
put the basis without adding a surface this prompt did not ask for. The page and
the Tier 1 record are the channel. (Noted while looking: that spent-exit row drops
its mark entirely — `session.py` never copies `cost_to_close`/`credit_stop`/
`still_triggered` out of `SpentExitAttempt`, so it always renders "mark:
unpriced". A separate defect, from a separate pass, raised separately.)

Note what cannot produce a non-positive realized basis: leg relabelling. The cash
sign comes from `LegAction.sells`, and a relabelled `Buy to Close` is still a buy.

## Traps, checked

| Trap | Where it is handled |
|---|---|
| A walk fills across several `broker_order_id`s under one `external_identifier` | The aggregate is over **every opening order in the trade-idea group**, contracts-weighted per leg — not the newest rung. §1. |
| Nothing symbol-keyed can carry this | The columns are on `order_legs`, keyed `(broker_order_id, symbol)` and read per trade idea. `positions` is not touched and remains safety-irrelevant for price. §1. |
| The correction loosens a safety rail | Sized above: 0.05-0.10 points, $5-$10/contract, 1-2% of a 5-wide's defined risk; `max_loss_per_position` and the welded invariant unaffected. |
| The no-price fallback must stay the LIMIT, never a zero | `priced_quantity == 0` on either leg → `CreditBasis.LIMIT`. A `None` price is never blended: `_apply_leg` writes neither column when `fill_price` returns `None`, so it cannot enter the notional as 0.0. Same posture as `_apply_position`'s "a slightly stale basis is a better lie than a halved one". |
| Migrations are immutable | New file `0005_order_leg_fill_prices.sql`. `scripts/check_migrations.py` and `tests/test_migration_check.py` already enforce this; the test's `CLEAN_FILES` set is updated. |
| The `entry_credit <= 0` guard must still hold | Above — one guard, on the chosen basis, and a non-positive REALIZED basis is rejected rather than downgraded. |
| Do not fix this by widening a staleness bound | Nothing in this design touches `staleness.*`. `market_data_max_age_s` is not read, not changed, not mentioned in the implementation. |

## What this is NOT

- **Not a retune of `credit_multiple`.** It makes that analysis possible. The
  prohibition stands until a dataset exists on the corrected basis, and it stands
  differently — see §4.
- **Not a change to what closes a position.** The trigger, the debounce, the
  one-attempt rule, `ExitExecutor.close`, the walk and the width-capped cross are
  all untouched. Only the number the credit trigger is aimed at, and the tolerance
  it is compared with.
- **Not a fix for `filled_contracts`' across-rungs under-count**, which is a
  different walk-versus-identifier instance on the reporting side.
- **Not `infer_direction`.** Direction is never read from a leg action anywhere in
  this change; only `LegAction.sells`, which the broker does not relabel.

---

## Tests

**D3**
1. `cost_to_close = 1.6000000000000005`, `credit_stop = 1.6` — the verbatim
   2026-09-01 pair — does **NOT** fire `CREDIT_MULTIPLE`.
2. The same pair via `evaluate_triggers` end to end, from four leg quotes that
   actually produce that mid, so the test covers the arithmetic and not just the
   comparison.
3. A genuine one-tick breach (`credit_stop + 0.05`) DOES fire. A half-tick breach
   (`+0.025`) DOES fire. A quarter-tick (`+0.0125`) DOES fire.
4. `cost_to_close` exactly equal to `credit_stop` does not fire, and one
   `PRICE_EPSILON` above it does — the boundary is pinned, in both directions.
5. Non-finite `cost_to_close` still lands in `unknown_reasons`, not in
   `true_reasons` — the `is_finite` guard is in front of the tolerance.
6. `underlying_breached` at exact equality still fires (the asymmetry is asserted,
   not just commented).

**D5**
7. A spread filled across two rungs at different prices reports the
   **contracts-weighted** credit: rungs (2.05/1.55) and (2.15/1.65) → 0.50; rungs
   netting 0.55 and 0.45 → 0.50, **not** 0.45.
8. Unequal contracts across rungs weight correctly (2 @ 0.50 + 1 @ 0.44 → 0.48).
9. A broker reporting no fill price falls back to the LIMIT, with
   `entry_credit_basis == LIMIT` and `priced_contracts == 0`.
10. One leg priced and the other not → LIMIT, not a half-credit.
11. A chained pair sharing a strike still attributes per trade idea: two spreads,
    shared strike, different realized credits, each gets its own — and the
    `positions` divergence invariant still passes.
12. A partially-priced leg (2 contracts applied, 1 priced) averages over the
    priced subset and reports `priced_contracts == 1`.
13. Non-positive realized basis → still **watched**, on the limit, with basis
    `LIMIT_FILLS_INCOHERENT`; a non-positive LIMIT is still `malformed` regardless
    of what the fills say.
14. Idempotency: replaying the same `PlacedOrder` event does not double-count
    `priced_notional` — the columns move only when `applied_quantity` moves.
15. Migration: a database at 0004 with existing `order_legs` rows migrates and
    those rows read as `LIMIT` (the no-backfill contract).
16. `InMemoryOrderStore` and `SqliteOrderStore` agree — the fake accumulates the
    same way and goes through the same `_pair_spreads`.
17. **Equivalence:** the same fills through `shadow.outcome.executions` and through
    the ledger produce the same credit.
18. **AST sweep:** no dict literal with the key `"entry_credit"` outside
    `store.py`.
19. `to_payload()` carries basis, limit and priced contracts; the six watchdog
    dicts carry them too (covered by 18, asserted directly on at least the
    `watchdog_triggered` page).

## Mutations to verify — each must fail the suite

| # | Mutation | Caught by |
|---|---|---|
| M1 | Remove the tolerance (`>=` on the raw difference) | 1, 2, 4 |
| M2 | Widen the tolerance to a half-tick (0.025) | 3 (the quarter-tick breach stops firing) |
| M3 | Flip the tolerance's sign (`>= -PRICE_EPSILON`) | 4 |
| M4 | Give `underlying_breached` the same tolerance | 6 |
| M5 | Use the NEWEST opening order's realized price instead of the aggregate | 7 |
| M6 | Weight by orders rather than contracts (unweighted mean of rungs) | 8 |
| M7 | Blend a `None` price as 0.0 | 9, 10 |
| M8 | Send a non-positive realized basis to `malformed` (drop the spread from the watch list) | 13 |
| M9 | Accumulate `priced_notional` before the `filled <= already` early return | 14 |
| M10 | Aggregate across CLOSING orders as well as opening ones | 7 (the exit debit would corrupt the credit) |
| M11 | Derive the cash sign from `LegAction.opens` instead of `.sells` | 11 (relabelled leg on the chained pair) |
| M12 | Drop `entry_credit_basis` from one of the six alert dicts | 18 |

## Deliverables after sign-off

1. **D3 implementation** — `zerobid.PRICE_EPSILON`, `TriggerThresholds.credit_breached`,
   the one call site, the `underlying_breached` comment, tests 1-6, mutations M1-M4.
2. **D5 implementation** — migration 0005, `_apply_leg` accumulation, `_pair_spreads`
   aggregation, `OpenSpread`'s three new fields + `credit_payload()`, the six alert
   dicts, `InMemoryOrderStore`, tests 7-19, mutations M5-M12.
3. **A decision record** `docs/decisions/2026-09-02-the-credit-stop-basis-and-its-comparison.md`,
   added to `docs/decisions/README.md`, `CLAUDE.md` updated. It must carry: the
   correction to `store.py:107-128`'s stale premise about per-fill prices; the
   sizing of the loosening; and what the `credit_multiple` prohibition becomes.
4. **`store.py:107-128` rewritten** — it is quoted as authority in three places and
   is now wrong in its load-bearing sentence.
5. **The pass record's "Recommended order" updated** to reflect what shipped.

---

## Sign-off — what the gate changed

Signed off 2026-09-02. Two questions were put and both moved the design.

**1. Ship order — the recommendation survived, its argument did not.** The draft
justified staggering on attribution; that was wrong, because both effects are
reconstructible per position from records that already exist. Staggering is
justified on RISK ISOLATION instead, and the section above now says so. The build
is unaffected either way: two independent commits, the deploy split is a
tag-time call.

**2. The non-positive realized basis does NOT go to `malformed`.** The draft
under-weighted what `malformed` costs — no triggers *including the flatten*,
nothing on `/status`, no concurrency slot — and conflated a corrupt record (a
non-positive LIMIT, which `build_order` cannot produce) with a coherent spread
whose fill-price aggregate is nonsense. The guards are now split, the spread stays
WATCHED on the conservative limit basis, and the contradiction is carried as a
third `CreditBasis` value that pages once per position. **The state is only worth
having because it is loud**; a quieter label than `LIMIT` would have been worse
than the malformed routing it replaced.
