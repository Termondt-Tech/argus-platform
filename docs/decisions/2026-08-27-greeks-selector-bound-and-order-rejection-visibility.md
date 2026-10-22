# The greeks bound was a price bound wearing a selector's job, and a rejected order left no reason behind (2026-08-27)

*Zero fills on a day with 16 candidates. Three independent problems stacked, each
one throwing away most of what the stage above it produced — and the first of
them, `no_short_leg_with_fresh_delta`, is a **self-inflicted 40x collapse in
candidate flow dating to 2026-08-24**, the day A1's freshness gate first began
checking an age nothing had ever checked. It is the residual
`docs/decisions/2026-08-24-entry-leg-admission-is-role-scoped.md` named and left
open — and it cost fifteen sessions' worth of normal trading before anyone
plotted it by day.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

## The funnel

```
   565  in-window signal ticks (10:00-15:00 ET)
    16  candidates                    2.8%   <- the greeks bound
     8  cleared the gate              50%    <- account-stream staleness
     4  reached submission
     2  routed to the exchange
     0  fills                                <- rejected, reason unrecorded
```

## When this started, and what caused it

**The first pass at this got the attribution wrong, and the correction matters
more than the original claim.** The reasoning was: `git diff v0.3.8..HEAD --
src/argus/marketdata src/argus/signal src/argus/orchestrator.py
src/argus/staleness.py` is empty, `market_data_max_age_s` has never moved,
therefore the condition is longstanding and only its NAME changed in v0.3.8.
Every one of those facts is true and the conclusion drawn from them was false.
**"Not caused by the last three releases" is not "not new."**

The daily series settles it. Per trading day, `signal.none` + `signal.candidate`
ticks, the share reporting `no_short_leg_with_fresh_delta`, candidates produced,
and fills booked:

| day | ticks | no_delta | candidates | fills | running |
|---|---:|---:|---:|---:|---|
| 08-19 | 4548 | **0.0%** | 2846 | 6 | v0.3.6 |
| 08-20 | 4549 | **0.0%** | 3115 | 8 | v0.3.6 |
| 08-21 | 4546 | **0.0%** | 2538 | 12 | v0.3.6 |
| 08-24 | 195 | 0.0%* | 102 | 4 | **v0.3.7** |
| 08-25 | 4187 | **77.0%** | 61 | 4 | v0.3.8/9 |
| 08-27 | 2577 | **88.2%** | 52 | 0 | v0.3.11 |

\* 08-24 reads 0% only because the query counted `signal.none` + `signal.candidate`
and that day's starvation was still shaped as `signal.skip` — 4,357 skips of 4,552
iterations, and 4552 − 195 = 4357 exactly. Same condition, pre-v0.3.8 name.

**Candidates fell 2538 → 61, a 40x collapse, on one date.** That date is the
v0.3.7 deployment, and v0.3.7 is the release that carried **A1, the entry-leg
freshness gate** (commit `260b871`, 2026-08-21, tagged v0.3.7 on 2026-08-23).
Before A1, `DXLinkChainSource._admit` tested `is None` and nothing else: **there
was no age check on greeks at all, ever, until 08-24.** The greeks feed was
almost certainly always this slow. Nothing had looked.

### A correction to the 2026-08-24 record

That decision attributes the starvation's appearance to "the first RTH session
running `[marketdata] source = "live"`," and reasons from there that it is a
live-feed property revealed by switching sources. **Git does not support that
premise**: `source = "live"` was set in commit `c009b7f` on **2026-07-20** and
has never been toggled since (`git log -L` over the `[marketdata]` block returns
that commit and the initial build, nothing else). The feed source did not change
on 08-24. The GATE did. The measurements in that record stand — 95.7% skip,
`stale_g` binary against `admissible`, every quote stale never once occurring —
only the causal story around them needs this correction. Appended there as a
dated addendum rather than rewritten, per the convention that settled records are
corrected in the open.

