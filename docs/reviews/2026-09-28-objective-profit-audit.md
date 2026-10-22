# Review (2026-09-28): the objective audit, or where the build leaves net P&L on the table

**Mode: REPORT.** Read-only on code and config. This file is the only change. Nothing is
tagged, deployed or reconfigured, and no finding below changes behaviour by itself (§5).
**Owner's focus:** the Objective in `CLAUDE.md` (PR #24): net P&L per trading day, judged
against day SD, the worst day and drawdown, with the mean-versus-tail exchange rate left
to Richard.

**Target.** `main` at `d8203b2`: the v0.5.3 image and `argus.live.toml` as committed (live
config sha `8322ab49c926` per `CLAUDE.md`). Behaviour claims are checked against code and
config at the lines cited, not against prose. Where they disagree, the disagreement is a
finding (P11, P13).

**What this review could not read.** No audit log exists in this environment (`dev/nas/`
is absent), and no droplet, database or broker was touched. **No number here was computed
from a live database in this pass.** Every number is from a committed document or from
code and config arithmetic, and each one is graded:

| Grade | Meaning |
|---|---|
| **MEASURED** | Read off a committed record of real fills, quotes or fees. The source is named |
| **MODELLED** | Output of a model or replay, or arithmetic on one. The model is named. **Research-note numbers are exploratory unless a decision record cites them**, and each is marked as one or the other |
| **UNKNOWN** | Not in the record. The query in §4 that settles it is named |

**Verification of §4.** All 16 queries were run against a synthetic audit log built on
`src/argus/db/migrations/0001_initial.sql` with payloads shaped from the writers cited in
§4. Each one parses and returns the arithmetic it claims. That proves shape, not results.
The harness is not committed.

**Tags** (exactly one per finding): **OPEN: ACTIONABLE** (a decision record or telemetry
change is warranted now) · **OPEN: MEASURE FIRST** (the §4 query decides whether anything
follows) · **DEFERRED** (a binding forward test already owns it) · **ALREADY CONSIDERED**
(a decision record priced it) · **NOT APPLICABLE** (evaluated and dismissed; reason given).

---

## 1. Summary

**The book, as built.** At 2 × 475 / 1050, one spread per side and the profit target off,
a typical day is two one-lot verticals sold within a minute of 10:00. The median second
entry is 30 s after the first (two-slot note §4.1, MODELLED on the recorded stream). They are
then held until a strike touch or the 15:30 flatten. Nothing else closes a position before
15:30, and `entry_latest` is 15:00. So on a day with no touch the slots, about $890 of
the $1,050 budget and the side rule are all committed from about 10:01 to the close. The
replay of this book (strike-touch decision, 24 sessions, both marks) has **+$12.6 to
+$13.6 a day**, day SD about $100 and worst day −$157.5. That is MODELLED and cited by a
decision. The replay does not model the flatten deferral, the live entry fills or the live
flatten fills (P2). Everything below is measured against that baseline.

**Unit economics per one-lot spread.**

| Line | Value | Grade, source |
|---|---|---|
| Credit | $45–70 (0.45–0.70 on the recorded live fills; the floor is 0.50 at the mid, 0.45 after one rung) | MEASURED, live fills (`tastytrade_activity_260925.csv`; post-stop note §A1) |
| Fees, closed intraday / held to expiry | ≈ $4.88 / ≈ $3.44 | MEASURED on the 2-lot schedule (ledger decision §2: $9.76 / $6.88 per 2-lot spread), halved. How the $0.04 transaction fee scales to 1 lot is UNKNOWN |
| Touch stop | −$176 to −$180, worst −$203 | MODELLED (strike-touch decision, replay) |
| Any other exit | +$47 average | MODELLED, same replay |
| Positions a day | ≈ 2.1 (0.38 touch stops + ≈ 1.7 others) | MODELLED: solved from the decision's own mean, stop rate and averages |

**The five largest items, ranked by estimated $/day.** P5's dollars are unknown, so it
ranks on its mechanism:

| # | Item | Estimated effect | Grade | Confidence |
|---|---|---|---|---|
| P1 | **The stop and the flatten, priced against holding.** At the book level, holding one put and one call to settlement made +$26.0/day (SD $184, worst −$399). Package B makes +$12.6–13.6 (SD ≈ $100, worst −$157.5). **As replayed, the rules cost ≈ $13/day of mean and cut day SD ≈ 46% and the worst day ≈ 60%.** The replay charges every 15:30 survivor at least a tick plus a close fee, but live paid nothing on 3 of 5 flattens. On that record the price falls to ≈ $6–9/day. The exchange rate has never been stated | ≈ −$6 to −$13/day for the tail cut | MODELLED (two replays, 23–24 sessions, different caps; flatten adjustment on n = 5) | low |
| P2 | **Friction is about half the pre-friction edge.** About $10/day of fees plus about $5/day of modelled exit cost, against about $13/day net. The replays mis-state it both ways: about $1.5–2.5/day optimistic on fees and entry fills, and about $4–7/day pessimistic on the flatten (P4) | ≈ $15/day friction; replay net likely ≈ $2–5/day below live | fees MEASURED, counts MODELLED | medium (share) / low (bias) |
| P3 | **The blackout calendar zeroes 25% of Q4-2026 sessions.** 12 of 63 are macro dates and 4 are discretionary. **10 of the 12 macro dates are 8:30 ET releases that print before the session opens.** The evidence for standing down is one FOMC day. At the replay's mean that is ≈ $2.5/day (macro only) to $3.3/day forgone, averaged over all sessions, or up to ≈ $5 at P2's adjusted mean. The sign is unmeasured | ≈ ±$2.5–5/day, sign UNKNOWN | count MEASURED, $ MODELLED | high (count) / low ($) |
| P4 | **The 15:30 flatten buys insurance at k = 10.** A filled flatten on a position with z in [2.33, 5.12) pays ≈ $6.44/lot against a calibrated expected loss of ≤ $0.70–1.76. At the replay's positions per day and the live fill rate (2 of 5) that is ≈ $3/day. **The deferral's decision is not recorded for a flatten it declines** (P11), so the population cannot be counted | ≈ −$3/day | MODELLED (n = 5 fill mix) | low |
| P5 | **The loss budget at two slots.** The broker estimate overstates W by every open position's mark (`loss_budget.py:437-441`). The 2026-09-21 record said to revisit this "before the cap is raised", and v0.5.2 raised the cap without doing so. The 1050 raise was argued on a $60–90 stop, but a touch stop costs ≈ $175. So after a touch stop the stopped side is typically not refilled while the other side is open, and the extra $100 of day-loss capacity mainly absorbs the overstatement | UNKNOWN $; the refused entries are opposite-side hedges worth ≈ +$5–8 each (MODELLED) | mechanism: code | high (mechanism) / low ($) |

**The largest single fact the record cannot yet answer:** whether the capital committed at
10:00 would earn more in the afternoon (P7). V1 in the VRP note (−0.380, p 0.019,
discovery only) says afternoon premium is richer. It is DEFERRED to its binding look at
30/60 sessions. **The largest measurement gap:** the strike ladder around the short is not
recorded (P9), so `target_short_delta`, `width` and `min_credit` can only be studied through
model marks.

---

## 2. Findings

