# Argus: The Credit Stop Is Measured Against Two Wrong Numbers

## Priority

**Real, both observed on 2026-09-01, and together they account for two of the day's
five `credit_multiple` stops.** Neither is a market event; both closed a position that
would have expired worthless.

- **D3 — the comparison.** `call-7670-7675` was stopped out at
  `cost_to_close: 1.6000000000000005` against `credit_stop: 1.6`. The trigger fired on
  **4.4e-16**, eleven orders of magnitude below the 0.05 tick. Realized −160.00.
  **This one is wrong in every regime**, it is the smallest fix on the day's list, and
  it has a direct precedent in this repo (2026-08-04, the mark tool's credit column,
  fixed with `Decimal`) — that one was a display column; this one closes positions.
- **D5 — the basis.** `OpenSpread.entry_credit` is the filled opening order's LIMIT,
  not the realized credit. `put-7630-7625` recorded 0.45 and actually collected 0.50
  on both contracts, so its stop sat at 0.90 instead of 1.00 and fired at 0.925 —
  a stop that should never have fired. Known and documented since 2026-08-20 at
  9-18%; **2026-09-01 is the first time it has a realized dollar consequence attached
  rather than a projected one.**

They are one prompt because they are the same decision point — `evaluate_triggers`'
credit branch — and share a test surface. **They may still ship separately**: D3 is a
few lines, D5 needs a schema migration. Decide that in the design.

**D5 changes the behaviour of a safety rail in the LOOSENING direction** (see the
traps). Design first, sign-off before implementation, on the gate P3, P5 and the exit
ladder took. D3 is small enough that the design can recommend shipping it immediately
and independently — say so if that is the recommendation.

Background: [the second v0.3.13 day](../decisions/2026-09-01-second-live-fire-day.md).

## D3 — a bare `>=` on two floats

### The evidence

From the 09:34 ET page, verbatim:

```
watchdog_triggered [20260901-call-7670-7675]
  {"cost_to_close": 1.6000000000000005, "credit_stop": 1.6,
   "entry_credit": 0.8, "fired": ["credit_multiple"], "spot": 7661.165}
```

`desk` scores the position `filled`, −160.00. SPX settled at 7637.705; the spread was
7670/7675 calls. It expired worthless.

### The mechanism, confirmed in source

`TriggerThresholds.credit_stop` (`execution/watchdog.py:465-467`) takes a `Decimal`
and immediately leaves the exact domain:

```python
def credit_stop(self, entry_credit: Decimal) -> float:
    """The cost-to-close level that trips the credit trigger."""
    return float(entry_credit) * self.credit_multiple
```

`SpreadMarket.cost_to_close` (`execution/watchdog.py:348-355`) is `spread_mid` over
four float quotes. The comparison (`execution/watchdog.py:574-578`) has no tolerance:

```python
if not is_finite(cost_to_close):
    problems.append(...)
    unknown.add(TriggerReason.CREDIT_MULTIPLE)
elif cost_to_close >= credit_stop:
    true_reasons.add(TriggerReason.CREDIT_MULTIPLE)
```

`0.8 * 2.0` and the four-quote mid land a half-ULP apart, and `>=` does the rest.

### Design questions

1. **Fix the comparison or fix the quantities?** `cost_to_close` is not only the
   trigger input — it is the MARK on `/status`, on the spent-attempt pages, and it
   prices the exit order at `watchdog.py:1044` (`credit=market.cost_to_close`). It
   also comes from `zerobid.spread_mid`, which the ENTRY path shares. Changing its
   type has a blast radius; changing the comparison does not. Argue the boundary.
2. **If a tolerance: what unit, and why?** A price grid with a 0.05 tick and a credit
   that is a half-tick ($0.025) quantity suggests its own answer, and 2026-08-04
   already reasoned about exactly this quantity for the mark tool. An absolute epsilon
   pulled from `sys.float_info` is not the same argument.
3. **Which direction does the tolerance fail?** A tolerance makes the stop fire
   strictly LESS often. That is a real behaviour change on a safety rail, however
   small the magnitude. Justify it as *removing a comparison that was never
   meaningful* rather than as *loosening the threshold*, and show the two are the
   same thing at this magnitude.
