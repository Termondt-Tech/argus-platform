# The exit ladder's second pass: the percentage is dimensionally wrong (2026-09-17)

*The data-backed `exit_walk_away_pct` / `exit_walk_min_steps` pass that has been owed
since 2026-08-10 and named as owed by both provenance strings. Read out of the
2026-09-14 cert backup — **780,853 audit rows, 2026-07-23 to 2026-09-14, 37 exit walks
across 12 trading days**. The headline is not a number: the exit opening-limit
distribution is **bimodal with an empty band between its modes**, so a single
percentage of the opening limit provisions ladder range **inversely to the runway each
regime has**. `exit_walk_min_steps` goes 1 → 3 in all three profiles;
`exit_walk_away_pct` is **held** at 0.20, deliberately rather than by default. And the
missing width cap, recorded on 2026-08-31 as reachable-in-principle, **fired in
production and cost $10** — it made a flatten do worse than doing nothing.*

> **COUPLED IMAGE + CONFIG.** The config half touches all three profiles and adds no
> new key (`exit_walk_min_steps` already exists and is REQUIRED). The `src/` half is the
> width cap, reported here as owed AHEAD of the config flip and then **built the same
> day** rather than letting the flip ship against a broken ceiling — so this is one
> release, not two, and the image and the config deploy together.
> Source: `dev/nas/argus-20260914T163001.db`, read read-only. Two CODE items remain
> reported and deliberately NOT fixed.

This pass was expected to be blocked. The first read of the problem concluded that the
counterfactual — *was there a resting offer one rung above where the ladder gave up?* —
was not recorded anywhere, because `walk.step` and `walk.holding` carry only our own
limit and `crossing_debit` is computed in `_cross_limit` and discarded. That was wrong.
**`watchdog.crossed` is Tier 1 and carries both numbers** (`walked_away_at` and
`crossing_limit`), so the deficit is directly computable. It is also only n=3 — the
cross is rare, because it happens only after a walk-away — so `watchdog.exit_started`
turned out to be the better instrument, and it is the one the decision rests on.

## What was measured

`watchdog.exit_started` records the exit's opening limit and the floor `PriceWalk` will
compute from it, together, in one Tier 1 row. 37 of them:

| regime | opening limits | n | rungs at `0.20` + `min_steps 1` |
|---|---|---|---|
| flatten, near-worthless | 0.05, 0.10 | 12 | 0–1 |
| credit stop | 0.95 – 1.95 | 23 | 4–8 |
| stressed / ITM outliers | 3.45, 5.05 | 2 | 14, 20 |

Rung-count histogram over all 37: `{0:4, 1:8, 4:3, 5:5, 6:8, 7:6, 8:1, 14:1, 20:1}`.

**Nothing lands between 0.10 and 0.95, and the gap is arithmetic rather than
sampling.** A credit stop fires at `credit_multiple × entry_credit` = 2.0 × [0.50…1.00],
so its opening limit is ~1.00–2.00 *by construction*. A flatten fires on whatever a
nearly worthless 0DTE winner is worth, so its opening limit is ~0 *by construction*.
The two triggers cannot produce the band between them. That is what makes the floor
separable: **any floor of 3 or fewer steps binds only below |0.75|, and zero of the 23
credit-stop rows is under 0.95.** Measured, not assumed.

## Finding 1 — the range is provisioned inversely to the runway

Measured rung-to-rung elapsed, from `watchdog.exit_started` to `walk.finished`, is
**4–23 seconds** — not the 1.0s `exit_cadence_s` / `cycle_wait_s` suggest, because each
rung pays an observation cycle and the COA settle window. So at `0.20`:

| regime | runway available | rungs | provisioned walk time | realized |
|---|---|---|---|---|
| flatten | **1800s** (`flatten_before_close_s`) | 0–1 | ~8s | 3–9s |
| credit stop | seconds — gamma into PM settlement | 4–8 | **40–80s** | 3–23s |

The regime with half an hour of runway gets one rung. The regime where every second
costs money gets up to eighty seconds of provisioned walking. **That is backwards, and
it is the functional form doing it, not the value**: a percentage makes the time budget
proportional to the price, and the price is inversely related to the urgency.

## Finding 2 — the missing width cap fired, and made the flatten worse than inaction

