# A relabelled closing leg can record an exit as an entry — CLOSED

**2026-08-25 — OPEN. Analysis only; nothing changed.**
**2026-09-01 — OBSERVED LIVE. 2026-09-03 — the ledger row read back.**
**2026-09-03 — FIXED. Mint over infer, at the two sites that still read the
broker's inference — see "What shipped (2026-09-03)" at the end of this file.**

Two updates since this was written, both evidence, neither a fix:

1. **The unresolved branch below is answered: tastytrade RELABELS, it does not
   reject.** Observed three times in one session
   ([second live-fire day](2026-09-01-second-live-fire-day.md)). So the "exit never
   places at all" variant is off the table and the mechanism this record describes is
   the real one.
2. **`orders.direction = 'open'` on a closing order is now READ, not inferred.** The
   three 2026-09-01 closing rows all carry it — see *What the ledger actually holds*
   at the end of this file. The prediction in *The mechanism* is confirmed at the row.

~~Still OPEN, and unchanged by either: the fix, and the PARTIAL-close
precondition, which has never fired.~~ **The fix is BUILT (2026-09-03), exactly
as the "Preferred direction" section below specifies. The PARTIAL-close
precondition has still never fired — the fix makes that case safe when it fires;
it does not, and cannot, exercise it.**

The exit-path mirror of
[broker relabels leg actions](2026-08-25-broker-relabels-leg-actions.md). That one
was a reporting defect and is closed. This one is on the LIVE path — it writes the
order ledger the watchdog reassembles positions from — so it is written up rather
than fixed alongside a scoring change.

## The mechanism

`infer_direction` ([`execution/models.py`](../../src/argus/execution/models.py))
derives an order's direction from the leg actions **the broker reported back**:

```python
if any(leg.action.opens for leg in legs):
    return OrderDirection.OPEN
```

The comment above it is accurate about Argus — "Argus never mixes opening and
closing legs in one order" — and that is exactly the assumption the broker does
not share. tastytrade labels each leg relative to THE ACCOUNT'S POSITION, so a
chained book produces mixed leg sets on single-purpose orders.

**The asymmetry is what makes this an exit problem.** "Any opening leg wins"
happens to be right for a relabelled ENTRY: 2026-08-25's real entry came back as
`[Sell to Open 7690, Buy to Close 7695]` and inferred OPEN correctly. Reverse it
and the same rule is wrong. Closing 7690/7695 while 7695/7700 is still open means
selling to close a 7695 the account is net FLAT on, which comes back `Sell to
Open`, and `[Buy to Close 7690, Sell to Open 7695]` infers **OPEN** for an order
whose whole purpose is to close.

Nothing downstream corrects it:

- `ack_from_response` and `broker.replace` both re-attach `candidate_id` from the
  order Argus sent, but neither re-attaches `direction` — the inferred value
  reaches `record_order` on the first submission, before any reprice.
- `_upsert_order`'s `ON CONFLICT` clause does not update `direction` at all, so
  the first write wins and the later fill events cannot repair it.

Result: `orders.direction = 'open'` on a row that is a closing order.

## What it costs

**A full close: nothing.** `_pair_spreads` computes `net` with
`signed_leg_delta`, which reads `action.sells` and is indifferent to open/close.
The legs still cancel, `outstanding` is empty, and the group short-circuits before
the direction is used for anything. This is why the hazard has been latent.

**A partial close: a live half-spread with no stop being evaluated.** With
contracts still outstanding, the group reaches:

```python
opens = [r for r in legs if str(r["direction"]) == str(OrderDirection.OPEN)]
head  = max(opens, key=lambda r: int(r["oid"]))
entry_credit = Decimal(str(head["limit_price"]))
if entry_credit <= 0:  # -> malformed
```

The mislabelled closing order is now in `opens`, it has the highest
`broker_order_id`, so it becomes `head` — and `build._limit_price` signs a CLOSE
negative, so `entry_credit <= 0` and the spread is reported `malformed` instead of
watched. Per
[spent exits on /status](2026-08-21-a-spent-exit-attempt-is-visible-on-status.md),
`scan.malformed` pages `watchdog.unwatchable` once per episode and **appears
nowhere on `/status`** — the visibility gap that record left deliberately open.

So the failure is: a real, partially-closed, still-live position stops being
watched, announces itself once, and is invisible from then on.

**Scoring is already safe.** `marker` groups fills by the direction in Argus's own
`external_identifier`, never by this column, and after today's fix a relabelled
closing leg prices correctly — `test_a_relabelled_closing_sell_is_kept_and_priced`
covers exactly this. The exposure is the ledger, not the record.

