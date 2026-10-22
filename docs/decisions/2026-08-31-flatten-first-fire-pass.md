# The flatten's first fire (2026-08-31)

v0.3.13's first live-fire day, and `FLATTEN_BEFORE_CLOSE`'s first fire ever. The
trigger worked exactly as designed. **The exit path underneath it did not, and the
reason is arithmetic rather than judgement:** the price ladder has zero range when
the spread's mark is inside one tick, which on a 0DTE book at 15:30 is the ordinary
case rather than the exceptional one.

Five findings. Two are consequential and three are small. Four mechanisms were
exercised for the first time, three of them successfully.

> **Findings 1 and 2 are CLOSED, designed first and signed off the same way P3 and P5
> were, then built 2026-08-31.** `WalkPolicy.min_concession_steps` puts a floor under
> the percentage (a REQUIRED `[watchdog] exit_walk_min_steps`, shipped at 1, so **the
> config deploys WITH the image**), and `CROSS_UNPRICEABLE` no longer spends the
> position's exit attempt. Finding 3 is reported and deliberately unfixed; findings 4
> and 5 are untouched. → [the exit ladder](2026-08-31-exit-ladder-near-tick.md)

## What ran

Two SPXW 0DTE credit spreads, 2 contracts each, both entered inside the first
twenty minutes and both comfortably OTM all day.

| | put 7645/7640 | call 7710/7715 |
|---|---|---|
| entered | 10:00:16 | 10:00:50 |
| entry walk | 1 reprice | 0 reprices |
| credit (fill-derived) | 0.55 | 0.50 |
| final order limit | 0.50 | 0.50 |
| flatten fired | 15:30:00.133 | 15:30:00.129 |
| exit | **failed** — `cross_unpriceable`, held to expiry | closed 15:30:39 via the market cross at 0.10 debit |
| result | full credit, $110 | realized $80 |

Day: $190 gross, $31.43 fees, **$158.57 net**. `tools.mark --dry-run` puts the
held-to-settlement counterfactual at $210, so the flatten's realized cost on the
call was $20 of debit plus roughly $10 of exit commissions it would not have paid
at expiry.

The gate saw 2,567 candidates and allowed 4 (two of which were already-attempted
strike pairs); 2,563 were denied on `concurrent_limit`, 11 on
`account_stream_stale`, 1 on `position_max_loss_exceeded`. Cert was flapping all
day — 211 `account.refresh_failed` against 85 recoveries, concentrated on
`/positions` and `/balances` with `ReadTimeout`, `Couldn't parse response: <html>`
and one explicit 502 from `sandbox-nginx.service.consul`.

## Finding 1 — the exit ladder has no range when the mark is inside one tick

**Open. This is the finding of the day.** Both flatten exits produced the same line:

```
15:30:34.520 walk argus-close-20260831-call-7710-7715: walked_away after 0 step(s)
             at -0.05 (walk-away threshold reached at -0.05)
15:39:59.499 walk argus-close-20260831-put-7645-7640:  walked_away after 0 step(s)
             at -0.05 (walk-away threshold reached at -0.05)
```

Zero steps, both times. The price-walking loop contributed **nothing** to either
exit, and the entire outcome of both flattens rode on the single market-crossing
limit that follows a walk-away.

The cause is in `PriceWalk.from_policy` (`execution/loop.py:352`), which computes
the walk-away floor as a **percentage of the initial price**:

```python
raw_floor = round_to_tick(initial - abs(initial) * Decimal(str(policy.walk_away_pct)))
if initial > 0 and raw_floor < TICK:
    # A credit walk must never reach zero or cross into a debit ...
    raw_floor = TICK
```

At `watchdog.exit_walk_away_pct = 0.20` and `TICK = 0.05`, an exit opening at a
0.05 debit gets `0.05 - (0.05 × 0.20) = 0.06`, which `round_to_tick` snaps back to
`0.05`. Floor equals initial. Verified across the range:

