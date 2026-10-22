# Design — recording the quoted spread

**Status: BUILT 2026-09-15. All nine §9 decisions taken as written, no
amendments.** The normative record is
[`docs/decisions/2026-09-15-recording-the-quoted-spread.md`](../decisions/2026-09-15-recording-the-quoted-spread.md).

**Two implementation-level notes this document did not spell out**, both recorded
in the decision. (1) §3.5 names `cost_to_close` and §6 asks for a test
*"parametrized over every numeric field"* — the two are only consistent if the
guard is applied uniformly, so `credit_stop`, `spot` and `seconds_to_flatten` on
`TriggerEvaluation.to_payload`, and `strike` / `mid` / `credit` / `width` /
`max_loss_per_contract` / `max_profit_per_contract` on `SpreadSignal.to_payload`,
took it in the same pass. Behaviour-neutral on every finite input; it is §3.5's own
rule (*a numeric reaching a payload is guarded at the boundary regardless of what
the producing code believes*) applied to the whole boundary rather than to one
field. (2) §2.4 lists three spellings and there was a **fourth**, `_payload_age`,
in the same shape — collapsed too, or §6's "no second wrapper" test would have
failed on the file it was written to protect. `entry.finished` also inherits the
leg fields through `markable_payload`; §2.11 named only the other two writers.

Origin: finding 12 of the
[2026-09-15 external research review](../../RESULTS.md),
its top recommendation. That review read four outside sources against Argus and
found that **Vilkov (2026)'s central implementation result — that gross-versus-net
is the difference between a positive and a negative unconditional edge for most
0DTE structures — lands on the one quantity Argus has never recorded.**

Related, and deliberately not bundled: finding 13 (credit-at-fixed-delta by
minute-of-day) is pure analysis over data that already exists and needs no code.
§7.3 explains why the two are halves of one test and still ship separately.

---

## 0. What this changes, in one paragraph

Argus records the **mid** of every leg it has ever selected and has never once
recorded the **spread**. `SpreadSignal.to_payload` writes `floored_mid` per leg
and not `bid`/`ask`; `TriggerEvaluation.to_payload` holds a `SpreadMarket` that
carries both legs' bid and ask and serializes only `cost_to_close`, the floored
mid. So the log can answer *what did Argus think this spread was worth* and
cannot answer *what would it have cost to trade* — which is the difference
between a mark and a price, and the difference Vilkov's Table 3 says decides
whether this structure makes money. This adds **four numbers to the entry-side
records and one to the exit-side record**. No config key, no new audit category,
no gate input, no alert, no threshold, no behaviour change — and **no schema
change**: the audit payload is one JSON column, so these are keys in a blob, not
columns in a table (2.13). It is an instrument, not a signal.

---

## 1. Recommendation in one paragraph

Record **raw `bid` and `ask` per leg** on `signal.candidate` (Tier 1) and on
`signal.none`'s per-side near-miss detail (Tier 2), and **`crossing_debit`**
alongside the existing `cost_to_close` on `TriggerEvaluation.to_payload()`, which
reaches `watchdog.check` and `watchdog.check_spent` through the one method both
already splat. Raw inputs on the entry side, because the leg attribution is a
*selection* question and because `bid == 0` is the only way the zero-bid floor's
firing becomes visible; a single spread-level number on the exit side, because
**`crossing_debit − cost_to_close` is algebraically exactly the sum of the two
floored half-spreads** (§3.3) and the strikes are fixed by then. Every new
numeric goes through **one shared JSON-safety helper** — the codebase has already
answered this question three times in three spellings (§2.4), and a fourth
spelling is how one of them drifts. Fix one adjacent latent defect found on the
way: `cost_to_close` reaches the payload **unguarded** today (§2.5). No backfill
is possible and that is the entire urgency (§7.4).

---

## 2. What is actually true in source

**2.1 The entry-side fields are absent, and the mid is present.**
`SpreadSignal.to_payload` ([signal/models.py:141-169](../../src/argus/signal/models.py))
emits, per leg, `symbol` / `strike` / `delta` / `mid`, where `mid` is
`OptionQuote.floored_mid`. `bid` and `ask` are fields on `OptionQuote`
([models.py:52-53](../../src/argus/signal/models.py)) and reach no record. The
near-miss detail in `strategy._evaluate_side`
([signal/strategy.py:324-337](../../src/argus/signal/strategy.py)) carries
`credit`, both strikes, both deltas and `short_greeks_age_s` — and no prices at
all.

