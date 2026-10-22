# Recording the quoted spread

*Argus recorded the MARK of every leg it ever selected and never once the PRICE —
built 2026-09-15, `src/` + `tests/` only, no config key, no audit category, no
migration, telemetry-and-analysis only.*

> Settled. Design and rejected alternatives:
> [`docs/designs/2026-09-15-recording-the-quoted-spread.md`](../designs/2026-09-15-recording-the-quoted-spread.md),
> all nine §9 decisions taken as written. See
> [`docs/decisions/README.md`](README.md) for the index.

`SpreadSignal.to_payload` wrote `floored_mid` per leg and not `bid`/`ask`;
`TriggerEvaluation.to_payload` held a `SpreadMarket` carrying both legs' bid and
ask and serialized only `cost_to_close`, the floored mid. So the audit log could
answer *what did Argus think this spread was worth* and could not answer *what
would it have cost to trade* — the difference between a mark and a price, and the
difference the outside literature says decides whether this structure makes money
at all. Four numbers on the entry-side records, one on the exit-side record.

**Nothing acts on any of them.** No gate input, no trigger, no threshold, no
alert, no `/status` row — the `[regime]` pattern exactly, and pinned by two AST
tests rather than by review opinion. It is an instrument.

**There is no backfill and that is the entire urgency.** `mid` runs back to
2026-07-24; the spread exists in no record and cannot be reconstructed from one,
so every day this is not deployed is a day permanently absent from the dataset.

## What was built

| Record | Tier | Added |
|---|---|---|
| `signal.candidate` | 1, **on the signal loop** | `bid` / `ask` per leg, nested beside `mid` |
| `signal.none` per-side near miss | 2 | `short_bid` / `short_ask` / `long_bid` / `long_ask`, flat |
| `watchdog.check`, `watchdog.check_spent`, `watchdog.unevaluable`, `watchdog.triggered` | 2 / 1 | `crossing_debit` |
| `shadow.would_submit`, `entry.dry_run`, `entry.finished` | 1 | the leg fields, inherited through `markable_payload` |

`signal/models.py` gained `payload_number()`; `signal/strategy.py`'s `_num` is
gone; `_payload_delta` / `_payload_age` are gone. `tests/test_payload_safety.py`
is new.

## The decisions, and the ones worth carrying

**Raw `bid`/`ask` per leg on the entry side, a single spread-level number on the
exit side — and the asymmetry is reasoned, not inherited.** Entry: `bid == 0` is
the only way `zerobid.floor_bid`'s firing is visible at all, and a derived
half-spread hides it twice over, since `(ask − 0)/2` is not a half-spread but half
the ask and enters the dataset looking like a very wide market rather than an
absent one. Which leg is the wide one is also a SELECTION question there — it
bears on `target_short_delta` and on the long-leg search. Exit: by then the
strikes are fixed and there is no selection decision left, so the total is the
whole question, and

```
cost_to_close  = (fb_s + a_s)/2 − (fb_l + a_l)/2
crossing_debit =  a_s − b_l
difference     = (a_s − b_s)/2 + (a_l − b_l)/2
```

**the difference between the two recorded numbers is exactly the sum of the two
half-spreads.** One field recovers the whole quantity at the highest-cadence
Tier 2 category in the system. **The identity is exact only where both legs are
two-sided**: with `b_l = 0` the floor contributes an extra 0.025, because
`cost_to_close` floors and `crossing_debit` deliberately does not. Analysis must
condition on that, which is what the entry side's raw bids are for. Pinned by two
tests rather than left in prose.

**`signal.none` gets them too, and that is the decision that makes the dataset
usable rather than merely present.** The argument is already in this codebase one
field over, on the regime block: *"a distribution sampled only from ticks that
already produced a candidate is conditioned on the conditions the current
thresholds happen to like."* It applies here with more force, because the
conditioning is MECHANICAL rather than incidental — `min_credit` rejects on
credit, credit is a difference of floored mids, and a floored mid is a function of
the very spread being measured. Recording the market only where the credit cleared
samples the tail of its own distribution.

**The four price fields live on the near miss and nowhere else in `_select`.** A
rejection that never found a long leg has no long market to report, and a
`long_bid: null` there would mean "this tick had no long leg" in one place and
"the feed gave no bid" in another. Two tests pin the absence.

**Key names match the record they are joining, not the other record.** Nested
(`short.bid`) on `SpreadSignal.to_payload`, where the nesting already exists and
readers already navigate it; flat (`short_bid`) on the near miss, where everything
around them is flat. B1's top-level-scope rule governs `candidate_id` on ALERTS,
not prices on records.

**NOT on `order.submit_attempt`. Join on `candidate_id`, do not copy.**
`build_order` bounds `SpreadSignal.captured_at` by `execution.signal_max_age_s =
1.0`, so the two moments are at most one second apart and already joinable. A
duplicated fact is a fact that can disagree with itself, in the form that is
hardest to notice because both copies are plausible.