| exit opening limit | floor | ticks of range |
|---|---|---|
| −0.05 | −0.05 | **0** |
| −0.10 | −0.10 | **0** |
| −0.15 | −0.20 | 1 |
| −0.25 | −0.30 | 1 |
| −0.50 | −0.60 | 2 |
| −1.00 | −1.20 | 4 |

**Any exit whose opening limit is a debit of 0.10 or less has no walk at all.**

**Why this is an oversight and not a judgement call.** The credit direction has an
explicit degenerate-case guard right there, with a comment explaining it. The debit
direction has none — because its degenerate case is *different*. A credit walk's
failure mode is crossing zero into a debit; a debit walk's is the multiplicative
allowance rounding to less than one tick, so the floor collapses **onto** the
initial. The existing guard was never written to catch that and does not.

**Why it is the modal case rather than a tail.** A winning 0DTE credit spread is
nearly worthless by 15:30 — that is what makes it winning. So the flatten's normal
operating regime is precisely the regime where the ladder degenerates. The
walk-then-cross shape was designed for a spread under price stress, with
`credit_multiple` breached and a real two-sided market on both legs. The flatten
fires on positions in the opposite state and inherits a ladder sized for the other
one.

**The entry walk has the same shape one step down**, at `entry_walk_away_pct = 0.10`:
credits of 0.25 and below get zero rungs, 0.30–0.55 get one, 0.85 gets two. Today's
entries at 0.50/0.55 therefore had exactly one rung each — which is consistent with,
and may fully explain, `argus.toml`'s `walk_source` claim that *"84% of candidates
(median credit 0.525) get exactly ONE $0.05 reprice before the floor."* **That
measurement may be describing this arithmetic rather than describing the market**,
which changes what it means for the owed walk-parameter pass. Not confirmed here.

→ design prompt: [`docs/tasks/argus_exit_ladder_near_tick_prompt.md`](../tasks/argus_exit_ladder_near_tick_prompt.md)

## Finding 2 — a cross that finds no offer spends the position's only attempt

**Open, and it is the second half of finding 1.**

```
15:40:00.815 watchdog_cross_unpriceable
             reason='no usable crossing price (no offer on the short leg?)'
```

`_cross_limit` (`execution/watchdog.py:1285`) returns `None` when
`market.crossing_debit` is not finite or not positive. The put's short leg — a
45-point-OTM 0DTE option at 15:40 — had **no offer to lift**. That produced
`ExitOutcome.CROSS_UNPRICEABLE`, which lands in the `is not WALKED_AWAY` catch-all
and spends the attempt.

The put's entire exit attempt lasted **1.3 seconds** — from the walk getting its
first usable price at 15:39:59.499 to `CROSS_UNPRICEABLE` at 15:40:00.815 — and
consumed both the ordinary attempt and the flatten's one-shot override. It then
paged 39 times over 1,170.6s and went to expiry.

**This is the open decision's own named cell, now with evidence.**
`docs/decisions/README.md`'s *"Should a confirmation-gated exit RE-ATTEMPT exist?"*
row already identifies `CROSS_UNPRICEABLE` as structurally safe to retry — it
reaches `_attempted` having ALREADY confirmed the abandoned order terminal, because
the refusal check runs first (`watchdog.py:1125`) and passed. Nothing is at the
broker; only the price construction failed. That is the same distinction P0 drew
three days earlier for `NO_ORDER_EXISTS` / `NOTHING_AT_BROKER`.

**What made today benign will not always.** A short leg with no offer is a short leg
with no risk, so the position Argus could not close was the one that did not need
closing. That correlation is not guaranteed: a gappy 0DTE book near the close can
strand an ITM short leg the same way, and the identical path would spend the attempt
on a position that genuinely needed out.

**A correction worth recording, because the first reading of this day got it wrong.**
The spent-position pages repeatedly carried `cost_to_close=0.0` (15:42:31, 15:43:01,
15:44:31, 15:45:01, 15:49:01, 15:50:01, 15:52:31, 15:54:02, 15:56:01). It is
tempting to read those as free closes that a retry would have captured. **They are
not.** `cost_to_close` is a MARK, and a 0.00 mark with no offer on the short leg is
not a closeable position. Any fix reasoned from "it was free to close and nothing
could act" would be reasoning from a number that was never executable. The defect is
upstream, in finding 1's range and in this finding's taxonomy — not in how often the
attempt may be re-spent.

