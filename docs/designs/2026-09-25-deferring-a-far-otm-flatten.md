# The flatten that fought a dead book: a calibrated decision to defer a far-OTM flatten, a cross priced by what can fill, and tracking the close that is still working

**Status: ALL EIGHT DECISIONS APPROVED 2026-09-25 (revision 4, one release); BUILT the same
day, DEPLOYED in v0.5.2 (Mira, 2026-09-26)** (first flatten under it: 2026-09-28 at the earliest) → [decision record](../decisions/2026-09-25-deferring-a-far-otm-flatten.md).

- Suite 2,998 (was 2,887); all 14 mutations of the load-bearing lines are caught by a test,
  all 12 mutations of the review fixes, and all 4 of the multi-position amendment (§4.5,
  condition 6).
- Shipped calibration: z₉₉ 2.33, α 2.93. These are the tool's output, and §4.4 notes how
  they differ from the scratch pass.
- Build corrections (§14), including the same-day review's eight fixes (§14, item 7). The
  sections below are amended where a fix changed what they say.

This is a design for the three findings of
[a flatten that crossed below its own ladder](../decisions/2026-09-24-a-flatten-that-crossed-below-its-own-ladder.md)
(2026-09-24, `20260924-put-7625-7620`), plus seven more found by reading that day's audit
trail (§3).

**Revision history, kept so it is clear what changed and why.**

- **Revision 1** paid past the ladder on every cross, and relied on quiet-quote
  confirmation to shorten the flatten's hold. The 09-24 trail disproved both premises.
- **Revision 2** deferred a flatten on a fixed **point** distance (40 or 60). Richard's
  review rejected it, and the review was right. A distance in points, or in percent of
  spot, is not a measure of risk. It ignores volatility and time, and nothing that a
  fixed number stands in for stays fixed (§4.1).
- **Revision 3** replaces it with a quantitative resolution framework (§4). The decision
  is an insurance-pricing test: a measured probability against an explicit risk budget.
  Its inputs are dimensionless and measured live. The tail model is calibrated from
  Argus's own recorded SPX path by a tool that anyone can rerun. The rule is monitored
  and has stated recalibration triggers.
- **Every other constant in the design was re-audited for the same defect** (§10). Each
  one is now derived from an existing key, or its derivation is stated.
- **Revision 4** collapses revision 3's four releases into **one** (§12), after a
  per-part risk review (§12.1).
  - Part A ships **on**. Its `record` stage bought almost nothing, because every
    live-input failure already fails to "no deferral", which is today's flatten. Three
    guards cover the one failure that does not (§4.9).
  - Part E is **removed from the release** rather than staged. The live trail shows it
    protects nothing observed (§8), and it was the one part adding a production unknown:
    new REST load against an unpublished rate limit.

The standing rules still apply:

- **Never write to `argus.db` or `argus-live.db`.** Every read below was `mode=ro`,
  against `dev/nas/argus-20260924T163001.db` (live, 09-15 → 09-24) and the 2026-09-14 cert
  backup. The scripts live in the session scratchpad, and §4.6 specifies the tool that
  replaces them.
- The audit chain is written before the next action proceeds.
- The broker is the only source of truth.
- Every autonomous send path is bounded and fails closed.
- One multi-leg order, never a single leg; no broker-side stops.
- **`staleness.market_data_max_age_s` is never raised.**

> Line numbers are against `main` at `914b45b`, and they will drift. The named sites are
> the anchor.

---

## 1. Recommendation in one paragraph

**The fix for 2026-09-24 is not a better price. It is declining to buy insurance that
costs more than the risk it covers.** A flatten is insurance against the price triggers
and the broker failing inside the last 30 minutes. Part A defers it only when both hold:

1. **The system can fully manage the position.** Nothing has been sent for it, the
   underlying trigger can see spot, the broker is answering, and the watchdog is healthy.
2. **The insurance is overpriced.** A conservative estimate of the expected loss it
   protects against, `k × P_touch × W`, is below the least any close can cost, one tick:

   - `P_touch` is the probability that spot reaches the underlying trigger before the
     close. It comes from a volatility- and time-standardized distance `z`, run through a
     fat-tailed model calibrated on the recorded SPX path.
   - `W` is the spread's width.
   - `k` is the one policy choice, the required safety multiple.

It is re-checked every tick. It only ever removes the flatten. The stops stay live until
the close.