4. **The sibling comparison.** `TriggerThresholds.underlying_breached`
   (`watchdog.py:469-479`) does `spot <= short_strike + buffer` / `spot >= strike -
   buffer` on floats. Answer whether it needs the same treatment. The answer may
   legitimately be no — strikes are integral, the buffer is in points, and spot moves
   in fractions — but it must be answered, not skipped.
5. **Is `credit_multiple` itself a float from config?** Confirm where precision is
   lost first and whether keeping `Decimal` all the way to the comparison is cheaper
   than a tolerance.

## D5 — `entry_credit` is the limit, not the fill

### The evidence

`put-7630-7625`, entry, from the audit chain:

```
585440 walk.holding           1429812  limit 0.50
585448 order.replace_attempt           limit 0.45
585450 order.fill    1429812  1  Sell to Open  2.05
585451 order.fill    1429812  1  Buy to Open   1.55     -> net 0.50
585461 order.replaced 1429813          limit 0.45
585474 order.fill    1429813  1  Sell to Open  2.15
585475 order.fill    1429813  1  Buy to Open   1.65     -> net 0.50
585488 walk.finished  1429813  filled_contracts 1  requested_contracts 2  steps 1
```

Both contracts collected **0.50**. The walk's final limit was **0.45**, and that is
what the stop was computed from. Three of the day's eight positions show the gap and
all three go the same way.

### The mechanism, already documented

`_open_spreads` (`execution/store.py:871`):

```python
entry_credit = Decimal(str(head["limit_price"]))
```

