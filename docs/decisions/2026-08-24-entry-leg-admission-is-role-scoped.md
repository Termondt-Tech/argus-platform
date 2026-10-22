# Entry-leg admission is role-scoped: only the short leg needs a delta (2026-08-24)

*The 2026-08-21 freshness gate demanded a fresh quote AND fresh greeks of every leg.
That was right for the SHORT strike, which is chosen BY delta, and wrong for the
LONG one, which is chosen by strike distance and contributes no delta to anything.
The greeks feed runs behind the quote feed, so it was the long leg's requirement
that starved the session: 95.7% of loop iterations ended in `signal.skip` on a chain
whose every quote was current.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

Finding A1's residual, found by measurement rather than by reading — the first RTH
session running `[marketdata] source = "live"`. **This also answers the open question
A1 recorded and could not close**: the entry gate's admission rate over a full RTH
session is **~4.3%**, and it was a GREEKS artifact end to end, not a quotes artifact.

## The defect

`DXLinkChainSource._admit` applied one test to every leg:

```python
quote = self._market.latest(streamer_symbol)
if quote is None or self._market.is_stale(streamer_symbol, self._max_age_s):
    return None
greeks = self._market.greeks(streamer_symbol)
if greeks is None or is_stale(self._market.greeks_age(streamer_symbol), self._max_age_s):
    return None      # <- correct for one leg of two
return quote, greeks
```

**Delta is the only greek Argus reads**, and it is read in exactly one place:

```python
short_leg = min(legs, key=lambda o: abs(abs(o.delta) - self._cfg.target_short_delta))
```

`strategy.py:262`. The long leg is chosen by `_find_long_leg` — nearest strike in
`[width, max_width]` further OTM — and priced off its own bid/ask. It never touches
a delta, in selection, in the credit, in the width, or in max loss. Gating it on
greeks bought no safety at all and cost the entire ladder.

## The measurement

Droplet, `argus.db`, read-only queries, 2026-08-24 (the first live-quote RTH
session; every earlier session ran delayed cert quotes, where arrival age is
meaningless — the caveat A1 wrote down):

| Reading | Value |
|---|---|
| `signal.skip` (chain snapshot returned `None`) | **4,357 of 4,552 loop iterations — 95.7%** |
| `stale_g == 120` (EVERY greek in the 120-symbol chain stale) | 95% of 1,520 session heartbeats |
| `stale_q == 120` (every quote stale) | **zero, ever, all-time** |
| `with_quote` / `with_greeks` / `with_both` | 120/120 on every single heartbeat |
| `admissible > 0` | 76 of 1,520 heartbeats |
| Visible ladder when `admissible > 0` | median 78/120, p25 37/120 |

`signal.skip` had never fired before in the database's history.

Two cross-tabulations settle the attribution rather than merely suggesting it:
`admissible == 0 & stale_g < 120` never occurred, and `admissible > 0 & stale_g == 120`
never occurred. `admissible` was a **pure function of `stale_g`**. The data arrives
fine; it is simply older than 2s by the time greeks update.

## What it cost, in positions

The gate did not only blank the chain — when it did admit legs it admitted a
ladder full of holes, and the long-leg search walks OUT to the next VISIBLE strike.
Every recorded spread in `docs/reconciliation.txt` before this session (dozens,
across 08-03 / 08-04 / 08-10 / 08-21) is 5-wide at 2 contracts. 08-24 produced:

- the **first-ever 10-wide** spread (`20260824-put-7615-7605`),
- the **first-ever 1-contract** position, the same one,
- and a desk `GATE` block with two denial reasons that appear on **no prior day**:
  `position_max_loss_exceeded` ×8, `size_cap_exceeded` ×3.

The arithmetic is straight off `argus.toml` and the recorded credit. SPX quotes a
5-point ladder, so with 7610 gated out the search took 7605:

| | width | max loss/contract | contracts under `max_loss_per_position = 1250` |
|---|---|---|---|
| What was built | 10 | (10 − 0.85) × 100 = **915** | 1 |
| What was available | 5 | (5 − 0.85) × 100 = **415** | 3, capped at 2 by `max_contracts_per_position` |

And `max_width = 25.0`, so two or more consecutive holes reach widths whose max
loss exceeds the rail outright — (15 − 0.65) × 100 = 1435 > 1250. That is the
`position_max_loss_exceeded` count, not a strategy that suddenly wanted wide
spreads. **The rails behaved correctly throughout**; they were refusing candidates
the gate had deformed upstream.

