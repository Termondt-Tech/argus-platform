# Design — intersect the chain Argus prices with the chain Argus can route

**2026-09-09. DESIGN ONLY — nothing built. ON HOLD, pending tastytrade's answer.**
Raised by
the sandbox environment assessment (private record, not published) §0.

> **Hold, added at challenge the same day.** This mechanism has **no value in
> production** — both sessions hit the same API, so the intersection is the identity.
> It is a cert-throughput workaround for a vendor defect that may be fixed within
> days, and cert throughput is not a go-live gate. §6 below argued for shipping it
> always-on regardless; **that argument is withdrawn — see §6's own correction.**
> Do not implement until tastytrade answers the support email, and then only if the
> answer is "intentional, cert carries 10-point strikes only". If it is built then,
> build it **cert-scoped and explicitly temporary**, not as a permanent always-on
> mechanism. Everything below is preserved for that case.

## Recommendation in one line

`DXLinkChainSource.load` gains a second session, fetches today's expiry from the
**routing** broker as well as the **pricing** broker, and keeps only the contracts
present in both — applied before `subscribe`, always on in every environment, no
config key, and failing OPEN with a Tier 1 page when the routing fetch cannot be
answered.

---

## 1. The premise, compactly

Argus sources its chain from a **production** market-data session
(`src/argus/session.py:1243` hands `md_tt.market_data_session` to
`DXLinkChainSource.load`) and routes orders to **cert**. Nothing anywhere checks
that a strike Argus prices is a strike Argus can route.

Since ~2026-09-02 cert has stopped carrying SPXW strikes ≡ 5 (mod 10). On
2026-09-08 that cost 11 of 18 entry attempts, each dying at submit with
`instrument_validation_failed` — and because `entry.py` claims a `candidate_id`
before the guard runs, each refusal **permanently retires that strike pair** for the
process lifetime.

**What kind of defect this is, stated precisely** (an earlier draft of this section
overstated it, and the correction matters for how urgently it is treated).

It is **not** a bug that has been silently breaking since 2026-07-20, and it is
**not** currently a hole in live. It is an *unstated, unverified assumption*:
"every strike production lists, cert will accept." That assumption was TRUE from
2026-07-20 until 2026-09-01 and became false on 2026-09-02. The code never checked
it because, before the market-data split, there was nothing to check — chain and
orders both came from cert, one registry, true by construction.

**The split moved two things and only one of them was reasoned about.** Its purpose
was quotes: sandbox quotes are 15-minutes delayed and useless for judging signal
quality. But `get_option_chain` rides the same session, so the *instrument universe*
moved to production at the same time, as an unremarked side effect of a change scoped
as "where do prices come from". The distinction between a price source and a
tradability source did not exist in the design because until that day it did not need
to.

In **live** the intersection is the identity — one registry, assumption true by
construction — so this is not a live-flip blocker. It stays always-on there for the
reasons in §6, not because live has a known hole.

---

## 2. What the change is

In `load()`, between `filter_strike_window` and `subscribe`:

```
mapped     = production chain, today's expiry, mapped to OptionContract
windowed   = filter_strike_window(mapped, spot, window)
routable   = filter_routable(windowed, routable_symbols)     # NEW
subscribe(routable)
```

`routable_symbols` is a `frozenset[str]` of OCC symbols, built by running the same
`get_option_chain` fetch against the **routing** session and applying
`select_expiration` to it. `filter_routable` is a new pure function in
`signal/contracts.py`, beside `select_expiration` and `filter_strike_window`, which
are already pure for exactly this reason.

That is the whole change. Everything downstream is untouched.

---

## 3. D1 — the routable set comes from the instruments API, on the trading session

Three candidates:

**(a) `get_option_chain(trading_session, chain_symbol)` — RECOMMENDED.** The
identical call the pricing fetch already makes, against a different session. The
sandbox docs are explicit that instruments and option chains work in cert while
market data does not, so this is the one thing cert can still answer authoritatively.
It reuses the existing injectable `ChainFetcher` — **no new collaborator, no new
type**, just a second call.

**(b) A targeted `/instruments/equity-options?symbol[]=…` batch for the windowed
symbols.** More precise, but ±150 points on a 5-point ladder is ~122 contracts and
the docs publish a 100-symbol batch limit for market data; whether instruments share
it is undocumented. Trading correctness for an unverified batch bound is a bad
trade, and (a) costs one call either way.

**(c) Dry-run each candidate order.** Rejected outright. It puts a network call on
the per-tick entry path, it answers a *margin* question rather than an *existence*
one, and it is submission-shaped — the pipeline has exactly one submission boundary
and this would blur it.

### The session argument, and the role split

`load` becomes `load(session, *, routing_session, today)`. Two sessions in, one
`_fetch` used twice.