| ID | Area | Mechanism | Estimated impact (grade) | Evidence | Rail / decision touched | Conf. | Status |
|---|---|---|---|---|---|---|---|
| P1 | Exits | Touch stop + 15:30 flatten vs holding one put + one call to settlement | ≈ −$6 to −$13/day mean for ≈ −$84/day SD and ≈ +$241 worst day (MODELLED) | two-slot note §4.4 (settle, P2 2×1); strike-touch decision "Why: the evidence"; exploration §4 H5 table; `watchdog.py:1011-1052` | strike-touch decision; Objective (λ unstated) | low | OPEN: ACTIONABLE |
| P2 | Friction | Fixed per-lot fees ≈ 9% of a 0.55 credit; ≈ 2.1 closes a day; exit slippage | ≈ $15/day ≈ 53% of the pre-friction edge; replay net likely ≈ $2–5/day below live (MODELLED on MEASURED fees and n = 5 flattens) | ledger decision §2; `replay.py:52`; exploration §7 H3; activity export 2026-09-25 | none (lens) | medium | OPEN: MEASURE FIRST |
| P3 | Availability | Whole-day `event_blackout` on FOMC/CPI/NFP/PCE + discretionary dates | 16 of 63 Q4 sessions (MEASURED); ≈ $2.5–5/day at the replay mean or adjusted mean, sign UNKNOWN | `blackout_dates.txt`; `strategy.py:173-176`; `argus.live.toml:1011-1036`; regime decision | event blackout (LIVE, not a non-negotiable) | low ($) | OPEN: MEASURE FIRST |
| P4 | Exits | Flatten fires unless k·P·W ≤ one tick with k = 10 and premium = tick only (no fee) | ≈ −$3/day (MODELLED) | deferral design §4.3, §4.10; v0.5.2 notes (five flattens); `argus.live.toml:919-929` | deferral decision (k is policy) | low | OPEN: MEASURE FIRST |
| P5 | Sizing | `W_broker = at_risk − daily_pnl` counts open marks; 1050 rationale based on the 2× stop | UNKNOWN $; refill after a touch stop needs realized ≤ ≈ $160 (MODELLED) | `loss_budget.py:425-448`; `snapshot.py:36`; loss-budget decision, As built, "Deviations from the design"; `argus.live.toml:134-146` | loss budget (non-negotiable, and not relaxed here); welded invariant | high/low | OPEN: ACTIONABLE |
| P6 | Exits | Touch-stop exit runs a debounce argued for option-mark prints and a ladder evidenced on the 0.95–1.95 credit-stop regime | UNKNOWN; worst ladder ≈ +$45–50/lot past mid before the cross (arithmetic) | `argus.live.toml:625-629, 641-646, 686-688, 935`; `watchdog.py:1831, 2197-2258`; `loop.py:546-607` | strike-touch decision review triggers | low | OPEN: MEASURE FIRST |
| P7 | Capacity | Slots, budget and side rule committed from ≈ 10:01 to 15:30 on a no-touch day | UNKNOWN (untooled replay: afternoon refills +$8–10 a trade, not evidence) | VRP note §1–§2; two-slot note §4.1; `orchestrator.py:277-312` | welded invariant; loss budget | — | DEFERRED |
| P8 | Entry pricing | Sandbox walk set on live money; one tick = $5/lot; markout never measured | ≤ $5/lot per concession; net UNKNOWN | `argus.live.toml:433-453`; exploration §7 H3; `loop.py:336-374` | walk_source (owed pass) | low | OPEN: MEASURE FIRST |
| P9 | Measurement | Only the chosen pair per side is recorded; at 475 the width is pinned to 5 by arithmetic, so delta is the one rail-compatible lever and it cannot be replayed | enabling | `strategy.py:346-383`; `signal/models.py:153-173`; two-slot note §3.2 | none (telemetry) | high | OPEN: ACTIONABLE |
| P10 | Entry selection | `min_credit` refusals in the mid-delta era have never been scored | UNKNOWN | `strategy.py:385-390`; post-stop note §A6 (feed era only) | none | — | OPEN: MEASURE FIRST |
| P11 | Measurement | A flatten the deferral declines records no z / P_touch; docstring says otherwise | blocks P4 | `watchdog.py:4289-4330, 5789-5795, 4666-4667` | deferral decision | high | OPEN: ACTIONABLE |
| P12 | Entry selection | `max_width` 10 is dead at 475; a ladder-gap candidate on an empty book displaces a qualifying 5-wide | ≈ $0 (MODELLED) | `strategy.py:317-319, 543-566`; `side_balance.py:103-129`; `gate.py:164-165` | none | medium | OPEN: MEASURE FIRST |
| P13 | Provenance | Six passages of provenance text in the live config describe values v0.5.2 moved or files that no longer exist | $0; misleads the owed passes | `argus.live.toml:152-159, 319, 377-386, 453, 935` | walk_source / trigger_source "must move with the values" | high | OPEN: ACTIONABLE |
| P14 | Entry selection | One spread per side forbids a second put when calls do not qualify | −$10 to −$19/day main effect, n.s. (MODELLED, decision-cited) | one-per-side decision "Why" | side-balance decision | — | ALREADY CONSIDERED |
| P15 | Entry timing | 10:00 release jolt; 10 of 15 live entries in 10:00–10:07 | UNKNOWN | exploration §5–§6 (F1) | — | — | DEFERRED |
| P16 | Exits | Profit target built, off | take ≈ +$21/day, refill gives it back (MODELLED) | exploration §3; strike-touch decision | package B | — | ALREADY CONSIDERED |
| P17 | Availability | Every refusal and stand-down path; live frequencies not in the record | UNKNOWN | table in §3.17 | kill switch, halts, staleness (non-negotiables) | — | OPEN: MEASURE FIRST |
| D1 | Sizing | `position_size_pct` 0.03 never binds: 3% × NLV ≥ 475 unless NLV < $15.8k | $0 | `argus.live.toml:161`; go-live findings C8 | size rails | high | NOT APPLICABLE |
| D2 | Friction | `max_contracts_per_position` 1 against fees: the schedule is linear per contract ($1.00 commission to open, $0.70 clearing + index fee), so lot size moves dollars, not friction share | $0 | ledger decision §2 | size rails | high | NOT APPLICABLE |
| D3 | Availability | `greeks_max_age_s` 120 no longer reaches selection: under `"mid"` the short is picked from quote-admitted legs, the feed pick is only the recorded counterfactual | $0 | `strategy.py:258, 266-270`; `live_chain.py:269-278` | — | high | NOT APPLICABLE |
| D4 | Entry policy | Cooldowns / same-side lockouts | −$6 / −$18/day (MODELLED, M6) | post-stop note §A4 | — | medium | NOT APPLICABLE |
| D5 | Sizing | Netted (condor) loss budget | raises the worst day | two-slot note §4.6 | loss budget | medium | NOT APPLICABLE |
| D6 | Capacity | Free a dead winner's slot without closing it | breaks W's definition; replay −$22/−$66 anyway | VRP note §1; loss-budget decision "The invariant" | loss budget | high | NOT APPLICABLE |
| D7 | Capacity | A third slot, or a larger day | scaling; welded invariant; G1 unproven | `CLAUDE.md` landmines | welded invariant | high | NOT APPLICABLE |
| D8 | Friction | One four-leg order for the pair | fees are per leg either way; merges two stops | two-slot note §7 | single-legging | high | NOT APPLICABLE |
| D9 | Entry pricing | Zero-bid floor lowers the recorded credit | ≤ 0.025, only on a zero long bid, rare at ≈ 0.10 delta intraday | `zerobid.py:55-67`; `strategy.py:345` | spec §7 | medium | NOT APPLICABLE |
| D10 | Entry pricing | Opening limit rounds half-up from the mid | favourable (step 0 at or above mid) | `zerobid.py:110-122`; `build.py:258` | — | high | NOT APPLICABLE |
| D11 | Sizing | A working opening order counts at zero credit in W | seconds; entries are serialized | `loss_budget.py:376-379, 429-430` | loss budget | high | NOT APPLICABLE |
| D12 | Risk | Breaker trips on a ≈ 5-min NLV cycle | risk-side, not P&L; budget uses the ledger | post-stop note §A2, §A8 #8 | breaker | — | NOT APPLICABLE |
| D13 | Execution | COA waits (1 s exit floor, 4 s entry hold) | settled by Cboe, not a lever | `argus.live.toml:91-111` | COA non-negotiable | high | NOT APPLICABLE |
| D14 | Signal | Arming `[regime]` | uncalibrated; landmine | regime decision | landmine | high | NOT APPLICABLE |
| D15 | Signal | Put-first tie-break at an empty book | the second side follows within seconds; no measured side edge | one-per-side decision; two-slot note §4.5 | side-balance decision | medium | NOT APPLICABLE |

---

## 3. Findings in detail

### 3.1 P1: the stop and the flatten, priced against holding: the exchange rate is unstated

**Mechanism.** Under package B the only acting stop is spot reaching the short strike, held
`sustained_s` (`watchdog.py:1011-1014` gates the credit trigger off, `:1042-1045` is the
touch). The flatten fires at 15:30 regardless (`:1051-1052`). Both convert an open
position into a realized one before settlement. On a 0DTE short vertical that sells the
reversion the market pays for: ρ30 is −0.132 [−0.232, −0.027] (post-stop note §A4,
MEASURED on 20 sessions).

**Evidence.** Each line is labelled with its source.

| Book (2 × 1 lot, one per side) | Mean $/day | Day SD | Worst day | Source |
|---|---|---|---|---|
| Held to settlement, no stop, no flatten | +26.0 | 183.9 | −399 | two-slot note §4.4, `settle` row, P2 policy, 23 sessions, day cap 950 (not binding when nothing is closed) |
| Package B: touch stop + flatten, 1050 | +12.6 to +13.6 | ≈ 100 | −157.5 | strike-touch decision (cites `config_grid.py`), one-per-side decision (SD 107 → 100), 24 sessions |
| Per idea, day-mean return on width | hold +0.0412 vs touch +0.0156 | 0.081 vs 0.051 | −0.186 vs −0.094 | exploration §4, H5 table, fixed-IV mark, **fees excluded** (`common_x.manage` charges none) |

- **The price as replayed, MODELLED:** ≈ −$13/day of mean, for ≈ −$84 of day SD (−46%)
  and ≈ +$241 on the worst day (−60%). Per idea, the gap is 0.0256 of width, ≈ $12.8 a lot
  before the $1.44 close fee that holding avoids.
- **The replay overcharges the flatten.** The package-B replay closes every 15:30 survivor
  at `max(mark + 0.025, 0.05)` plus a $1.30 fee (`config_grid.py`), which is ≥ $6.30 on
  ≈ 1.7 survivors a day. Live, 3 of 5 flattens paid nothing: 2 sent nothing and 1 went
  unfilled, and all 5 expired worthless (v0.5.2 notes). The deferral, not in the replay,
  withholds ≈ 13% more. On that record a survivor costs ≈ $2.2 in expectation, not ≥ $6.30,
  so package B's live mean is ≈ $4–7/day above its replay. **Adjusted, the rules' price
  against holding is ≈ $6–9/day** (MODELLED; the adjustment rests on five flattens).
- **Annualized Sharpe (mean/SD × √252):**
  - ≈ 2.24 held against ≈ 2.08 for package B as replayed. On that basis the rules behave
    like a position-size cut, not a source of edge, and Argus at minimum size cannot cut
    size any other way.
  - ≈ 2.7–3.2 for package B with the flatten adjustment. On that basis the rules
    improve the ratio.
  - **Which of the two holds turns on what the flatten costs on far-OTM survivors**
    (P4, P11).
