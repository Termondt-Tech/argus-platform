# The unwatched-position gap (found 2026-08-11, before live-fire)

*A held position never booked into `positions` is never watched - and the broker-agreement check that fixes it.*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


**A held position that is never booked into `positions` is never watched.** Found
while reviewing why the account stream disconnected six times in one sandbox
session. Inert in shadow and in dry-run (neither holds anything); it becomes real
the moment `dry_run_submissions = false`.

The chain is VERIFIED from source:

- The `positions` TABLE is written ONLY by `store.py` — i.e. only by `FillConsumer`,
  i.e. only from `PlacedOrder` events delivered over the `AlertStreamer`.
- The account REST refresh (`stream.seed()`) writes only the in-memory
  `AccountStateCache`. **It does not touch the table.**
- Reconciliation runs at process start and session start. There is NO intra-session
  position reconcile and no fill backfill.
- `PositionWatchdog.request_refresh()` re-reads the LEDGER, not the broker.
- The watchdog's watch list is `store.open_spreads()` — the ledger.

So: order working -> stream drops -> order fills during the gap -> the event is
missed -> no `positions` row -> `open_spreads()` returns nothing -> the virtual stop
is never evaluated for a real 0DTE position.

**ONE LINK IS UNVERIFIED, and do not present the scenario as established until it
is:** whether tastytrade re-delivers state on re-subscribe. Argus's reconnect path
does re-enter the streamer and `subscribe_accounts` runs again, so if the server
sends a snapshot on subscribe, the missed fill arrives on reconnect and nothing is
lost. That is server behaviour, not answerable from the SDK source or offline.

What IS protected either way:
  - **New entries stop.** `gate.py`'s concurrent-position check reads
    `snapshot.open_position_count`, which comes from the REST-seeded cache — so an
    unbooked position still counts against `max_concurrent_positions`. The system
    degrades toward not trading, the safe direction for entries.
  - **It is detected, but only next session.** Order reconciliation finds the
    unaccounted `FILLED` order and hard-halts. Not silent forever, just not
    same-day. The exit is what goes unprotected in the meantime.

**BUILT 2026-08-11** — `PositionWatchdog._check_broker_agreement`, called from
`_refresh_if_due` (the slow ledger-refresh interval, not the cadence tick), wired in
`session._build_trading_side`. Settled details:

- **Both sides are INJECTED, both-or-neither.** `broker_position_count` (a callable
  over the account cache) and `ledger_state` (the store's own method). A ValueError
  at construction if only one is passed — the same rule `Reconciler(execution=,
  order_store=)` follows, because half a comparison is not a weaker check, it is a
  check that silently never runs. `_build_trading_side` takes it as a REQUIRED
  argument for the same reason: a default would quietly disable it.
- **`ledger_state` is injected rather than added to `OpenSpreadSource`**, whose
  docstring earns its keep at "one method, so it fakes trivially".
- **UNITS: both sides count non-flat LEG positions.** `AccountSnapshot
  .open_position_count` is one per OCC symbol; `LedgerState.positions` is a row count
  of a table that DELETES a row when it goes flat. Comparing against
  `len(scan.spreads)` would have been wrong by 2x — a vertical is two legs. Deriving
  the ledger count from `OpenSpreadScan` was rejected: `unattributed` holds position
  SYMBOLS while `malformed` holds ORDERS whose legs are also in `positions`, so
  summing them risks double-counting the exact rows the check exists to notice.
- **Either direction flags**, with `direction` in the payload
  (`held_but_unbooked` / `booked_but_absent`) — one condition, one escalation slot.
- **Rising edge only** (`_Escalation`, sentinel key `__book__`): Tier 1 +
  `watchdog_book_disagreement` alert once per episode, Tier 2 for the repeats,
  Tier 1 `watchdog.book_agrees` on recovery. Same shape as the unevaluable/
  unpriceable edges — the condition persists on every refresh, so a Tier 1 per cycle
  would be a trail nobody can read plus contention on the writer lock.
- **It pages; it does not halt.** Nothing here says internal state is corrupted —
  the right response is a human looking, not taking `/status` down with the process.
- **A None broker count SKIPS the check.** The gate already fails closed on an
  unusable account snapshot, so nothing new is being entered; inventing a comparison
  there would be a false alarm.

Audit categories added: `watchdog.book_disagreement`, `watchdog.book_agrees`
(Tier 1); `watchdog.book_disagreement_still` (Tier 2).

**THE FIX DELIBERATELY DOES NOT DEPEND ON THE REPLAY QUESTION.** The REST refresh
already yields a broker-derived position count every `account_refresh_s`;
`open_spreads()` yields the ledger's. If they disagree, something is held that is
not watched — whatever the cause: a missed event, a hand-placed fill, a
`FillConsumer` bug. Nothing compared them before this: `open_position_count` had
exactly two consumers, `/status` display and the gate's concurrent limit. Comparing
them adds no broker round trip and is correct whichever way the replay assumption
falls, which is why it is preferred over measuring the replay behaviour and then
trusting it.