`_cross_limit` caps at `min(debit, width)`. `PriceWalk` does not, which the 2026-08-31
record noted — and neither does `_limit_price`, which refuses only a non-positive
price, which nothing had noted. On 2026-09-03 at **15:37:59 ET**, on the position left
unmanaged by that day's halt and picked up after the manual restart:

```
argus-close-20260903-call-7725-7730   width 5.00   entry_credit 0.70   2 contracts
  opening limit  -5.05        <- ALREADY past the width
  walk-away floor -6.05  (20 rungs, the last 21 of them unpayable by construction)
  filled at step 0 at -5.05
  realized -870.00
```

Holding to settlement pays the width: `(0.70 − 5.00) × 2 × 100 = −860.00`. **The flatten
paid $870 to avoid an $860 loss.** CLAUDE.md records this position at −860.00,
"identical to holding it to settlement"; the ledger says −870.00, and the $10 is exactly
the 0.05 by which the limit exceeded the maximum the spread could ever be worth.

The breach was at the **opening limit**, not the floor. `cost_to_close` came off a wide
print on a deep-ITM spread and marked above intrinsic; nothing compared it to the width.
This upgrades the 2026-08-31 note from *reachable on a gap open or a stale wide quote* to
*realized, with a dollar figure*.

## Finding 3 — `exit_walk_min_steps = 1` is proven, and 1 was sample-limited

Before the fix reached cert (v0.3.14 deployed 2026-09-01 ~17:02 EDT, after that day's
15:31 ET flatten), **every flatten walk in the near-worthless band walked away having
conceded nothing**:

```
pre   2026-08-31  walked_away  steps=0  final=-0.05
pre   2026-08-31  walked_away  steps=0  final=-0.05
pre   2026-09-01  walked_away  steps=0  final=-0.05
pre   2026-09-01  walked_away  steps=0  final=-0.05
```

After it, none did:

```
post  2026-09-02  filled  steps=0  final=-0.05
post  2026-09-02  filled  steps=0  final=-0.05
post  2026-09-08  filled  steps=0  final=-0.10
post  2026-09-09  filled  steps=1  final=-0.15   <- used the rung
post  2026-09-09  no_order_exists  steps=0  final=-0.10
```

The walk-away→cross escalation disappeared from that regime entirely, and one exit
actually reached the rung the fix added. **The 2026-08-31 record's claim that 1 is the
unique value which changes the two broken rows and no row that already works was true at
n=2 and is false at n=37**: with the empty band visible, 2 and 3 also change nothing that
already works. Its reasoning is intact; its conclusion was sample-limited.

## Finding 4 — the deficit is exactly one tick, three times, and cannot grow

> **Correction 2026-09-25.** This table is right because all three rows are 0-step walks,
> where the opening limit equals the floor. `walked_away_at` reported the OPENING limit on
> every row until the 2026-09-25 build. A row carrying `opening_limit` is corrected, and
> any other row with `steps > 0` is not. The "3 buys tail coverage past the one-tick
> deficit" reasoning assumed the cross sits ABOVE the ladder's end. From v0.4.3 until the
> fix it sat below it on the flatten's modal book.
> → [deferring a far-OTM flatten](2026-09-25-deferring-a-far-otm-flatten.md)

All three `watchdog.crossed` rows in the backup:

| seq | date | floor | market | deficit |
|---|---|---|---|---|
| 577342 | 2026-08-31 | 0.05 | 0.10 | 1 tick |
| 625225 | 2026-09-01 | 0.05 | 0.10 | 1 tick |
| 625292 | 2026-09-01 | 0.05 | 0.10 | 1 tick |

`crossing_limit` is derived from `crossing_debit`, i.e. from production quotes
(`[marketdata] source` has been `"live"` in every profile since 2026-07-20), so **the
price is a quote fact and admissible from cert** — the same carve-out already granted to
`entry_drift_abort_points`. What is *not* admissible is whether it filled.

But n=3, one trigger, one regime, and **it cannot grow**: no category in 780,853 rows
carries a leg bid or ask, so `crossing_debit` exists in the record only where a cross
actually fired. Recording it at `exit_started` and at walk-away is reported below.

## Finding 5 — the fill distribution is real and unusable, and the bias runs the wrong way