## Preconditions

All three, together:

1. Two spreads chained on a shared strike — routine at `signal.width = 5.0`
   against SPX's 5-point ladder, and observed on 2026-08-25.
2. An exit on the spread whose LONG leg is the other's SHORT leg.
3. That exit partially fills.

(2) has never happened: no exit fired on 2026-08-25. (3) is in the NOT YET
EXERCISED set from the live-fire pass.

~~There is a second, unresolved branch: tastytrade may **reject** `Sell to Close` on
a flat position rather than relabelling it. That is a different failure — the exit
never places at all, and the one-attempt rule in
[spent exit attempt](2026-08-21-exit-attempt-spent-is-not-the-end.md) then applies
to a position that was never actually attempted. **Which of the two happens is not
known**, and guessing decides the fix.~~

**RESOLVED 2026-09-01: it relabels.** An opening order's leg came back
`Sell to Close` and two closing orders' legs came back `Sell to Open`, in one
session. The reject variant does not occur, so the one-attempt rule is not implicated
and the fix is chosen on the ledger question alone.

## This one is provable in cert

Unlike the daily max-loss breaker, which is structurally inert in sandbox, cert
tracks positions well enough to exercise this: `position_before: -2 →
position_after: 0` on seq 420587 is cert doing the relabelling that starts the
whole chain. So the reject-vs-relabel question above is answerable by a deliberate
chained pair and an exit, run outside the session window per the standing
broker-side-effect discipline. ~~**Answer that before choosing a fix.**~~ **In the
event it answered itself: 2026-09-01 traded a chained pair and exited it in the
ordinary session, no deliberate experiment needed.**

## Preferred direction (decided and built 2026-09-03)

Prefer Argus's own identifier over the broker's inference, on exactly the
principle the entry-side fix established: `external_identifier` is a field Argus
MINTED and the broker echoes verbatim, while the leg action is a field the broker
COMPUTED against account state. Where they disagree about intent, the minted one
is the answer.

Concretely: `map_placed_order` takes the direction from
`parse_external_identifier` when the identifier is Argus's own, and falls back to
`infer_direction` only for foreign orders, which is the case that rule was
actually written for. This needs `parse_external_identifier` to move from
`execution/build.py` to `execution/models.py` — `ORDER_ID_PREFIX` and
`is_argus_identifier` already live there, and `build` imports `models`, so leaving
the parser where it is would cycle.

**A disagreement must not halt.** It is now known to be normal on a chained book.
Telemetry at most.

Rejected as insufficient: repairing it in `_pair_spreads` (leaves a wrong column
for every other reader), or in `_upsert_order` (the same wrong value arrives from
two call sites and reconciliation has its own view).

## Related, same root cause

`watchdog.note_fills` records `fill.action.closes and fill.position_after == 0`
as "a leg Argus's own exit just took flat". Seq 420587 satisfies both literally —
it is an ENTRY — so the watchdog briefly counted an opening fill as closing-fill
cover and suppressed `held_but_unbooked` on that symbol. Bounded by
`book_settle_s` and benign on the day, but it reads the same relabelled suffix and
should be reconsidered in the same pass.

## What the ledger actually holds (read 2026-09-03)

The read this record's prediction was owed. Against the live cert DB, read-only:

```
1430448  argus-close-20260901-put-7605-7600   dir=open   Filled      lim=-1.25
1430542  argus-close-20260901-put-7600-7595   dir=open   Cancelled   lim=-0.05
1430543  argus-close-20260901-put-7600-7595   dir=open   Filled      lim=-0.10
```

Three closing orders, all durably `direction = 'open'`, exactly as *The mechanism*
above predicts. Note `1430542`/`1430543` are two rungs of one walk: the mislabelling
is per ROW, so a repriced exit writes it once per rung, and `head` in `_pair_spreads`
is `max` by `broker_order_id` — the LAST rung of a mislabelled close is what would win
the tie against a real opening order.

**The corroborating evidence is not durable in the ledger, and that shapes any future
forensic on this.** `order_legs` was empty for these rows — 0 rows table-wide — because
the sandbox reset deletes `order_legs` alongside `positions` while deliberately keeping
`orders` ([sandbox reset](2026-08-17-sandbox-reset-order-ledger.md)). So the broker's
verbatim leg actions survive only in the audit log:

- `order.submit_attempt` — the order as Argus BUILT it (`Buy to Close` / `Sell to
  Close`, direction `close`).
- `order.submitted` — the order as the broker RETURNED it, which is what
  `map_placed_order` reads. **Its payload is `OrderAck.to_payload()`, so the order is
  nested under an `"order"` key**; a flat `payload["legs"]` reads empty and looks like
  a missing record when it is a shape mismatch.