### Fifteen sessions of evidence that a stale delta is tolerable

This is the part that changes the safety argument for the fix below, and it only
became visible once the attribution was corrected.

**For the three weeks before A1, Argus selected short strikes off deltas of
completely unbounded age** — no check existed — and produced ordinary 5-wide
2-contract spreads at 4/6/6/8/12 fills a day, every one of them in
`docs/reconciliation.txt`. That is a fifteen-session natural experiment for
"does trading on a stale delta produce bad spreads?", and the answer in the
record is no.

So the fix below is not a rail being loosened into the unknown. It is a bounded
version of a regime that demonstrably worked — 120s where there used to be
infinity — carrying per-tick telemetry that the unbounded era never had. The
evidence would arguably support going wider still; 120.0 is the conservative
choice, and `min_greeks_age_s` is now the instrument that can settle it from one
day of traffic rather than a second guess.

### What the 2026-08-24 role split did and did not do

It correctly stopped the LONG leg from needing a delta it is never chosen by,
and it correctly named the residual `no_short_leg_with_fresh_delta` instead of
letting it hide inside `signal.skip`. What it did not do — deliberately, and
recorded as such — is question the THRESHOLD both roles shared. That is what
follows.

## Finding 1: the greeks bound was priced like a quote

`_admit` gated both quote and greeks on `staleness.market_data_max_age_s = 2.0`.
That number's own justification in `argus.toml` is an argument about PRICES:
*"they drive pricing and should stream continuously, so a gap really is
staleness."* Delta is not a price. It is read in exactly one place —
the `min()` in `strategy._select` that picks the short strike — and contributes
nothing to the credit, the width, the max loss, the sizing, the gate, or the
walk's limit; every one of those comes off a quote independently bounded and
fresh throughout.

**Measured, 2026-08-27, `[marketdata] source = "live"`:** the greeks feed
refreshes the whole chain in synchronized bursts, not a steady trickle. Gaps
between bursts: **median 137s, p90 421s, max 452s.** Session coverage samples
(`short=` on the 15s status line) were bimodal — 258 zeros against non-zero
values clustered at ~60/122 and ~120/122 — the signature of a chain-wide sweep
caught mid-burst by a 15s sample, never a scattered drop. `signal.none` for the
day: 550 of 565 in-window ticks (97%) reported `no_short_leg_with_fresh_delta`
with the ladder fully priceable throughout (0 `signal.skip`, 0
`no_tradeable_legs`).

Ruled out before measuring: the SDK silently discarding `Greeks` events on
pydantic validation (`tastytrade.dxfeed.event.Event.from_stream` skips events
that fail validation, and a `"NaN"` in a required field like `price` or
`volatility` fails it). If that were the mechanism, admitted symbols would be a
scattered handful, not most-of-the-chain-or-none. `queue_depths` were 0
throughout and `reconnects: 0` — nothing was backing up or dying, the vendor's
own recalculation cadence is simply this slow.

