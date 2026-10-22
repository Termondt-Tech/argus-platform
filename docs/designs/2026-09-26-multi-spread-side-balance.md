# Multi-spread books: balancing puts and calls — a quantitative spec

**Status: APPROVED 2026-09-26 (Richard, as "package B") and BUILT the same day for v0.5.2, DEPLOYED in v0.5.2 (Mira, 2026-09-26)** (first live session under it: 2026-09-28) → [decision record](../decisions/2026-09-26-one-position-per-side.md).
§6's recommendation was adopted: strict balance (S), put first on a tie, and no put lean.
**One deviation from §7.4:** the tie-break reuses `signal.sides` order instead of a
third key, so the config is `risk.side_balance` (bool) and `risk.max_side_imbalance`
(int). **Amended 2026-09-27 (Richard, option b of the v0.5.2 review's Q2):** the
symmetric band and the absent `would_lean` field are accepted, and §1, §6, §7.4, §7.5,
§8 and D6 now describe what was built. See the build notes below. D1–D3 and D6 were decided as drafted. D7 was overtaken: it ships with v0.5.2,
bundled with the strike-touch stop. D8 is built (`other_sides`). D9 is resolved by the
profit target shipping OFF ([strike-touch stop](../decisions/2026-09-26-strike-touch-stop.md)).

**Build notes: deviations found by the 2026-09-27 review** ([findings](../reviews/2026-09-27-v0.5.2-mira.md), #2–#3).
The line above recorded one deviation; the build made four more.

- **(a) The band is one symmetric integer, not §7.4's `max_net_puts` / `max_net_calls`.
  DECIDED 2026-09-27 (Richard, the v0.5.2 review's Q2, option b): accepted.** The design
  is amended to match (§1, §6, §7.4, §8.2, D6): promoting a lean is a code + config
  release, never a config flip. Why, as recommended:
  - the lean has no measured support (§4.1);
  - it costs tail in three of four models (§5.2);
  - it acts on 1–2 days in 24 at N = 2;
  - §3.5 puts its test months away (33–200 sessions at realistic edges);
  - building the two keys now would be a coupled live-config change during package B's
    first live week, for a switch that changes nothing.

  The code change later is small: `side_balance.admissible` becomes `−a ≤ d ≤ b`, with
  the unknowns bounding each end.
- **(b) `would_lean` (§7.5) is not recorded as a field. DECIDED 2026-09-27 (same
  decision): not built, and not needed — it is DERIVABLE from what v0.5.2 already
  writes** (verified 2026-09-27). When no side is admissible, `SignalLoop._choose`
  forwards the strategy's side and labels it `refused`. That `signal.candidate` row
  carries:
  - the refused side;
  - both legs, the credit and the width;
  - `book` (`puts`, `calls`, `unknown`, `choice`).

  §8.1 and §8.2 now name that query as their data source (§8.4). **Keep the label's
  meaning stable:** a change to `_choose`'s `refused` semantics breaks the derivation.
  Two caveats apply to a `would_lean` field just the same:
  - at N = 2 the events are rare (~1–3 per 24 sessions), so §8.2's second criterion will
    have a handful of positions after 40 sessions;
  - the counterfactual must also model the slot the lean would have filled, which no
    single field records.
- **(c) §7.1's AST pin was not built; BUILT 2026-09-27**
  (`test_the_per_side_idea_counts_are_keyed_on_the_identifier_never_a_symbol`).
- **(d) §7.1's "a held leg that cannot be attributed to a side fails closed and counts
  toward both" was built as written, and "toward both" is neutral** for a rule on
  `p − c`. **FIXED 2026-09-27:** unknowns are their own count, and admissibility holds
  `|known d after| + unknown ≤ band`, the worst case over every assignment.
- `book.choice`'s labels are `off` / `first` / `balance` / `refused` / `attempted`, not
  §7.5's `balance` / `tiebreak` / `only_side`, and `book` gained `unknown`.

**The proposal (Richard, 2026-09-26):** with more than one concurrent spread, balance
the book between puts and calls, favouring puts when it is out of balance.

**Where it stands.** `max_concurrent_positions = 2`, `max_loss_per_position = 475`,
`max_contracts_per_position = 1` and `daily_max_loss = 950` are in all three profiles
for the next release. That release has no side rule: the second slot takes whatever
the strategy returns (put first, no fallback past a repeat). The
[2026-09-25 note](../../RESULTS.md) measured that book
and found one position per side the better one. This spec generalizes the rule to any
number of slots, adds the proposal's put preference, and tests both.

**Inputs.** Two read-only snapshots: the cert chain `dev/nas/argus-20260914T163001.db`
(2026-07-23 → 09-14; `[marketdata] source = "live"` throughout, so its quotes and
index path are the real market) and the live chain `dev/nas/argus-20260925T163001.db`
(2026-09-15 → 09-25). **24 sessions** with a full 5-second signal stream; 25
non-event sessions for the index-path measurement. Scripts:
[`../research/2026-09-26-side-balance-queries/`](../../RESULTS.md).

---

## 1. Summary

The proposal has two parts, and the evidence splits along them.

**"Balance" is supported by mathematics, by measurement and by every model.**

- A closed form (§3.2) shows book variance rising with the square of the imbalance,
  at a rate set by how much more same-side positions move together than opposite-side
  ones.
- Measured over 24 sessions, same-side ideas correlate at +0.62 to +0.69 and
  opposite-side ones at −0.26 (settlement) to −0.59 (live exits).
- At the measured correlations, a two-spread book leaning one way has 1.5–2.0× the SD
  of a balanced one.
- Replayed on the recorded stream, strict balance cuts day SD by 21–34% at N = 2 under
  live exits, against the release as configured.
- In the path models at N = 2, strict balance dominates both the release's book and the
  proposal in three of four models: equal or better mean, and CVaR 1% better by
  $125–218 a day against the release (§5.2). The fourth is the momentum model, the
  opposite of the measured market; there it trades up to $10 a day of mean for tail.

**"Favour puts" has no measured support, and there is a reason to doubt it.**

- *Measured edge.* Put ideas returned −0.013 of width less than call ideas under live
  exits, and −0.053 less at settlement, paired by day. Both intervals span zero.
- *Pricing.* The skew prices the down-tail correctly. At the distance each side's
  0.15-delta short actually sits, the index touched puts and calls equally often
  (0.178 against 0.177).
- *Long-run evidence.* Vilkov's 9.5-year 0DTE sample ranks call spreads ahead of put
  spreads as the thing to sell.
- *The one argument for puts* is the equity premium: 0.013–0.040 of width per trade,
  if it accrued intraday. The overnight-return literature says most of it does not.

**The lean costs tail and is rarely used.** "When out of balance, lean toward puts"
adds tail risk in the models. At N = 2 it changed the book on only 1–2 of 24 days,
because the balancing side almost always qualifies within seconds.

**Draft recommendation (§6).**

- **Strict balance**, with **puts first on a tie**. That keeps today's put-first
  ordering and costs nothing measurable.
- **No put lean yet.** It is not built; it has a **pre-registered promotion test**
  (§8.2), and passing it leads to a decision record and a code + config release
  (*amended 2026-09-27*: the draft had the lean ship as config, off). The records the
  test needs are written from deploy day (§8.4). At realistic effect sizes the test
  needs 33–200 sessions.

**One constraint frames all of it (§2.1).** At today's $950, only two spreads fit.
Three or four need `daily_max_loss` raised, which is a scaling decision for the phase
gate, not for this spec.

---

## 2. Definitions and constraints

### 2.1 Feasibility: how many slots the budget allows

The welded invariant (`max_concurrent_positions × max_loss_per_position ≤
daily_max_loss`) and the loss budget's full-width count bound the slot count by the
smallest lot's worst case:

```
N_max = floor( D / L_min ),    L_min = (width − min_credit) × 100 = (5 − 0.50) × 100 = 450
```

At D = 950 that is **N_max = 2**, and it takes D ≥ 1,350 for 3 and ≥ 1,800 for 4. Every
N > 2 figure below is therefore a **scaling scenario** (N × 475 per position, D =
N × 475), shown because the rule has to be right for them too. None is proposed.

### 2.2 The policy family

With `p` puts and `c` calls open (per trade idea, including working opening orders)
and imbalance `d = p − c`, a **band policy B(a, b, tie)** decides each entry:

1. **Priority.** Among the sides that qualify on this tick, the one whose entry leaves
   `|d|` smaller goes first. On a tie (`d = 0`), the `tie` side goes first.
2. **Admissibility.** An entry is allowed only if afterwards `−a ≤ d ≤ b`.
3. **Then today's rules.** The `candidate_id` dedup (a repeat is skipped and the next
   admissible side tried), the size rails and the loss budget.

| Policy | Band | Meaning |
|---|---|---|
| **P0** | — | As built: the strategy's one output, put first, no fallback past a repeat. The release as configured |
| **S** | B(1, 1, put) | Strict balance; puts first on a tie |
| **PF** | B(1, 2, put) | **The proposal**: balance first; when balance is not available, the book may lean one extra position toward puts, never toward calls |
| **CF** | B(2, 1, put) | The mirror, as a control: lean toward calls |
| **PREF** | B(∞, ∞, put) | Balance as a preference only; anything admissible |

At N = 2 the reachable states differ only in the last row:

| State (p, c) | d | S | PF | CF |
|---|---|---|---|---|
| (1, 0), (0, 1), (1, 1) | +1, −1, 0 | yes | yes | yes |
| (2, 0) two puts | +2 | **no** | **yes** | no |
| (0, 2) two calls | −2 | no | no | **yes** |

"Favour puts" is two separable things, and the spec keeps them separate:

- **The tie-break** (which side first at `d = 0`). It is free of risk: both choices
  leave `|d| = 1`.
- **The lean** (`b > a`), which is a bet that puts are the better trade.

### 2.3 Constraints any design must keep

- **The loss budget counts full widths**, never a netted pair. A netted "condor" budget
  raised the worst day from −206 to −430 in the prior replay, because stops make the
  settlement bound false intraday (2026-09-25 note, §4.6).
- **Held sides are counted per trade idea, never by symbol.** Chained spreads net a
  shared strike to zero at the broker and in `positions`. This is CLAUDE.md's fourth
  standing landmine.
- **Every vertical stays one order with its own watchdog, stop and flatten.** There is
  no four-leg order (the single-legging guardrail), and no simultaneous-entry
  requirement.
- **The phase is live, minimum size.** Nothing here raises a rail.

---

## 3. The quantitative framework

### 3.1 Units

- **Return on width**: P&L per contract ÷ (width × 100). It is dimensionless, comparable
  across widths and index levels, and gross of fees, which are identical per contract
  on either side and so cannot move a put-versus-call comparison.
- **z**: a distance in implied standard deviations over the time left, `|ln(K/S)| /
  (atm_iv · √τ)` on `regime.py`'s calendar basis. It is the unit strikes are selected
  in.
- **The day is the unit of evidence.** Ideas within a day share one index path, so every
  interval resamples days.

### 3.2 The composition law (risk)

For a book of `N = p + c` positions, each with day-P&L SD σ, same-side correlation
`ρ_s` and opposite-side correlation `ρ_o`, expanding the variance of the sum and
substituting `p = (N + d)/2`, `c = (N − d)/2` gives, exactly:

```
Var(N, d) = σ² [ N(1 − ρ_s) + N²(ρ_s + ρ_o)/2 + d²(ρ_s − ρ_o)/2 ]
```

- **Variance rises with d², at slope (ρ_s − ρ_o)/2.** Balance (`d = 0`, or `|d| = 1`
  for odd N) is the variance minimum exactly when `ρ_s > ρ_o`.
- **The benefit grows with N.** The reachable imbalance grows with N, so the variance
  an unbalanced book can accumulate grows too.
- Evaluated at the measured correlations (`analytic.py`):

| Correlations from | ρ_s − ρ_o | N=2: d 0→2 | N=3: \|d\| 1→3 | N=4: d 0→2 | N=4: d 0→4 |
|---|---|---|---|---|---|
| Settlement, 24 sessions | 0.88 | 1.48× SD | 1.45× | 1.19× | 1.62× |
| Live exits, fixed-IV (sticky-strike) mark | 1.28 | 2.03× | 1.88× | 1.50× | 2.45× |
| Live exits, IV-tracking (sticky-moneyness) mark | 1.24 | 2.00× | 1.88× | 1.52× | 2.49× |
| Model M6 | 1.17 | 1.68× | 1.57× | 1.24× | 1.77× |

### 3.3 The side-choice law (value)

- **Expected value is linear:** `E[book] = p·μ_put + c·μ_call`. Composition cannot
  create edge.
- **The tie-break carries no risk** (both choices leave `|d| = 1`). Its whole cost is
  `|μ_put − μ_call|` times how often a balanced book is offered both sides at once, so
  it should follow the measured side edge, and it is harmless when that edge is zero.
- **The lean trades risk for edge.** It moves `|d|` from 1 to 2, which adds `2σ²(ρ_s −
  ρ_o)` of variance by §3.2, so it is worth having only if the favoured side's edge
  pays for that.
- **The decision rule is dominance first.** A policy that is no worse in expected P&L
  (within its interval) and no worse in tail (1% quantile, CVaR 1%, worst day) is
  adopted.
- **An exchange rate is needed only on a real trade-off.** When a rule buys expected
  P&L at a tail cost, the owner sets one: `λ` dollars of expected P&L per day given up
  per dollar of improvement in CVaR 1%. This is a policy call, like the flatten
  deferral's k. **The draft finds no such trade-off yet** (§5), so no λ is proposed.

### 3.4 Decision value, bounded

A rule's stake is how often it changes a decision, times the effect of the change.
Both are measured: the replay counts the entries where each band changed what the book
held (`lean entries`), and §4 measures the per-side effect. A rule that fires on 2 days
in 24 with an effect whose interval spans zero is a low-stakes rule, and its default
should be the one with the smaller tail.

### 3.5 Power

Paired by day, the put − call difference in return on width has a day SD of 0.10
under live exits and 0.26 held to settlement (`side_edge.py`). At 80% power and 5%
two-sided, the sessions needed to detect a true difference are:

| True put − call edge | 0.02 of width ($10/ct) | 0.05 ($25/ct) | 0.10 ($50/ct) |
|---|---|---|---|
| Live exits | **~200** | **~33** | ~9 |
| Held to settlement | ~1,280 | ~205 | ~52 |

The drift argument for puts (§4.4) is 0.013–0.040 of width. At that size **no sample
this system will have for months can confirm it.** So the lean cannot be justified
from Argus's own data in the near term, only refuted if it is large and wrong-signed.

---

## 4. Evidence on the sides

### 4.1 Per-side edge, measured (`side_edge.py`)

Every distinct candidate idea (576), scored once from the tick it was first proposed.
Return on width, day-weighted, with 95% day intervals:

| Side | Ideas | Credit / width | Breach at settlement | Median z of short | Held to settlement | Live exits, fixed-IV mark | Live exits, IV-tracking mark |
|---|---|---|---|---|---|---|---|
| Put | 289 | 0.107 | 11.4% | 1.04 | +0.005 [−0.067, +0.063] | −0.015 [−0.034, +0.005] | −0.025 [−0.044, −0.005] |
| Call | 287 | 0.124 | 10.8% | 0.85 | +0.058 [−0.009, +0.109] | −0.001 [−0.026, +0.024] | −0.011 [−0.036, +0.014] |
| **Put − call, paired** | | | | | **−0.053 [−0.154, +0.045]** | **−0.013 [−0.055, +0.026]** | **−0.013 [−0.054, +0.027]** |

- **No side is measurably better, and the point estimates favour calls.** Puts led on
  5 of 24 days held to settlement and on 12 of 24 under live exits.
- **The per-day table shows what the sample measures: its drift.** Put losses cluster
  on August's down days (08-11, 08-20, 08-28) and call losses on September's rally
  (09-18, 09-21, 09-24). That is Vilkov's finding (single-sided verticals load on
  the day's direction, R² 0.23–0.26) showing up in Argus's own record. Over 24
  sessions, "side edge" is mostly which way the index went.
- **Under live exits both sides return slightly below zero.** That is the stop's cost
  (the 2026-09-21 note §A5), and it is a separate question. It does not bear on which
  side to prefer.

### 4.2 Why neither side wins: the skew prices the down-tail (`direction_tail.py`)

A test with more data behind it than P&L separates pricing from outcome. It uses 7,281
start-minutes over 25 non-event sessions, from each minute 10:00–15:00 to the close,
standardized by the recorded ATM implied vol. Those are the shipped flatten
calibration's own session loader and path.

- **Where the shorts sit.** At a fixed 0.15 delta, the put's short sits further out:
  median z 1.06 against 0.96 for the call, on ticks that recorded both sides. That
  gap is the skew.
- **Realized, at those distances:**

| Event | Put | Call | Put − call [95% day CI] | Priced (Gaussian) |
|---|---|---|---|---|
| Index touches the short before the close | 0.178 | 0.177 | +0.001 [−0.162, +0.158] | — |
| Index finishes beyond the short | 0.074 | 0.058 | +0.016 [−0.077, +0.108] | put 0.145, call 0.169 |

- **The down-tail is slightly fatter in standardized terms,** by +0.03 to +0.05 in touch
  probability at z 0.75–1.25, with intervals spanning zero. The skew puts the put
  further out by just enough that touches equalize.
- **Both sides are overpriced:** realized finish-beyond rates are a third (calls) to a
  half (puts) of what the price implies. Over this window the move to the close
  realized 0.67 of its priced SD, the variance risk premium measured by the 2026-09-08
  note, in different units.
- **The sample drifted down:** the mean start-to-close move was −0.11 sd, and it was up
  on only 9 of 25 sessions. Even so, the far tail (z ≥ 2) was on the up side.

### 4.3 The outside evidence

- **Vilkov (2026), Table 3, as read in the 2026-09-15 review.** Over 9.5 years, net of
  friction, the long 0DTE bull call spread lost 0.0115% of spot per trade and the long
  bear put spread made +0.0022%. For a seller that ranks **calls ahead of puts**.
  - Mapping caveats: his structures are near the money, 3× wider in the wing, held
    unmanaged to expiry, and friction applies to buyer and seller alike, so the sign
    flip is only directional.
  - It is still the longest 0DTE sample on either side of this question, and it points
    against the proposal.
- **The standard 30-day finding that index puts are overpriced** is not evidence at
  0DTE, and this draft does not lean on it.

### 4.4 The case for puts, quantified: drift (`analytic.py`)

A physical upward drift moves the index away from put shorts and toward call shorts,
so it favours selling puts. At the 0.15-delta short (z ≈ 1), a 5-wide spread and
6 hours to the close:

| Equity premium | Drift over the session | Put − call edge |
|---|---|---|
| 5% a year | +0.047 sd | +0.020 of width ($10/ct) |
| 7% a year | +0.066 sd | +0.028 of width ($14/ct) |
| 10% a year | +0.094 sd | +0.040 of width ($20/ct) |

- **These are upper bounds.** They assume the premium accrues evenly through the
  trading day. The empirical literature on overnight versus intraday returns (Cooper,
  Cliff & Gulen 2008; Lou, Polk & Skouras 2019) finds most of the US equity premium
  earned *outside* trading hours. **Not re-read for this draft; to verify before
  anyone cites this section.** If it holds, intraday drift is near zero and so is the
  argument.
- **Even at face value, the effect is below what Argus's data can confirm for months**
  (§3.5: ~200 sessions at 0.02 of width).

### 4.5 The case against leaning either way: tails

- **Structurally, the index's largest moves are down, and fast.** Gaps, too, are more
  often down than up. A put-heavy book is the one a gap through the stop finds.
- **In the 24-session sample, the largest standardized move was up** (09-21).
- **The loss budget bounds the dollars either way.** Two 475-dollar puts are the same
  worst case as two calls. What differs is how likely that worst case is, and this
  sample cannot measure it.

---

## 5. The policies, replayed and modelled

### 5.1 The replay (`replay_band.py`, 24 sessions, $ per day)

Each policy is scored three ways: held to settlement (needs no model), and under live
exits on each of two marks (fixed IV, and IV tracking the recorded ATM). The mark
models and their validation against 452k recorded watchdog marks are in the 2026-09-25
note §3.2. "Leaned entries" counts entries that left |d| ≥ 2.

**N = 2, 2 × 1 / 950 (the release as configured).** Each cell is mean / SD / worst day:

| Policy | Held to settlement | Live exits, fixed-IV mark | Live exits, IV-tracking mark | Leaned entries (days) |
|---|---|---|---|---|
| Before: 1 × 2 / 950 | +67.6 / 197.4 / −877 | −21.2 / 148.2 / −532 | −38.0 / 153.7 / −512 | — |
| **P0 (release)** | +49.4 / 157.2 / −397 | −31.2 / 128.3 / −343 | −43.0 / 110.6 / −250 | 6–12 (6–11) |
| **S** | +29.1 / 180.6 / −399 | **−21.8 / 84.3 / −206** | **−34.3 / 86.9 / −212** | 0 |
| **PF (proposal)** | +29.2 / 180.7 / −399 | −23.7 / 93.5 / −262 | −33.6 / 91.7 / −242 | **1–3 (1–2)** |
| CF | +50.8 / 157.7 / −397 | −19.5 / 99.9 / −206 | −36.4 / 107.1 / −283 | 3–9 (3–8) |
| PREF | +50.9 / 157.8 / −397 | −21.4 / 107.9 / −262 | −35.7 / 111.1 / −283 | 4–11 (4–10) |

- **Under live exits,** S has the lowest SD and the best worst day. Mean differences
  against P0 are +8.7 to +9.5 a day, with intervals from about −25 to +48.
- **PF differs from S on 1–2 days of 24.** Where it does, the tail is slightly worse:
  worst day −262 against −206 on the fixed-IV mark, −242 against −212 on the IV-tracking mark.
- **Held to settlement, S is worse than P0 on this sample.** That is one day's doing
  (2026-08-11, where balance put a breaching put in place of a winning chained call;
  2026-09-25 note §4.5). With one entry per slot and no stops, 24 sessions are two
  positions a day and one event sets the SD.
- **The call side is imputed on some ticks** for every policy except P0 (7–15 entries
  at N = 2). This is the gap §7.2 closes.

**N = 4, 4 × 1 / 1900 (a scaling scenario, not proposed).** Each cell is mean / SD /
worst day:

| Policy | Held to settlement | Live exits, fixed-IV mark | Live exits, IV-tracking mark | Leaned entries (days) |
|---|---|---|---|---|
| P0 | +56.5 / 367.9 / −1271 | −98.4 / 179.0 / −425 | −96.3 / 165.4 / −430 | 13–42 (9–22) |
| **S** | **+78.1 / 299.7 / −800** | **−71.5 / 156.8 / −483** | **−91.9 / 144.3 / −431** | 0 |
| PF | +77.6 / 299.5 / −800 | −80.7 / 171.6 / −483 | −102.3 / 153.0 / −431 | 2–9 (1–5) |
| CF | +60.0 / 369.0 / −1271 | −88.7 / 180.7 / −544 | −90.7 / 162.6 / −427 | 7–23 (6–15) |
| PREF | +59.5 / 368.8 / −1271 | −97.1 / 196.2 / −572 | −96.6 / 158.6 / −427 | 10–39 (7–21) |

- **At four slots the as-built book stacks constantly:** 13–42 leaned entries.
- **S has the lowest SD under live exits, and ties PF held to settlement.** It cuts the
  worst day held to settlement from −1,271 to −800.
- **The worst day under live exits does not improve.** On the fixed-IV mark S's worst
  day is −483 against P0's −425; on the IV-tracking mark they are equal (−431, −430).
  One day in 24 sets that number.
- **Balance as a mere preference (PREF) is no better than P0.** Only a band removes
  the stacked days.

### 5.2 The models (`mc_band.py`, 20,000 paired days per row)

The 2026-09-21 path models, flat smile. By symmetry they measure what composition does
to *risk*, never which side is better; PF's lean here stands for a lean toward either
side. **M6 is the measured market** (mean-reverting, ρ30 −0.13). **M4 is its
opposite** (momentum, ρ30 +0.17), so it is the model that would overturn the
recommendation.

**N = 2, 2 × 1 / 950 (the release).** Each cell is mean / SD / CVaR 1%, $ per day:

| Policy | M6 measured | M1 no edge | M2 premium only | M4 momentum |
|---|---|---|---|---|
| Before: 1 × 2 / 950 | −33.6 / 177 / −571 | −21.6 / 157 / −480 | +8.3 / 141 / −395 | +35.6 / 123 / −312 |
| ANY (≈ P0, the release) | −33.3 / 119 / −534 | −22.9 / 96 / −378 | +10.8 / 87 / −284 | **+41.9** / 74 / −161 |
| PREF | −29.7 / 112 / −491 | −22.9 / 95 / −374 | +10.4 / 87 / −279 | +39.9 / 76 / −156 |
| **S** | **−19.0 / 96 / −409** | **−22.6 / 81 / −160** | **+10.0 / 82 / −121** | +31.7 / 82 / **−108** |
| PF (the proposal) | −24.0 / 103 / −445 | −23.0 / 89 / −331 | +9.8 / 85 / −241 | +35.9 / 79 / −140 |

- **In three models of four, S dominates.** Its mean is equal or better (+$0.2 to +$14.3
  a day against ANY) and its CVaR 1% is better by $125–218 a day.
- **S also dominates the proposal** in those three models: CVaR better by $36–171, mean
  equal or better. The lean buys nothing and costs tail.
- **In momentum, S trades mean for tail.** It gives up $4.2 a day against PF and $10.2
  against ANY, for CVaR better by $32 and $53. With persistent trends, balance means
  selling the side the market is moving into. The measured market is the opposite
  (ρ30 −0.13, interval [−0.23, −0.03]), which is why §8.3 makes ρ30 turning positive a
  recalibration trigger.

**N = 3 and 4 (scaling scenarios).** Mean / CVaR 1%:

| Policy | M6, N=3 | M6, N=4 | M1, N=4 | M4, N=3 | M4, N=4 |
|---|---|---|---|---|---|
| ANY | −55.7 / −662 | −83.4 / −891 | −45.4 / −523 | **+50.3 / −261** | **+76.7 / −267** |
| PREF | −41.8 / −557 | −62.6 / −697 | −47.5 / −519 | +45.8 / −294 | +69.8 / −306 |
| **S** | **−40.1 / −539** | **−47.1 / −626** | −48.8 / **−477** | +45.2 / −305 | +59.7 / −353 |
| PF | −40.6 / −550 | −54.3 / −655 | −47.9 / −513 | +45.2 / −305 | +64.5 / −345 |

- **In the measured market, the benefit grows with N.** At N = 4, S beats the
  unconstrained book by $36 a day in mean and $265 in CVaR 1%.
- **At odd N the lean almost never binds** (0.4–2.3% of days at N = 3). Balance
  already leaves one side ahead by one.
- **In momentum, the unconstrained book wins outright at N = 3 and 4.** This is the
  model-dependence the recalibration triggers exist for.

---

## 6. Draft recommendation

1. **Adopt strict balance, S = B(1, 1, put), as the rule for any N ≥ 2.**
   - At N = 2 it is "at most one open spread per side".
   - "Favour puts" survives as the **tie-break**: when the book is balanced and both
     sides qualify, the put goes first. This is today's ordering, and at a measured
     side edge indistinguishable from zero it costs nothing.
2. **Do not ship the lean (PF) on by default.**
   - It has no measured value (§4.1), and the skew prices the down-tail (§4.2).
   - The longest outside sample points the other way (§4.3).
   - The one argument for it is an upper bound that the intraday-return literature
     disputes (§4.4).
   - It adds tail in the models and the replay (§5), and at N = 2 it acts on too few
     days to matter either way.
3. **Ship the band as ONE symmetric config value, `risk.max_side_imbalance`** (§7.4).
   *Amended 2026-09-27 (decided, Richard)*: the draft shipped a two-sided band so the
   lean could be promoted without a code change. As built and accepted, promoting a lean
   (by the §8.2 test, or by the owner's call on the §4.4 argument) is a decision record
   plus a code + config release that adds the asymmetric band.
4. **Keep the tie-break as config too.** It is `signal.sides` order (put first), so if
   §8's measurement ever puts calls ahead with confidence, flipping it is still a
   config-only release.

---

## 7. Engineering design

### 7.1 Held-side counts (`risk/models.py`, `risk/snapshot.py`)

`RiskSnapshot` gains `put_spread_count` and `call_spread_count`. Each follows
`open_spread_equivalent`'s own rule, per side:

```
side_count = max( ceil(broker legs of that right / 2), ledger trade ideas of that side )
             + working OPENING orders of that side
```

- **Ledger side** comes from the trade idea's key; **working-order side** from
  `parse_external_identifier`, never the broker's leg labels (the broker relabels
  them). **Broker side** comes from the right in the OCC symbol.
- **Taking the higher of broker and ledger** keeps both existing protections: an
  unbooked broker leg still consumes its side, and chained spreads, which net to
  zero at the broker, are still counted by the ledger.
- **A held leg that cannot be attributed to a side fails closed** and counts toward
  both.
- **An AST test pins that the ledger half is keyed on trade ideas, never on symbols.**

### 7.2 The strategy returns every qualifying side (`signal/strategy.py`)

`evaluate` stays pure and position-blind. It returns every qualifying side's signal, in
`signal.sides` order, instead of the first.

- **This closes a data gap.** Today a put candidate means the call side was never
  evaluated or recorded, so every analysis of the call side on those ticks has had to
  impute it. From deploy day, `signal.candidate` records each qualifying side, with
  the chosen one marked.
- It also makes §8's counterfactual measurable.

### 7.3 The selector (`orchestrator.py`, one pure function)

```
select(candidates, put_count, call_count, band, tie, already_attempted) -> SpreadSignal | None
```

- The priority and band of §2.2, then the first candidate not already attempted.
- **The dedup read is new.** Today `_attempted` lives in `EntryPipeline`, so a repeated
  put hides a qualifying call for the tick. The selector takes a read-only predicate
  from the pipeline. In shadow mode it is `lambda _: False`.
- It is pure, so the whole state table is testable without a loop.

### 7.4 The gate refuses too (`risk/gate.py`), and the config

- **Second enforcement.** `PreTradeGate` refuses a proposal whose side would take `d`
  outside the band: reason `side_imbalance_exceeded`. As with the size rails, the
  selector chooses and the gate refuses.
- **As built and accepted (amended 2026-09-27): two REQUIRED `[risk]` keys, identical
  in all three profiles.** The draft's three keys (`max_net_puts` b, `max_net_calls` a,
  `side_tiebreak`) became:

| Key | Value | Meaning |
|---|---|---|
| `side_balance` | `true` | The rule on; `false` is the pre-v0.5.2 entry path exactly |
| `max_side_imbalance` | `1` | a = b: largest allowed `\|p − c\|` |

  The tie-break is `signal.sides` order. There is no lean to configure: that is a code
  change, by decision (§6 item 3).
- **The loader:** `side_balance` a strict bool; `max_side_imbalance` a positive integer.
  With `max_concurrent_positions = 1` the band is inert and the loader says nothing.
- **Deploy class:** REQUIRED keys make this a **coupled image + config** release.

### 7.5 Records and `/status`

- **`signal.candidate`** gains (*as built, amended 2026-09-27*):
  - `book`: `puts`, `calls`, `unknown` (sides that could not be read), `side_balance`,
    `max_side_imbalance` and `choice` (`off` / `first` / `balance` / `refused` /
    `attempted`);
  - `other_sides`: every side not proposed on the tick, qualifying or not.

  **No `would_lean` field** (decided 2026-09-27): the counterfactual behind §8.2 is
  derived from `refused` rows (§8.4).
- **`gate.decision`** carries the two side counts.
- **`/status`** gains a Book row, for example "puts 1 · calls 0 · band −1…+1 · tie put".
  None of these fields decide anything but the band itself.

### 7.6 Tests (the load-bearing ones)

- **The selector's full state table:** every (p, c) with p + c < N, every qualifying
  set, every band, and the dedup predicate. Pinned rows: at N = 2 with a put held and
  both qualifying, S takes the call; with only a put qualifying, S takes nothing and
  PF takes the put.
- **Side counts under chaining:** two chained puts (shared strike, netting to zero at
  the broker) count 2 from the ledger; an unbooked broker leg counts from the broker; a
  working opening order adds.
- **The gate refuses what the selector should never have sent,** with the selector
  bypassed.
- **Config:** the profile drift test, the loader refusal, and inertness at N = 1.
- **Shadow mode:** the selector runs, and nothing reaches the submission boundary.

### 7.7 What this does not change

The strategy's thresholds and delta source; the loss budget and its full-width count;
the watchdog, stops, flatten and far-OTM deferral; the size rails and the welded
invariant.

**The far-OTM flatten deferral's concentration concern shrinks.** Its loader comment
worries that same-side spreads are touched by one move. Under S at N = 2, at most one
position per side is open.

---

## 8. Validation and monitoring (pre-registered)

### 8.1 On every session, from deploy day

- **Book composition over the session.** Time spent in each state, and a `|d| > band`
  observation, which must be zero: a nonzero count is a defect, not a finding.
- **Per-side per-position return on width,** from fills.
- **Lean opportunities** (§8.4's query), and what the leaned position would have
  returned. The counterfactual is scored at settlement and under the live triggers,
  exactly as `side_edge.py` does.

### 8.2 The promotion test for a put lean (PF)

To be fixed here, before any data arrives:

- **Measure:** put − call per-position return on width under live exits, paired by
  day, over recorded ideas (`side_edge.py`, rerun on the full record), **plus** the
  lean counterfactuals' realized return on width, from §8.4's query (*amended
  2026-09-27*: the draft read a `would_lean` field that was not built).
- **Promote PF** when both hold over at least **40 sessions**:
  - the day-resampled 95% interval of put − call lies **above zero**;
  - the counterfactual leaned positions' mean return on width is **above zero**.
- **Promote CF** by the mirror test.
- **A lean is a trade-off, not a dominance** (§3.3), so promotion also needs the owner's
  λ, stated in the decision record.
- **Promotion is a release, not a flip** (*amended 2026-09-27*): the decision record,
  then a code + config release adding the asymmetric band (§6 item 3).
- **Expect the second criterion to be thin.** At N = 2 the lean's events run about 1–3
  per 24 sessions, so 40 sessions give a handful of positions. Read it as a sign check,
  not an estimate.

### 8.4 The lean's counterfactual, from existing records (added 2026-09-27)

No field records it; one query does. **A PF lean opportunity** is a `signal.candidate`
row with:
- `book.choice == "refused"`;
- `side == "put"`;
- `book.puts − book.calls == 1`.

That is exactly the state where B(1, 2, put) admits the put that B(1, 1, put) refused.
**CF** is the mirror: `side == "call"` and `book.calls − book.puts == 1`.

How to read it:
- The row carries the refused spread's own legs, credit and width, so the position is
  scored as `side_edge.py` scores any idea.
- Deduplicate by candidate idea, first tick, because the rule re-refuses every 5 s.
- Exclude rows with `book.unknown > 0`: an unreadable side makes the refusal a
  fail-closed one, not a band one.
- Say what the lean would have displaced: the slot it fills is the one a later call
  would otherwise have taken. The counterfactual book, not the lone position, is what
  §3.3's trade-off is about.

**This depends on `_choose`'s `refused` meaning "no side admissible on the counts"**
(`orchestrator.SignalLoop._choose`). A change to that label's semantics must update
this query in the same commit.

### 8.3 Recalibration triggers (rerun `side_edge.py` and `mc_band.py`, revisit the rule)

- **ρ_s − ρ_o falls below 0.3** over a trailing 40 sessions. The composition law's
  benefit then shrinks below 1.1× SD at N = 2.
- **ρ30 turns positive** over a trailing 40 sessions (`q1_rho_kappa.py`). In the
  momentum model, balance trades mean for tail at N = 2 and loses outright at N ≥ 3
  (§5.2).
- **A side's live-exit interval excludes zero** in either direction.
- **The slot count changes.** This is the scaling decision: rerun at the new N and D.
- **`signal.target_short_delta`, `width` or the stop changes.** Each moves σ and the
  correlations.

---

## 9. Decisions for us to work through

| # | Decision | Draft position |
|---|---|---|
| D1 | Is "favour puts" the **tie-break**, the **lean**, or both? (§2.2) | Tie-break yes; lean deferred |
| D2 | Rule: strict balance S, or the proposal PF? | S by default; PF behind §8.2 |
| D3 | Tie-break side | Put (today's ordering; costs nothing measurable) |
| D4 | Does the §4.4 drift argument carry weight without verified intraday evidence? If yes, what λ (§3.3) prices its tail cost? | Verify the literature first; no λ yet |
| D5 | Promotion test thresholds: 40 sessions, interval above zero (§8.2) | As drafted |
| D6 | Band as config (three REQUIRED keys) vs a fixed rule | Config. **Decided 2026-09-27: one symmetric key (`max_side_imbalance`); a lean is a code + config release** (§6 item 3) |
| D7 | Ship with the next release, or after the first live week on the config-only P0 book | After the first week, so the P0 week measures what the rule then removes |
| D8 | Ship `signal.candidate` recording every qualifying side (§7.2) early, on its own, to start the dataset now | Yes. It changes no decision, and every week without it is a week of imputed call data |
| D9 | The side rule and the profit target interact. Under S, switching the target on raised day SD by 64–74% in the replay, against 20–28% with no side rule, because each take frees a slot that S must refill on a side ([exploration note §3](../../RESULTS.md)). Settle the target's refill before, or with, the side rule | Decide the refill (the exploration note §9, item 1) first |

---

## 10. Reproducing

From the repo root, read-only. Build the tick file once:

```bash
ARGUS_LIVE_DB=dev/nas/argus-20260925T163001.db TWO_SLOT_TICKS=dev/research/side-balance-ticks.pkl ./.venv/bin/python docs/research/2026-09-25-two-slot-queries/extract.py
```

Then, in any order:

```bash
./.venv/bin/python docs/research/2026-09-26-side-balance-queries/side_edge.py        # §4.1, §3.5, correlations
./.venv/bin/python docs/research/2026-09-26-side-balance-queries/direction_tail.py   # §4.2, ~30 s
./.venv/bin/python docs/research/2026-09-26-side-balance-queries/analytic.py         # §3.2, §4.4
./.venv/bin/python docs/research/2026-09-26-side-balance-queries/replay_band.py      # §5.1, ~1 min
./.venv/bin/python docs/research/2026-09-26-side-balance-queries/mc_band.py M6       # §5.2, ~30 min per model
```

`mc_band.py` takes M1, M2, M4 or M6 and needs numpy (present in the dev venv only
transitively). It imports the committed 2026-09-21 simulator unchanged and reproduces
the 2026-09-25 note's two-slot rows exactly (M6, 2 × 1: SD 119.2, CVaR −534).
