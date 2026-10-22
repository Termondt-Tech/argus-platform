# The strike-touch stop, and the profit target off ("package B")

**Status: DECIDED 2026-09-26 (Richard); BUILT the same day for v0.5.2, DEPLOYED in v0.5.2 (Mira, 2026-09-26)** (config sha `8322ab49c926`; **no trade has run under it yet — the first live session is 2026-09-28**). It ships alongside [one open position per side](2026-09-26-one-position-per-side.md).
**Review 2026-09-27 ([findings](../reviews/2026-09-27-v0.5.2-mira.md)): one BLOCKING defect in
how this switch reaches the `blind` page; FIXED, TAGGED and DEPLOYED as v0.5.3 (Pherkad,
2026-09-27)**, `src/` + tests only, image-only, config sha unchanged at `8322ab49c926`. v0.5.2
as deployed paged nothing when the SPX feed alone went stale, because `blind` still counted
the switched-off credit trigger as watching; no session ran on v0.5.2 while that gap was live
(the first is 2026-09-28). See "The switch gates the FIRING only" below. **Also decided
2026-09-27:** pages and `/status` stop showing `credit_stop` while the trigger is off (see
Interactions), built and deployed the same day.
→ [v0.5.3 release notes](../releases/v0.5.3.md)
Evidence: [the exploration](../../RESULTS.md) §3–§4
and its `config_grid.py` / `h5_calibrated_stop.py`, plus the week of 2026-09-21
reconstructed below.

> **THIS AMENDS THE `credit_multiple` PROHIBITION.** CLAUDE.md's rule is that
> conclusions about the credit stop come only from realized-basis positions, and that
> a number measured in cert cannot size a change on its own. This record is where
> that rule is set aside for this change, by the owner, with the evidence and its
> limits stated and a reversal rule fixed in advance. The prohibition's warning, "one
> trending-then-reverting session is the single regime where a stop is maximally
> wrong", is exactly why the evidence below is 24 sessions, two folds and four models
> rather than a day.

## The decision

| Key (all three profiles) | Was | Now | Effect |
|---|---|---|---|
| `watchdog.credit_trigger_enabled` (new, REQUIRED) | — | `false` | The 2×-credit mark trigger no longer fires |
| `watchdog.underlying_buffer_points` | 5.0 | **0.0** | The emergency exit is spot reaching the short strike, held `sustained_s` |
| `watchdog.credit_multiple` | 2.0 (live had 3.0 in the working tree) | 3.0 | Inert while the trigger is off; the value a rollback returns to |
| `watchdog.profit_target_enabled` | true (as v0.5.2 was prepared) | **false** | Winners ride to the 15:30 flatten or expiry; their slot is not refilled |

**The switch gates the FIRING only.** `credit_stop` and the mark are still computed and
recorded, the profit target (same branch) is still evaluated, and `unknown` still
reports a missing mark (`tests/test_strike_touch_stop.py`).

**Correction (2026-09-27 review, finding 1).** This paragraph used to end "so `blind` and its
page are unchanged". The code was unchanged, but what it measured was not. `blind` was
`unknown ⊇ {credit, underlying, profit target}`, so with the credit trigger off a dead SPX
feed with fresh legs read DEGRADED: no page, and no trigger able to fire, because the
underlying backstop is the only stop left. **`blind` is now measured against the stops that
can fire** (`TriggerThresholds.acting_stops`: the underlying backstop, plus the credit
trigger only when it is on). Under this decision a dead SPX feed alone is blind, and pages.
With the credit trigger on, the test is the old one exactly.

## Why: the week that prompted it

Every live trade from 2026-09-17 to 09-25, rebuilt read-only from `order.fill`:

- **−$515 gross, all of it from eight 2×-credit stops (−$1,045).** Everything held,
  flattened or expired made money.
- **On the 09-21 rally the stop was right each time:** held to settlement, the five
  calls would have lost $2,750. The damage was re-entering the same side four times
  into the trend.
- **On 09-24 and 09-25 it was wrong:** three puts stopped out that expired as winners
  (−$565), one of them three minutes after a 10:00 entry.

The mark doubles on noise that reverts, and the strike is what decides the loss.

## Why: the evidence

| Check | Strike-touch vs the 2× stop |
|---|---|
| Replay: 24 sessions, each factor averaged over every other factor | **+$48.9/day [+21.6, +77.3]** on the fixed-IV mark, **+$71.1 [+41.3, +102.4]** on the IV-tracking one; 19/24 days |
| Two folds (cert-era days vs live days) | +$48.7 / +$49.6 and +$70.5 / +$72.6 |
| Exit slippage stressed to 0.10 and 0.20 | The gap *widens*, to +$58–81/day; interval clear of zero throughout |
| Four path models (5,000 paired days each) | M6 (measured) +$25.8; M1 +$5.7; M2 +$4.3; **M4 (momentum) −$9.2**. CVaR 1% is equal or better in all four |
| Per-idea, pre-registered (H5) | +0.019 of width per idea-day, p = 0.040, both folds. **Fails the pre-registered Holm correction** |

At the package-B caps (2 × 475 / 1050, one per side, profit target off), the replay has:

- **Nine touch stops in 24 sessions** (0.38 a day), each losing $176–$180 a lot
  including fees, worst −$203.
- Every other exit averaging +$47.
- Days averaging +$12.6 to +$13.6, worst −$157.5.

**The evidence grade, stated plainly:**

