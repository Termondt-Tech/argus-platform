-- 0004_orders: Argus's durable record of the orders it has placed (spec §7).
--
-- Two jobs, and neither of them is "an order state machine". This is a RECORD of
-- what the broker has told us, never a plan to be resumed: after a crash, Argus
-- asks the broker what actually happened (`get_live_orders()`) and hard-halts on
-- anything unexplained. It never replays a previously-sent message.
--
--   1. `orders` answers "is this broker order one of mine?" at restart, which is
--      what turns an unexplained working order into a reconciliation mismatch
--      instead of a silent surprise (`argus.reconcile.reconcile_orders`).
--   2. `order_legs.applied_quantity` is the idempotency ledger for fill
--      application. `PlacedOrder` events push CUMULATIVE per-leg
--      `remaining_quantity` and repeat on every status change, so applying "what
--      the broker says is filled" on each event would book the same contracts
--      over and over. Storing what has already been applied means each event
--      contributes only its delta, and a re-delivered (or replayed-after-restart)
--      event contributes nothing.
--
-- Why `remaining_quantity` and not the per-fill records: chunk 1 MEASURED
-- `remaining_quantity` coming back per leg from the real sandbox API (2026-07-29).
-- Per-fill detail was never observed, because nothing was allowed to fill. The
-- ledger is therefore built on the field that is known to arrive; fill records,
-- when present, only price the position.
--
-- `positions` (migration 0002) is written from this same transaction, so the
-- expected-position set reconciliation reads can never disagree with the ledger
-- that produced it. Immutable: never edit this file — add a new migration.

CREATE TABLE orders (
    broker_order_id     INTEGER PRIMARY KEY,   -- the broker's id; the only stable key
    external_identifier TEXT    NOT NULL,      -- Argus's own key (build.external_identifier)
    candidate_id        TEXT,                  -- joins to shadow-mode history; null if unknown
    direction           TEXT    NOT NULL,      -- 'open' | 'close'
    underlying          TEXT    NOT NULL,
    status              TEXT,                  -- last status seen, verbatim from the broker
    limit_price         REAL    NOT NULL,      -- signed: + credit, - debit
    recorded_at         TEXT    NOT NULL,      -- ISO-8601 UTC, first seen
    updated_at          TEXT    NOT NULL       -- ISO-8601 UTC, last seen
);

-- Orders are looked up by Argus's key when matching a trade idea to what the
-- broker is holding; the primary key already covers the id lookup.
CREATE INDEX idx_orders_external_identifier ON orders (external_identifier);

CREATE TABLE order_legs (
    broker_order_id  INTEGER NOT NULL REFERENCES orders (broker_order_id),
    symbol           TEXT    NOT NULL,       -- OCC symbol
    action           TEXT    NOT NULL,       -- LegAction, verbatim ('Sell to Open', ...)
    quantity         INTEGER NOT NULL,       -- contracts ordered on this leg
    -- Cumulative contracts already booked into `positions` for this leg. Monotonic
    -- by construction (see store.py): a late or out-of-order event reporting LESS
    -- filled than we have applied is ignored rather than allowed to unwind a
    -- position that really exists.
    applied_quantity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (broker_order_id, symbol)
);
