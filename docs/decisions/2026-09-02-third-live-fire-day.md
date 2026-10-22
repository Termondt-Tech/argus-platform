# The first v0.3.14 day (2026-09-02)

**Status: REVIEW PASS. Nothing fixed here; no code or config changed by this
record.** Four passes: `/status` + the Slack trail, then `tools.desk` /
`tools.mark`, then `tools.evaluability`, then **a direct audit-log read that
overturned one of the first pass's conclusions** — finding 6 was written as a
cert outage and is not one. That retraction is left in place rather than edited
away, because the wrong version is what `/status` and Slack support on their own.

**The headline: both of the day's `credit_multiple` stops fired on floating-point
noise — 2 of 2 — at breach margins of 4.4e-16 and 6.7e-16.** That is the same
defect `docs/decisions/2026-09-02-the-credit-stop-basis-and-its-comparison.md`
closed on `main` this morning, observed again on the deployed image the same day.
**The second headline is the correction to it: on this day's tape those stops
would have fired anyway, minutes later and at a worse price**, so D3 and D5 are
worth deploying as CORRECTNESS fixes and must not be scored as a $540 recovery.
The money went somewhere else, and the analysis below says where.

The image is **v0.3.14 (Grumium)**, deployed 2026-09-01 at 17:02 EDT and running
unbroken since (`started_at 2026-09-01T17:02:24`, `uptime_s 84737.1` at the 16:34
snapshot). Uptime covers **exactly one session**, so this is an unmixed read of
one code version — no PID split needed. It carries none of `main`'s unreleased
work: **not** the identifier-wide read at a walk's terminal branch (`90234b5`),
**not** the credit stop's epsilon (`e8ef993`, D3), **not** the realized entry
credit + migration 0005 (`2d8bc03`, D5), **not** the flatten allowance fix
(`adbb5b0`/`a81a3ad`, D6/D6b). Three of the four are exercised below.

## What the day was

| | |
|---|---|
| Candidates / gate decisions | 2610 / 2610 |
| `entry.finished` | 12 |
| Positions opened | 4 |
| `order.submit_attempt` / `submitted` / `submit_failed` | 16 / 8 / 8 |
| `walk.disconnected` | 8 |
| `watchdog.triggered` / `exit_started` / `exit_finished` | 4 / 4 / 4 |
| `account.refresh_failed` / `refresh_recovered` | 218 / 98 |
| `watchdog.check` | 36072 |
| `watchdog.unevaluable` / `evaluable_again` / `degraded` | 545 / 545 / 2658 |
| Realized / fees / total | −105.00 / −35.60 / **−140.60** |

Twelve trade ideas reached a walk; **four filled and eight died
`no_order_exists`** — of which **seven were cert refusing to trade the strike at
all** and one was a genuine broker timeout (finding 6; the distinction is
invisible on `/status`, where all eight read identically). Four positions, four
exits — two on `credit_multiple` (both call spreads, both in the first 20
minutes) and two on `FLATTEN_BEFORE_CLOSE` at 15:30. SPX settled **7655.08**
(from the feed, via `tools.mark --from-feed`).

The 16 walks reconcile exactly: 12 entries + 4 exits = 16 `submit_attempt`; the 8
failures are **all** on the entry side, and the 8 successes are the 4 entry fills
plus all 4 exits. `walk.disconnected: 8` matches `order.submit_failed: 8`
one-for-one.

## Nothing dangerous happened

Worth stating before the findings, because the day reads worse than it was. No
halt, no kill switch, no breaker, no reconciliation mismatch, no
`watchdog.unwatchable`, `tier2_dropped: 0`, `audit.failed: null`. Every one of the
eight `NO_ORDER_EXISTS` was a broker-**confirmed** empty answer — **no exposure
was taken and no order was left working.** Every position that opened was watched
by the watchdog for its whole life, and every position that opened was closed.

## Worked as designed

**`FLATTEN_BEFORE_CLOSE` fired to the second on both held positions**
(`seconds_to_flatten: -0.54869`) and closed both — *while both were unpriceable at
the moment it fired*. Second consecutive clean fire, and the first one where the
trigger had to work through a degraded feed on both positions at once. This is the
trigger nothing can blind, doing exactly that.