This hands `DXLinkChainSource` a trading session, which is a widening of what that
object can touch. It is acceptable and should be stated in the docstring: the
`SessionRole` split exists so that **nothing which can move money holds a possibly-live
market-data session** (`tt.trading_session` RAISES for a market-data session). The
converse — a read-only chain fetch on the trading session — is not what that rail
guards, and `tools/account.py` and `tools/loop_lag.py` already do read-only work on a
trading session.

The alternative, passing a pre-built `frozenset` down from `session.py`, keeps the
chain source ignorant but moves the fetch, its retry and its page into the largest
file in the repo, and splits one operation across two modules. Rejected on those
grounds, not on principle — it is a defensible second choice (§12 Q1).

---

## 4. D2 — the comparison is the OCC symbol, and an empty intersection is a bug, not a verdict

**Compare `OptionContract.symbol`** — the OCC string, which is precisely "what an
order references" per `contracts.py`'s own docstring. Not `streamer_symbol`, which is
a dxfeed identifier the routing broker has no reason to carry. Not a reconstructed
strike/right/expiry triple, which would be a second OCC implementation beside
`occ.py`, the sole parser.

**Exact string equality, no normalisation.** OCC symbols from one SDK mapping on both
sides should be byte-identical (`"SPXW  260908P07655000"`, two spaces). Normalising
whitespace to be safe is the kind of accommodation that hides a real format change.

**But an empty intersection must never be silently accepted.** A symbol-format
divergence and a genuinely empty routing chain are indistinguishable at the set
level, and both render as "nothing is tradeable today" — which is the *safe*
direction and therefore the one that fails quietly for a whole session. The rule:

> If the pricing chain is non-empty and the routing chain is non-empty and the
> intersection is empty, that is a Tier 1 page (`chain.routable_empty`), not a quiet
> zero.

A genuinely empty *routing* chain (cert has no expiry today) is a different, benign
condition and reads as such, because it is caught by the non-empty precondition.

---

## 5. D3 — the filter goes at `load()`, before `subscribe`

Not at `strategy._select`, and not in `snapshot()`.

At `load()`:

- unroutable contracts are never subscribed, so they cost no feed bandwidth and no
  headroom against the published 25,000-subscription / 10,000-changes-per-minute
  caps;
- `_admit`, `coverage()` and `snapshot()` need **no change at all** — the one
  admission rule stays the one admission rule, which is the property the 2026-08-24
  role split was careful to preserve;
- `strategy._select` needs no change, and cannot propose an unroutable strike
  because the leg is not in the snapshot to select;
- `ChainCoverage.total` keeps its existing meaning ("subscribed contracts") and
  simply counts a smaller, honest universe.

Filtering at selection would let unroutable legs into every snapshot and require the
strategy to learn a concept it has no business knowing. Filtering in `snapshot()`
would put a set lookup on the per-tick path for a fact that changes once a day.

**Consequence to accept knowingly:** the filter runs once, at session start, on a
chain that is itself loaded once. If the routing broker's instrument table is
populated *lazily during the day*, a strike that appears at 11:00 stays filtered
until tomorrow. The 2026-09-02 evidence (C7675 traded at 10:00, C7685 refused at
10:07) is consistent with static coverage rather than drift, so this is the right
trade for now — but it is an assumption, and it is the one to revisit if
tastytrade's answer says coverage is repopulated intraday (§12 Q3).

---

## 6. D4 — always on, never conditional on environment — **WITHDRAWN**

> **This section's recommendation is withdrawn (2026-09-09, at challenge).** The
> argument below leans on the repo's "shadow-vs-rest, not live-vs-rest" precedent.
> That precedent governs a **safety** boundary — one that *does something* in live and
> must therefore be proven out in cert before it gets there. This mechanism does
> nothing in live: same API, same registry, intersection is the identity. Shipping it
> always-on would add a network call and a new failure path to the live startup
> sequence to guard a hazard that cannot exist there, which is the opposite of the
> trade that precedent makes. The "different OAuth registrations might diverge"
> argument below is speculative and was doing more work than it could carry.
>
> If this is ever built, it is **cert-scoped**. The original reasoning is kept
> verbatim below so the withdrawal is legible rather than silently rewritten.

### Original reasoning, superseded

The tempting optimisation is to skip the routing fetch when the two sessions point at
the same API — i.e. under `environment = "live"` with `marketdata.source = "live"`,
where the intersection is the identity.

**Rejected.** A branch that means the live path never executes the code cert executes
is exactly the shape this repo refuses elsewhere: the reconciliation halt boundary is
"shadow-vs-rest, not live-vs-rest, because sandbox must prove the halt out before
live"; shadow's terminal action is one branch asserted by an AST test rather than a
family of environment special-cases. A conditional here would mean the first time the
intersection ever runs in live is the first live trading day.