- **A structural fact that is not in the record** (arithmetic, from the config, exact):
  - With no stop and the profit target off, nothing closes before 15:30, which is after
    `entry_latest`. So each side holds at most one position a day.
  - A put and a call cannot both finish in the money.
  - So the no-stop book's worst day at these caps is about one width less two credits:
    ≈ −(5.00 − 0.55 − 0.55) × 100 − fees ≈ **−$397**. The replay's −$399 is exactly that.
  - The touch-stop book's structural worst is not lower. Two touches cost ≈ −$360, and a
    refill after a double stop is admissible (W ≈ 360 leaves ≈ 690). A gap through the
    strike fills near width. Only the loss budget (1,050) bounds it.
  - **So the stop improves the typical bad day, not the structural worst day.**
- **Limits.** The two book-level rows differ in sample (23 vs 24 sessions), cap (950 vs
  1050) and call-side imputation (P2 imputes 6–12 entries). The settle row's mean is ≈ 0.7
  SE from zero (SD 184 over 23 days). Everything here is exploratory and from one regime.
  The strike-touch decision compared the touch stop against the 2× stop only. It never
  priced it against no stop.

**Impact.** The largest number in the record: half to all of the modelled net of the book.
It is a risk trade, not a defect. As replayed, the rules are worth their cost only if
Richard values **$1 of day SD above ≈ $0.15 of mean**, or **$1 of worst day above ≈ $0.05 of
mean**. With the flatten adjustment the thresholds are ≈ $0.07–0.11 and ≈ $0.025–0.04.
Neither rate has been stated, and the Objective requires one.

**What would settle it.**

1. An apples-to-apples replay. Add a `stop = "none"` level (flatten only) and a
   `hold` level (no flatten) to `config_grid.py`'s factorial. Score all three on the same
   24 sessions at package-B caps, both marks, fees on the measured schedule. **Model the
   flatten as it runs:** the deferral, and no order when the mid rounds to zero
   (`build.py:258-267`). Report mean, SD, 1% quantile, CVaR 1% and the worst day, paired by
   day. This is a research note, not a pre-registration: the effect is already found.
2. The live side comes from Q14 (§4): every position's realized net against the same
   position held to settlement.
3. F2 (exploration §6, looks at 30/55) continues unchanged.

**Next step.** A short decision record by Richard stating λ, as the rate at which he
trades mean for day SD and for the worst day. P1, P4 and P5 are all priced against it.
**This review does not recommend removing or loosening the stop.** The `credit_multiple`
prohibition's evidence standard (realized-basis live positions) still governs any change.

### 3.2 P2: friction is about half of the pre-friction edge, and the replays mis-state it both ways

**Mechanism.** The schedule is linear per contract and front-loaded: $1.00 commission to
open, $0.70 clearing and index fee each way, and closes free of commission (ledger
decision §2, MEASURED from confirmations). At one lot and a 0.55 credit, a round trip
costs ≈ 8.9% of the credit and an expiry ≈ 6.3%. The quoted half-spread is ≈ 10–12% of
the credit (VRP note D6, MEASURED on 7 sessions of bid/ask). Entries open at the
tick-rounded mid (`build.py:258`) and exits open at the mid (`watchdog.py:1831`).

**Evidence and impact (package-B replay, MODELLED):**

| Line | $/day | Basis |
|---|---|---|
| Net after friction | +13.1 | strike-touch decision (mid of +12.6 to +13.6) |
| Fees in the replay | 9.5 | ≈ 2.07 positions × $4.60 (`replay.py:52`: $3.30 open, $1.30 close; every position is closed at 15:30 in `config_grid.py`) |
| Exit slippage in the replay | 5.2 | 2.07 × 0.025 × 100 (`SLIP`) |
| **Pre-friction edge** | **≈ 27.8** | sum |
| **Friction share** | **≈ 53%** | |

**The replays mis-state friction, in both directions (MODELLED).**

| Replay assumption | Live record | Effect on replay net |
|---|---|---|
| Entries fill at the recorded mid | 9 of 15 opening walks filled at step 0, 5 conceded a tick ($5 a lot), 1 walked away (exploration §7 H3, MEASURED). On 2026-09-25, 2 of 3 fills conceded a tick (activity export, MEASURED). Step-0 fills sit up to half a tick above the mid, because the opening limit rounds half-up | optimistic by ≈ $0.5–1 a fill, ≈ $1–2/day |
| Fees $3.30 / $1.30 a lot | ≈ $3.44 / $1.44 | optimistic by ≈ $0.6/day |
| Every 15:30 survivor pays `max(mark + 0.025, 0.05)` + close fee | 3 of 5 live flattens paid nothing and all 5 expired worthless. The deferral withholds more | **pessimistic by ≈ $4–7/day** (P1, P4) |
| Stop exits pay mark + 0.025 | five 2×-credit stops filled −0.005 against the trigger mark (post-stop note §A1). Touch stops: no data (P6) | unknown for the touch stop |

**Net: replay net is likely ≈ $2–5/day below live**, and nearly all of that correction
rests on five flattens. Friction is still about half the pre-friction edge on either basis.

**What would settle it.**
- Q4, Q5 and Q6 give entry and exit slippage against mid and against natural.
- Q14 gives net against held.
- The ledger's broker fees give the fee line to the cent.

**Next step.** No decision. Two things follow:
- **Every future replay should use** the measured schedule ($3.44 / $1.44 a lot), the
  measured entry-concession mix and the flatten as it runs (the deferral, and no order on
  a zero mid), and say so.
- **The one rail-compatible lever on friction share is credit per lot.** At 475 the
  width is pinned at 5 (P9), so the only such lever is `target_short_delta`. It is
  unmeasured because the ladder is not recorded (P9).

### 3.3 P3: the blackout calendar zeroes a quarter of Q4 sessions, most for events that resolve before the open

**Mechanism.** On a listed date the strategy returns `EVENT_BLACKOUT` before any side is
priced (`strategy.py:173-176`), so the day carries zero expected P&L by construction. The
watchdog, exits and flatten are untouched.

**Evidence.**
- The Q4 calendar (`blackout_dates.txt`, MEASURED): 63 trading days from 2026-09-28 to
  12-24 (holidays per `argus.live.toml:968-998`).
  - **Macro: 12 dates (19%).** FOMC 10-28 and 12-09. CPI 10-14, 11-10, 12-10. NFP 10-02,
    11-06, 12-04. PCE 09-30, 10-29, 11-25, 12-23.
  - **Discretionary: 4 dates (6%)**: 10-15, 10-20, 11-27 (a half-day) and 12-24 (a half-day).
- **10 of the 12 macro dates are CPI, NFP or PCE, released at 8:30 ET.** The release prints
  70 minutes before `market.session_open` (09:40) and 90 before `entry_earliest` (10:00).
  By the first eligible tick, the 0DTE chain has repriced to the post-release state.
- The rationale is recorded as practitioner convention plus one day. 2026-07-29, an FOMC
  day with a 14:00 intraday event, was "the largest-magnitude loss day in the would-submit
  tally" (regime decision; `argus.live.toml:1015-1021`). **No measurement in the repo
  concerns an 8:30 release day.**
- The regime block (spot, `atm_iv`, minutes to settlement) **is** recorded on blackout
  days: `orchestrator.py:188-196` computes it before the strategy is consulted and
  `signal.none` carries it. So realized/implied on those days is measurable today.

**Impact.**
- Proportion: if blackout days carried the average day's edge, the calendar forgoes ≈ 19%
  (macro) to 25% (all) of expected P&L.
- In dollars at the replay mean (+$13.1): ≈ $2.5/day (macro only) to $3.3/day, averaged
  over all sessions. At P2's adjusted mean (+$17–20) it is ≈ $3.3–5/day (MODELLED).
- The sign is UNKNOWN. Post-release days may be good for premium (the event premium is
  gone) or bad (realized vol stays high).

**What would settle it.** Q15: realized/implied to the close, 10:00–15:00, on blackout days
split FOMC / 8:30 release / discretionary, against every other day. The cert chain has
carried live market data since 2026-07-20, so seven sessions already exist:
- six recorded blackouts (VRP note §2: 08-07, 08-12, 08-26, 09-04, 09-11, 09-16);
- 2026-07-29, an FOMC day recorded before the blackout existed. Q15 classifies FOMC dates
  from `fomc_dates.txt` so that it lands in the right group.

A binding version should be pre-registered in its own research note, with its own α,
before it is scored forward. Draft statistic:
- Per session, `½ ln(mean z²)` over [10:00, 15:00), z as in `vrp_common._z_atm`.
- Test the 8:30-release days against non-event days, two-sided, by permutation of session
  labels.
- One look at 20 release-day sessions (≈ 7 months of calendar), α 0.025.
- A confirmation licenses only a design record for release days. **FOMC days stay out of
  scope** (an intraday event).

**Next step.** Q15 now. It is descriptive, and nothing is licensed by it alone.

### 3.4 P4: the flatten buys insurance at k = 10, and the price has never been stated in dollars

**Mechanism.**
- The flatten is withheld only while k · P_touch(z) · W ≤ one tick (`argus.live.toml:919-929`).
- At k = 10 and W = 5 that means P_touch ≤ 10⁻³, i.e. z ≥ 5.12 (deferral design §4.3).
- The premium side counts the tick only, not the $1.44 close fee or the half-spread
  (design §4.3: "leaving them out is the conservative choice").
- A 0.15-delta short entered at 10:00 on an unchanged market sits at z ≈ 3.6 at 15:30.
  That is arithmetic at 13% vol, before `atm_iv`'s late-day upward bias, which z also
  carries, consistently with the calibration. So most unstopped positions are flattened.

