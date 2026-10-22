# The exit ladder has no range in the regime the flatten fires in — investigation

**2026-08-31. SIGNED OFF AND BUILT.** Same gate P3 and P5 took. Source prompt:
`docs/tasks/argus_exit_ladder_near_tick_prompt.md`.

> Richard's answers to the four open questions at the end: **(1)** new config key
> `exit_walk_min_steps`, **(2)** value 1, **(3)** both changes in one deploy,
> **(4)** on `main`. Built the same day; 1,755 tests pass. The normative record is
> [`docs/decisions/2026-08-31-exit-ladder-near-tick.md`](../decisions/2026-08-31-exit-ladder-near-tick.md)
> — this file is kept as the investigation the decision was made from, not as the
> current state of the code.

Every number below was computed by running the repo's own `zerobid.round_to_tick`
and `PriceWalk` arithmetic, not by hand.

---

## The one-paragraph version

The walk-away floor is a **percentage of a price on a quantized grid**, and a
percentage smaller than half a tick rounds away to nothing. Below a debit of 0.125
the floor collapses onto the initial and the exit ladder has zero rungs. That is
the flatten's modal regime, so on 2026-08-31 the ladder contributed nothing to
either exit and both outcomes rode on a single instantaneous market-crossing limit.
**The recommended fix is a minimum absolute concession of ONE step**, expressed as
a `WalkPolicy` field so the entry path opts out explicitly rather than by a
direction branch — the unique value that changes the two broken rows and no row
that already works, at most $10 per spread at current size, with an expected sign
that is negative. Separately, **`CROSS_UNPRICEABLE` should stop spending the
position's exit attempt**, in exactly the shape P0 gave `NOTHING_AT_BROKER` three
days ago, because it is epistemically *stronger* than that case, not weaker.
Item 4's "should a flatten skip the walk" is answered **no** — P2's design premise
is correct and it was the arithmetic under it that failed.

---

## Item 1 — what should the ladder be when the mark is inside one tick?

### F1. The defect, stated exactly

`PriceWalk.from_policy` (`src/argus/execution/loop.py:351`) computes
`raw_floor = round_to_tick(initial - abs(initial) * pct)`. The guard binds when the
concession is smaller than half a tick:

    pct x |initial| < TICK/2   =>   floor rounds back to initial   =>   zero rungs

At `exit_walk_away_pct = 0.20` that is `|initial| < 0.125`, i.e. **0.05 and 0.10**.
Verified against the repo's own arithmetic:

| exit opening limit | floor now | rungs now |
|---|---|---|
| −0.05 | −0.05 | **0** |
| −0.10 | −0.10 | **0** |
| −0.15 | −0.20 | 1 |
| −0.20 | −0.25 | 1 |
| −0.25 | −0.30 | 1 |
| −0.30 | −0.35 | 1 |
| −0.50 | −0.60 | 2 |
| −1.00 | −1.20 | 4 |
| −1.70 | −2.05 | 7 |

The prompt's table reproduces exactly. The asymmetry it flags is real: the credit
direction has an explicit degenerate-case guard (`initial > 0 and raw_floor < TICK`)
and the debit direction has none, because the debit degenerate case is a different
one — the floor collapses onto the initial rather than crossing zero.

### F2. The ladder is the half of the escalation that survives a dead feed — the cross is not

This is the finding that decides the answer, and it inverts the intuitive reading
of the walk-then-cross design.

- A **rung** is pure arithmetic on the walk's opening limit. `PriceWalk.price_at`'s
  call graph is `{round_to_tick}`; `loop.py` imports no market data by design. A
  rung is priceable in a completely dead book.
- A **cross** re-reads the market. `_cross_limit` (`watchdog.py:1283`) returns
  `None` when `market.crossing_debit` is not finite or not positive — no offer on
  the short leg, no cross.

So in exactly the regime where the cross fails, the ladder is the *more* robust of
the two mechanisms — and today it has zero range there. The walk is not the gentle
warm-up act in front of the real escalation; near the tick it is the only escalation
that can be constructed at all.