## The fix: split by ROLE, not by threshold

`_admit` now takes a fresh **quote** as admission and attaches greeks only when
those are fresh too. A leg without them reaches the strategy with `delta=None`:
priceable, and never selectable as a short strike.

```python
quote = self._market.latest(streamer_symbol)
if quote is None or self._market.is_stale(streamer_symbol, self._max_age_s):
    return None                                  # no price -> no leg, in either role
greeks = self._market.greeks(streamer_symbol)
if greeks is None or is_stale(self._market.greeks_age(streamer_symbol), self._max_age_s):
    return AdmittedLeg(quote, None)              # priceable; not a short strike
return AdmittedLeg(quote, greeks)
```

**No new config key.** `staleness.market_data_max_age_s` stays 2.0 and stays the
bound for quotes and for short-leg greeks — what changed is WHICH fields a leg must
clear for WHICH job, never the number. So this image deploys alone, unlike the
coupled config+image deploy a `staleness.greeks_max_age_s` key would have forced.
Raising `market_data_max_age_s` was never on the table: it also governs exit-path
pricing and the gate's staleness rail, and CLAUDE.md is explicit that widening it
to fix a staleness symptom is the wrong move.

### Why the split lands in two modules

`_admit` cannot know a leg's role. Whether a leg is short or long is decided by
`CreditSpreadStrategy._select`, by comparing deltas across the whole admitted set —
so **the last point at which no leg has a role yet is the snapshot**, and the first
point at which every leg has one is strike selection. The freshness verdict
therefore travels WITH the leg (`OptionQuote.delta: float | None`), the same shape
as `captured_at` and `underlying_price` travelling with a candidate, and the refusal
is applied where the role is known:

```python
short_candidates = [o for o in legs if o.has_delta]      # A1's rail, scoped
if not short_candidates:
    return None, {"reason": NO_SHORT_LEG_WITH_FRESH_DELTA, ...}
short_leg = min(short_candidates, key=...)
long_leg = _find_long_leg(legs, ...)                      # EVERY tradeable leg
```

The alternative — having the strategy consult the market port for ages at selection
time — was rejected: `evaluate()` is pure and deterministic by design, testable
without live data, and that is worth more than saving a field.

### `None`, not NaN, and that is not a style choice

`SpreadSignal.underlying_price` uses NaN for "unevaluable", and this deliberately
does not follow it. That field is consumed by a **comparison**, where NaN fails
CLOSED — every comparison against NaN is False, so the drift check aborts. This
field is consumed by **`min()`**, where NaN fails OPEN for the identical reason: a
NaN key never loses a comparison either, so `min` RETURNS it rather than skipping
it. A NaN delta would have been silently selected as the short strike — precisely
the failure the field exists to prevent. `None` cannot be compared at all, so a
consumer that forgets to filter raises instead of mis-selecting. `has_delta` tests
`is not None and is_finite(...)`, because `OptionGreeks.delta` is a float straight
off the feed and a NaN can arrive there on its own.

`SpreadSignal.to_payload` gained a guard for the same reason `underlying_price` has
one: canonical audit hashing runs `json.dumps(..., allow_nan=False)`, and a refused
Tier 1 write halts trading. A long leg with no delta serializes as `null`.

## What this fix does NOT do, stated plainly

**It does not make the greeks feed fresher, and the short leg still requires a
fresh delta.** On a session shaped like 2026-08-24 — every greek stale on 95% of
samples — a short strike is selectable about as often as before. What changes:

1. **The spread built around a selectable short strike is now the right one.** The
   long leg comes off a full ladder, so 5-wide at 2 contracts instead of 10-wide at
   1. This is the load-bearing improvement and it is worth the change on its own.
2. **The starvation is now named instead of hidden.** It was reported as
   `signal.skip` / `no_chain`, which is indistinguishable from a quiet day. It is
   now `no_short_leg_with_fresh_delta`, ranked above `no_tradeable_legs` because a
   priceable ladder with no delta is a lagging-feed diagnosis and a different day
   from an empty chain.
3. **`regime` telemetry gets the full strip.** `compute_regime` reads prices and
   `tradeable` only, never delta, so its ATM-IV and skew estimates were being taken
   over whatever fraction of the ladder the greeks feed happened to allow — median
   78/120, p25 37/120. That is the dataset `[regime]` is eventually calibrated
   from, and it was thinned for no reason.