## Finding 3 — the exit path is gated by an entry-sized staleness bound

**Open, deliberately not fixed with finding 1.** The put could not price at all from
15:30:00 to 15:39:59 — nineteen `EXIT STILL NOT IN THE MARKET ... (unattempted)`
pages, reaching 571.0s — because `staleness.market_data_max_age_s = 2.0` governs the
exit path as well as entry. The `problems` arrays name it directly:

```
"quote for .SPXW260831P7645 is 3.12s old (max 2s)"
"quote for .SPXW260831P7640 is 5.82s old (max 2s)"
```

Spot was updating every second throughout; it was the SPXW **leg** quotes that went
sparse near the close, running 2–17s old.

That bound is sized for delta-selecting a strike during the entry window. It is
being applied to closing a 45-point-OTM position at 15:35. Structurally this is the
same error as A4 (`2026-08-21-book-confirm-timeout-bound.md`, a bound inherited from
a cadence rather than from the thing being awaited) and the same shape as the
2026-08-24 greeks role split (one threshold serving two roles that need different
ones). The precedent for the fix — give the role its own bound — exists twice.

**The obvious fix is forbidden and stays forbidden.** Raising
`market_data_max_age_s` also loosens entry pricing and the gate's staleness rail.
CLAUDE.md names this rule specifically, from the 2026-08-13 pass, and today is
exactly the day that makes breaking it tempting.

## Finding 4 — the book disagreement ran six hours, inverted, and never resolved

**Open, and the cause is not established.**

```
10:01:22  BOOK DISAGREEMENT: broker holds 2 leg(s), ledger has 4
15:59:52  book_disagreement_still: broker_legs 4, ledger_legs 2, held_but_unbooked
```

One Tier 1 `watchdog.book_disagreement`, 1,535 `_still` records (continuous at the
15s refresh cadence from 10:01 to 16:00), and **no `watchdog.book_agrees` at any
point** — the only record that closes an escalated episode.

What is established: `account.position_evicted` was never written and the journal's
`account cache: dropped` line never appeared, so the cache did not lose the legs on
the age gate. The order stream delivered all four entry legs (the ledger booked
them); the position channel — `CurrentPosition` frames plus REST re-seeds — only
ever showed two, and three successful re-seeds (10:01:24, 10:01:48, 10:07:15) did
not repair it.

The benign reading is that cert's book lagged the call spread's entry all day and
then lagged its close, flipping the direction when the ledger dropped to 2. The
worse reading, which the available evidence does not exclude, is that cert never
booked the call's **opening** legs and then booked its **closing** legs, leaving two
phantom inverted positions. The discriminator is which four symbols the broker
reported and their signs.

**The consequence while it ran:** `open_spread_equivalent = max(ceil(2/2),
ledger_spread_count) + working = max(1, 2) + 0 = 2`. The concurrency cap was held
entirely by the ledger term for six hours, with the broker-derived term reading one
spread. This is the 2026-08-17 "take the HIGHER of the two counts" rule doing
exactly what it was built for — but the redundancy was spent, not spare, and had the
ledger term degraded too the gate would have authorized a third spread against a cap
of two.

## Finding 5 — two small visibility and consistency gaps

**Both small, both open.**

**`tools.desk` cannot see a spent position's marks.** It reported the put marked
`@15:29:51` — nine seconds before the flatten — because it reads `watchdog.check`
and a spent position emits `watchdog.check_spent`. The watchdog looked 1,175 more
times after that. P1 put the mark on `/status` and on the repeating page but not
where desk looks.