**v0.3.14's `exit_walk_min_steps = 1` produced its first rung.** `walk.step: 2` and
`order.replace_attempt` / `order.replaced` both 2; `desk` attributes 1 step to an
entry (`put-7625-7620`), which leaves **1 on an exit**. Under v0.3.13 both flatten
exits walked away after 0 steps. Consistent with the fix; **not conclusive** —
proving it needs the `walk.finished` payloads, which this pass did not pull.

**`watchdog.unwatchable` never paged.** `unwatchable_settled: 2` with
`unwatchable_still: 2` means both mid-close book windows were absorbed inside
`book_settle_s` and closed without a Tier 1. That allowance is sized right.

**`sustained_events: 0`** against `breach_count: 4` (max lag 0.3149s, the session-open
chain subscribe). The `trip_on_sustained_stall` landmine did not arm today. That is
a data point, **not** an argument for flipping it — persistence now makes a trip
permanent, which is the opposite of what a daily morning artifact should produce.

**Not exercised, still untested in anger:** kill-switch persistence (no restart;
`engaged: false`, `restored: false`) and `_end_entries_today` (no
`entry.entries_ended_today`, no `entry.aborted_day`).

## Findings

### 1. Both `credit_multiple` stops fired on floating-point noise (2 of 2)

| position | `cost_to_close` | `credit_stop` | breach margin |
|---|---|---|---|
| `call-7670-7675` | 1.4000000000000004 | 1.4 | **4.441e-16** |
| `call-7675-7680` | 1.3000000000000007 | 1.3 | **6.661e-16** |

The deployed comparison is the bare `elif cost_to_close >= credit_stop:` at
v0.3.14's `watchdog.py:577`. Both margins sit eleven orders of magnitude below the
0.025 grid a net mid can land on. Verified directly: with `PRICE_EPSILON = 1e-6`
neither fires.

This is the second consecutive day the defect has fired — 2026-09-01 had
`call-7670-7675` on 4.4e-16 — and today it is **2 of 2 rather than 1 of 5**.
`e8ef993` (D3) is the fix and is on `main`.

### 2. The limit-vs-realized `entry_credit` gap, on 2 of 4 filled positions

Both understating by exactly one tick, the same direction as every prior instance.

| position | runtime `entry_credit` (limit) | collected (from fills) | stop used | stop on realized basis |
|---|---|---|---|---|
| `call-7670-7675` | 0.70 | 0.70 | 1.40 | 1.40 |
| `call-7675-7680` | **0.65** | **0.70** | **1.30** | **1.40** |
| `put-7625-7620` | 0.45 | 0.45 | 0.90 | 0.90 |
| `put-7640-7630` | **0.75** | **0.80** | **1.50** | **1.60** |

`call-7675-7680` therefore ran a stop **0.10 — two ticks, 7% of the threshold —
tighter than its own fills justify.** Across 2026-09-01 and 2026-09-02 the count is
now **5 of 12 filled positions**, every one understating by exactly 0.05.
`2d8bc03` (D5, migration 0005) is the fix and is on `main`.

### 3. The D6 flatten-override defect reproduced, 2 of 2

Both flatten exits raised `watchdog_exit_unattempted` with
`"ValueError: net mid 0.0 rounds to 0.00 at a 0.05 tick — there is no valid limit
price for this spread"`. That is **4 of 4 across two days**, which settles the
question the design record framed as a prediction: a net mid rounding to 0.00 is
the flatten's **modal** regime, not its tail.

Harmless today for exactly the reason the record gives — neither position had spent
its ordinary attempt, so `NOT_ATTEMPTED` restored it. `put-7625-7620` closed inside
the same minute; `put-7640-7630` escalated once
(`watchdog_exit_still_unplaced`, `unresolved_for_s: 31.571`, bound 30.0s) and filled
at **15:34**, four minutes past its flatten clock. `adbb5b0` / `a81a3ad` are on
`main`.

**The refusal itself was correct at every step** — the system declined to invent a
price, retried on cadence, and filled when the market was priceable. Nothing about
finding 3 argues for pricing an unpriceable spread.