**The residual is now a measurable question with an instrument pointed at it**:
`ChainCoverage.short_admissible`, printed every 15s beside `admissible`. If
`short_admissible` sits near zero over a session with `admissible` full, the next
move is the DXLink greeks subscription, not this gate — and that is a different
investigation from this one.

## `ChainCoverage` grew a second count, and the promise is unchanged

```
before:  total with_quote with_greeks with_both admissible          stale_quote stale_greeks
after:   total with_quote with_greeks with_both admissible short_admissible stale_quote stale_greeks
```

`admissible` still means exactly what it meant — the count that equals what
`snapshot()` emits — and `short_admissible ⊆ admissible` is the subset a short
strike can be chosen from. **Both come from the same `_admit` call**, which is what
keeps the operator-facing number from drifting from the legs the strategy is
handed; `_admit` was NOT forked into two rules, and a test pins that neither count
can disagree with the snapshot.

Reading them together is the diagnosis, which is the entire reason there are two:

| `admissible` | `short_admissible` | Means |
|---|---|---|
| 0 | 0 | Quote problem. No signal can be built at all. |
| high | 0 | **Greeks problem.** Ladder fully priceable, no short strike selectable. This is 2026-08-24, and it had no representation before. |
| high | high | Normal. |

Both print sites in `session.py` are updated (the startup wait and the 15s
heartbeat), plus `tools/order_smoke.py`. The startup wait still breaks on
`admissible` rather than `short_admissible` — waiting on the stricter count would
burn the full 20s deadline every morning to learn nothing the heartbeat does not
report anyway — and a new warning branch names the greeks feed when the ladder is
priceable but no short strike can be chosen. `order_smoke` goes the other way and
waits for `short_admissible`, because it runs the strategy ONCE and needs a
candidate out of it.

## Coverage

`tests/test_chain_source.py` (extended, not forked) and `tests/test_strategy.py`.
The end-to-end claims run through the real `CreditSpreadStrategy`, because they
span both modules and neither states them alone.

| Case | Test |
|---|---|
| Fresh quote + stale greeks admitted, delta dropped not carried | `test_fresh_quote_with_stale_greeks_is_admitted_without_its_delta` |
| Missing greeks admitted with `delta=None`, never defaulted to 0.0 | `test_snapshot_admits_a_delta_less_contract_without_inventing_a_delta` |
| Stale quote excluded outright, whatever the greeks | `test_snapshot_skips_a_contract_with_no_quote_at_all` |
| Bound inclusive on each feed independently, and what breaching it costs per role | `test_freshness_bound_is_inclusive_on_both_feeds` (6 cases) |
| `snapshot()` and `coverage()` cannot disagree, on EITHER count | `test_admissible_equals_what_snapshot_emits` |
| `None` only when no leg is priceable — never for stale greeks | `test_snapshot_none_only_when_no_leg_is_priceable` |
| **The measured defect**: ladder hole → 10-wide where 5 was available | `test_a_stale_delta_costs_a_leg_its_short_role_and_nothing_else` |
| Fail-closed: the nearest-delta leg is refused as SHORT when its delta is stale, and used as LONG | `test_a_stale_delta_never_becomes_a_short_strike`, `test_a_leg_without_a_delta_is_never_the_short_leg` (with a fresh-delta control on the identical ladder) |
| Every delta stale → refuse under its own reason, ladder still priceable | `test_a_chain_with_no_fresh_delta_refuses_rather_than_guessing`, `test_no_short_leg_when_no_delta_is_fresh_anywhere` |
| A NaN delta is refused as a short leg (the case `min()` cannot reject for itself) | `test_a_nan_delta_is_refused_as_a_short_leg_too` |
| A delta-less long leg survives `allow_nan=False` in payload and near-miss | `test_a_delta_less_long_leg_stays_audit_safe` |
| Greeks diagnosis outranks an empty chain in the headline reason | `test_a_lagging_greeks_feed_outranks_an_empty_chain_in_the_headline` |

Three mutations run to prove the tests can fail:

| Mutation | Result |
|---|---|
| `_admit` returns `None` on stale greeks (the pre-fix rule) | 9 failures |
| `short_candidates = list(legs)` (fail-closed removed) | 8 failures |
| `_payload_delta` returns the raw delta (audit guard removed) | 1 failure, on the NaN case |

Full suite: 1,443 passed.

