-- 0002_positions: Argus's record of positions it believes it holds.
--
-- The reconciliation target (spec §6): broker positions are compared against
-- this table on every startup. Populated by order-recording (not yet built);
-- empty is a valid state — a fresh Argus against a flat account reconciles clean.
--
-- Keyed by broker symbol (OCC option symbol). `signed_quantity` is +long/-short.

CREATE TABLE positions (
    symbol             TEXT    PRIMARY KEY,
    underlying         TEXT    NOT NULL,
    signed_quantity    INTEGER NOT NULL,
    average_open_price REAL    NOT NULL,
    opened_at          TEXT    NOT NULL
);