**2.2 The exit-side field already exists as a property and is not serialized.**
`SpreadMarket.crossing_debit` ([execution/watchdog.py:383](../../src/argus/execution/watchdog.py))
is `short.ask - long.bid` — *"what it costs to take the spread out of the market
RIGHT NOW"*, with the zero-bid floor deliberately not applied. It is what
`_cross_limit` acts on. `TriggerEvaluation` holds `market: SpreadMarket | None`
([watchdog.py:555](../../src/argus/execution/watchdog.py)) and `to_payload`
([watchdog.py:582-593](../../src/argus/execution/watchdog.py)) emits
`cost_to_close`, `credit_stop`, `spot`, the two reason sets, `problems` and
`seconds_to_flatten`. The market is in hand at the moment the payload is built.

**2.3 One method, two records, no divergence risk.** `evaluation.to_payload()` is
splatted at `watchdog.check` ([watchdog.py:2876](../../src/argus/execution/watchdog.py))
and `watchdog.check_spent` ([watchdog.py:2856](../../src/argus/execution/watchdog.py)),
and at two alert/record sites (`_report_evaluability` at
[watchdog.py:3590](../../src/argus/execution/watchdog.py), `_trigger` at
[watchdog.py:3639](../../src/argus/execution/watchdog.py)). Adding a field to the
method reaches all four. **This matters for the 2026-09-14 lesson**: `read_marks`
named one category and the runtime added another, so the mark froze at the last
tick before the exit died. Adding to the shared method rather than to the two
call sites is the shape that cannot reproduce that.

**2.4 The JSON-safety rule has been decided three times, in three spellings.**
The canonical hash uses `allow_nan=False`
([audit/models.py:53](../../src/argus/audit/models.py)), so a non-finite float in
a payload is a refused record. Three existing guards, all the same rule:

| Site | Spelling |
|---|---|
| `models._payload_delta` | `leg.delta if leg.has_delta else None` |
| `models.to_payload`, `underlying_price` | inline `is_finite(...)` ternary |
| `strategy._num` ([strategy.py:397](../../src/argus/signal/strategy.py)) | `value if value is not None and math.isfinite(value) else None` |

`_payload_delta`'s own docstring states the reason: *"the audit log refuses a
payload `json` cannot encode, and a refused Tier 1 write halts trading."*

**2.5 The consequence is worse on Tier 1 than that docstring implies, and
different on Tier 2.** Traced, because this design's whole risk surface is here:

- **Tier 1.** `AuditLog.record` calls `_reject_unserializable`
  ([audit/log.py:493](../../src/argus/audit/log.py)), which raises `ValueError`
  — **not** `AuditWriteError`. In `orchestrator.evaluate_once` the
  `signal.candidate` write sits **above** `self._gate.evaluate` and
  `self._submit`, and `run()` catches the raise in its generic
  `except Exception: # a bad iteration must not kill the loop`
  ([orchestrator.py:243](../../src/argus/orchestrator.py)). So a non-finite field
  would **silently suppress every entry** for as long as the condition held —
  no alert, no `/status` row, no audit record, one `logger.exception` per tick.
  A telemetry field would have become a trading-behaviour change.
- **Tier 2.** `_consume` drops the **whole record** with a `tier2_unserializable`
  alert ([audit/log.py:190](../../src/argus/audit/log.py)). On `signal.none` that
  discards the near-miss credit this design exists to collect; on
  `watchdog.check` it discards the mark trail `tools.desk` reads.

**2.6 Non-finite prices cannot reach these fields *from the live adapter*, and
the port does not promise it.** `DXLinkMarketData._ingest`
([marketdata/dxlink.py:158-171](../../src/argus/marketdata/dxlink.py)) maps every
side through `_to_float`, which returns `None` for NaN/inf, then maps `None` to
**`0.0`**. So `Quote.bid`/`ask` are finite by construction on the live path.
But `MarketDataPort` is a `Protocol` with three implementations today
(`DXLinkMarketData`, `FakeMarketData`, `book.py`) and a `SimBroker` planned, and
**it makes no finiteness promise** — which is exactly why `evaluate_triggers`
already guards `cost_to_close` with `is_finite`
([watchdog.py:636](../../src/argus/execution/watchdog.py)) and why
`OptionQuote.has_delta` guards delta. The guard is a property of the payload
boundary, not of whichever adapter happens to be installed.

**2.7 An adjacent latent defect, found on the way.** That `is_finite` check at
`watchdog.py:636` adds a `problems` entry and marks `CREDIT_MULTIPLE` unknown —
and then `to_payload` records the raw `cost_to_close` anyway. **In the one branch
where the number is known to be unusable, the record that would explain the
refusal is the record that gets dropped.** Unreachable today via DXLink (2.6);
reachable via any other port; and it is the same defect class this design must
not add to. §3.5.

**2.8 `_to_float` conflates "no bid" with "bid not reported".** Both become
`0.0`. Nothing downstream — including `bid_size`, which takes the same mapping —
can separate them. This bounds what §7.1's zero-bid question can conclude and is
stated there rather than discovered later.

