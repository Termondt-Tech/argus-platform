# Entry legs are admitted on freshness, not merely on presence (2026-08-21)

*The entry path's only leg-admission test was `is None`. A wing that quoted once and
went quiet was joined into every snapshot for the rest of the day, at any age, and
the order that priced off it was self-consistent all the way to the exchange —
because every check downstream recomputed from the same stale legs.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

Finding A1. Found by reading, not by an incident. Pre-live-min-size blocker: at
live-min-size these are the quotes real capital is priced off.

## The defect

`DXLinkChainSource.snapshot()` joined each subscribed contract with its quote and
greeks, and skipped a contract only when one of them was **missing**:

```python
quote = self._market.latest(contract.streamer_symbol)
greeks = self._market.greeks(contract.streamer_symbol)
if quote is None or greeks is None:
    continue  # no market or no delta -> not selectable, skip it
```

The comment is right about what it does and silent about what it does not: `latest()`
returns the last quote ever seen, with no upper bound on its age. A leg that printed
at 09:31 and never again satisfied this test at 15:45.

## Why nothing downstream caught it

Three checks look like they might, and none of them measures the age of an option
leg's quote:

| Check | What it actually bounds |
|---|---|
| `PreTradeGate` / `RiskSnapshot.market_data_age_s` | `max(age(s) for s in reference_symbols)`, and `session.py` passes `reference_symbols=(underlying,)` — SPX spot alone |
| `execution.signal_max_age_s` (1.0s) | `ChainSnapshot.captured_at` → order build. `captured_at` is the snapshot's OWN build time, so this measures pipeline latency, ~0 by construction |
| `_assert_credit_matches_legs` | That the limit equals the credit recomputed **from the same legs** — self-consistency, which a stale pair satisfies perfectly |

`signal_max_age_s` is the one most likely to be mistaken for this bound, and its own
config comment says plainly that it is not: it exists so a stall *between gate-pass
and construction* is caught **without refetching a quote**. It never looked at quote
age, and the loader refuses a value above `market_data_max_age_s` precisely because
the looser rail would fire first.

So: `spread_mid` reads a credit off a stale quote, `size_contracts` sizes against it,
the gate approves because SPX itself is streaming fine, the credit assertion passes,
and the order goes out at a limit derived from a market that no longer exists.

## The asymmetry was already documented — one module short

`entry.py` had already reasoned about this exact gap, for the underlying:

> `LegQuotes.spot()` adds a staleness gate the chain source does not, which is the
> direction to err in.

The exit path has enforced a per-leg bound since chunk 4 (`LegQuotes.resolve`,
`watchdog.py:295`). `watchdog.py`'s own docstring states that
`market_data_max_age_s` "also governs entry pricing and the pre-trade gate's
staleness rail" — true of the rail, and until now not true of the legs. This was
not a new invariant to design; it was an existing one that stopped one module short
of the legs it was written to protect.

## The threshold: `staleness.market_data_max_age_s`, not a new key

Resolved from source rather than chosen:

- Its own definition already names this use — `config.py:96`: *"DXLink quote age past
  which entries are blocked."* Entries. The bound was always meant to reach here.
- It is the same bound the exit path applies per leg and the gate applies to the
  underlying, over **one feed**. A second number would mean the same quote is fresh
  enough to close a position and not fresh enough to open one, with nothing to
  explain the difference.
- `execution.signal_max_age_s` is structurally the wrong instrument (above), and is
  constrained to `<= market_data_max_age_s` anyway.
- No new REQUIRED config key, so — unlike `watchdog.exit_unattempted_max_s` — **this
  image deploys without a matching config change.**

`staleness.py`'s module docstring already scopes the rule to "market data, account
state, greeks, and the pre-trade gate", so greeks fall under the same bound by the
definition that already exists.

## Greeks are gated as tightly as quotes, and here entry differs from exit

`LegQuotes` tolerates missing greeks — `delta=greeks.delta if greeks is not None else
0.0` — because the watchdog prices an EXISTING position and never selects on delta.

Entry does. `strategy._select` picks the short strike by
`min(legs, key=lambda o: abs(abs(o.delta) - target_short_delta))`. A stale delta does
not degrade an entry the way a stale price does; it silently selects a **different
strike** and reports the sizing as correct. So on the entry path greeks get the full
bound, and the divergence from `LegQuotes` is deliberate rather than an oversight to
be tidied away later.

## The fix

