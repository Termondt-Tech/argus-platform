-- 0001_initial: the hash-chained audit log (spec §6).
--
-- The black-box recorder. Every signal, gate decision, and order writes here.
-- `seq` is assigned by the application (a single serialized writer owns the
-- chain) rather than AUTOINCREMENT, so the hash chain is deterministic and
-- gap-detectable. `prev_hash` links each row to its predecessor; `hash` covers
-- (seq, ts, tier, category, payload, prev_hash). Tampering with any row breaks
-- verification from that row forward.
--
-- Immutable: never edit this file after it has shipped. Add a new migration.

CREATE TABLE audit_log (
    seq       INTEGER PRIMARY KEY,   -- monotonic, app-assigned; no gaps
    ts        TEXT    NOT NULL,      -- ISO-8601 UTC, millisecond precision
    tier      INTEGER NOT NULL,      -- 1 = sequenced (crash-critical), 2 = telemetry
    category  TEXT    NOT NULL,      -- e.g. 'gate.decision', 'order.fill', 'kill_switch.trip'
    payload   TEXT    NOT NULL,      -- canonical JSON
    prev_hash TEXT    NOT NULL,      -- hash of seq-1 (genesis = 64 zeroes)
    hash      TEXT    NOT NULL,      -- sha256 over the canonical record
    CHECK (tier IN (1, 2))
);

CREATE INDEX idx_audit_log_category ON audit_log (category);
CREATE INDEX idx_audit_log_ts ON audit_log (ts);