Two tie-free ladders were needed. `_put_chain()`'s -0.20 and -0.10 neighbours are
equidistant from a 0.15 target, and float arithmetic — not the rule under test —
settles which one `min()` returns. An assertion resting on that would pass or fail
on rounding.

## Not changed here

- **`execution/watchdog.py`'s `LegQuotes`.** It defaults a missing delta to 0.0 and
  that stays correct: the watchdog prices an EXISTING position and never selects on
  delta. The entry/exit divergence A1 recorded is now narrower — it applies to the
  short leg only — but it is still deliberate, not an oversight to tidy away.
- **`staleness.market_data_max_age_s`.** Value untouched, on both feeds.
- **`execution.walk_source` / `watchdog.trigger_source`.** This change feeds better
  candidates into the entry walk, which makes their second, data-backed pass more
  useful — it does not perform it.
- **The 2026-08-21 quote gate.** Fully intact. A leg with a stale quote is admitted
  in no role at all, which is what A1 was actually written to close.

---

## ADDENDUM 2026-08-27 — the attribution above is wrong, and the fix was incomplete

*Appended rather than rewritten. Everything measured above stands; one causal
claim does not, and the residual this record left open turned out to be a
nine-day regression rather than a feed property.*

**The attribution error.** This record frames the starvation as revealed by "the
first RTH session running `[marketdata] source = "live"`," and reasons that the
greeks lag is a property of the live feed that delayed cert quotes had hidden.
Git does not support the premise: `source = "live"` was set in commit `c009b7f`
on **2026-07-20** and has never been toggled (`git log -L` over the
`[marketdata]` block returns that commit and the initial build, nothing else).
The market-data source did not change on 08-24.

**What did change on 08-24 was this codebase.** v0.3.7 deployed, carrying A1 —
the entry-leg freshness gate. Before A1, `DXLinkChainSource._admit` tested
`is None` and nothing else. **No age check on greeks existed at any point before
that deploy.** The greeks feed was almost certainly always bursting on this
cadence; nothing had ever measured it, so 4,357 `signal.skip` on 08-24 was not a
feed that had gotten worse, it was a gate that had started asking.

**The daily series, which neither this record nor A1 plotted:**

| day | ticks | `no_short_leg_with_fresh_delta` | candidates | fills | running |
|---|---:|---:|---:|---:|---|
| 08-19 | 4548 | **0.0%** | 2846 | 6 | v0.3.6 |
| 08-20 | 4549 | **0.0%** | 3115 | 8 | v0.3.6 |
| 08-21 | 4546 | **0.0%** | 2538 | 12 | v0.3.6 |
| 08-24 | 195 | (4,357 `signal.skip`) | 102 | 4 | **v0.3.7** |
| 08-25 | 4187 | **77.0%** | 61 | 4 | v0.3.8/9 |
| 08-27 | 2577 | **88.2%** | 52 | 0 | v0.3.11 |

**The role split above was correct and insufficient.** It freed the long leg,
which was right and which this record measured properly. It deliberately kept
ONE threshold for both roles — "a role split, not a threshold change... no new
key, the image deploys alone" — on the reasoning that the number was not the
problem. The number was the other half of the problem, and leaving it cost
another three sessions at ~2-3% candidate flow.

**The evidence that settles the threshold, and that was sitting here all along.**
For the fifteen sessions before A1, short strikes were selected off deltas of
completely UNBOUNDED age, and the result was ordinary 5-wide 2-contract spreads
at 4–12 fills a day, every one in `docs/reconciliation.txt`. A stale delta
mis-selects a neighbouring strike; it cannot mis-price anything, because the
credit, width, max loss and walk limit all come off quotes bounded separately.
That is a natural experiment this record could have cited and did not.

**Superseded by** [the greeks selector bound](2026-08-27-greeks-selector-bound-and-order-rejection-visibility.md),
which gives greeks their own `staleness.greeks_max_age_s` (120.0) and adds the
per-tick age telemetry (`min_greeks_age_s`, `short.greeks_age_s`) that makes the
next bound a measurement instead of a third guess.

**The rule worth carrying out of this.** Both this record and its 2026-08-27
successor initially reasoned about a behaviour change from SOURCE — what the code
does, what changed in the diff — and both got the attribution wrong until someone
plotted the outcome BY DAY. `git diff` proves what a release changed. It cannot
prove when a behaviour changed, and the audit log has always been able to.
