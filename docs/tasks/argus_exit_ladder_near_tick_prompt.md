# Argus: The Exit Ladder Has No Range In The Regime The Flatten Fires In

## Priority

**Real, observed on the first live fire of `FLATTEN_BEFORE_CLOSE`, and it half-failed.**
Not an availability nuisance and not a safety breach — no wrong order was submitted,
no rail was bypassed, and the day's outcome was max profit on both spreads. What
happened is that the exit price ladder contributed **nothing** to either exit, and
the entire outcome of both flattens rode on a single instantaneous market-crossing
limit. One found an offer and filled; one did not and spent the position's only
attempt.

This is the flatten's **modal** regime, not its tail, so it needs fixing before the
flatten is trusted at size. But it touches the exit's price construction and the
one-attempt rule, both of which are load-bearing. **Investigate and propose a design
FIRST; do not implement without Richard's sign-off**, on the same gate P3 and P5 took
(`argus_p3_p4_p5_p6_v0313_scope_prompt.md`).

## Context

2026-08-31, v0.3.13's first live-fire day and P2's first-ever fire. Two SPXW 0DTE
credit spreads open, 2 contracts each, both comfortably OTM. At 15:30:00 the flatten
fired on both, correctly and to the second.

**The call spread (7710/7715) — worked:**

```
15:30:00.129 watchdog_triggered  fired=['flatten_before_close'] cost_to_close=0.0
15:30:34.520 walk argus-close-...call-7710-7715: walked_away after 0 step(s) at -0.05
             (walk-away threshold reached at -0.05)
15:30:34.520 residual order 1429112 registered (walked_away, cancel_requested=True)
15:30:35.974 residual order 1429112 resolved: dead
15:30:36.897 watchdog_crossed_market: walked_away_at=-0.05 crossing_limit=-0.1
15:30:38-39  booked 4 leg fill(s) for order 1429114
```

**The put spread (7645/7640) — did not:**

```
15:30:00.133 watchdog_triggered  fired=['flatten_before_close'] cost_to_close=None
15:30:00.135 watchdog_exit_unattempted  outcome=not_attempted
15:30:33 -> 15:39:33  19x EXIT STILL NOT IN THE MARKET ... (unattempted), to 571.0s
15:39:59.499 walk argus-close-...put-7645-7640: walked_away after 0 step(s) at -0.05
             (walk-away threshold reached at -0.05)
15:40:00.815 watchdog_cross_unpriceable
             reason='no usable crossing price (no offer on the short leg?)'
15:40:30 -> 15:59:31  39x EXIT ATTEMPT SPENT AND STILL HELD, to 1170.6s
```

The put's entire exit attempt lasted **1.3 seconds** — from the walk getting its
first usable price at 15:39:59 to `CROSS_UNPRICEABLE` at 15:40:00 — and consumed
both the ordinary attempt and the flatten's one-shot override. It then sat held and
triggered for the remaining twenty minutes and went to expiry.

### What actually caused the zero-step walk

`PriceWalk.from_policy` (`execution/loop.py:352`) computes the walk-away floor as a
**percentage of the initial price**:

```python
raw_floor = round_to_tick(initial - abs(initial) * Decimal(str(policy.walk_away_pct)))
if initial > 0 and raw_floor < TICK:
    # A credit walk must never reach zero or cross into a debit ...
    raw_floor = TICK
```

With `watchdog.exit_walk_away_pct = 0.20` and `TICK = 0.05`, an exit opening at a
debit of 0.05 gets `0.05 - (0.05 x 0.20) = 0.06`, which `round_to_tick` snaps back to
**0.05**. Floor equals initial. Zero rungs. Verified across the range:

| exit opening limit | floor | ticks of range |
|---|---|---|
| −0.05 | −0.05 | **0** |
| −0.10 | −0.10 | **0** |
| −0.15 | −0.20 | 1 |
| −0.25 | −0.30 | 1 |
| −0.50 | −0.60 | 2 |
| −1.00 | −1.20 | 4 |

**Any exit whose opening limit is a debit of 0.10 or less has no walk at all.** For a
0DTE credit spread being flattened at 15:30, that is the ordinary case — a winning
spread is nearly worthless by then, which is the whole reason it is winning. The
ladder built for a spread under stress is being applied to a spread in the opposite
state.

Note the asymmetry that makes this a genuine oversight rather than a judgement call:
the **credit** direction has an explicit degenerate-case guard right there in the
code, with a comment explaining it. The **debit** direction has none. Its degenerate
case is different — the floor collapses onto the initial rather than crossing zero —
so the existing guard does not catch it and was never written to.

### And the second half: a cross with no offer

`_cross_limit` (`execution/watchdog.py:1285`) re-reads the market and returns `None`
when `market.crossing_debit` is not finite or not positive. For the put's short leg —
a 45-point-OTM 0DTE option at 15:40 — there was **no offer to lift**. That produced
`ExitOutcome.CROSS_UNPRICEABLE`, which lands in the `is not WALKED_AWAY` catch-all
and spends the attempt.

