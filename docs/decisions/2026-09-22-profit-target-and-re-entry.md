# A profit target, and the second swing behind it (2026-09-22)

*Raised by Richard on 2026-09-22: a position taken at 10:00 ET was at max profit by
14:17 with nearly two hours of market time left — should the watchdog take its profit
the way it already takes its flatten, and should Argus then re-enter? **These are two
questions, not one, and only the first one is answerable from the data we hold.** The
profit target is close to FREE and the reason is structural rather than statistical:
Argus never holds to expiry, so it pays the exit tick at 15:30 either way, and the tick
does not shrink while you wait. The re-entry needs no building at all — it is already
automatic — and its own economics are the weaker half of the proposal.*

> **THE PROFIT TARGET IS BUILT (2026-09-23), DEPLOYED IN v0.5.0 (Vindemiatrix, 2026-09-23 — see
> [release notes](../releases/v0.5.0.md)), SHIPPED
> `profit_target_enabled = false` IN ALL THREE PROFILES.** Coupled image + config
> release. §12 records what was built, the four things the build decided that the
> design had not, and the one hazard it found; §12.1 the five review fixes, including
> that a take which FILLS spends the close. The re-entry half is unchanged: not
> built, not coupled, `signal.entry_latest` untouched.
>
> **RESEARCH + DESIGN (as first written). NOTHING IS BUILT AND NOTHING IS SIGNED OFF.** Read-only
> analysis of `dev/nas/argus-20260914T163001.db` (cert, 780,853 rows, 2026-07-23 →
> 2026-09-14) and `dev/nas/argus-20260921T163001.db` (live, 75,148 rows, 2026-09-15 →
> 2026-09-21). No tree changes beyond this file and its index entry. The recommendation
> is to build the profit target ALONE, and to treat the re-entry gate as a separate
> decision that the present sample cannot size.
>
> **§11 COMPLETES THE DESIGN AND ITS RECOMMENDATIONS ARE AGREED (2026-09-23).** The
> recommendation section alone was not buildable: a profit-target send would have spent
> the position's ONE emergency exit. §11 settles that (no market-crossing escalation for
> this trigger, its own count budget, never `_attempted`), adds the
> `watchdog.profit_target_enabled` switch, records which knobs deliberately are not
> config, and resolves the re-entry contradiction. **Read §11 before building.**
>
> **§10 CORRECTS THE TRIGGER THIS RECORD FIRST SPECIFIED**, from a live flatten that had
> been refusing for 16 minutes while this was being written. `crossing_debit ≤ 0.05`
> fires on **0 of 5,010** buildable ticks — an arithmetic empty set, not a sampling one.
> The trigger is now `round_to_tick(cost_to_close) == 0.05`, buildable by construction,
> catching **both** tail cases instead of one and retiring §4's debounce cliff.
> **§1–§9 stand as measured; only the trigger changed, and the recommendation's
> numbers are §10's.**

## Why this is not the usual "don't pick up pennies" question

The standing argument against closing a nearly worthless credit spread is that you pay
a tick to get out of something that would have expired worthless for free. **That
argument does not apply to Argus.** `watchdog.flatten_before_close_s = 1800` closes
every surviving position at 15:30 ET through `ExitExecutor.close`. The exit tick is
paid on every winning trade already, as a matter of design.

So the comparison is not *pay a tick* against *pay nothing*. It is **pay a tick now**
against **pay a tick in three hours**, holding full width-sized risk in between.

## What was measured

### 1. The cost of getting out does not decline with time

`crossing_debit` (the executable offer) has ridden every evaluation payload since
v0.4.3, beside `cost_to_close` (the mid). Their difference is algebraically the sum of
the two half-spreads. Over 25,857 live evaluations carrying both, **conditioned on the
spread being nearly worthless** (`cost_to_close ≤ 0.10`, n = 5,010):

| `crossing_debit − cost_to_close` | share |
|---|---|
| **+0.05 — one tick** | **91.7%** |
| +0.07 | 4.3% |
| +0.08 | 3.3% |
| +0.10 | 0.7% |

It is one tick, near-deterministically, and it is one tick at 12:00 exactly as it is at
15:30. The tick grid floors it; there is no further decay to collect.

Both live flattens confirm it end to end, from the offer through to the realized fill:

| day | position | `crossing_debit` from | flatten filled | net debit |
|---|---|---|---|---|
| 2026-09-17 | `put-7585-7580` | — (pre-v0.4.3) | 15:31:13 | 0.08 − 0.03 = **0.05** |
| 2026-09-18 | `put-7595-7590` | **0.05 from 14:15** | 15:32:13 | 0.12 − 0.07 = **0.05** |

On 09-18 the offer was 0.05 from 14:15 onward and the flatten paid 0.05 at 15:32.
**Seventy-seven minutes of gamma exposure bought nothing and cost nothing.**

### 2. Simulating the target over every position we have a path for

48 positions carry a complete priced mark path and a known entry credit. Policy A is
what runs today (credit stop, else the 15:30 flatten). Policy B adds an exit at
`cost_to_close ≤ 0.00` sustained 30s. Where `crossing_debit` predates v0.4.3 it is
proxied at `cost_to_close + 0.05`, the measured modal value above.

**20 of the 48 positions reach the trigger.** Their individual deltas:

| outcome | n | Δ |
|---|---|---|
| identical — the flatten paid the same tick | **18** | **$0** |
| the flatten paid a worse mark than the trigger would have | 2 | +$6 each |
| reached max profit, then reversed and stopped out | **1** | **+$226** |

