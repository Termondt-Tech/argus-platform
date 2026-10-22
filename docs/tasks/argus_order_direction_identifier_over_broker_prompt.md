# Argus: Order Direction — Prefer the Identifier Argus Minted Over the Broker's Relabelled Leg Action

## Priority

**APPROVED — Richard signed off on the fix direction 2026-09-03** (mint over infer;
`note_fills` included in the same pass; no backfill of historical rows). This document
records the approved scope, not a standing order to build it: **implementation still
needs an explicit "start" from Rich**, per `docs/tasks/README.md`'s non-negotiable. Do
not begin until told to.

This is a live path — it changes how `orders.direction` and
`PositionWatchdog._closed_legs` get written — so build exactly what is specified below.
If anything here forces a material deviation once you're in the code (a call site that
doesn't fit the pattern, a test that reveals the fix shape is wrong), stop and re-raise
rather than deciding it silently; the P2/P3/P5 sign-off gate applies to the *shape* of
the fix, and this document is that shape.

Background, read first:
[relabelled closing legs](../decisions/2026-08-25-relabelled-closing-legs-open.md)
(the mechanism, the transformation table, the preferred direction),
[broker relabels leg actions](../decisions/2026-08-25-broker-relabels-leg-actions.md)
(the entry-side sibling, already fixed),
[the second v0.3.13 day](../decisions/2026-09-01-second-live-fire-day.md) (live
observation, 2026-09-01).

## The bug, in one paragraph

