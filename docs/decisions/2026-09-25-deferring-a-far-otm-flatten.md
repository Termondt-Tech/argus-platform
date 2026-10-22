# Deferring a far-OTM flatten, pricing the cross by what can fill, and tracking the close that is still working (2026-09-25)

> **BUILT 2026-09-25, DEPLOYED in v0.5.2 (Mira, 2026-09-26)** (config sha `8322ab49c926`, read back off `/status`; the live process started 12:49 ET on a Saturday, so **no flatten has yet run under it — the first is 2026-09-28 at the earliest**). This closes all three findings of
> [a flatten that crossed below its own ladder](2026-09-24-a-flatten-that-crossed-below-its-own-ladder.md).
> It also answers the open decision *"Should `FLATTEN_BEFORE_CLOSE` be CONDITIONAL"*,
> under both of that decision's hard constraints. All eight decisions of
> [the design](../designs/2026-09-25-deferring-a-far-otm-flatten.md) (§13) were approved
> by Richard; he set **k = 10**.
>
> **Deployment.** One coupled image + config release, shipped as v0.5.2 with the strike-touch stop,
> the side rule and the caps ([release notes](../releases/v0.5.2.md)). It adds five REQUIRED
> `[watchdog]` keys, so the config sha moved.
>
> **Tests.** Suite 2,998 (was 2,887). All 14 mutations of the load-bearing lines are
> caught by a test, and so are all 12 mutations of the review fixes and all 4 of the
> multi-position amendment (below).
>
> **Reviewed the same day; seven findings and one open question, all fixed** — see
> [Review fixes](#review-fixes-2026-09-25). The text below describes the build as fixed.

## What happened, and what it proved

The 2026-09-24 audit trail answered the record's three open questions, and each answer
changed the design:

- **What held the flatten for 8 minutes.** The **zero-mid refusal**, not staleness.
  Stale legs were present on 84% of ticks, but every fresh look found a book that rounds
  to zero. The one buildable tick came from a WIDER quote.
- **What `crossing_debit` read at `exit_started`.** It was 0.10, so rung 1 bid exactly
  the natural. Then:
  - rungs at 0.10, 0.15 and 0.20 went unfilled;
  - a 0.10 bid then rested for 22 minutes against a 0.05 natural.

  **On a book whose long leg has no bid, no price buys a fill**: nothing can be legged
  into, and no responder came.
- **What `walk_away_price` read.** It was −0.20, which is `walk.final_limit_price`.

Across the five live flattens:

- two filled at one tick;
- **two sent nothing in 30 minutes** (09-22, 09-23);
- one sent an order nobody took.

All five expired worthless. In its modal regime the flatten was not a working backstop.
It was friction.

## What was built

**Part A — the flatten deferral** (`execution/flatten_deferral.py`, pure; wired in
`PositionWatchdog._apply_flatten_deferral`).

**The rule.** A position whose only true trigger is `FLATTEN_BEFORE_CLOSE` defers the
flatten on a tick when:

```
k · P_touch(z) · W  ≤  one tick
z       = ln(spot / barrier) / (max(IV, RV) · √(minutes to close / MINUTES_PER_YEAR))
barrier = short strike ± underlying_buffer_points
P_touch = 0.01 · (z / z99) ^ (−alpha)     for z ≥ z99
```

**The inputs.**

- **IV** is `regime.compute_regime` computed BY THE WATCHDOG (`ChainImpliedVol`), from
  the chain source it already shares through `LegQuotes`. It never reads `SignalLoop`
  state. A missing estimate is carried forward in exactly two cases: inside the
  estimator's own NaN regime (below 10 minutes to settlement, decided with the
  estimator's own arithmetic on the same instant), or while the last reading is no
  older than `staleness.market_data_max_age_s`. Anywhere else it is no reading, which
  refuses.
- **RV** is `realized.realized_vol` over the watchdog's own trailing 30-minute spot path.

**Eligibility, all required:**

- only the clock fired;
- nothing was sent for the position, and nothing may be working (not in `_attempted` or
  `_exiting`, and no retired profit take with `order_may_be_working`);
- no closing send for it has failed (`_sends`): hard constraint 1's "spent **or
  failing**", read literally;
- spot is fresh and the short strike is known — read from the chain identity, never a
  leg quote;
- the watchdog has watched spot since 30 minutes before the flatten mark (15:00, or
  12:00 on a half-day), so **a restart inside that window disables the deferral for the
  rest of the day**;
- the last `get_live_orders()` poll AND the last scan are within
  `2 × refresh_interval_s`;
- the watchdog is not degraded;
- the welded size invariant holds, `max_concurrent_positions × max_loss_per_position ≤
  daily_max_loss`. **The loader refuses `on` otherwise** — keyed on the DOLLARS one
  barrier event can cost, never on the count (amended the same day, below).

**What it may and may not do.**

- It **only ever removes `FLATTEN_BEFORE_CLOSE`**, and it is never applied to a spent
  position, so the one-shot override and the stub continuation are untouched.
- It is re-checked every tick.
- **Every failure means "flatten as today"**, and so does any exception while deciding
  (the one correct swallow, since the fail-safe direction is the action).
- It needs no ORDER latch: a sent order sets `_attempted`, which bars deferral; a
  `NOT_ATTEMPTED` sent nothing. **Its RECORDS are bounded separately** (below), because
  a position hovering at the boundary on a book that cannot be built re-defers after
  every release.

**Records and pages.**

- `watchdog.flatten_deferred` (Tier 1, paged once) on a position's first deferral.
- `watchdog.flatten_undeferred` (Tier 1, paged) the FIRST time each distinct reason
  releases a deferral; the flatten is handed off on the same tick. A repeat of a reason
  already on record is Tier 2 `watchdog.flatten_undeferred_again`, unpaged; a new
  reason (an emergency trigger beside the clock, a stale poll) is always loud.
- A tick the deferral withholds is **not a falling edge of the `_TRIGGERED` episode** —
  the flatten's condition is still true, only its action is held back (the
  `_PROFIT_TRIGGERED` rule). So a released flatten pages `watchdog_triggered` once, not
  once per crossing.