There is an irony that made today benign and will not always: a short leg with no
offer is a short leg with no risk, so the position Argus could not close was the one
that did not need closing. That correlation is not guaranteed. A gappy 0DTE book near
the close can strand an ITM short leg the same way, and then the identical path spends
the attempt on a position that genuinely needed out.

## What to investigate

**1. What should the exit ladder be when the spread's mark is inside one tick?**

This is the core question and it is a RISK question, not only an arithmetic one.
Giving the exit walk more range means authorizing it to **pay more** to close, and
the walk-away exists precisely to stop that. Do not treat "give it a floor of N
ticks" as obviously correct — derive it.

Things that must inform the answer:

- The width cap already bounds the worst case (`_cross_limit`'s
  `min(debit, market.width)`), and CLAUDE.md's rule is explicit that paying the width
  **guarantees** max loss where holding realizes at most it. Any additional range you
  grant the walk sits between the mark and that cap, and you should say where and why.
- An exit at a 0.05 debit that walks to 0.15 is risking $20 on a 2-contract spread to
  eliminate a tail. State the trade in those terms — absolute dollars at current size
  — rather than in percentages, because percentages are what produced the bug.
- Whether the fix belongs in `PriceWalk.from_policy` (a minimum absolute range for
  debits, mirroring the existing credit guard) or in `WalkPolicy.for_exit` (a
  separate near-tick policy) or in the flatten's own handoff. Argue the placement.
  Note `PriceWalk`'s docstring is emphatic that the state machine has **no direction
  branch** — "an exit walk is an entry walk with a different policy and a negative
  starting price, and that is the whole difference." The existing credit guard is
  already a direction branch in `from_policy`, so the precedent for one exists there
  and nowhere else; if your design needs direction awareness anywhere else, that is a
  finding worth stating loudly rather than a detail to slip in.

**2. Does the same collapse affect the ENTRY walk, and does it matter?**

`entry_walk_away_pct = 0.10` produces the identical shape one step lower:

| entry opening credit | floor | ticks of range |
|---|---|---|
| +0.10 / +0.20 / +0.25 | same | **0** |
| +0.30 / +0.50 / +0.55 | −1 tick | 1 |
| +0.85 | −2 ticks | 2 |

Today's entries were 0.50 and 0.55, so both had exactly one rung — which is
consistent with, and may fully explain, the measured claim in `argus.toml`'s
`walk_source` that *"84% of candidates (63/75, median credit 0.525) get exactly ONE
$0.05 reprice before the floor."* That number may be describing this arithmetic
rather than describing the market. **Check whether that reading holds**, because it
changes what the measurement means. Separately: an entry at a credit of 0.25 or below
would get zero rungs, and `entry.py`'s one-attempt-per-`candidate_id` rule means a
walk-away RETIRES that strike pair for the process lifetime. Report whether the
signal engine can actually propose credits that low; do not fix the entry path in
this task without flagging it first.

**3. Should `CROSS_UNPRICEABLE` spend the position's exit attempt?**

Compare it against `CROSS_REFUSED`, which shares the code path and must keep
spending. They are not the same epistemic state, and the distinction is the same one
P0 drew three days ago:

- `CROSS_REFUSED` — Argus could not positively confirm the walked-away order is dead.
  A second closing order could stack onto one that can still fill, which on expiry day
  re-opens the position inverted. Retrying is genuinely dangerous.
  → `docs/decisions/2026-08-21-empty-broker-answer-is-not-a-verdict.md`
- `CROSS_UNPRICEABLE` — the prior order was already positively confirmed terminal
  (the refusal check runs FIRST, at `watchdog.py:1125`, and passed). Nothing is at the
  broker. The only thing that failed is building a price from a feed that had no offer
  a moment ago and may have one now.

Read `docs/decisions/2026-08-28-exit-attempt-taxonomy-and-halt-scope.md` in full
before proposing anything here — it is the same argument, already made and accepted
once, for `NO_ORDER_EXISTS` / `NOTHING_AT_BROKER`. If your recommendation is that
`CROSS_UNPRICEABLE` should retry, then it must inherit that decision's whole shape,
not just its conclusion: the licence is `DuplicateOrderGuard` re-asking the broker,
**never the outcome's own answer**; and the budget is a COUNT
(`_EXIT_SEND_ATTEMPTS = 3`), not a rate, because each retry costs a submission.
That release doc also warns explicitly against generalizing the one-attempt rule —
so if you land here, say precisely why this is a third narrow exception and not the
uniform retry the record forbids, or recommend against it.

**4. Is walk-then-cross the right shape for a clock-driven exit at all?**

The ladder assumes a spread under price stress with a real two-sided market. The
flatten fires on a clock, on positions typically in the opposite state, and its
success today was entirely a function of whether one instantaneous cross found an
offer. Consider — and this may well be the right answer to reject — whether a flatten
should start nearer the crossing price rather than walking down to it. Two hard
constraints if you explore this: it must **never invent a price** (an unpriceable
flatten still sends nothing, and pricing at the width guarantees max loss), and it
must keep routing through the same `ExitExecutor.close`, the same guarded walk, and
the same width cap. P2's whole design premise is that the flatten changes WHEN, never
HOW. If you propose changing HOW, that is a real amendment to a signed-off design and
must be argued as one.

