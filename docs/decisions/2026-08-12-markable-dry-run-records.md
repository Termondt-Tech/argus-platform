# Markable dry-run records: `entry.dry_run` + the marker's category union

**2026-08-12. Item 2 of `docs/post-close-outcome-processing.md`.**

## What was wrong

`tools.mark` filtered on exactly one audit category, `shadow.would_submit`
(`marker.py`, the module constant and the `WHERE category = ?` in
`read_proposals`). That category is written in **one place**: `SignalLoop`'s
`submit is None` branch, i.e. shadow mode's terminal action.

When `argus.toml` flipped to `environment = "sandbox"` on **2026-08-11**, the
loop started calling `EntryExecutor.submit` instead, and the terminal record
became `entry.dry_run`. Nothing was broken and nothing errored — marking simply
began reporting *"0 distinct trade idea(s)"* for every session, because the
category it was looking for had stopped existing.

`entry.dry_run` could not have filled the gap even if the marker had read it. It
carried `candidate_id`, `submitted`, `walked`, `why_not_walked`, `guard` and the
order ack — the record said that a candidate had been dry-run, and nothing about
**what**. Marking needs the strikes, the side, the width, the credit and the
contract count, none of which were there.

**Consequence: the continuous signal-quality series — the dataset the second,
data-backed parameter pass depends on — has a discontinuity starting
2026-08-11.**

## The two-part fix

### 1. `entry.dry_run` carries what marking needs

`_dry_run` now writes the same `signal` + `proposal` block `shadow.would_submit`
has always carried. **Purely additive**: every pre-existing key keeps its exact
name, type and meaning, and neither added key collides with the order ack's
(`order`, `dry_run`, `buying_power_effect`, `fees`, `warnings`, `errors`) or with
the five fields already there. Nothing that reads this record today sees a
different value — `test_dry_run_addition_is_purely_additive` pins the whole key
set so a future addition has to be a deliberate act.

The block is built by **`shadow.outcome.markable_payload`**, one function called
by both writers, rather than a dict literal in each. The failure mode of "two
places that must agree" is silent here: a writer that drifts by one key name
produces records that decode to nothing, and nobody finds out until a marking run
weeks later reports zero. That is, precisely, the failure this document exists
because of.

Secondary benefit, noted in the source doc: `entry.dry_run` is now
self-describing. Reading one no longer requires a join to `signal.candidate` to
find out what was dry-run.

### 2. The marker reads a category **union**, keyed on presence

```python
PROPOSAL_CATEGORIES = (WOULD_SUBMIT, DRY_RUN)   # shadow.would_submit, entry.dry_run
```

`read_proposals` selects `WHERE category IN (...)`, ordered by `seq`.

**The property that matters is not "sandbox works now" — it is that the marking
path never consults `environment`.** `shadow/marker.py` imports no `Config`,
names no environment, and asks the *records* which categories a date holds rather
than asking a config file which category it should expect. Three things follow,
and all three were the goal:

- **Reverting to shadow needs no change here**, for any reason, at any time,
  including mid-investigation. No new gap, no special case.
- **An old date is scored by what was recorded on it**, not by what the config
  says today — which is the only correct basis for backfilling a date months
  later, given the environment will have moved on.
- **A mixed day is not a special case** (below).

`test_the_marker_never_consults_the_environment` enforces this structurally: it
parses `marker.py` and fails on a config import, on an `environment` identifier
or attribute, or on a bare `"shadow"`/`"sandbox"`/`"live"` literal.

Adding a third terminal category later means adding it to `PROPOSAL_CATEGORIES`
and writing `markable_payload` into it. Nothing else.

> **The third category arrived on 2026-08-13**: `entry.finished`, the live entry
> path's terminal record. It cost exactly what this paragraph predicted — one
> tuple member and one `markable_payload` call — and the union property held
> unchanged. What the same change ALSO needed, and what this document did not
> anticipate, is that a filled position is not scoreable at settlement at all;
> see [the live-fire pass, Finding 6](2026-08-13-live-fire-sandbox-pass.md).
> `PROPOSAL_CATEGORIES` is now three, and the marker reads a SECOND source
> (`order.fill`) alongside them.

## The cadence difference, and why dedup is untouched

The two categories emit on **different cadences**, which was the open question
going in:

| | written | per idea |
|---|---|---|
| `shadow.would_submit` | every qualifying cadence tick | tens to hundreds |
| `entry.dry_run` | once, at the entry attempt | **exactly one** |

`entry.dry_run` is once-per-candidate because `EntryExecutor._attempted` claims
the spread *before* the submit, so every later tick short-circuits to Tier 2
`entry.already_attempted` — and dry-run mode never walks, so there is no reprice
cadence to emit on either. (More than one is still possible if the process
restarts mid-day and re-arms `_attempted`; the tests cover it.)

**Dedup semantics are unchanged and needed no accommodation.** `trade_ideas`
groups on `(trading date, side, short strike, long strike)` and keeps the first
row by `seq`; a one-row group's first row is its first proposal. What *does*
degrade on a dry-run day is the entry-timing diagnostic, not the score:
`proposals` reads 1 and `best_credit == worst_credit == credit`, because the
re-proposal history was never recorded at this layer. The credit itself is still
right — it is the price the order was actually built at, which is exactly the
"first proposal, the one actually collectable" the dedup was defined to keep.

