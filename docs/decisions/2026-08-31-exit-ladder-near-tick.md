# The exit ladder had no range in the regime the flatten fires in (2026-08-31)

*v0.3.13's first live-fire day and P2's first-ever fire. Two SPXW 0DTE credit spreads,
2 contracts each, both comfortably OTM. At 15:30:00 the flatten fired on both,
correctly and to the second. The exit price ladder then contributed **nothing** to
either exit: both walked away after **0 step(s)** and the entire outcome of both
flattens rode on a single instantaneous market-crossing limit. One found an offer and
filled; one did not, and spent the position's only attempt in 1.3 seconds.*

> BUILT 2026-08-31, design-first and signed off before any code — the same gate P3 and
> P5 took, for the same reason: this touches exit price construction and the one-attempt
> rule. Adds a REQUIRED `[watchdog] exit_walk_min_steps`, so **the config deploys WITH
> the image**. Investigation:
> [`docs/tasks/2026-08-31-exit-ladder-near-tick-investigation.md`](../tasks/2026-08-31-exit-ladder-near-tick-investigation.md).

Nothing about the day was a safety breach. No wrong order was submitted, no rail was
bypassed, and the day closed at max profit on both spreads. What failed is that
nothing existed between the mark and the cross.

## What happened

```
call 7710/7715 — worked
15:30:00.129  watchdog_triggered  fired=['flatten_before_close']  cost_to_close=0.0
15:30:34.520  walked_away after 0 step(s) at -0.05
15:30:36.897  watchdog_crossed_market: walked_away_at=-0.05  crossing_limit=-0.1
15:30:38-39   booked 4 leg fill(s)

put 7645/7640 — did not
15:30:00.133  watchdog_triggered  fired=['flatten_before_close']  cost_to_close=None
15:30:33 → 15:39:33   19x EXIT STILL NOT IN THE MARKET, to 571.0s
15:39:59.499  walked_away after 0 step(s) at -0.05
15:40:00.815  watchdog_cross_unpriceable  'no offer on the short leg?'
15:40:30 → 15:59:31   39x EXIT ATTEMPT SPENT AND STILL HELD, to 1170.6s
```

## The defect: a percentage of a price on a tick grid

`PriceWalk.from_policy` computed the walk-away floor as
`round_to_tick(initial − |initial| × walk_away_pct)`. The concession vanishes when it
is smaller than half a tick:

    pct × |initial| < TICK/2   ⇒   the floor rounds back onto `initial`   ⇒   zero rungs

At `exit_walk_away_pct = 0.20` that is `|initial| < 0.125` — **every exit opening at a
debit of 0.10 or less**. For a 0DTE credit spread being flattened at 15:30 that is the
ordinary case, not a tail: a winning spread is nearly worthless by then, which is the
whole reason it is winning. **The ladder built for a spread under stress was being
applied to a spread in the opposite state.**

The asymmetry that made this an oversight rather than a judgement call: the credit
direction has an explicit degenerate-case guard right there in the function
(`initial > 0 and raw_floor < TICK`), with a comment. The debit direction had none —
its degenerate case is different (the floor collapses onto the initial rather than
crossing zero), so the existing guard neither catches it nor was written to.

## Why this is worth range at all — the finding that decided the number

The intuitive reading of walk-then-cross is that the walk is the gentle attempt and
the cross is the real escalation behind it. **In this regime that is backwards.**

- A **rung** is pure arithmetic on the walk's opening limit. `PriceWalk.price_at`'s
  call graph is `{round_to_tick}` and `loop.py` imports no market data by design, so a
  rung is priceable in a completely dead book.
- A **cross** re-reads the market. `_cross_limit` returns `None` when
  `crossing_debit` is not finite or not positive — no offer on the short leg, no cross.

So in exactly the regime where the cross fails, the ladder is the **more** robust of
the two mechanisms, and it had zero range there. That is what happened to the put.

Two further consequences, both from the day's own records:

- **P2's own argument for keeping the walk was already false when it was written.**
  Its question 2, reason 2 reads *"the walk from mid to walk-away is a handful of COA
  cycles, seconds, against 1,800s of runway."* At a 0.05 mid it is **zero** cycles.
  The premise — the flatten changes WHEN, never HOW — is right and stands; the
  arithmetic underneath it was not. This change restores the premise rather than
  amending it. → [a clock is the third trigger](2026-08-28-time-based-flatten-trigger.md)
- **The call spread's successful exit did not need `_cross` at all.** It filled at
  mid + 1 tick. `_cross` is the one place on the exit path that submits **without the
  duplicate guard** — deliberately, because a guarded walk and a positive
  `_await_settled` confirmation run in front of it. Every exit that completes on the
  ladder never reaches that path, never needs the settle window, and never opens the
  double-close window that path exists to manage.