Those two side by side are the per-leg picture of the relabelling at the instant it
happens, and they are hash-chained, so they will still be there whenever the fix is
taken up. The ledger column will not tell you more than it already has.

### The transformation, read off the chain

Sent vs. returned, same record pair, 2026-09-01 (strikes abbreviated):

```
618912  18:43:19  submit_attempt  open-...-put-7600-7595   dir=open    7600 Sell to Open   7595 Buy to Open
618918  18:43:23  submitted       open-...-put-7600-7595   dir=open    7600 Sell to Close  7595 Buy to Open
619072  18:44:58  submit_attempt  close-..-put-7605-7600    dir=close   7605 Buy to Close   7600 Sell to Close
619079  18:45:01  submitted       close-..-put-7605-7600    dir=open    7605 Buy to Close   7600 Sell to Open
625199  19:31:23  submit_attempt  close-..-put-7600-7595    dir=close   7600 Buy to Close   7595 Sell to Close
625205  19:31:26  submitted       close-..-put-7600-7595    dir=open    7600 Buy to Close   7595 Sell to Open
625218  19:31:30  submit_attempt  close-..-put-7600-7595    dir=close   7600 Buy to Close   7595 Sell to Close
625224  19:31:31  submitted       close-..-put-7600-7595    dir=open    7600 Buy to Close   7595 Sell to Open
```

Five things this pins that the fills alone did not:

1. **The rewrite is the SUBMISSION RESPONSE**, not a later event. `dir` is already
   wrong at `order.submitted`, which is the record `record_order` writes from — so
   there is no window in which the row was ever right.
2. **Exactly ONE leg is relabelled per order, and one is enough.** `infer_direction`
   is `any(leg.action.opens)`, so a single flipped leg carries the whole order.
3. **The pivot is the SHARED STRIKE, every time.** 7600 is the long leg of
   `7605-7600` and the short leg of `7600-7595`; 7595 is short in `7600-7595` and
   flat elsewhere. The relabelled leg is always the one the rest of the book has an
   opinion about — which is the chaining precondition, doing exactly what it was
   predicted to do.
4. **618918 is the benign ENTRY case, observed.** An opening order came back
   `Sell to Close` on 7600 — the account was long 7600 from the 14:00 spread — and
   still inferred `open`, because the *other* leg was `Buy to Open`. "Right by luck",
   with the luck now visible: had the untouched leg been the one relabelled, an
   entry would have recorded as a close.
5. **Both rungs of the same walk (625205, 625224) were relabelled identically.** The
   mislabelling is deterministic given the book, not a race — so a reprice does not
   sometimes escape it, and `head` being the LAST rung changes nothing.

**Argus's own intent IS durable, at Tier 1, one record earlier.**
`order.submit_attempt` records `direction = close` correctly (`AuditLog.record` is
Tier 1 — awaited, hash-chained, never dropped). So the ledger column is repairable
from the chain if a backfill is ever wanted: the truth was written down 3 seconds
before the wrong value was.

**None of this changes the preferred direction above** — mint over infer. It removes
the last reason to wonder whether the column is really wrong on disk. It is, it is
wrong from the first write, and the correct value was in hand at the time.

## What shipped (2026-09-03)

The preferred direction above, built exactly as specified — **mint over infer** —
at the two sites that still read the broker's inference. `src/` only, no config
key, no migration, so the image deploys alone. Approved scope: Richard signed off
2026-09-03 (mint over infer; `note_fills` in the same pass; no backfill).

**Two call sites changed:**

1. **`map_placed_order`** (`execution/models.py`) now takes `direction` from
   `parse_external_identifier` when the order is Argus's own, with
   `infer_direction` unchanged as the fallback for a foreign order — the case it
   was actually written for. `external_identifier` is computed ONCE into a local
   that feeds both the parse and the `Order` field, and an AST test pins the
   ordering (parse before inference, one local, no second computation) so a
   future edit cannot silently re-open this. Fixing the mapper fixes
   `working_orders.open_working_spread_equivalents` by construction — it reads
   `Order.direction` — so that module needed no code change, only the docstring
   that said "a known residual, not fixed here" replaced with this closure, plus
   the regression test that proves it on the real 1430448 shape through the
   mapper rather than assuming it.