### 4. `staleness.market_data_max_age_s = 2.0` on the exit path is no longer only alert noise

One session produced, from that one entry-sized bound:

- **545 blind episodes** over 2 positions — 3.5% of ticks (1,262 of 36,072), p50
  2.0s, p90 5.0s, p99 10.6s, **max 21.9s**. Every episode lists `"no fresh
  underlying quote"` alongside the leg problems, so **both** market triggers
  (`credit_multiple` and `underlying_near_short_strike`) were dark for the whole
  window, not one of them. Only `FLATTEN_BEFORE_CLOSE` stayed live.
- **30 `watchdog.exit_unpriceable` rising edges** (plus 64 `_still`), flapping
  inside the 15:30–15:34 flatten window.
- One of the two reasons `put-7640-7630` sat unclosed for four minutes past 15:30.

`watchdog.degraded: 2658` confirms the backstop held throughout, and the 21.9s
tail is the real exposure — a 0DTE position with no market trigger evaluated —
not the Slack volume. `tools.evaluability` sizes the owed Fix D allowance at
**~20.0s** (4× p90, floored at 4× cadence), which is now supported by a second
day of measurement.

**The prohibition stands: this must not be fixed by raising
`market_data_max_age_s`**, which also governs entry pricing and the gate's
staleness rail.

### 5. `watchdog.leg_imbalance` paged falsely, and is the last book-shape page with no allowance

At 15:30 `put-7625-7620` reported `long_quantity: 2 / short_quantity: 1` — a
mid-fill snapshot of **its own closing order** — and `watchdog_exit_filled` with
`contracts: 2` landed in the same minute. `watchdog.py:2844` pages unconditionally
on `spread.imbalanced`, with no settle allowance and no evidence test.

It is **page-only**: an imbalanced spread stays in `scan.spreads` and keeps its
triggers, so no rail was impaired. But it is now the only member of this family
without the treatment the other two already got — `unwatchable` has
`book_settle_s`, `held_but_unbooked` has the "evidence, never a timer"
suppression keyed on Argus's own closing fill. The same evidence would cover this
page.

### 6. 7 of the 8 lost trade ideas were CERT INSTRUMENT COVERAGE, not a broker fault

**This finding was initially written as "cert was unhealthy — an 18-minute
outage." That reading was wrong, and the audit log says so plainly.** It is
recorded here as a correction because the wrong version is the one an operator
would reach for from `/status` alone, where every failure reads
`no_order_exists` with `reject_reason: null`.

Pulling `order.submit_failed` and `walk.disconnected` for the day gives **one**
genuine broker fault and **seven** deterministic refusals:

| ET | idea | failing symbol | role | broker error |
|---|---|---|---|---|
| 10:00:41 | `put-7615-7610` | — | — | `timeout_error: Request Timed Out` |
| 10:07:57 | `call-7680-7685` | C 7685 | LONG | `instrument_validation_failed` |
| 10:18:43 | `call-7685-7690` | C 7685 | SHORT | `instrument_validation_failed` |
| 10:19:50 | `put-7635-7630` | P 7635 | SHORT | `instrument_validation_failed` |
| 10:20:42 | `call-7690-7695` | C 7695 | LONG | `instrument_validation_failed` |
| 10:23:00 | `call-7695-7700` | C 7695 | SHORT | `instrument_validation_failed` |
| 10:27:02 | `call-7700-7705` | C 7705 | LONG | `instrument_validation_failed` |
| 10:35:49 | `put-7640-7635` | P 7635 | LONG | `instrument_validation_failed` |

`Trading of SPXW <symbol> is not supported`. **Four strikes — C7685, C7695,
C7705, P7635 — killed seven trade ideas**, each one twice: once as a short leg
and once as the long leg of the adjacent spread. Nothing was wrong with the
network, and nothing was wrong with Argus.

**The mechanism is structural, and it is the two-scoped-session design working as
specified.** `session.py:1243` loads the chain from `md_tt.market_data_session`,
and `[marketdata] source = "live"` — so the strike ladder is **production's**.
Orders route through `tt.trading_session`, which is **cert**. Cert's tradeable
instrument universe is a partial subset of production's, so Argus selects a
strike that demonstrably exists, quotes, and has greeks, and the order router
then refuses to trade it. The gap is arbitrary rather than patterned — cert
accepted 7670C, 7675C, 7680C, 7690C and 7700C but not 7685C, 7695C or 7705C —
and **only the FIRST invalid leg is named per rejection, so the true gap set may
be larger than these four.**