**2.9 `floor_bid` neutralizes a zero bid and the record does not say so.**
`floor_bid` ([execution/zerobid.py:55](../../src/argus/execution/zerobid.py))
returns `SPX_MIN_TICK` for any `bid <= 0`, so a zero-bid long leg records
`mid = (0.05 + ask)/2` with nothing indicating the floor fired. The recorded
`credit` is therefore built on a floored mid, which is **systematically
optimistic relative to a crossing exit** by exactly the floor. This is a friction
fact and it is currently invisible.

**2.10 `tradeable` is `ask > 0.0` and bid is unconstrained.** Applied at
`strategy.py:267` and `regime.py:239`. A NaN ask fails it (every NaN comparison
is False), so a selected leg always has a positive finite ask. **A selected leg's
bid has no such guarantee** — it may legitimately be `0.0`, and per 2.8 that
value is overloaded.

**2.11 Adding keys to these payloads is additive-safe for every reader.**
`marker._decode` ([shadow/marker.py:143-151](../../src/argus/shadow/marker.py))
reads named keys and ignores the rest; `tools/desk.py:220` uses `.get()`.
`markable_payload` ([shadow/outcome.py:170](../../src/argus/shadow/outcome.py))
embeds `signal.to_payload()` whole, so `shadow.would_submit` and `entry.dry_run`
inherit the new fields for free — which is desirable, and is why that function
exists as a function.

**2.12 The submit-time spread is within 1 second of the proposal-time spread.**
`build_order` bounds `SpreadSignal.captured_at` age by
`execution.signal_max_age_s = 1.0`. So the entry-side record does not need to be
duplicated at submit time; the two are joinable on `candidate_id` and at most 1 s
apart. §3.6.

**2.13 NO SCHEMA CHANGE: the payload is one JSON column.** Stated explicitly
because the field names below read like columns and are not.
`audit_log` ([db/migrations/0001_initial.sql](../../src/argus/db/migrations/0001_initial.sql))
is `(seq, ts, tier, category, payload, prev_hash, hash)`, where `payload TEXT NOT
NULL -- canonical JSON` holds the **entire** dict; `_insert`
([audit/log.py:273](../../src/argus/audit/log.py)) writes
`json.dumps(rec.payload, sort_keys=True, ...)` into that one column. So every
field this design adds is a **key inside a JSON blob**, not a column:

| | Migration 0005 (`entry_credit` realized basis) | This design |
|---|---|---|
| What changed | Two real columns on `order_legs` | Keys in an existing `payload` blob |
| Schema | `ALTER TABLE` | Untouched |
| Migration file | `0005_order_leg_fill_prices.sql`, immutable once shipped | **None** |
| `PRAGMA user_version` | Advances | Unchanged |
| Older rows | Keep `NULL`; no backfill | Simply lack the keys |
| Readers | Must learn the column | `.get()` / named-key, additive-safe (2.11) |

The practical consequences: no `0006_*.sql`, nothing for
`scripts/check_migrations.py` to police, no schema step in the deploy, and a
mixed database — rows written before and after the image roll — is the **normal
and expected** state rather than something to reconcile. It is also why §8.2's
growth is *bytes added to existing rows*, not rows or columns added to a table.

---

## 3. The design questions

### 3.1 (Q1) Raw `bid`/`ask` on the entry side, not a derived width

Three shapes: **(a)** raw `bid` and `ask` per leg (4 numbers); **(b)** a derived
`half_spread` or `relative_spread` per leg (2 numbers); **(c)** one spread-level
number.

**Recommend (a).** Four arguments, in order of weight:

1. **`bid == 0` is only visible in the raw value**, and per 2.9 it is the
   difference between a credit that is collectable and one built on a floor. A
   derived half-spread hides it — `(ask − 0)/2` is not a half-spread, it is half
   the ask, and it would enter the dataset looking like a very wide market rather
   than an absent one.
2. **Leg attribution is a selection question on this side.** Whether the wide leg
   is the short or the long bears on `target_short_delta` and on the long-leg
   search; a spread-level number cannot answer it. The exit side has no such
   question, which is why §3.3 goes the other way — the asymmetry is reasoned,
   not inherited.
3. **The derived quantities are all recoverable from the raw; the reverse is
   false.** Half-spread, relative spread, floored mid, `two_sided`, and the
   crossing cost all follow from `(bid, ask)`. This is the one place in the
   design where "record the inputs" is cheap enough to be obviously right.
4. **It costs nothing that (b) saves.** Two extra floats per leg on records that
   already carry four fields per leg.

The case *against* (a) is payload size and loop latency, answered in §8.2 and
§8.3, and that it makes
`mid` redundant — which it does, and **`mid` stays**: it is a wire contract
`marker._decode` and `tools.desk` read, and removing a key readers depend on to
save 20 bytes is the trade nobody wants.