Net **+$238** across 48 positions. **31.7 position-hours** of width-sized exposure
removed, median **1.40 h** per position, maximum 4.51 h.

**The cost side is the robust half and the benefit side is n=1.** That asymmetry is
the finding, not the $238.

### 3. The threshold is not a free parameter — past 0.10 it forfeits real credit

| take-profit threshold | total | win % | stdev |
|---|---|---|---|
| none (current) | −$1,471 | 50.0% | $121 |
| `≤ 0.00` | **−$1,233** | 52.1% | $122 |
| `≤ 0.05` | −$1,228 | 54.2% | $118 |
| `≤ 0.10` | −$1,213 | 56.2% | $113 |
| `≤ 0.15` | −$1,433 | 56.2% | $109 |
| `≤ 0.20` | −$1,673 | 56.2% | $105 |

*(The absolute levels are NOT admissible as a statement about Argus's edge — the cert
entry credits are cert fills. The policy DIFFERENCE is admissible, because the entry
credit cancels out of it and what remains is a pure quote-path question.)*

`0.15` and `0.20` are **worse than doing nothing**: they buy the win rate by giving
away credit the position would have kept. `0.00` is the only threshold that is exactly
free, and it is free for a mechanical reason — the trigger and the flatten pay the same
one tick. **`0.00` is the recommendation, and it is recommended because it costs
nothing, not because it scored best.**

### 4. The entire benefit is one position, and there is a hard debounce cliff

`20260901-put-7605-7600`: entered 10:00, 15,194 marks. Spot reached 7663 against a 7605
short put — 58 points of cushion — and the spread marked 0.00 across **15 separate
episodes between 11:38:09 and 12:12:30**. It then bled back, oscillated 0.10–0.45 for
two hours, and collapsed at 14:40 when SPX fell to 7614. Stopped at −$126. The target
would have banked +$100.

**Its longest continuous zero episode was 36 seconds** (11:47:39 → 11:48:16, 18 marks).
That number is the whole design constraint:

| sustain | Δ (slip +0.05) | Δ (slip +0.10) | 09-01 caught? |
|---|---|---|---|
| 5s / 15s / 30s | **+$238** | **+$234** | yes |
| 60s / 120s | +$12 | +$8 | **no** |
| 300s | +$6 | +$4 | no |

Robust to doubling the slippage assumption; **not** robust to a debounce above ~36s.
`watchdog.sustained_s = 5.0` is already the house debounce for the other two triggers,
so 5s sits inside existing convention — but anyone "hardening" it to a minute silently
retires the only benefit this change has.

A second position, `20260820-put-7635-7630`, flips −$116 → +$70 at a `0.05` threshold
and does **not** trigger at `0.00` (its minimum mark was 0.05, never 0.00). So the
observed tail is n=2 and the threshold choice trades one against the other. **n=2
cannot choose between them. The tiebreak is that `0.00` is free.**

## The re-entry half

### 5. It is already built, and that is the first thing to know

If a position closes at 14:17, nothing needs adding for Argus to take another swing:

- `max_concurrent_positions = 1` frees the moment the position closes
- `signal.entry_latest = "15:00"` still admits entries
- `candidate_id` is keyed `(date, side, short, long)`, so afternoon strikes are a NEW
  id and `entry.py`'s one-attempt-per-candidate rule does not block it
- the loss budget passes when the day is flat or up (§7)

**The question is not whether to add re-entry. It is whether to GATE the one that
already exists.**

### 6. The market pays the same rate all day; only the cushion collapses

Candidates at the delta-selected short strike, by ET half-hour (cert pre-09-03 where the
5-point ladder was intact, plus live; live market data throughout):

| ET | n | median credit | credit / width | cushion (pts OTM) | median delta |
|---|---|---|---|---|---|
| 10:00 | 5,119 | 0.60 | 0.115 | **28.4** | 0.153 |
| 12:00 | 4,234 | 0.55 | 0.110 | 20.4 | 0.156 |
| 14:00 | 2,752 | 0.55 | 0.110 | 13.8 | 0.165 |
| 14:30 | 2,683 | 0.57 | 0.115 | **12.2** | 0.168 |

Every bucket clears `min_credit = 0.50` 100% of the time. **Selection is by delta, so
the credit per unit width is flat by construction** — the market is pricing the same
probability and charging the same for it. The afternoon entry is not a worse *price*.
It is the same price with less than half the cushion, which repackages the variance
rather than changing the mean.

### 7. Realized risk shows no time trend, and the test cannot see one

Tracking each delta-selected strike forward on the recorded index path, one observation
per (day, side, half-hour):

| ET | P(breach short strike by 15:30) | P(ITM at close) |
|---|---|---|
| 10:00 | 12.8% | 5.1% |
| 12:00 | 21.1% | 13.2% |
| 14:00 | 22.9% | 8.6% |
| 14:30 | 10.8% | 13.5% |

Both series sit near the 0.15 the delta target implies, with no monotone trend.

**This is 374 observations over 20 TRADING DAYS, and the effective n is 20.**
Observations inside a day are near-perfectly correlated — a trend day breaches every
bucket it contains. At a ~15% rate and n≈20 the standard error is ~8 points, so the
interval on any bucket is roughly **±16 percentage points**. *This test cannot
distinguish a 10% breach rate from a 25% one.* It is evidence of no detectable
degradation and must never be cited as evidence of no degradation.

### 8. What does degrade is CAPTURE, because the flatten truncates the trade

A 14:30 entry gets 60 minutes before 15:30 forces it out. It cannot ride to
worthlessness — it pays the exit tick on a spread that still carries real extrinsic
value. The six late entries on record:

| position | entered | credit | 15:29 mark | outcome |
|---|---|---|---|---|
| `20260820-put-7635-7630` | 14:11 | 0.65 | 0.12 | ~74% of credit kept |
| `20260901-put-7615-7610` | 13:27 | 0.75 | 1.87 | **stopped out** |
| `20260901-put-7600-7595` | 14:43 | 0.55 | 0.00 | full |
| `20260901-put-7595-7590` | 14:45 | 0.55 | 0.00 | full |
| `20260908-call-7700-7710` | 14:46 | 0.50 | 0.20 | ~50% |
| `20260909-call-7660-7670` | 13:36 | 0.55 | 0.18 | ~58% |

n=6 settles nothing. But the *structure* is not a sampling question: a re-entry at
14:59 holds for 31 minutes, captures perhaps 0.15–0.20 net of the tick, and carries the
full 5.00 width the entire time. **The pairing of `entry_latest = "15:00"` with
`flatten_before_close_s = 1800` permits a trade whose own credit-capture arithmetic says
it should not be taken.** That is a defect in the existing configuration, independent of
anything proposed here.

### 9. The loss budget de-sizes the sequence; it does not stop it

**A claim made earlier in the raising conversation was wrong and is corrected here.** It
was asserted that under v0.4.6 the 2026-09-21 sequence would have been a single trade.
It would not. `orchestrator.size_contracts` takes the budget as its fourth bound and
`max(1, min(...))` floors the lot count, so the budget **reduces size and keeps
trading** — the module's own docstring says so: *"what makes 1-lot re-entries after a
loss automatic"*.

2026-09-21 ran on **v0.4.5**, before the budget shipped: five sequential call spreads up
a trending ladder, five stop-outs, **−$730** realized (`daily_pnl` −778.80 with fees).
Replaying it against v0.4.6's rule, using the actual realized debits:

| # | contracts | `remaining` at entry | realized |
|---|---|---|---|
| 1 | 2 | 950 | −170 |
| 2 | **1** | 780 | −75 |
| 3 | 1 | 705 | −65 |
| 4 | 1 | 640 | −60 |
| 5 | 1 | 580 | −80 |
| | | 500 — a 6th entry still fits | **−450** |

**−$450 instead of −$730, across the same five trades.** The budget is a de-risking
brake, not a stop.