**Why it happened at 10:07 and not at 10:00 is the rally.** As spot ran 7647.79 →
7673.81, the 0.15-delta short strike marched up the ladder (7670 → 7675 → 7680 →
7685 → 7690 → 7695 → 7700) and walked straight into cert's missing strikes. The
failures are a *consequence of the tape*, not a coincidence in time — which is
exactly why the clustering looked like an outage.

**Three consequences, in order of importance:**

1. **The real submit-failure rate is 1 in 12 (8%), not 8 in 12 (67%).** Every
   conclusion resting on the larger number is void, including this record's own
   first draft of the "aperture" section below.
2. **It disappears at the live flip**, when both sessions are production and the
   two universes are one universe. It must NOT be treated as a reliability defect
   to fix before scaling — there is nothing here to fix in `src/`.
3. **Cert live-fire has a strike-biased entry sample.** Which trade ideas reach
   the market in cert is filtered by cert's instrument coverage, and that filter
   is correlated with direction (it took out the call ladder during a rally).
   This belongs alongside "sandbox proves API contracts and wiring, never
   realistic fill behaviour" as a standing limit on what cert can measure.

**The defect worth naming is the REPORTING, not the failure.** A deterministic
`instrument_validation_failed` is routed through the DISCONNECT path — it records
`walk.disconnected`, pays a reconnect and a back-off, and resolves as
`NO_ORDER_EXISTS`, an outcome whose text is *"submit failed and no order exists
under this identifier."* That is machinery built for a transient network fault,
spent on a refusal that can never succeed. On the page it is worse: the alert
carries `reject_reason: null`, because v0.3.12's plumbing reads
`PlacedOrder.reject_reason` off a REJECTED order and this never became an order
at all. **So a permanent instrument refusal and a transient timeout are
indistinguishable on `/status` and in Slack, and the broker's own prose — which
names the exact symbol — sits in `order.submit_failed.error` and never reaches
the operator.** This is the same shape as v0.3.12's rejection-visibility fix and
D6b's "name the refusal": a refusal being reported as an absence. Filed as a
follow-up, not fixed here.

> **RESOLVED 2026-09-04**, after 2026-09-03 made it the day's dominant failure
> mode — **5 of 7 entry attempts (71%)**, and by then it had also cost the exit
> path a send from a budget of three. `WalkResult.mutation_error` now carries the
> causing exception (type + message, bounded) from `_recover`'s terminal branch,
> where it had been in scope and discarded all along; it reaches `walk.finished`,
> `entry.finished`, `ExitReport` and the four per-position alert surfaces, and
> `_recover` is shared so a refused REPRICE carries it too. Purely reporting — the
> outcome, the retry set, the one-attempt rule and the entry/exit asymmetry
> described just below are all unchanged, and an AST test refuses any comparison or
> string match on the field, because deciding permanent-vs-transient from broker
> prose is the tempting wrong fix.
> → [a submit refusal is not an absence](2026-09-04-a-refusal-is-not-an-absence.md)

Separately, and unrelated to the above: `account.refresh_failed: 218` (improved
from 498 on 2026-09-01) and `watchdog.working_orders_unread: 15`, each one
blinding P4's working-order count and P5's residual recheck together.

The entry/exit asymmetry held as designed: `NO_ORDER_EXISTS` is in `entry.py`'s
`_ALERTING_OUTCOMES` and is **not** retried, while the exit path retries it
through `DuplicateOrderGuard`. Today strengthens that asymmetry rather than
challenging it — seven of the eight were refusals a retry could never satisfy,
and the one-attempt-per-`candidate_id` rule correctly retired each strike pair
instead of grinding on it.

### 7. Memory