`store.py:107-128` documents this fully and honestly, including the 9-18% figure and
the standing prohibition (*"Do not read any conclusion about `credit_multiple` off a
dataset measured against this basis"*). It names the fix as *"capturing
`FillInfo.fill_price` durably (a new column and a migration)"*.

### What 2026-09-01 changed about that assessment

**One premise in that docstring is now stale, and it is the one that made the fix look
expensive.** It says `positions.average_open_price` is *"0.0 whenever the broker
reported no per-fill prices, which is every case chunk 1 actually observed."*

Today's `order.fill` records carry real per-fill prices — 2.05, 1.55, 2.15, 1.65,
4.50, 3.00 — throughout. So `OrderLeg.fill_price` (`execution/models.py:262-283`)
returns real numbers, `_map_fills` (`models.py:690`) is populating `LegFill`, and
`store.py:501` already computes a realized price for every leg on every fill
application and passes it into `_apply_position` and `AppliedFill.price`.

**The data is already flowing. It is simply not durable at spread level.** Verify this
against the chain before designing on it — but if it holds, the fix is smaller than
the docstring assumed, and that docstring needs correcting either way.

### Traps — the first one is the whole design

- **A walk that reprices fills across SEVERAL `broker_order_id`s under ONE
  `external_identifier`.** Proven today: 1429812 filled 1 contract, 1429813 filled 1.
  `_open_spreads` reads *"the NEWEST filled opening order"*. A realized price attached
  per ORDER still misses rung 1. **The realized credit must aggregate across every
  order sharing the identifier**, weighted by contracts. Today both rungs happened to
  net 0.50, so newest-only is right by luck; rungs at 0.55 and 0.45 give 0.45 against
  a truth of 0.50. This is the third instance in one day of the walk-versus-identifier
  distinction — the same one behind D1 and behind `_await_settled`'s FILLED check.
- **Nothing symbol-keyed can carry this.** `positions.average_open_price` is per
  SYMBOL, and chained verticals net to zero at a shared strike (`signal.width = 5.0`
  on SPX's 5-point ladder makes that the COMMON case). It cannot be the source. Same
  landmine as the concurrency cap's.
- **The correction LOOSENS a safety rail, and that must be stated and sized, not
  slipped in.** Today's basis is low, so the stop fires EARLY — the conservative
  direction, which is exactly why it was safe to defer. Correcting it makes every
  credit stop fire LATER. That is the right thing and it is not a free win: quantify
  the shift (9-18% historically, and today's three observations) and say what it does
  to `max_loss_per_position` exposure before it ships.
- **The no-price fallback must stay the LIMIT, never a zero.** `fill_price` returns
  `None` when the broker said nothing, and `_apply_position` already reasons that *"a
  slightly stale basis is a better lie than a halved one."* Same posture here: absent
  realized price falls back to today's behaviour.
- **Migrations are immutable.** New file, never an edit to an existing one.
  `order_legs` (migration 0004) already carries `applied_quantity` as the per-trade-idea
  idempotency ledger and is the natural home — evaluate it against alternatives.
- **The `entry_credit <= 0` malformed guard at `store.py:872` must still hold**, and
  must still reject a realized basis that comes back non-positive.
- **Do not fix this by widening `staleness.market_data_max_age_s`** or any other
  bound. Unrelated, and forbidden.

### Design questions

1. Where does the realized credit live, and does it aggregate at write time or at
   read time in `_open_spreads`?
2. Does `OpenSpread.entry_credit` become realized-with-limit-fallback, or does a
   second field appear so `/status` and the pages can show both? A stop computed from
   one number while an operator reads another is its own defect.
3. `tools/desk.py` and `tools/mark.py` already reconstruct realized fills
   independently, and `desk` REUSES marker's reconstruction rather than scoring a
   second way. Does this become a third implementation, or does one of them become the
   shared one? State it — the repo has an explicit rule against scoring two ways.
4. What does this unblock, concretely? The `credit_multiple` prohibition is the
   headline: name what analysis becomes possible and what still is not (cert fills are
   not realistic; a number measured in cert still cannot size a change on its own).
5. Does the deployed image need a backfill for positions opened before the migration,
   or is limit-fallback sufficient for the transition?

## Deliverables

1. **A design document first**, answering every question above for both defects, with
   an explicit recommendation on whether D3 ships ahead of D5 or with it.
2. After sign-off: the implementation.
3. **A decision record** in `docs/decisions/` — dated, added to
   `docs/decisions/README.md`, `CLAUDE.md` updated. It must include: the correction to
   `store.py:107-128`'s stale premise about per-fill prices; the sizing of the
   loosening; and what the `credit_multiple` prohibition becomes afterwards.
4. **Tests.** At minimum: the exact 2026-09-01 pair
   (`cost_to_close = 1.6000000000000005`, `credit_stop = 1.6`) does NOT fire; a
   genuine one-tick breach DOES; a spread filled across two rungs at different prices
   reports the contracts-weighted credit, not the newest rung's; a broker reporting no
   fill price falls back to the limit; and a chained pair sharing a strike still
   attributes per trade idea.
5. **Verified mutations**, including: remove the tolerance; use the newest order's
   price instead of the aggregate; blend a `None` price as 0.0.
6. Update the pass record's "Recommended order" to reflect what shipped.

## Non-negotiables this must respect

- **NEVER TOUCH THE LIVE DATABASE.** Read-only URI for any exploration, or the
  droplet's `docker compose run --rm -T argus python -` pattern from the pass record.
  Dev runs use `argus.dev.toml`.
- Every size rail is still enforced twice and the tightest still governs.
- `max_concurrent_positions x max_loss_per_position <= daily_max_loss` still holds.
- The virtual stop stays in-process; no broker-side stops, no unbounded market orders.
- `store.py` remains the ONLY writer of `positions`.
- Migrations are immutable and additive.

## Out of scope

- **D1/D2 — the reprice-rejection read and the exit alert's dropped `reject_reason`**
  (`argus_reprice_rejection_identifier_wide_prompt.md`). Same day, different defect.
- **D4 — the flatten allowance** (`argus_flatten_allowance_spent_early_prompt.md`).
- Retuning `credit_multiple` itself. This prompt makes that analysis POSSIBLE; it does
  not perform it, and the prohibition stands until the basis is fixed and a dataset
  exists on the corrected basis.
- Deploying `watchdog.exit_walk_min_steps` — built, waiting, separate.
- `infer_direction` and the relabelled closing legs.