- `watchdog.flatten_deferral_outcome` (Tier 1), once per deferred position. It records
  the minimum z, whether the barrier was touched, and which PRICE triggers fired after
  the first deferral (never the flatten itself): **the reliability dataset**. Its normal
  writer is `PositionWatchdog.stop()` (`ended: session_end`, `seconds_to_close` ≈ 0),
  because the scheduler stops the session AT the close; `close` (a pass that beat the
  stop) and `left_the_ledger` are the other two.
- `watchdog.check` carries the decision on every tick where nothing fired. **Since
  2026-09-28, `watchdog.triggered` and `watchdog.triggered_still` carry it too** on a
  tick where the flatten fired. Before that, a flatten the deferral declined to defer
  had its decision on no record (see the amendment at the end).
- `/status` gains a "Flatten deferral" row with the loaded mode and parameters.

**The calibration: `argus.tools.flatten_calibration`** (read-only). It reproduces
`z99` and `alpha` from the recorded path with the runtime's own estimators.

The shipped values are its 2026-09-25 output over 28 non-event sessions:

- **z₉₉ = 2.33**, the envelope across 5–30-minute windows;
- **α = 2.93**, the day-bootstrap lower bound of a Hill estimate of 3.91, capped at 3,
  and heavier than the fit;
- the Gaussian is about 35× too thin at 4σ on this path.

At k = 10 on a 5-wide spread the boundary is **z ≥ 5.12**.

- **Backtest:** the largest standardized 15:30 → 16:00 move was 1.93 (38% of the
  boundary).
- **Replay of all 15 recorded flattens:** it defers **09-17 (z 5.17) and 09-24 (z 6.02)
  only**.