RSS 109.0 → 180.9 MB over 23.5h (`rss_growth_bytes: 71880704`), against 3 gen2
collections. **The prior +80 MB / 51.7h reading is not comparable** — that window
spanned a weekend, this one is one session plus 17h idle. ~72 MB per session-day
at 4 GB is not urgent, but it keeps the pre-live-flip look on the list with a
second data point.

## What this actually cost, honestly

`tools.mark` reports `exiting early cost 555.00`, of which **$540 is the two call
spreads and $15 is the flatten paying the closing tick on the two puts.** The $15
is insurance working correctly and cheaply. **The $540 is not recoverable by D3
and D5, and this record should be the place that says so** before the number gets
quoted as a fix's value.

Both stops fired at a breach margin of ~5e-16 — meaning `cost_to_close` was **at**
2× credit to within float noise. That is a genuine touch of the threshold, not a
spurious spike. And the tape kept going: spot was 7658.14 when `call-7670-7675`
stopped at 10:07, then 7662.41 (10:18) → 7665.52 (10:20) → 7669.37 (10:23) →
7672.22 (10:27) → **7673.81 (10:35)**, taking the 7670 short strike **4 points
ITM**.

So with D3 deployed, that stop fires **one tick later at a marginally worse
price**, not never. With D5, `call-7675-7680`'s stop moves 1.30 → 1.40 — a real
two-tick difference — but spot came within 1.2 points of *that* short strike, so
it almost certainly breaches too.

**D3 and D5 are correctness fixes, not P&L fixes.** Their value is that a rail
fires on a price rather than on noise, and that the number it fires against is the
one the fills collected. On a day where the market kept moving they change the
timing and not the outcome. The reason to deploy them is stated at the end, and it
is not this $540.

Fees are worth one line: **$1.27 per option contract, ≈$10.16 round-trip on a
2-contract vertical — 7.3% of max profit per position, and 34% of today's realized
loss.** That is a scale argument, not a config one.

## The question the day actually raises: is `credit_multiple = 2.0` right for 0DTE?

The arithmetic is uncomfortable and should be written down.

- A 5-wide at 0.70 credit has max profit 0.70 and max loss 4.30. A 2× stop
  realizes **−0.70**. That is **1:1 at the stop** — >50% of positions must avoid
  it just to break even before fees.
- `call-7670-7675` doubled on a **10.3-point** SPX move (7647.79 → 7658.14). At
  7650 with VIX ~15 that is roughly **0.14 sigma**. SPX does 10 points in seven
  minutes routinely.
- At **both** stops the short strike was still **~12 points OTM**, and
  `underlying_buffer_points = 5.0` — the trigger actually keyed to danger — was
  seven points from arming.

`trigger_source` predicted the ordering: *"credit_multiple should almost always
bind first."* It does, by a wide margin. The consequence worth naming is that
**the liquidity backstop is the trigger with the sound economics and the primary
stop is the one firing on ordinary intraday noise.** A 2× credit stop is a
percentage-of-premium stop, and at 0DTE premium is small and gamma-dominated, so
it triggers at a distance from the strike that is still large.

Raw stop rate across the two live-fire days: **7 of 12 filled positions (58%)**.
At 1:1 that is structurally losing.

**Do not act on that number, and this is the load-bearing conclusion of the
pass.** Four of the seven stops were mechanical, not market — two on float noise
today, and on 2026-09-01 one on 4.4e-16 and one on a limit basis 0.05 low.
More decisively: **the entire two-day sample is `entry_credit_basis = limit`**,
which is precisely the category `CLAUDE.md` and
`2026-09-02-the-credit-stop-basis-and-its-comparison.md` already **exclude** from
`credit_multiple` conclusions. Tuning a strategy parameter to compensate for a
comparison bug and a systematically-tight basis is the wrong move in the most
expensive direction.

**Both days were also trending-then-reverting** — the single regime where a stop is
maximally wrong. Two in a row is evidence about two days, not about the stop.

Raising `credit_multiple` to 2.5 is a **config flip, not a deploy**, and 2.5 is
the top of the range `trigger_source` already documents as practitioner-supported.
It is the obvious candidate for the owed data-backed pass. **It should not be
flipped until D5 has produced a sample of `entry_credit_basis = realized`
positions.**

## The aperture: this day is capacity-bound, not signal-bound