### F3. P2's own argument for keeping the walk was already false in the flatten's modal regime

`docs/decisions/2026-08-28-time-based-flatten-trigger.md`, question 2, reason 2:

> `exit_cadence_s = 1.0`, `exit_step_ticks = 1`, `exit_walk_away_pct = 0.20`: the
> walk from mid to walk-away is a handful of COA cycles, seconds, against 1,800s of
> runway.

At a 0.05 mid it is **zero** cycles. The premise (*the flatten changes WHEN, never
HOW*) is right and should stand. The arithmetic underneath it was not. Restoring the
rungs restores the premise rather than amending it.

### F4. The call spread's successful exit did not need `_cross` at all

```
15:30:34.520  walked_away after 0 step(s) at -0.05
15:30:36.897  watchdog_crossed_market: walked_away_at=-0.05 crossing_limit=-0.1
15:30:38-39   booked 4 leg fill(s)
```

The cross filled at **mid + 1 tick**. A ladder with one rung would have offered
−0.10 on the guarded walk and (on this evidence) filled there. That matters beyond
price: **`_cross` is the one place on the exit path that submits without the
duplicate guard** (`watchdog.py:1156` — deliberately, because a guarded walk and a
positive `_await_settled` confirmation ran in front of it). Every exit that
completes on the ladder is one that never reaches the unguarded path, never needs
`_await_settled`, and never opens the double-close window that path exists to
manage. Reducing traffic through `_cross` is a safety improvement independent of the
dollars.

### F5. The risk, in dollars at current size

Current size: 2 contracts, SPX $100 multiplier, `signal.width = 5.0`,
`min_credit = 0.50`.

- **1 tick = $10** per spread (0.05 x 100 x 2).
- Defined risk per spread ≈ **$900** ((5.00 − 0.50) x 100 x 2).
- `max_loss_per_position` **$1250**, `daily_max_loss` **$2500**,
  `max_concurrent_positions` 2.

What a minimum of N steps changes, and what it costs if the walk fills at the new
floor rather than the old one:

| initial | now | N=1 | N=2 | N=3 |
|---|---|---|---|---|
| −0.05 | −0.05 / 0 | **−0.10 / 1  (+$10)** | −0.15 / 2 (+$20) | −0.20 / 3 (+$30) |
| −0.10 | −0.10 / 0 | **−0.15 / 1  (+$10)** | −0.20 / 2 (+$20) | −0.25 / 3 (+$30) |
| −0.15 | −0.20 / 1 | −0.20 / 1 (+$0) | −0.25 / 2 (+$10) | −0.30 / 3 (+$20) |
| −0.25 | −0.30 / 1 | −0.30 / 1 (+$0) | −0.35 / 2 (+$10) | −0.40 / 3 (+$20) |
| −0.50 | −0.60 / 2 | −0.60 / 2 (+$0) | −0.60 / 2 (+$0) | −0.65 / 3 (+$10) |
| −0.75 and beyond | unchanged | unchanged | unchanged | unchanged |

**N=1 is the unique value that changes only the rows that are broken.** Every exit
whose ladder already has range is bit-identical.

**The expected sign of that $10 is negative — it is a saving, not a cost.** The
alternative to the extra rung is not "hold"; it is the cross that immediately
follows, priced at the short leg's ask minus the long leg's bid. `crossing_debit`
is always ≥ the floored `spread_mid` by construction, so the cross is never cheaper
than the mid, and **a limit walking up through the grid cannot skip a resting
offer** — it fills at the offer, which is where the cross would have paid anyway.
The ladder can pay more than the cross only if the market moves against it during
the walk, in which case the cross would also have paid more. The bounded downside is
$10 per spread; the bounded upside is one fewer unguarded submission and one fewer
`exit_cancel_settle_s` window.

### F6 (new, incidental) — the walk's floor has no width cap