**Part B — the cross priced by what can fill** (`ExitExecutor._cross_limit`).

- **When the long leg has a bid:** `max(natural, |last rung| + one ladder step)`. The
  last rung is `walk.final_limit_price`, never `order.limit_price`.
- **When the long leg has no bid:** the natural (today's arithmetic, on purpose; N1).
- **Both** go through `build.cap_at_width`, now public, the one floor-quantized ceiling.
- `CROSS_UNPRICEABLE` and its retry taxonomy are unchanged.

**Part C — the rung the walk stopped at.** One `_crossed_detail` builder feeds both
`watchdog.crossed` and its page:

- `walked_away_at` = `walk.final_limit_price`;
- plus `opening_limit`, `walk_steps`, `limit_basis`, `crossing_debit`, `short_ask` and
  `long_bid`. **Nothing decides on the last four.**

**Part D — the resting cross is tracked.**

- **Registration:** `_register_cross` registers it with the residual recheck as a
  `working_expected` CLOSE carrying its `limit_price`.
- **Paging:** it pages only past `unmanaged_order_max_s`, and is never cancelled
  (`cancel_licence` refuses CLOSE). **When it fills it resolves at Tier 2**
  (`order.residual_resolved`, `sighting: filled`) with no page — never as
  `order.residual_filled`, which means an ABANDONED order filled and the position is
  real.
- **Naming:** `ResidualOrderRecheck.working_close_for` feeds a `working_close` block to
  the spent-exit page, three new `SpentExitAttempt` / `UnclosablePosition` fields, and a
  `/status` line: *"CLOSE WORKING at the broker … check the broker BEFORE sending
  anything by hand"*.

**Finding 3 — closed by measurement, nothing built.** A quiet quote does read as stale;
DXLink *"publishes events only as they occur"*. But:

- 99.6% of 21,423 stale-leg ticks came while the mark was below 25% of the credit stop;
- all six live credit stops fired on fresh quotes;
- for the flatten, Part A removes the case.

The REST-corroboration design is kept in the design's history. It has three reopen
triggers: a stop delayed or unfilled while a leg was stale; stale ticks at ≥ 50% of the
stop above 1% of all stale ticks over five sessions; or the size rails rising.

## Rules to carry

- **A flatten threshold is never a distance in points or percent.** 09-17 and 09-23 were
  both 0.70% out of the money and were 5.17σ against 4.55σ. The unit is the standardized
  distance, and the threshold is a probability budget.
- **`z99` and `alpha` are calibration outputs.** Rerun the tool; never hand-tune them.
  **k is policy:**
  - raising it is the safe direction (fewer deferrals) and needs no analysis;
  - lowering it needs a calibration rerun and a decision record;
  - either way it is a config-only release, never between 15:00 and 16:00 with a
    position held.
- **A restart after 15:00 (12:00 on a half-day) disables deferral for the rest of that
  day**, because the watchdog must have watched spot since 30 minutes before the mark.
  That is the intraday rollback; `flatten_deferral = "off"` is the permanent one.
  **Never rest it on `realized_vol`'s refusal alone**: that accepts 80% of its window,
  so it re-arms about 24 minutes after any restart.
- **A refusal test is spelled so that NaN refuses** (`not x > 0`, `not lhs <= premium`).
  A comparison with NaN is False, so the natural spelling passes a NaN straight through
  to "defer".
- **Never apply the deferral to a spent position**, and never let it remove anything but
  the flatten (AST-pinned).
- **The deferral reads no leg price** (AST-pinned). Quiet legs cannot influence it.
- **A cross above the ladder is only for a book where a leg-in exists.** On a zero-bid
  long leg, paying more buys nothing (N1).
- **The resting cross is a registered CLOSE: paged, never cancelled.**

## Recalibration triggers

Rerun `tools.flatten_calibration` and cut a config-only release when any of these
happens:

- a `flatten_deferral_outcome` records `touched: true`;
- any non-event 30-minute window exceeds 5.35σ;
- the monthly re-fit moves α or z₉₉ by more than their bootstrap interval;
- the data grows by 50%.

## Not changed

- **The zero-mid refusal.** It is moot for every deferred position. 09-22-shape flattens
  (z 3.33) still meet it and send nothing, and that is the honest residual.
- **Short-leg-only closes.** Rejected: they would break the single-legging
  non-negotiable.
- **The ladder, `exit_walk_min_steps = 3`, `_await_settled`, the one-attempt rule,
  `_EXIT_SEND_ATTEMPTS`, the size rails, and `market_data_max_age_s`.** All untouched.

## Review fixes (2026-09-25)

A code review of the build, the same day and before deploy, found seven defects and one
open question. All eight are fixed. Every fix is in the safe direction (it defers
less, records less or pages less), none adds a config key, and each is pinned by a test
that fails when the fix is reverted (12 mutations, 12 caught).

| # | Finding | Fix |
|---|---|---|
| 1 | **The outcome row was almost never written in production.** It waited for a pass at or after 16:00:00, but the scheduler sets the session's stop AT the close, so the last pass runs at ~15:59:59; a held-to-expiry position leaves the ledger after settlement, when the watchdog is gone. The test called `check_once()` at 16:00, which production never does. | `PositionWatchdog.stop()` writes every outstanding outcome as `ended: session_end` (never raising into the teardown), with `seconds_to_close` saying whether it was the close or a SIGTERM. Pinned through `run()`, the way the session drives it. |
| 2 | **Boundary flicker wrote Tier 1 rows and pages on every crossing.** A deferred tick fell the `_TRIGGERED` episode, so each release re-opened it; on a zero-mid book each handoff is `NOT_ATTEMPTED` and the position re-defers. 20 alternating ticks: 10 Tier 1 `flatten_undeferred`, 10 Tier 1 `watchdog.triggered`, 20 pages. 09-17 replays at z 5.17 against a 5.12 boundary. | A withheld tick is not a falling edge of `_TRIGGERED`; a release is Tier 1 and paged once per distinct reason, a repeat Tier 2. Re-checking every tick and re-deferring after `NOT_ATTEMPTED` are unchanged. The same 20 ticks now write one of each. |
| 3 | **The documented intraday rollback was false.** `realized_vol` accepts 80% of its window, so the deferral re-armed ~24 minutes after any restart, and a restart before ~15:06 did not disable it at all. | A new eligibility condition: the watchdog has watched spot since 30 minutes before the mark. "A restart after 15:00 disables it for the day" is now true. |
| 4 | **The IV carry-forward was wider than designed.** It carried on any missing estimate, for the whole window, with no age bound — and the estimator fails on crossed or stale quotes, a fast market, which is when a stale, lower IV inflates z. | Carried only inside the estimator's NaN regime (decided with its own arithmetic on the same instant, pinned equal), or while no older than `market_data_max_age_s`. |
| 5 | **A filled resting cross paged `residual_order_filled`**, whose documented meaning is "an abandoned order filled — the position is real" — the wrong message to an operator just stopped out. | A `working_expected` residual that fills resolves at Tier 2, unpaged. The walk's abandoned rung keeps the Tier 1 record and the page. |
| 6 | **The outcome's trigger list was never empty**: it counted the flatten itself, true on every observed tick. | It counts the price triggers only. |
| 7 | **`decide()` deferred on a NaN `minutes_to_close`** (spot one point from the barrier). Unreachable from the watchdog, but every refusal test was spelled so that NaN passed it. | A non-finite time refuses (`time_to_close_unknown`), and every comparison is written so NaN refuses. |
| Q | Hard constraint 1 says never skip a position whose exit is spent **or failing**; a retryable failed send (`NOTHING_AT_BROKER`, `CROSS_UNPRICEABLE`) leaves the position out of `_attempted`. | Ineligible once any closing send has failed (`exit_send_failed`). |

## Amendment: more than one position (2026-09-25)

**The loader's coupling is re-keyed from the COUNT to the DOLLARS.** As built, `on` was
refused unless `max_concurrent_positions == 1`, on the reasoning that two same-side
spreads share one barrier event and the per-position budget had "no portfolio form".
The same day Richard moved the caps to **2 spreads × 1 contract** (from 1 × 2), with
`max_loss_per_position` 950 → 475 so that total risk is unchanged, and the coupling
would have refused the deploy.

**Why the per-position test is valid for several positions:**

- **In expectation it needs nothing.** Expected losses add whether or not positions are
  correlated, so if every position passes `k·P_touch·W ≤ one tick`, the book passes in
  total.
- **What a second position changes is concentration**: same-side spreads are touched by
  one move, and the failure the flatten insures against (a dead broker, an unpriceable
  book) is common to all of them. So the thing to bound is the most ONE barrier event can
  cost across every deferred position, which is at most `max_concurrent_positions ×
  max_loss_per_position`.
- **The welded invariant holds that to `daily_max_loss`** — the same bound the approved
  one-position configuration had (1 × 950 = 950). At 2 × 475 the worst joint event is
  again 950, and it is no more likely than the approved case: both positions are lost
  only if spot reaches the FARTHER barrier, and each position individually passed its
  own budget. On opposite sides one move cannot touch both: it takes a reversal through
  both barriers inside the window, and each barrier is covered by its own position's
  budget (the [two-slot research](../../RESULTS.md)
  measures this composition; it is not a decision).

**So the loader now refuses `on` unless `max_concurrent_positions ×
max_loss_per_position ≤ daily_max_loss`**, through one predicate
(`RiskConfig.open_risk_is_welded`) that the watchdog's runtime condition 6 re-reads
rather than copies. The invariant was already required of every profile by
`tests/test_config_profiles.py`; the loader now also requires it wherever the deferral
acts. Each position is still decided against its own barrier: the pinning test holds a
put and a call at one spot, flattens the nearer and defers the farther. Nothing else in
the rule changes, and no portfolio-level rule (for example deferring a side all or
nothing) is needed for this configuration.

**What would reopen it:** a configuration whose worst joint event exceeds the day's loss
budget, which the loader refuses; or a change to `k`'s meaning from per-position insurance
to a book-level one.

## Amendment: the decision on a flatten it declines to defer (2026-09-28)

**What was wrong.**

- The decision was recorded on `watchdog.check` only, which is written only on a tick
  where nothing fired.
- A flatten the deferral declines to defer fires. That tick goes to `_trigger`, whose
  `watchdog.triggered` payload carried no decision, and `_note_deferral` writes only on
  deferral edges.
- So z, `P_touch`, sigma and `lhs` were recorded for no flatten that actually ran. On
  the 15 recorded flattens that is 13 of 15, the population k is priced against.
- `_note_deferral`'s docstring said the opposite.
- Found by the
  [2026-09-28 objective audit](../reviews/2026-09-28-objective-profit-audit.md) (P11).
  [Its due diligence](../../RESULTS.md) §2
  makes it the prerequisite for any decision on k: at k = 10 the flatten pays a
  premium above what it insures, and without this record no live flatten says by how
  much.

**The fix. Telemetry only; it changes no decision, no action and no config key.**

- One module-level builder, `_deferral_field`, writes the `flatten_deferral` field for
  both records. The literal appears once in the watchdog (AST-pinned).
- `_trigger` takes the tick's decision as a **required** keyword with no default, so a
  new caller cannot silently drop it again.
- The field is absent, never null, where the deferral decided nothing: mode `off`, no
  flatten in the fired set, or a spent position. Every older row keeps its shape.
- The shape is `DeferralDecision.to_payload()`, the same as on `watchdog.check`. Every
  number goes through `payload_number`, so a z of `inf` at the close cannot refuse the
  Tier 1 row.

**What the record now says, by case:**

| Case | `watchdog.triggered.flatten_deferral` |
|---|---|
| declined to defer (`on`) | `defer: false` and the numbers |
| released after a deferral | the releasing decision, the same as `flatten_undeferred`'s |
| `record` mode, would have deferred | `defer: true` (decided, not acted on) |
| an emergency trigger beside the clock | `defer: false` and why |
| a refused input, or an exception while deciding | `defer: false` with the reason (`deferral_error:<type>`) |
| `off`, or a spent position's override | absent |

**Tests.** Pinned by eight behaviour tests (eleven cases) in
`tests/test_watchdog_flatten_deferral.py` and one structural test in
`tests/test_flatten_deferral.py`. Four mutations, all caught: dropping the field from
`_trigger`, passing `None` from `_check_one`, a hand-built dict, and null instead of
absent.

**Now answerable on live data:** the live price of k, as the due diligence's §2 asks. That
means every flatten's z and `P_touch` against what it paid (the audit's Q6 and Q14) and
against its settlement.