**2,387 of 2,610 gate decisions cite `concurrent_limit`.** (The denial reasons
overlap — 223 + 2387 + 179 + 148 + 9 + 7 sums past the 2,610 decisions — so they
are reason-frequency, not a partition.) Both slots were occupied from 10:37 to
15:30: **4h53m of the 5h entry window at zero capacity.** All twelve entry attempts
happened inside 37 minutes.

Two things follow.

**By construction this is a first-hour strategy.** With `max_concurrent_positions
= 2` and no exit before the 15:30 flatten, entries concentrate in the opening —
which is the highest-gamma part of the 0DTE session and the part where a 2× credit
stop is most exposed. Both stopped positions were opened in the first 90 seconds.

**The cap absorbed the lost ideas, whatever their cause.** Of the eight failures,
seven would have been `concurrent_limit`-denied anyway; the measurable cost was
one position entered ~19 minutes later than it might have been. **This is true
regardless of finding 6's cause and does not depend on the retracted "outage"
reading.**

**Retracted:** an earlier draft of this section concluded *"the cap is masking a
67% submit-failure rate, so fix submit reliability before raising
`max_concurrent_positions`."* Finding 6 shows the real rate is **1 in 12**, and
that the other seven are a cert-only instrument gap that does not exist in live.
There is no submit-reliability problem to sequence ahead of the cap. The cap
remains welded to `daily_max_loss` (2 × 1250 = 2500, exactly), and that — not
reliability — is what governs moving it.

Every signal-quality reading today is through a ~9% aperture, and finding 6 adds
a second filter on top of it: the ideas that did reach the market were also
selected by cert's instrument coverage, which took out the call ladder during a
rally. Both caveats govern everything in this section.

## `signal.max_width = 25.0` is unreachable

At `max_loss_per_position = 1250.0`, a 15-wide spread is $1,500 of defined risk at
**one** contract and can never pass the gate; SPX's near-the-money ladder is
5-point, so 15/20/25-wide candidates are all structurally undeliverable. The
widest tradeable spread is **10-wide at 1 contract** — today's `put-7640-7630`,
`max_loss` 920. That is what the **148 `position_max_loss_exceeded`** denials are:
candidates the gate must refuse, built fresh every day.

The rails behaved correctly throughout (tightest governs; the sizer reduced
`put-7640-7630` to 1 contract and the gate accepted it). `max_width = 10.0` would
change nothing that can currently trade.

**Ranked low deliberately**, because it couples `signal.max_width` to
`risk.max_loss_per_position` — raise the risk cap at scale and `max_width`
silently becomes the binding constraint instead of the rail. Whether removing
148 daily wasted decisions is worth a fourth welded config pair is Richard's call,
not an obvious yes.

## Recorded, not actionable: the skew at a fixed short delta