**No `/status` row, no alert, no threshold — and the trigger that would reopen it
is stated.** A widened market on an open position is exactly where the
`credit_multiple` math is least trustworthy, which sounds like an operational row.
Acting on it needs a threshold and there is no number to set one from; that is
what this change exists to produce. Choosing the threshold first and collecting
the data afterwards is the `[regime]` error precisely. It reopens on a measured
distribution showing the mark-to-cross gap is materially bimodal, or a live episode
where a wide market is the proximate cause of a bad exit.

**No displayed depth**, and the argument is structural rather than scope: Argus
routes a multi-leg order into a complex-order auction, so per-leg top-of-book
depth does not describe the liquidity Argus actually accesses. The half-spread
does describe the cost benchmark, because it prices the mid the walk starts from.
Revisit if size grows past the <4-contract average 0DTE execution.

## ONE payload guard, and what a refused write actually costs

**`payload_number()` in `signal/models.py` is now the single definition of
"non-finite becomes `None` on the way into the audit log".** The rule had been
decided three times in three spellings — `_payload_delta`'s `has_delta` ternary,
`underlying_price`'s inline `is_finite` ternary, `strategy._num`'s `isfinite`
ternary — all behaviour-identical on every input, `None` and NaN included, which
is asserted by a test rather than by reading. `zerobid`'s own docstring is the
precedent, and it exists because this exact mistake was anticipated for the tick
floor: *"Two implementations of the same safety rule is exactly how one of them
quietly drifts, so there is one."*

**`risk/gate.py::_num` is deliberately NOT collapsed into it.** That one
STRINGIFIES a non-finite value where this one nulls it, and the difference is the
purpose: a gate decision records the limit in force, where `inf` (a
never-connected stream) is the information rather than noise to erase. Do not
merge them.

**The consequence of a refused write is worse than "no record" on Tier 1 and
different on Tier 2**, and this is the reason the guard is not a nicety:

- **Tier 1** — `_reject_unserializable` raises `ValueError`, **not**
  `AuditWriteError`. `signal.candidate` is written in `orchestrator.evaluate_once`
  ABOVE `self._gate.evaluate` and above `self._submit`, and `SignalLoop.run`
  catches the raise in its generic `except Exception: # a bad iteration must not
  kill the loop`. So one non-finite field would **silently suppress every entry**
  for as long as the condition held — no alert, no `/status` row, no audit record,
  one `logger.exception` per tick. A telemetry field would have become a
  trading-behaviour change.
- **Tier 2** — `_consume` drops the WHOLE record with a `tier2_unserializable`
  alert, taking the near-miss dataset or the mark trail `tools.desk` reads with it.

**An adjacent latent defect was fixed on the way: `cost_to_close` reached the
payload unguarded.** `evaluate_triggers` already tests `is_finite(cost_to_close)`,
files a `problems` entry and marks `CREDIT_MULTIPLE` unknown — and `to_payload`
then recorded the raw value anyway, so **in the one branch where the number is
known to be unusable, the record that would explain the refusal is the record that
gets dropped.** The other three numerics on that payload (`credit_stop`, `spot`,
`seconds_to_flatten`) and the six on `SpreadSignal.to_payload` took the same guard
in the same pass, on §3.5's own rule: *a numeric reaching a payload is guarded at
the boundary regardless of what the producing code believes.* Leaving one guarded
field beside an unguarded one is a worse state to hand the next reader than either
consistent alternative, and it is what made §6's headline test — parametrized over
every numeric input — true rather than partially true.

**These values cannot arrive from the live adapter, and the port does not promise
it.** `DXLinkMarketData._ingest` maps every side through `_to_float`, which
returns `None` for NaN/inf and then maps `None` to `0.0`, so `Quote.bid`/`ask` are
finite by construction there. `MarketDataPort` is a `Protocol` with three
implementations today and a `SimBroker` planned, and it makes no finiteness
promise. **The guard is a property of the payload boundary, not of whichever
adapter happens to be installed.**

## What is written and never read

Two AST tests, on the `reject_reason` precedent and for the same reason
(*permanent-vs-transient from a recorded number is the tempting wrong fix*):

- **`crossing_debit`** is consumed in exactly two functions in `src/` — its own
  property definition and `_cross_limit`, which acted on it long before it was
  recorded. Anything else reading it fails the test.
- **The near-miss price KEYS** (`short_bid` and the other three) are looked up
  nowhere. The entry side cannot make the same claim about `bid`/`ask` themselves
  — reading those IS what pricing is — so the assertion is narrowed to what is
  actually new.

That is what makes "no behaviour change" a structural claim rather than a review
opinion.

## What this does not touch, and two reader facts