### 3.2 (Q2) `signal.none` too, and this is the decision that makes the dataset usable

`signal.candidate` alone would be the smaller change. **Recommend both.**

The argument is already written in this codebase, one field over.
`orchestrator.evaluate_once`'s own comment on the regime block says it: *"a
distribution sampled only from ticks that already produced a candidate is
conditioned on the conditions the current thresholds happen to like, which is
precisely the wrong population to calibrate a future gate from."*

It applies here with more force than it does to the regime, because the
conditioning is **mechanical rather than incidental**: `min_credit` rejects on
credit, credit is a difference of floored mids, and a floored mid is a function
of the very spread being measured. Recording the spread only where the credit
cleared samples the tail of its own distribution. The near-miss detail was built
for exactly this class of question (`StrategyResult`'s docstring: *"lets you ask
'what `min_credit` would have fired, and how often?' directly from the audit
log"*) and it is where these fields belong.

Tier 2 droppability is the tolerable defect here, on the realized-vol design's
§2.2 reasoning: a distribution survives a sparser sample, and nothing in §7 needs
a complete one.

### 3.3 (Q3) ONE field on the exit side, and the identity that decides it

`crossing_debit` alone, not four raw leg prices. The deciding argument is
arithmetic, not scope.

With both legs two-sided, writing `a`/`b` for ask/bid and `fb` for `floor_bid`:

```
cost_to_close   = (fb_s + a_s)/2 − (fb_l + a_l)/2
crossing_debit  =  a_s − b_l

crossing_debit − cost_to_close = (a_s − b_s)/2 + (a_l − b_l)/2
```

**The difference between the two recorded numbers is exactly the sum of the two
half-spreads** — which is precisely Vilkov's friction layer (ii) for a two-leg
structure. One field recovers the whole quantity.

Two caveats, both real and both stated rather than buried:

- **The identity is exact only when both legs are two-sided.** With `b_l = 0` the
  floor contributes an extra `0.025`, because `cost_to_close` floors and
  `crossing_debit` deliberately does not. Analysis must condition on
  `two_sided`; `Quote.two_sided` already exists as the predicate and the entry
  side records the raw bids that make the same test possible there.
- **It does not give leg attribution.** Four unknowns, two equations. Accepted:
  by exit time the strikes are fixed and there is no selection decision left, so
  the spread-level total is the whole question.

The volume argument agrees rather than leads: `watchdog.check` is the
highest-cadence Tier 2 category in the system at `watchdog.cadence_s = 1.0` per
position (§8.2).

### 3.4 (Q4) ONE JSON-safety helper, and the existing three collapse into it

**Recommend `payload_number()` as a single public helper in `signal/models.py`**,
used by the new fields and adopted by all three existing spellings from 2.4.

The primitive already exists (`zerobid.is_finite`); what is duplicated is the
`→ None if not finite` wrapper. The collapse is behaviour-identical:
`payload_number(leg.delta)` and `leg.delta if leg.has_delta else None` agree on
every input, `None` and NaN included.

`zerobid`'s own module docstring is the precedent and it is unusually direct:
*"Two implementations of the same safety rule is exactly how one of them quietly
drifts, so there is one."* That module exists because this exact mistake was
anticipated for the tick floor. Adding a fourth spelling of the payload guard
while citing that file for the floor would be difficult to defend.

*This is the most arguable call in the document and the cheapest to overrule.* If
the collapse is unwanted, the new fields still get a guard — they just get a
fourth spelling, and §6's test pins the invariant either way.

### 3.5 (Q5) Guard `cost_to_close` at the payload boundary, in this change

2.7's defect is one line and it is in the method this change is already editing.
**Recommend fixing it here**, on the rule that a numeric reaching a payload is
guarded at the boundary regardless of what the producing code believes.

Not fixing it would leave `to_payload` emitting one guarded new field beside one
unguarded old one, which is a worse state to hand the next reader than either
consistent alternative. The behavioural surface is nil: the `is_finite` check at
`watchdog.py:636` already handles the *logic*, and this only stops the
explanatory record from being the casualty.

### 3.6 (Q6) NOT on `order.submit_attempt`

The tempting third surface is the order record, tying the quoted spread to the
walk that followed. **Recommend against.** Per 2.12 the two moments are at most
1 second apart and joinable on `candidate_id`, and a duplicated fact is a fact
that can disagree with itself — the failure the `markable_payload` docstring
describes for a drifted key name, in a form that is harder to notice because both
copies are plausible. **Join, do not copy.**

### 3.7 (Q7) No `/status` row, no alert, no threshold

A reader will ask, and the 2026-08-20 assessment supplies the case *for*: a
widened market on an open position is exactly the condition under which the
`credit_multiple` cost-to-close math is least trustworthy, which sounds like an
operational condition worth a row.

**Recommend against, for now**, because acting on it needs a threshold and
**there is no number to set one from** — that is what this change exists to
produce. Choosing the threshold first and collecting the data afterwards is the
`[regime]` error precisely: a plausible mechanism, an armed knob, no measurement.
`[regime]` has been inert since 2026-08-04 for that reason and `regime.md §5`
still recommends leaving it so.

The trigger that would change this answer, stated now so it is recognizable:
a measured distribution showing the mark-to-cross gap is materially bimodal, or
a live episode where a wide market is the proximate cause of a bad exit. Either
makes a threshold derivable rather than invented.

### 3.8 (Q8) No new audit category

Every field lands on an existing record at an existing tier. No category means no
new reader has to learn a name, and — per the 2026-09-14 finding that a reader
naming one category makes a closed-world claim the runtime is free to invalidate
— no new way for a future reader to miss half the data.

### 3.9 (Q9) Key names are `bid` and `ask`, nested beside `mid`

Inside the existing `short` / `long` dicts, alongside `strike`, `delta`, `mid`.
Not `short_bid` at the top level: the nesting already exists, readers already
navigate it, and B1's top-level-scope rule governs `candidate_id` on alerts, not
prices on records. On the near-miss detail the surrounding keys are flat
(`short_strike`, `long_strike`), so the new ones match that shape there —
`short_bid`, `short_ask`, `long_bid`, `long_ask`. **Match the record you are
joining, not the other record.**

---

## 4. The change, file by file

| File | Change |
|---|---|
| `src/argus/signal/models.py` | `payload_number()` helper (§3.4). `to_payload` gains `bid`/`ask` per leg, guarded. `_payload_delta` and the `underlying_price` ternary adopt the helper. |
| `src/argus/signal/strategy.py` | Near-miss detail gains `short_bid`/`short_ask`/`long_bid`/`long_ask`. `_num` adopts the shared helper. |
| `src/argus/execution/watchdog.py` | `TriggerEvaluation.to_payload` gains `crossing_debit` (guarded) and guards `cost_to_close` (§3.5). One method; four consumers inherit it. |
| `tests/test_strategy.py` | Near-miss fields present, and absent-leg cases. |
| `tests/test_shadow_outcome.py` | `markable_payload` carries the new leg fields. |
| `tests/test_watchdog.py` | `crossing_debit` present and `None` when blind. |
| `tests/test_payload_safety.py` | **NEW.** §6's invariant across all three payloads. |

**Not touched:** `config.py`, any `argus.*.toml`, `blackout_dates.txt`,
`docker-compose.yml`, `argus.service`, `orchestrator.py`, `risk/*`,
`execution/build.py`, `execution/loop.py`, `execution/entry.py`, `status.py`,
`session.py`.

**No migration, and no `db/` change of any kind.** Per 2.13 the audit payload is
a single JSON `TEXT` column, so these fields are dict keys rather than columns.
There is no `0006_*.sql`, `PRAGMA user_version` does not move, and a database
holding rows from both before and after this deploy is the expected state, not a
reconciliation problem.

**No field is read by anything.** Every new value is written and never consulted
— no comparison, no branch, no threshold. That is the invariant §6 pins with an
AST test, and it is what makes "no behaviour change" a structural claim rather
than a review opinion.

---

## 5. What this deliberately does NOT do

- **It does not gate anything.** No `PreTradeGate` input, no `strategy.evaluate`
  input, no trigger, no size effect.
- **It does not add a config key.** The releases around v0.3.14 forced four
  coupled config moves in five; this one must not.
- **It does not add displayed depth.** `Quote` carries `bid_size`/`ask_size` and
  Vilkov's liquidity family includes depth (`d`) and a tightness ratio
  `T = h/(d+1)`. Rejected on a structural argument, not scope: **Argus routes a
  multi-leg order into a complex-order auction, so per-leg top-of-book depth does
  not describe the liquidity Argus actually accesses.** The half-spread does
  describe the cost benchmark, because it prices the mid the walk starts from.
  Depth would also require two new fields on `OptionQuote` and a change in
  `live_chain`, where bid/ask need neither. Revisit if size ever grows past the
  <4-contract average 0DTE execution the Cboe Q1 2025 deck reports.
- **It does not change what `credit` means.** The zero-bid floor stays; this only
  makes its firing visible.
- **It does not answer whether friction kills the edge.** It is the instrument.
  The measurement needs live fills and is named in §7.
- **It does not touch any rail, halt path, or the exit decision.**

---

## 6. Tests

- **The payload-safety invariant, parametrized over every numeric field.** For
  `SpreadSignal.to_payload`, the near-miss detail, and
  `TriggerEvaluation.to_payload`: substitute NaN, +inf and −inf into each numeric
  input in turn and assert `json.dumps(payload, allow_nan=False)` succeeds. This
  is the test that would have caught 2.7, and it is deliberately a behavioural
  test rather than an AST one — it pins the harm, not the spelling.
- **The entry path survives a hostile quote end to end.** Drive
  `orchestrator.evaluate_once` with a port returning a non-finite bid and assert
  a candidate is still recorded **and still reaches the gate**. This is the 2.5
  failure mode — the silent entry suppression — and nothing else in the suite
  would catch it.
- **The `crossing_debit` identity.** On a two-sided market,
  `crossing_debit − cost_to_close == half_spread(short) + half_spread(long)`
  within `PRICE_EPSILON`. On a zero-bid long leg, assert the stated `0.025`
  divergence rather than the identity — the caveat is pinned, not left in prose.
- **`None` propagates, `0.0` does not.** A blind position records
  `crossing_debit: null`, never `0.0`. Same rule `cost_to_close` already holds,
  and the reason `/status` renders `unpriced` rather than a number.
- **Nothing reads the new fields.** An AST test that no comparison, branch or
  arithmetic in `src/` consumes `crossing_debit` outside `to_payload` and
  `_cross_limit`'s existing use. The `reject_reason` precedent, for the same
  reason: *permanent-vs-transient from a recorded number is the tempting wrong
  fix.*
- **The helper is shared, not copied.** An AST test that `signal/` and the edited
  watchdog payload define no second finiteness-to-`None` wrapper — the rule
  `realized.py` already carries for `MINUTES_PER_YEAR`.
- **Readers are unbroken.** `marker._decode` and `tools.desk` decode a payload
  carrying the new keys unchanged.

---

## 7. What it unlocks

### 7.1 Entry side

1. **Half-spread as a fraction of credit**, per candidate and per rejected tick —
   Vilkov's friction layer (ii), parameterized for a 5-point wing instead of for
   structures three times the notional. This is the number the review's §2.4-3
   says cannot be transferred and must be re-derived.
2. **How often the zero-bid floor fires, and on which leg** (2.9). Upper bound
   only, per 2.8's conflation of "no bid" with "not reported" — stated here so it
   is not discovered by someone quoting the figure.
3. **Spread width by minute-of-day**, which with finding 13 completes the
   10:00-clustering test (§7.3).
4. **Spread width against walk outcome**, joining `signal.candidate` →
   `walk.finished` on `candidate_id`: does a wide quote at proposal predict a
   walk-away, a rejection, or more rungs? This is `gate-signal-candidates.md`
   item #8, which the 2026-08-20 assessment called *"data-starved"*. These are
   the fields that un-starve it.

### 7.2 Exit side

5. **The gap between the mark and the crossing price**, tick by tick, over a
   position's life. The repo has learned this narratively twice — 2026-08-31's
   *"`cost_to_close` is a MARK, never a fillable price"*, and the 2026-08-20
   assessment's point that a widened spread is exactly where the
   `credit_multiple` math is least trustworthy. **This is the quantitative
   version of a lesson already paid for.**
6. **The first admissible input to the owed `exit_walk_away_pct` /
   `exit_walk_min_steps` pass.** `argus.live.toml`'s `trigger_source` note says
   `exit_walk_away_pct` *"was NEVER independently derived"* and that its only
   live-fire telemetry is n=2, *"evidence about the ARITHMETIC and not about the
   market."* The mark-to-cross gap is evidence about the market, and it accrues
   in cert (§8.1).

### 7.3 Why this and finding 13 ship separately

They are halves of one test — *is the credit worse at 10:00, and is the spread
wider?* — and finding 13 needs no code at all. Bundling would hold a zero-risk
analysis behind a deploy. Ship this to start the clock; run finding 13 whenever.

### 7.4 There is no backfill, and that is the urgency

`mid` is recorded historically, so the floored-mid series runs back to 2026-07-24.
**The spread does not exist in any record and cannot be reconstructed from one.**
Unlike realized volatility — where the 2026-09-08 design could note that the raw
path was already durable and the trailing clock did not start on ship day — every
day this is not deployed is a day permanently absent from the dataset. It is the
only item in the 2026-09-15 review with that property.

---

## 8. Deploy shape and risk

### 8.1 Class, and why cert counts

`src/` and `tests/` only. Under the v0.3.20 classifier the **image** is one of the
things the container can reach, so this is an ordinary image release: build with
`ARGUS_VERSION`, deploy, **restart**. No config move, no bind mount, no unit
change — the first uncoupled deploy shape since v0.3.15.

**The data is admissible in cert**, which is unusual and is the reason this can
ship before the live flip. `[marketdata] source = "live"` has been set since
2026-07-20, so these are real production quotes; nothing here touches a fill, an
order or a position. The standing caveat — *a number measured in cert cannot size
a change on its own* — applies to cert's undocumented multi-leg fill mechanism
and does not reach a quoted spread. This is the same exemption `regime.md §6`
claims for the regime telemetry, and for the same reason.

### 8.2 Growth

Rough, and worth stating rather than waving at:

| Record | Cadence | New bytes/record | Per day |
|---|---|---|---|
| `signal.none` | 5 s, both sides | ~100 B (8 numbers) | ~470 KB |
| `signal.candidate` | 5 s when firing | ~50 B (4 numbers) | included above |
| `watchdog.check` | 1 s per position | ~25 B (1 number) | ~360 KB worst case |

Under 1 MB/day, ~250 MB/year at current cadences. **This adds bytes to existing
rows, not rows** — the cheap kind of growth. The one place it touches a known
constraint is `verify_chain`, which the Litestream design measured at ~10.7 µs/row
and found exceeds `TimeoutStartSec` at ~4 years of growth; that bound is
row-count-driven with a payload-size term, and this moves the term slightly and
the count not at all. Litestream is deferred past go-live, so there is no
replication cost today.

### 8.3 Latency on the signal loop, measured

`signal.candidate` is **Tier 1** — `await self._audit.record(...)` on the signal
loop — so "a few more bytes" is not a sufficient answer. Traced and measured.

**What is actually on the loop.** A Tier 1 write serializes the payload **three
times**: `_reject_unserializable`'s validating `json.dumps`
([log.py:147](../../src/argus/audit/log.py)), `canonical_bytes`' hashing
`json.dumps` ([models.py:41](../../src/argus/audit/models.py)), and `_insert`'s
`json.dumps` for the INSERT ([log.py:273](../../src/argus/audit/log.py)). The
first two run **on the event loop**; the third runs inside
`await asyncio.to_thread(self._insert, rec)`. That 3x multiplier is pre-existing
and is the reason to measure rather than assume.

**The dominant term is unchanged.** `audit.synchronous = "FULL"` in both profiles,
so each Tier 1 commit is an fsync, run in a thread that yields the loop with
`_chain_lock` held across it ([log.py:148-151](../../src/argus/audit/log.py)).
42 bytes on a 488-byte payload changes neither the page count nor the fsync.
Measured against a WAL + `synchronous=FULL` SQLite doing one row per explicit
transaction:

| | this machine | droplet, expected |
|---|---|---|
| on-loop serialize + hash (dumps 1+2) | 16.9 µs | ~17 µs |
| threaded insert + commit (dump 3) | 53.5 µs | **0.5–3 ms** |
| serialization share of the write | 24% | **~0.5–3%** |

**The local ratio flatters serialization and must not be quoted as the
droplet's.** SQLite's `PRAGMA fullfsync` defaults to `0`, so on macOS
`synchronous=FULL` issues a plain `fsync()`, which does **not** flush the drive's
write cache; on the droplet's Linux block storage the same commit is one to two
orders of magnitude slower. The direction is favourable — serialization matters
*less* in production than the bench suggests — but the 24% figure is a macOS
artifact.

**Measured** (7 × 20,000 iterations, median, this machine; replicates the two
on-loop steps exactly and excludes the threaded insert):

| Record | Tier | Payload | On-loop serialize+hash | Δ |
|---|---|---|---|---|
| `signal.candidate` | 1, **on the signal loop** | 488 → 530 B | 13.05 → 14.82 µs | **+1.77 µs** |
| `signal.none` | 2, consumer task | 580 → 708 B | 9.10 → 10.96 µs | +1.85 µs |
| `watchdog.check` | 2, consumer task | 281 → 303 B | 5.13 → 5.38 µs | +0.25 µs |

**Against the loop's own alarm.** `heartbeat.lag_alert_threshold_s = 0.05` —
50,000 µs. The added on-loop work is **1.77 µs, roughly 1/28,000th of a single
breach**, and it occurs at most once per `signal.cadence_s = 5.0` (one
`signal.candidate` per `evaluate_once`; ~1,180/day measured 2026-08-28). Duty
cycle ≈ 3.5 × 10⁻⁵ %.

**Tier 2 does not touch the producing loop at all.** `telemetry()` is a
`put_nowait` ([log.py:170](../../src/argus/audit/log.py)); the serialize and hash
happen in `_consume`. Those two rows therefore cost the signal loop and the
watchdog nothing directly. They do share `_chain_lock`, so the indirect exposure
is extra lock-hold: worst case ≈ 1 × 0.25 + 0.2 × 1.85 ≈ **0.6 µs per second**.

**What this measurement is not.** It is the CPU half, on this machine, not a
production measurement of an awaited write end to end. **No Tier 1
write-latency instrument exists** — the Litestream design proposed exactly that
histogram, on the argument that *you cannot judge a regression in a number you do
not collect*, and it is deferred past go-live. So the claim here is "the added
CPU is four orders of magnitude below the loop's breach threshold", which the
numbers support, and **not** "Tier 1 write latency was measured before and after
in production", which nothing can currently say. If that histogram ever ships,
this is a cheap change to re-check against it.

**Method, to re-run.** Build a representative `signal.candidate` payload (both
legs, the `regime` block, `contracts`), then time the two on-loop steps together:
`json.dumps(payload, allow_nan=False)`, followed by `canonical_bytes(...)` over a
record envelope and `hashlib.sha256(...).hexdigest()` on the result. Median of 7
batches of 20,000. Exclude `_insert` — it is in a thread. Compare the same
payload with and without the four leg fields; the delta is the whole answer.

**Why the 3x is structural, and the rule it implies.** Worth recording, because
the multiplier looks like something to optimize and is not:

| Dump | Where | Removable? |
|---|---|---|
| `canonical_bytes` | on loop | **No.** It *is* the hash; the canonical form is the chain's definition. |
| `_insert` | in the thread | **No.** The payload has to reach the `payload TEXT` column. It could reuse the hash's bytes only by hand-building the envelope JSON so the payload fragment could be spliced in — replacing a library call with hand-rolled JSON in the one place a mistake silently breaks verification for every future row. |
| `_reject_unserializable` | on loop, pre-lock | **Yes, and deliberately kept.** Redundant as a *check* (if the payload serializes, the envelope does), it buys a categorized error naming the category instead of a raw `Out of range float values are not JSON compliant` from inside `canonical_bytes`, and it fails **before** `_chain_lock`. Measured cost: **5.63 µs**. Given 2.5 — a refused Tier 1 write is the path that silently suppresses entries — that is insurance on the failure mode this design most has to get right. |

A detail that looks alarming and is not: `_insert` passes no `separators=`, so the
**stored text is not byte-identical to what was hashed**. Harmless — `verify_chain`
does `json.loads(row["payload"])` → object → `canonical_bytes` → sha256
([log.py:471-476](../../src/argus/audit/log.py)), re-deriving the canonical form
rather than re-hashing stored bytes. The column is a storage representation; the
canonical form is computed and never read back. **Do not "fix" the
inconsistency** — making the two agree would create a byte-level coupling the
chain does not currently have.

**The rule for whoever designs the next Tier 1 record**, which is the part of this
section with a life beyond this change:

> **A Tier 1 payload is serialized three times, twice of them on the event loop.
> Size on a Tier 1 record costs 3x, and cadence is what turns that into
> latency.**

Irrelevant at 488 bytes once per 5 s — that is this change, and it is why the
answer here is "no impact". It stops being irrelevant for a **large** Tier 1
payload at **high** cadence: a full chain snapshot, a leg-by-leg book, an
unbounded list. That is the case where "it is only telemetry" would be wrong, and
the 3x is the reason.

### 8.4 Risk

Structurally low and concentrated in one place. Nothing reads the new fields
(§4), so the only reachable harm is a payload that will not serialize — §2.5's
silent entry suppression on Tier 1, §2.5's dropped record on Tier 2. That is
precisely what §3.4's helper and §6's first two tests exist for, and §2.6 says
the live adapter cannot produce the input in the first place. The residual is a
future port implementation, which is the case the guard is for.

The one thing to watch after deploy: `tier2_unserializable` in the alert channel.
It should stay at zero. If it moves, a port is producing something `_to_float`
does not.

---

## 9. On sign-off

Nine decisions to accept, amend or overrule:

1. **Raw `bid`/`ask` per leg on the entry side**, not a derived width (§3.1).
2. **`signal.none`'s near-miss detail gets them too**, on the
   don't-sample-your-own-threshold argument (§3.2). *The decision that makes the
   dataset usable rather than merely present.*
3. **One field on the exit side — `crossing_debit`** — on the identity that it
   plus `cost_to_close` recovers both half-spreads exactly (§3.3).
4. **One shared `payload_number()` helper, and the three existing spellings
   collapse into it** (§3.4). *The most arguable call here; overruling it costs
   the collapse, not the guard.*
5. **`cost_to_close` gets guarded at the payload boundary in this change**
   (§3.5).
6. **Nothing on `order.submit_attempt`** — join on `candidate_id`, do not copy
   (§3.6).
7. **No `/status` row, no alert, no threshold** (§3.7), with the stated trigger
   that would reopen it.
8. **No new audit category, no config key** (§3.8).
9. **No displayed depth** (§5), rejected on the complex-order-auction argument
   rather than on scope.