One admission rule, `_admit`, called by both `snapshot()` and `coverage()` so the
number the operator reads cannot drift from the legs the strategy is handed. Quotes
go through the port's own `is_stale`; greeks through `argus.staleness.is_stale`
applied to `greeks_age` — both already implemented in both adapters, and until now
with zero production callers. Fail-closed comes for free: `+inf`, NaN and a
backwards clock all read stale.

`max_age_s` is a **required** constructor argument with no default. A default is how
one of the two call sites ends up ungated, which is the bug this argument exists to
close; a test pins it.

**Rejection costs a leg its STRIKE, not the tick.** This is the same shape as the
identity/price split the exit path settled on 2026-08-13: a stale leg drops out of
the snapshot, and `_select` takes the nearest-delta leg among those admitted, so the
strategy routes around it. Only an entirely stale chain yields `None`, which
`SignalLoop` records as an ordinary `signal.skip` (`no_chain`). Nothing halts.

## Admission rate: instrumented, NOT yet measured

Deep-OTM 0DTE wings quote sparsely — 14% of exit-path ticks breached this same 2s
bound on 2026-08-13 — so "does this gate starve entries?" is a fair question, and
**the honest answer today is that it is unmeasured.** It could not be answered from
the record: per-leg quote ages are written nowhere. `ChainCoverage` was
stdout/journald only, and `signal.candidate` carries strikes, deltas and mids but no
timestamps, so no admission rate can be reconstructed from any past session.

`ChainCoverage` therefore gained `admissible` / `stale_quote` / `stale_greeks`
alongside the existing presence counts, and the session heartbeat now prints them
every 15s. The gap `with_both - admissible` is the gate's cost, attributed to a thin
market versus a lagging greeks feed. Measure it over a full RTH session before entry
size is scaled.

Two priors worth holding while it is unmeasured, neither a substitute for the number:

- The 14% figure is from a **different population** — the legs of an open position,
  which for a 0.15-delta credit spread includes the long wing. The strategy selects
  its short strike near 0.15 delta, which quotes far more actively than a far wing.
  Expect the gate's cost to fall disproportionately on strikes the strategy was
  unlikely to pick.
- **The bound measures ARRIVAL age, not exchange-timestamp age.** A 15-min-delayed
  cert feed still updates `received_at` on every push, so sandbox will report a high
  admission rate and prove nothing about content freshness. That is the usual
  sandbox caveat, and it applies here with force: production reads
  `[marketdata] source = "live"`, and only that measurement counts.

## Coverage

`tests/test_chain_source.py`, over the real source and — for the routing claim — the
real `CreditSpreadStrategy`.

| Case | Test |
|---|---|
| Stale quote rejected, fresh sibling kept | `test_snapshot_rejects_leg_with_stale_quote` |
| Fresh quote + stale greeks rejected | `test_snapshot_rejects_fresh_quote_with_stale_greeks` |
| Fully fresh admitted | `test_snapshot_admits_fully_fresh_leg` |
| Bound is inclusive, bracketed just-under / exactly-at / just-over, on each feed independently | `test_freshness_bound_is_inclusive_on_both_feeds` (6 cases) |
| Sparse deep-OTM market admitted while current, rejected only once aged | `test_gate_keys_on_age_not_on_a_thin_deep_otm_market` |
| Whole chain stale → `None`, never a stale snapshot | `test_snapshot_none_when_the_whole_chain_is_stale` |
| `coverage()` cannot disagree with `snapshot()` | `test_admissible_equals_what_snapshot_emits` |
| Stale target strike routes to next-nearest delta, end-to-end | `test_stale_target_strike_is_routed_around_not_blocked` |
| `max_age_s` has no silent default | `test_max_age_is_required_and_has_no_silent_default` |

The bracket is the load-bearing one: `is_stale` accepts `age == max_age_s`, and an
off-by-one in either direction silently changes how much of the chain is tradeable.
The deep-OTM test is the counterweight — it pins that the gate keys on **age**, so a
thin market at 0.00 x 0.05 is admitted while it is current, and the zero-bid floor
keeps handling the economics.

## Not changed here

`DXLinkChainSource.spot()` still has no staleness gate, and that is deliberate: the
underlying is already bounded on the entry path by `PreTradeGate` through
`reference_symbols=(underlying,)`, and on the walk by `LegQuotes.spot`. The legs
were the unbounded input. Adding a third gate on spot would be a second definition
of the same rule with no gap to close.