- It is the strongest this project has for any setting: consistent across folds,
  marks, slippage and three of four models.
- It is still **exploratory at the book level**: found after the fact, on one market
  regime (August–September 2026), with model marks for the exits.
- The momentum model is the one where it loses. Momentum is the opposite of the
  measured market (ρ30 −0.13), and exactly the regime a trending month would bring.

## Why the profit target is off

The 2026-09-22 decision established that a take beats holding *for one position*.
Measured at the book level with two slots:

- **It makes days more volatile, consistently:** day SD +25–140%.
- **Its benefit never holds up.** The sign flips between marks, between the 950 and
  1,050 caps, and between refilling the freed slot and leaving it empty (exploration
  §3). The take frees a slot, and the slot is refilled at whatever a fresh entry is
  worth under the stop in force.
- **Off in this release** keeps the first live week of the new stop and side rule
  attributable. On later is a config-only release, preferably with the side rule
  already proven, since it contains the take's variance.

## Reversal rule (fixed before the first live session)

**Review triggers.** A review, then a config-only rollback if the review agrees:

1. **Loss per stop.** The mean realized loss of the first five live strike-touch
   stops is worse than **−$250 a lot**. The replay has −$176 to −$180, worst −$203; the
   margin allows for real exit prices.
2. **Frequency.** Over the first ten qualifying sessions, strike-touch stops run above
   **one per session**. The replay has 0.38.
3. **Day tail.** Any single live day worse than **−$400** under package B. The replay's
   worst is −$158, and the loss budget caps the day at −$1,050.

**Rollback.** `credit_trigger_enabled = true` and `underlying_buffer_points = 5.0` (the
v0.5.1 stop, now at `credit_multiple` 3.0). **Never with a position open.**

**The forward test continues.** F2 in the exploration note §6 (strike-touch against the
2× stop, scored on the recorded candidate stream at 30 and 55 sessions) is unaffected by
this decision. It scores modelled exits, not Argus's own positions, and its result can
reverse the decision.

## Interactions

- **The flatten deferral's barrier moves to the strike.** It is short strike ±
  `underlying_buffer_points`. Five points against the 50–80 point distances the
  deferral acts at makes z slightly larger, so marginally more deferrals. Its
  calibration (z₉₉, α) is unchanged: it is fitted on excursions, not on the buffer.
- **The 10:00 window.** Its cost ran through the mark-triggered stop (exploration §5).
  `entry_earliest` stays 10:00; F1 measures the mechanism.
- **Pages and `/status` show no credit stop while the credit trigger is off**
  (Richard, 2026-09-27, the v0.5.2 review's Q4; this reverses the line that stood here,
  "Pages and `/status` still show `credit_stop`"). The level is the one a disabled
  trigger *would* use, nothing acts on it, and a page printing the mark beside it reads
  as "the mark against the stop". What a human sees:
  - **pages**: every `credit_stop` key is dropped and `"credit_trigger": "off"` is
    added, by `watchdog.page_view` in `PositionWatchdog._notify`, the one sink every
    watchdog page passes through (the journal line it writes too);
  - **the spent-exit journal line**: `cost_to_close=…, credit trigger off` instead of
    `… vs stop …`;
  - **`/status`**: the spent row reads `mark: X (credit trigger off)`, and its JSON
    carries `credit_stop: null` beside `credit_trigger_enabled: false`.

  **The audit records keep `credit_stop`**, as "still computed and recorded" above
  says. The page is still built from the record's dict, so the one-builder rule holds;
  it drops one inert number rather than stating a different fact. With the credit
  trigger on, every surface is unchanged. Tests: `tests/test_strike_touch_stop.py`
  (including an AST sweep that every watchdog page goes through `_notify`, or is named
  and pinned never to carry the key) and `tests/test_status.py`.

## The first live stop (2026-09-28)

**The position:** `20260928-put-7680-7675`, entered at 10:00 for a 0.60 credit.
Reproduced by [`first_live_stop_and_flatten.py`](../../RESULTS.md) §1.

| ET | Event |
|---|---|
| 10:45:08 | Spot touches 7680. It holds 4 s, then reverts: the debounce filtered it |
| 10:45:14 | It touches again |
| 10:45:19 | Fired after 5.0 s held, spot 7678.51. Mark 2.50, natural 2.60 |
| 10:45:20 | **Filled at 2.40, at the first rung, about 1 s after the trigger, 0.10 better than the mid** |

- **Realized: −$184.88 a lot with fees.** The replay had −$176 to −$180.
- **Review triggers, all clear:**
  1. the first five stops averaging worse than −$250: one so far, at −$185;
  2. more than one a session: one;
  3. any day worse than −$400: the day was −$138.32, with the call expiring for +$46.56.
- **The audit's P6 concern did not materialize.** It expected a stop at the strike to walk
  9–10 rungs over 40–230 s. This one filled at the first rung, inside the mid. n = 1.
- **Against holding:**
  - Spot fell to 7666.67 by 10:55, the spread near full width.
  - It spent about 57 minutes at or below the strike, the last at 15:56:35, and closed at
    7684.74, 4.7 points above the strike.
  - Held to settlement it made +$56.56, so the stop cost $241 on the day. But the held
    outcome was decided in the last four minutes.
  - It is one draw of the insurance trade the replays price
    ([due diligence](../../RESULTS.md) §2: about
    zero in mean against holding, with the tail cut). It is not evidence either way.