**Evidence.**
- **Replay of the 15 recorded flattens (MEASURED inputs):** 13 flatten at k = 10 and 2 defer.
- **The five live flattens (MEASURED, v0.5.2 notes):** 2 filled at one tick, 2 sent nothing
  and 1 went unfilled. All five expired worthless.
- **k sensitivity** (design §4.10): k = 5 would also defer 2026-09-23; k = 20 defers nothing.
- **Calibrated expected loss** at the recorded z values (P_touch × W, an upper bound, since
  a touch triggers an exit costing far less than W): ≤ $1.76 at z = 3.33 (2026-09-22) and
  ≤ $0.70 at z = 4.55 (2026-09-23).

**Impact (MODELLED).**
- About 1.7 positions a day reach 15:30 (P2's replay arithmetic). About 13% defer, which
  leaves about 1.5 flattens a day.
- At the live fill rate (2 of 5), about 0.6 fills a day each pay ≈ $5 + $1.44.
- Against an expected loss of ≤ $0.7–1.8, **the net is ≈ −$3/day.**

That is Richard's k = 10 at work. The design stated k's meaning, not its cost. Under two
slots the population is twice the one-slot era's.

The replays charge every 15:30 survivor `max(mark + 0.025, 0.05)` plus a close fee
(`config_grid.py`). That is ≈ $10.7/day at 1.7 survivors, against ≈ $3.8/day paid on the
live record. So the flatten's live price is the smaller number, and the replay's +$13 is
≈ $4–7/day low on this line (P1, P2). **The fill rate is the load-bearing unknown.** Two of
five is the whole sample, and a far-OTM flatten whose mid rounds to zero sends nothing by
construction (`build.py:258-267`).

**What would settle it.**
- Q9 gives the deferral outcomes.
- Q6 and Q14, filtered to `flatten_before_close`, give each flatten's realized cost
  against settlement.
- The z of every flatten the deferral declined is **not recorded** (P11). It has to be
  rebuilt offline from `watchdog.triggered`'s spot and the tick's `atm_iv` until P11 is fixed.

**Next step.** After about ten sessions of Q14, append the measured $/flatten to the
deferral decision's recalibration section. **k is policy and unchanged.** Lowering it needs
the calibration rerun and a decision record (`argus.live.toml:920-924`).

### 3.5 P5: the loss budget at two slots overstates W in live, and 1050's stated reason no longer holds

**Mechanism, part 1: the broker estimate** (`loss_budget.py:425-448`).
- `W_broker = at_risk − daily_pnl`, and `daily_pnl` is NLV minus the day's anchor
  (`snapshot.py:36`), so it carries every open position at the broker's mark.
- The design formula is `−daily_pnl + Σ(width − cost_to_close)`. As built it uses a zero
  cost-to-close, which **overstates W by Σ mark × 100**.
- The loss-budget record accepted this as "moot at `max_concurrent_positions = 1` … worth
  revisiting (with a consistent mark) before the cap is raised" (loss-budget decision,
  As built, "Deviations from the design").
- v0.5.2 raised the cap to 2. No v0.5.2/v0.5.3 document, review or release note mentions
  `W_broker`. It was not revisited.
- Consequence, with one position open at credit c₁ and broker mark m₁, a second lot at
  credit c₂ fits iff:
  `m₁ ≤ c₁ + c₂ + 0.5 − fees/100` ≈ **c₁ + c₂ + 0.47 at 1050**, and ≈ c₁ + c₂ − 0.53 at 950.
  - At 950 the second slot would be refused whenever the first position is at or above
    its entry mark once the NLV has updated (a ≈ 5-min cycle, post-stop note §A2).
  - At 1050 it is refused once the first position is ≈ $100 underwater.
  - **Much of the 1050 raise is buying back headroom this overstatement consumed.**

**Mechanism, part 2: the stated reason for 1050.**
- `argus.live.toml:134-143` and v0.5.2 §A2 justify 1050 as "slack ≈ 150, about one
  ordinary stop (~60-90 a lot)". That figure is the 2×-credit stop, and package B switched
  it off in the same release.
- After a touch stop realizing R (ledger, before fees) with the other side open at c_open,
  a refill at c_new fits iff `R ≤ 50 + 100(c_open + c_new)` (from `compute_loss_budget`).
  That is ≈ $160 at 0.55 credits.
- The replay's touch stops cost ≈ $172–175 before fees (MODELLED). **So under package B a
  touched side is typically not refilled while the other side is open**, which is until
  15:30. At 950 it would never be.

**Impact.** UNKNOWN in dollars.
- Each refused entry is worth ≈ +0.010 to +0.016 of width (+$5–8) under the touch stop
  (exploration §3, MODELLED).
- **The refusals are correlated with the hedge.** A put under pressure in a sell-off is
  when the call wing's IV rises and the call qualifies, and that is exactly when
  `W_broker` refuses the call.
- The extra $100 of day-loss bound buys little under package B beyond absorbing the
  overstatement. The package-B replay ran at 1050 only, so 950 vs 1050 under the touch
  stop is not in the record. The exploration's §2 cap comparison used the 2× stop.

**What would settle it.**
- Q3: gate decisions refused on the budget, with `binding = "broker"`, where the ledger
  estimate would have admitted the lot.
- Q2's `daily_loss_budget_exceeded` alone gives the cliff (the exploration's D4).
- Rerun `budget_cliff.py` at 950/1050 under the touch stop.

**Next step.** A decision record for the revisit the 2026-09-21 record already owes: a
broker-side W with a **consistent** mark. The design warns that a watchdog mark from another
instant can understate W, so the fix must use the broker's own position marks from the
same account snapshot. Any unreadable input stays zero budget. **This corrects an
overstatement toward the rail's own definition; it does not loosen the rail.** Also correct
the 1050 comment (P13). Whether 1050 is still wanted once the estimate is corrected is
Richard's call, weighed against P1's λ.

### 3.6 P6: the touch-stop exit runs on parameters argued for a different regime

**Mechanism: the debounce.**
- `sustained_s = 5.0` (`argus.live.toml:625-629`). Its written reason (`trigger_source`,
  line 935) is quote artifacts on the option mark: "false-positive exits on one wide quote".
- Under package B the only acting stop reads the SPX index (`watchdog.py:1021-1045`), which
  has no bid/ask to print wide.
- The debounce now does two things:
  - it filters genuine touches that revert within 5 s, which saves wasted stops in a
    mean-reverting market;
  - it delays real ones at the point of maximum gamma.
- The replays modelled "held ≥ 5 s" on a 5 s grid, so their +$49–71 already embeds this
  value. The alternatives are unmeasured.

**Mechanism: the exit ladder.**
- The exit opens at the mid (`watchdog.py:1831`) and walks to +20% (`loop.py:546`).
- The ladder was held at 0.20 because "it governs ONLY the credit-stop regime": 23 exits
  opening at 0.95–1.95 (`argus.live.toml:641-646`).
- A touch stop opens near the ATM value of a 5-wide, ≈ 2.2–2.4. That is the replay's ≈ $175
  loss plus a 0.55 credit.
- So its floor is ≈ 2.70–2.90: **9–10 rungs**. Measured rung-to-rung time is 4–23 s
  (`argus.live.toml:686-688`), so ≈ 40–230 s of walking at the strike before the cross.
- The cross is `max(natural, last rung + a tick)`, capped at the width (`watchdog.py:2197-2258`).
- Worst ladder: ≈ +0.45–0.50 ($45–50/lot) past the trigger mid, before the cross.

**Impact.** UNKNOWN.
- The strike-touch decision's own review triggers bound the tail: the first five touch
  stops averaging worse than −$250 a lot, more than one a session, or any day worse than −$400.
- The functional-form fix, "an absolute tick/time budget capped at width", is already
  recorded as owed (`trigger_source`).

**What would settle it.**
- Q7 counts touches that reverted inside the debounce, per position, with their eventual
  outcome from Q14.
- Q8 gives touch stops as fired (hold time, spot versus strike, mark, natural).
- Q6 gives time from trigger to fill, rungs used, and paid-over-mid.

**Next step.** Read Q6–Q8 on the first five touch stops, which is the decision's own review
trigger 1, before any change to `sustained_s` or the exit walk. Either change is a decision
record under the `credit_multiple` prohibition's evidence standard.

### 3.7 P7: the capital is committed at 10:00 and the afternoon premium is not sold (DEFERRED)

**Mechanism.** With the profit target off, every position closes at a touch or at 15:30.
Both slots, ≈ 890 of 1,050 of budget and the side rule are therefore committed by the two
10:00 entries on any day without a touch (`orchestrator.py:277-312`, `gate.py:130-175`).

**Evidence, all DISCOVERY.**
- VRP note V1: −0.380 [−0.669, −0.092], p 0.019, 26 sessions. Afternoon realized/implied
  ≈ 0.60 against ≈ 0.83 midmorning.
- D5: ideas held to settlement returned −0.040 of width midmorning against +0.065 in the
  afternoon.
- The recycling replay (afternoon refills +$8–10 a trade, freeing a slot −$22/−$66 a day)
  is untooled, and the note itself says it is not evidence.

**Status: DEFERRED** to V1's binding looks: 30 sessions (≈ 2026-11-20, α 0.001) and 60
(≈ mid-January 2027, α 0.024). Power is 0.88 at the discovered effect and 0.20 at half of it.

**Next step.** None before V1. Q11 records the occupancy it would act on. Two ways of
freeing the slot are already settled against:
- closing a dead winner early (the profit target, off by package B, P16);
- not counting it (D6), which would break the loss budget's definition of W.

### 3.8 P8: entry pricing on live money, with no markout