```
exit walks   filled steps=0: 12   steps=1: 5   steps=4: 1      (17/18 within one rung)
entry walks  filled steps=0: 30   steps=1: 9   steps>=2: 0     (39/39 within one rung)
```

The temptation is to read this as *rungs past 1 are unnecessary* and narrow the floor.
**Do not.** These are cert fills, and cert's multi-leg SPXW matching is undocumented. If
cert fills too easily, it **overstates** how little concession is needed — so the bias
runs against using this to argue for *less* range. It establishes only that provisioned
rungs beyond the first are rarely **reached**, which is a different claim.

## The decision

### `exit_walk_away_pct` — held at `0.20`

It governs only the credit-stop regime, and nothing there is broken: every credit-stop
exit that filled did so at step 0, 1, or once 4, inside the 4–8 rungs it provisions.

The reason narrowing is wrong is structural, not cautious. **An unreached rung is free.**
The walk stops at the fill; a limit walking up the grid cannot skip a resting offer (*corrected 2026-09-25: true of a resting COMPLEX offer, false of a natural derived from leg quotes — on a zero-bid long leg no leg-in exists; see the 2026-09-25 record*); and
the cross that follows is already capped at the width. So a wider floor can only fill at
or below what the cross would have paid, and its only costs are time and adverse
movement during the walk. Narrowing therefore buys nothing and would cost the one 4-step
fill on record. Lowering to 0.10 would halve the credit-stop range on the cert fill
evidence whose bias runs the wrong way.

Widening it is the *principled* direction — toward the width, where the cross already
sits — but that is the functional-form change below, not a value tweak, and it is the
one place where time genuinely costs money.

### `exit_walk_min_steps` — `1` → `3`, all three profiles

- **Scope is measured**: binds only below |0.75|, and zero of 23 credit-stop rows is
  under 0.95. It touches the 12 flatten rows and nothing else.
- **Affordable by two orders of magnitude**: 3 rungs is ~15–45s against 1800s of runway.
- **It is the one regime where the ladder is the only mechanism.** The cross needs a
  live offer and 2026-08-31 proved it can be absent — `CROSS_UNPRICEABLE`, attempt
  spent in 1.3 seconds, held to expiry at the full width.
- **Asymmetry ≈ 90:1.** An exhausted flatten ladder costs up to the width (~$900 at 2
  contracts). An extra rung costs $10, and only if reached.
- **Nowhere near the cap**: max floor at 3 on a 0.05 opening limit is 0.20 against a
  5.00 width.

**2 versus 3.** Both touch the same 12 rows. 2 covers the observed one-tick deficit plus
one; 3 covers plus two. The choice is tail coverage in the regime with the most runway
and no backstop, and it is explicitly *not* a fill measurement. 1 is what n=3 strictly
justifies and was already in place; 3 is chosen on the asymmetry.

### Not changed, and why

- `credit_multiple = 2.0` — untouched. The prohibition stands: conclusions only from
  `entry_credit_basis = realized`, and a number measured in cert cannot size it alone.
- `underlying_buffer_points = 5.0` — one exit fired on it (2026-09-01 18:42,
  `underlying_near_short_strike`). That is the backstop working, not a calibration
  signal.
- `sustained_s = 5.0` — no evidence in this dataset bears on it.
- `flatten_before_close_s = 1800.0` — the data **supports** it. That runway is precisely
  what makes three rungs free.

## Findings reported and deliberately NOT fixed

- ~~Cap the limit at the width.~~ **BUILT the same day — see below.** It was reported
  here as owed ahead of any further tuning of `exit_walk_away_pct`, and rather than
  leave the config flip to ship against a broken ceiling it was fixed first.
- **Record `crossing_debit`.** **MOSTLY BUILT — see below.** Every mark and
  `exit_started` now carry it. What is still owed is the walk-away INSTANT, and the two
  remaining gaps are named there.
- **Replace the percentage with an absolute tick or time budget, capped at width.** The
  principled fix and the one Finding 1 actually argues for: it removes the collapse
  below a tick, removes the unbounded top end, makes the parameter a quantity that can
  be reasoned about against the runway, and **deletes `exit_walk_min_steps` rather than
  tuning it** — that key exists only to patch the percentage's low end. This is why the
  two keys have always been described as one decision: they are one parameter. Behind
  the two items above.
