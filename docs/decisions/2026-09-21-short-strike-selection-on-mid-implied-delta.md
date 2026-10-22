# Short-strike selection on mid-implied delta

**Status: BUILT 2026-09-21; PRE-DEPLOY GATE PASSED 2026-09-21; DEPLOYED in v0.4.6 (Alshain), 2026-09-21** on 10,840 recorded
live legs, with criterion 2 amended from the median ABSOLUTE gap to the SIGNED
median before it was accepted (see [The gate](#the-gate-passed-with-criterion-2-amended)). Coupled image + config deploy
(REQUIRED `signal.short_delta_source`, `"mid"` in all three profiles), no migration,
no new audit category. `target_short_delta` (0.15) and `min_credit` (0.50) are
unchanged by this design, deliberately; see [Consequences](#consequences), which is
the part of this record that must be read before approving it.

**Deployed** in [v0.4.6](../releases/v0.4.6.md), coupled image + config, read-backs
`version: v0.4.6` and `config.sha b941e29cc1bf`. The first live session on `"mid"`
is not yet read.

## The defect

The short strike is chosen as the leg whose **feed** delta is closest to
`target_short_delta` (`signal/strategy.py:298`). The credit that `min_credit` then
tests is computed from the leg **mids** (`strategy.py:324`). Those are two different
sources, and combining them does something neither does alone.

**The feed delta is an estimate with error.** Its greeks can be up to
`staleness.greeks_max_age_s` (120s) old, and the feed publishes them in chain-wide
bursts (median gap 137s, 2026-08-27). On 2026-09-21, the first day on which leg
bid/ask was recorded, the call-side gap between the feed delta and the delta implied
by the same leg's mid decomposed as:

| | Calls | Puts |
|---|---|---|
| Mean gap, all rows (2,192 call / 1,288 put short legs) | +0.007 | +0.002 |
| Offset remaining at zero staleness (`a`) | +0.003 | 0.000 |
| Slope on γ × spot move since greeks capture (`b`) | 1.13 (R² 0.67) | 0.64 (R² 0.43) |

Staleness explains most of the per-row error, as the arithmetic predicts (b ≈ 1).

**The credit floor selects the error's dangerous tail.** Among strikes that all read
≈0.15 on the feed, a spread's value rises with its TRUE delta, so the ones that clear
0.50 are disproportionately those where the feed UNDER-states delta, i.e. the strikes
that are really closer to the money. This holds even when the error averages zero,
and it is a selection effect, not a staleness effect: shortening the greeks bound
would shrink the error but not remove the selection. Measured:

- **The five live entries of 2026-09-21 had a mean gap of +0.041**, six times the
  all-rows mean. Their true (mid-implied) short deltas were 0.182, 0.197, 0.182,
  0.151 and 0.193: **Argus was selling ~0.19-delta calls, not 0.15.** Entry 1's greeks
  were 3 seconds old.
- **In the settlement record (2026-07-23 to 08-10, `shadow.outcome`), call ideas
  whose credit barely cleared the floor (0.50–0.55) breached 42.9% of the time** (35
  ideas, 7 days) against 10.5% for 0.55–0.70. Ideas that only just clear the floor
  are the ones the mechanism predicts are the most mis-selected. Seven days; the
  interval spans zero; corroboration, not proof.

→ [research note](../../RESULTS.md) §5
and addendum

## Decision

Select the short strike by the delta **implied by that leg's own mid**, computed on
every tick from the same quotes the credit is computed from. Selection and credit
then share one source, so there is no estimation error for the floor to select on:
a quote that ticks up raises both the credit and the implied delta together, which
moves selection OUTWARD — the safe direction.

The feed delta stays on the record, unchanged, as telemetry.

## Design

### A new pure module: `signal/implied.py`

Stdlib only (`math.erf`), no I/O, no clock, fully deterministic. It **imports its
conventions from `signal/regime.py` rather than restating them**, so the two
estimates of the same market can never drift apart:

| Convention | Source | Value |
|---|---|---|
| Time basis | `regime.MINUTES_PER_YEAR` | calendar year (CBOE convention) |
| Time to expiry | `RegimeSnapshot.minutes_to_settlement` | half-day aware, via `MarketCalendar.close_time` |
| Minimum time | `regime.MIN_MINUTES_TO_SETTLEMENT` | 10 min |
| Underlying | `regime.implied_forward` | put-call-parity forward |
| Discounting | `regime.DISCOUNT` | 1.0 |

Model: **Black-76 on the forward**, which is what makes a call and a put at the same
strike imply the same vol when priced off one forward.

```
d1 = ln(F/K) / v + v/2,   v = σ√T,   d2 = d1 − v
call = F·N(d1) − K·N(d2)          put = K·N(−d2) − F·N(−d1)
delta: call N(d1), put N(d1) − 1   (forward delta; = spot delta at DISCOUNT 1)
```

**`implied_vol(price, F, K, T, right) -> float | None`**

1. Refuse (`None`) unless `intrinsic < price < upper`, with upper `F` for a call and
   `K` for a put. Out of bounds means no vol can produce this price.
2. **Newton on σ with a bisection safeguard**, bracket `[0.005, 5.0]`: each
   iteration narrows the bracket by the sign of the price error. A Newton step that
   leaves the bracket, or a vega below 1e-12, bisects instead.
3. Converged when `|price(σ) − price| < 1e-7` points. `None` after 40 iterations.
4. Never raises, and never returns NaN or inf (the `OptionQuote.delta` docstring
   explains why NaN is the one value `min()` cannot reject).

**`selection_delta(quote, F, T) -> MidDelta | None`**: `None` unless the quote is
two-sided (`bid > 0` and `ask ≥ bid`). The price inverted is the **raw** mid
`(bid + ask)/2`, never `floored_mid`; the floor only fires at `bid == 0`, which is
refused here. Returns `(delta, iv)`.

**Measured cost:** about 2.7 µs per leg from a cold start (5.2 iterations on
average). Solving every out-of-the-money two-sided leg on both sides, about 60 legs,
costs roughly 0.2 ms per 5-second tick, against the 50 ms event-loop lag alarm.
**So there is no warm-start cache and no early-exit search:** both would add state
and failure modes to save microseconds. Every candidate leg is solved every tick.

**Why a quote spread needs no new threshold:** a half-spread `h` moves the implied
vol by `h / vega`. At these strikes vega is about 0.43 per vol point, so a 0.05
half-spread moves vol by about 0.1 point and delta by about 0.003. Even a 0.25
half-spread moves delta by about 0.014. Tolerable without a knob.

### Selection (`CreditSpreadStrategy._select`)

Under `short_delta_source = "mid"`:

1. `T` comes from `regime.minutes_to_settlement`. **`regime is None` or fewer than
   `MIN_MINUTES_TO_SETTLEMENT` minutes: no short leg is selectable**, reason
   `no_short_leg_with_mid_delta`. There is no fallback to the feed delta, because a
   silent fallback re-introduces exactly the selection effect this removes. (The
   orchestrator always passes a regime; `None` occurs only in tests and tools, which
   must now supply one.)
2. `F` is `implied_forward` over the chain's usable legs. If it is unavailable (no
   strike quotes both a call and a put), **fall back to `underlying_price` and record
   `forward_source: "spot"`.** The spot-forward basis is ~0.1–0.5 points, a delta
   error of ≤0.007, well inside the 5-point grid; refusing the tick would cost more
   than it protects.
3. Short candidates: **out-of-the-money legs only** (calls with `K > F`, puts with
   `K < F`) with a solvable `selection_delta`. The feed delta is never consulted.
4. Choose `argmin |abs(delta) − target|`. **A tie goes to the farther out-of-the-money
   strike**, the lower-risk one; today the tie falls to chain order, which is
   unspecified.
5. Everything after is unchanged: the long-leg search, the credit from floored mids,
   the `min_credit` test, the `credit < width` test.

Under `short_delta_source = "feed"`: today's behaviour exactly (`has_delta` and the
feed delta), with the mid delta computed and **recorded but not used**.

### Config: one REQUIRED key

```toml
[signal]
short_delta_source = "mid"   # "mid" | "feed"
```

REQUIRED, no default, and the loader refuses any other value. That is the
repository's rule for a key that changes trading behaviour. It is a new key, so this
is a **coupled image + config deploy**. It is also the rollback lever: switching back
to `"feed"` is a config-only release (a restart after the close, never mid-session
with a position open). All three profiles carry it, so
`tests/test_config_profiles.py`'s identical-key-set rule holds.

### Telemetry: nothing decides on any of it

Every number goes through `signal.models.payload_number`. A non-finite value in the
Tier 1 `signal.candidate` row raises inside `SignalLoop.run`'s generic handler, which
would silently suppress every entry (2026-09-15).

- **`signal.candidate`**: each leg gains `mid_delta` and `mid_iv`. The existing
  `delta` keeps meaning the feed delta, because `tools.marker` and `tools.desk` read
  it. The top level gains `delta_source`, `forward`, `forward_source` and
  **`alt_pick_strike`**: the strike the OTHER source would have chosen. Recording the
  counterfactual on every tick measures the disagreement rate and direction from the
  first session, at no cost.
- **`signal.none`** near-miss (flat keys, matching the record around them):
  `short_mid_delta`, `short_mid_iv`, `long_mid_delta`, `alt_pick_strike`,
  `delta_source`, `forward`, `forward_source`.
- **New rejection reason `no_short_leg_with_mid_delta`**, carrying counts by refusal
  cause (one-sided, out of bounds, no convergence). It ranks with
  `no_short_leg_with_fresh_delta` in `_REASON_RANK`.

### What does not change

- `target_short_delta` (0.15), `min_credit` (0.50), `width`, `max_width`, the long-leg
  search, the credit arithmetic, the gate, the sizer, loss-budget sizing.
- `live_chain` admission. It already admits a leg on a fresh quote alone (the
  2026-08-24 role split), which is all this needs.
- Exits, the watchdog, the stop and the flatten. None of them use delta.
- `staleness.greeks_max_age_s` stays. It still governs `"feed"` mode and
  `has_delta`, but under `"mid"` it no longer decides which strike is selectable.

## Consequences

**This section is the price of the change, stated before approval, not discovered
after it.**

1. **Fewer call entries in low-vol regimes.** Under call-wing skew, a true 0.15-delta
   5-wide call spread is worth about 0.41–0.50, so `min_credit` will refuse most call
   candidates when vol is low. **On 2026-09-21, three or four of the five call entries
   would not have happened**: the strikes a mid-delta selector would have chosen were
   worth 0.44, 0.46, 0.41 and 0.50. This is intended. The call side's edge is
   unmeasured (the settlement record's interval is ±$75 per contract), its cheapest
   ideas were its worst, and the floor now acts as a clean filter on thin call premium
   rather than as a selector of estimation error.
2. **More short strikes selectable per tick**, because selection no longer waits for
   a greeks burst. Expect more candidates, mostly puts, and a higher share of
   `credit_below_min` near-misses on calls.
3. **The traded short delta returns to about 0.15**, within the grid's ±0.03, instead
   of ~0.19 on calls. The model puts the stop rate at about 40% instead of 45%, which
   matters because the measured market is mean-reverting and each stop costs money.
4. **A data boundary.** Call trades before this deploys were effectively ~0.19-delta
   trades. **Never pool call outcomes from before and after this deploy** when measuring the
   strategy; use `delta_source` on the record to split them.
5. **Entry frequency will change, and in which direction depends on the regime.**
   More selectable ticks push it up; more call refusals push it down. The first week's
   `gate.decision` and `signal.none` counts should be read against that, not against
   the prior week.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Shorten `greeks_max_age_s` | Shrinks the error, keeps the selection. Starves selection at the feed's burst cadence (2.8% of ticks selectable at 2.0s, 2026-08-27). |
| Gamma-correct the feed delta (Δ + Γ·δS) | Needs spot at greeks capture, which is not recorded. Leaves the feed's model offset. Still two sources. |
| Select by credit (farthest strike clearing 0.50) | Did $4.6–7.9 a day worse than delta-first in every clean model comparison: it sells least premium when premium is rich, and moves closest to the money when it is cheap. |
| Raise the target to 0.19 (what was actually traded) | Edge per unit of risk peaks at 0.12–0.15 and falls after. More stops in a mean-reverting market. |
| Lower `min_credit` to a cost floor (~0.30) | Would open the call side while its edge is unmeasured. Revisit when the settlement record supports it. |
| Per-side delta targets | Data cannot fit a per-side edge curve yet. Coarser side levers come first. |

## Rollout: ship in `"mid"`, gated by an offline replay

**The change does not need more evidence; the new code needs verifying.** The case
for selecting on mid-implied delta is made above. What remains is the ordinary risk
of any new calculation that trades money: a wrong sign on puts, the wrong time to
expiry on a half-day, a solver that fails on odd quotes. A record-only live phase
would catch those, but only by spending more sessions trading on the selection this
record exists to remove. The same check can run **before deploy**, against quotes
already recorded: since 2026-09-21 every `signal.none` and `signal.candidate` row
carries each selected leg's bid and ask, several thousand legs per session.

(Cert cannot exercise this either way: its entry path has been stopped since
~2026-09-02 by the lost 5-point strike ladder.)

### The pre-deploy gate

A replay test runs `implied.py` over recorded live rows. It must pass before the
image ships.

- **Data.** A fixture extracted read-only from 2026-09-21's `signal.none` and
  `signal.candidate` rows (`underlying_price`, `regime.minutes_to_settlement`, and
  each recorded leg's strike, right, bid, ask, feed delta and greeks age) is committed
  under `tests/fixtures/`. It holds public market quotes only; nothing about the
  account. A read-only tool, `argus.tools.replay_selection --config … --date D`, runs
  the same checks against any later recorded day (`mode=ro`, no lock, safe mid-session).
- **Criteria:**
  1. The solver succeeds on **≥99%** of recorded two-sided out-of-the-money legs.
     Query 2 ran the same math over 6,955 of 2026-09-21's legs, solved all of them,
     and skipped one wide quote.
  2. **Put short legs with greeks ≤10s old: |median(|mid delta| − |feed delta|)| ≤ 0.005**
     — the SIGNED median. This is the convention check, on the side where the feed has
     agreed with the mids (query 2: 0.001). *Amended 2026-09-21 from the median of the
     ABSOLUTE gap*, which measures dispersion rather than bias; see
     [The gate](#the-gate-passed-with-criterion-2-amended).
  3. **The five entries of 2026-09-21 reproduce** short mid deltas of 0.182, 0.197,
     0.182, 0.151 and 0.193, each to ±0.001.
  4. **Every new telemetry field is finite or `None`** after `payload_number`, on every
     replayed row.
- **What the replay cannot cover, and what does.** Recorded rows hold only the two
  legs chosen for each side, not the full chain. So the replay (a) cannot compute the
  put-call-parity forward and uses spot instead, which exercises the fallback path and
  accounts for any residual in criterion 2; and (b) cannot re-run the full strike
  search. The search — out-of-the-money only, closest to target, tie to the farther
  strike, no feed fallback — is covered by the unit tests on synthetic chains under
  [Testing](#testing).

### Deploy

1. **Ship with `short_delta_source = "mid"` in all three profiles.** A coupled image
   and config deploy, after the close, flat.
2. **`alt_pick_strike` stays on every row permanently**, so the live disagreement
   rate between the two sources is visible from the first session, by side and
   direction. The call side should mostly disagree outward.

### Watching the first sessions, and when to roll back

Not gates, but read after each of the first few sessions:

- `no_short_leg_with_mid_delta` should be rare. **Roll back** if it appears on more
  than 1% of in-window ticks: the solver is refusing live quotes the replay accepted.
- Every chosen short's mid delta should sit within about 0.12–0.18 (the target ± the
  grid). **Roll back** on any entry outside 0.10–0.20.
- Call entries should fall and `credit_below_min` near-misses on calls should rise
  (Consequence 1). That is the change working, not a fault.

**Rollback** is `short_delta_source = "feed"`: a config-only release, after the
close, never mid-session with a position open.

## Testing

- **`implied.py`, by property:** price → vol → price round-trips across strikes,
  times from 10 minutes to 6 hours, and vols from 5% to 200%. Put and call at the
  same strike imply the same vol off one forward. |delta| decreases with distance
  out of the money. Out-of-bounds prices, zero bids and crossed quotes return `None`.
  NaN or inf never escapes.
- **Selection:** the mid mode picks by mid delta and never reads `delta`. The feed
  mode is byte-identical to today, so the existing tests pass unchanged. `regime is
  None` refuses with the new reason. A forward fallback is flagged. A tie goes to the
  farther strike. An in-the-money leg is never a short.
- **The recorded-quotes replay** (the pre-deploy gate, under Rollout): solver success
  rate, put-side convention agreement, the five 2026-09-21 entries reproduced to
  ±0.001, and payload safety, all over the committed fixture.
- **Payload safety:** every new field goes through `payload_number`, joining
  `tests/test_payload_safety.py`.
- **Config:** the key is required and enum-validated, and all three profiles carry
  it.
- **Performance:** a benchmark test, skipped by default, that times `evaluate` on a
  60-leg chain and reports per-tick cost. Wall-clock is never asserted in CI.

## Decisions taken (2026-09-21, Richard)

Both open questions were answered as recommended:

1. **A missing forward falls back to spot**, recorded as `forward_source: "spot"`,
   rather than refusing the tick. The spot-forward basis moves delta by ≤0.007, well
   inside the 5-point grid.
2. **The replay's market-data fixture is committed** under `tests/fixtures/`, trimmed
   to the fields the replay reads: public quotes only, nothing about the account. That
   keeps the pre-deploy gate runnable in CI, not only where the production database is
   reachable.

## As built

Built as designed. What the design left open, and what building it decided:

- **`RegimeSnapshot` moved to `signal/models.py`** (still re-exported from
  `strategy.py`, where every caller imports it). `implied.py` imports `regime.py`'s
  conventions and `strategy.py` imports `implied.py`, so leaving it in `strategy.py`
  closed an import cycle through `regime.py`.
- **The solver** starts at the Manaster–Koehler point (the vol where vega peaks,
  clamped to ≥0.2), which makes Newton monotone from either side; the bracket
  narrows on every iteration, so bisection is a guarantee. `N` is spelled with
  `erfc`, not `erf`, because every leg asked about is OTM, where `1 + erf` cancels
  most of N's digits. Measured on a 4,054-case grid (10 min–6.5 h, 3%–400% vol,
  ±600 points): no failures, 5.7 evaluations mean, 11 max. `evaluate()` on a
  62-leg chain costs ~0.13 ms/tick (`-k benchmark`, never asserted in CI).
- **A price needing a vol outside `[0.005, 5.0]` is `no_convergence`**, not
  `out_of_bounds`: the arbitrage bounds are the only thing checked before solving,
  and checking the bracket's endpoints up front would cost two price evaluations on
  every leg to relabel a refusal that essentially never happens.
- **`forward_source` is `"parity"` or `"spot"`**, both `None` together with
  `forward` when no mid delta was computable at all. The forward is formed once
  per tick over the chain's tradeable legs (the set `compute_regime` uses) and
  shared by both sides.
- **`no_short_leg_with_mid_delta` carries `candidates` (OTM legs asked),
  `refusals` (`one_sided` / `out_of_bounds` / `no_convergence`) and `cause`**
  (`no_time_to_settlement` or `no_forward` when nothing could be asked), plus the
  selection context — including `alt_pick_strike`, so a tick the mid selector
  refused still records what the feed would have sold. Every per-side rejection
  past `no_tradeable_legs` carries the selection context, in both modes.
- **The long leg's `mid_iv` is recorded too** (`long.mid_iv` on the candidate),
  beside the listed fields; the near miss carries exactly the listed flat keys.
- **A synthesised `SpreadSignal`** (chunk 4's exit path) records the new keys as
  nulls, never as absences.
- **`"feed"` mode is byte-identical in SELECTION**, chain-order tie included. Its
  records gain the new keys, which is the point of recording the counterfactual.
- **The startup coverage warnings are mode-aware**: under `"mid"`, a greeks feed
  with no fresh reading no longer claims that no signal can fire.
- **The test fixtures keep `"feed"`** (`tests/support.py` and the four hand-built
  `SignalConfig` helpers): their chains carry hand-set feed deltas and no
  regime-consistent prices. The mid selector's search is pinned in
  `tests/test_strategy_mid_delta.py` on Black-76 chains whose true deltas are known
  and whose feed deltas are set independently — including the defect itself (feed
  sells the closer strike, mid selects the true one and the floor refuses it).
- **`tools.replay_selection` takes `--db FILE`** as an alternative to `--config`,
  so a pulled snapshot is one command; `--expect-entries` supplies criterion 3.
  An *entry* is a candidate whose `gate.decision` allowed it AND whose opening
  `walk.started` followed — a guard-blocked candidate is not one.

### The gate: passed, with criterion 2 amended

Run 2026-09-21 after the close against that evening's 16:30 snapshot
(`argus-20260921T163001.db`); the fixture is committed at
`tests/fixtures/selection_replay_2026-09-21.json` (10,840 legs, market quotes only),
so the gate now runs in CI.

| Criterion | Result |
|---|---|
| 1. Solver success on two-sided OTM legs | **100%** — 10,840 of 10,840, no refusals of any kind |
| 2. Put fresh-greeks (≤10s, n=280) signed median gap | **−0.0003** (bound ±0.005) |
| 3. The five entries | **0.1822, 0.1972, 0.1816, 0.1514, 0.1926** against 0.182, 0.197, 0.182, 0.151, 0.193 — all within ±0.0003 |
| 4. Reconstructed payloads refused | **0** of 10,840 |

The all-rows mean gaps also reproduce this record's defect table: +0.0020 on puts,
+0.0071 on calls (table: +0.002, +0.007).

**Why criterion 2 was amended, and why it was not loosened.** As first written it
tested the median of the ABSOLUTE gap and failed at **0.0051**. That statistic is
dispersion, not bias, and it does not move with anything a convention error would
move:

| Put short legs, greeks age | n | Signed median | Median \|gap\| |
|---|---|---|---|
| ≤ 2 s | 57 | −0.0004 | 0.0046 |
| ≤ 10 s | 280 | −0.0003 | 0.0051 |
| ≤ 30 s | 812 | +0.0001 | 0.0054 |

It stays ~0.005 as the greeks get FRESHER, and shifting the replay's forward from
spot−1 to spot+1 moves it only within 0.0049–0.0060 — so neither staleness nor the
spot fallback explains it. It is the floor set by half-spread noise (~0.003 of delta
per 0.05 of half-spread) and the feed's own vol model. A put sign error, a wrong time
basis or a wrong forward is a systematic OFFSET, which is what a signed median
measures; and "query 2: 0.001", which the bound was set against, matches the signed
figure (and §5.3's single-tick +0.0004/+0.0010), not the absolute one. So the
statistic changed to match the criterion's stated purpose, the bound stayed at 0.005,
and the dispersion is still printed beside it, ungated. The rejected alternative —
keep the absolute gap and set the bound from this day (~0.006) — would have been a
scatter threshold fitted to one session, no better at catching a sign error.
`tests/test_replay_selection.py` pins both halves: a ±0.02 bias fails in either
direction, and zero-offset scatter wider than 09-21's passes.

**One reading from before the fixture existed**, kept because it was reasoned about
at the time: the same arithmetic over the 09-16–09-18 CANDIDATE rows (each leg's
recorded `mid` standing in for bid/ask) gave 0 failures in 8,146 legs and a median
ABSOLUTE put gap of 0.0061 — the same dispersion, on the credit-floor-selected tail.