**`final_limit_price` reports a rung that was never live at the broker.**
*(Corrected 2026-09-01. The first reading of this called it an `entry_credit`
inconsistency — "`/status` says 0.55, which is the fill, but desk documents that
field as the limit, and those cannot both be right." They can. `entry_credit` is
`head["limit_price"]` from the NEWEST FILLED opening order (`store.py:871`) — a
limit, exactly as documented — and a limit order normally fills AT its limit, so
fill 0.55 and limit 0.55 agreeing is the ordinary case, not a contradiction. There
is no `entry_credit` gap on this position.)*

The number that matches neither is desk's LIMIT column, which reads
`WalkResult.final_limit_price` — and that is `state.sent.limit_price`
(`loop.py:1282`), the limit the walk believes it last SENT, not the limit of the
order that filled. Desk reported 0.50 for the put; the order that filled had a limit
of 0.55.

The day recorded `order.replace_attempt: 1` and `order.replace_failed: 1`, and the
put is the only entry with `steps: 1`. **Leading hypothesis: the reprice 0.55 → 0.50
failed, the original 0.55 order filled, and both `final_limit_price` and `steps`
describe a reprice that never landed.** Not confirmed.

If that holds, the consequence reaches past display. `steps` is what desk's
EXECUTION QUALITY column means by *"reprices needed to get filled"*, and it is the
same field `argus.toml`'s `walk_source` measured its *"84% of candidates get exactly
ONE $0.05 reprice before the floor"* from — which would give a **second, independent**
reason that number needs re-deriving before it informs anything, alongside finding
1's zero-range arithmetic. The 9–18% `entry_credit` gap itself is not directly
implicated: it was measured against realized fills rather than against `steps`. But
it is worth re-checking now that a same-day fill/limit pair has been observed
agreeing exactly.

## What was exercised for the first time

**Four mechanisms, three of them successfully.**

**The market-crossing escalation** (`watchdog.crossed: 1`). Walked away at −0.05,
crossed at −0.10, filled in two seconds across four leg movements. It worked. Its
width cap was never approached. Struck from CLAUDE.md's NOT YET EXERCISED list.

**Partial fills**, on entry. Order 1428425 booked four leg movements on a two-leg
order across 3.07s (10:00:43.972 → 10:00:47.038); order 1428418 booked its two legs
cleanly 0.265s apart. `store.apply` returns only what moved, so at least two of the
call's four are quantity increments on a leg already booked. Handled correctly — the
spread came out at `contracts: 2`, `problems: []`, no `watchdog.leg_imbalance`.
Struck from the list.

**P5's residual registry**, twice, end to end:

```
15:30:34.520 residual order 1429112 registered (argus-close-...call, walked_away, cancel_requested=True)
15:30:35.974 residual order 1429112 resolved: dead
15:39:59.499 residual order 1429137 registered (argus-close-...put,  walked_away, cancel_requested=True)
15:40:00.682 residual order 1429137 resolved: dead
```

Registered at `_finish` on a walked-away order with a cancel outstanding, resolved
on the very next poll, 1.2–1.5s each. No `order.residual_would_cancel`, no
`order.residual_unmanaged` page. That is precisely the shape v0.3.13's release doc
predicted, and it is real `would_cancel`-stage evidence — the doc says explicitly not
to wait for a `would_cancel` before promoting.

**A `Cancel Requested` order going terminal** — in the benign fast case only. Both
residuals above were cancel-requested and observed DEAD within 1.5s. The concerning
case, an order that *stays* at `Cancel Requested`, is still unexercised and stays on
the list.

## What was confirmed

**P2 fired to the second, on a position the market could not price.** 15:30:00.129
and .133, `fired: ['flatten_before_close']`, with the put's `cost_to_close` already
`None` at the moment of firing and `unknown_reasons: ['credit_multiple']` alongside
it. The market-dependent trigger was dark and the clock fired anyway. That is the
entire thesis of the third trigger, demonstrated on the first attempt.

**P1 is the reason any of this is legible.** 1,175 `watchdog.check_spent` records and
39 escalating pages carrying the live mark, `cost_to_close` rendered as `unpriced`
when `None` rather than as a number. Before P1 the put would have been thirty minutes
of silence.

