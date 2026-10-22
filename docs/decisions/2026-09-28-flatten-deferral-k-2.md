# The flatten deferral's k: 10 → 2, on the rerun calibration (2026-09-28)

**Decided 2026-09-28 (Richard). Config only: all three profiles, no code.** **Deployed in v0.5.4, 2026-09-28** (config sha `d6639d0d7a1a`). It is a restart, and never between 15:00 and 16:00 with a position held.

| Key | Was | Now |
|---|---|---|
| `flatten_defer_insurance_multiple` (k, policy) | 10.0 | **2.0** |
| `flatten_defer_tail_z99` (calibration) | 2.33 | **2.38** |
| `flatten_defer_tail_alpha` (calibration) | 2.93 | **2.80** |
| deferral boundary on a 5-wide | z ≥ 5.12 | **z ≥ 3.05** |

**In plain terms:** the 15:30 flatten is skipped only when its cost (one tick, $5 a lot) is
at least **twice** the expected loss it insures. That loss is valued at the spread's full
width, and the touch probability comes from the calibrated tail. Everything else in
[the deferral](2026-09-25-deferring-a-far-otm-flatten.md) is unchanged:
- it only ever removes the flatten;
- never for a spent, failed-send, blind or unreachable position;
- it is re-decided every tick;
- the touch stop stays live to the close.

## Why

**The prompt.** On 2026-09-28, the first live session under v0.5.2/v0.5.3, the flatten
fired on `20260928-call-7745-7750`.
- The position was ≈ 58 points out of the money at 15:30. That is an estimated z ≈ 4–5 at
  late-day volatility; the exact value is rebuilt from the day's snapshot, since P11 was
  not yet deployed (it is, since v0.5.4).
- It walked 0.05 → 0.20 twice and left a 0.10 debit resting, against a long leg with no bid.
- At k = 10 the deferral skips only at z ≥ 5.12, so it declined to skip. At k = 2 it would
  have skipped this flatten unless σ exceeded ≈ 0.33.

**The evidence.** From
[`flatten_k.py`](../../RESULTS.md) and the
[due diligence](../../RESULTS.md) §2. It is
exploratory: 24 sessions of one regime, with model marks for the flatten's price.

1. **At k = 2 the tail model is checked by data.** At k = 10 it is extrapolation.

   | k | Boundary z | Model P_touch | Observed, 16,350 all-day 30-min windows | 15:30–16:00, 26 sessions |
   |---|---|---|---|---|
   | 1 | 2.38 | 1.00% | 1.00% | 0 |
   | **2** | **3.05** | **0.50%** | **0.46%** | **0** |
   | 3 | 3.52 | 0.33% | 0.26% | 0 |
   | 5 | 4.23 | 0.20% | 0.12% | 0 |
   | 10 | 5.41 | 0.10% | never observed | 0 |

   The model is at or above the data at every boundary. The largest standardized
   15:30–16:00 move in 26 sessions was 1.93.