2. **`PositionWatchdog.note_fills`** (`execution/watchdog.py`) decides "a leg
   Argus's own close just took flat" the same way: the identifier's CLOSE wins,
   `fill.action.closes` remains the fallback for a foreign fill, and the
   relabelled-entry case (an `argus-open-` identifier, a `Buy to Close` action,
   `position_after == 0` — seq 420587's exact shape) buys no `held_but_unbooked`
   suppression and pages as it always should have.

**Telemetry: Tier 2 only, two categories.** The task doc left open whether the
`note_fills` disagreement should fold into one category with a `source` field;
decided: **separate categories**, because the two surfaces answer different
questions (a ledger write versus a fill-consumer allowance) and neither payload
naturally shares the other's fields.

- `order.direction_disagreement` — written by `AuditedOrderExecution.submit` and
  `.replace`, immediately after the Tier 1 `order.submitted` / `order.replaced`
  record they already write, carrying `broker_order_id`,
  `external_identifier`, `minted_direction` (the identifier's answer, which is
  what got written) and `broker_inferred_direction` (`infer_direction`'s answer,
  for the record). Deliberately NOT written from `get_live_orders()`: that path
  polls on every cycle and a chained-book relabelling is a durable property of
  one order — per-poll telemetry would be noise re-reporting an already-known
  fact, and the write moment is precisely where `_upsert_order`'s
  first-write-wins durability means the value being written now is the value
  that sticks.
- `watchdog.note_fills_disagreement` — written by `note_fills` for the reverse
  disagreement (Argus's own order says OPEN, the broker's leg action says
  CLOSE).

A disagreement never halts, never raises, never writes Tier 1, and feeds no
rail — mutation-verified both ways (a `raise` and a Tier 1 rerouting each fail
their tests).

**Deliberately NOT changed:**

- **No backfill of historical `orders.direction` rows** — Richard's explicit
  decision, 2026-09-03. The truth is durable at Tier 1 (`order.submit_attempt`
  holds the correct `close` three seconds before `order.submitted` wrote the
  broker's inference), so the column IS repairable from the chain if it is ever
  wanted. What a backfill is not is free: it would open a new write path against
  a table whose design principle is first-write-wins, for a column nothing
  safety-critical reads (`marker` scores off `external_identifier`). The three
  2026-09-01 rows stay `dir=open` forever, as the historical record of the bug
  they are.
- **`_upsert_order`'s `direction` column stays INSERT-only** — not added to the
  `ON CONFLICT DO UPDATE SET` clause. Pinned by a first-write-wins test that
  the mutation fails.
- **`store._apply_position` / `signed_leg_delta`** — untouched. They read
  `action.sells` for the SIGN of a position move, deliberately
  open/close-agnostic; that is the already-shipped entry-side fix
  ([broker relabels leg actions](2026-08-25-broker-relabels-leg-actions.md)),
  and this pass touches no position-sign math anywhere.
- **`residual_orders.py`'s `minted_direction`** — untouched apart from its
  `parse_external_identifier` import re-pointing at the function's new home. It
  still derives from `state.sent.direction`, never from an order that came back
  off the wire, and its AST test (no `.direction` reads in that module) still
  holds.
- **`_await_settled`'s FILLED-identifier-wide / settle-scoped-to-
  `broker_order_id` split** — untouched. Nothing here widens or narrows either
  half.
- **Nothing besides `direction`** in `map_placed_order` prefers the minted
  value: `limit_price`, `status`, `reject_reason` and the legs are the broker's
  facts about its own order and stay exactly as they were.

**The relocation that came with it:** `parse_external_identifier` moved from
`execution/build.py` to `execution/models.py` — beside `ORDER_ID_PREFIX` and
`is_argus_identifier`, which it already depended on — so the mapper can call it
without an import cycle (`build` imports `models`; the reverse would cycle).
Every importer re-pointed: `reconcile.py` (whose module docstring no longer
needs to justify importing `execution.build` at all — it now imports none of
it), `shadow/marker.py`, `execution/residual_orders.py`, and
`tests/test_order_build.py`, which the original fix list did not name but which
imported the parser from `build` and would have broken silently otherwise.

**Regression coverage uses the real 2026-09-01 orders**, pulled verbatim from
the transformation table above: the benign relabelled ENTRY (618918) still maps
OPEN, and the two relabelled CLOSEs (619079; 625205/625224, one per walk rung)
now map CLOSE — correcting what they would have asserted before the fix.
Synthetic cases cover the chained-pair partial close, the foreign-identifier
fall-through in both directions, and seq 420587's `note_fills` shape.

**The PARTIAL-close precondition is still unexercised.** Every relabelling seen
so far, entry or exit, has been on a FULL open or close — the benign case by
construction. This fix makes the partial case safe when it fires; it does not,
and cannot, exercise it.
