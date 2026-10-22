# The broker's open/close suffix is not a statement about Argus's intent

**2026-08-25 — closed.** `src/` only, no config key, the image deploys alone.

## What happened

`tools.mark` and `tools.desk` both emitted one line on the 2026-08-25 session:

```
skipping order.fill at seq=420587: action Buy to Close disagrees with direction 'open'
```

`mark` then reported `1 idea(s) actually filled … scored 110.00` and `desk`
reported a day total of `90.74`. Two spreads had filled. The day was `210.00`
gross, roughly `197` net of the fees actually charged.

The record it dropped, in full:

```json
{"action": "Buy to Close", "broker_order_id": 1405428, "candidate_id": null,
 "contracts": 2, "external_identifier": "argus-open-20260825-call-7690-7695",
 "position_after": 0, "position_before": -2, "price": 1.2, "signed_delta": 2,
 "status": "Filled", "symbol": "SPXW  260825C07695000"}
```

## Mechanism

The day's two filled spreads CHAINED:

| ET | idea | short | long | |
|---|---|---|---|---|
| 10:19 | 7695/7700 | **7695** | 7700 | filled, sell 1.95 / buy 1.40 → 0.55 |
| 10:23 | 7690/7695 | 7690 | **7695** | filled, sell 1.70 / buy 1.20 → 0.50 |

The second spread's LONG leg is the first spread's SHORT leg. That is the COMMON
case, not a tail: `signal.width = 5.0` against SPX's 5-point ladder makes
consecutive candidates share a strike, which is the same fact
[overlapping verticals](2026-08-17-overlapping-verticals.md) was written about.

Argus sent `BUY_TO_OPEN` (`build._ACTIONS`). The account was short 2× the 7695
from four minutes earlier, so tastytrade labelled the effect **relative to the
account's position** and echoed back `Buy to Close`. `position_before: -2 →
position_after: 0` is that fact in two fields.

`marker._decode_fill` compared the broker's suffix against the direction in
Argus's own identifier and dropped the record. Downstream:
`outcome._net_legs` saw one leg symbol where it needed two → `("1 filled leg(s),
expected 2",)` → `opened_contracts = 0` → `Outcome.filled` False →
`SHAPE_UNFILLED`. A filled position was scored as one that never opened, and
re-scored counterfactually at settlement on the PROPOSED size.

**The two fields were never in conflict.** One says what Argus was doing; the
other says what the fill did to the account. On a chained book they routinely
disagree and nothing is wrong.

## The runtime was never affected

Worth stating explicitly, because the reports looked alarming and the trading
did not go wrong.

`store.signed_leg_delta` is deliberately open/close-agnostic — it reads
`action.sells` only — so `_pair_spreads` netted `7690 → −2, 7695 → +2` inside the
second idea's own group, `7695` cancelled to zero ACROSS ideas, and the
divergence invariant against `positions` passed. Both spreads were watched all
afternoon; the gate's 56 `concurrent_limit` denials show it counted two.

The economic position was one 7690/7700 10-wide at $210 net credit, max loss
$1,790. **No rail was breached**: two independent overlapping 5-wides carry the
same 1,790 aggregate, and `max_concurrent_positions × max_loss_per_position ≤
daily_max_loss` (2 × 1250 = 2500) bounded it — which is the job that welding is
for. The narrower true statement is that `max_loss_per_position` does not
describe any object that exists on a chained book, and `daily_max_loss` is the
only thing bounding the contiguous exposure.

## What replaced it

Two changes, and the SHAPE of the second is the durable lesson.

**1. Ask about the cash sign and the strike, not the suffix.**
`outcome._role_problems` resolves each fill to the leg it moved (its OCC strike
against the candidate key's two strikes) and checks that the leg moved premium
the way that role must: an opening short leg collects, an opening long leg pays,
and a close reverses both. Neither input is position-relative, so neither is
relabelled.

This is strictly stronger than what it replaced. It still refuses the corruption
the old check was reaching for — an exit's fills carrying an entry identifier
show up as a short leg PAYING premium on an open — and it additionally catches a
fill on a strike belonging to no leg of this candidate, which the old check could
not see. On a chained book one strike is legitimately a leg of two different
spreads, so "is this fill even mine?" is a real question.

**2. A rejection must not delete evidence.** The first attempt at this fix kept
the check per-record in `_decode_fill` and returned `None`. The test suite caught
it: `test_a_leg_both_bought_and_sold_in_one_direction_is_refused` went green for
the wrong reason. Filtering the offending record at decode meant `_net_legs`
never saw the inconsistency, so a knowingly-broken execution scored CLEAN — the
same defect as the original, one layer over.

So the check moved to `executions()`, where a violation becomes an entry in
`Execution.problems` and the records survive. **A per-record drop is silent by
construction; `problems` argues for itself.**

**Considered and rejected:** asserting that an opening group nets a credit. It
catches the entry/exit swap with no strike parsing, but it is blind to a
wrong-strike fill and it duplicates a rule `_open_spreads` already enforces from
the order ledger. Role-plus-cash-sign subsumes it.

## `Outcome.shape` asks `problems` before `filled`

An independent defect the same day exposed. `_net_legs` reports zero contracts
ALONGSIDE its problems — it cannot count spreads it could not assemble — so a
malformed execution fell through `filled` onto `SHAPE_UNFILLED`. That is the one
wrong answer available: UNFILLED means "never opened", it scores the idea
counterfactually at settlement on the proposed size, and it does so silently.

It is now `SHAPE_UNPRICEABLE`, which refuses to score, returns `None` rather than
a number, and prints under `mark`'s `!!` line. **A position whose records will not
assemble must never be indistinguishable from one that never traded** — the two
differ by real money, and only one of them is a hole in the record.

This matters beyond the relabelling case: it is the general rail for every future
cause of a malformed execution.

## Consequences

- `occ.py` gained `occ_strike`. Its docstring previously said "only the expiry is
  exposed" because strike and right are carried as typed fields wherever they are
  needed — **true when written, false the moment a reader appeared for which it is
  not.** An `order.fill` record carries `symbol` and nothing else about the
  instrument, `candidate_id` NULL by construction. Same rule as the
  `get_live_orders` comment in
  [unmappable orders](2026-08-20-unmappable-orders-reach-reconciliation.md):
  a comment naming the callers it is safe for expires when one is added.
- `desk` inherits the fix — it reuses `marker`'s fill reconstruction rather than
  scoring a second way.
- **2026-08-25 was never marked for real** (every run was `--dry-run`), so no
  record needs correcting. Re-running `mark` now scores both spreads.

## Left open

Two, both raised by the same mechanism and neither fixed here.

**`watchdog.note_fills` booked an entry as an exit.** It records
`fill.action.closes and fill.position_after == 0`, and seq 420587 satisfies both
literally. For `book_settle_s` the watchdog believed it had closed the 7695,
suppressing `held_but_unbooked` cover on that symbol. Bounded and benign on the
day; still exactly backwards, and it has the same root cause as everything above.

**`execution.models.infer_direction` is the exit mirror**, and it is on the live
path rather than the reporting one. Written up separately in
[relabelled closing legs](2026-08-25-relabelled-closing-legs-open.md).