## The change: a minimum in STEPS, on the POLICY

`WalkPolicy` gains `min_concession_steps`, and `from_policy` applies it as a floor
under the percentage:

```python
raw_floor = round_to_tick(initial - abs(initial) * Decimal(str(policy.walk_away_pct)))
least = round_to_tick(initial - policy.step * policy.min_concession_steps)
if abs(raw_floor - initial) < abs(least - initial):
    raw_floor = least
if initial > 0 and raw_floor < TICK:      # unchanged, and it runs AFTER
    raw_floor = TICK
```

Four things about the shape are decisions rather than mechanics.

**It is not a direction branch, and that was the point of putting it on the policy.**
`PriceWalk`'s docstring is emphatic that the state machine has none — *"an exit walk is
an entry walk with a different policy and a negative starting price, and that is the
whole difference."* Conceding is subtraction for both a credit and a debit, so `least`
is sign-correct with no test on `initial`. The asymmetry that genuinely exists lives in
`WalkPolicy`, whose own docstring says *"Everything about HOW a walk paces and steps.
Entry and exit differ ONLY here."* An `initial < 0` branch in `from_policy` was the
obvious alternative and was rejected on that basis; the existing credit clamp remains
the only direction branch in the file.

**`for_entry` passes 0 explicitly.** The same collapse exists on the entry side below a
credit of 0.25 and is out of scope here — see the reported finding below. Writing the 0
makes the entry side provably unaffected and makes the opt-out greppable from the walk
rather than implied by its absence.

**The two guards compose in the safe order.** The minimum applies first, the credit
clamp after, so a credit walk given a nonzero minimum still cannot be pushed across
zero into a debit. Reversing them would let it. A test pins it.

**STEPS, not ticks.** The guarantee is "this many reprices", so it survives
`exit_step_ticks` being raised above 1. Counting ticks would silently halve the
promised rungs on a 2-tick walk.

### Shipped at 1, and why not 3

`exit_walk_min_steps = 1` is the **unique** value that changes the two broken rows and
no row that already works:

| exit opening limit | before | at 1 | at 3 |
|---|---|---|---|
| −0.05 | −0.05 / **0 rungs** | −0.10 / 1 | −0.20 / 3 |
| −0.10 | −0.10 / **0 rungs** | −0.15 / 1 | −0.25 / 3 |
| −0.15 | −0.20 / 1 | unchanged | −0.30 / 3 |
| −0.25 | −0.30 / 1 | unchanged | −0.40 / 3 |
| −0.50 | −0.60 / 2 | unchanged | −0.65 / 3 |
| −0.75 and beyond | | unchanged | unchanged |

One step is not a tuning choice — it is the minimum coherent expression of any nonzero
percentage on a tick grid. The case for more range is real (it is the dead-feed argument
above), but it is a $30-per-spread decision at current size and the only exit-walk
telemetry that has ever existed is **n = 2, both zero-step**: evidence about the
ARITHMETIC, not about the right number. It belongs to the owed data-backed
`exit_walk_away_pct` pass, and **raising it is a config flip, not a deploy** — the same
shape P5 used for `unmanaged_order_cancel`.

### The risk, in dollars at current size

2 contracts, $100 multiplier, so **1 tick = $10 per spread**. Defined risk per spread
≈ $900; `max_loss_per_position` $1,250; `daily_max_loss` $2,500.

The change adds **at most $10 per spread**, and only for exits opening at 0.05 or 0.10.
**The expected sign is negative — it is a saving.** The alternative to the extra rung is
not holding; it is the cross that immediately follows, priced at the short leg's ask
minus the long leg's bid. `crossing_debit` is always ≥ the floored `spread_mid` by
construction, and **a limit walking up the grid cannot skip a resting offer** — it fills
at the offer, which is where the cross would have paid anyway. The ladder can pay more
than the cross only if the market moves against it during the walk, in which case the
cross would have paid more too.

**Nothing touches the width cap.** `_cross_limit`'s `min(debit, width)` is unchanged,
and the minimum cannot reach it: at 1 the guard binds only for `|initial| < 0.25`, a
maximum floor magnitude of 0.30 against a 5.00 width.

## `CROSS_UNPRICEABLE` no longer spends the exit attempt

The second half of the day's failure. The put's cross could not be priced — a
45-point-OTM 0DTE short leg at 15:40 had no offer to lift — and `CROSS_UNPRICEABLE`
landed in the `is not WALKED_AWAY` catch-all, spending both the ordinary attempt and
the flatten's one-shot override on a transient feed state.

