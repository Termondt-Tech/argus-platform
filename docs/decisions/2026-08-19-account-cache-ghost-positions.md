# Ghost positions in the account cache (measured 2026-08-19)

*A closed position is OMITTED from the REST snapshot, not reported flat — so the
merge-only re-seed could never clear one, and the "broker-derived" leg count was a
mirror that drifted.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

## What was observed

Intraday checkpoint, 12:02 ET. `/status` reported `open_position_count: 3` and
`open_spread_equivalent: 2` against `max_concurrent_positions = 2`, so every
candidate was denied `concurrent_limit`. The watchdog carried a standing book
disagreement:

```
"watchdog.book_disagreement": 1            <- Tier 1, paged
"watchdog.book_disagreement_still": 266    <- ~66 min at the 15s refresh cadence
{ "broker_legs": 3, "ledger_legs": 2, "direction": "held_but_unbooked" }
```

`held_but_unbooked` means a position is held with no ledger row and therefore no
virtual stop — the exact gap
[the broker-agreement check](2026-08-11-unwatched-position-gap.md) exists to catch,
and the one direction deliberately given no settle allowance.

`tools.account` against cert, ~12:10 ET, said otherwise:

```
broker positions (2):
  SPXW  260819P07685000  signed_qty=-2  open=3.6
  SPXW  260819P07680000  signed_qty=2   open=3.0
```

Exactly the spread the watchdog was watching. **There was no third leg and no
unwatched position.** The day's own records agreed: 2 entries + 1 watchdog exit =
3 orders, 6 fills, 2 legs held.

## Mechanism

`AccountStateCache` had one eviction path — a `CurrentPosition` event carrying
quantity zero. `seed()`, the "authoritative REST snapshot", wrote every row it was
given and **subtracted nothing**:

```python
for position in positions:
    self._cache.update_position(position)
self._cache.update_net_liq(balances.net_liquidating_value)
```

The false premise is that a closed position arrives as a zero row. It does not —
the REST positions endpoint simply omits it (the output above returns two
positions, not three with one flat). So:

1. One leg of the exited spread had its zero-quantity frame lost. The session saw
   `account.refresh_failed: 53` across `account.refresh_recovered: 22` outage
   episodes, which is the documented way to miss a stream event.
2. Nothing could then remove it. The liveness refresh re-seeded every 10s for over
   an hour, refreshing the two real legs and stepping past the ghost each time.
3. `open_position_count` read 3. `ceil(3/2) = 2` pinned the concurrent limit, and
   the watchdog paged its no-allowance condition on a position closed hours earlier.

**Confirmed from the order ledger after the fact (same evening), not left as
inference.** The audit log identifies the ghost exactly:

```
14:56:38Z  order.submitted
14:56:40Z  order.fill  broker_order_id 1357342  price 3.7
14:56:41Z  order.fill  broker_order_id 1357342  price 2.5   (no entry.finished — this is the EXIT)
```

That is the watchdog closing `20260819-call-7750-7755` at **10:56:41 ET**, a 1.20
debit — exactly `credit_stop` on a 0.60 credit. The book disagreement's 266
`book_disagreement_still` records at the 15s refresh cadence place its start at
roughly 10:55. So **the ghost is one leg of the call spread**, whose zero-quantity
`CurrentPosition` frame was lost in the seconds after its own exit filled.
Mechanism, timing, and identity all agree.

The blast radius was bounded by luck, not design: `AlertStreamerAccount` is built
per session, so the ghost would have died at the next day's 09:40 start, and
startup reconciliation reads REST directly (`Reconciler(broker, ...)`), so there was
never a false-halt path.

## What was wrong with the design, stated plainly

[`risk/models.py`](../../src/argus/risk/models.py) claimed `open_position_count` is
"BROKER LEGS... Broker-derived", and that claim is the entire safety argument for
preferring it over the ledger. `session._broker_position_count` went further and
said the periodic REST re-seed is what makes it trustworthy. It was a stream-fed
mirror that could retain a closed position for the life of a session, and both
docstrings have been corrected to say so.

## The fix

**1. `seed()` subtracts.** `AccountStateCache.evict_absent(present, older_than_s=)`
drops entries a successful, complete REST fetch did not report. A failed fetch never
reaches it — the cache is left untouched so a broken link keeps looking broken.

**2. The eviction is AGE-GATED, and that is the whole safety argument.** Evicting on
absence alone fails in the direction that matters: the broker's position book trails
a fill by ~4-5s (measured 2026-08-13; the lag `watchdog.book_settle_s` exists to
absorb), so a snapshot taken inside that window legitimately omits a position that
really is held. Dropping it would under-count `open_spread_equivalent` and release a
concurrent slot already spoken for.