2. **Its value.** The population is the 41 package-B 15:30 survivors in 24 sessions.

   | k | Skipped | Margin at the boundary (touch at full width / at the touch stop's ≈ $250) | Gain vs k = 10, $/day | Brought back before 16:00 | Modelled touches/yr |
   |---|---|---|---|---|---|
   | 1 | 32 | 1.3× / 2.6× | +1.5 to +5.6 | 6 | 1.2 |
   | **2** | **23** | **2.6× / 5.2×** | **+1.1 to +3.9** (path replay +3.8) | **2** | **0.5** |
   | 3 | 19 | 3.9× / 7.7× | +0.9 to +3.0 | 0 | 0.3 |
   | 10 | 7 | 12.9× / 25.8× | — | 0 | 0.05 |

   - **The gain range** runs from the live fill rate (2 of 6 live flattens filled) with a
     touch valued at the full width, to the replay's fills with a touch at the touch
     stop's loss.
   - **The margin** is the flatten's cost ($6.44) over the expected loss at the boundary.
     It says how far the tail could be understated before a boundary deferral loses money.
   - **The fill rate cancels:** a flatten that doesn't fill leaves the position as exposed
     as a deferral does, so it scales the premium and the tail alike.

3. **Every recorded flatten agrees.** Of the 16 from 08-31 to 09-25, 15 have a z.
   - At k = 2, 11 of the 15 would have been skipped. All 15 expired out of the money,
     and none was touched after the mark.
   - The one that finished in the money (09-03) has no z, because realized vol was
     unavailable. It is ineligible at any k.

**Why not k = 1 or 3.**
- **Not 1:** its extra +$0.4 to +$1.7/day comes from positions 2.4–3σ out. There the margin
  at full width is 1.3×, and 6 of its 32 skipped positions came back before 16:00 anyway.
- **Not 3:** it gives up $0.2–0.9/day for margin that k = 2 does not need.

**The recalibration.** The design's procedure for lowering k (§4.9) requires a rerun first.
- The rerun had 29 non-event sessions through 09-25 and gave z₉₉ 2.38 and α 2.80: Hill 3.76,
  with a 90% day-bootstrap interval of [2.80, 5.48].
- It is heavier than the shipped 2.33 / 2.93, which is the conservative direction, so it
  is adopted with k. On the old values k = 2's boundary would be 2.95.
- The rerun's backtest has zero violations. `flatten_deferral_outcome` rows on record:
  none, because no deferral had yet run live.

## Risk

- **Frequency.** The model expects about one skipped position every two years to reach its
  strike before the close. That is an upper bound: the model is one-shot, and the runtime
  re-decides every second and releases the flatten as z falls.
- **Cost of such a touch.** It is exited by the touch stop, at about $250 a lot. If that
  exit also fails, the cost is at most width minus credit, about $445 a lot.
- **Across both positions:** at most $890, inside `daily_max_loss` 1050. The loader still
  refuses `on` without the welded invariant.
- **Model risk:** one regime. A stress regime could fatten the standardized tail. The
  triggers below watch for that.

## Review and reversal (the design's own triggers, restated)

- **Any `flatten_deferral_outcome` with `touched: true`** → review. Rerun the tool, and
  raise k (config only, the safe direction) if the review agrees.
- **Recalibrate** on any of the design's §4.7 triggers: a non-event window above 5.35σ, a
  monthly refit outside its interval, or data grown by 50%.
- **What P11 makes visible from its deploy:** every flatten's z, P_touch and reason on
  `watchdog.triggered`. Read them against the fills (the audit's Q6 and Q14) after
  ≈ 10 sessions.
- **Rollback:** `flatten_defer_insurance_multiple = 10.0`, a config-only release.

## Amendment: recalibrated for v0.5.5 (2026-09-29)

**z₉₉ 2.38 → 2.60 and α 2.80 → 2.72.** On a 5-wide, k = 2's boundary moves from
z ≥ 3.05 to **z ≥ 3.35**. It is config only, in all three profiles, riding v0.5.5.

- **The rerun:** 30 non-event sessions through 2026-09-28. One FOMC day and five 8:30
  release days are excluded ([why the release days stay out](2026-09-29-the-flatten-calibration-excludes-release-days.md)).
  - z₉₉ is the envelope of the 5–30-minute p99s: 2.42, 2.41, 2.44, 2.51 and 2.60.
  - Hill α is 3.33, with a 90% day-bootstrap interval of [2.72, 4.74].
- **What moved it: one session, and mostly z₉₉.** Without 2026-09-28 the rerun returns
  exactly 2.38 / 2.80. With it:
  - **z₉₉ 2.38 → 2.60.** It is the 30-minute p99 alone that sets the envelope; the 5–20-minute
    ones are 2.41–2.51. 2026-09-28 had several 4σ 30-minute windows; the largest, 4.4 at
    12:06, is under the 5.35 trigger.
  - **α 2.80 → 2.72 is a small move.** The Hill estimate went 3.76 → 3.33, still inside the
    previous interval [2.80, 5.48]. The shipped α is the interval's lower bound by
    construction, and that bound moved 0.08.
  - **No trigger fired.** Trigger 3 asks whether the refit leaves its interval, and it did
    not.
- **Why adopt it: a rerun ships in either direction** (the rule below). That it is heavier
  is not the reason.
- The fit is still sensitive to single days at 30 sessions. Expect more movement until the
  data grows.
- **At the new boundary:**
  - The model's touch probability, 0.50%, is at or above the data: 0.45% of 17,000
    all-day windows, and 3 against 2.9 expected in non-overlapping ones.
  - It skips 20 of 41 replayed survivors, down from 23 at the old fit.
  - That is about −$0.3/day at the live fill rate and about −$0.8/day on the replay's
    fill model (`flatten_k.py` on the 2026-09-28 snapshot).
- **The first live flatten under the deferral, 2026-09-28, was at z = 4.42**, not the
  "≈ 4–5" estimated above. It is deferred at either calibration
  ([deferral record](2026-09-25-deferring-a-far-otm-flatten.md)).

### The adoption rule: a rerun ships in either direction (2026-09-29)

**Whenever `tools.flatten_calibration` is rerun, at a §4.7 trigger or not, its output ships
at the next config release, heavier or lighter.** A rerun is never set aside because of its
direction. The triggers are unchanged: they say when a rerun and a release are *required*,
not when a fit is *allowed* to ship.

- **Why not only when heavier.** That is a ratchet. Every noisy heavy draw would ship and
  no light one would, so the boundary drifts up with nothing pricing the drift.
- **The conservatism already has two stated places:** α is the bootstrap lower bound, and
  it is capped at 3. A one-sided adoption rule would be a third, unpriced one.
- **The 2026-09-28 recalibration above** was adopted with the words "heavier …, which is the
  conservative direction, so it is adopted with k". It was also the rerun §4.9 requires
  before lowering k, so it ships under this rule too.
- **A lighter fit needs no decision record.** k is the policy; z₉₉ and α are measurements.
  What does need one is lowering k (§4.9), or changing which sessions the tool calibrates on
  ([release days](2026-09-29-the-flatten-calibration-excludes-release-days.md)).