The market-data source is `live` (production quotes, not cert's delayed feed), so
the *quoted* credit at candidate time is real market data even though the fills
are not. At `target_short_delta = 0.15` on 2026-09-02:

- **Calls**: 0.70 on a 5-wide at 22 points OTM (10:00:20), 0.70 at 17 points
  (10:01:26), then 0.70 / 0.70 / 0.625 / 0.60 up the ladder through 10:27 —
  **~14% of width**.
- **Puts**: 0.50 at 33 points OTM, 0.45–0.55 on 5-wides at 33–41 points OTM;
  0.80 on a 10-wide — **~8–11% of width**.

At the same nominal delta the call side paid **~1.4–1.5× per point of width, at
roughly half the distance from spot** — ordinary equity skew, showing up where it
should. The two positions that got tested were both calls. n=1 day, so this is
telemetry, not a calibration.

**It is specifically not an argument for adding a `[regime]` section**, which is a
standing landmine: the skew/IV gate is deliberately inert and adding the section
is a live behaviour change.

## The read that closed finding 6

`walk.disconnected` and `order.submit_failed` are Tier 1 and carry `call`,
`error_type` and the broker's `error` prose; **none of them appears on
`/status`**, where all eight failures render identically as `no_order_exists`
with `reject_reason: null`. This pass drew the wrong conclusion from `/status`
and the Slack trail alone, and only the audit log corrected it — which is the
argument for the reporting follow-up in finding 6, and the reason this query is
recorded rather than just its answer.

**There is no `sqlite3` binary in the image** — the runtime is stdlib-only Python
apart from the tastytrade SDK, so an ad-hoc read goes through `python -c` and the
`sqlite3` module, never the CLI. The DB is at **`/data/argus.db`**: the compose
volume is `argus-state:/data`, the Dockerfile's `WORKDIR` is `/data`, and
`audit.db_path` is deliberately RELATIVE. `ts` is **UTC**, and an ET session
(13:40–20:00Z) falls inside the same UTC date, so a `LIKE` on the ET date is
correct for a daytime read and would NOT be for anything overnight.

`order.submit_failed` is queried alongside it on purpose: `walk.disconnected` is
the retry, `submit_failed` is the outcome, and only the latter carries the
broker's prose naming the offending SYMBOL — which is the whole of finding 6.
(`call` is absent from the `submit_failed` payload — `None` there is expected,
not a missing field.)

```bash
sudo docker compose -f /opt/argus/docker-compose.yml run --rm argus python -c '
import sqlite3, json, collections
db = sqlite3.connect("file:/data/argus.db?mode=ro", uri=True)
q = "SELECT seq, ts, category, payload FROM audit_log WHERE category IN (?,?) AND ts LIKE ? ORDER BY seq"
rows = db.execute(q, ("walk.disconnected", "order.submit_failed", "2026-09-02%")).fetchall()
tally = collections.Counter()
for seq, ts, cat, payload in rows:
    p = json.loads(payload)
    call, et = p.get("call"), p.get("error_type")
    tally[(cat, call, et)] += 1
    print("%7d %s %-20s %-34s call=%-16s attempt=%-4s %-24s | %s" % (
        seq, ts[11:23], cat, (p.get("external_identifier") or "-"),
        call, p.get("attempt"), et, (p.get("error") or "")[:90]))
print()
for (cat, call, et), n in tally.most_common():
    print("%4d  %-20s call=%-16s %s" % (n, cat, call, et))
print(len(rows), "records")
'
```

The program contains no `$`, no backslash and no single quote, so the outer single
quotes make it paste-safe. `mode=ro` is on the connection, per the standing rule.

## Recommendation

**Tag and deploy v0.3.15 (Alnasl).** Three of its four fixes were exercised today
— D3 twice (finding 1), D5 twice (finding 2), D6/D6b twice (finding 3). Only
`90234b5` was not: there were 2 replaces and both succeeded, with no rejection in
the trail.

It is `src/`-only, adds no config key, and migration 0005 is additive and runs
from the image at startup — **the first uncoupled deploy since v0.3.10**, after
four consecutive releases that each forced a config move.

**Deploy it for the basis, not for today's dollars.** The $540 is not recoverable
(see above). What *is* at stake is that until D5 is running, `credit_multiple`
cannot be tuned from any position in the record — and `credit_multiple` is where
the money is going.

## Follow-ups this pass opened, neither for v0.3.15

- **The owed Fix D settle allowance** for `watchdog.unevaluable`, now sized at
  ~20.0s from a second day of measurement, and now known to have delayed a real
  close rather than only generating pages. A new `[watchdog]` key, TIME-based on
  wall time since first observation, suppressed observations still writing a Tier
  2 trail, `0` disabling it. **It must not reuse `book_settle_s`** — different
  condition, different urgency.
- **A settle allowance (or the `held_but_unbooked` evidence test) for
  `watchdog.leg_imbalance`**, the last book-shape page firing unguarded during
  Argus's own closing fill.
- **A submit-time refusal must not report as an absence** (finding 6). The
  broker's prose on `order.submit_failed` — which names the exact symbol — never
  reaches `/status` or the alert, so `instrument_validation_failed` and
  `timeout_error` are indistinguishable to an operator, and both arrive as
  `no_order_exists` with `reject_reason: null` after paying a reconnect and a
  back-off built for a transient. Same shape as v0.3.12's rejection-visibility
  fix and D6b's "name the refusal." **Cert-only in its current cause, but the
  reporting gap is environment-independent** — a live instrument halt would read
  exactly the same way.