The two cases separate cleanly on age. A real position appears in EVERY snapshot, so
each seed re-touches it and its age never exceeds the refresh interval. A ghost
appears in none, so its age climbs without bound.

**3. `staleness.account_position_evict_after_s`, its own key, `60.0`.** ~12x the
propagation lag and 6x the refresh interval. The loader REFUSES a value at or below
`account_refresh_s`, because below that a real position can age out between two
seeds — enforced, not documented, since it is the direction that fails open.

It is **not** a reuse of `watchdog.book_settle_s`. Both are sized against the same
propagation lag, which is exactly why sharing one number would weld two unrelated
conditions together — the same reasoning `tools/evaluability.py` records for the
blind-episode allowance.

**4. The no-allowance page confirms against REST first.**
`PositionWatchdog._cache_is_over_reporting` runs one `fetch_positions()` round trip
before `held_but_unbooked` pages. REST agreeing with the LEDGER means the cache
invented the leg: record Tier 1 `watchdog.book_cache_over_reporting`, do not alert.
REST agreeing with the CACHE means a real position is unbooked: page exactly as
before.

Three properties are load-bearing:

- **Every failure path pages.** A confirm that times out, raises, or returns None
  proves nothing, and "could not check" must never be why an unwatched position
  goes unreported. Absent the seam entirely, behaviour is unchanged.
- **It does not consume the escalation edge.** `_Escalation.rising` is what makes an
  episode page exactly once, so marking it during a suppressed observation would
  mean a disagreement that later turns real could never page — the same trap
  `_book_allowance_elapsed` documents.
- **One round trip per distinct `(broker, ledger)` shape.** A steady disagreement
  confirms once; any change in the numbers re-confirms, so a verdict can never
  outlive the state it was measured against.

`booked_but_absent` does not consult it. That direction already has a settle
allowance and a round trip there buys nothing.

**5. `account.position_evicted` telemetry**, carrying the age each entry reached.
That is the measurement the threshold gets re-tuned from — the discipline
`book_settle_s` was sized under. A healthy session evicts NOTHING, because the
stream's zero-quantity frames arrive and clear positions first. **The rate is the
signal**: sustained evictions mean the account stream is dropping events.

## Residual risk, named

If REST returns a PARTIAL position list for longer than `account_position_evict_after_s`
while the stream is simultaneously silent, a real leg is evicted and the concurrent
limit under-counts. Judged acceptable: eviction requires a successful complete fetch,
60s is ~12x the observed lag, and a broker returning partial position lists for a
full minute is a failure that breaks reconciliation too.

## What was checked and found NOT to be a defect

The broker's open prices implied a net entry credit of 3.60 − 3.00 = 0.60 while the
watchdog carried `entry_credit: 0.55`. That is deliberate and documented at
[`execution/store.py`](../../src/argus/execution/store.py): `entry_credit` is the
filled opening order's LIMIT, not the realized fill, which biases the 2x trigger
slightly EARLY. Safe direction, no change.

**A cancel/replace race was suspected and RULED OUT — recording the elimination,
because it is the obvious thing to re-suspect later.** The 0.05 gap appeared on
exactly the one order that repriced (`STEPS 1`; the call, at `STEPS 0`, filled at
its limit), and the walk mints a new broker order id per step and cancels the prior
one — so "the original filled before the cancel landed" fit the shape. It is not
what happened. The ledger attributes both fills to **1356440**, the id minted by
`order.replaced`, not the original's:

```
14:00:07Z  order.submitted                                     (step 0)
14:00:17Z  order.replaced   broker_order_id 1356440  limit 0.55
14:00:20Z  order.fill       broker_order_id 1356440  price 3.6
14:00:20Z  order.fill       broker_order_id 1356440  price 3.0
```

The replacement filled. What it did was fill **0.05 BETTER than its own limit** —
0.60 realized against a 0.55 limit — which is cert price improvement, and cert is
where fills are synthetic by construction.

That makes `store.py`'s claim a MEASUREMENT rather than an argument for the first
time: "for a limit order the realized credit is at least the limit, so this basis is
at worst slightly LOW." Realized 0.60 ≥ limit 0.55, and the stop sat at 1.10 where a
fill-based basis would have put it at 1.20 — early, which is the safe side.
**This closes only the one shape observed here.** The four order-behaviour questions
stay open, in an environment whose fills prove nothing about a real auction.

## Scope

Not sandbox-specific — the mechanism is identical in live. **Not a live-flip gate**:
a ghost inflates the count, so the concurrent limit errs closed, and reconciliation
reads REST. The cost is lost trading days and a crying-wolf alert on the highest
-severity watchdog channel, which is why it lands before size is scaled.