**Calibrated values:** k = 10, tail anchored at the 99th percentile z₉₉ = 2.33, tail index
α = 2.93 (the tool's output at build; see the note in §4.4). Under those it would have deferred **09-24 and 09-17** and flattened **09-18, 09-22
and 09-23**.

Three smaller parts ship with it, **all in one coupled image + config release** (§12):

- **Part B.** The cross pays past the ladder only where a leg-in is possible.
- **Part C.** The page reports the rung the walk actually stopped at.
- **Part D.** The working cross is tracked and named. This is the most safety-relevant of
  the three: a page claiming nothing is closing a position invites a manual second close
  on top of a working one, which is C5's double close.

**Part E (quiet-quote confirmation) is not in this release.** Finding 3 is closed by
measurement, with a reopen trigger (§8).

---

## 2. What 2026-09-24 did (the audit trail)

### 2.1 The flatten, tick by tick (ET)

| Time | Audit trail |
|---|---|
| 15:30:00 | The flatten fires. Spot is 7706.0, the short strike 7625 (**80.3 points** out of the money), IV ≈ 0.21–0.22. The spread is priced at `cost_to_close` 0.00, `crossing_debit` 0.05. The build is refused because the mid rounds to 0.00. |
| 15:30 → 15:37:54 | **401 ticks, no order.** 338 (84%) had a stale leg: 7620P median 7.9 s (max 46 s), 7625P median 5.3 s (max 31 s). **62 of the 63 priced ticks were zero-mid**: `cost_to_close` 0.00, `crossing_debit` 0.05. |
| 15:37:54 | The one buildable tick. `crossing_debit` reads 0.10 because **the short leg's ask WIDENED** from 0.05 to 0.10. `exit_started`: limit −0.05, `walk_away_price` −0.20. |
| 15:37:55 → 15:38:03 | Rungs at 0.05, 0.10, 0.15 and 0.20, each live about 1.2–1.5 s. **None filled — including 0.10, which was exactly the natural, and two rungs above it.** The walk ends `walked_away`, `steps 3`, `final_limit_price −0.20`. |
| 15:38:04 | The cross goes out at 0.10. The page reads `walked_away_at: −0.05`. |
| 15:38 → 16:00 | **All 239 priced ticks read natural 0.05. The 0.10 bid sat one tick above it for 22 minutes and never filled.** `exit_spent_still_held` was written at Tier 1 43 times, and none of those rows names the working close. |
| Settlement | Expired worthless: +$50.00 − $3.44 in opening fees. |

**The record's §5 questions, answered:**

1. **What held the flatten:** the **zero-mid refusal**. Staleness was present on most
   ticks but never binding.
2. **`crossing_debit` at `exit_started`:** 0.10, so rung 1 bid exactly the natural and
   did not fill.
3. **`walk_away_price`:** −0.20.

### 2.2 Every live flatten, on the measures that matter

Columns:

- **Barrier:** the short strike ± `underlying_buffer_points`, i.e. the level where the
  underlying trigger fires.
- **IV:** `regime.compute_regime`'s `atm_iv` at 15:30.
- **RV:** trailing 30-minute realized volatility, 1-minute close-to-close.
- **z:** the standardized distance to the barrier, defined in §4.2.

| Day | Spread | Points / % OTM at 15:30 | IV / RV | z at 15:30 | Lowest z before 16:00 | What the flatten did |
|---|---|---|---|---|---|---|
| 09-17 | put 7585/7580 ×2 | 52.5 / 0.69% | 0.160 / 0.073 | **5.16** | 5.16 | filled 0.05 at step 0 |
| 09-18 | put 7595/7590 ×2 | 43.7 / 0.57% | 0.187 / 0.078 | 3.60 | 3.49 | filled 0.05 at step 0 |
| 09-22 | put 7740/7735 ×2 | 34.0 / 0.44% | 0.148 / 0.061 | 3.35 | 3.08 | **no order in 30 min** (160 Tier 1 unpriceable edges, 61 pages) |
| 09-23 | call 7760/7765 ×2 | 54.0 / 0.70% | 0.184 / 0.100 | 4.55 | 4.16 | **no order in 30 min** (135 edges, 66 pages) |
| 09-24 | put 7625/7620 ×1 | 80.3 / 1.04% | 0.218 / 0.117 | **6.02** | 6.02 | **cross unfilled** |

**Look at 09-17 against 09-23.** They are 52.5 and 54 points out, and 0.69% and 0.70%:
identical by either points or percent. But they are 5.16σ and 4.55σ, because 09-23's
volatility was 15% higher. A points rule and a percent rule both treat them the same. The
risk does not. That is the case against revision 2's rule, in one row.

**All five flattens expired worthless.** In its modal regime the flatten filled 2 of 5
times, sent nothing on 2 of 5, and on the fifth sent an order nobody took.

---

## 3. Findings, and the part that closes each

| # | Finding | Closed by |
|---|---|---|
| F1 | The cross priced below its own ladder. | **Part B**, revised by N1. |
| F2 | `walked_away_at` reports the opening limit. | **Part C.** |
| F3 | A quiet quote reads as stale. | **Closed by measurement, with nothing built** (§8). The flatten regime is handled by Part A (N2). For the credit stop, 99.6% of stale-leg ticks came while the spread was marked below 25% of its stop, and all six live credit stops fired on fresh quotes. There is a stated reopen trigger. |
| N1 | **When the long leg has no bid, no price buys a fill.** Rungs at and above a 0.10 natural went unfilled; a 0.10 bid then sat 22 minutes against a 0.05 natural. | **Part A** keeps these positions off the path entirely; **Part B** never pays past the ladder on them. |
| N2 | **The zero-mid refusal, not staleness, holds far-OTM flatten exits.** It held 09-22 and 09-23 for the whole window, and 09-24 for 8 minutes, released only by a *wider* quote. | **Part A** removes the case for every deferred position. It also removes Finding 3's flatten motivation (§8). The refusal itself stays (§11). |
| N3 | The record's distance figure was wrong: IV at 15:30 was about 0.21, not 0.27. | Superseded by §2.2's measured z. |
| N4 | **The working cross is invisible.** It is never registered as a residual order, and the spent-exit page said "nothing is trying to close" 43 times. | **Part D.** |
| N5 | Fees: $3.44 to open, $1.44 per close. | This is why Part A takes one tick, **before** fees, as the price of the insurance. It is conservative (§4.3). |
| N6 | The ladder uses 8.8 s of an 1,800 s runway. | Moot for deferred positions (§11). |
| N7 | Tier 1 edge flicker: 160 and 135 rows per flatten. | Mostly retired by Part A (§11). |

---

## 4. Part A — the quantitative resolution framework for deferring a flatten

### 4.1 Why not points, and why not percent

- **Points** drift with the index level: 40 points at SPX 7700 is 0.52%, and at 6000 it is
  0.67%.
- **Percent** fixes the level, and nothing else. What decides risk is the distance
  **measured in the expected move over the time left**:
  - the same 0.5% is 4.4σ over 30 minutes at IV 0.15, and 1.4σ at IV 0.46;
  - at a fixed IV, 0.5% at 15:30 is half the standardized distance that it is at 15:52.
- **A percent rule is a points rule with the level divided out.** It is still silently
  pinned to one volatility and one time of day.
- **The pasted framework's criteria** (short delta < 0.02, mid < 0.10, time-to-close) point
  at the right quantities, but three of them read the one thing that is unreliable on
  this book:
  - a delta or implied volatility inverted from a 0.00/0.05 quote is ill-conditioned;
  - a mid is a mark, not a price (N1).

  So each criterion below is replaced by its measurable, calibrated equivalent (§4.8).

### 4.2 The measurement

Every tick, for a position whose flatten is true, three quantities are computed.

```
barrier  B = short_strike + underlying_buffer_points     (put)
             short_strike − underlying_buffer_points     (call)
tau        = minutes to today's close / MINUTES_PER_YEAR          # regime.py's basis, imported
sigma      = max(sigma_implied, sigma_realized)                   # annualized, same basis
z          = ln(spot / B) / (sigma · √tau)     (put)
             ln(B / spot) / (sigma · √tau)     (call)
```

- **The barrier is the underlying trigger's level, not the strike.** Reaching it forces an
  emergency exit into a stressed book, and *that* is the event the flatten insures
  against. Measuring to the strike would understate the risk by the buffer.
- **`sigma_implied`** is `regime.compute_regime(...).atm_iv`. The watchdog computes it
  itself, from the chain source it already holds (`DXLinkChainSource.snapshot()`, the same
  object `LegQuotes` uses as its contract directory), with the same pure function the
  signal loop uses.
  - **That shares the market-data feed, never `SignalLoop` state.** The spec's
    independence rule, *"gamma risk in the final hour doesn't care whether entry logic is
    running"*, still holds: the signal loop can die and this keeps working.
  - Below 10 minutes to settlement `compute_regime` returns NaN
    (`MIN_MINUTES_TO_SETTLEMENT`). There, the last finite reading taken at or after the
    flatten mark is carried forward. `regime.py` documents that the estimate runs 1.2–1.7×
    high in the last hour, so a carried value errs toward caution. **Which side of 10
    minutes a pass is on is decided with the estimator's own arithmetic on the same
    instant** (review fix), so the two cannot disagree for a tick.
  - Elsewhere a missing estimate is carried only while the last reading is no older than
    `staleness.market_data_max_age_s` (one missed estimate tolerated; review fix). An
    estimator failing mid-window is failing on crossed or stale quotes — a fast market —
    where a stale, lower IV would inflate z.
  - With no usable reading, there is no deferral.
- **`sigma_realized`** is `realized.realized_vol` over the trailing 30 minutes, on its own
  1-minute grid and coverage rules, fed from a bounded deque of `(et_now, spot)` that the
  watchdog appends every tick (≤ 3,600 entries).
  - It catches a volatility jump the market has not yet repriced.
  - After a restart the buffer is empty and `realized_vol` refuses (`span_too_short`) —
    but only until 80% of its window is covered, about 24 minutes. **The restart rule is
    therefore its own condition** (§4.5, condition 4a, review fix): no deferral unless the
    watchdog has watched spot since 30 minutes before the mark.
- **`max(implied, realized)`** is the standard conservative combination, and it is also
  what the tail was calibrated on (§4.4). **The runtime estimator and the calibration
  estimator are the same functions on the same inputs.** That is what makes the
  calibration apply.

### 4.3 The decision (insurance pricing)

```
P_touch(z) = 0.01 · (z / z99) ^ (−alpha)        for z ≥ z99      # calibrated Pareto tail (§4.4)
           = no deferral                       for z <  z99      # inside the body: too close
premium    = TICK                                                # 0.05: the least any close can cost
defer iff  k · P_touch(z) · W  ≤  premium
```

- **W is the spread's actual width** (`risk.loss_budget.vertical_width`, which the watchdog
  already imports). It is the most the position can lose on a touch. That is a
  conservative bound, since a touch triggers an exit that usually costs far less than W.