- **`cost_to_close` can exceed the width and nothing says so.** The 5.05 mark is the
  instance. The zero-bid floor bounds it below; nothing bounds it above, on either the
  exit path or the entry path's `credit`.

## The width cap — BUILT 2026-09-17

Reported above as owed; fixed the same day rather than letting the config flip ship
against a broken ceiling. **`src/` only, no config key** — which changes this release
from config-only to a coupled image + config deploy.

**The invariant, stated once:** a vertical cannot be worth more than the distance
between its strikes. The long leg caps it, which is what "defined risk" means. So a
price above the width is arithmetically impossible for the instrument — never an
aggressive price, always a bad number. `watchdog._cross_limit` has always held this
(`min(debit, market.width)`) and was **the only place in the system that did**.

Two clamps, at the two places that skipped it:

- **`build._limit_price`** now takes a REQUIRED, keyword-only `width` and clamps the
  magnitude through a new `_cap_at_width`. This is where the observed breach was: the
  refusal it already had covers only a non-positive price, so a 5.05 mark on a 5.00-wide
  spread built an order at −5.05 unchallenged. `width` comes from `SpreadSignal.width`,
  the ACTUAL strike distance, already in scope at the build site. An unusable width
  (non-finite, ≤ 0) **raises** rather than skipping the cap — skipping would fail open,
  which is the defect.
- **`PriceWalk.from_policy`** takes an optional `width` and clamps the floor **last**,
  after the percentage, the `min_concession_steps` minimum and the credit clamp. A cap
  another clause may override is not a cap. `Decimal.copy_sign` keeps it
  **direction-free**, so the state machine keeps having no direction branch anywhere in
  it — the same contract `min_concession_steps` is written to.

**CAP, not refuse, and the distinction is the decision.** `UnpriceableSpreadError`
refuses because there is no price to express; here there *is* one, it is merely above a
ceiling the instrument itself defines, so the honest answer is the ceiling. Refusing
would strand a deep-ITM 0DTE spread with no automated path to close — strictly worse
than paying the most it can possibly cost.

**Where the width comes from, and why it is not a market read.** `Order.width` is a new
property reading the legs' own OCC symbols through `occ_strike`. That keeps
`PriceWalk` market-free, which is load-bearing: a rung must stay computable in a
completely dead book, and that property is exactly what the 2026-08-31 ladder fix turns
on. `None` means "cannot say" per `occ.py`'s standing rule, and failing closed here
means the floor is simply not capped — refusing to build a walk would strand a triggered
position over a symbol parse. The two bounds therefore do not share a failure mode: the
opening limit is capped from `SpreadSignal.width` regardless. For an order Argus built,
`None` is unreachable — `build_order` mints both symbols.

**The ceiling is quantized DOWN to the tick, never through `round_to_tick`**, which is
`ROUND_HALF_UP` and on an off-grid width would round the cap *outward*, putting the bound
back above the thing it bounds. Every SPX strike distance is already a multiple of 0.05,
so this is a no-op today; it is written this way so a ladder with odd spacing cannot
silently reopen the hole.

### What it changes, measured

| exit opening limit | floor before | floor after | rungs after |
|---|---|---|---|
| −5.05 (the 2026-09-03 mark) | — | *order no longer built at all; capped to −5.00* | — |
| −5.00 | −6.00 | **−5.00** | 0 |
| −4.50 | −5.40 | **−5.00** | 10 |
| −4.00 | −4.80 | −4.80 (inside the width) | 16 |
| −1.65 | −2.00 | −2.00 | 7 |
| −0.05 | −0.20 | −0.20 | 3 |

The −4.50 row is the case the 2026-08-31 record named and left. Note the change
**narrows** the walk in the deep regime, which is the safe direction and does not touch
`exit_walk_away_pct`'s value.

**The 2026-09-03 position, recomputed**: capped, the exit prices at −5.00 and realizes
`(0.70 − 5.00) × 2 × 100 = −860.00` — **exactly equal to holding to settlement**, where
it had realized −870.00. The $10 was the whole of the excess.

**The entry ladder is bit-identical**, and by arithmetic rather than by a flag: an
opening walk concedes downward from a credit `_limit_price` has already capped at the
width, so `abs(floor) ≤ abs(initial) ≤ width` holds before the clamp is reached. Pinned
by a test across the production entry credits — the same standing `min_concession_steps`
has.