**No schema change and no migration.** `audit_log` is
`(seq, ts, tier, category, payload, prev_hash, hash)` with `payload TEXT NOT NULL
-- canonical JSON` holding the entire dict, so every field here is a **key inside
a JSON blob**, not a column. There is no `0006_*.sql`, `PRAGMA user_version` does
not move, nothing for `scripts/check_migrations.py` to police, and **a database
holding rows from both before and after the deploy is the normal and expected
state**, not a reconciliation problem.

**Adding keys is additive-safe for every reader**, asserted end to end rather than
assumed: `marker._decode` reads named keys and ignores the rest, `tools/desk.py`
uses `.get()`, and `markable_payload` embeds `signal.to_payload()` WHOLE — which
is why `shadow.would_submit`, `entry.dry_run` and `entry.finished` inherit the leg
fields with no writer being edited. That is desirable, and is why that helper is a
function rather than a convention.

**`mid` stays despite being derivable from `bid`/`ask`.** It is a wire contract
readers depend on; removing a key to save 20 bytes is the trade nobody wants.

**Nothing else moved.** `config.py`, every `argus.*.toml`, `orchestrator.py`,
`risk/*`, `execution/build.py`, `execution/loop.py`, `execution/entry.py`,
`status.py`, `session.py` and `db/` are all untouched. The zero-bid floor is
unchanged — this only makes its firing visible.

## Deploy shape

`src/` and `tests/` only. Under the v0.3.20 classifier the **image** is one of the
things the container can reach, so this is an ordinary image release: build with
`ARGUS_VERSION`, deploy, **restart**. No config move, no bind mount, no unit
change.

**The data is admissible in cert**, which is unusual and is why this can ship
before the live flip: `[marketdata] source = "live"` has been set since
2026-07-20, so these are real production quotes, and nothing here touches a fill,
an order or a position. The standing caveat — *a number measured in cert cannot
size a change on its own* — is about cert's undocumented multi-leg fill mechanism
and does not reach a quoted spread. Same exemption the regime telemetry claims.

**Growth** is under 1 MB/day at current cadences (~470 KB of `signal.none`,
~360 KB worst-case of `watchdog.check`), and it is **bytes added to existing rows,
not rows added to a table** — the cheap kind.

**The one thing to watch after deploy: `tier2_unserializable` in the alert
channel.** It should stay at zero. If it moves, a port is producing something
`_to_float` does not.

## The rule for whoever designs the next Tier 1 record

Traced and measured during the design, and it has a life beyond this change:

> **A Tier 1 payload is serialized three times, twice of them on the event loop.
> Size on a Tier 1 record costs 3x, and cadence is what turns that into latency.**

`_reject_unserializable`'s validating `json.dumps` and `canonical_bytes`' hashing
`json.dumps` both run on the loop; `_insert`'s runs inside
`asyncio.to_thread`. None of the three is removable — the canonical form IS the
chain's definition, the payload has to reach the column, and the first is
redundant as a check but **deliberately kept**, because it buys a categorized
error naming the category and it fails BEFORE `_chain_lock`. Here the added
on-loop work is **+1.77 µs** against `heartbeat.lag_alert_threshold_s` of 50,000 µs,
at most once per `signal.cadence_s = 5.0` — four orders of magnitude below the
loop's own alarm, so the answer is "no impact". It stops being irrelevant for a
LARGE Tier 1 payload at HIGH cadence: a chain snapshot, a leg-by-leg book, an
unbounded list. That is the case where "it is only telemetry" would be wrong.

A related detail that looks alarming and is not: `_insert` passes no
`separators=`, so the **stored text is not byte-identical to what was hashed**.
Harmless — `verify_chain` re-derives the canonical form from the parsed object
rather than re-hashing stored bytes. **Do not "fix" the inconsistency**; making
the two agree would create a byte-level coupling the chain does not have.

## What it unlocks, and the one bound on it

Half-spread as a fraction of credit, per candidate and per rejected tick; spread
width by minute-of-day, which with credit-at-fixed-delta completes the
10:00-clustering test; spread width against walk outcome, joined
`signal.candidate` → `walk.finished` on `candidate_id`, which is the
`gate-signal-candidates.md` item the 2026-08-20 assessment called *"data-starved"*;
the mark-to-cross gap tick by tick over a position's life, which is the
quantitative version of a lesson this repo has paid for twice narratively; and the
first admissible input to the owed `exit_walk_away_pct` / `exit_walk_min_steps`
pass, whose only live-fire telemetry is n=2 and is *"evidence about the ARITHMETIC
and not about the market."*

**The bound, stated so it is not discovered by someone quoting a figure:
`_to_float` conflates "no bid" with "bid not reported" — both become `0.0`**, and
nothing downstream can separate them. So "how often the zero-bid floor fires" is
an **upper bound**, not a measurement.