Not part of the reported failure, found while checking the same function. The width
cap lives in `_cross_limit` (`min(debit, market.width)`) and **nowhere in
`PriceWalk`**. On a 5-wide spread:

| exit opening limit | floor | rungs |
|---|---|---|
| −4.50 | **−5.40** | 18 |

The last several rungs offer to pay more than the spread can possibly be worth —
the exact thing CLAUDE.md's width rule exists to prevent, one function away from
where the rule is enforced. Not reachable in the modal case (`credit_multiple = 2.0`
fires at a cost-to-close near 1.00–1.10, floor −1.20/−1.32), but reachable on a gap
open or a stale wide quote.

**Recommend: report, do not fix here.** `PriceWalk` is deliberately market-free, and
the width is a market/leg fact the loop has no access to (the order's legs carry OCC
symbols; `occ_strike` exists but importing it into `loop.py` gives the pure state
machine a symbol parser). This belongs to the owed data-backed `exit_walk_away_pct`
pass, where the floor's construction can be reconsidered whole. What must be said
now: **the minimum proposed below can never contribute to it** — at N=1 the guard
binds only for `|initial| < 0.25`, giving a maximum floor magnitude of 0.30 against
a 5.00 width.

### F7 — where the fix goes, and why it is not a direction branch

Three placements were considered.

**Rejected — the flatten's own handoff.** It would make the flatten construct
prices differently from a `credit_multiple` stop, which is a real amendment to P2's
signed-off premise, and it would not fix the same collapse when a price stop fires
on a near-worthless spread. The defect is in the arithmetic, not in the trigger.

**Rejected — `from_policy` with an `initial < 0` branch.** It works, and the
precedent for a direction branch exists in that exact function. But `PriceWalk`'s
docstring is emphatic that the state machine has no direction branch, and a second
one is a real cost. It is also unnecessary:

**Recommended — a `WalkPolicy` field, applied direction-neutrally in
`from_policy`.** `WalkPolicy`'s own docstring says *"Everything about HOW a walk
paces and steps. Entry and exit differ ONLY here."* That is precisely what this
difference is.

```python
# WalkPolicy
#: Minimum number of REPRICES the walk must have, whatever the percentage
#: rounds to. A percentage of a price on a tick grid vanishes below half a
#: tick; this is the floor under it. Steps, not ticks, so the guarantee holds
#: if step_ticks is ever raised.
min_concession_steps: int   # >= 0

# PriceWalk.from_policy
raw_floor = round_to_tick(initial - abs(initial) * Decimal(str(policy.walk_away_pct)))
least = round_to_tick(initial - policy.step * policy.min_concession_steps)
if abs(raw_floor - initial) < abs(least - initial):
    raw_floor = least
if initial > 0 and raw_floor < TICK:
    ...unchanged...
```

- **No new direction branch.** Conceding is always subtract (module docstring), so
  `least` is sign-correct for both directions with no test on `initial`.
- **`for_entry` passes 0** — explicitly, with a comment naming F8 below. The entry
  path is then provably unaffected (`0` makes the clause inert), and the opt-out is
  greppable rather than implied.
- **`for_exit` reads config.** The existing credit clamp runs **after**, unchanged,
  so a hypothetical credit walk with a nonzero minimum still cannot cross zero.
- **Monotone.** `max(pct, N steps)` keeps a cheaper spread from getting more
  patience than a dearer one, which a "only repair the exactly-zero case" version
  would not.

### Recommendation for item 1

**`min_concession_steps`, shipped at 1, from a new REQUIRED key
`[watchdog] exit_walk_min_steps = 1`.**

Config rather than a module constant, against `_EXIT_SEND_ATTEMPTS`'s precedent, for
three reasons: it decides **how much more Argus pays to close**, which is the same
class as `exit_walk_away_pct` and that is config; the owed data-backed pass must be
able to raise it in the same edit that re-derives the percentage; and CLAUDE.md's
standing rule is that risk caps are not hardcoded. The cost is a **coupled
config+image deploy**, the same coupling P2 and P5 already forced.