**Mechanism.**
- The walk (`argus.live.toml:433-449`) is the 2026-08-10 sandbox set, "NOT tuned to live"
  (`walk_source`).
- It opens at the tick-rounded mid, holds 4 s, concedes one tick and walks away at 10%.
  That is one rung at 0.50–0.75 credit (`loop.py:546`, arithmetic).
- A walk-away retires the strike pair for the day. The selector skips it, so the other
  side or a different pair can go instead.

**Evidence.**
- 15 live opening walks: 9 filled at step 0 in 4.4–5.8 s, 5 conceded one tick, 1 walked
  away (exploration §7 H3, MEASURED).
- 2026-09-25: two 0.55 → 0.50 concessions and one walk-away (activity export, MEASURED).
- **What nobody has measured is the markout**, whether step-0 fills at mid are
  adversely selected. A fill in the auction window is at least consistent with a
  responder who knows more.

**Impact.** ≤ $5 a lot per concession, direct. Adverse selection is UNKNOWN.

**What would settle it.**
- Q4: the walk outcome mix, which is also `walk_source`'s required baseline.
- Q5: realized credit per idea.
- Q13: the watchdog's own mark 60 s and 300 s after each fill, against the entry credit.
- `entry_drift_abort_points` needs no fills. Its computation is fixed in `walk_source` and
  not yet run: the time-of-day-conditioned 20–30 s move distribution.

**Next step.**
- Run Q4 and Q5 from 2026-09-15 (the first live session with leg bid/ask on
  `signal.candidate`), and Q13 on package-B sessions.
- Run the owed drift computation. Its data has been admissible since 2026-07-20.
- No parameter change before the size-increase pass the config names.

### 3.9 P9: the strike ladder is not recorded, so delta, width and min_credit can only be modelled

**Mechanism.**
- `signal.candidate` records the chosen short and long per side (`signal/models.py:153-173`).
- `signal.none` records the near miss's pair (`strategy.py:346-383`). No other strike's
  quote is recorded.
- Every "what if 0.12 or 0.20 delta, or 10 wide" question therefore needs model marks. The
  two-slot note §3.2 found those track the mark (median error 0.043–0.051) but not the stop
  timing.

**Why it matters now.**
- At 475 per position a spread fits only if (w − c) × 100 ≤ 475, i.e. w ≤ 4.75 + c. The
  width is **pinned at 5** (P12).
- The credit per lot, P2's friction lever, can then move only through
  `target_short_delta`, a config value Richard owns. §A6's "edge per unit of risk peaks at
  0.12–0.15 delta" is an untooled smile-aware model.

**Next step.** A design record for telemetry only:
- a Tier 2 ladder snapshot, e.g. once a minute in the window, of each side's short
  ± 3 strikes (bid, ask, `mid_iv`, `mid_delta`);
- sized against the audit write-rate measurement in `argus.live.toml:285-311`;
- every numeric through `payload_number` (Tier 2 drops a non-finite row);
- nothing reads it.

### 3.10 P10: min_credit's refusals in the mid-delta era have never been scored

**Mechanism.** `credit < min_credit` rejects the side (`strategy.py:385-390`). Under
mid-implied delta, a thin call wing fails the floor: "fewer call entries in low-vol regimes"
(post-stop note §A6). The only scored evidence, "cheapest call ideas breached 42.9%", is
from the feed-delta era, where the floor selected estimation error. That reason is gone.

**What would settle it.** Q16 scores first-seen `credit_below_min` ideas (mid delta,
5-wide, 10:00–15:00) at settlement against admitted ideas, day-level. It is descriptive.
A threshold chosen from it would need its own pre-registration.

**Next step.** Q16. Nothing else.

### 3.11 P11: a flatten the deferral declines records no z

**Mechanism.**
- `_apply_flatten_deferral` returns the decision (`watchdog.py:4598-4627`).
- The decision is written to `watchdog.check` only on ticks where nothing fired
  (`:4289-4330`, key at `:4323`).
- When the flatten fires un-deferred, the tick goes to `_trigger`, whose `watchdog.triggered`
  payload has no decision in it (`:5789-5795`).