**`blind` is still reachable** — `watchdog.unevaluable: 3`, `evaluable_again: 3`, all
episodes 1.0s, at 15:01:34 and 15:01:40 on `credit_multiple,underlying_near_short_strike`,
0.0% of 37,965 ticks. P2's `_MARKET_TRIGGER_REASONS` split works; had `blind` been
measured over all reasons it would be identically `False` and this count would be
zero. Note also *why* 15:30–16:00 shows `degraded` (2,399) rather than `blind`: the
flatten was live as the backstop. **This closes Fix D from the 2026-08-13 pass** — 3
episodes over 2 positions is not a flap, measurement recorded, no code change.

**The 2026-08-26 off-loop account refresher held.** 211 refresh failures today
against 26 by mid-morning, and `breach_count` finished at **5** with `max_lag_s`
**0.4948** — literally unchanged from the 10:13 snapshot. An eightfold increase in
failure volume produced zero additional loop breaches. `sustained_events: 1` is the
documented morning chain-subscription stall.

**RSS is not a leak.** 109.15 MB at start to 184.42 MB, with the hourly rate
*falling* from 3.37 to 2.70 MB/h across the day. Decelerating is warm-up, not
accumulation. Closed.

**Audit write rate, measured over 6.33h:** Tier 1 5,308 (0.23/s), Tier 2 46,138
(2.02/s), total 51,446 (**2.26/s**). Consistent with the 2.33/s high-water mark
already recorded in `argus.toml`'s `[audit]` comment, and with two positions watched
all session, which is the case that comment says to size against.

## What this cost, and what it bought

The flatten's first fire cost roughly **$30** — a $20 debit on the call plus about
$10 of exit commissions it would not have paid at expiry — against $190 gross on the
day. Fees overall were $31.43, which at 2 contracts is the fixed-cost drag showing
through rather than anything about the flatten.

That is the insurance premium, and it is a sizing question rather than a correctness
one: it scales linearly with contracts while the tail it prevents scales with the
loss it prevents. It should not be read as an argument against the flatten on the
strength of one day where both positions happened to expire worthless — that is
precisely the day on which insurance looks like a waste.

## Still open after this pass

| | |
|---|---|
| ~~**Finding 1** — the ladder's zero range near the tick~~ | **CLOSED 2026-08-31.** `min_concession_steps` on the POLICY, not a direction branch in `PriceWalk`; `exit_walk_min_steps = 1`, the unique value that changes the two broken rows and no row that already works. → [decision](2026-08-31-exit-ladder-near-tick.md) |
| ~~**Finding 2** — should `CROSS_UNPRICEABLE` spend the attempt~~ | **CLOSED 2026-08-31 — no.** It is unreachable except through `_await_settled`'s positive confirmation, so it retries on P0's shape and P0's shared budget. `CROSS_REFUSED` is untouched. |
| **Finding 3** — an exit-scoped staleness bound | still open. Reported in the decision above with the seam identified (`LegQuotes.resolve` is exit-only; `.spot` is shared); do **not** fix by raising `market_data_max_age_s` |
| **New from that pass** — the walk floor's missing width cap, and `cost_to_close` vs `crossing_debit` degenerate cases | both real, both belong to the owed `exit_walk_away_pct` pass |
| **Finding 4** — the six-hour book disagreement | cause not established; needs the broker's four symbols and their signs |
| **Finding 5** — `watchdog.check_spent` is invisible to BOTH `tools.desk` and `tools.evaluability` (in the latter it is a denominator error, not a display gap); and `final_limit_price` / `steps` describe a reprice that may never have landed | design prompt written: [`argus_spent_mark_visibility_and_walk_step_lineage_prompt.md`](../tasks/argus_spent_mark_visibility_and_walk_step_lineage_prompt.md) |

The daily max-loss breaker remains structurally inert in sandbox and is unchanged by
any of this. `execution.walk_source` / `watchdog.trigger_source` still carry
SANDBOX-ENTRY values — and note that today produced the **first exit-walk telemetry
that has ever existed** (n=2, both zero-step), which is evidence about the arithmetic
and not yet evidence about the right percentage.