Shipping at 1 rather than 3 is deliberate and matches the prompt's own scope
statement — *"this task is about the ladder having zero range at all, not about what
the right range is once it has one."* One step is not a tuning choice; it is the
minimum coherent expression of any nonzero percentage on a tick grid. **Raising it
later is a config flip, not a deploy** — the same shape P5 used for
`watchdog.unmanaged_order_cancel`.

The argument for 3 is F2 (the ladder outlives the cross, so range is worth more than
the rung count suggests) and it is a good one. It is also a $30-per-spread tuning
decision with n=2 of exit-walk telemetry behind it, and the prompt forbids using
that n to tune. **Ship the mechanism at 1; let the data-backed pass raise it.**

---

## Item 2 — the entry walk's identical collapse

**Confirmed, and unreachable today — but only by a `[signal]` key that does not
know it is load-bearing.**

At `entry_walk_away_pct = 0.10` the collapse binds at `|initial| < 0.25`, and the
tick-aligned boundary is exact:

| entry credit | floor | rungs |
|---|---|---|
| 0.10 / 0.20 / 0.25 | same | **0** |
| 0.30 … 0.75 | −1 tick | 1 |
| 0.80 / 0.85 / 1.00 | −2 ticks | 2 |

**F8 — `signal.min_credit = 0.50` is the only thing keeping the entry walk out of
this hole.** `strategy.py` emits nothing below it, so `initial >= 0.50` by
construction and every entry gets at least one rung. Nothing in `[execution]`,
`loop.py` or `entry.py` records that dependency. Lower `min_credit` to 0.25 — an
ordinary-looking `[signal]` calibration move, and `tools.order_smoke` already sets it
to `0.0` for its own purposes — and **every entry walk silently becomes zero-rung**.
Combined with `entry.py`'s one-attempt-per-`candidate_id` rule, the first unfilled
cycle then RETIRES that strike pair for the process lifetime. Per the prompt this is
flagged, not fixed. The cheapest containment, if wanted later, is a config-load rail
refusing `min_credit` at or below the collapse boundary for the configured
`entry_walk_away_pct`.

**F9 — `walk_source`'s measured claim is arithmetic, not market behaviour.** The
string in `argus.toml` reads *"84% of candidates (63/75, median credit 0.525) get
exactly ONE $0.05 reprice before the floor, 16% get two."* Under
`entry_walk_away_pct = 0.10`, credits **0.50–0.75 get exactly one rung and ≥0.80 get
exactly two** — deterministically, before any order is sent. So the 84/16 split is a
restatement of *"84% of candidate credits were below 0.80"*. It is a true statement
about the credit distribution and it is **not evidence about fill behaviour, the
auction, or the right value of `entry_walk_away_pct`.** The prompt's suspicion holds.
This should be corrected in `walk_source` when the owed pass edits it, because right
now it reads as a measurement that supports the current value and it does not.

---

## Item 3 — should `CROSS_UNPRICEABLE` spend the attempt?

**Recommendation: no. It should retry, in exactly the shape P0 gave
`NOTHING_AT_BROKER` — and the case for it is stronger than P0's, not weaker.**

### The epistemic state at `CROSS_UNPRICEABLE` is the strongest one on the exit path

To reach `_cross_limit` at all (`watchdog.py:1136`) the code must already have
passed `_await_settled` with `outcome=None`, and that is the single most demanding
confirmation in the module: the broker **named `walk.broker_order_id`** and every
disposition it gave for that order is terminal, and no rung under the identifier
filled. Compare the three:

| outcome | what the broker said | one-attempt argument applies? |
|---|---|---|
| `CROSS_REFUSED` | still working / absent / unreadable / unnamed | **Yes.** A second closing order may stack on one that can still fill. Keep spending. |
| `NOTHING_AT_BROKER` (P0) | asked under this identifier, named nothing — *"not a claim that no order can ever appear"* | No. Retries under a budget, licensed by the guard. |
| `CROSS_UNPRICEABLE` | **named this specific order and confirmed it terminal** | No — and this is a *positive* confirmation where P0's was an absence. |