## Amendment: k = 2, on a recalibration (2026-09-28)

k is lowered from 10 to 2, and z₉₉ / α move to the rerun's 2.38 / 2.80. The boundary on a
5-wide moves from z ≥ 5.12 to z ≥ 3.05. This followed §4.9's procedure (rerun, backtest, a
decision record) and changes no code. It was prompted by the first live flatten under the
deferral, which k = 10 declined to skip at z ≈ 4–5.
→ [the k decision](2026-09-28-flatten-deferral-k-2.md)

## The first live flatten under the deferral (2026-09-28)

`20260928-call-7745-7750`, credit 0.50, about 58 points out of the money at 15:30. This ran
under v0.5.3: k = 10, z₉₉ 2.33, α 2.93, before P11. So the decision is rebuilt by the tool's
replay ([`first_live_stop_and_flatten.py`](../../RESULTS.md)
§2 and §3).

**z at the mark was 4.42.**

| Release | Parameters | Boundary | Decision on this flatten |
|---|---|---|---|
| v0.5.3 | k 10, 2.33 / 2.93 | z ≥ 5.11 | flattened (`insurance_not_overpriced`) |
| v0.5.4 | k 2, 2.38 / 2.80 | z ≥ 3.05 | **deferred** |
| v0.5.5 | k 2, 2.60 / 2.72 | z ≥ 3.35 | **deferred** |