It is now retryable, in exactly the shape P0 gave `NOTHING_AT_BROKER` three days
earlier. **The case for it is stronger than P0's, not weaker**, and that is the whole
argument:

| outcome | what the broker said | one-attempt argument |
|---|---|---|
| `CROSS_REFUSED` | still working / absent / unreadable / unnamed | **applies.** Keep spending. |
| `NOTHING_AT_BROKER` | asked under this identifier, named nothing — its own record: *"not a claim that no order can ever appear"* | does not apply |
| `CROSS_UNPRICEABLE` | **named this specific order and confirmed every disposition terminal** | does not apply — a *positive* confirmation where P0's was an absence |

`CROSS_UNPRICEABLE` is unreachable except **through** `_await_settled` returning
`outcome=None`, which is the single most demanding confirmation in the module.

**Why this is a third narrow exception and not the uniform retry the record forbids.**
The licence is the same one `_cross` itself already acts on: `_cross` responds to that
confirmation by submitting a second closing order **unguarded**, while the retry
responds to the identical confirmation by rebuilding on the next cadence tick and
submitting **through `DuplicateOrderGuard`** (`guard_first_submission=True`). The retry
is strictly better protected than the escalation it replaces. It does not generalize:
it is available only to outcomes downstream of that positive confirmation.

Four properties carried over from P0 unchanged:

- **The licence is the guard, never the outcome's own answer.** If anything did land,
  the next tick comes back `BLOCKED`.
- **Bounded by a COUNT, not a rate** — each retry costs a submission.
- **ONE budget**, shared with `NOTHING_AT_BROKER` rather than a second `_EXIT_SEND_ATTEMPTS`
  of its own. The hazard bounded is how many closing orders a broker sees from a
  position that will not close, and that is a single hazard however the attempt failed.
  A position that has already burned two on `NOTHING_AT_BROKER` has one left, not three.
- **Exhausting the budget lands in `_attempted` and pages exactly as before.** The
  terminus is unchanged.

`_RETRYABLE_EXITS` is a mapping rather than two `elif`s specifically so that the
one-attempt rule's exception list is **one greppable object**. `CROSS_REFUSED` is not
in it, returns before `_cross_limit` is ever called, and is pinned by its own test —
they share a method and it is the easy thing to break by accident.

**One alert moved.** `_cross` no longer emits `watchdog_cross_unpriceable` directly; the
watchdog's escalation raises that kind instead, on its own tag (`_CROSS_UNPRICEABLE`,
separate from `_NOTHING_SENT` because on a phone they read identically and the
operator's next move is opposite — broker versus feed). The payload gains what the old
one could not know: the send count, the budget, and `candidate_id` at the top level so
B1's throttle scopes it by TRADE IDEA like every other per-position page rather than by
the closing order's identifier. `_record_exit`'s `watchdog_exit_cross_unpriceable`
still fires for the attempt itself; alerting from both places would have paged three
times for one event. `CROSS_REFUSED` keeps its own notify — it is terminal, so there is
no retry context to wait for.

## Honest limits

- **Neither change would have altered 2026-08-31's outcome.** The put's short leg had
  no offer for the rest of the session: no rung would have filled and three attempts
  would have failed the same way. What they change is the case the day's irony
  conceals — *a short leg with no offer is a short leg with no risk*, so the position
  Argus could not close was the one that did not need closing. A gappy 0DTE book can
  strand an **ITM** short leg the same way, and then the identical path spent the
  attempt on a position that genuinely needed out.