- `_note_deferral` writes only on deferral edges.
- So **z, P_touch, σ and the lhs are never recorded for the flattens the rule declined**:
  the 13-of-15 population that P4 prices. The docstring at `:4666-4667` ("its every tick
  is on `watchdog.check`/`watchdog.triggered*` already, carrying the decision") is false
  for that case.

**Next step.**
- Telemetry only: carry `deferral.to_payload()` on `watchdog.triggered` when a decision
  exists, through the same builder. Fix the docstring.
- A `src/` change with its own test, no config key.
- Until then, P4's z must be rebuilt offline.

### 3.12 P12: max_width is dead config at 475, and a ladder gap costs a tick

**Mechanism.**
- The long leg is the nearest strike in [5, 10] among quote-fresh legs (`strategy.py:317-319,
  543-566`; freshness at `live_chain.py:269`).
- A stale or gapped 5-point long therefore yields a 10-wide candidate. It always fails
  `position_max_loss_exceeded` at 475 (`gate.py:164-165`).
- The side ranking does not know that (`side_balance.py:103-129`). On an **empty book**,
  where both sides tie and the put goes first, a 10-wide put is proposed and refused, and
  a qualifying 5-wide call is not proposed on that tick.
- The `max_width` comment's coupling ("past ~13pt") is stale: at 475 it is past ≈ 5.25 pt
  (P13).

**Impact.** ≈ $0 (MODELLED). The cost is a delay of one or more 5 s ticks, only while the
book is empty.

**What would settle it.** Q10 counts `signal.candidate` rows wider than 5, on an empty
book, whose other side had a qualifying 5-wide.

**Next step.** If Q10 shows it at 10:00, `signal.max_width = 5.0` is the clean fix. The
loader allows `max_width == width` (`config.py:1468-1470`). A side whose 5-point long is
stale then returns `no_long_leg_within_max_width` instead of a guaranteed refusal. That is
a config-only release with a short decision record. It changes no risk, since nothing
wider than 5 can open at 475.

### 3.13 P13: the provenance text in the live config describes values v0.5.2 moved

Measured against `argus.live.toml` on `main`:

| Line | Says | Is |
|---|---|---|
| 152-159 (`max_concurrent_positions`) | "There is NO side rule … the side rule (not built)" | `side_balance = true` at 183 |
| 134-143 (`daily_max_loss`) | slack ≈ 150 is "about one ordinary stop (~60-90 a lot)" | a touch stop is ≈ $175 (P5) |
| 377-386 (`max_width`) | "past ~13pt … can never clear `max_loss_per_position`" | at 475 the bound is ≈ 5.25 pt |
| 453 (`walk_source`) | "`max_contracts_per_position` (2) … one tick is $10 per spread" | 1 contract; a tick is $5 |
| 935 (`trigger_source`) | argues `credit_multiple = 2.0` as the binding stop and `underlying_buffer_points = 5.0` as a liquidity backstop | 3.0 and inert; 0.0 and the only stop |
| 319 (`[audit]`) | re-measure with `tools.evaluability --config argus.toml` | `argus.toml` no longer exists (`CLAUDE.md`), so the command as written fails |

- The config's own rule is "Never change a value here without updating trigger_source in
  the same edit" (`argus.live.toml:536`). v0.5.2 moved two of the five values it governs
  (`credit_multiple`, `underlying_buffer_points`) and edited the string only for the cross
  correction.
- These strings are exactly what the owed data-backed passes will read first.

**Next step.** A text-only correction riding the next config release. It changes the sha,
so it is still a restart and never hot-reloaded.

### 3.14 P14: the side rule's cost (ALREADY CONSIDERED)

The one-per-side decision states it: −$10 to −$19/day as a main effect, not significant,
partly an artifact of imputing the call side. v0.5.2's `other_sides` records the call on
every tick, so from 2026-09-28 the replay can run imputation-free. Re-run `replay.py`'s P0/P2
contrast as sessions accrue, as the two-slot note's finding 4 already says.

### 3.15 P15: the 10:00 window (DEFERRED)

F1 (exploration §6) is binding at 30 sessions. One observation for whoever reads F1: the
10:00 cost ran through the mark-triggered stop (exploration §5). With the credit trigger off,
the jolt's excess of +0.65 implied sd is measured over 195 s (`h1_round_minute.py:40`).
That is ≈ 1.4 SPX points at 13% vol, against a short strike ≈ 24 points away at 10:00. So
the channel F1 would act on is largely shut under package B. `entry_earliest` stays.

### 3.16 P16: the profit target (ALREADY CONSIDERED)

Package B keeps it off. The take is worth ≈ +$21/day, and the refill gives it back
(exploration §3). The VRP note's recycling replay agrees that the second position is the
loss, not the close. No new evidence.

### 3.17 P17: every path that refuses or ends entries

Live frequencies are not in the committed record, apart from 2026-09-21's manual `/kill`
with a position open (206 `kill_switch_engaged` denials, post-stop note §A1). Q1, Q2, Q4
and Q12 count all of them.

| Path | Where | Scope | Live frequency |
|---|---|---|---|
| Outside the entry window / event blackout | `strategy.py:154-176` | tick / day | P3 |
| No tradeable legs, no mid delta, no long leg within max width, credit below min | `strategy.py:258-392` | tick | Q2 via `signal.none` reasons |
| No chain (`signal.skip`) | `orchestrator.py:170-177` | tick | Q1 |
| Kill switch (durable until `/resume`) | `gate.py:111-112` | until a human | Q12 |
| Breaker, concurrency, side rule, the three size rails, loss budget, `market_data_stale`, `account_stream_stale` | `gate.py:116-182` | tick | Q2 |
| Already attempted; stale at build (`signal_max_age_s` 1.0) | `entry.py:557-608` | idea | Q12 (`entry.*`) |
| Walk-away, drift abort, never worked, rejected | `loop.py` outcomes | idea | Q4 |
| Guard unreachable ×3 → entries ended for the day | `argus.live.toml:489-497`; `entry.py:1306` | day | Q12 |
| Login retries (6 × 10 min), reconcile retry (30 min) | `argus.live.toml:64-76, 1008-1009` | morning | Q12 |
| Watchdog exception → `HaltGrade.ENTRIES` (durable) | `2026-09-10-a-watchdog-exception-must-not-take-the-watchdog.md` | until a human | Q12 |

Two of these paths are durable: the kill switch and the ENTRIES halt. Either one left
engaged overnight costs every following session until `/resume`. That is the
non-negotiable working as designed, and its price is one mean day per halted session. Q12
shows whether any session has opened halted.

### 3.18 The parameter register (audit surface 8)

Not separate findings. Each row points to the finding that covers it.

| Key (`argus.live.toml` line) | Value | Provenance | Calibrated? | What would calibrate it | Finding |
|---|---|---|---|---|---|
| `signal.target_short_delta` (375) | 0.15 | hand, "shadow-mode tuning knob" | no | ladder telemetry + settlement scoring by delta | P9 |
| `signal.width` / `max_width` (376 / 387) | 5 / 10 | hand; 10 dead at 475 | no | ladder telemetry | P9, P12 |
| `signal.min_credit` (388) | 0.50 | hand | no | Q16 | P10 |
| `signal.entry_earliest` / `entry_latest` (389 / 390) | 10:00 / 15:00 | hand | no | F1; V1 | P15, P7 |
| `execution.entry_*` (433-449) | 4 s / 1 tick / 10% / 3.0 pt | 2026-08-10 sandbox set | no | Q4/Q5/Q13; drift computation owed | P8 |
| `execution.guard_timeout_s` (489) | 3.0 | "UNMEASURED" | no | Q12 (`entry.guard_unreachable_*`) | P17 |
| `watchdog.sustained_s` (629) | 5.0 | sandbox set; rationale is mark prints | no | Q7/Q8 | P6 |
| `watchdog.exit_walk_away_pct` / `exit_walk_min_steps` (660 / 718) | 0.20 / 3 | cert credit-stop and flatten regimes | no | Q6 on touch stops | P6, P4 |
| `watchdog.exit_cancel_settle_s` (725) | 3.0 | "unmeasured guess" | no | a submit-then-cancel measurement | none (operational) |
| `watchdog.flatten_before_close_s` (793) | 1800 | desk convention | no | Q14 by exit reason | P4 |
| `watchdog.flatten_defer_insurance_multiple` (924) | 10 | **policy** | n/a | λ | P4, P1 |
| `watchdog.flatten_defer_tail_z99` / `_alpha` (927 / 928) | 2.33 / 2.93 | `tools.flatten_calibration` | **yes** | rerun at its triggers | none |
| `watchdog.credit_trigger_enabled` / `underlying_buffer_points` (618 / 624) | false / 0.0 | package B decision | decided, F2 pending | F2 at 30/55 | P1 |
| `risk.daily_max_loss` (146) | 1050 | policy; rationale on the 2× stop | n/a | P5's rerun | P5 |
| `risk.max_concurrent_positions` / `max_loss_per_position` / `max_contracts_per_position` (160 / 170 / 171) | 2 / 475 / 1 | live minimum size, welded | n/a | scaling gate | D7 |
| `risk.position_size_pct` (161) | 0.03 | non-binding | n/a | — | D1 |
| `staleness.greeks_max_age_s` (219) | 120 | interim | moot under `"mid"` | — | D3 |
| `staleness.market_data_max_age_s` (189) | 2.0 | landmine | — | never raise for an exit problem | — |

---

## 4. Measurement plan

**How to run.** SQL with the sqlite3 shell, read-only:
`sqlite3 -readonly "file:dev/nas/<snapshot>.db?mode=ro" < q.sql`. Python from the repo
root: `python3 q.py dev/nas/<snapshot>.db`, saving the block to a scratch file first.
Every query reads `audit_log` only.

**Conventions.**
- ET dates in SQL use `date(ts, CASE WHEN ts >= '2026-11-01T06:00:00Z' THEN '-5 hours' ELSE
  '-4 hours' END)`, valid 2026-09-15 → 2027-03-13. The Python blocks use `America/New_York`.
- `argus-open-` is 11 characters and `argus-close-` is 12. `substr(…, 12)` and `substr(…, 13)`
  strip them to the `candidate_id`. That is `order.fill.candidate_id`'s fallback, since the
  field was null on 2026-09-21 (post-stop note §A8 #12).
- Counts of `gate.decision` are **ticks**, not episodes: a refused candidate is re-proposed
  every 5 s.

**Q1 (P3, P17): session inventory.**
```sql
WITH a AS (
  SELECT seq, ts, category, payload,
         date(ts, CASE WHEN ts >= '2026-11-01T06:00:00Z' THEN '-5 hours' ELSE '-4 hours' END) AS d
  FROM audit_log
  WHERE ts >= '2026-09-28T00:00:00Z'
    AND category IN ('session.event_blackout','signal.none','signal.candidate','gate.decision',
                     'order.fill','watchdog.exit_started','watchdog.flatten_deferred')
)
SELECT d,
  MAX(category = 'session.event_blackout')                                    AS blackout,
  SUM(category IN ('signal.none','signal.candidate'))                         AS signal_ticks,
  SUM(category = 'signal.candidate')                                          AS candidates,
  SUM(category = 'gate.decision' AND json_extract(payload,'$.allowed') = 1)   AS gate_allowed,
  SUM(category = 'gate.decision' AND json_extract(payload,'$.allowed') = 0)   AS gate_refused,
  SUM(category = 'order.fill'
      AND json_extract(payload,'$.external_identifier') LIKE 'argus-open-%')  AS open_leg_fills,
  SUM(category = 'order.fill'
      AND json_extract(payload,'$.external_identifier') LIKE 'argus-close-%') AS close_leg_fills,
  SUM(category = 'watchdog.exit_started' AND EXISTS (SELECT 1 FROM json_each(a.payload,'$.trigger_reasons')
      WHERE value = 'underlying_near_short_strike'))                          AS touch_exits,
  SUM(category = 'watchdog.exit_started' AND EXISTS (SELECT 1 FROM json_each(a.payload,'$.trigger_reasons')
      WHERE value = 'flatten_before_close'))                                  AS flatten_exits,
  SUM(category = 'watchdog.flatten_deferred')                                 AS deferrals
FROM a GROUP BY d ORDER BY d;
```

**Q2 (P5, P17): gate refusals by reason, and as the only reason.**
```sql
SELECT date(g.ts, CASE WHEN g.ts >= '2026-11-01T06:00:00Z' THEN '-5 hours' ELSE '-4 hours' END) AS d,
       r.value AS reason,
       COUNT(*) AS decisions,
       SUM(json_array_length(g.payload,'$.reasons') = 1) AS as_only_reason
FROM audit_log AS g, json_each(g.payload,'$.reasons') AS r
WHERE g.category = 'gate.decision' AND g.ts >= '2026-09-28T00:00:00Z'
GROUP BY d, reason ORDER BY d, decisions DESC;
```

**Q3 (P5): budget refusals bound by the broker estimate that the ledger estimate would have admitted.**
```sql
SELECT seq, ts,
  json_extract(payload,'$.proposal.symbol')                        AS spread,
  json_extract(payload,'$.proposal.max_loss')                      AS max_loss,
  json_extract(payload,'$.snapshot.loss_budget.remaining')         AS remaining,
  json_extract(payload,'$.snapshot.loss_budget.worst_case_ledger') AS w_ledger,
  json_extract(payload,'$.snapshot.loss_budget.worst_case_broker') AS w_broker,
  round(json_extract(payload,'$.snapshot.loss_budget.worst_case_broker')
      - json_extract(payload,'$.snapshot.loss_budget.worst_case_ledger'), 2) AS broker_minus_ledger,
  json_array_length(payload,'$.reasons') = 1                       AS budget_only_reason,
  json_extract(payload,'$.reasons')                                AS reasons
FROM audit_log
WHERE category = 'gate.decision' AND ts >= '2026-09-28T00:00:00Z'
  AND json_extract(payload,'$.snapshot.loss_budget.binding') = 'broker'
  AND EXISTS (SELECT 1 FROM json_each(payload,'$.reasons') WHERE value = 'daily_loss_budget_exceeded')
  AND json_extract(payload,'$.proposal.max_loss')
      <= json_extract(payload,'$.limits.daily_max_loss')
       - json_extract(payload,'$.snapshot.loss_budget.worst_case_ledger') + 0.005
ORDER BY seq;
```
`broker_minus_ledger` should approximate Σ mark × 100 + fees. If it does, P5's mechanism is
confirmed in live.

**Q4 (P2, P8): entry walk outcome mix, last limit against mid, mid against natural.** This
is `walk_source`'s required baseline.
```sql
SELECT json_extract(payload,'$.outcome') AS outcome,
       json_extract(payload,'$.steps')   AS steps,
       COUNT(*)                          AS walks,
       round(AVG(json_extract(payload,'$.final_limit_price') - json_extract(payload,'$.signal.credit')), 4)
                                         AS avg_last_limit_minus_mid,
       round(AVG(json_extract(payload,'$.signal.credit')
               - (json_extract(payload,'$.signal.short.bid') - json_extract(payload,'$.signal.long.ask'))), 4)
                                         AS avg_mid_minus_natural
FROM audit_log
WHERE category = 'entry.finished' AND ts >= '2026-09-15T00:00:00Z'
GROUP BY outcome, steps ORDER BY walks DESC;
```

**Q5 (P2, P8): realized opening credit per trade idea, from the fills.**
```sql
SELECT substr(json_extract(payload,'$.external_identifier'), 12) AS cid,
       SUM(ABS(json_extract(payload,'$.signed_delta'))) / 2       AS lots,
       round(SUM(-json_extract(payload,'$.signed_delta') * json_extract(payload,'$.price'))
             / (SUM(ABS(json_extract(payload,'$.signed_delta'))) / 2.0), 4) AS credit_per_lot,
       SUM(json_extract(payload,'$.price') IS NULL)              AS unpriced_rows
FROM audit_log
WHERE category = 'order.fill' AND ts >= '2026-09-15T00:00:00Z'
  AND json_extract(payload,'$.external_identifier') LIKE 'argus-open-%'
GROUP BY cid ORDER BY MIN(seq);
```

**Q6 (P2, P4, P6): each exit, from trigger to fill.**
```sql
WITH s AS (
  SELECT json_extract(payload,'$.position.candidate_id') AS cid, MIN(seq) AS seq, ts AS t_trigger,
         json_extract(payload,'$.trigger_reasons')       AS reasons,
         json_extract(payload,'$.position.entry_credit') AS entry_credit,
         -json_extract(payload,'$.order.limit_price')    AS opening_limit,
         json_extract(payload,'$.crossing_debit')        AS natural_at_trigger,
         json_extract(payload,'$.walk_away_price')       AS walk_away_price
  FROM audit_log WHERE category = 'watchdog.exit_started' AND ts >= '2026-09-28T00:00:00Z'
  GROUP BY cid),
x AS (
  SELECT json_extract(payload,'$.position.candidate_id') AS cid, MAX(seq) AS seq,
         json_extract(payload,'$.outcome')            AS outcome,
         json_extract(payload,'$.walk.steps')         AS steps,
         json_extract(payload,'$.cross_limit_price')  AS cross_limit
  FROM audit_log WHERE category = 'watchdog.exit_finished' AND ts >= '2026-09-28T00:00:00Z'
  GROUP BY cid),
f AS (
  SELECT substr(json_extract(payload,'$.external_identifier'), 13) AS cid, MAX(ts) AS t_fill,
         SUM(json_extract(payload,'$.signed_delta') * json_extract(payload,'$.price'))
           / (SUM(ABS(json_extract(payload,'$.signed_delta'))) / 2.0) AS debit_per_lot
  FROM audit_log WHERE category = 'order.fill' AND ts >= '2026-09-28T00:00:00Z'
    AND json_extract(payload,'$.external_identifier') LIKE 'argus-close-%'
  GROUP BY cid)
SELECT s.cid, s.t_trigger, f.t_fill,
       round((julianday(f.t_fill) - julianday(s.t_trigger)) * 86400, 1) AS trigger_to_fill_s,
       s.reasons, x.outcome, x.steps, s.entry_credit, s.opening_limit, s.natural_at_trigger,
       round(f.debit_per_lot, 4)                     AS debit_per_lot,
       round(f.debit_per_lot - s.opening_limit, 4)   AS paid_over_mid,
       round((s.entry_credit - f.debit_per_lot) * 100, 2) AS gross_per_lot
FROM s LEFT JOIN x USING (cid) LEFT JOIN f USING (cid) ORDER BY s.seq;
```

**Q7 (P6): touches the debounce filtered.** Positions with touch ticks but no Q8 row are
touches that reverted inside `sustained_s`.
```sql
SELECT json_extract(payload,'$.candidate_id') AS cid,
       date(ts, CASE WHEN ts >= '2026-11-01T06:00:00Z' THEN '-5 hours' ELSE '-4 hours' END) AS d,
       COUNT(*) AS touch_ticks,
       MAX(json_extract(payload,'$.sustaining.underlying_near_short_strike')) AS longest_unfired_hold_s
FROM audit_log
WHERE category = 'watchdog.check' AND ts >= '2026-09-28T00:00:00Z'
  AND EXISTS (SELECT 1 FROM json_each(payload,'$.true_reasons') WHERE value = 'underlying_near_short_strike')
GROUP BY cid, d ORDER BY d;
```

**Q8 (P6): touch stops as fired, against the strike-touch decision's review triggers.**
```sql
SELECT ts, json_extract(payload,'$.position.candidate_id') AS cid,
       json_extract(payload,'$.held_s.underlying_near_short_strike') AS held_s,
       json_extract(payload,'$.spot') AS spot,
       json_extract(payload,'$.cost_to_close') AS mark,
       json_extract(payload,'$.crossing_debit') AS natural,
       json_extract(payload,'$.position.entry_credit') AS entry_credit
FROM audit_log
WHERE category = 'watchdog.triggered' AND ts >= '2026-09-28T00:00:00Z'
  AND EXISTS (SELECT 1 FROM json_each(payload,'$.fired') WHERE value = 'underlying_near_short_strike')
ORDER BY seq;
```

**Q9 (P4): the deferral's outcome rows.** `touched: true` is a recalibration trigger.
```sql
SELECT ts, json_extract(payload,'$.candidate_id') AS cid, json_extract(payload,'$.ended') AS ended,
       json_extract(payload,'$.touched') AS touched, json_extract(payload,'$.first.z') AS z_first,
       json_extract(payload,'$.min_z_after_first_deferral') AS z_min,
       json_extract(payload,'$.emergency_triggers_after_first_deferral') AS fired_after
FROM audit_log WHERE category = 'watchdog.flatten_deferral_outcome' ORDER BY seq;
```

**Q10 (P12): ladder-gap candidates, and whether they displaced a 5-wide.**
```sql
SELECT date(ts, CASE WHEN ts >= '2026-11-01T06:00:00Z' THEN '-5 hours' ELSE '-4 hours' END) AS d,
       json_extract(payload,'$.side') AS side,
       COUNT(*) AS candidates,
       SUM(json_extract(payload,'$.book.puts') + json_extract(payload,'$.book.calls') = 0) AS on_empty_book,
       SUM(EXISTS (SELECT 1 FROM json_each(payload,'$.other_sides') AS v
                   WHERE json_extract(v.value,'$.qualified') = 1
                     AND json_extract(v.value,'$.width') <= 5.0001)) AS other_side_had_5_wide
FROM audit_log
WHERE category = 'signal.candidate' AND ts >= '2026-09-28T00:00:00Z'
  AND json_extract(payload,'$.width') > 5.0001
GROUP BY d, side ORDER BY d;
```

**Q11 (P7): slot occupancy.** How long each idea held its slot; NULL close means expiry.
```sql
WITH o AS (
  SELECT substr(json_extract(payload,'$.external_identifier'), 12) AS cid, MIN(ts) AS t_open
  FROM audit_log WHERE category = 'order.fill' AND ts >= '2026-09-28T00:00:00Z'
    AND json_extract(payload,'$.external_identifier') LIKE 'argus-open-%' GROUP BY cid),
c AS (
  SELECT substr(json_extract(payload,'$.external_identifier'), 13) AS cid, MAX(ts) AS t_close
  FROM audit_log WHERE category = 'order.fill' AND ts >= '2026-09-28T00:00:00Z'
    AND json_extract(payload,'$.external_identifier') LIKE 'argus-close-%' GROUP BY cid)
SELECT o.cid, o.t_open, c.t_close,
       round((julianday(c.t_close) - julianday(o.t_open)) * 1440, 1) AS minutes_held,
       c.t_close IS NULL AS held_to_expiry
FROM o LEFT JOIN c USING (cid) ORDER BY o.t_open;
```
`forward.py`'s D1–D4 (exploration §6) are the pre-registered descriptive versions.

**Q12 (P17): stand-downs, halts, and durable state.**
```sql
SELECT seq, ts, category, substr(payload, 1, 300) AS payload_head
FROM audit_log
WHERE ts >= '2026-09-15T00:00:00Z'
  AND category IN ('kill_switch.trip','kill_switch.resume','kill_switch.restored','session.entries_ended',
                   'entry.aborted_day','entry.guard_unreachable_abandoned','session.aborted',
                   'session.login_attempt_failed','session.login_gave_up','session.monitor_only',
                   'daily_loss.breach','daily_loss.budget_exhausted','watchdog.fatal')
ORDER BY seq;
```

**Q13 (P8): entry markout.** The watchdog's own mark 60 s and 300 s after each opening fill.
```sql
WITH e AS (
  SELECT substr(json_extract(payload,'$.external_identifier'), 12) AS cid, MAX(ts) AS t_fill
  FROM audit_log WHERE category = 'order.fill' AND ts >= '2026-09-28T00:00:00Z'
    AND json_extract(payload,'$.external_identifier') LIKE 'argus-open-%' GROUP BY cid)
SELECT e.cid, e.t_fill,
  (SELECT json_extract(w.payload,'$.entry_credit') FROM audit_log AS w
    WHERE w.category = 'watchdog.check' AND json_extract(w.payload,'$.candidate_id') = e.cid
    ORDER BY w.seq LIMIT 1) AS entry_credit,
  (SELECT json_extract(w.payload,'$.cost_to_close') FROM audit_log AS w
    WHERE w.category = 'watchdog.check' AND json_extract(w.payload,'$.candidate_id') = e.cid
      AND w.ts >= strftime('%Y-%m-%dT%H:%M:%S', e.t_fill, '+60 seconds')
    ORDER BY w.ts LIMIT 1) AS mark_60s,
  (SELECT json_extract(w.payload,'$.cost_to_close') FROM audit_log AS w
    WHERE w.category = 'watchdog.check' AND json_extract(w.payload,'$.candidate_id') = e.cid
      AND w.ts >= strftime('%Y-%m-%dT%H:%M:%S', e.t_fill, '+300 seconds')
    ORDER BY w.ts LIMIT 1) AS mark_300s
FROM e ORDER BY e.t_fill;
```

**Q14 (P1, P2, P4, P6): every position's realized net against the same position held to
settlement.** Settlement is proxied by the day's last recorded spot before 16:00, as
`tools.ledger` does. An ITM expiry should be checked against `/etc/argus/settlements.csv`.
```python
import json, sqlite3, statistics, sys
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DB, SINCE = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "2026-09-28T00:00:00Z")
OPEN_FEE, CLOSE_FEE = 3.44, 1.44   # $/1-lot spread, from the 2-lot schedule (ledger decision §2)
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
et = lambda ts: datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)

pos = {}
for ts, pl in db.execute("SELECT ts, payload FROM audit_log WHERE category = 'order.fill' "
                         "AND ts >= ? ORDER BY seq", (SINCE,)):
    p = json.loads(pl); xid = p.get("external_identifier") or ""
    for kind, prefix, sign in (("open", "argus-open-", -1), ("close", "argus-close-", 1)):
        if xid.startswith(prefix) and p.get("price") is not None:
            e = pos.setdefault(xid[len(prefix):], {"open": [0.0, 0], "close": [0.0, 0], "t": None})
            e[kind][0] += sign * p["signed_delta"] * p["price"]; e[kind][1] += abs(p["signed_delta"])
            if kind == "open" and e["t"] is None: e["t"] = et(ts)
last = {}
for ts, pl in db.execute("SELECT ts, payload FROM audit_log WHERE category IN "
                         "('signal.none', 'signal.candidate') AND ts >= ? ORDER BY seq", (SINCE,)):
    t, s = et(ts), json.loads(pl).get("underlying_price")
    if isinstance(s, (int, float)) and t.hour < 16: last[t.date().isoformat()] = s
why = {}
for (pl,) in db.execute("SELECT payload FROM audit_log WHERE category = 'watchdog.exit_started' "
                        "AND ts >= ? ORDER BY seq", (SINCE,)):
    p = json.loads(pl); why.setdefault(p["position"]["candidate_id"], "+".join(sorted(p["trigger_reasons"])))
rows = []
for cid, e in pos.items():
    d, side, ks, kl = cid.split("-"); ks, kl, lots = float(ks), float(kl), e["open"][1] / 2
    if lots == 0: continue
    credit, s = e["open"][0] / lots, last.get(f"{d[:4]}-{d[4:6]}-{d[6:]}")
    liab = None if s is None else min(max((s - ks) if side == "call" else (ks - s), 0.0), abs(kl - ks))
    closed = e["close"][1] > 0
    debit = e["close"][0] / (e["close"][1] / 2) if closed else liab
    real = None if debit is None else ((credit - debit) * 100 - OPEN_FEE - CLOSE_FEE * closed) * lots
    held = None if liab is None else ((credit - liab) * 100 - OPEN_FEE) * lots
    hm = e["t"].strftime("%H:%M")
    bucket = "10:00-10:05" if hm < "10:05" else "10:05-12:00" if hm < "12:00" else "12:00-15:00"
    rows.append((cid, hm, bucket, why.get(cid, "expired"), credit, debit, real, held)); print(*rows[-1], sep=",")
for label, col in (("exit reason", 3), ("entry bucket", 2)):
    print(f"\nby {label}: n, mean realized, mean if held, mean (realized - held)")
    for key in sorted({r[col] for r in rows}):
        xs = [r for r in rows if r[col] == key and r[6] is not None and r[7] is not None]
        if xs: print(key, len(xs), *(round(statistics.mean(v), 2) for v in
                     ([r[6] for r in xs], [r[7] for r in xs], [r[6] - r[7] for r in xs])))
```

**Q15 (P3): realized/implied to the close, 10:00–15:00, on blackout days against every
other day.** It reuses the frozen VRP reader by import and never edits it. Pass both
snapshots (the cert chain since 2026-07-20, then live).
```python
import math, statistics, sys
sys.path.insert(0, "docs/research/2026-09-28-vrp-term-structure-queries")
import vrp_common as v

dates = lambda p: {x.strip() for x in open(p) if x.strip() and not x.startswith("#")}
FOMC, DISC = dates("fomc_dates.txt"), dates("discretionary_blackout_dates.txt")
out = {}
for d, rs in sorted(v.load_sessions(sys.argv[1:]).items()):
    if rs[0]["t"] > 10 * 3600 or rs[-1]["t"] < 15 * 3600 + 55 * 60: continue   # full sessions only
    zs = v._grid(rs, (10 * 3600, 15 * 3600), v._z_atm(rs[-1]["S"]))
    if len(zs) < 150: continue
    kind = "fomc" if d in FOMC else ("discretionary" if d in DISC else "release_0830") \
        if v.is_event_day(rs) else "other"         # FOMC by date: 07-29 predates the blackout
    out.setdefault(kind, []).append((d, math.sqrt(statistics.mean(z * z for z in zs))))
for kind, xs in sorted(out.items()):
    vals = [x for _, x in xs]
    print(kind, len(vals), "mean", round(statistics.mean(vals), 3), "median",
          round(statistics.median(vals), 3), [d for d, _ in xs])
```

**Q16 (P10): min_credit's refusals held to settlement, against the ideas it admitted.**
This is the mid-delta era (from 2026-09-22). The unit is the first-seen idea per day, and
the result is reported per day.
```python
import json, sqlite3, statistics, sys
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
seen, ideas, last = set(), [], {}
pick = lambda o: {"side": o["side"], "short_strike": o["short"]["strike"], "long_strike": o["long"]["strike"],
                  "credit": o["credit"], "delta_source": o.get("delta_source")}
for ts, cat, pl in db.execute("SELECT ts, category, payload FROM audit_log WHERE category IN "
                              "('signal.none', 'signal.candidate') AND ts >= '2026-09-22T00:00:00Z' ORDER BY seq"):
    t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET); day, p = t.date().isoformat(), json.loads(pl)
    if isinstance(p.get("underlying_price"), (int, float)) and t.hour < 16: last[day] = p["underlying_price"]
    if not 10 <= t.hour < 15: continue
    if cat == "signal.none":
        found = [(r, "refused") for r in (p.get("per_side") or {}).values() if r.get("reason") == "credit_below_min"]
    else:
        found = [(pick(p), "admitted")]
        for o in (p.get("other_sides") or {}).values():
            if o.get("qualified"): found.append((pick(o), "admitted"))
            elif o.get("reason") == "credit_below_min": found.append((o, "refused"))
    for r, verdict in found:
        key = (day, r["side"], r["short_strike"], r["long_strike"])
        if r.get("delta_source") == "mid" and key not in seen:
            seen.add(key); ideas.append((day, r["side"], float(r["short_strike"]), float(r["long_strike"]),
                                         float(r["credit"]), verdict))
per_day = {}
for day, side, ks, kl, cr, verdict in ideas:
    s, w = last.get(day), abs(kl - ks)
    if s is None or w > 5.0001: continue
    liab = min(max((s - ks) if side == "call" else (ks - s), 0.0), w)
    per_day.setdefault((side, verdict), {}).setdefault(day, []).append((cr - liab) / w)
for key, days in sorted(per_day.items()):
    means = [statistics.mean(x) for x in days.values()]
    print(key, "days", len(means), "ideas", sum(map(len, days.values())),
          "day-mean return on width", round(statistics.mean(means), 4))
```

**Order of work.** Q12, Q1 and Q2 first: they are cheap and show whether any session was
lost to a stand-down. Then Q3, which confirms or refutes P5 in live. Then Q14 after about ten
package-B sessions, since it is the one query that prices P1, P2 and P4 on live money.
Q15 and Q16 can run today on the existing snapshots.

---

## 5. What this review does NOT license

- **No config or `src/` change follows from it directly.** Each actionable finding needs
  its own decision record: P1 (λ), P5 (the consistent mark), P9 and P11 (telemetry), P12
  (`max_width`), P13 (text).
- **No change to the stop, the flatten or k.** P1 and P4 state prices. The
  `credit_multiple` prohibition's evidence standard still governs the stop, F2 is still
  its forward test, and k is Richard's policy with a stated procedure for lowering it.
- **No change to the blackout calendar.** P3 counts sessions and proposes a measurement.
  An 8:30 release day being "resolved before the open" is a mechanism, not evidence that
  trading it pays. FOMC days are out of scope.
- **No relaxation of the loss budget, and no scaling.** P5's fix restores the budget's own
  design formula with a consistent mark, and every unreadable input stays zero budget. The
  welded invariant, the size rails and `daily_max_loss` are untouched. G1 is still unproven.
- **No afternoon allocation, profit target or `entry_latest` change** before V1's looks (P7).
- **Not evidence of an edge.** Every book-level number here is exploratory, from one regime
  (August–September 2026), on 23–24 sessions, with model marks for exits. The package-B
  mean has a standard error of about $20 a day.
- **Nothing here is normative.** A review is not a decision log. This file changes nothing
  until a record in `docs/decisions/` cites it.