P0's record is explicit that its own answer is weaker than a confirmation: *"This is
not a claim that no order can ever appear. The submit may have reached the broker and
simply not be visible yet."* `CROSS_UNPRICEABLE` has the thing P0 lacked. If P0's
argument licenses a retry, this licenses one a fortiori.

### Why this is a third narrow exception and not the uniform retry the record forbids

- **The licence is the same one `_cross` itself already acts on.** `_cross` responds
  to that confirmation by submitting a second closing order **unguarded**. A retry
  responds to the identical confirmation by rebuilding on the next cadence tick and
  submitting **through `DuplicateOrderGuard`** (`guard_first_submission=True` on the
  exit walk). The retry is strictly *better* protected than the escalation it
  replaces. That is the whole argument, and it does not generalize: it is available
  only to outcomes downstream of `_await_settled`'s positive confirmation.
- **The licence is the guard, never the outcome's own answer**, per P0. If anything
  did land, the next tick comes back `BLOCKED`.
- **Bounded by a COUNT, not a rate**, per P0 — each retry costs a submission.
  Recommend **sharing `_EXIT_SEND_ATTEMPTS = 3`** rather than adding a second budget:
  the hazard being bounded is *"closing orders one position may send while the exit
  keeps failing to complete"*, and that is one hazard whichever half failed. A
  position that has already burned two on `NOTHING_AT_BROKER` correctly has one left.
- **`CROSS_REFUSED` does not change, and structurally cannot be reached by the new
  branch.** It returns at `watchdog.py:1128`, before `_cross_limit` is called. The
  only way to the new behaviour is through the confirmation `CROSS_REFUSED` is
  defined as the absence of. This must be pinned by a test, per the prompt — they
  share a method and it is the easy thing to break.
- **Exhausting the budget lands in `_attempted` and pages exactly as today.** No
  position becomes un-spendable; the terminus is unchanged.

### What it needs, concretely

- `_run_exit`'s `elif` widens from `report.outcome is NOTHING_AT_BROKER` to *either
  retryable outcome*, both charged through `_spend_send`.