It is also not free of value in live. The two production sessions are **different OAuth
registrations** (`TASTYTRADE_LIVE_*` for market data, the trading pair for orders),
and an entitlement difference between them would present as exactly this failure. The
identity case costs one instruments call per session start.

---

## 7. D5 — a failed routing fetch fails OPEN, loudly

What is this filter, in safety terms? **It is not a rail.** Proposing an unroutable
strike is not dangerous — it produces `no_order_exists`, a `mutation_error` carrying
the broker's prose, and a burned `candidate_id`. It is wasteful, not unsafe. Nothing
downstream is made less safe by the filter's absence, because the filter's absence is
today's behaviour.

Therefore:

- **Fail open.** A routing fetch that cannot be answered leaves the chain unfiltered
  and the session proceeds exactly as it does today. Failing closed would let one
  cert 502 at 09:40 — an ordinary event on that host — cost the entire trading day,
  which is a strictly worse outcome than the problem being solved.
- **Bounded burst first**, on the established shape: 3 attempts, 1s/2s, each under
  the broker HTTP timeout. Same reasoning as `_ask_guard` and reconcile's
  three-attempts-per-await — the bound is on a fetch that returns data, so a retry
  cannot mask a disagreement.
- **Tier 1 page on giving up** (`chain.routable_unknown`), with an alert. The day's
  entry statistics are not comparable to a filtered day, and that must be legible
  without reading the journal.
- **Never `_halt_process`, never `_end_entries_today`.** Both would be a monitor
  disabling trading over the failure of an efficiency filter — the 2026-08-28
  mistake ("a monitor disabled by the failure of an action it requested"), mirrored.

---

## 8. D6 — visibility

Three surfaces, all additive:

1. **The startup line.** `load()` already prints
   `subscribed N 0DTE SPXW contracts (+/-150 pts …)`. It gains the drop count:
   `… (M dropped: not routable at the broker)`. Silent when `M == 0`.
2. **A Tier 1 audit record** when `M > 0` — `chain.unroutable`, carrying the count,
   the count kept, and the dropped strikes (as strikes, not full OCC symbols; the
   strike is the readable unit and the record is once a day). Tier 1 rather than
   Tier 2 because it bounds what the whole day can trade, and one record per session
   costs nothing against a measured 0.2 Tier 1 records/sec.
3. **A `/status` row.** Recommended but separable (§12 Q4). If it lands it **must**
   join `tests/test_session_status_seams.py`'s `_SEAMS` sweep — the 2026-09-02
   under-copy record exists because a converter with `None` defaults cannot fail
   loudly, and this row would be built from a source that may gain fields.

---

## 9. D7 — no config key

No new key, in either direction. Not a threshold, not an on/off flag.

An off switch is a landmine of the documented kind: someone flips it during an
incident and the process silently reverts to burning candidate_ids, with nothing on
`/status` saying so. And a REQUIRED key forces a coupled config deploy — four of the
last five releases forced one, and `BLACKOUT_WARN_DAYS` was deliberately made a
module constant to avoid exactly this for a smaller feature.

**`src/` only. The image deploys alone.**

---

## 10. What this is NOT

Stated so none of it is added during implementation.

- **It does not touch the exit path.** Exits reference symbols from `positions` —
  strikes that were already routed, therefore already routable. Masking an exit
  symbol would mean refusing to close a live position because a lookup disagreed,
  which inverts the entire safety direction of the system. `ExitExecutor`,
  `LegQuotes` and the flatten see no change whatsoever.
- **It is not a guarantee, and the `no_order_exists` path stays exactly as it is.**
  The mask filters on what the routing broker's *instrument* API says exists. If the
  *order* API refuses something the instrument API lists, the mask cannot see it —
  and 2026-09-08's `put-7675-7670` may be precisely that case (5-wide with a ≡5 short
  leg, yet an order was created and then rejected). Submit-time refusal handling and
  `WalkResult.mutation_error` remain the backstop and must not be softened on the
  grounds that the mask exists.
- **It is not a fix for cert's missing strikes.** It stops Argus wasting trade ideas
  on them. The strikes come back when tastytrade restores them.
- **It is not a change to `signal.width`.** See §11.
- **It does not make the chain reload intraday.** That is a separate question with
  its own cost (a mid-session resubscribe), and the strike window is already fixed at
  load for the same reason.

---

## 11. The strategy adapts for free — and the economics change

Worth stating explicitly, because it is the strongest argument for this design over
the one-line `signal.width = 10.0` alternative.