**What a stale delta actually costs.** Calibrated against a real 2026-08-27
candidate (S=7702.67, K=7735, δ=0.1442, 351 min to settlement — backing σ√T out
of the vendor's own delta rather than assuming a day-count convention): γ ≈
0.00749 delta/pt, and one 5-point SPX strike is 0.0374 of delta.

| greeks age | 1σ move | 1σ selection error | in strikes |
|---:|---:|---:|---:|
| 2s *(the old bound)* | 0.30pt | 0.0022 | 0.06 |
| 30s | 1.14pt | 0.0086 | 0.23 |
| 60s | 1.62pt | 0.0121 | 0.32 |
| **120s** | **2.29pt** | **0.0171** | **0.46** |
| 300s | 3.62pt | 0.0271 | 0.72 |
| 420s *(the p90 gap)* | ~4.3pt | ~0.032 | ~0.86 |

At 2s, 97% of ticks were rejected to buy a fraction of one strike, against a
`target_short_delta` that is itself an uncalibrated shadow-mode tuning knob.

### The fix: `staleness.greeks_max_age_s`, a separate REQUIRED key

`_admit` (`signal/live_chain.py`) now takes two bounds. A leg is still admitted
on its quote alone (`market_data_max_age_s`, unchanged); greeks are attached, and
the leg becomes short-selectable, only when they also clear
`greeks_max_age_s`. `ChainCoverage` gained `min_greeks_age_s` — the freshest
greeks reading anywhere in the chain, `math.inf` when none exists — which is the
FEED, not the gate: unlike `stale_greeks`/`short_admissible`, it keeps reporting
once the bound is wide enough to admit everything, so it is what the NEXT bound
gets sized from. `OptionQuote.greeks_age_s` travels with every leg, whether or
not the delta cleared, for the same reason: the age of a REFUSED tick is exactly
the dataset a drift-based bound needs and `has_delta` alone destroys it.
`signal.none` now carries `min_greeks_age_s` per side; `signal.candidate` and
every credit near-miss carry `short.greeks_age_s`.

Set to **120.0**. Deliberately NOT sized to cover the p90 gap (421s, costing
0.86 of a strike at 1σ and 2.6 at 3σ) — 120s captures the bulk of the loss at
under half a strike of 1σ error, and no single TIME bound is right at both ends
of a 45s–452s distribution: sized for the median it goes blind through the
tail, sized for the tail it overpays on the median. **This is the interim, not
the destination.**

The right destination is a **drift-based bound**: admit a stale delta only while
`γ × |spot_now − spot_at_capture|` stays under a tolerance, rather than while an
age stays under a threshold. That is correct on a quiet tape (a seven-minute-old
delta is fine if nothing moved) and a fast one (a twenty-second-old delta is not,
if it did) — a timer is right in neither case. It needs `spot_at_greeks_capture`
threaded through `MarketDataPort` and both adapters, and its tolerance needs a
day of `min_greeks_age_s` / `short.greeks_age_s` traffic to size — hence the
telemetry landing first, in the same deploy, rather than the drift bound itself.

**Not cert-only, unlike the account bound below.** `[marketdata] source` is
already `"live"`; the burst cadence measured here is the production feed's own
behaviour, so 120s is the right number in live too, and `config.py` places no
environment ceiling on it.

Config validation refuses `greeks_max_age_s < market_data_max_age_s`: a leg is
admitted on its quote and only then considered for the short role, so a tighter
greeks bound would refuse nothing the quote bound had not refused already — it
would be `market_data_max_age_s` wearing a second name, and the 2026-08-24 role
split would be undone by config rather than by code.

## Finding 2: the account-stream bound was calibrated for a healthier cert

Of sixteen candidates, eight were denied at the gate — every single denial
`account_stream_stale`, alone. Cert's `/positions` was timing out often enough
that the REST liveness re-seed could not stamp the account stream at all: 245
`account.refresh_failed` records across 48 outage episodes in one session, worst
single outage 92.6s, against `account_stream_max_age_s = 30.0`. The stream
itself was healthy throughout — `connected: true`, `reconnects: 0`, queue
depths 0 — it was the *liveness proof* that kept expiring, not the liveness.

`argus.toml`'s own reasoning for the bound already argues for widening it in
this shape: *"the looser tolerance is safe because when account state actually
matters (positions open) the stream pushes marks continuously; the quiet case is
precisely when there's no position risk to be late on."* That was true all day —
book flat throughout — and the one field in the cached snapshot that carries
real information in cert, `open_spread_equivalent`, has a second live source
that does not depend on this stream: it takes the higher of the broker mirror
and `ledger_spread_count`, fed from Argus's own `positions`/`order_legs`.

### The fix: 30 → 120, fenced structurally to sandbox

`staleness.account_stream_max_age_s` is now `120.0`, sized to the day's worst
observed outage (92.6s) with margin. **This one IS cert-only**, unlike the
greeks bound — live's `/positions` has no equivalent latency problem, and this
rail is what stops an entry being sized and gated against an account picture
nothing has proven current. `config.py` refuses
`environment == "live" and account_stream_max_age_s > LIVE_MAX_ACCOUNT_STREAM_AGE_S`
(30.0), in the same seam the `audit.synchronous` durability refusal already
uses — a cert-only widening of a fail-closed rail is exactly the kind of thing
that rides into live by being forgotten, and CLAUDE.md's own standing-landmines
section exists because that has happened before.

All staleness invariants hold at the new values: `5 < 10 < 120`
(`account_refresh_timeout_s < account_refresh_s < account_stream_max_age_s`),
evict `60 > 10`.

**This did not move the needle today, and the funnel says why.** Widening it
converts denials into more entry attempts feeding a submission stage that
rejected 100% of what reached it — see Finding 3. Shipped anyway, in the same
deploy, because it costs nothing beyond the one restart the other two changes
already require, and it is real per Finding 2's own measurement.

## Finding 3: EVERY order the broker accepted was rejected, and Argus recorded no reason for any of them

*Corrected 2026-08-27 evening, from the day's full Slack trail. The first pass at
this section read a midday DB snapshot and said "two orders." The day ran to
sixteen trade ideas and **seven rejections** — a 100% refusal rate on everything
that reached the exchange, which is a different finding from two isolated ones.*

Sixteen ideas reached the guard or the walk. They split three ways:

| outcome | n | meaning |
|---|---:|---|
| `entry_rejected` | **7** | reached the broker; the broker refused |
| `entry_unresolved` | 5 | submit never landed (`timeout_error`, `empty_response`) |
| `entry_guard_unreachable_abandoned` | 4 | duplicate check unanswerable; idea retired |

The five unresolved are **the duplicate-submission rail working as designed** —
each one asked whether an order existed under its identifier, confirmed none did,
and stood down rather than assuming. Nothing to fix there. The four abandoned
guards are v0.3.11's retry doing its job: every one carries `"consecutive": 1`
against `"max_consecutive": 3`, so the streak never accumulated and the day never
ended. Under v0.3.10 the first of them (10:26 ET) would have called `_end_day`
and stopped the watchdog with ~4 hours of window left.

The seven rejections are the finding:

```
1422817  Routed 14:30:23.002 -> Rejected 14:30:23.313   steps: 0   call 7740/7745
1422841  Routed 14:34:37.000 -> Rejected 14:34:37.935   steps: 0   call 7735/7740
1423005  10:14 CT                                       steps: 0   call 7745/7750
1423158  11:33 CT                                       steps: 0   put  7710/7705
1423174  11:38 CT                                       steps: 0   call 7750/7755
1423275  12:13 CT                                       steps: 0   put  7725/7720
1423293  12:15 CT                                       steps: 0   call 7755/7760
```

Spanning 10:30–13:15 ET, both sides, every one sub-second and `steps: 0`. Not
intermittence — a sustained, deliberate refusal.

**And it is NEW as of this day.** 08-25 submitted 3 and filled 4; 08-21 submitted
7 and filled 12. 08-26 was a blackout with zero orders, and it is also the day
v0.3.10 (morning) and v0.3.11 (evening) deployed — so **2026-08-27 is the first
day any order was submitted under that code**, and it is 0-for-7. That adjacency
was checked rather than assumed: `git diff v0.3.9..v0.3.11 --
src/argus/execution/build.py broker.py loop.py zerobid.py` is **empty**. The only
`execution/` change across those tags is `entry.py`, entirely guard-retry work
with nothing between candidate and wire. The order shapes confirm it — 2
contracts, 5-wide, limits 0.50–0.90, `max_loss` 825–900, all under the 1250 rail,
structurally identical to what filled on 08-25. What Argus sends is unchanged;
what the broker does with it is not.

**The first time `Rejected` has ever fired** — CLAUDE.md's live-fire pass lists
rejections under NOT-YET-EXERCISED. And Argus recorded nothing about why. The
SDK's `PlacedOrder` carries `reject_reason: str | None` the whole time; grep
`reject` across `src/argus/` before this fix returns exactly one file,
`execution/fake.py`. `map_placed_order` mapped `status`, `replaces_order_id`,
`replacing_order_id` — and stopped. The broker explained itself seven times and
the mapping discarded every one.

Compounding it: the record that would have carried the reason,
`order.event`, was unconditionally Tier 2 — queued and droppable. Correct for a
status flap (the common case by volume, a live order re-sent on every change);
wrong for the terminal state of a trade idea, which in the common case has no
other record naming the broker's verdict at all.

### The fix: `Order.reject_reason`, and a Tier 1 promotion scoped to DEAD

`Order` gained `reject_reason: str | None = None`; `map_placed_order` reads it
via `getattr(placed, "reject_reason", None)`, duck-typed like every other field
there so a dry-run echo, a live order and a test stub all map safely. An empty
string (the API's shape for "no rejection") normalizes to `None` — `""` and
`None` must not read as two different states in a field whose whole question is
"was there a reason, and what was it." Bounded at 500 characters before it
reaches the canonical hasher: broker prose in a hash-chained audit payload is
not something to hand the serializer unmeasured.

`FillConsumer` now records `order.event` at **Tier 1** the first time an order
is seen `disposition == "dead"` (rejected, cancelled, or expired with nothing
filled) — deduped on `broker_order_id`, **never** `external_identifier`: a walk
reprices, so several rungs share one identifier, and deduping on that would
swallow the second rung's own rejection, the same "one identifier, many orders"
trap the market-crossing confirmation is scoped around. Repeats of an
already-recorded terminal order still carry the reason, degraded to Tier 2.
**`FILLED` is deliberately excluded** — terminal in the lifecycle sense, and per
`OrderDisposition`'s own docstring the one thing that must never be filed next
to a cancellation; its record is `order.fill`, on the applied path, and it never
reaches this branch.

Nothing decides on the string. `classify_order` reads `status` alone, unchanged
— a rejection reason is broker prose that can change wording without notice,
and a rail built to match on it would fail open the day it does. This field's
entire job is to be present for a human to read.

### Closed same evening: `reject_reason` was `'Session offline'`, all seven

*The `buying_power_effect: -8.08558` anomaly this section originally raised —
fees plus a dollar, on a vertical carrying ~$870 of defined risk — pointed at
margin or spread-recognition as the leading hypothesis. It was wrong, and the
reason it was checkable at all is the finding worth keeping: cert's order
history is a rolling 24-HOUR window (the same one the sandbox-reset rule is
scoped around), so the seven rejections were still readable that evening from
the existing order-read path, with the market closed and no order placed —
`dev/reject_reasons.py` (dev-only, gitignored), one read-only call to
`Account.get_live_orders`.*

**All seven carried the identical `reject_reason`: `'Session offline'`.**
Not a margin problem — `derivative_buying_power: 100925.54`,
`used_derivative_buying_power: 0.0`, `maintenance_requirement: 0.0`, against
orders needing ~$870. Not a construction problem — `Limit`/`Day`/size 2,
correct `Sell to Open`/`Buy to Open` legs, correct strikes and root, identical
in shape to what filled cleanly on 08-25. The `buying_power_effect` oddity was
cert reporting that field strangely on a rejected order, not evidence of
anything real.

**Read plainly: tastytrade's cert order-matching session was down for the whole
trading day**, and Argus asked seven times and was told the venue was shut seven
times. Combined with the same evening's 245 `/positions` timeouts, 5 submit
failures, and 4 unreachable duplicate-guard bursts — four different failure
shapes, one environment — this reads as a cert-side incident, not a code defect,
and follows the six-hour cert outage `docs/decisions/2026-08-26-account-refresh-off-the-event-loop.md`
already recorded two days earlier. Two consecutive bad cert days is worth a
support ticket; tastytrade may not know.

**What this does NOT settle.** `'Session offline'` is tastytrade's string, read
once, on one incident — not documented broker behaviour, and not something to
build a rail against without seeing it recur. `classify_order` and every other
consumer of `reject_reason` are correctly untouched: nothing decides on this
string, and this finding is a reason to keep it that way, not an exception to
carve out.

**A real gap it exposes, left for a future decision rather than fixed here:**
Argus has a rail for the broker disagreeing (halt) and one for the broker being
unreachable (v0.3.11's bounded retry). It has none for the broker answering
*"the venue itself is closed"* — the entry pipeline proposed into it seven
times over three hours with no back-off, spending seven `_attempted` claims and
seven Tier 1 pages on a condition no single retry can fix. The principled
version bounds on the COUNT of consecutive terminal rejections, never on
matching the reason string — the same shape v0.3.11 already used for
"broker unreachable," reused for "broker reachable but refusing everything."
→ recorded in `docs/decisions/README.md`'s open-decisions table.

**Also worth doing, not done here:** `WalkResult` and the `entry_rejected`
Slack alert carry Argus's own `"reason": "broker rejected the order"`, never
the broker's string — so today's finding took a dev-only read-only script
against the order history, not a glance at the phone. Threading
`reject_reason` from `Order` onto `WalkResult` closes that; it needs a
rebuild, and was deliberately not rushed into this deploy.

## Sequencing: capture the reason before widening the funnel

Raised in conversation and worth recording: whether `reject_reason` should ship
alone, ahead of the two staleness bounds, so nothing raises throughput into an
unread rejection. **Rejected in favor of one deploy** — in cert, more rejections
cost log lines and nothing else, and the reason lands on the first one whether
it is rejection #1 or #40. The four changes shipped together.

## What this does not do

It does not raise `market_data_max_age_s`. That bound is untouched, governs exit
pricing and the pre-trade gate's staleness rail, and CLAUDE.md is explicit that
widening it to fix a staleness symptom is the wrong move regardless of which
symptom.

It does not make the greeks feed faster, and a short strike still needs a real
delta — `has_delta` is unchanged as the sole admission test, pinned by a test
that a 9,999s-old delta still selects if `delta is not None`. What changes is
how much of the burst gap counts against it.

It does not explain the two rejections. It gives the next one a chance to.

## What still needs a day of production traffic

`min_greeks_age_s` and `short.greeks_age_s`, both landing in this deploy, are
what size the drift-bound tolerance — not a re-run of the estimation done here,
which disagreed with itself by 2-3x across three methods (a sampled duty cycle
at two different cadences, and inter-burst gaps) because a point-burst model
does not fit a sweep with real duration. A read-only cadence probe
(`dev/greeks_cadence.py`, gitignored) exists to measure per-symbol
inter-arrival directly rather than infer it, but was not run this pass — it
opens a second DXLink connection on the shared `TASTYTRADE_LIVE_*`
registration, and whether tastytrade rotates that refresh token on exchange is
undocumented in this repo.

Also queued, deliberately behind this: `NestedOptionChain` in place of
`get_option_chain` for the startup chain fetch. A real efficiency win
(`Strike` maps 1:1 onto `OptionContract`, plus `tick_sizes` and `settlement_type`
Argus currently hardcodes/assumes) — but `Option.active` / `is_closing_only` /
`stops_trading_at` exist only in the flat form and are the fields most likely to
explain a sub-second post-routing rejection like the two above. Waits on
`reject_reason` actually landing on a rejection before trading away access to
them.
