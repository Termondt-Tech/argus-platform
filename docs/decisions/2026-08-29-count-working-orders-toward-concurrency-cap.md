# Count working opening orders toward `max_concurrent_positions` (2026-08-29)

*P4 from the [2026-08-28 exit-attempt taxonomy pass](2026-08-28-exit-attempt-taxonomy-and-halt-scope.md),
Finding 6. Mechanical and additive — built directly, no design gate, per the
[P3–P6 scope prompt](../tasks/argus_p3_p4_p5_p6_v0313_scope_prompt.md).*

> BUILT. `src/` only — **no new config key**, the image deploys alone.

## The gap

`RiskSnapshot.open_spread_equivalent` was `max(ceil(broker_legs / 2),
ledger_spread_count)`. Both terms count FILLED positions. Neither counted an order
that has been sent and has not filled. Between submit and fill that window is
normally seconds and the price-walking loop owns it — but on 2026-08-28 an entry
walk abandoned order 1424563 after a failed cancel (`_request_cancel` is
best-effort), and it sat `working` for two hours. Had it filled it would have been
a third spread against a cap of two, ~$2,550 against `daily_max_loss = 2500`.

## What was built

`open_spread_equivalent` gains a third term, `working_open_spread_count`, which
**ADDS** rather than folding into the `max()`:

```
max(ceil(broker_legs / 2), ledger_spread_count) + working_open_spread_count
```

The value is the number of distinct trade ideas
(`open_working_spread_equivalents`, `execution/working_orders.py`) with an OPENING
order still working at the broker, polled by the watchdog on its
`refresh_interval_s` (and on `request_refresh`, i.e. immediately after a fill) and
read late-bound by `RiskSnapshotBuilder` — the exact wiring `ledger_spread_count`
already uses for overlapping verticals.

## Design questions, settled

- **Where does the count come from?** `get_live_orders()` — the same
  broker-derived source `DuplicateOrderGuard` and restart-time reconciliation
  already trust, routed through the same audited + throttled stack. No second
  source of truth. The **stream** was rejected: the motivating incident was a cert
  outage that degraded exactly the order stream, and an event-driven tracker built
  on it would have been blind at the one moment it mattered.

- **Entry-side or exit-side orders?** Opening only —
  `direction is OrderDirection.OPEN`. An exit's own order closes a position
  `open_spread_equivalent` already counts; adding it would UNDER-count available
  concurrency for new entries at the worst moment. Entries and exits are different
  call sites anyway (the gate only ever runs before an entry), so there is no
  re-entry check to worry about here.

- **Per what unit?** Per `external_identifier` (trade idea). A walk reprices by
  cancel+replace — new broker id, same identifier — so several rungs can read
  WORKING at once under one idea, and that is still one spread. Matches
  [overlapping verticals](2026-08-17-overlapping-verticals.md).

- **The welded invariant.** `max_concurrent_positions × max_loss_per_position ≤
  daily_max_loss` (2 × 1250 = 2500) is untouched: this change makes the cap count
  MORE things toward the same limit, never raises the limit. It tightens the gate,
  which is the safe direction for that invariant.

- **`None` / unread.** A broker-read failure in the watchdog poll **cannot halt
  the watchdog** — the one place in that module that swallows an exception,
  deliberately: the count only gates NEW entries, and position monitoring must not
  be taken down by an order-level read blip (the same asymmetry P3 is about). On
  failure the last good count stands (a missed working order would only ever LOWER
  the count, so "keep the last value" fails toward blocking an entry) and
  `watchdog.working_orders_unread` telemetry is written.

## What was NOT done

- **No snapshot-time broker call.** `RiskSnapshotBuilder.build()` stays sync and
  pure; the async read lives in the watchdog, cached, exactly as the ledger count
  does.

- **The gate constructor is unchanged.** `open_spread_equivalent` remains the one
  place all concurrency terms compose, so the gate logic (`>= self._max_concurrent`)
  did not move.

- **Foreign working orders are not counted.** A hand-placed order is a
  reconciliation concern, not an entry-budget one; `is_argus_identifier` filters
  mappable orders. Unmappable orders under an Argus identifier ARE counted,
  regardless of direction — working-or-worse, the guard's own posture.

## Still open after this

- **`infer_direction` can mislabel a closing order as opening** when the account
  is net flat on that strike
  ([relabelled closing legs](2026-08-25-relabelled-closing-legs-open.md)). Such an
  order would be miscounted here as a working opening spread — inflating the count,
  blocking an entry. Fail-closed direction; the fix belongs with that decision.

- **Freshness is bounded by `refresh_interval_s` (15s), not instantaneous.** Fine
  for the finding, which is about a *long-lived* abandoned order, not a
  sub-second race. A brief broker outage spanning exactly a gate evaluation, with
  a pre-existing abandoned order under a *different* identifier, plus recovery,
  could still let one entry through — strictly better than today (always
  uncounted), noted not fixed.

## Tests

`tests/test_working_orders.py` (the pure counter — opening-only, per-idea,
unmappable, foreign), `tests/test_gate.py` (working orders ADD to the equivalent;
the Finding 6 case is refused; audit payload carries the count),
`tests/test_snapshot.py` (late-bound read at every build),
`tests/test_watchdog.py` (poll exposes the count; a failing poll never halts and
keeps the last value; no source → zero).