- **Cert cannot prove the economics.** Sandbox orders match inside tastytrade's own
  book against 15-minute-delayed quotes, so a fill there says nothing about the Cboe
  auction. The case for the extra rung rests on the arithmetic above — a limit cannot
  skip a resting offer — not on a cert measurement. What cert *can* prove is that the
  reprice goes out (the walk's reprice cycle is already on the PROVEN list) and that
  `CROSS_REFUSED` still refuses.
- **Cert cannot produce the retry's real setting** either: a short leg with genuinely
  no offer while a position is still held and triggered. v0.3.13's release doc already
  said so about the flatten's send allowance. 2026-08-31 produced it by accident. The
  tests with injected quotes are the only coverage that will exist.

## Findings reported and deliberately NOT fixed

Each is real, each was found in this pass, and each is out of this change's scope.

- **The entry walk has the identical collapse below a credit of 0.25, and
  `signal.min_credit = 0.50` is the only thing preventing it.** `strategy.py` emits
  nothing lower, so `initial ≥ 0.50` by construction — but nothing in `[execution]`,
  `loop.py` or `entry.py` records that dependency, and `tools.order_smoke` already sets
  `min_credit = 0.0` for its own purposes. Lower that `[signal]` key to 0.25 and every
  entry walk silently becomes zero-rung; combined with `entry.py`'s
  one-attempt-per-`candidate_id` rule, the first unfilled cycle then RETIRES that strike
  pair for the process lifetime. The cheapest containment later is a config-load rail
  refusing `min_credit` at or below the collapse boundary for the configured
  `entry_walk_away_pct`. A test pins the collapse so it cannot be discovered twice.
- **`execution.walk_source`'s measured claim is arithmetic, not market behaviour.** It
  reads *"84% of candidates (63/75, median credit 0.525) get exactly ONE $0.05 reprice
  before the floor, 16% get two."* At `entry_walk_away_pct = 0.10`, credits of
  0.50–0.75 get exactly one rung and ≥0.80 get exactly two — deterministically, before
  any order is sent. The 84/16 split therefore restates *"84% of candidate credits were
  below 0.80"*. It is true and it is **not evidence about fill behaviour, the auction,
  or the right value of the percentage.** Correct the string in the same edit that
  re-derives the number.
- **The walk's floor has no width cap.** `_cross_limit` caps at `min(debit, width)`;
  `PriceWalk` does not. On a 5-wide spread an exit opening at −4.50 gets a floor of
  −5.40 and 18 rungs, the last several offering to pay more than the spread can possibly
  be worth. Not reachable in the modal case (`credit_multiple` fires near a 1.00
  cost-to-close) but reachable on a gap open or a stale wide quote. `PriceWalk` is
  deliberately market-free, so this belongs to the pass that reconsiders the floor's
  construction whole. **The minimum shipped here can never contribute to it.**
- **`cost_to_close` and `crossing_debit` have different degenerate cases**, because the
  zero-bid floor is applied to one and deliberately not the other. Both legs quoting
  0.00/0.05 gives a mid of 0.00 — `_limit_price` raises, the exit returns
  `NOT_ATTEMPTED`, and the spread can therefore **never reach the cross that would have
  priced it at 0.05**. A short leg with no offer gives the opposite. Both are why the
  put's whole exit attempt lasted 1.3 seconds.
- **`staleness.market_data_max_age_s = 2.0` governs the exit path end to end** — trigger
  evaluation, `_build_exit_order`, and `_cross_limit`'s re-read all take their bound
  from the one `LegQuotes` instance. Leg quotes on a 45-point-OTM 0DTE wing were running
  2–17s old, so the put could not price for **571 consecutive seconds** while the
  flatten was already firing. Same altitude mismatch as
  [book-confirm timeout](2026-08-21-book-confirm-timeout-bound.md) and the
  [greeks role split](2026-08-24-entry-leg-admission-is-role-scoped.md), and the fix has
  precedent in both: a **separate bound for the separate role**, never a raised shared
  one. The seam is clean — `LegQuotes.resolve` is used only by the watchdog and the exit
  path, while `LegQuotes.spot` is the half shared with the entry walk's drift breaker.
  **Never fix this by raising `market_data_max_age_s`**, which also governs entry pricing
  and the gate's staleness rail.

## Tests

`tests/test_execution_loop.py` — the table-driven regression guard (−0.05 / −0.10 /
−0.15 / −0.25 / −0.50 / −1.00 → floor and rung count; it fails at its first two rows
against any purely multiplicative floor), the defect itself pinned at
`min_concession_steps=0`, the minimum only ever widening a walk that is already wider,
STEPS-not-ticks, the credit clamp still binding after the minimum, and `for_entry`
opting out with the production entry ladder unchanged.

`tests/test_watchdog_exit.py` — a flatten on a spread marked at one tick submits at
−0.05, **reprices once at −0.10**, and still crosses at the natural cost; the near-tick
ladder stays far inside the width cap; the unpriceable cross alerts through
`_record_exit` rather than a second notify.

`tests/test_watchdog.py` — an unpriceable cross does not spend the attempt, shares the
one send budget with `NOTHING_AT_BROKER`, still reaches the spent page when the budget
is out, and **`CROSS_REFUSED` still spends on the first try** and never charges the
budget.

`tests/test_config.py` — the key is required, and 0 is refused along with negatives.

## Deploy

| change | files | config |
|---|---|---|
| `min_concession_steps` + `exit_walk_min_steps` | `loop.py`, `config.py`, all three `argus*.toml` | **REQUIRED new key — the config deploys WITH the image** |
| `CROSS_UNPRICEABLE` retry | `watchdog.py` | none |

Both ship together. 1,755 tests pass.
