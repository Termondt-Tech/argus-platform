# Order execution engine — chunk 2: recording, fills, duplicate guard (spec §7/§9)

*Order recording, fill detection and the duplicate-submission guard.*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


Chunk 2 consumes fills, writes them to `positions`, extends restart-time
reconciliation to cover orders, and builds the duplicate-submission guard. It
CALLS chunk 1's primitives and never reimplements them. Like chunk 1 it is
standalone — nothing here is wired into `SignalLoop` or the shadow session body.

Decisions that are settled — do not relitigate:

- **No in-flight order state machine, and no replay of a sent message, ever.** A
  crash mid-cancel/replace is not recovered by resuming; it is recovered by asking
  the broker what actually happened (`get_live_orders()`) and hard-halting on
  anything unexplained, the same way a position mismatch halts. `store.py` is a
  RECORD of what the broker said, not a plan to be re-executed. A test asserts (by
  AST) that no chunk-2 module contains a `while` loop or a sleep — the shape a
  retry/replay would take. Cadence is chunk 3's, in chunk 3's file.
- **`classify_order` is THE order-status classification** (`execution/models.py`),
  three-way and shared by BOTH consumers — the duplicate guard and
  `reconcile_orders` — specifically so they can't drift:
    - `WORKING` — non-terminal (incl. `PARTIALLY_REMOVED`) or unknown status.
    - `FILLED` — it left a position. `OrderStatus.FILLED`, **or** a
      terminal-but-not-filled order with any leg reporting a fill (a 3-lot that
      filled 1 then cancelled is `CANCELLED` and holds a 1-lot), **or** a terminal
      order whose per-leg quantities were never reported (fail closed: no evidence
      it left nothing).
    - `DEAD` — terminal with every leg reporting zero filled.
  The trap this exists to prevent is a binary `is_terminal` check, which files
  `FILLED` next to `CANCELLED` and doubles the position. The flip side matters
  just as much: `DEAD` must stay unblocked — re-entering a spread after a
  cancelled attempt is what crash recovery needs, and blocking it would be a
  defect, not a safety feature.
- **The duplicate guard asks the BROKER, and raises rather than allowing.**
  `DuplicateOrderGuard.has_conflicting_order(external_identifier)` queries
  `get_live_orders()` — not local state, because local state is exactly what a
  crash loses. `external_identifier` is a MATCHING key with no broker-side
  uniqueness (measured, see chunk 1), so this is the only backstop spec §9 has. If
  the broker can't be reached it RAISES: an unanswerable question must never read
  as permission. Returns a `ConflictVerdict` (not a bare bool) so the blocking
  reason reaches the audit log; no `__bool__`, so `.blocked` must be explicit.
  **Chunk 3/5 wire the call**, before every brand-new trade idea's FIRST
  submission and *never* before a cancel/replace inside an attempt already in
  flight — a price-walking replace re-sends the same identifier against its own
  still-working order, so guarding there would block the loop from repricing.
- **Fill idempotency keys off cumulative `remaining_quantity`, not fill ids.**
  `PlacedOrder` events carry cumulative per-leg quantities and re-send on every
  status change, so `order_legs.applied_quantity` (migration 0004) records what has
  already been booked and only the delta moves `positions`. That field is the one
  chunk 1 MEASURED coming back from the real API; per-fill records were never
  observed (nothing was allowed to fill), so they price the position and never
  count it. An event reporting less than what is applied is a no-op — fills do not
  un-happen. Ledger and position update share one transaction.
- **`positions.average_open_price` is best-effort and safety-irrelevant.** It is
  quantity-weighted from broker-reported fill prices, carries the existing basis
  when a fill arrives unpriced, and is 0.0 when the broker reported nothing.
  Reconciliation compares `symbol` and `signed_quantity` only. A flat position is
  DELETED, not left at zero.
- **The fill consumer will not adopt a foreign fill.** An order whose
  `external_identifier` isn't Argus's (`is_argus_identifier`, `models.ORDER_ID_PREFIX`)
  is recorded Tier 1 and alerted, never applied. `positions` is Argus's record of
  ITS OWN positions; absorbing a hand-placed fill would make reconciliation agree
  with the broker by rewriting what it expected — the guess spec §6 forbids.
- **Fill consumption needed no change to `account/stream.py`.** Verified, not
  assumed: `AlertStreamerAccount._listen_orders` has decoded `PlacedOrder` frames
  and dispatched them to an injected `on_order` callback since the account layer
  was built. What was missing was a consumer. `FillConsumer` is callable with one
  order, so it drops straight into that hook. It never raises out of `consume()` —
  an exception there tears down `_pump` and takes positions and balances down with
  it.
- **Order reconciliation is asymmetric with positions, on purpose.** There is no
  "missing at broker" for orders: an order Argus recorded that the broker no longer
  lists cannot execute, and `get_live_orders()` only reaches back 24h so everything
  older would read as missing forever. An unaccounted `DEAD` order is likewise NOT
  a discrepancy — yesterday's rejected order is in every morning's answer and can
  neither fill nor hold anything. Unaccounted `WORKING`/`FILLED` orders ARE, and
  they ride in the SAME `ReconciliationResult` as position discrepancies, so they
  inherit `mismatch_halts_process`, the alert, and the refusal without a second
  rail. `Reconciler(..., execution=, order_store=)` are optional but must be passed
  together; shadow passes neither.
- **Pre-send Tier 1 writes live in a DECORATOR** (`AuditedOrderExecution`), not in
  chunk 1. It implements the same `OrderExecutionPort` and delegates, so chunk 1's
  adapter is untouched and chunk 3/5 can drop it in. The record beats the wire
  because an order that reached the exchange and isn't in the log is invisible to a
  restart. An audit write failure is NOT caught — it propagates, latches the log
  failed, and halts the process, so no order is ever sent with the recorder dead.
  It deliberately does NOT call the guard (see above).
- **`AuditLog.write(fn)` is the writer-connection gateway**, the twin of `read(fn)`.
  `positions`/`orders`/`order_legs` live in the same database as the chain and the
  process holds ONE writer connection under one `flock`; a private handle here
  would sidestep the process lock from inside the process it protects. Same lock,
  same `to_thread`. `fn` owns its transaction and must NOT touch the audit log
  (the lock is not reentrant) — audit after `write()` returns.

Audit categories added: `order.submit_attempt`/`order.submitted`/`order.submit_failed`,
`order.cancel_attempt`/`order.cancel_requested`/`order.cancel_failed`,
`order.replace_attempt`/`order.replaced`/`order.replace_failed`, `order.fill`,
`order.foreign`, `order.unmappable`, `order.duplicate_blocked` (all Tier 1);
`order.event`, `order.duplicate_check` (Tier 2 — a status change that books nothing
is the common case by volume and would otherwise drown the Tier 1 trail).

Worth measuring next time sandbox is exercised: whether a REJECTED order reports
per-leg `remaining_quantity`. If it does not, the fail-closed branch makes a
rejected identifier unsubmittable for 24h — safe, but noisier than it needs to be,
and a REJECTED-specific carve-out would then be justified by data rather than
guessed at now.
