# Argus: Bound `_open_spreads`' Ledger Read to Recent Trading Days

## Priority

Not urgent — no incident, no measured latency problem yet. Filed because the
shape is clear and the fix is cheap now; it gets more invasive to reason about
the longer the ledger runs unbounded. Backlog priority — schedule whenever, but
sooner is cheaper than later (see "Why now, not later" below).

## Context

`execution/store.py`'s `_open_spreads` — the watchdog's every-`refresh_interval_s`
(15s) read of "what's currently open" — runs this query with **no date bound at
all**:

```sql
SELECT o.broker_order_id AS oid, o.external_identifier, o.candidate_id,
       o.underlying, o.limit_price, o.direction,
       l.symbol, l.action, l.applied_quantity
FROM orders o
JOIN order_legs l ON l.broker_order_id = o.broker_order_id
WHERE l.applied_quantity > 0
ORDER BY o.broker_order_id
```

`order_legs`/`orders` are deliberately **never pruned** — they're the durable P&L
record (`docs/decisions/2026-08-12-expired-position-resolution.md`: *"nothing to
gain, a P&L history to lose"*). So this read's row count grows for the entire
operational life of the system, and every single row that isn't from the current
(or very recent) trading day is provably irrelevant: this is a 0DTE strategy, so
nothing that was opened on a prior trading day can still be a legitimately open
position by the time `_open_spreads` is asked. The 2026-08-25 expiry-resolution
work (`docs/decisions/2026-08-12-expired-position-resolution.md`, addendum) taught
the **in-memory pairing logic** (`_pair_spreads`, via `_leg_is_expired`) to stop
treating an expired leg as outstanding — but that's a Python-side filter applied
*after* every historical row has already been read from SQLite, grouped, and
netted. It doesn't reduce what gets read, and was explicitly scoped not to: that
task's own constraints ruled out touching `order_legs` data or the query shape,
only the "is this leg still open" predicate.

There is also no index that would make a date-scoped version of this query cheap
— the only index on either table is `idx_orders_external_identifier`
(`db/migrations/0004_orders.sql`). `orders.recorded_at`/`updated_at` exist
(ISO-8601 UTC, "first seen" / "last seen") but are unindexed.

**Why this is worth fixing on its own terms, not just as a latency number.**
`_open_spreads` runs through the SAME connection and SAME `_chain_lock` a Tier 1
audit write takes (`SqliteOrderStore.open_spreads`'s own docstring, and
`AuditLog.write`'s docstring on why order recording shares the writer connection).
`AuditLog.read`'s contract for the *other* caller sharing that lock (`/status`) is
explicit: *"Every query is index-backed and BOUNDED."* `_open_spreads` is neither,
today. It contends for the exact lock a crash-critical Tier 1 write needs during
the moment spec §6/§9 care most about — a watchdog needing to log a stop-loss exit
*now*. At current volume this is not measured to be costing anything; the point is
that the query's cost is a function of the system's entire calendar lifetime, not
of what's actually relevant on any given day, and that only gets worse.

**Why now, not later.** Carrying forward permanently-irrelevant historical rows
into a hot read path is the kind of thing that's a one-line fix early and a
load-bearing migration/backfill problem once something downstream (a monitoring
query, a manual `tools.desk` invocation, an operator habit) starts depending on
the read including old data for some incidental reason nobody intended. Fix the
scope now, while the only consumer is the one watchdog method this document
traces in full.

## The fix — bound the SQL read, not just the Python-side predicate

Add a date-scoped `WHERE` clause to `_open_spreads`'s query, keyed on
`orders.recorded_at`, so SQLite never returns rows from trading days that cannot
possibly still be relevant. This is **in addition to**, not a replacement for,
the existing `_leg_is_expired` check in `_pair_spreads` — the two serve different
purposes and both stay:

- The SQL bound stops irrelevant HISTORY from ever being read, grouped, or
  compared against `positions` at all.
- `_leg_is_expired` still does its job for a row that IS inside the window but
  whose OWN leg has expired (i.e., the case the 2026-08-25 fix targeted) — it
  operates on individual legs' OCC expiry, which the query bound cannot replace,
  because a window wide enough to safely include "still open" rows will also
  include some rows that expired days ago but happen to fall inside the margin.

**Where the boundary comes from, and why it needs its own margin (not "today"
alone).** A literal `WHERE o.recorded_at >= <start of today, ET>` is tempting but
too tight, and unsafe for the same class of reason Task 1 of the 2026-08-25 work
had to reason through:

- The watchdog is constructed fresh per session with its own `trading_date` seam
  (`session._build_trading_side`, `PositionWatchdog(trading_date=lambda:
  trading_date, ...)`), but a crash-restart INSIDE a trading day, or a watchdog
  task that's slow to be torn down across a session boundary, could plausibly
  read against a boundary that doesn't yet reflect the current instant.
- The reconciliation rule this scan depends on for cleanup (`expiry_verdicts` /
  `_resolve_expired_positions`) runs once per session start, not continuously —
  there is a window, however small, between an ET midnight rollover and the next
  reconcile where a literal same-day-only bound could exclude a row that a wider
  margin would have safely carried.

Use **the same ET-day-boundary helper the rest of the codebase already uses for
exactly this kind of query** — `clock.et_day_bounds_utc_iso(day)` — rather than a
hand-rolled comparison (`status.py` and `risk/rehydrate.py` are the existing
callers; follow their pattern). Compute the cutoff from `today` (the same
argument `_open_spreads(conn, today)` already receives, threaded from the
watchdog's `trading_date` seam) minus a safety margin of **calendar days**, not
trading days — a fixed few-calendar-day margin needs no market-calendar import
into `execution/store.py` (which currently has none, and per this codebase's
existing discipline around keeping modules narrowly dependent — see
`reconcile.py`'s own docstring on why it stays `Config`-free — importing
`marketcalendar` here for this alone should be avoided unless you find a concrete
reason a fixed margin is unsafe). Recommend starting from **3 calendar days**
(safely spans a weekend without needing holiday awareness) and justify the exact
number the way the 2026-08-25 work justified its own boundaries — state the
number and the reasoning explicitly rather than picking one silently; if you find
a reason a fixed margin doesn't work here (e.g., a multi-day weekend + holiday
combination it doesn't cover), say so and decide from there rather than reaching
for a full calendar dependency without first checking whether it's needed.

## Constraints

- **`order_legs`/`orders` data itself is untouched.** This bounds a READ, not the
  retention. The 2026-08-12 decision's rationale for keeping this table
  unpruned — the durable P&L record, and the fact that `reconcile_orders` never
  treats a recorded order as a discrepancy — is unaffected and must stay true
  after this change. Anyone reading the table directly (a future backfill, a
  manual audit query, `tools.desk`/`tools.mark`'s SEPARATE reconstruction path
  through `shadow.marker.read_executions`) must see exactly the same data they
  see today.
- **Verify `tools.mark`/`tools.desk` are unaffected before assuming it.** They
  were already confirmed (2026-08-25 addendum) to use a different reconstruction
  path (`shadow.marker.read_executions`, reading the audit log) and never call
  `_open_spreads`. Re-confirm this by reading rather than trusting this prompt,
  the same way the prior task was asked to.
- **The bound must never be able to exclude a row describing a position that
  could genuinely still be open.** That is the one direction that must never be
  wrong. Being too WIDE (reading a few extra days of already-closed history)
  costs nothing but the read size this task exists to shrink; being too NARROW
  drops a live spread off the watch list with no stop evaluated — the same
  failure class the 2026-08-25 work was careful about on the Python-side filter.
  Err wide, and say so if the margin you land on trades off against the intent
  of this task.
- **`OpenSpreadSource.open_spreads(*, today: date)`'s signature does not need to
  change again** — `today` already flows to `_open_spreads(conn, today)`; this
  task only changes what that function does with it (also feeding the new SQL
  bound, not just `_pair_spreads`). If the margin needs a second input beyond
  `today` (e.g., an explicit override), justify why `today` alone isn't enough
  before adding one — the existing signature was deliberately kept minimal in
  the 2026-08-25 work.
- **Add an index if the query plan needs one.** `orders.recorded_at` is currently
  unindexed. If the new `WHERE` clause would otherwise force a full table scan
  with a filter applied after, add an index in a **new** migration (next number
  after `0004_orders.sql`; migrations in this codebase are immutable — never edit
  an existing one) and confirm the query plan actually uses it
  (`EXPLAIN QUERY PLAN`) rather than assuming.
- **Don't touch `_leg_is_expired` or `_pair_spreads`'s divergence-check logic.**
  Both are correct and were exercised carefully in the 2026-08-25 work; this task
  is additive at the SQL layer only.
- **Don't touch the audit log's own tables or `AuditLog.read`/`write`.** This is
  entirely inside `execution/store.py`'s own query.

## Testing

- Extend `tests/test_open_spreads.py` (already carries the expiry-awareness
  coverage from the 2026-08-25 work — build on it, don't fork a new file):
  - A leg from well outside the margin (e.g., 30 days back) with an otherwise
    identical shape to a currently-open spread must not appear in the scan at
    all — not `spreads`, not `malformed`, not `unattributed`. It's simply never
    read.
  - A leg from INSIDE the margin but expired (the 2026-08-25 case) must still be
    handled exactly as it is today — this proves the two mechanisms compose
    rather than one silently replacing the other.
  - A leg genuinely still open, recorded right at the edge of the margin, must
    still appear — the boundary itself needs a test, inclusive/exclusive per this
    codebase's house convention (see `test_chain_source.py`'s inclusive-bound
    parametrization for the pattern), not just points well inside and outside it.
  - `InMemoryOrderStore`'s fake `open_spreads` must be updated to apply the same
    margin so the store-parity tests (`test_sqlite_and_in_memory_stores_pair_identically`-style)
    keep proving the fake doesn't diverge from production on this dimension too.
- If an index is added, a migration test alongside the existing migration
  coverage (`test_migrations.py`) confirming it applies cleanly.
- Full suite green (`./.venv/bin/python -m pytest`), and confirm the existing AST
  guards (`test_no_leaks.py`, `test_session_trading_side.py`, `test_order_build.py`,
  `test_order_store.py`'s single-writer scan) stay untouched — this task has no
  legitimate reason to be near any of those boundaries.

## Deliverable

One commit (or small stack) on `main`. Don't push. Update
`docs/decisions/2026-08-12-expired-position-resolution.md` with a short dated
addendum section (matching the existing pattern) noting the SQL-side bound as a
distinct, later fix from the Python-side expiry filter — cite why the two are
complementary rather than redundant, so a future reader doesn't wonder why both
exist. Add a row (or extend the existing row) in `docs/decisions/README.md`.
Summarize at the end: the exact margin chosen and why, whether an index was
needed and what the query plan showed, and confirmation the full test suite
passes.