- **The premium is one tick, before fees.** Adding fees ($1.44 per close) or the natural
  (0.05–0.10) would only make deferral *easier*. Leaving them out is the conservative
  choice, and it needs no fee config.
- **k is the one policy choice**: "decline insurance only when it is at least k times
  overpriced." It covers both the estimation error in a tail measured from 34 sessions
  (α̂ ranges 2.85–4.09 across fits) and plain risk aversion. **k = 10, decided by Richard
  on 2026-09-25.** It is a config key, not a constant; §4.9 has the rules for changing it.
- **k in plain English.** The flatten is insurance that costs at least one tick ($5 a
  contract). At k = 10 Argus declines it only when that premium is at least 10× the
  expected loss it protects against. On a 5-wide spread, valued at the full $500 a
  contract, that means an expected loss of at most 50¢, roughly a 1-in-1,000 chance of
  spot reaching the underlying trigger before the close.
- **The rule scales correctly in every input.**
  - Index level: z is a log ratio.
  - Volatility and time: z is standardized.
  - Width: a 10-wide spread must be further out than a 5-wide one, by the factor
    `2^(1/alpha)` ≈ 1.26 in z.
  - Contracts: they cancel from both sides.
- **With k = 10 and W = 5 the budget is `P_touch ≤ 1 × 10⁻³`, i.e. `z ≥ z99 · 10^(1/α)` =
  5.12** at the shipped calibration. For W = 10 it is z ≥ 6.47.

### 4.4 Calibration — where z₉₉ and α come from (hard constraint 2: from data)

**The population.** Every 30-minute window, stepping 1 minute, from 10:00 to 16:00, on
every **non-event** session in the recorded path. That is 34 sessions (07-24 → 09-24) and
15,556 direction-windows, standardized exactly as in §4.2 at the window's start.

Event days are identified **from the audit log itself**: a session on which `signal.*`
records `reason = event_blackout`. That needs no historical calendar file. Excluding them
is correct rather than convenient, because an event day **cannot hold a position**: entries
are blacked out.

> **Superseded for CPI, NFP and PCE (2026-09-29).** Since v0.5.4 those days are traded and
> log no blackout. They stay out, identified from FRED as well as the log, for a different
> reason: → [decision](../decisions/2026-09-29-the-flatten-calibration-excludes-release-days.md).

**The population is all-day, not just the last hour, deliberately.** It is a conservative
superset. It contains the midday shock episodes (09-24 11:00–13:00 and 08-28 11:00 carry
most of the tail), and it keeps the sample 10× larger.

**Measured.** Exceedance means the standardized maximum move over the window is at least z:

| z | Empirical | Gaussian (2Φ(−z)) | Ratio |
|---|---|---|---|
| 2.0 | 0.0196 | 0.0455 | 0.43 |
| 3.0 | 0.0046 | 0.0027 | 1.7 |
| 4.0 | 0.0022 | 0.00006 | **35** |

- **The Gaussian is dangerously thin in the tail**, and the model must not use it.
- **Envelope across window lengths.** L ∈ {5, 15, 30} min gives z₉₉ = 2.31, 2.32 and 2.34 on
  the scratch pass. The shipped tool's pass gives 2.33 across {5, 10, 15, 20, 30}, and that is
  what ships: **z₉₉ = 2.33**.
- **The tail index.** The Hill estimator gives α̂ = 3.5–3.9 on the overlapping windows and
  2.85–4.09 on the 536 non-overlapping ones. That agrees with the well-established
  "inverse cubic law" for index returns (α ≈ 3).
- **The model uses α = min(3, the day-bootstrap lower bound)**, which is heavier than the fit,
  and the loader **refuses α > 3**. The shipped tool gave Hill 3.91 with a 90% interval of
  [2.93, 6.32], so **α = 2.93**.
- **Build note (2026-09-25).** The shipped values are `tools.flatten_calibration`'s output, not
  the scratch pass above. The tool uses `realized.realized_vol` exactly as the runtime does,
  and takes event days from the log itself: 28 usable sessions from 08-05, when IV recording
  began. The difference moves the k = 10 boundary from 5.04 to 5.12 and changes no replay
  decision.
  34 sessions cannot certify a thinner tail than the published one.

**Why the tail has to be modelled at all.** A budget of 10⁻³ is below what 34 sessions can
certify empirically. By the rule of three, zero exceedances in the 536
non-overlapping direction-windows only bound the rate at ≤ 5.6 × 10⁻³. Extrapolation is therefore unavoidable, and
it is done with a stated, conservative tail rather than a hidden one.

**Backtest.** In the last 30 minutes, the closest any non-event 15:30 → 16:00 path came to
the z = 5.12 deferral boundary was **1.93σ, 38% of the way**. Zero violations in 34
sessions.

### 4.5 Eligibility (hard constraint 1: never skip a position that is spent or failing)

Deferral is considered only if **all** of these hold. Each is a plain read of in-memory
state:

1. **Only the clock fired.** After `_may_act_on`, `fired == {FLATTEN_BEFORE_CLOSE}`.
2. **Nothing Argus sent for it may still be working, and no exit of it has failed.** The
   key is not in `_attempted` (so the flatten's one-shot override and the stub
   continuation are excluded by construction), not in `_exiting`, has no retired profit
   take with `order_may_be_working`, and **is not in `_sends`** — no closing send came
   back `NOTHING_AT_BROKER` or `CROSS_UNPRICEABLE` (review fix: hard constraint 1 says
   spent *or failing*, and a retryable failure leaves the key out of `_attempted`).
3. **The remaining backstop can see.** `spot` is fresh, and the short strike is known
   through `short.contract` (the chain identity, **never a leg quote**).
4. **The inputs exist.** A finite `sigma_implied` taken since the mark (carried only as
   §4.2 allows), and a `sigma_realized` that passes `realized.py`'s own coverage rules.
   - **4a. The watchdog has watched spot since 30 minutes before the mark** (15:00, or
     12:00 on a half-day; review fix). This is what makes the intraday rollback of §4.9
     true: a restart inside that window disables the deferral for the rest of the day.
5. **The system can act.**
   - The last successful `get_live_orders()` poll is within `2 × refresh_interval_s`: one
     missed poll is tolerated, two are not. This needs a new `_live_orders_ok_at` stamp.
   - The last successful scan is within the same bound.
   - The watchdog is not degraded.