## The mixed-category day: defined, not left implicit

Structurally possible whenever a process restarts mid-day across an environment
change — both categories then land on one trading date. The defined behaviour:

- **Ordering is the audit chain's global `seq`**, not category-then-seq. `seq` is
  assigned by the single chain writer under one lock, so it totally orders every
  record in the log and "the FIRST proposal for this spread" is well-defined
  across categories. Both categories are Tier 1, so `ts` (the indexed column the
  ET-day window filters on) and `seq` agree on order.
- **Grouping ignores the category.** A spread appearing in both collapses into
  **one** idea, whose credit comes from whichever record has the lower `seq` —
  not from whichever category the reader happened to scan first. `proposals` is
  the combined count; `best_credit`/`worst_credit` span both.
- `entry_rank` counts ideas per (date, side) in `seq` order, so it is unaffected.

Two tests fix the direction: one where a `shadow.would_submit` precedes the
`entry.dry_run` and one where it follows, asserting the credit flips accordingly.
A category-by-category read would pass the first and fail the second.

## The 2026-08-11 discontinuity: NOT backfilled, deliberately

**8 trade ideas** (per `docs/post-close-outcome-processing.md`) were recorded on
2026-08-11 with no markable payload, and every dry-run day between then and this
change is in the same state. **This is a decision, not a dropped thread.**

**The recovery path exists.** `signal.candidate` (Tier 1, written on every tick a
candidate fires) carries the full `signal.to_payload()` — side, both strikes,
width, credit, `underlying_price` — plus `contracts` at the top level. That is
every field `marker._decode` reads, at a different nesting. It is not sufficient
alone, because it is written *before* the gate and so includes candidates the
gate denied; restricting to approved ones needs `gate.decision`.

**The join is real but it is not on `candidate_id`** — neither category carries
that field. (`docs/post-close-outcome-processing.md` describes it as a
`candidate_id` join; that is the one inaccuracy in the source note.) The workable
key is the proposal symbol: `orchestrator` sets
`EntryProposal.symbol = signal.describe()`, which `gate.decision` records at
`proposal.symbol`, and `describe()` is
`f"{underlying} {side} credit spread {short:g}/{long:g}"` — fully reconstructible
from `signal.candidate`'s own payload. So the backfill would be:

1. reconstruct `describe()` from each `signal.candidate` on the date;
2. join to the `gate.decision` sharing that `proposal.symbol`, taking the next
   one by `seq` (the loop serializes, so the pairing is unambiguous in practice);
3. keep `allowed == true`;
4. dedup on (date, side, strikes) keeping the lowest `seq` — reimplementing
   `trade_ideas` against a second payload shape;
5. optionally intersect with `entry.dry_run.candidate_id`, which *is* derivable
   as `{date}-{side}-{short:g}-{long:g}`, to keep only ideas that reached the
   entry path rather than merely passing the gate.

**Judged not worth building for 8 records.** It is a second decoder for a second
payload shape plus a sequence-adjacency join, all of it write-once code that
would then sit in the tree misleading the next reader into thinking
`signal.candidate` is a supported marking source. The cost of not having it is 8
ideas from one day, in a series that is otherwise continuous from the first
shadow session and continuous again from 2026-08-12 forward.

If that judgement is ever revisited, the steps above are the whole of it and the
records are durable — the audit log is append-only, so the inputs are not going
anywhere.

## Scope

Touched: the `entry.dry_run` payload (additive), `shadow/marker.py`'s query,
`shadow/outcome.py` (the shared constructor), `orchestrator.py` (now calls it),
`tools/mark.py` (wording only — "would-submit record(s)" → "proposal record(s)",
which was wrong on a dry-run day).

Deliberately NOT touched: the price-walking loop's reproposal cadence, any
existing `entry.dry_run` field's semantics, and Items 3–6 of the source doc.
Item 4 (live-fire outcome attribution) remains the thing that makes settlement
marking conceptually wrong once orders actually fill; this change is about the
records being *readable*, not about whether settlement is the right score for a
filled position.

> **Item 4 was settled on 2026-08-13**, one day after this was written and one day
> after live fire began: `shadow/outcome.py` now scores a filled position from its
> FILL prices and keeps settlement for the contracts actually held to expiry, in
> one two-term formula. Every conclusion above survives it — the union is still
> keyed on presence, `trade_ideas` still dedups on the same tuple, the mixed-day
> behaviour is unchanged, and an unfilled idea still scores byte-identically. →
> [the live-fire pass, Finding 6](2026-08-13-live-fire-sandbox-pass.md)

## Tests

`tests/test_shadow_marker.py` — pure shadow day (regression: unchanged output),
pure dry-run day, the two scoring identically, first-credit dedup within each
category independently, mixed-day collapse in both `seq` directions, pre-fix
`entry.dry_run` rows skipped without costing the date, the ET-day window applying
to both, adjacent `entry.*` categories *not* being read as proposals, the
no-environment AST check, and an end-to-end mark with no `Config` in the call.

`tests/test_entry.py` — the emitted payload's marking block, its byte-identity
with `markable_payload`, the additive-only key set, and the real emitted record
decoded by the real `marker._decode` into a scored `TradeIdea`.
