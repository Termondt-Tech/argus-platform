# A flatten that crossed below its own ladder (2026-09-24)

> **RESOLVED 2026-09-25 — BUILT, DEPLOYED in v0.5.2 (Mira, 2026-09-26)** →
> [deferring a far-OTM flatten](2026-09-25-deferring-a-far-otm-flatten.md). The §5 query
> was read the same day. The hold was the ZERO-MID refusal, not staleness. `crossing_debit` at
> `exit_started` was 0.10, and rungs at and above it went unfilled because the long leg had
> no bid. The resolution:
>
> - Finding 1: the cross pays past the ladder only where a leg-in exists.
> - Finding 2: fixed, with the reader rule in §3 below.
> - Finding 3: closed by measurement.
> - This day's flatten would have been deferred altogether, since the flatten is now conditional.
>
> The text below is the record as raised.

> **OPEN — THREE FINDINGS, NOTHING BUILT, NOTHING DECIDED.** Raised by Richard from the
> 2026-09-24 live session's 15:30 flatten of `20260924-put-7625-7620`, with the
> tastytrade Activity export (`tastytrade_activity_260924.csv`), the Slack pages and a
> 15:47:50 ET `/status` snapshot. The audit rows for the 15:30–15:38 window have
> **not** been read yet (§5 has the query). Finding 2 is a reporting defect with an
> obvious fix. Finding 1 changes exit pricing and needs a design sign-off. Finding 3
> needs a vendor-docs check before any design. The fourth question the day raises,
> whether the flatten should fire on a spread this far out of the money, is **already
> an open decision** (*Should `FLATTEN_BEFORE_CLOSE` be CONDITIONAL*, in
> [the index](README.md#open-decisions)). This record adds a data point to it and does
> not reopen it.

## 1. What happened

One contract of the 7625/7620 put vertical, opened 11:14:32 ET at a 0.50 realized
credit, seven seconds after the 7645/7640 ×2 credit stop filled at a 1.10 debit. The
underlying then rallied, and by 15:30 the short strike was about 80 points out of the
money. **Times are ET. The Activity export is in Central, one hour behind.**

| ET | Event | Source |
|---|---|---|
| 15:30:00 | `FLATTEN_BEFORE_CLOSE` fires | `seconds_to_flatten` = −1070.0 at 15:47:50 |
| 15:30:00 → 15:37:56 | **No order for ~8 minutes** | cause not yet split, see finding 3 |
| 15:37:56 | rung 0, **0.05 debit** (#509260274) | Activity export |
| 15:37:59 | rung 1, **0.10** (#509260291), cancelled within ~1 s | Activity export |
| 15:38:00 | rung 2, **0.15** (#509260314) | Activity export |
| 15:38:03 | rung 3, **0.20** (#509260332), cancelled; the walk's floor | Activity export |
| 15:38:03 | cross, **0.10 debit** (#509260365), still **Working** | Activity export, `watchdog_crossed_market` |
| 15:38:34 | `watchdog_exit_spent_still_held`, `exit_outcome: crossed`, `cost_to_close: null` | Slack |

At 15:47:50 `/status` showed the position spent (`outcome: crossed`), impaired
(`credit_multiple`, `profit_target` unknown), the flatten still true, spot 7706.1, and
both leg quotes "stale" (`7625P` 15.01 s, `7620P` 24.01 s, against 2 s), with the ages
rising exactly one second per second. By Richard's account the spread's mid at the
broker was 0.00, and spot was 7710 soon after.

Nothing needs a human. The working 0.10 close either fills ($10) or dies at the
close as a Day order, and the spread settles worthless. Sending a second close by hand
would have been the double-close C5's SOP warns against.

## 2. Finding 1: the cross was less aggressive than the ladder it escalated from

The ladder walked 0.05 → 0.20 and got no fill. The market-crossing escalation then bid
**0.10**. The escalation's premise is that it prices past where the walk gave up. Here
it priced **two ticks below** the walk's own floor, at a level the walk had already
bid (rung 1) and failed to fill at.

**Mechanism.** `ExitExecutor._cross_limit` (`execution/watchdog.py`) prices the cross
from `SpreadMarket.crossing_debit` alone (short ask − long bid), capped at the width.
Nothing floors it at the rung the walk reached. That was harmless while the ladder
ended at or below the natural price. **It stopped being harmless on 2026-09-17 (v0.4.3),
when `exit_walk_min_steps` went 1 → 3.** From the flatten's usual 0.05 opening, the
floor went from 0.10 to 0.20, which is above the ~0.10 natural price of a spread whose
short leg is offered at 0.10 and whose long leg is bid at zero. Nothing re-checked how
the ladder's end relates to the cross.

**This contradicts two premises of the 2026-09-17 decision.** Neither was wrong on the
data it had:

- *"a limit walking up the grid cannot skip a resting offer"* holds for a resting
  **complex** offer. The natural price is derived from **leg** quotes. A complex order
  at 0.10, 0.15 and 0.20 did not trade against a 0.10 leg-derived natural, so that
  natural was not an executable complex price. Either the class does not leg into the
  simple book, or no market maker answered the auction. The per-order history for
  #509260274–#509260332 is where to find out which. The export does not say.
- *"3 buys tail coverage past the one-tick deficit seen on all 3 `watchdog.crossed`
  rows"* assumed the cross sits **above** the ladder's end. At 3 steps it sits below it
  in exactly the regime the change was made for.

**It is the flatten's usual case, not an edge case.** Every flatten opening at 0.05
now ends its ladder at 0.20. So whenever the ladder fails, the cross re-bids something
at or under a price the market just refused at double.

**Options (not decided):**

| | Cross limit | Cost | Note |
|---|---|---|---|
| A | `max(natural, abs(floor) + 1 tick)`, capped at width | pays past the floor when the natural reads below it | **Recommended.** Keeps the escalation monotone. The width cap still holds. In the credit-stop regime it binds only when the natural reads *below* a floor the walk just failed at. That happens only if the natural is not executable or the market moved in our favour within ~3 s, and A overpays by at most `floor − natural` in the second case |
| B | skip the cross when natural ≤ abs(floor); report `CROSS_UNPRICEABLE` (retryable) | spends a send from `_EXIT_SEND_ATTEMPTS` | the retry re-walks the same ladder from a new mid, likely with the same result |
| C | `abs(floor) + k` ticks, ignoring the natural | a new constant | throws away the one input that re-reads the market |

Any option is a `src/` change to exit pricing and needs a test that pins
`abs(cross) > abs(floor)` for every walk that ended `WALKED_AWAY`. Whether it needs a
config key depends on the option (A needs none).

## 3. Finding 2: `walked_away_at` reports the opening limit, not where the walk stopped

`ExitExecutor._cross` records and pages `"walked_away_at": float(order.limit_price)`, where
`order` is the order **handed to** the walk, i.e. its opening limit. Today's page said
`walked_away_at: -0.05` for a walk that ended at −0.20. The Tier 1 `watchdog.crossed`
row carries the same wrong number. It is the 2026-09-10 class again (`_reprice` moves
`state.sent`, so a number read off the wrong object describes the wrong rung), this
time at the escalation.

**The page hid finding 1.** It read as a normal escalation, 0.05 → 0.10.

**It also affects past analysis.** The 2026-09-17 record's Finding 4 table labels
`walked_away_at` as **floor**, and its reproduction recipe differences
`crossing_limit` against `walked_away_at` for the deficit. That table was correct **by
coincidence**: all three rows were 0-step walks, opened before `exit_walk_min_steps`
existed, so opening = floor. **Applied to any row with `steps > 0`, the recipe gives the
wrong answer.** Today it would report a *one-tick deficit* (0.05 → 0.10) for a cross
that was actually two ticks *below* the floor. **Reader rule, since the 2026-09-25 build:
a `watchdog.crossed` row carrying `opening_limit` has the corrected `walked_away_at`. A row
without it carries the opening limit under that name, and is right only where
`exit_finished.walk.steps = 0`.** Of the four rows that exist, only this day's is wrong.
For rows from before the fix, compute the deficit
from `watchdog.exit_started.walk_away_price` (from `PriceWalk` itself, width-capped),
joined on `external_identifier`. Don't use `watchdog.crossed.walked_away_at`.

**Fix (small, `src/` only, no config key, so the image deploys alone):** take
`walked_away_at` from `walk.final_limit_price` (falling back to `order.limit_price` only
when that is `None`), and add `opening_limit` beside it so no reader loses the old
number. Pin it with a test that a multi-rung walk-away pages its last rung. Rows already
written stay as they are. The table in §5 of this record says which ones are affected.

## 4. Finding 3: a quiet quote is read as a stale one

The watchdog judges a leg by `MarketDataPort.age(symbol)`, the time since the last
event **arrived** for that symbol (`LegQuotes.resolve`), against
`staleness.market_data_max_age_s = 2.0`. DXLink sends a quote event **only on change**.
Nobody re-quotes a far-OTM 0DTE strike late in the day, so its 0.05/0.10 market sits
unchanged. **The quote is current, and it reads as stale.** Richard has seen this
pattern repeatedly. The resolver's own comment describes it (a wing "goes stale against
a 2s bound constantly (14% of ticks by the end of 2026-08-13)") but treats it as a cost
to absorb, not a false reading.

**The flatten is where this hits hardest.** A winning spread at 15:30 is, almost by
definition, a pair of strikes the market has stopped re-quoting. It also coincides with
the zero-mid refusal (`UnpriceableSpreadError`, which held a live flatten for 16 min on
2026-09-22). Both make a nearly worthless winner unpriceable, for different reasons.
**Which one held today's flatten for ~8 minutes is not yet known.** `exit_unpriceable`
(35 episodes, 303 repeats) and `exit_unattempted` (1, 61) are whole-session counts;
§5's query splits them. The walk *did* open at a 0.05 mid at 15:37:56, so zero-mid was
not the reason at that moment.

**What must not happen:**

- **Never raise `market_data_max_age_s`.** It governs entry pricing and the gate's
  staleness rail. That rule stands (2026-08-13, 2026-08-31).
- **Never admit a quote because it is quiet.** "Didn't change" and "this symbol's
  subscription died" look identical from the stream. That is exactly A1's case (a wing
  that quoted once and went quiet priced entries all day). **Feed-wide liveness does not
  settle it**: `market_data.age_s` 0.92 with 0 reconnects proves the connection, not
  this subscription.
- **Don't add an exit-only longer timeout.** It is a guess wearing a bound's clothes.

**Direction (not designed): ask, don't infer.** When an exit-path leg is quiet, get a
fresh answer about that one symbol, from a DXLink re-subscribe or a REST quote read. A
fresh reply, even an unchanged 0.05/0.10, is a *current* quote obtained on demand. That
is evidence, the same shape as `held_but_unbooked`'s REST confirm. Scope: the watchdog
and exit path only. Entry keeps its 2 s rule, because refusing an entry costs nothing.
**Prerequisite: confirm from the vendor's current docs** that a re-subscribe returns a
snapshot of an unchanged quote, or that a REST quote endpoint exists and is fresh.
Neither has been verified, and a design built on an assumed vendor behaviour has
already cost this project once (2026-09-22).

## 5. What is still to read

```bash
sqlite3 "file:argus-live.db?mode=ro" "SELECT seq, ts, category, json_extract(payload,'$.problems'), json_extract(payload,'$.reason'), json_extract(payload,'$.crossing_debit'), json_extract(payload,'$.walk_away_price') FROM audit_log WHERE ts BETWEEN '2026-09-24T19:29:55Z' AND '2026-09-24T19:38:40Z' AND category IN ('watchdog.triggered','watchdog.exit_unpriceable','watchdog.exit_unattempted','watchdog.exit_started','walk.step','walk.finished','watchdog.crossed','watchdog.exit_finished') ORDER BY seq;"
```

It answers:

1. **What held the flatten from 15:30:00 to 15:37:56:** stale legs (`exit_unpriceable`,
   `problems` naming quote ages) or zero-mid (`exit_unattempted`, the
   `UnpriceableSpreadError` sentence), and in what proportion.
2. **`crossing_debit` at `exit_started`.** If it already read 0.10 at 15:37:56, then
   rung 1 bid *exactly* the natural and did not fill, which is the strongest form of
   finding 1's evidence.
3. **`walk_away_price` at `exit_started`**, which should be −0.20 and is the correct
   floor for the deficit.

After the close: whether #509260365 filled or died, and the settlement print. The
nightly ledger values a held-to-expiry trade at settlement (v0.5.0).

**Which `watchdog.crossed` rows carry a wrong `walked_away_at`:** every row whose walk
had `steps > 0`. That means none of the three rows the 2026-09-17 record used. It may
include some since 2026-09-01's v0.3.14 deploy, when `exit_walk_min_steps` first gave
the flatten's ladder a range. Today's row is one.

## 6. The data point for the open flatten-exemption decision

Short strike ~80 points out of the money at 15:30, IV ~0.27, 30 min to settlement:
roughly 2.5σ, and 90 points (~3σ) by the time spot reached 7710. The flatten tried to
pay 0.05–0.20 to close it, would have paid $10 had the cross filled, and spent about 8
minutes unpriceable first. That is the same shape as 2026-09-17 and 2026-09-18
(40–50 points, a 0.05 debit). **It is recorded here and changes nothing in that
decision's two hard constraints**: never skip a position whose exit is spent or
failing, and the threshold comes from data, not a guess.

## Additional Insights

The decision of whether a flattening algorithm should skip a far-out-of-the-money (OTM) 0DTE SPX spread hinges on balancing guaranteed execution friction against the low-probability, high-severity tail risk of terminal gamma. In automated trading and risk management, resolving this open decision requires moving away from a binary "always/never skip" approach and establishing programmatic thresholds.

## Arguments for Skipping (Holding to Expiration)

⚬ Cash Settlement Architecture: SPX options are European-style and cash-settled. Unlike SPY, there is zero risk of physical stock assignment. An OTM expiration cleanly resolves to cash the next business day without unexpected margin calls or overnight equity exposure.
⚬ Slippage and Commission Drag: Far-OTM 0DTE spreads frequently quote with wide relative spreads (e.g., $0.00 bid / $0.05 ask). Forcing a close requires crossing the bid-ask spread and paying exchange fees to eliminate near-zero risk, which significantly degrades algorithmic expected value over thousands of trade cycles.
⚬ Execution Bottlenecks: Routing marketable limit orders for highly illiquid, near-zero premium legs can result in partial fills or rejected orders. This operational drag can bottleneck the algorithm's routing sequence, delaying the flattening of more critical, higher-delta positions.

## Arguments for Executing the Flatten

⚬ Terminal Gamma Risk: In 0DTE SPX options, gamma peaks exponentially in the final hours of the session. A sudden macro headline or structural imbalance can rapidly drive a far-OTM short strike into the money, transforming a nominal risk into a maximum-loss event.
⚬ Liquidity Evaporation in Volatility: If the market suddenly trends toward the skipped spread, liquidity on those specific strikes will vanish. Attempting an emergency flatten during a localized volatility spike will cost exponentially more than the baseline slippage of a preemptive close.
⚬ Systematic State Integrity: For automated systems, leaving a position open violates a strict "net-zero" daily risk mandate. If the architecture requires total liquidation to lock daily P&L or reset margin limits, skipping a spread leaves the risk state technically unresolved.

## Quantitative Resolution Framework

To resolve this, the flattening logic should incorporate conditional parameters:

⚬ Dynamic Price and Risk Thresholds: Program the flatten sequence to skip the spread only if specific criteria are met—for example, if the short leg delta is less than 0.02, the remaining time to expiration is under 15 minutes, and the spread's mid-price is below $0.10.
⚬ Time-to-Close Dependency: A flatten triggered by a mid-day stop-loss (e.g., 1:00 PM) must close all far-OTM spreads, as three hours is plenty of time for the index to bridge a gap. A time-based flatten at 3:55 PM justifies a skip, as the probability of a black-swan move in five minutes is mathematically remote.
⚬ Asymmetric Liquidation: Rather than evaluating the spread as a single atomic unit, the algorithm can route an order to buy back the short leg only. This eliminates all directional tail risk while saving the commission and bid-ask friction of attempting to sell the worthless long wing.

The incident from the September 24 live session (20260924-put-7625-7620) provides direct empirical data for the open decision on whether FLATTEN\_BEFORE\_CLOSE should be conditional. Attempting to force-flatten an 80-to-90-point OTM put vertical (\\sim 2.5\\sigma to 3\\sigma) at 15:30 ET created significant execution friction for a position with a broker mid-price of 0.00 and virtually zero terminal risk.

## **1\. Assessment of Today's Incident vs. Exemption Logic**

Today's session highlights three primary failure modes that occur when blindly forcing an end-of-day flatten on far-OTM options:

> * **Execution Bottlenecks via False-Positive Staleness (Finding 3):** Far-OTM wings late in the session rarely trade or change quotes. Because streaming feeds (like DXLink) push updates only on price changes, unchanged legs exceeded the system's staleness.market\_data\_max\_age\_s \= 2.0s limit. This stalled the flatten execution for nearly 8 minutes (15:30:00 to 15:37:56).  
> * **Sub-Floor Escalation Anomalies (Finding 1):** The forced walk ladder progressed through 0.05 \\rightarrow 0.10 \\rightarrow 0.15 \\rightarrow 0.20 debit without filling. Upon failing the walk, the cross-escalation mechanism (ExitExecutor.\_cross\_limit) re-bid 0.10 debit based on the leg-derived natural price—pricing two ticks *below* the walk floor that the market had just rejected.  
> * **Negative EV from Spread-Crossing Friction:** Bidding up to a 0.20 debit (\$20 per contract) or crossing at 0.10 (\$10) to close an illiquid spread worth 0.00 trades guaranteed slippage for protection against a statistically negligible tail event.

## **2\. Core Bug Fixes Required (Independent of Exemption Decision)**

Regardless of whether far-OTM spreads are eventually skipped, the incident exposed mechanical defects in the execution engine that must be fixed for standard exits and stop-losses:

### **Finding 1: Enforce Monotonic Escalation (Option A)**

The cross escalation must never bid below the highest step reached by the walk ladder.

> * **Fix:** Implement Option A for ExitExecutor.\_cross\_limit: \\text{cross\\\_limit} \= \\min\\left(\\text{width}, \\max\\left(\\text{natural}, \\vert{}\\text{floor}\\vert{} \+ 1\\text{ tick}\\right)\\right)  
> * This ensures the escalation always bids past where the walk gave up, preventing backward pricing loops when the natural price sits below the ladder's floor.

### **Finding 2: Correct Audit Reporting**

ExitExecutor.\_cross logged order.limit\_price (the walk's opening limit of \-0.05) into walked\_away\_at instead of the ladder's floor (-0.20).

> * **Fix:** Read walked\_away\_at directly from walk.final\_limit\_price (falling back to order.limit\_price only if None), preserving opening\_limit as a separate key.

### **Finding 3: Leg Quote On-Demand Polling**

To stop quiet, far-OTM legs from tripping the 2-second staleness rail during exits:

> * **Fix:** Implement an explicit REST poll or market-data re-subscription trigger on the exit path when a leg is flagged as quiet. If an on-demand quote returns the same 0.05/0.10 price, it is validated as *current* rather than stale, bypassing the UnpriceableSpreadError without loosening entry staleness bounds.

## **3\. Data-Driven Framework for the Exemption Decision**

To make FLATTEN\_BEFORE\_CLOSE conditional while honoring the two core system constraints (**never skip a position with a spent or failing exit**, and **basing all thresholds on data**), the flattening engine should evaluate a position against strict conditional gates:

| Gate Parameter | Threshold Criterion | Decision Rationale |
| :---- | :---- | :---- |
| **Moneyness / Delta** | Short Leg \\Delta \< 0.01 (or \> 2.5\\sigma OTM) | Structural probability of expiring ITM in remaining time is near zero. |
| **Time to Expiration (T)** | T \\le 30\\text{ minutes} (Post-15:30 ET) | Limits the window for high-velocity underlying moves to bridge the gap. |
| **Spread Value** | Mid Price \= 0.00 (or natural ask \\le 0.05) | Avoids crossing wide bid-ask spreads for worthless positions. |
| **State Integrity** | outcome \!= impaired AND exit\_attempts \== 0 | **Mandatory Constraint:** If a stop-loss or prior exit failed or is in progress, the position *must* be flattened regardless of delta. |

### **Proposed Routing Logic**

When FLATTEN\_BEFORE\_CLOSE fires at 15:30 ET:

> 1. Check position health. If the trade has open orders, prior failed exits, or impaired state, **flatten immediately**.  
> 2. If position state is clean, check if short leg \\Delta \< 0.01 and spread mid \= 0.00.  
> 3. If true, log event FLATTEN\_SKIPPED\_FAR\_OTM with spot, delta, and distance metrics, then hold the position to settlement.  
> 4. If false, execute the standard flatten sequence.