### Tests

`tests/test_order_build.py` — the production case end to end through `build_order` from
legs that genuinely price above their own width (5.30 − 0.25), the cap binding AT the
width and not a tick early, the off-grid round-DOWN, direction-freedom, an unusable
width raising, and `width` being required and keyword-only.
`tests/test_execution_loop.py` — the floor table above, a sweep asserting **no rung on
any exit walk can exceed the width**, the clamp being last (an absurd
`min_concession_steps` on a near-width debit still stops at the width), entry
bit-identity, and a `None` width leaving the floor uncapped rather than breaking the
walk. Plus a **structural** test: every production `PriceWalk.from_policy` call must
pass `width=`, because the parameter defaults to `None` for the unit tests' sake and a
caller that forgets it gets an uncapped floor and no error — which is the shape of this
very defect. `tests/test_execution_models.py` — `Order.width` off real OCC symbols,
absolute distance whichever leg is higher, a gapped ladder, and the `None` cases.

Full suite: **2,155 passed.**

## Recording the offer — BUILT 2026-09-17 (partly)

Finding 4's instrument. `src/` only, no config key, **telemetry only — nothing decides
on it**, which is the same standing `scan_age_s` and `min_greeks_age_s` have.

`cost_to_close` is the zero-bid-floored **MID**. `crossing_debit` is the short leg's ask
minus the long leg's bid — the price a market-crossing limit actually fills at. The gap
between them is the entire question this pass could not answer, and it existed in the
record **only where a cross had already fired**: n=3 across 780,853 rows, and
right-censored there by `min(debit, width)`.

Two sites, both one-liners because the objects already held the number:

- **Every mark.** `TriggerEvaluation` already carries `market: SpreadMarket | None`, and
  `SpreadMarket.crossing_debit` already existed for `_cross_limit`. A new
  `TriggerEvaluation.crossing_debit` property plus one key in `to_payload()` puts the
  offer beside the mid on **`watchdog.check`, `watchdog.check_spent`,
  `_report_evaluability`'s pages and `watchdog.triggered`/`triggered_still`** —
  everything that splats that payload. At the 1s cadence that is a continuous record of
  the offer distance for every held position, all day, for free. *(Corrected
  2026-09-18: this line originally said "the unpriceable pages", which are hand-built
  rather than splatted and carry no market to report anyway; the fourth consumer is
  `_trigger`.)*
- **`watchdog.exit_started`.** RAW — not capped at the width, not tick-rounded. With
  `order.limit_price` (where the walk opens) and `walk_away_price` (where it stops) in
  the same row, one record now answers *was the resting offer inside the ladder's range,
  or was this walk always going to have to cross?*