**5. The 571 seconds of `unattempted` — bound the scope, do not fix it here.**

The put could not price at all from 15:30:00 to 15:39:59 because
`staleness.market_data_max_age_s = 2.0` — a bound sized for delta-selecting a strike
during the entry window — governs the exit path too. Leg quotes were running 2–17s
old. This is a second, separate finding (same altitude mismatch as
`docs/decisions/2026-08-21-book-confirm-timeout-bound.md` and the 2026-08-24 greeks
role split, and the fix has precedent in both). **Report on it; do not fix it in this
task**, and above all do not fix it by raising `market_data_max_age_s`, which CLAUDE.md
forbids by name because that key also governs entry pricing and the gate's staleness
rail.

## Constraints

- **Never pay more than the spread's width.** `_cross_limit`'s
  `min(debit, market.width)` is the rail that turns a bad print into a bounded loss
  instead of an unbounded one. Nothing proposed here may widen, bypass, or
  conditionally skip it.
- **No broker-side stop orders, no unbounded market orders.** The escalation stays a
  single market-crossing **limit**. This is a CLAUDE.md non-negotiable.
- **The one-attempt rule stays intact for everything that reached the broker.** It
  has bent exactly twice, both narrowly and both broker-reconfirmed. A third bend
  needs the same standard of proof, and `_request_cancel` being best-effort is why
  the rule exists at all.
- **Do not raise `staleness.market_data_max_age_s`** (see investigation item 5).
- **`exit_walk_away_pct` is a SANDBOX-ENTRY value**, per `argus.toml`'s `walk_source`
  string, and is already owed a data-backed pass before size is scaled. This task
  overlaps that pass and should feed it — but note today produced the **first exit-walk
  telemetry that has ever existed** (n=2, both zero-step), so it is evidence about the
  ARITHMETIC and not yet evidence about the right percentage. Do not use n=2 to
  re-tune the number.
- **Cert cannot validate the flatten's send allowance.** v0.3.13's own release doc
  says so: the scenario needs a position still held and spent at the mark, which
  sandbox has never produced and cannot be forced. Today produced it by accident.
  Whatever you design, say plainly what cert can and cannot prove about it.

## Testing

Extend the existing suites rather than forking new files —
`tests/test_execution_loop.py` for `PriceWalk`/`WalkPolicy`, `tests/test_watchdog.py`
for the exit path. At minimum:

- **The arithmetic, table-driven**: for exit openings of −0.05, −0.10, −0.15, −0.25,
  −0.50, −1.00, assert the resulting floor and rung count. This table is the
  regression guard — it fails today at the first two rows and must keep failing if
  anyone reintroduces a purely multiplicative floor.
- The credit direction's existing guard (`initial > 0 and raw_floor < TICK`) still
  behaves exactly as today. If your change touches `from_policy` at all, an existing
  entry-walk test must prove the entry side is unaffected.
- A flatten on a spread marked at 0.05 produces a walk with a non-zero rung count and
  still cannot exceed the width cap.
- `CROSS_UNPRICEABLE` behaves per whatever item 3 concludes, with an explicit test
  that `CROSS_REFUSED` did **not** change — they share a path and it is the easy
  thing to break by accident.
- If a retry is introduced: it is gated by `DuplicateOrderGuard` and bounded by a
  count; exhausting the budget still reaches the spent state and still pages.

Use a fake clock and injected quotes, never real sleeps or a live feed — house
convention for every timing-dependent test in this suite.

## Deferred / explicitly out of scope

- `staleness.market_data_max_age_s` and any exit-scoped staleness bound (item 5 is
  report-only).
- The ENTRY walk's own near-tick collapse — investigate and report per item 2, but do
  not change `entry.py` or `entry_walk_away_pct` here.
- Re-tuning `exit_walk_away_pct`, `exit_step_ticks`, `exit_cadence_s` or any
  `[watchdog]` trigger parameter. This task is about the ladder having zero range at
  all, not about what the right range is once it has one.
- `tools.desk`'s inability to read `watchdog.check_spent` records, and the
  `entry_credit`-vs-`final_limit_price` inconsistency observed the same day. Both real,
  both separate.
- The unresolved 2026-08-31 book disagreement (broker 2 / ledger 4 in the morning,
  inverted to 4 / 2 by the close, `watchdog.book_agrees` never written). Separate
  investigation.

## Deliverable

A written investigation and recommendation FIRST, then sign-off, then code — the same
gate P3 and P5 took, and for the same reason: this touches exit price construction and
the one-attempt rule. Present findings for all five investigation items, a concrete
recommendation with the risk stated in dollars at current size, and what cert can and
cannot prove about it.

Once approved: the change, the tests above, and a decision record in
`docs/decisions/` — the house convention is to document the reasoning, not just the
change, and this one needs it because the fix authorizes Argus to pay more to close
than it does today. Add it to `docs/decisions/README.md` so it is not orphaned, and
update CLAUDE.md's current-phase section with one sentence plus the pointer.

Confirm with Richard whether to work on a branch or on `main`, and whether to commit.
Do not push unless explicitly told to.