- Its own escalation tag (not `_NOTHING_SENT`'s). The two look identical on a phone
  and the operator's next move differs: `_NOTHING_SENT` says look at the broker; this
  one says look at the feed — there is no offer on the short leg.
- **No change to the flatten allowance.** `_flatten_attempted` is spent at the
  handoff (`_trigger`), unconditionally, so a retrying position gets no extra
  headroom from it. That is correct and needs no edit.

### Honest limit

**This would not have changed 2026-08-31's outcome.** The put's short leg had no
offer for the rest of the session; three attempts would have failed the same way and
the position would still have gone to expiry. What it changes is the case the prompt
names: a gappy book that strands an **ITM** short leg for thirty seconds, where the
position genuinely needs out and today's code spends its last action on a
transient feed state.

---

## Item 4 — is walk-then-cross the right shape for a clock-driven exit?

**Yes. Recommend rejecting any change to HOW.**

P2's three reasons for keeping the walk (question 2 of its record) hold, with one
correction and one addition:

1. *"The walk already ends there"* — unchanged. The escalation asked for is the one
   already built and already confirmation-gated.
2. *"There is no time pressure to buy"* — **this is the sentence today falsified**,
   and F1 is why: at a 0.05 mid the walk is not "a handful of COA cycles", it is
   zero. The remedy is to make the sentence true again (F7's minimum), not to
   abandon the walk it argues for. At one step the flatten's ladder costs ~1 extra
   COA cycle against 1,800s of runway.
3. *"A direct cross would be a SECOND submission path on the exit side"* —
   unchanged, and **F2 and F4 strengthen it considerably**. Starting nearer the
   cross means starting from a price that requires a live offer to compute, which is
   exactly the input that failed today. It would make the flatten *less* able to act
   in a thin book, not more, while adding a second unguarded submission path.

The two hard constraints are also decisive on their own: the flatten must never
invent a price (so it cannot start at the width), and it must keep routing through
`ExitExecutor.close` (so it cannot construct its own limit). What is left is
"start at the offer", which is `_cross` with no walk in front of it — reason 3.

**F10 — one thing genuinely is worth naming under this item.** `cost_to_close` and
`crossing_debit` have *different degenerate cases*, because the zero-bid floor is
applied to one and deliberately not the other:

| both legs quote | `cost_to_close` (mid) | `crossing_debit` |
|---|---|---|
| short 0.00/0.05, long 0.00/0.05 | 0.00 → **no order can be built** | 0.05 → priceable |
| short with no offer (ask 0.00) | may round to 0.05 | ≤ 0 → **`None`** |

The first row is a deadlock: a spread whose mid rounds to zero raises `ValueError` in
`_limit_price`, returns `NOT_ATTEMPTED`, and can therefore **never reach the cross
that could have priced it at 0.05**. The second row is today's put. Both are why the
put's whole exit attempt lasted 1.3 seconds. This is a real structural finding and it
is *not* fixed by anything above; recommend it be recorded and carried into the same
pass as F6, since both are about where the exit's price is allowed to come from.

---

## Item 5 — the 571 seconds of `unattempted` (report only)

**Confirmed and bounded. Not fixed here, and not by raising
`market_data_max_age_s`.**

`LegQuotes` is constructed once (`session.py:1235`) with
`max_age_s = config.staleness.market_data_max_age_s = 2.0`, and that single bound
governs **three** exit-path decisions: whether a trigger can be evaluated at all,
whether `_build_exit_order` can construct a limit, and whether `_cross_limit` can
re-read the market. Leg quotes on a 45-point-OTM 0DTE wing at 15:30 were running
2–17s old, so the put could not price for 571 consecutive seconds while the flatten
was already firing.

This is the **same altitude mismatch** as two closed findings, and the fix has
precedent in both:

- `2026-08-21-book-confirm-timeout-bound.md` — an await took its bound from a loop
  cadence in scope rather than from the thing being awaited.
- `2026-08-24-entry-leg-admission-is-role-scoped.md` / `greeks_max_age_s` — one
  threshold served two ROLES with different tolerances; the fix was a **separate
  bound for the separate role**, never a raised shared one.

The seam is clean and worth recording for whoever takes it: **`LegQuotes.resolve`
(per-leg) is used only by the watchdog and the exit path; `LegQuotes.spot` is the
half shared with the entry walk's drift breaker** (`session.py:1075`). An
exit-scoped per-leg bound therefore touches `resolve` only and leaves the shared
underlying read on the tighter value. That is a small, contained change — but it is
its own decision with its own risk (an exit priced off an older quote), and it is out
of scope here.

**CLAUDE.md's standing rule is restated because it is exactly the tempting fix:
never fix an exit-path staleness problem by raising `market_data_max_age_s`.** That
key also governs entry pricing and the pre-trade gate's staleness rail.

---

## What cert can and cannot prove about this

**Can prove:**

- The arithmetic, entirely offline — the rung table is unit-testable and needs no
  broker at all. This is the bulk of the change.
- That a near-tick exit walk now *submits a reprice*: an exit opening at −0.05 sends
  a second order at −0.10 after one COA cycle. Cert routes orders and reprices; the
  walk's reprice cycle (new broker order id per step, prior one cancelled) is on
  CLAUDE.md's PROVEN list.
- That `CROSS_REFUSED` still refuses, and that an exhausted retry budget still lands
  in `_attempted` and still pages.

**Cannot prove:**

- **Anything about whether the extra rung fills.** Sandbox orders match inside
  tastytrade's own book against 15-minute-delayed quotes; a fill there is not
  evidence about the Cboe auction. The entire economic case for the change rests on
  F5's reasoning (a limit cannot skip a resting offer), not on a cert measurement.
- **The `CROSS_UNPRICEABLE` retry in its real setting.** It needs a short leg with
  genuinely no offer while the position is still held and triggered — the same class
  of scenario v0.3.13's release doc already says cert has never produced and cannot
  be forced. Today produced it by accident. A test with injected quotes is the only
  coverage that will exist.
- **The right value of `exit_walk_min_steps`.** Cert cannot size it, which is
  another reason to ship the mechanism at the minimum and leave the number to the
  owed data-backed pass.

Per the standing rule: **a number measured in cert cannot size a change on its own.**

---

## Proposed test plan (per the prompt's list)

In `tests/test_execution_loop.py`:

- **Table-driven arithmetic**, exits of −0.05 / −0.10 / −0.15 / −0.25 / −0.50 /
  −1.00 → asserted floor and rung count. Fails today at the first two rows; must
  keep failing if anyone reintroduces a purely multiplicative floor.
- The credit guard (`initial > 0 and raw_floor < TICK`) behaves exactly as today.
- **An entry-walk test proving the entry side is bit-identical** — the existing
  `PriceWalk.from_policy(Decimal("0.85"), policy())` assertions plus an explicit
  `for_entry(...).min_concession_steps == 0`.
- `min_concession_steps` is expressed in STEPS: with `step_ticks = 2` the minimum
  still guarantees N reprices.

In `tests/test_watchdog.py`:

- A flatten on a spread marked at 0.05 produces a walk with a non-zero rung count
  and still cannot exceed the width cap.
- `CROSS_UNPRICEABLE` does not populate `_attempted` while budget remains; charges
  `_spend_send`; exhausting the budget lands in `_attempted` and pages.
- **`CROSS_REFUSED` did not change** — same path, still spends, still pages
  `watchdog_cross_refused`.
- The retry's second submission goes through `DuplicateOrderGuard` and comes back
  `BLOCKED` when the guard finds a working order.

Fake clock and injected quotes throughout; no real sleeps, no live feed.

---

## Deploy shape

| change | files | config |
|---|---|---|
| `min_concession_steps` + `exit_walk_min_steps` | `loop.py`, `config.py`, `argus.toml` | **REQUIRED new key — config deploys WITH the image** |
| `CROSS_UNPRICEABLE` retry | `watchdog.py` | none |

Same coupling P2 and P5 already force. If an image-only deploy is preferred, the
alternative is a module constant on `_EXIT_SEND_ATTEMPTS`'s precedent — see the
open question below.

---

## Open questions for Richard

1. **`exit_walk_min_steps` as config (recommended) or a module constant?** Config
   costs a coupled deploy and buys a config-flip path to 3 later. A constant keeps
   this image-only, which `_EXIT_SEND_ATTEMPTS`'s own comment argues is worth
   something for an exit-path change.
2. **Ship at 1 (recommended) or 3?** 1 changes only the broken rows; 3 buys the F2
   robustness argument at up to $30 per spread and re-tunes rows that work today.
3. **Both changes in one deploy, or the arithmetic first?** They are independent.
   The ladder fix is offline-provable; the retry is not.
4. **Branch or `main`, and commit?** Not started either way — nothing has been
   written to `src/`.

## Deliberately not done

- `staleness.market_data_max_age_s` / any exit-scoped staleness bound (item 5).
- The entry walk's collapse and `entry_walk_away_pct` (item 2) — reported only.
- Re-tuning `exit_walk_away_pct`, `exit_step_ticks`, `exit_cadence_s`.
- F6 (the walk floor's missing width cap) and F10 (the mid/cross degenerate-case
  split) — both new, both real, both belong to the owed `exit_walk_away_pct` pass.
- `tools.desk`'s `watchdog.check_spent` blindness, the
  `entry_credit`-vs-`final_limit_price` inconsistency, and the 2026-08-31 book
  disagreement.