`_find_long_leg` (`signal/strategy.py:370-391`) already takes "the nearest strike at
least `width` further OTM, no further than `max_width`", and `actual_width =
abs(short.strike - long.strike)` drives every downstream economic. Its own docstring
says why: *"Requiring an exact `width` match silently yields no signal whenever the
ladder is sparse."* **Argus was built for gapped ladders.** Filtering the unroutable
strikes turns cert's chain into a clean 10-point ladder, and the strategy picks the
10-away long leg with no config change and no code change.

The consequence is real and must be read into any cert number afterwards: at 10-wide
and a ~0.60 credit, max loss is ~$940 per contract against
`max_loss_per_position = 1250`, so `size_contracts` will select **1 contract at
10-wide** where it previously selected 2 at 5-wide. That is the rails working
correctly, not a defect — but cert will be trading a different economic profile than
the design intends, and fill statistics gathered under it are not comparable to
2026-09-01 and earlier. The welded invariant
(`max_concurrent_positions × max_loss_per_position ≤ daily_max_loss`) is untouched:
`max_loss_per_position` does not move.

---

## 12. Open questions — sign-off needed before implementation

1. **Does the chain source hold the routing session, or does `session.py` fetch and
   pass a `frozenset`?** Recommendation is the former (§3). This is the only question
   that changes the shape of the work.
2. **Fail open or fail closed on an unanswerable routing fetch?** Recommendation is
   OPEN with a Tier 1 page (§7). The counter-argument is that trading a day whose
   universe is unverified is what produced 2026-09-08 — but that argument makes cert's
   flakiness able to cancel a trading day, which is worse.
3. **Once per day, or refreshed?** Recommendation is once, at load (§5). Revisit if
   tastytrade's answer to the support email says cert instrument coverage repopulates
   intraday.
4. **Does `/status` get a row now, or later?** Recommendation yes, separable (§8).
5. **Should the empty-intersection page also refuse to start the session?** It is the
   one case where "nothing is routable" is more likely a bug than a fact.
   Recommendation is no — page loudly, proceed unfiltered, on the same reasoning as
   Q2 — but it is arguable and is not the same question as Q2.
6. **Ships alone, or bundled?** `src/` + tests only, no config key, so it is an
   image-only release and can go alone. Given it restores entry throughput, alone and
   soon is the recommendation.

---

## 13. Blast radius

### Source

- [ ] `src/argus/signal/contracts.py` — new pure `filter_routable(contracts,
      routable: frozenset[str]) -> list[OptionContract]`, plus a helper building the
      set from a fetched chain (`select_expiration` + `.symbol`). Module docstring
      gains one sentence on why the OCC symbol is the join key.
- [ ] `src/argus/signal/live_chain.py` — `load` takes `routing_session`; the bounded
      burst; the intersection; the drop count; the empty-intersection check. Class
      docstring gains a third numbered phase, or phase 1 gains the second fetch.
- [ ] `src/argus/session.py:1243` — pass `routing_session=tt.trading_session`.
- [ ] `src/argus/session.py` — startup line drop count; the two audit records; the
      alert wiring.
- [ ] `src/argus/status.py` — optional row (Q4); if added, join `_SEAMS`.

### Tests

- [ ] `filter_routable` pure unit tests, including the identity case.
- [ ] `load` with two injected fetchers: distinct chains → correct intersection;
      identical chains → identity; routing chain missing today's expiry → benign zero.
- [ ] **The empty-intersection page** — both chains non-empty, disjoint symbols →
      Tier 1 record, session proceeds.
- [ ] **The fail-open path** — routing fetcher raises on all 3 attempts → chain
      unfiltered, `chain.routable_unknown` written, no halt, no `EntriesEnded`.
- [ ] A test asserting the two fetches receive the **two different sessions**, in the
      right order. Getting them backwards is silently wrong in the worst way: masking
      production against production is a no-op, and pricing off cert yields no quotes
      at all.
- [ ] `tests/test_config_profiles.py` — no change expected (no key). Verify.

### Mutations that must fail the suite

- [ ] Swap the two sessions at the call site.
- [ ] Change the empty-intersection page into a silent zero.
- [ ] Make the fail-open path fail closed (return an empty chain).
- [ ] Apply the filter in `snapshot()` instead of `load()`.
- [ ] Compare `streamer_symbol` instead of `symbol`.

### Docs

- [ ] `docs/decisions/` record on implementation, and a pointer row in its README.
- [ ] `CLAUDE.md` — one sentence in the architecture note, alongside the existing
      "0DTE lives under the `SPXW` root" paragraph, which is where the two-symbol /
      two-registry subtleties already live.
- [ ] The assessment doc's §0 item 2 links here.

**Size: ~5 source files, ~6 test cases. No config key, no migration, no audit
category beyond two new record names, no change to any rail.**