`None`, never NaN, when the legs cannot be priced — `occ.py`'s "cannot say" rule applied
to a price. On the mark that is genuinely reachable and is what a **degraded tick** looks
like (1,447 of 2026-09-17's 19,626 live ticks). On `exit_started` the guard is defensive
only: `crossing_debit` and `cost_to_close` derive from the same four numbers, so a
non-finite quote refuses the build before anything is recorded — pinned by a test, so
nobody reads the guard as evidence the case is live.

**Amended 2026-09-18, at the merge with the quoted-spread change** (built independently
on 2026-09-15 per
[its design](../designs/2026-09-15-recording-the-quoted-spread.md), and it added the
same key to the same method). One thing from this section did not survive, on that
design's own rules — and a second was removed at the merge and then **RESTORED** the
same day:

- **The `exit_started` copy STAYS (removed at the merge, restored 2026-09-18 on review).**
  The merge removed it in favour of a join on `position.candidate_id` to the triggering
  row, on the design's §3.6 "join, do not copy", because `_trigger` writes the
  evaluation payload — `crossing_debit` included — *before* handing the same
  `evaluation.market` to `ExitExecutor.close`. **That join is exact only on the
  trigger's RISING edge**, where the row is Tier 1 `watchdog.triggered`. The trigger
  escalation falls only on a tick where nothing fires, so every LATER exit on a
  still-triggered position — the flatten on a spent position, the `NOTHING_AT_BROKER`
  and `CROSS_UNPRICEABLE` retries, the flatten's one-shot override — is preceded by
  Tier 2 `watchdog.triggered_still`: queued, so its `seq` can land AFTER the Tier 1
  `exit_started`, and dropped when the queue is full. Those are the flatten-regime exits
  this pass's re-derivation needs most. So `exit_started` carries `crossing_debit`
  again, through `payload_number`, off the very `market` the walk was handed — it cannot
  disagree with the triggering row, which was built from the same object.
  `test_nothing_in_src_reads_the_crossing_debit` allows that one path-qualified site
  (`execution/watchdog.py::close`) and nothing else; the rising-edge half stays pinned by
  `test_the_offer_an_exit_opens_against_is_on_the_triggering_row`. Rows written between
  the merge and the restore (none were deployed) would lack the key; readers use
  `.get()`.
- **The property's inline `is_finite` ternary is gone** — it was a fourth spelling of the
  one rule the design collapsed into `signal.models.payload_number`, and a multi-statement
  one, which `test_the_payload_guard_has_exactly_one_definition` would not have caught.
  The property now delegates to the helper, and `to_payload` reads the property, so the
  expression exists once.

**It made an existing finding visible rather than inferred.** In the flatten's modal
regime both legs quote `0.00/0.05`: the floored mid is `0.00` — which `_limit_price`
cannot express, so no order is built at all — while a real `0.05` offer is standing.
That is 2026-08-31's *"a mid of 0.00 can never reach the cross that would have priced it
at 0.05"*, now in the trail on every tick.

### What is still owed

- **The walk-away instant.** `_cross_limit` collapses three distinct causes into one
  `None` (no market, no offer, a price that rounds to zero) and discards the debit it
  saw, so `CROSS_REFUSED` and `CROSS_UNPRICEABLE` still record no market state at all.
  Fixing it means changing that method's return shape and adding a field to
  `ExitReport` — a design change, not a one-liner, and `ExitReport` has converters
  downstream, which is the 2026-09-02 under-copy hazard.
- **The mark still goes dark during the walk.** `check_once` skips any key in
  `_exiting`, and a walk runs 4–23s, so `exit_started` and `watchdog.crossed` bracket
  the walk without covering its middle. Marking an exiting position while suppressing
  only the send is the 2026-08-28 shape one state over and closes this properly.

## Reproducing the numbers

Read-only, against the backup or — wrapped for the droplet, where the `sqlite3` CLI is
absent from the `slim-bookworm` image and both databases sit on the `argus-state` volume
at `/data` — inside the running container:

```
sudo ARGUS_ENV="$(sudo sed -n 's/^ARGUS_ENV=//p' /etc/argus/host.env)" \
  docker compose -f /opt/argus/docker-compose.yml exec -T argus python - <<'PY'
import json, sqlite3
for DB in ("/data/argus-live.db", "/data/argus.db"):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    for ts, p in con.execute("SELECT ts,payload FROM audit_log"
                             " WHERE category='watchdog.exit_started' ORDER BY seq"):
        o = json.loads(p)
        op = abs(float(o["order"]["limit_price"])); fl = abs(float(o["walk_away_price"]))
        print(f"{DB} {ts[:19]} open={op:.2f} floor={fl:.2f} rungs={round((fl-op)/0.05)}"
              f"  {','.join(o.get('trigger_reasons') or [])}")
PY
```

Swap the category for `watchdog.crossed` and difference `crossing_limit` against
`walked_away_at` for the deficit; for the outcome mix use `walk.finished`, splitting on
whether `external_identifier` starts `argus-close-` or `argus-open-`. **(2026-09-25: only on
rows carrying `opening_limit`, or where `exit_finished.walk.steps = 0`. An older row with
`steps > 0` records the opening limit under `walked_away_at`, and this recipe then gives the
wrong answer.)**

## Tests

No behaviour changed, so no new test. `tests/test_config_profiles.py` continues to pin
identical key sets across all three profiles and the divergence allowlist — the value is
now `3` in all three, so this introduces **no new divergence**; only the `trigger_source`
prose differs, which was already allowlisted on 2026-09-09.
`tests/test_execution_loop.py`'s table-driven floor guard is unaffected: it parameterizes
`min_concession_steps` directly rather than reading the deployed config. Full suite:
2,121 passed.