The reverse case is the one that matters here. `remaining = daily_max_loss − W` is
**unclamped**, so after a profit target banks +$90 the budget reads **$1,040** and the
next candidate sizes to a **full 2 lots**. This is coherent with the module's induction
(the day's final loss still cannot exceed `daily_max_loss`), and it is worth saying
plainly: **the profit target mechanically funds a full-size second swing out of the
first swing's profit.** That consequence belongs to whoever signs off on coupling the
two, and it is the main reason not to.

## 10. CORRECTION — the first trigger specified here could never have built an order

*Raised by Richard 2026-09-22 at ~15:45 ET, from a live episode in progress
(`20260922-put-7740-7735`, `seconds_to_flatten: -910`): the flatten had been refusing for
16 minutes, alternating `watchdog.exit_unpriceable` (`quote … is 2.23s old (max 2s)`)
with `watchdog.exit_still_unplaced` (`UnpriceableSpreadError`). **His point: a profit
target would meet the same wall, because it fires in the same regime.** He was right,
and more completely than the objection claimed.*

`build._limit_price` refuses when `round_to_tick(mid) <= 0`. `round_to_tick` is
ROUND_HALF_UP on a 0.05 tick, so **a net mid below 0.025 refuses and 0.025+ builds at
0.05**. Cross-tabulating the live zone (n = 5,010 evaluations, `cost_to_close ≤ 0.12`):

| mid | `crossing_debit` | share of zone | order builds? | fires `crossing_debit ≤ 0.05`? |
|---|---|---|---|---|
| **0.00** | 0.05 | **44.4%** | **refuses** | **yes** |
| 0.02 | 0.10 | 3.6% | refuses | no |
| 0.03 | 0.10 | 1.2% | builds | no |
| **0.05** | 0.10 | **35.1%** | **builds** | **no** |
| 0.07 / 0.08 | 0.15 | 2.7% | builds | no |
| 0.10 | 0.15 | 12.2% | builds | no |

**The intersection is EMPTY — 0 of 5,010 ticks — and it is empty by arithmetic rather
than by sampling**, the same shape as the 2026-09-17 bimodal finding.
`crossing_debit = short ask − long bid`, so a cross of 0.05 requires short ask 0.05 and
long bid 0.00, which puts both leg mids at 0.025 and the net at exactly 0.00 — the
refusal. **Every buildable tick has `crossing_debit ≥ 0.10`.** The trigger as written in
the first draft of this record would have fired only on ticks where no order can exist.

### The corrected trigger, and it is strictly better

Fire on **`round_to_tick(cost_to_close) == 0.05`** — the cheapest CLOSING LIMIT that can
be expressed at all. Buildable by construction, because the condition *is* buildability
at the minimum tick.

| | n fires | Δ vs current | both tail saves? |
|---|---|---|---|
| fill at the 0.05 limit (both live flattens did) | 23 / 48 | **+$455** | yes |
| conservative: fills one rung up at 0.10 | 23 / 48 | **+$240** | yes |

**17 of the 23 land at exactly $0** — still free — and **both** tail cases are now caught,
not one: `20260901-put-7605-7600` (+$225) and `20260820-put-7640-7635` (+$195), the
latter of which the `0.00` trigger missed entirely. It also fires **earlier and on far
more ticks**: 09-01 triggers at **10:34** instead of 11:47, because a 0.05 mid is
common where a 0.00 mid is a knife-edge. **The 36-second debounce cliff of §4
disappears with it** — that cliff was an artifact of triggering on the rarer condition.

### Unbuildability is the tail, not the mode — and the two failures are not symmetric

Across 09-01's whole take-profit window (11:00–12:59, 6,262 marks) the mid was
**buildable on 98.7%** of ticks; only 84 refused. The state oscillates.

And the consequence differs completely by *which* exit is blocked:

| | retry budget when it cannot build | cost of one failed tick |
|---|---|---|
| **flatten at 15:30** | hard deadline at expiry — today, ~960s used of ~1,800 | rides to settlement, **up to the full width** |
| **profit target at 12:00** | ~12,600 ticks before the flatten even fires | **nothing** |

**A profit target is best-effort by nature and does not need to be reliable to be
valuable.** Every tick it fails costs zero; the one tick it succeeds banks the trade.
So today's episode is an argument **for** it: 23 of 48 positions would have left the book
hours before 15:30 and never reached the unbuildable window at all. The target is
partial insurance against the flatten's own unreliability — which is the failure mode of
2026-09-14 (every exit lost at the transport layer, ridden to settlement) and of this
very episode.

**What it does NOT fix:** a spread that is unbuildable for its entire life, and the
stale-quote half (`market_data_max_age_s = 2.0` governing the exit path). Neither is
caused or worsened here, and **raising that key remains forbidden**.

## The recommendation

### Build the profit target, alone

A third trigger reason alongside `credit_multiple`, `underlying_breached` and
`flatten_before_close`, routed through the same `ExitExecutor.close` and the same
guarded walk. **It is NOT the flatten's shape all the way down** — §11 removes the
market-crossing escalation for this trigger alone, because that is where the outcomes
that spend a position's one exit attempt come from and a profit take has no deadline
that justifies paying for them.

- **Trigger on `round_to_tick(cost_to_close) == 0.05`** — the cheapest expressible
  closing limit. **Not `crossing_debit ≤ 0.05`**, which §10 measures as unbuildable on
  every tick it fires. `crossing_debit` stays telemetry, so the AST test below is
  **NOT** retired after all.
- **Debounce at the existing `watchdog.sustained_s = 5.0`.**
- **It must never be the reason a spread is held.** The other triggers keep precedence;
  this one only ever fires on a position nothing else wants to touch.
- **A refusal must stay free.** `NOT_ATTEMPTED` must not spend the exit attempt and must
  not touch the flatten's one-shot override — the 2026-09-02 lifecycle, unchanged. The
  whole design rests on a failed tick costing nothing. **That bullet covers only the
  unbuildable case, which is the benign one; §11 covers the outcomes that reach the
  broker, and it is the part of this design that actually needed deciding.**
- **Behind `watchdog.profit_target_enabled`, shipped `false`** — §11.6. One new REQUIRED
  key, so a coupled image + config deploy.

**`tests/test_payload_safety.py::test_nothing_in_src_reads_the_crossing_debit` STAYS.**
The first draft of this record proposed retiring it, because the trigger it specified
read `crossing_debit`. §10 replaced that trigger with one reading the mid, so nothing
in `src/` needs to decide on the offer and the 2026-09-15 rule is untouched. **Worth
carrying: the AST test was not an obstacle to route around — the design that wanted to
retire it was the one that was wrong.**

### Do NOT couple the re-entry to it

Re-entry is already automatic (§5), it is governed by a rail whose **first live denial
is still unread**, and §9 shows the profit target expands the budget that authorizes it.
Shipping both together means the next bad day cannot be attributed to either.

The defensible narrow version is a **minimum-remaining-time gate on a second entry** —
it would have permitted the 08-28 (10:51) and 09-01 (11:48) second swings and refused
the 14:59 one. **This pass cannot size that number.** What it can say is that the
30-minute version is structurally bad on its own arithmetic (§8).

### What would settle the re-entry question

Capture rate as a function of **hold time available**, measured on live fills, at an
effective n above ~40 days. At the current rate that is roughly two more months of live
sessions. The profit target does not need to wait for it; the re-entry gate does.

## 11. THE ATTEMPT BUDGET, THE SWITCH, AND THE KNOBS

*The design the recommendation above was missing. Raised 2026-09-22 by Richard asking
whether the record was complete enough to build from — it was not — and settled
2026-09-23. **Recommendations below are agreed.** §1–§10 establish that the profit
target is worth having; §11 is how it is built without being worse than not having it.*

### 11.1 The hole — a profit take would spend the position's one emergency exit

`_attempted[key]` is written on **any** exit outcome that reached the broker
(watchdog.py:3995 (private record, not published), and again at
:4033 (private record, not published) for the crash path). After that
`check_once` still evaluates, records and pages — the 2026-08-28 fix — but **never
submits again**. The only action left is the flatten's one-shot override at 15:30.

So a profit-target send at 10:34 that comes back `REJECTED`, `NOT_RESOLVED`, `BLOCKED`
or `CANCELLED_ELSEWHERE` **retires the position's intraday stop five hours early.**

That inverts the feature on exactly the positions it exists for.
`20260901-put-7605-7600` is the case: §10's trigger fires at 10:34, and if that send
fails the 14:40 `credit_multiple` stop cannot fire. **A +$225 save becomes an
uncovered tail.** Broker flakiness here is measured, not hypothetical — 3 of 11 entry
ideas lost to `NO_ORDER_EXISTS` on 2026-09-02, seven failed guard round trips on
2026-09-03.

### 11.2 What the outcome data says, and why EV is not the argument

Every recorded exit, cert + live (n=38), and the 11 whose opening limit was ≤ 0.10 —
the profit-target regime:

| outcome | all | limit ≤ 0.10 | spends `_attempted`? |
|---|---|---|---|
| `filled` | 25 (66%) | 6 | closed |
| `crossed` | 3 (8%) | **3** | closed |
| `nothing_at_broker` | 7 (18%) | 1 | retryable, `_EXIT_SEND_ATTEMPTS` |
| `cross_unpriceable` | 1 (3%) | **1** | retryable, same budget |
| `not_resolved` | 2 (5%) | **0** | **absolute** |
| `cross_refused` | 0 | **0** | **absolute** |

**Zero absolute spends in the target regime (0/11).** Rule of three puts the 95% upper
bound near 26%, so that alone settles nothing; pooled over all 38 it is 5.3%, and both
instances came from `credit_multiple` and `underlying_near_short_strike` — stressed
regimes, not this one.

**The exposure is bounded by less than first feared.** A spent position keeps its
evaluation, its mark, its repeating page, and **the flatten's one-shot override** —
`_flatten_attempted` is a separate set that `_spend_on_crash` explicitly refuses to
touch. So the incremental tail is not the width but `(width − 2 × credit) × 200 ≈ $780`
on a position that would otherwise have stopped.

Run through: 5.3% × 8% of triggered positions later needing their stop (2 of 25) × $780
≈ **$3.3 per triggered position**, against **$19.8** (0.05 fill) or **$10.4**
(conservative 0.10 fill) of benefit.

**So the EV case survives doing nothing, and that is deliberately NOT the reason to fix
it.** A feature whose entire purpose is banking a small certain gain must not be able
to manufacture a $780 uncovered tail. That is the same asymmetry reasoning the
2026-09-17 ladder pass turned on, and it is a statement about character rather than
about expectation.

### 11.3 The cross is the path, and a profit take does not need it

**4 of the 11 same-regime exits reached the market-crossing escalation** (`crossed` 3 +
`cross_unpriceable` 1). That escalation is where both absolute-spend outcomes come
from: `CROSS_REFUSED` is its failure mode, and `_RETRYABLE_EXITS`' own docstring
singles it out — *"Everything absent from it spends the position's attempt on the first
try, including — and especially — `CROSS_REFUSED`."*

**The cross exists because a stopped-out position must get flat NOW.** A profit target
has no such deadline — §10 measures ~12,600 ticks of retry budget before the flatten
even fires, and a failed tick costs nothing. Escalating a profit take is importing an
emergency mechanism into a non-emergency, and paying its failure modes for nothing.

The bound's SHAPE is derivable from rules this repo already holds:
`_EXIT_SEND_ATTEMPTS` is a **COUNT** because each retry costs a SUBMISSION; P5's cancel
recheck and `_GUARD_ATTEMPTS` are **RATES** because a cancel and a GET are not. A
profit-target attempt is a submission, so **its bound is a COUNT.**

### 11.4 The options, and the decision

| | shape | verdict |
|---|---|---|
| **A** | own budget, `_attempted` untouched, safety from `DuplicateOrderGuard` | **no, alone.** Sound in principle — it is the codebase's stated licence, *"the licence is `DuplicateOrderGuard`, never the outcome's own answer"* — but a lingering profit order makes the emergency send return `BLOCKED → NOT_RESOLVED`, which spends `_attempted` anyway. Does not close the hole by itself. |
| **B** | fire only with a full budget; accept that one failure retires it | **no.** Names the hole rather than solving it — a spent position never reaches `_trigger` anyway, so the precondition is free, and the "accept" clause is the status quo. Keep the precondition, drop the acceptance. |
| **C** | **no escalation: the profit target never crosses** | **CHOSEN.** Removes the failure mode instead of compensating for it, and follows from a property the profit target genuinely has and no other exit does. |
| **D** | guard-gated re-arm — an emergency trigger gets one further send after a profit spend, exactly as `_flatten_attempted` does | **held in reserve.** Proven mechanism, but it treats the symptom and adds a third allowance to a position that already has two. It is where C falls back to if 11.5's terminal branch cannot be built cleanly. |

### 11.5 The design

- **`WalkPolicy.for_profit_target(...)`** — a third constructor beside `for_entry` and
  `for_exit`. The class contract already says *"Entry and exit differ ONLY here"*, and
  this is the same seam used a third time. `min_concession_steps` stated **explicitly**,
  on that field's own no-default rule.
- **No market-crossing escalation.** This is the whole of C. `CROSS_REFUSED` and
  `CROSS_UNPRICEABLE` become unreachable for this trigger by construction.
- **`_PROFIT_SEND_ATTEMPTS = 2`**, a module constant on `_EXIT_SEND_ATTEMPTS`'
  precedent — **not config** (§11.7). Two rather than three: there is no urgency to
  justify the third, and the position gets another look in one second.
- **Its own `_profit_spent`, never `_attempted`.** Exhausting the budget retires the
  FEATURE for that position and leaves the emergency exit and the flatten override
  fully intact.
- **`_flatten_attempted` untouched**, on the 2026-09-02 lifecycle and
  `_spend_on_crash`'s rule that a failure must never GRANT.
- **Safety rests on `DuplicateOrderGuard`.** The emergency exit already runs
  `guard_first_submission=True`, so if a profit order were somehow still live the
  emergency send returns `BLOCKED` rather than double-closing. Same licence
  `NOTHING_AT_BROKER` and the flatten override already stand on.

**The one thing C must not quietly become.** An unfilled resting limit that times out
at `working_timeout_s` reports `NEVER_WORKED → NOT_RESOLVED`, which is an absolute
spend — the very outcome C exists to avoid, arriving through the front door. **C is
only correct if the terminal branch cancels, confirms settled through
`_await_settled`, and reports a RETRYABLE outcome.** If that cannot be built cleanly,
C collapses into D, and **that must be discovered at design time rather than at build
time.** This is the first thing to prototype.

### 11.6 The switch — a plain bool, and why there is no observe mode

**`watchdog.profit_target_enabled`, shipped `false`.** P5's
`watchdog.unmanaged_order_cancel` is the precedent exactly: promotion and rollback are
a **CONFIG FLIP, not a deploy**, which matters most for a new live behaviour whose
benefit rests on n=2.

**A tri-state `off` / `observe` / `on` was considered and rejected, because observe
mode would add nothing.** `watchdog.check` already records `cost_to_close` at the 1s
cadence, and the trigger is a pure function of it — *"would it have fired, and when"*
is computable today from telemetry that already exists, which is literally how every
number in §1–§10 was produced. The only thing observe mode cannot give is the OUTCOME
of a send, and neither can a real shadow week. **So the shadow week costs a deploy and
buys nothing: replay the trigger against next week's live rows instead.**

### 11.7 The knobs, and what deliberately is not one

| candidate | verdict | why |
|---|---|---|
| **`profit_target_enabled`** (bool) | **CONFIG** | §11.6. The one thing that must be changeable without a deploy. |
| max debit to take at (0.05 / 0.10) | **module constant** | It is not a number, it is *the minimum expressible limit*. §3 measured 0.15 and 0.20 as **worse than doing nothing** — they buy the win rate by giving away credit — so the bad region is one notch from the default. A knob like that, in a file that already carries four standing landmines, is the wrong trade. The live data to size it will exist before anyone wants to move it. |
| earliest fire time | **not a knob** | Delaying the target destroys the benefit — removing exposure hours IS the benefit (§2: 31.7 position-hours). If the concern is the second swing, gate the ENTRY (§11.8), never the exit. |
| `_PROFIT_SEND_ATTEMPTS` | **module constant** | Direct `_EXIT_SEND_ATTEMPTS` precedent, which is deliberately not config for this exact reason. |
| debounce | **reuse `watchdog.sustained_s`** | A second debounce key invites the two drifting apart, and §10 retired the cliff that would have justified one. |

**One new REQUIRED key, so a COUPLED image + config deploy** across all three profiles
— `tests/test_config_profiles.py` pins identical key sets, and a REQUIRED key in two
files of three is a process that will not boot.

### 11.8 B2 — "do not couple the re-entry" is not achievable by inaction

The recommendation above says do not couple the re-entry; §5 says re-entry is already
automatic. **Those contradict, and §5 wins**: shipping the target ships re-entry, and
§10 sharpens it, since the corrected trigger fires ~1 hour earlier (10:34 vs 11:47 on
09-01), leaving more of the day to re-enter into.

**The free lever is `signal.entry_latest`.** §8 measured that a re-entry at 14:59 holds
31 minutes, captures ~0.15–0.20, and carries the full width throughout. Lowering that
key from `"15:00"` is a **config-only change with no code at all**, and it puts a floor
under any second swing's hold time — approximating the minimum-remaining-time gate this
record recommends and cannot size.

**This pass still cannot size the number.** What it can say is that the 30-minute
version is bad on its own arithmetic, that the lever already exists, and that it moves
and measures independently of the profit target — which is what keeps the two changes
attributable.

### 11.9 Build spec

**Touches:** `execution/watchdog.py` (the trigger, `_profit_spent`, the reason member),
`execution/loop.py` (`WalkPolicy.for_profit_target`), `config.py` (the key + its
`[watchdog]` home on `TriggerThresholds`), all three `argus.*.toml`, `status.py`.
**Release class: COUPLED image + config.**

**Settled and needing no further decision:**

- `_MARKET_TRIGGER_REASONS` **auto-join is correct.** The trigger reads
  `cost_to_close`, the same input as `CREDIT_MULTIPLE`, in the same branch of
  `evaluate_triggers` — the two are co-evaluable, so `blind`'s superset test is
  unchanged in practice. The constant's own *"a fourth, market-dependent trigger joins
  this set automatically"* applies as written.
- **NOT in `_INSTANT_TRIGGER_REASONS`.** It is a market observation with a quote
  artifact to filter; the flatten's clock-shaped exemption does not transfer.
- **Trigger on `round_to_tick(mid) == 0.05`, never the economic "mid ≤ 0.075".** The
  latter fires at mid 0.00 — 44% of zone ticks — where `UnpriceableSpreadError`
  refuses, producing `watchdog_exit_still_unplaced` every 30s on a WINNING position.
  That is the 2026-09-22 live episode, unprompted and all afternoon. Buildability
  belongs in the trigger.
- **`test_nothing_in_src_reads_the_crossing_debit` stays** (§10).

**Tests the design implies:** the terminal branch reports retryable and leaves nothing
working at the broker (11.5's prototype-first item); a profit-target outcome never
writes `_attempted` (AST or structural, on `residual_orders`' `minted_direction`
precedent); `_flatten_attempted` is untouched by every profit-target path; the trigger
never fires where `build_order` would refuse; and the switch at `false` produces no
outbound call, asserted structurally on `shadow.would_submit`'s shape.

## 12. AS BUILT (2026-09-23)

**Release class: COUPLED image + config.** One new REQUIRED key,
`watchdog.profit_target_enabled` (strict bool — `1` and `"false"` are refused at boot),
`false` in `argus.dev.toml`, `argus.sandbox.toml` and `argus.live.toml`. Identical in
all three, so no `ALLOWED_DIVERGENCE` entry. Promotion is that one line to `true` as a
config release; rollback is the same line back. Neither strands a position.

**What §11.9 specified, built as specified:**

- `TriggerReason.PROFIT_TARGET`, fired on `round_to_tick(cost_to_close) == TICK`
  (`watchdog.at_profit_target`), evaluated in `evaluate_triggers`' credit branch and
  unknown exactly when `CREDIT_MULTIPLE` is. Joins `_MARKET_TRIGGER_REASONS`
  automatically; not in `_INSTANT_TRIGGER_REASONS`; debounced at `sustained_s`.
  `test_the_trigger_fires_exactly_where_the_close_builds_at_one_tick` sweeps the whole
  0.00-0.30 leg grid through the executor's own builder: the trigger is true exactly
  where the close builds at one tick, and never where `UnpriceableSpreadError` refuses.
- `WalkPolicy.for_profit_target`, the third constructor. **No cross**: after a walk
  with nothing filled the executor calls `_await_settled` (the one positive
  confirmation) instead of `_cross`. Confirmed dead -> new `ExitOutcome.UNFILLED`,
  retryable. Filled after all -> `FILLED_LATE`. Anything less -> `NOT_RESOLVED`, which
  **retires the profit target and nothing else**.
- `_PROFIT_SEND_ATTEMPTS = 2`, a module constant; its own `_profit_sends` /
  `_profit_spent`. An AST test (`test_a_profit_take_never_touches_an_emergency_allowance`)
  sweeps every profit-path function and both `if profit:` branches of `_run_exit` for a
  write to `_attempted`, `_sends` or `_flatten_attempted`, and pins `_profit_spent` to
  one writer. A crashed take retires the TAKE (in memory first) and still halts entries.
- The switch is read at one gate (`_may_act_on`), plus the `/status` report — pinned.

**11.5's prototype-first item is answered, and C does NOT collapse into D.**
`NEVER_WORKED` (and `WALKED_AWAY`, `REJECTED`, `CANCELLED_ELSEWHERE`, `DRIFTED`) are
settled rather than spent: cancel, confirm terminal, report `UNFILLED` —
`test_never_worked_is_settled_not_spent` runs the same broker through both paths.
The one case that cannot be made retryable is a cancel that is never confirmed (C5's
stuck `Cancel Requested`), and **D would not fix it either**: D's re-arm also goes
through `DuplicateOrderGuard`, which blocks on the same live order. A working close at
the broker legitimately blocks a second close on every path. That case retires the
take and pages `watchdog_profit_target_retired` (`order_may_be_working: true`,
`exit_attempt_intact: true`) — check the broker before sending anything by hand.

**Four things the design left open, decided at build time:**

1. **Maximum debit is two ticks (0.10): one rung.** `min_concession_steps = 1` and
   `step_ticks = 1`, both pinned rather than read from `[watchdog]`, and the executor
   refuses to walk a take from any opening limit but one tick (`NOT_ATTEMPTED`, free).
   So the floor is -0.10 for every legal `exit_walk_away_pct`. Not the exit's 3-step
   minimum: that walks to 0.20, which §3 measured as worse than doing nothing. One
   rung lifts the modal 0.10 offer (§1: 91.7% of the zone); §10's conservative +$240
   is the one-rung-up case.
2. **Which walk outcomes settle and which retire** is a partition of `WalkOutcome`
   (`_PROFIT_SETTLE`, `_PROFIT_RETIRE`, plus the shared `FILLED`/`NO_ORDER_EXISTS`),
   pinned by a test so a new walk outcome cannot reach the profit path unclassified.
   `UNRESOLVED` retires: its "two working orders under one key" shape cannot be
   confirmed through one order id. `BLOCKED` retires: a close already exists.
   `ABORTED` retires: a halt must not add a round trip. A `REJECTED` ack with no order
   id is retryable at once — the ack is the broker's terminal word on the only order
   sent, and there is no id for the settle to confirm.
3. **Paging.** A take pages when it CLOSES the position (`watchdog_exit_filled`, the
   executor's usual page) and when it retires UNCONFIRMED. It does not page when
   unfilled, when its budget runs out with nothing at the broker (Tier 1
   `watchdog.profit_target_retired`, no page), or when it cannot be built
   (`NOT_ATTEMPTED`, Tier 2 only — never `_UNATTEMPTED`'s repeating page, which on a
   winning position is the 2026-09-22 afternoon the trigger's equality exists to
   prevent). It records `watchdog.triggered` Tier 1 on its rising edge and does not
   send `watchdog_triggered`, the stop-loss page.
4. **`/status` has a Profit target row**: the RUNNING switch (the only place a config
   promotion can be confirmed live — `cancel_enabled`'s precedent), takes this
   session, and each held position the target has retired, amber only when its order
   may be working. Never red: every emergency protection is intact on every row.

**One hazard the build found.** Sharing `_TRIGGERED` would let a profit episode
swallow a STOP's page: a take that goes unfilled leaves the edge risen, and a backstop
breach sustained on the next tick arrives as Tier 2 `triggered_still` with no
`watchdog_triggered` on the phone. The take has its own tag (`_PROFIT_TRIGGERED`);
`test_a_retired_take_leaves_the_stop_armed_and_its_page_unswallowed` is the sequence.
Also fixed on the way: a spent position's `still_triggered` now reads emergency reasons
only, so a spent spread that has come back to one tick no longer shows "still firing".

**A visible telemetry change for everyone, switch on or off.** Because the trigger is
always evaluated, `watchdog.check` carries `profit_target` in `true_reasons` whenever
it would fire (the replay input §11.6 wanted), and every degraded/impaired record and
`/status` row now lists `unknown_reasons: [credit_multiple, profit_target]` where it
used to list `credit_multiple` alone. The backstop is still live in exactly the same
cases; the list is longer because two triggers read the mid.

### 12.1 Review fixes (2026-09-23)

A code review of the build found five defects; all five are fixed, each pinned by a
test that fails on the unfixed line.

1. **A PARTIAL take orphaned its stub.** The walk cancels a partial's remainder and
   reports `FILLED`. The take's fill sits under the SAME `argus-close-*` key as every
   later close, and `DuplicateOrderGuard` blocks on FILLED, so the stub's stop and its
   flatten would both come back `BLOCKED` — while the watchdog believed the exit
   intact and said nothing until the stop fired. **The build's rule was one step too
   strong: a take that FILLS spends the close, as an emergency fill always has**
   (`_spend_attempt`, extracted unchanged from `_run_exit`). A full fill loses nothing
   (the row drops in seconds); a stub gets the repeating
   `watchdog_exit_spent_still_held` page with its mark and NO EXIT LEFT on `/status`.
   `filled_contracts` is rung-scoped and cannot tell partial from full, so the spend
   is on every fill. `test_the_guard_really_blocks_a_close_after_a_partial_take` pins
   the guard premise against the real executor: if the guard ever learns to size a
   close against what is held, that test fails and this spend should be revisited.
   **Not solved: the stub still needs a human** — the same state an emergency partial
   has always left. **Captured as an open decision, with the unattended-risk sizing and a
   recommended gate on promoting the switch:
   [closing the stub a partial close leaves](2026-09-23-closing-the-stub-a-partial-close-leaves.md).**
2. **A refusal re-fired every tick.** `NOT_ATTEMPTED` (a guard that cannot be reached)
   was free and immediate, and each attempt held the position in `_exiting` — skipped
   whole by `check_once` — for up to ~12s of guard burst, so an outage held the stop,
   backstop and flatten to one look per burst. Now a refusal stands the take down for
   `_PROFIT_REFUSAL_BACKOFF_S = 30` (a RATE: it costs no submission), the position is
   evaluated every tick in between, and the profit episode stays open through the
   stand-down so it is not re-recorded Tier 1 on each cycle.
3. **A broker REJECTION was silent.** Two rejected takes retired as "benign". A
   rejection is the broker refusing a CLOSE on this position (C5), and the stop sends
   the same kind of order, so it now pages `watchdog_profit_target_rejected` once per
   position with the broker's words — keyed on the walk's `REJECTED` outcome, never on
   its prose.
4. **The trigger could raise after the stop was decided.** `round_to_tick` throws for a
   finite mark around 1e22 (one corrupt leg quote), which would have discarded an
   evaluation whose credit stop was true. `at_profit_target` now checks the float band
   `(0, 2 ticks)` first — a strict superset of its true region, so the exact rounding
   still decides every boundary — which is total over every float and is also the cheap
   exit for every position not near its target.
5. **`for_profit_target` re-spelled the COA settle assembly** that `for_exit` says must
   exist once. Both now build on `WalkPolicy._exit_from_config`, and the profit policy
   is `replace(exit, step_ticks=1, min_concession_steps=1)`, with no second clamp log.

## Reported and deliberately NOT proposed here

1. **`entry_latest = "15:00"` against a 15:30 flatten permits a 31-minute trade.**
   §8 says it should not be taken. Pre-dates this pass and is not caused by it.
2. **`remaining` is unclamped above `daily_max_loss`** (§9). Coherent with the module's
   own induction — flagged for visibility, not as a defect.
3. **No backfill question arises.** Unlike the quoted-spread pass, everything this
   analysis needs is already recorded; `crossing_debit` since v0.4.3 is the input the
   trigger would read.

## Reproducing the numbers

All read-only, against the NAS snapshots, never a live profile:

```
sqlite3 "file:dev/nas/argus-20260921T163001.db?mode=ro"
```

- **§1** — `watchdog.check` payloads carrying both `cost_to_close` and
  `crossing_debit`; bucket the difference conditioned on `cost_to_close ≤ 0.10`.
- **§1 fills** — `order.fill` rows for `argus-close-20260917-put-7585-7580` and
  `argus-close-20260918-put-7595-7590`; net the two leg prices.
- **§2–§4** — group `watchdog.check` + `watchdog.check_spent` by `candidate_id` into
  mark paths; replay policy A and policy B over each. Timestamps are UTC; ET is −4
  throughout this window.
- **§6–§7** — `signal.candidate` payloads (`underlying_price`, `short.strike`,
  `short.delta`, `credit`); **exclude cert from 2026-09-03 onward**, where the lost
  5-point SPXW ladder contaminates strike selection.
- **§9** — `order.fill` rows for 2026-09-21; `process.start` confirms the session ran
  on v0.4.5.

**Admissibility.** `[marketdata] source = "live"` since 2026-07-20, so every quote and
index path above is real market data even in cert. What cert cannot supply is FILL
behaviour — which is why §2's absolute P&L levels are not cited as evidence about
Argus's edge, and only the policy DIFFERENCE is. Nothing in §5–§8 touches a fill at all.