**What the flatten did.** Every order went unfilled, and the position expired worthless
(+$46.56). The cost was noise, not money:

| ET | Event |
|---|---|
| 15:30:00–15:31:06 | 66 s with no order: the mid rounded to zero (`NOT_ATTEMPTED`), or a quiet leg quote read as more than 2 s old (`exit_unpriceable`), on alternating ticks |
| 15:31:06–15:31:22 | Walk 1: 0.05 → 0.20, four orders, walked away |
| 15:31:22 | The cross came back `CROSS_UNPRICEABLE`: the 7750 leg's quote was 11.2 s old. A far out-of-the-money leg that does not change sends no updates (Finding 3, N2). That outcome is retryable by design |
| 15:31:25–15:31:39 | Walk 2, the same ladder |
| 15:31:39 | Crossed at the natural 0.10 (`natural_no_long_bid`, long bid 0). It rested and never filled |

That is 9 orders in 33 s. Between 15:30 and 16:00 the watchdog also wrote 188
`watchdog.degraded` rows and 56 Tier 1 `exit_spent_still_held` rows; the 15-minute alert
throttle kept those to about three pages.

**Nothing built.** The fixes that suggest themselves concern positions k = 2 now defers:
- skip the ladder on a zero-bid long leg;
- on a stale quiet quote, retry only the cross, not the whole walk.

Revisit once P11's records show how often an undeferred flatten, a nearer position,
still meets a zero-bid long leg.