6. **The welded size invariant holds: `max_concurrent_positions × max_loss_per_position
   ≤ daily_max_loss`** (amended 2026-09-25; the first cut required a cap of exactly 1).
   The test is per position and is valid for any number of positions in expectation,
   since expected losses add. What a second position changes is concentration — two
   same-side spreads are touched by one move — so the bound is on the DOLLARS one barrier
   event can cost across every deferred position, which this invariant holds to the day's
   loss budget: the same 950 the approved one-position configuration had. **`config.py`
   refuses `flatten_deferral = "on"` otherwise**, through one predicate
   (`RiskConfig.open_risk_is_welded`) that this condition re-reads at runtime. The
   derivation is in the
   [decision record's amendment](../decisions/2026-09-25-deferring-a-far-otm-flatten.md#amendment-more-than-one-position-2026-09-25).

### 4.6 Placement, the no-latch argument, and failure handling

**The pure core.** A new module, `execution/flatten_deferral.py`, holds the arithmetic:
stdlib `math`, no I/O. It contains `standardized_distance(...)`, `touch_probability(...)`,
and `decide(inputs, params) -> DeferralDecision`. That result carries `defer`,
`ineligible_reason`, `z`, `p_touch`, `lhs = k·P·W`, `premium`, `sigma_implied`,
`sigma_realized`, `tau_min` and `distance_points`. The watchdog gathers the inputs.

**Where it runs.** It is applied in `_check_one` right after `_may_act_on`, for positions
not in `_attempted`, on that method's contract: **it only ever REMOVES
`FLATTEN_BEFORE_CLOSE`.** A deferred tick takes the ordinary no-action path, so the
position is still evaluated, marked and recorded every second.

**Any exception inside the deferral path means "not deferred".** The flatten proceeds, and
the failure is recorded (`watchdog.flatten_deferral_failed`, Tier 2). This is one of the
very few places where catching and continuing is correct, because here the fail-safe
direction *is* the action. A pure function cannot raise `AuditWriteError`.

**No latch is needed.** A tick that does not defer hands off a flatten exit, and one of two
things follows:

- **It sends an order.** Then `_attempted` is set, and condition 2 bars deferral for the
  rest of the day.
- **It sends nothing.** Then deferring on the next tick is exactly as safe as the first
  time.

So at most one real flatten order can come of a position that hovers near the boundary.

**The records need a bound the orders do not** (review fix). On a book that cannot be
built, every release is a `NOT_ATTEMPTED` and the next tick re-defers, so a position
hovering at the boundary crosses it again and again. Two rules keep that to a handful of
rows:

- a tick the deferral withholds is **not a falling edge** of the `_TRIGGERED` episode,
  because the flatten's condition is still true (the `_PROFIT_TRIGGERED` rule), so a
  released flatten writes `watchdog.triggered` and pages once;
- `watchdog.flatten_undeferred` is Tier 1 and paged the first time each distinct reason
  releases a position, and Tier 2 (`watchdog.flatten_undeferred_again`) for a repeat.

No hysteresis constant is added, and the decision is still re-checked every tick.

**Performance.** Nothing extra runs until a position's flatten is true, and then only for
the ≤ 30-minute window. Per tick that costs one `compute_regime` over the chain snapshot
(sub-millisecond) and one `realized_vol` over about 30 grid points. The spot deque append
runs every tick all day, which is O(1).

### 4.7 Records, pages, `/status`, monitoring

- **`watchdog.flatten_deferred`** (Tier 1) is written on a position's first deferral of the
  day and carries the full `DeferralDecision`.
  - It pages once as `watchdog_flatten_deferred`, with `candidate_id` at the top level.
    For example: *"flatten deferred: z 6.02 (P_touch 6.2e-4, k·P·W 0.031 ≤ 0.05); stops
    live to the close."*
  - Later re-deferrals are Tier 2.
- **`watchdog.flatten_undeferred`** (Tier 1, and paged) names the condition or budget
  that failed, the first time each distinct reason releases a position; a repeat is Tier 2
  (§4.6). The flatten is handed off on the same tick.
- **`watchdog.flatten_deferral_outcome`** (Tier 1) is written once for each position
  that was deferred at any point. Its normal writer is `PositionWatchdog.stop()`
  (`ended: session_end`), because the scheduler stops the session AT the close — a pass
  at or after 16:00 is not guaranteed (review fix). It records:
  - the minimum `z` and minimum distance after the first deferral;
  - whether the barrier was touched;
  - which price triggers fired (never the flatten itself, which is true throughout);
  - `seconds_to_close` when written (≈ 0 is the close; more is a SIGTERM or an abort).

  **This is the reliability dataset.** The model predicts touches at ≤ `premium/(k·W)`
  per deferral, so they can be counted against that.
- **`watchdog.check`** carries the `DeferralDecision` on every tick while the flatten is
  true.
- **`/status`** gains a "Flatten deferred" row per position (z, P_touch, budget, since)
  and the mode on its summary line. It reads a frozen row through **one** new converter
  in `session.py`, which is **added to the seam sweep**.
- **Recalibration triggers.** The tool is rerun and a config-only release cut when any of
  these happens:
  1. a `flatten_deferral_outcome` records a touch;
  2. any non-event 30-minute window exceeds the largest standardized excursion in the
     current calibration (5.35σ);
  3. the monthly re-fit moves α or z₉₉ by more than their bootstrap interval;
  4. the data grows by 50% since the last calibration.

**The calibration tool** is `argus.tools.flatten_calibration`, read-only (`mode=ro`),
`--config` required.

- **Pure core, thin I/O**, like `tools.regime` and `realized.py`.
- It reads `signal.*` `underlying_price` and `regime.atm_iv` from one or more databases,
  and excludes `event_blackout` sessions.
- **It prints:**
  - the CCDF tables per L ∈ {5, 10, 15, 20, 30} (all-day and 15:00+);
  - z₉₉ as the envelope across L;
  - α̂ by Hill, with a block bootstrap by day for its interval;
  - the recommended `alpha = min(3.0, α̂ lower bound)`;
  - the last-30-minute backtest, as closest approach to the boundary per day;
  - a replay of every live flatten;
  - the outcome dataset's touch count against the budget;
  - the TOML snippet, including a provenance string.
- **Its tests** pin the Gaussian case (a Brownian path reproduces 2Φ(−z) within
  tolerance), the Hill estimator on a known Pareto sample, and the envelope rule.

### 4.8 The pasted framework's criteria, mapped

| Criterion there | Here | Why it changed |
|---|---|---|
| Short delta < 0.02 | `P_touch(z)`, fat-tailed, to the trigger barrier | A delta read off a 0.00/0.05 quote is ill-conditioned, and it measures *finishing* ITM rather than *needing an emergency exit*. |
| Time to expiry ≤ 15–30 min | `tau` inside `z` | It is continuous rather than a step, and gets tighter or looser with volatility. |
| Mid < 0.10 | Premium = one tick, and no quote read at all | The mid is a mark on a book that cannot trade (N1). Using the least possible premium is conservative. |
| Mid-day stop must close; late flatten may skip | Only the clock trigger is ever deferred | An emergency trigger is never deferred (condition 1). |
| Never skip a failing position | §4.5, conditions 2–5 | As the open decision requires. |
| Short-leg-only liquidation | **Rejected** (§11) | It breaks the single-legging non-negotiable. |

### 4.9 Modes, config, rollout

**Five REQUIRED `[watchdog]` keys, identical in all three profiles.** Each is dimensionless
or prose, and none is a price or a distance:

| Key | Value | Kind |
|---|---|---|
| `flatten_deferral` | `"off"` \| `"record"` \| `"on"` | strict string |
| `flatten_defer_insurance_multiple` | `10.0` (k; **decided 2026-09-25**) | policy; must be finite; refused `< 1` |
| `flatten_defer_tail_z99` | `2.33` | model, from the tool; refused `≤ 0` |
| `flatten_defer_tail_alpha` | `2.93` | model, from the tool; refused outside `(1, 3]` |
| `flatten_defer_source` | provenance: tool version, databases, sessions, date, **and the k decision** | prose, required, so provenance cannot be dropped (the `trigger_source` precedent) |

**Changing k is a config change, never a code release.** The keys fall into two groups:

- **Policy:** k, and the mode.
- **Calibration:** `z99` and `alpha`, which only the tool produces.

All four are loaded from the profile, and nothing in `src/` carries a default or a
literal for any of them. An AST test pins that `flatten_deferral.py` reads them only from
its `params` argument. The procedure, and the rules that make it safe:

- **It is a config-only release, and a restart, never hot-reload** (CLAUDE.md's standing
  rule). Edit `argus.live.toml`, run `argus-deploy.sh`, and check that the config
  fingerprint read back from `/status` has moved. **Do it outside 15:00–16:00 when a
  position is held.** A restart then disables deferral for the rest of the day (the
  realized-vol buffer is empty), which is safe, but it is not the change you meant to make.
- **Raising k is the safe direction.** Argus defers less often and flattens more, so it
  needs no new analysis. It is the lever to pull if a deferral outcome or a recalibration
  trigger (§4.7) ever looks wrong, and it is also the fastest partial rollback short of
  `"off"`.
- **Lowering k needs evidence, not just an edit.** It means accepting more tail risk.
  Before any release that lowers k:
  1. rerun `tools.flatten_calibration` on the latest data;
  2. read its backtest and the accumulated `flatten_deferral_outcome` rows;
  3. write a short decision record, citing §4.10's sensitivity table at the new value.

  The loader refuses k < 1, where Argus would decline insurance even when it is
  *underpriced*. That is not a risk appetite, it is a sign error.
- **k lives only in `argus.live.toml` in practice.** Cert has no entry path and dev has no
  positions, but the key stays identical in all three profiles, so the profile test's key
  sets do not diverge. If k is ever set differently per profile, that goes on the
  allowlist with its reason.
- **The loaded k is visible where decisions are made.** Every `DeferralDecision`, and so
  every deferral page, Tier 1 row and `watchdog.check` payload, carries the `k`, `z99` and
  `alpha` it was computed with. The `/status` summary line shows them beside the mode. A
  change is confirmable on the page, and every historical decision says which parameters
  produced it, so outcomes can be split across a change the way deploy days are split by
  PID.

In `record` mode the decision is computed and recorded with `acted: false`, and the
flatten proceeds exactly as today. A differential test pins that. `record` is kept as a
mode because it costs nothing, and gives a way to watch without acting if that is ever
wanted. **It is not the shipped value.**

**Rollout (D3): ship `on`, in the one release.** Revision 3 staged this through
`record` "to validate live inputs". That reason does not survive a failure-mode review:

- **Every live-input failure already fails safe.** Each of these makes the position
  *ineligible*, and the flatten proceeds exactly as today:
  - `atm_iv` missing or NaN with nothing carried forward;
  - the realized-vol buffer short or gappy (`realized.py`'s own refusals);
  - a stale poll or scan;
  - a degraded watchdog;
  - any exception in the deferral path.

  `record` mode would observe these outcomes. It would not prevent a single one, because
  none of them is harmful.
- **The one harmful failure is a wrong-but-plausible input**, one that inflates z. Three
  guards cover it at least as well as a `record` stage would:
  1. **The IV is proven to be the calibrated IV.** A wiring test builds a chain snapshot,
     runs it through the watchdog's own IV path and through `compute_regime` directly,
     and asserts they are identical. It also asserts the watchdog passes
     `MarketCalendar.close_time(today)` (so a half-day is correct) and `regime.py`'s
     `MINUTES_PER_YEAR` (imported, never redefined). This is the wiring bug `record` was
     meant to find, caught before deploy instead of after three live days.
  2. **The first live decision is seen the same afternoon.** The Tier 1 deferral page
     carries z, P_touch, `k·P·W`, `sigma_implied`, `sigma_realized`, `tau` and the
     distance in points. A wrong input is visible on the phone at 15:30, alongside the
     spot and strike that anyone can check by eye.
  3. **Two rollbacks already exist:**
     - **Intraday, for today:** any restart after 15:00 (12:00 on a half-day) disables
       deferral for the rest of the session: condition 4a fails, because the new
       watchdog has not watched spot since 30 minutes before the mark, and the flatten
       fires. (As first built this rested on the empty realized-vol buffer alone, which
       re-arms after ~24 minutes; the review made it its own condition.) It is an
       ordinary `systemctl restart`, and the standing restart rules apply: a restart
       re-runs reconciliation with a position open.
     - **Permanently:** `flatten_deferral = "off"`, as a config-only release.
- **The residual exposure, if all three guards miss a defect,** is a position held
  without its flatten. The credit stop and the underlying trigger stay live until the
  close. A loss needs a move through the strike inside the last 30 minutes, and is
  capped at width − credit ($450 at 1 lot, `max_loss_per_position` $950).

### 4.10 What 2026-09-24 does under Part A (`on`, k = 10, z₉₉ = 2.33, α = 2.93)

| Time | New behaviour |
|---|---|
| 15:30:00 | All six eligibility conditions hold. z = 6.02 gives P_touch = 6.2 × 10⁻⁴ and k·P·W = 0.031 ≤ 0.05, so the flatten is **deferred**. One Tier 1 row and one page. |
| 15:30 → 16:00 | Re-evaluated every second. z never falls below 6.02; it rises as tau shrinks. **No order, no walk, no cross, no zero-mid refusals, no unpriceable edges and no spent pages.** The credit stop and the underlying trigger (7630) stay live. At the close, `flatten_deferral_outcome` records: not touched, minimum z 6.02. |
| Settlement | **+$50.00 − $3.44 = +$46.56.** |

**What brings the flatten back on 09-24:** spot at or below about **7694 at 15:30**, or
about **7656 at 15:55**, at unchanged volatility. A move that large also raises realized
volatility, so it would come back earlier still. It would then fire at once, through the
unchanged exit path.

**The other four days:**

- **09-17** defers (z 5.17, k·P·W = 0.049).
- **09-18, 09-22 and 09-23 flatten as today.** Revision 2's 40-point rule would have
  deferred 09-18 and 09-23. Measured in σ, both were closer than they looked.

**Sensitivity to k** (the one policy choice):

| k | Budget (W = 5) | Deferral boundary | Defers |
|---|---|---|---|
| 5 | 2 × 10⁻³ | z ≥ 4.04 | 09-17, 09-23, 09-24 |
| **10** | 1 × 10⁻³ | z ≥ 5.12 | 09-17, 09-24 |
| 20 | 5 × 10⁻⁴ | z ≥ 6.47 | none |

### 4.11 Tests

**Pure core:**

- `z` is scale-invariant: multiplying spot, strike and buffer by c leaves it unchanged.
- The call side's sign is correct.
- `tau` comes from the day's own close, so a half-day is covered.
- `P_touch` is monotone in z.
- Width scales as `2^(1/α)`.
- Contracts cancel.
- Every §4.10 number reproduces from the recorded inputs: z, P_touch, the boundary, and
  the 7694/7656 spot levels.

**Replays.** All five live days, driven through `check_once` with the recorded spot, IV
and clock, give §4.10's decisions.

**Eligibility.** Each condition alone blocks deferral (parametrized, including no IV since
the mark, RV refused after a restart, a broken welded size invariant refused by the loader, and modes
`off` and `record`).

**Invariants:**

- The deferral only removes the flatten: `result ⊆ fired` and `fired − result ⊆ {FLATTEN}`.
- An exception inside the deferral path leaves the flatten intact.
- A `NOT_ATTEMPTED` handoff re-defers; any sent order bars deferral.
- Mutation: drop condition 2, and the sent-order test fails.

**The three rollout guards (§4.9):**

- The watchdog's IV equals `compute_regime` run directly on the same snapshot, with the
  same `close_et`, including on a half-day.
- The deferral page carries every input of the `DeferralDecision`, and the page and the
  Tier 1 row come from one builder.
- A watchdog built fresh (the restart case) with a flatten already due does not defer,
  and flattens instead — **for the rest of the day** if it started watching after the
  window opened, even once its realized-vol path is usable again (condition 4a).

**AST pins:**

- The decision reads no leg `bid`, `ask`, `cost_to_close` or `crossing_debit`.
- It reads no `SignalLoop` object.
- `MINUTES_PER_YEAR` is imported from `regime.py`, never redefined.

**Config:**

- Five required keys, each refusal tested (including a non-finite k and k < 1), and all
  three profiles load.
- An AST test pins that `flatten_deferral.py` reads k, `z99` and `alpha` only from its
  `params` argument, with no default and no literal.
- A test pins that every `DeferralDecision` payload carries the k, `z99` and `alpha` it
  was computed with, and that the `/status` line renders the loaded values.
- A test that changing k in a loaded `Config` changes the §4.10 boundary as the formula
  says: k = 5 gives z ≥ 4.04, k = 20 gives z ≥ 6.47.

**The tool.** Its three pinned properties (§4.7).

**Seams.** The sweep includes the new converter.

---

## 5. Part B — price the cross by what can fill (Finding 1, revised by N1)

```python
if market.long.bid > 0.0:                          # a leg-in is possible: the natural can trade
    magnitude = max(natural, abs(last_rung) + policy.step)    # never re-bid a refused price
    basis = "natural" if natural >= abs(last_rung) + policy.step else "past_ladder"
else:                                              # nothing to sell the long leg to (N1)
    magnitude, basis = natural, "natural_no_long_bid"         # today's arithmetic, deliberately
magnitude, basis = cap_at_width(magnitude, market.width)      # floor-quantized; basis "width" if it binds
```

- `last_rung` is `walk.final_limit_price`, falling back to `order.limit_price`. For a
  walk-away it is always the last rung actually bid.
- `natural` is `round_to_tick(crossing_debit)`, unchanged.
- `build._cap_at_width` becomes the public `cap_at_width`, and its one caller is updated.
- An unpriceable natural still returns `None`, so `CROSS_UNPRICEABLE` and
  `_RETRYABLE_EXITS` are exactly as today.
- **The threshold is structural, not tuned.** A zero long bid is the book condition under
  which a leg-in cannot exist. There is no number to choose.

**Where each branch applies:**

- Option A (pay past the ladder) applies where the natural is tradeable: credit stops, the
  underlying trigger, and near-money flattens.
- The zero-bid branch leaves a resting order at the cheapest price the evidence supports.
  N1 measured that a higher bid does not attract a responder, and a lower bid costs less
  if one arrives.

**Invariants:**

- The cross never exceeds the width.
- With a long bid and the width not binding, `|cross| ≥ |last_rung| + step`.
- A cross at the width is still sent.

**Recorded** on `watchdog.crossed` and its page, telemetry that nothing decides on:
`limit_basis`, the raw `crossing_debit`, `short_ask` and `long_bid`.

**Tests:**

- 09-24: −0.10, basis `natural_no_long_bid`.
- The same walk with a 0.05 long bid: −0.25, basis `past_ladder`.
- A natural of 0.35: −0.35.
- Width binding at −4.95 and at −5.00.
- `CROSS_UNPRICEABLE` unchanged.
- A property test over openings, steps, `step_ticks` ∈ {1, 2}, widths ∈ {5, 10} and float
  noise.
- **Mutations:** read `order.limit_price` for the last rung; drop `+ step`; invert the
  long-bid test; cap before the max.
- The profit path still never reaches `_cross`.

The 2026-09-17 premise *"a limit walking up the grid cannot skip a resting offer"* gets a
dated correction: it holds for a resting complex offer and fails for a leg-derived natural.

---

## 6. Part C — report the rung the walk stopped at (Finding 2)

- `walked_away_at` becomes `walk.final_limit_price` (falling back to `order.limit_price`),
  with `opening_limit` and `walk_steps` beside it.
- **One builder (`_crossed_detail`) feeds both the Tier 1 row and the page.** Each call
  site adds only its own keys.
- **The reader rule is structural.** A row carrying `opening_limit` is corrected. A row
  without it is correct only if `exit_finished.walk.steps = 0`.
- **Rows already written:** of the four `watchdog.crossed` rows that exist, only 09-24's
  is wrong. It stays as written, and the 2026-09-17 reproduction recipe gets a warning.
- **Tests:**
  - a 3-rung walk-away gives −0.20, `opening_limit` −0.05, `walk_steps` 3;
  - a 0-step walk-away gives `walked_away_at` equal to `opening_limit`;
  - the row and the page agree;
  - mutation: restoring `order.limit_price` fails a test.

---

## 7. Part D — the close that is still working is visible (N4)

- **Register the cross.** After `_cross`'s submit returns an ack with an id and a working
  status, the order is registered with `ResidualOrderRecheck.register(...)`:
  - `minted_direction = crossing.direction`, i.e. as Argus built it;
  - `walk_outcome = "crossed"`;
  - `cancel_requested = False`;
  - a new optional `limit_price` on `ResidualOrder` and `OutstandingResidual`.

  It is a **closing** residual, so P5's rule applies: paged, never cancelled, resolved on
  DEAD or FILLED, and no action taken on an absence. **Its FILLED resolution is Tier 2
  and unpaged** (`order.residual_resolved`, review fix): `order.residual_filled` means an
  abandoned order filled and the position is real, which for a cross is the opposite of
  what happened.
- **Name it where the operator looks.**
  `ResidualOrderRecheck.working_close_for(external_identifier)` returns a frozen row. It
  feeds `_page_if_spent_and_still_held`, `SpentExitAttempt` and the `/status` spent row as
  `working_close: {broker_order_id, limit_price, working_for_s}`. On 09-24 each page would
  have read *"close #509260365 working at 0.10 for N min."* The page kind is unchanged.
- **The seam.** `_to_unclosable` gains the field, and the sweep forces the copy.
- **Tests:**
  - a crossed exit registers exactly one closing residual with the cross's id and limit;
  - it is never cancelled in either `unmanaged_order_cancel` setting;
  - the page and `/status` name it;
  - a terminal ack, or one with no id, registers nothing;
  - the sweep covers the new fields.

---

## 8. Finding 3 — closed by measurement, nothing built in this release

**The finding is true.** DXLink *"publishes events only as they occur"* (vendor docs, read
2026-09-25). A quiet leg therefore reads as stale against `market_data_max_age_s`, and
after 15:00 ET 53% of live watchdog evaluations have a stale leg (p50 3.9 s, p99 26 s).

**Its consequences, measured on the 2026-09-15 → 09-24 live trail, are already handled or
too small to justify the build:**

| Where staleness could cost | Measured | Disposition |
|---|---|---|
| **The flatten** (the record's motivating case) | Every fresh look at the five flatten books found zero-mid (N2). A confirmed quote would have hit the same refusal, and all five outcomes are unchanged. | **Handled by Part A.** A deferred position never reaches the refusal. |
| **The credit stop** | Of **21,423** stale-leg ticks on held positions, **21,333 (99.6%)** came while the last mark was **below 25% of the credit stop**, 81 at 25–50%, 6 at 50–75%, and 1 at 75% or above. **All six live credit stops fired on fresh quotes**, with no `problems` at the trigger. Quiet legs are the far-OTM ones, where the stop is nowhere near. | **No observed cost.** |
| **The cross** | Part B's zero-bid branch prices the cross from the natural. The single cross on record priced from fresh legs. | **No observed cost.** |

**Against that, the build is the largest surface in this design, and it was the only part
adding a production unknown.** It needs a new port and adapter, a background task, and a
REST call about once a second while a leg is quiet. Those calls run on the event loop,
include OAuth token refreshes, and hit `/market-data/by-type`, whose rate limit is
unpublished. It is not known whether that limit is shared with anything that can move
money. Shipping it in `record` mode would not have removed that unknown, only measured it.

**Disposition.** Finding 3 is closed by the decision record for this release, with the
measurement above and the design preserved in revision 3 of this file (git history). It
has three **reopen triggers**:

1. a credit-stop or underlying-trigger exit that is delayed, or goes unfilled, while a leg
   was stale at the trigger;
2. stale-leg ticks with the mark at 50% or more of the credit stop exceeding 1% of stale
   ticks, over any five-session window;
3. `risk.max_concurrent_positions` or the size rails rising, which changes what a delayed
   stop costs.

`tools.desk` or the nightly ledger can report trigger 2 from the rows that already exist,
with no new recording needed.

**The design to use on reopen** (revision 3, §8):

- **How it asks:** REST snapshot corroboration. A stream quote is admitted only if a
  snapshot, asked within `market_data_max_age_s` and after that quote arrived, agrees with
  it within `PRICE_EPSILON` on both bid and ask.
- **Price source:** the snapshot never becomes a price.
- **Isolation:** entry is isolated structurally.
- **Rejected alternative:** a DXLink re-subscribe, because it manufactures freshness in
  the shared `QuoteBook`.
- **Gates:** gate 0 before building, then derived production gates.

---

## 9. What this changes nothing about

| Rule | Why it still holds |
|---|---|
| The open decision's hard constraints | Constraint 1 is §4.5. Constraint 2 is §4.4, a calibrated model with a tool, a backtest, monitoring and recalibration triggers. |
| The watchdog is independent of `SignalLoop` | IV is computed by the watchdog from the shared chain source with a pure function. The signal loop is not read. |
| `market_data_max_age_s` never raised; nothing admitted for being quiet; entry keeps 2 s | Nothing admits a quote in this release. Part A reads no leg quote at all. |
| `[regime]` landmine | Untouched. No `[regime]` section. `atm_iv` becomes an input to a separately calibrated *deferral* model, never to the dormant skew/IV gate. |
| One multi-leg order; no broker-side stops; no unbounded orders | Untouched. |
| The cross requires positive confirmation that the walk's order is dead | `_await_settled` is untouched. |
| One exit attempt; `_EXIT_SEND_ATTEMPTS`; `_RETRYABLE_EXITS` | Untouched. Deferral happens before any handoff and never touches `_attempted` or `_flatten_attempted`. |
| Size rails; loss bounded by the width; the welded caps | Untouched. The loader couples `on` to the welded invariant, which bounds what one barrier event can cost across every deferred position (§4.5, condition 6). |
| The flatten never invents a price | Untouched. A deferral sends nothing. |
| Kill switch, halts, audit ordering | Nothing new halts. The new Tier 1 rows are written by the watchdog. |
| Broker round trips on the watchdog path | None added. Part A reads in-memory state only. Part D registers into an existing registry, and its recheck rides the existing `get_live_orders()` poll. |

---

## 10. Constant audit

Every number this release introduces, and where it comes from:

| Constant | Source |
|---|---|
| `z99`, `alpha` | Calibrated by the tool (§4.4); the loader refuses α > 3. |
| `k` | Policy, with a sensitivity table (§4.10). |
| `premium = TICK` | The least expressible close (`zerobid.TICK`); conservative (§4.3). |
| `W` | The position's own strikes. |
| The barrier | The existing `underlying_buffer_points`. |
| The IV and RV estimators | The existing pure functions, the same ones used for calibration. |
| The RV window (30 min) and grid (60 s) | `realized.py`'s constants, matched to the calibration windows. |
| The IV carry-forward | `regime.MIN_MINUTES_TO_SETTLEMENT`; the direction of its bias is documented. |
| Poll and scan freshness `2 × refresh_interval_s` | One missed poll tolerated. |
| The welded size invariant | An existing invariant, already required of every profile by `tests/test_config_profiles.py` (§4.5, condition 6; amended from "cap = 1"). |
| Part B `long_bid > 0` | Structural (whether a leg-in can exist). |
| Part B `+ policy.step` | The ladder's own rung. |
| Finding 3's reopen thresholds (50% of the stop; 1% of stale ticks) | Reporting triggers only. Nothing decides on them at runtime. |

**Nothing is a price level or a distance in points.**

---

## 11. Considered and not done

- **Short-leg-only closes.** They would make a fill possible, at the same cost as the
  natural, but they break the single-legging non-negotiable. Part A removes the case more
  cheaply. If ever wanted, it needs its own decision record.
- **Changing the zero-mid refusal.** It stays. Under Part A it only ever meets
  non-deferred flatten exits, which are the nearer ones where the book has value.
  - 09-22 (z 3.35) and 09-23 (z 4.55) will still meet it and send nothing. That is the
    honest residual.
  - Their settlement risk is covered by the live stops, and their outcomes are already
    visible in the existing exit and `check_spent` records.
  - **Revisit if a non-deferred flatten held by the refusal then loses.**
- **The ladder's time budget (N6) and the Tier 1 edge flicker (N7).** Both are real, and
  both are mostly retired by Part A. They are recorded so that nobody rediscovers them.
- **A portfolio version of the budget.** Not needed while the welded size invariant
  holds (amended 2026-09-25, when the caps moved to 2 spreads × 1 contract at unchanged
  total risk): the per-position test is valid in expectation for any count, and the
  invariant bounds what one barrier event can cost across every deferred position. A
  side-level rule (defer a side all or nothing, on its nearest barrier) remains the
  natural form if `k` is ever redefined as book-level insurance.
- **Gap between the last print and the official close.** SPXW settles on the official
  16:00 close. The gap between the last tick Argus sees and the settlement print is not
  measured, and the 5-point buffer is assumed to cover it. The tool should compare the
  recorded last tick with the settlement print in `/etc/argus/settlements.csv` once that
  history exists.
- **Quiet-quote confirmation (Part E).** Out of this release; see §8.

---

## 12. Release plan — one release

| Contents | Class |
|---|---|
| **Part A:** `execution/flatten_deferral.py`; the watchdog's IV, spot buffer, `_live_orders_ok_at` stamp and eligibility; records, pages and the `/status` row; the five REQUIRED keys **with `flatten_deferral = "on"`** in all three profiles. **`tools.flatten_calibration`**, which produced the shipped `z99`, `alpha` and `flatten_defer_source`. **Part B**, **Part C**, **Part D.** The `trigger_source` prose correction in `argus.live.toml` (§5). | **One coupled image + config release.** It has new REQUIRED keys, so the image and config deploy together, after the close, through `argus-deploy.sh` with the fingerprint and version read back. |

- **If Parts B, C and D are built before A,** they may go out first as an image-only
  release. That is a scheduling choice, not a risk one, and the coupled release follows.
- **Before deploy:** the full suite; the three rollout guards (§4.9); and
  `tools.flatten_calibration` rerun on the latest NAS snapshot, so the shipped `z99` and
  `alpha` come from the newest data and the provenance string says so.
- **The first live check is the first 15:30 with a position held.** Read the
  deferral-or-flatten page against the spot and strike on it. At the close, read
  `flatten_deferral_outcome`. Nothing else has to be scheduled.

### 12.1 Why one release (the risk review behind revision 4)

| Part | Worst plausible defect | Would a staged release catch it? | Bound |
|---|---|---|---|
| A — deferral | A position deferred that should have flattened | **Mostly no.** Every input failure already fails to "no deferral", so `record` would only observe harmless outcomes. The one harmful case (a wrong-but-plausible input) is covered by §4.9's guards, which run **before** deploy. | The stops stay live. Loss ≤ width − credit (**$450 at 1 lot**), and only on a move through the strike inside 30 minutes. A restart after 15:00 disables it for the day. |
| B — cross pricing | A wrong cross price | **No.** There have been 4 crosses in all of history, so a staged week would not see one. | The width cap, `_await_settled` and the one-attempt rule are unchanged. On a zero-bid book it is identical to today. |
| C — reporting | A wrong number in a record | Nothing to catch. | No trading effect. |
| D — tracked cross | A closing order cancelled by mistake | **No**, and it is structurally excluded: `cancel_licence` refuses CLOSE, AST-pinned. | It only ever pages. |

**Staging's real costs are not zero.** Each extra release is its own deploy, with its own
failure modes (v0.3.18's deploy failed on the deploy script itself), a restart, and
operator time. Each `record` day with a flatten that fills pays $10–25. And while Part D is
unshipped, a false *"nothing is closing this"* page keeps inviting a manual double close.
**Urgency is modest in dollars and real for Part D.**

---

## 13. Decisions for sign-off

1. **D1 — Part A's framework.** An insurance-pricing test, `k · P_touch(z) · W ≤ TICK`:
   - on a standardized barrier distance, with `sigma = max(IV, RV)`;
   - with a calibrated Pareto tail;
   - behind six eligibility conditions;
   - re-checked every tick;
   - only ever removing the flatten.
2. **D2 — DECIDED 2026-09-25: k = 10**, as the config key
   `flatten_defer_insurance_multiple`, changeable by a config-only release under §4.9's
   rules: raising needs no analysis; lowering needs a calibration rerun and a decision
   record. z₉₉ = 2.33 and α = 2.93 are calibration outputs, rerun by the tool before deploy.
3. **D3 — Rollout.** Ship **`on`** with the three guards of §4.9. `record` stays available
   as a mode, but is not the shipped value.
4. **D4 — Part B.** Option A only when the long leg has a bid; otherwise today's natural.
   A cross at the width is still sent.
5. **D5 — Part D.** Register the cross as a closing residual, and name it on the spent
   page and on `/status`.
6. **D6 — Finding 3.** Closed by measurement, **nothing built**, with three reopen
   triggers. Revision 3's design is preserved for reopening.
7. **D7 — The zero-mid refusal stays** (§11), with its revisit condition.
8. **D8 — One coupled release** (§12). B, C and D may ship first only if they are ready
   before A.

---

## 14. Build notes (2026-09-25)

What the build changed or found, against the design above:

1. **The calibration numbers are the tool's, not the scratch pass's** (§4.4 build note).
   z₉₉ 2.33, α 2.93, boundary 5.12. The replay of all 15 recorded flattens (cert and live)
   defers exactly 09-17 and 09-24.
2. **`ResidualOrderRecheck` pages any residual still working past the 3-second
   cancel-settle allowance.** Correct for an abandoned rung, and a false alarm for a
   resting cross, which is SUPPOSED to be working. So the cross registers as
   `working_expected` and pages only past `unmanaged_order_max_s`.
   `working_close_for` prefers that row over the walk's abandoned rung, which is also a
   registered close for a poll or two.
3. **One pre-existing test encoded Finding 1 as an expectation.**
   `test_walking_away_submits_one_market_crossing_order` pinned the cross at the 1.90
   natural after a walk that had bid 2.05. It now pins 2.10 (`past_ladder`), with the
   reason in the test.
4. **The suite's base config allows two concurrent positions at 1000 each against a
   1000 day**, which breaks the welded invariant, so the deferral tests state
   `max_concurrent_positions = 1`; the two-position case has its own test at 2 × 475
   against 950. The loader refuses `on` otherwise, which was the first thing the new
   tests hit.
5. **A blind backstop releases a deferral into today's path exactly.** With the legs
   stale too, the released flatten is the existing unpriceable escalation and sends
   nothing. Pinned by a test.
6. **`/status` naming the working close** is a warn line under the spent row, not a
   change of the row's red headline. The headline still says the attempt is spent; the
   line says what is resting and to check the broker first.
7. **The same-day code review found seven defects and one open question; all eight are
   fixed**, each in the safe direction, none with a config key, and each pinned by a test
   that fails when the fix is reverted (12 mutations, 12 caught). The table is in the
   [decision record](../decisions/2026-09-25-deferring-a-far-otm-flatten.md#review-fixes-2026-09-25).
   In one line each: the outcome row is written by `stop()` (§4.7); release records are
   bounded per reason and a withheld tick keeps `_TRIGGERED` open (§4.6); the restart
   rule is its own condition 4a (§4.5, §4.9); the IV carry is scoped (§4.2); a filled
   resting cross resolves quietly (§7); the outcome counts price triggers only; `decide()`
   refuses a non-finite time and spells every refusal so NaN refuses; and a position with
   a failed exit send is ineligible (condition 2).