tastytrade labels each leg relative to the **account's** position, not Argus's intent.
On a chained book (routine at `signal.width = 5.0` against SPX's 5-point ladder),
closing a spread whose long leg is another live spread's short means selling to close a
strike the account is net FLAT on, which the broker echoes back as `Sell to Open`.
`infer_direction` (`any(leg.action.opens)`) then reads a closing order as an entry.
Benign on a full close today (`_pair_spreads` short-circuits before reading direction).
On a partial close, the mislabelled order becomes `head`, its negative CLOSE limit
fails `entry_credit <= 0`, and a **live half-spread reports `malformed` instead of
watched** — no stop evaluated, no `FLATTEN_BEFORE_CLOSE`, nowhere on `/status`.

Observed live 2026-09-01, three times — always at the shared strike, always exactly
one leg per order, deterministic across both rungs of a repriced walk. The read on
2026-09-03 confirmed `orders.direction = 'open'` is durably written on all three
closing rows, and — the fact that shapes the fix below — **Argus's own correct
intent survives one record earlier, at Tier 1**: `order.submit_attempt` carries
`direction=close` three seconds before `order.submitted` overwrites it with the
broker's inferred value. See the transformation table in the background doc for the
full evidence.

## Where `direction` is decided and read today

- **Written**: `map_placed_order` ([`src/argus/execution/models.py:723`](../../src/argus/execution/models.py))
  calls `infer_direction(legs)` — the single caller of that function anywhere in
  `src/`. `AuditedExecution.submit`/`.replace` ([`src/argus/execution/audited.py`](../../src/argus/execution/audited.py))
  then call `self._store.record_order(ack.order)`, which reaches `_upsert_order`
  ([`src/argus/execution/store.py:487`](../../src/argus/execution/store.py)) — **`direction`
  is absent from its `ON CONFLICT` clause, so the first write is permanent.**
- **Read for spread reassembly**: `_pair_spreads` ([`src/argus/execution/store.py:1006`](../../src/argus/execution/store.py))
  filters `orders.direction = 'open'` to pick `head`.
- **Read for the concurrency cap**: `open_working_spread_equivalents`
  ([`src/argus/execution/working_orders.py:56`](../../src/argus/execution/working_orders.py))
  filters `order.direction is OrderDirection.OPEN` on the SAME `Order.direction`
  `map_placed_order` sets. Its own docstring already names this exposure and says
  *"the fix belongs with that decision, not here"* — this is that decision. **No code
  change needed at this site** — fixing `map_placed_order` fixes it by construction —
  but its stale docstring must be updated (see Fix 5) and it needs a regression test
  (see Deliverables).
- **A separate, already-correct mechanism — do not touch**: `residual_orders.py`'s
  `minted_direction` compares `state.sent.direction` (what Argus itself built) against
  `parse_external_identifier(external_identifier)`, never against `Order.direction`.
  That is the identifier-over-broker principle already applied where it was safe to
  apply in isolation (2026-08-29, P5). This document generalizes it to the two sites
  that still read the broker's inference.
- **Fills, per leg**: `PositionWatchdog.note_fills`
  ([`src/argus/execution/watchdog.py:2217`](../../src/argus/execution/watchdog.py))
  reads `fill.action.closes` — `AppliedFill.action` is the same broker-relabelled
  per-leg field `infer_direction` reads, on a different struct — to decide whether a
  leg that just went flat was Argus's OWN close, for `held_but_unbooked` suppression.
  `AppliedFill` already carries `external_identifier` ([`store.py:63-79`](../../src/argus/execution/store.py)),
  sourced from `order.external_identifier` (Argus's own, unaffected by relabelling) —
  so this is the same fix, the same primitive, no new plumbing.

## The fix

### 1. Relocate `parse_external_identifier`: `build.py` → `models.py`

Move the function as-is
([`src/argus/execution/build.py:169-188`](../../src/argus/execution/build.py)) into
`src/argus/execution/models.py`, near `ORDER_ID_PREFIX`/`is_argus_identifier`
(`models.py:53-59`) — it already depends on nothing else (`is_argus_identifier`,
`ORDER_ID_PREFIX`, `OrderDirection`, all in `models.py`), so this is a pure relocation,
no logic change. `build.py` imports from `models.py` today
(`build.py:40`); the reverse would cycle, which is why `models.py` is the target, not
the other way round.

Re-point every import:

- [`src/argus/reconcile.py:84-85`](../../src/argus/reconcile.py) — drop the standalone
  `from argus.execution.build import parse_external_identifier` line and fold
  `parse_external_identifier` into the existing `from argus.execution.models import
  (...)` block. **Update the module docstring at `reconcile.py:62-63`**: it currently
  states *"`execution.build` is imported for `parse_external_identifier` alone"* to
  justify why this module stays SDK-free. After the move `reconcile.py` no longer
  imports `execution.build` at all — rewrite the sentence to say so, and confirm
  `execution.models` is still the SDK-free import it already is (it is; no tastytrade
  import there today — keep it that way).
- [`src/argus/shadow/marker.py:68-69`](../../src/argus/shadow/marker.py) — split the
  combined `from argus.execution.build import candidate_key, parse_candidate_key,
  parse_external_identifier` line: `candidate_key`/`parse_candidate_key` stay from
  `build`, `parse_external_identifier` moves into the existing `from
  argus.execution.models import LegAction` line.
- [`src/argus/execution/residual_orders.py:114,116`](../../src/argus/execution/residual_orders.py) —
  same pattern: drop the `build` import line, fold into the existing `models` import.
- `build.py` itself — remove the function; confirm nothing else in `build.py` calls
  it internally (it does not, per current source) before deleting the import
  scaffolding it no longer needs.

Also update `src/argus/execution/build.py`'s own module docstring if it references
`parse_external_identifier` as living there.

### 2. Fix `map_placed_order` — mint over infer

```python
def map_placed_order(placed: Any) -> Order:
    ...
    external_identifier = str(getattr(placed, "external_identifier", "") or "")
    parsed = parse_external_identifier(external_identifier)
    direction = parsed[0] if parsed is not None else infer_direction(legs)
    return Order(
        direction=direction,
        ...
        external_identifier=external_identifier,
        ...
    )
```

Compute `external_identifier` once, before deciding direction, and reuse the same
local both for the `Order(...)` field and the parse — do not compute it twice under
different code paths (that's the seam a future edit could silently desync).
`infer_direction` is unchanged and stays the fallback for a foreign order (not
Argus's own identifier) — the case it was actually written for, per its own
docstring's true premise about what Argus *sends*.

### 3. Disagreement telemetry — Tier 2, and NOT inside `map_placed_order`

`map_placed_order` is a pure, duck-typed mapper with no audit handle — it must stay
that way (it is called from `broker.py`, which has no `self._audit`, as well as from
places that do). Compute the disagreement at the callers that already have one and
already record the Tier 1 event this augments:

- `AuditedExecution.submit` and `.replace`
  ([`audited.py:94-148`](../../src/argus/execution/audited.py)), immediately
  around the existing `await self._audit.record("order.submitted", ...)` /
  `"order.replaced"` calls. Recompute `infer_direction(ack.order.legs)` (cheap, pure,
  no I/O) and compare to `ack.order.direction`; when the identifier parsed AND the two
  disagree, `self._audit.telemetry("order.direction_disagreement", {...})` with
  `broker_order_id`, `external_identifier`, `minted_direction` (the identifier's
  answer, which is what got written), and `broker_inferred_direction`
  (`infer_direction`'s answer, for the record).
- **Do not add this to `get_live_orders()`.** That path polls on every reconcile
  cycle; a chained-book relabelling is a durable property of one order, and firing
  telemetry every poll would just be noise re-reporting the same already-known fact.
  Restrict this to the write moment (`submit`/`replace`), which is also precisely
  where `_upsert_order`'s first-write-wins durability means the value being written
  now is the value that sticks.
- **A disagreement must never halt, never raise, never touch Tier 1, never feed a
  rail.** Telemetry only — this is known-normal on a chained book, not a fault.

### 4. Fix `note_fills` — same primitive, same fallback shape

```python
def note_fills(self, applied: Iterable[AppliedFill]) -> None:
    now = self._clock()
    for fill in applied:
        if fill.position_after != 0:
            continue
        parsed = parse_external_identifier(fill.external_identifier)
        closes = parsed[0] is OrderDirection.CLOSE if parsed else fill.action.closes
        if closes:
            self._closed_legs[fill.symbol] = now
        elif parsed is not None and fill.action.closes:
            # Argus's own order says OPEN, the broker's leg action says CLOSE —
            # the relabelled-entry case (seq 420587). Tier 2 only.
            self._audit.telemetry(
                "watchdog.note_fills_disagreement",
                {"symbol": fill.symbol, "external_identifier": fill.external_identifier,
                 "broker_order_id": fill.broker_order_id, "action": str(fill.action)},
            )
```

`PositionWatchdog` already holds `self._audit` and already calls
`self._audit.telemetry(...)` elsewhere (`watchdog.py:2496,2517,2624`) — no new
plumbing. Decide during implementation whether the `elif` branch's telemetry is worth
a second category or should fold into `order.direction_disagreement` from Fix 3 with a
`source` field distinguishing the two call sites; either is acceptable, pick one and
be consistent with the decision doc.

### 5. `working_orders.py` — docstring only, no logic change

Fixing `map_placed_order` fixes this consumer by construction (it reads
`Order.direction`, which now prefers the identifier). Update the docstring at
[`working_orders.py:33-38`](../../src/argus/execution/working_orders.py) — it
currently says *"A known residual, not fixed here... the fix belongs with that
decision, not here"* — to say the residual is closed, point at the decision record,
and note this module needed no code change. Add the regression test in Deliverables
to prove it rather than assume it.

## Traps

- **`_upsert_order`'s `direction` column stays INSERT-only.** Do not add it to the
  `ON CONFLICT DO UPDATE SET` clause, and **do not backfill any historical row** —
  Richard's explicit "no" on backfill, 2026-09-03. Nothing safety-critical reads the
  column (`marker` scores off `external_identifier`); a backfill adds a new write
  path against a table whose whole design principle is first-write-wins, for zero
  downstream benefit.
- **`store._apply_position`/`signed_leg_delta` are unaffected and must stay
  untouched.** They use `leg.action` for the SIGN of a position move, deliberately
  open/close-agnostic — that is the already-shipped fix for the entry-side sibling
  ([broker relabels leg actions](../decisions/2026-08-25-broker-relabels-leg-actions.md)).
  This document does not touch position-sign math anywhere.
- **`residual_orders.py`'s `minted_direction` mechanism is already correct.** Do not
  "simplify" it to call through `map_placed_order` or reuse its result — it derives
  from `state.sent.direction`, Argus's own record of what it built, which is a
  different (and already-safe) source than a broker-echoed `Order`.
- **`_await_settled`'s FILLED-identifier-wide / settle-scoped-to-`broker_order_id`
  split is untouched.** Nothing here widens or narrows that asymmetry.
- **Do not widen the identifier-preference principle to any field besides
  `direction`.** `limit_price`, `status`, and everything else `map_placed_order` sets
  stay exactly as they are.
- **A disagreement must never halt.** This is the property most likely to regress
  into an accidental rail — mutation-verify it specifically (see Deliverables).

## Deliverables

1. **Code** — sections 1-5 above.
2. **Tests**, at minimum:
   - `tests/test_execution_models.py` — unit: `map_placed_order` on a synthetic
     chained-pair partial close (`external_identifier` = an Argus `close-` identifier,
     legs mixing `Buy to Close` + `Sell to Open`) asserts `direction is
     OrderDirection.CLOSE` — the identifier wins over the broker echo.
   - Same file — regression, using the **real 2026-09-01 orders as fixtures** (pull
     them verbatim from the transformation table in
     [relabelled closing legs](../decisions/2026-08-25-relabelled-closing-legs-open.md#the-transformation-read-off-the-chain)):
     both the benign relabelled ENTRY (618918, `Sell to Close` on 7600 mixed with
     `Buy to Open` on 7595, still infers OPEN — must be unchanged) and the two
     relabelled CLOSEs (619079, 625205/625224) now assert `CLOSE`, correcting what
     they'd have asserted before the fix.
   - Same file — a foreign (non-Argus) identifier still falls through to
     `infer_direction` unchanged, both directions.
   - **AST test** (repo convention — see `tests/test_status_unmanaged.py`,
     `tests/test_halt_scope.py` for the pattern), pinning that `map_placed_order`
     computes `external_identifier` and attempts `parse_external_identifier` before
     falling back to `infer_direction` — stops a future edit silently reordering it
     the way P6 (2026-08-27) had to guard elsewhere.
   - `tests/test_watchdog.py` — unit: a synthetic `AppliedFill` shaped like seq
     420587 (an Argus `open-` identifier, `action=BUY_TO_CLOSE`, `position_after=0`)
     asserts `_closed_legs` is **not** updated (no false `held_but_unbooked`
     suppression). Regression: the existing `_closed(...)` helper cases (identifier
     and action already agree) still pass unchanged.
   - Disagreement telemetry test(s): a case where `infer_direction` and the
     identifier disagree emits the Tier 2 category and category alone — asserts no
     Tier 1 record, no exception, no halt-related side effect.
   - `tests/test_working_orders.py` (or wherever that module's tests live) —
     regression: a chained-pair partial-close's closing order, previously counted as
     an opening working order, is now correctly excluded from
     `open_working_spread_equivalents`.
   - `tests/test_residual_orders.py` — confirm unchanged: `minted_direction` behavior
     must not regress from this change.
3. **Mutation-verify** (repo standard), at minimum:
   - Reorder `map_placed_order` to try `infer_direction` first — must fail the AST
     test and the chained-pair unit test.
   - Revert the `note_fills` fix to plain `fill.action.closes` — must fail its unit
     test.
   - Make the disagreement telemetry raise, or route through `self._audit.record`
     (Tier 1) instead of `.telemetry` (Tier 2) — must fail a test asserting Tier 2
     only, no exception.
   - Add `direction` to `_upsert_order`'s `ON CONFLICT DO UPDATE SET` — must fail a
     test pinning first-write-wins (add one if it doesn't already exist).
4. **A decision record.** Flip
   [relabelled closing legs](../decisions/2026-08-25-relabelled-closing-legs-open.md)
   from OPEN to CLOSED: what shipped, exactly (the two call sites, the telemetry
   category names chosen, the `working_orders.py` docstring fix), what was
   deliberately NOT changed (backfill, `_apply_position`, `residual_orders.py`,
   `_await_settled`), and the explicit no-backfill decision with its reasoning.
5. **`docs/decisions/README.md`** — flip the *"Where should an order's DIRECTION come
   from"* row from open to answered, following the struck-through convention already
   used elsewhere in that table.
6. **`CLAUDE.md`** — replace the paragraph on this item (currently ending in *"What
   is still open is the fix itself... and the PARTIAL-close precondition, which has
   never fired"*) with a one-line CLOSED pointer, following the pattern already used
   for P3/P5/D3/D5 in that file. Keep the still-genuinely-open half: the PARTIAL-close
   precondition has still never fired live — say so, it is not resolved by this fix,
   only made safe for when it does.

## Non-negotiables this must respect

- **NEVER TOUCH THE LIVE DATABASE.** Any exploration is read-only, via
  `sqlite3.connect("file:argus.db?mode=ro", uri=True)`, or the droplet's `docker
  compose exec -T argus python -` pattern. Dev/test runs use `argus.dev.toml`.
- No backfill of historical `orders.direction` rows. Decided.
- A direction disagreement is telemetry (Tier 2), never a halt, never Tier 1, never a
  rail input.
- `_upsert_order`'s `direction` column stays INSERT-only.
- `_await_settled`'s identifier-wide/`broker_order_id`-scoped split is untouched.
- `residual_orders.py`'s `minted_direction` mechanism is untouched.
- No new config key.
- `src/`-only deploy. If anything here turns out to force a config key, stop and
  say so before writing it — that changes the deploy coupling.

## Out of scope

- **Any backfill of historically-wrong `orders.direction` rows.** Decided no,
  2026-09-03.
- **The confirmation-gated exit re-attempt decision.** Unrelated; do not conflate.
- **The reprice-rejection identifier-wide fix**
  ([separate prompt](argus_reprice_rejection_identifier_wide_prompt.md)/
  [design](argus_reprice_rejection_identifier_wide_design.md)). Both invoke
  "identifier over broker echo" reasoning, but on different call sites solving a
  different problem (a walk's terminal verdict, not order direction). Do not fold in.
- **Generalizing "prefer minted over broker-computed" to any field besides
  `direction`.** Scope is exactly: `map_placed_order`, `note_fills`, and the
  automatically-fixed `working_orders.py` consumer.
- **The PARTIAL-close precondition itself.** It has never fired live. This fix makes
  that case safe *when* it fires; it does not, and cannot, exercise it.
