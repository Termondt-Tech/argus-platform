# The Slack alert storm (resolved 2026-08-05)

*The loop_lag_breach flood, the warmup-stall landmine, and the status line's full scan.*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


The first deployment of the execution engine (merge `4d1ced4`, 2026-08-04) was
followed by a Slack storm — `loop_lag_breach` / `loop_lag_recovered`, a pair every
~10 seconds — and was reported as "the execution engine is causing constant
disconnects". **It was neither a disconnect problem nor an execution-engine
problem.** Root-caused from the droplet's own journal; do not relitigate:

- **The lag is PRE-EXISTING and was measured, not assumed.**
  `journalctl -u argus.service --since 2026-07-25 | grep -c loop_lag_breach` →
  **13,406**, against a deploy on 08-04. `account/stream.py`, `account/rest.py` and
  `heartbeat.py` are untouched by the merge (git). What the merge changed is the
  DESTINATION: before it, `app.py` was `self._alert = alert or log_alert` and
  `__main__.py` injects no sink, so every one of these alerts had been a WARNING
  line in a journal nobody reads. Chunk 4's `notify.py` wired the same seam to a
  phone. Nothing regressed; a long-standing signal became audible.
- **The source is the account liveness refresh, identified by PERIOD.** Breaches
  arrive every 10.31s ±0.02 — `[staleness] account_refresh_s = 10.0` plus a ~0.31s
  round trip, because `AlertStreamerAccount._refresh_loop` sleeps the interval and
  THEN does the REST re-seed. It is the only ~10s cadence in the process (the
  dxlink stall watcher ticks at ≤5s and touches only memory; the signal loop is 5s;
  the session status print is 15s). ~60-170ms of the round trip runs on the event
  loop.
- **The flapping is the interval/threshold pair, not a stall.** At
  `interval_s = 0.1` / `lag_alert_threshold_s = 0.05`, `_run` catches up after a
  stall by burning ticks at `sleep(0)`, so lag decays 0.1 per tick: a 0.10-0.14s
  stall breaches once, a 0.16-0.17s stall twice, then a clean tick. That is
  exactly the observed `after_breaches` distribution. `sustained_breach_count = 5`
  would need a 0.45s+ stall, so `loop_lag_sustained` has never fired.
- **THE DISCONNECTS WERE NOT HAPPENING.** The `{...}` block on the session status
  line is `session._counts()` — all-time totals over the whole audit DB, not a live
  rate. Across the reported window every one of them is byte-identical
  (`marketdata.disconnected: 20`, `account.stream_disconnected: 6`,
  `account.refresh_failed: 285`), i.e. **zero** disconnects and **zero** refresh
  failures in that period, with `md_age=0.7-0.8s` and `chain 120/120`. Read that
  block as a cumulative ledger; a live rate has to be differenced across two lines.
- **Nothing in the execution engine ran.** `environment = "shadow"` →
  `trades_for_real()` is False → `_build_trading_side` is never called. The only
  new per-tick work in shadow is `compute_regime`, which is O(n log n) over ~120
  quotes.

The fix is two parts, and the split is deliberate:

- **`heartbeat.py` no longer ALERTS on recovery** — it logs. A recovery is not
  actionable (the breach that opened the episode already alerted, and
  `after_breaches` is in the metrics), and alerting on it doubled the volume of a
  detector whose anti-spam design assumes stalls are LONG AND RARE. The droplet's
  are short and periodic, so `_consecutive == 1` re-armed every single episode and
  the suppression never engaged. Breach and sustained alerts are unchanged.
- **`notify.py` now COALESCES per `kind`** (`DEFAULT_THROTTLE_S = 900.0`). This is
  the structural half: `AlertFn` has a dozen call sites, several inside loops or
  per-tick paths, and `SlackNotifier` shipped with a bounded queue and no rate
  limit of any kind — so the first chatty caller was always going to flood, and a
  flooded channel gets muted. That is a safety regression, because
  `audit_write_failed` and `reconciliation_mismatch` arrive on the same webhook.
  The rule is the weakest one that fixes it: the FIRST occurrence of a kind is
  always delivered immediately (an incident is never delayed by a rate limiter),
  repeats inside the window are counted, and the count rides on the next delivered
  message of that kind (`... (+58 suppressed in the last 900s)`) — deferred and
  summarized, never discarded. The window is PER KIND so a storm of one cannot
  starve a rare other, which is the failure a single global limiter would have.

  **Superseded in grain on 2026-08-21 (B1), not in reasoning.** `kind` was the
  right key for the caller this was calibrated against — `loop_lag_breach`, one
  condition flapping on a process-wide signal — and the wrong one for callers where
  a repeat inside the window is a different open POSITION. The key is now
  `(kind, scope)`; a kind that names no subject is unchanged, and every rule in this
  bullet holds at the finer grain. → [the alert
  throttle](2026-08-21-alert-throttle-is-per-position.md)
- **Coalescing happens at ENQUEUE, not at delivery**, so suppressed alerts occupy
  no queue slot and cannot push a different kind out of a full queue. `suppressed`
  and `dropped` are separate counters for the same reason: one is a deliberate
  summary, the other is a real loss. A DROPPED alert deliberately does NOT start
  the throttle window — suppressing the follow-ups to a message that was never sent
  would turn one dropped alert into a silent 15 minutes.
- **`throttle_s` is a module constant, not a config key**, same reasoning as
  `DEFAULT_TIMEOUT_S`: it shapes notification cadence and nothing about trading, so
  it stays out of the fail-closed loader — and a new REQUIRED key would couple this
  fix to a config deploy, the trap `[watchdog]`/`[risk]` already document.
  Constructor-overridable; `throttle_s=0.0` disables it.
- **The log sink is unaffected.** `combine()` calls `log_alert` independently, so
  the full unthrottled sequence is still in the journal — the throttle is about the
  phone, not the record.

**Immediate mitigation, if this ever recurs before a code fix:** unset
`ARGUS_SLACK_WEBHOOK_URL` in `.env` and restart. `build_alert_fn` returns
`log_alert` alone, which is byte-for-byte the pre-chunk-4 behaviour — it reverts
only the notifier and touches nothing else. `[heartbeat] lag_alert_threshold_s` is
also config on a bind mount, so raising it needs a restart, not a rebuild.

## The warmup stall, and why `trip_on_sustained_stall` must stay false

Found while verifying the fixes above, and it is a LANDMINE rather than a bug —
nothing is wrong today, and one word of config would break every morning.

**Measured on the droplet, twice** (2026-08-05, 11:15:50 and 11:47:54): subscribing
the 120-contract 0DTE chain to DXLink (quote+greeks) blocks the event loop for
**~0.45–0.55s**, at every session start. The attribution is not inferred — the
breach alert lands 2ms before the `subscribed to 120 symbol(s)` line:

```
11:47:54.745  ALERT loop_lag_breach {'lag_s': 0.4552}
11:47:54.747  subscribed to 120 symbol(s) (quote+greeks)
```

At `interval_s = 0.1` the heartbeat catches up by burning ticks, so a 0.5s stall is
~5 consecutive breaches — it reaches `sustained_breach_count = 5` **on every session
start**, and `loop_lag_sustained` fires daily. That is currently just an alert.

**With `trip_on_sustained_stall = true` it would engage the kill switch at ~09:40
every trading day** (`app.Application._trip_on_stall`, `source="heartbeat"`). And
because the kill switch is PROCESS-lifetime and deliberately survives day rollover
(a rollover must never un-halt — see "Kill-switch scope"), it would NOT clear at the
next open: entries refused all day, every day, until a human restarts the process.
The stall itself is benign where it occurs — warmup, before the signal loop starts,
nothing held and nothing working at the exchange. Only the threshold is wrong for it.

Two prerequisites before flipping the flag, either sufficient: move the chain
subscribe off the event loop, or raise `sustained_breach_count` above what warmup
produces — and MEASURE first, because it scales with chain size and therefore with
`[chain] strike_window_points`. The warning is duplicated at the config key itself,
which is where someone will be standing when they consider the change.

## The status line's full scan (fixed 2026-08-05, same day)

Found while root-causing the storm above, and NOT its cause (wrong period, still
under the 50ms threshold) — but a worse problem on a longer fuse.
`session.run_session`'s 15-second status print rendered its `{...}` block with
`_counts(db_path)`: a synchronous `sqlite3.connect` + full-table
`SELECT category, COUNT(*) ... GROUP BY category`, **on the event loop**, ~1,500
times a session, against a table growing ~14k rows a day. Measured (synthetic
table, real schema + migration 0003's index, droplet's own category distribution):

| rows | when | this Mac | ~droplet |
| --- | --- | --- | --- |
| 88k | 2026-08-05 | 4.5ms | ~22ms |
| 508k | +30 trading days | 26ms | ~132ms |
| 1.35M | +90 trading days | 70ms | ~350ms |
| 3.6M | +1 year | 392ms | **~2s** |

It degrades WORSE than linearly past ~1M rows (the file stops fitting in page
cache), and the timeline is the dangerous part: it becomes a multi-hundred-ms
event-loop stall every 15 seconds at roughly the point sandbox/live turn on — the
moment the watchdog's 1s cadence, the price-walking loop's COA cycle boundary and
`POST /kill`'s response time all start depending on a free loop.

Now `AuditLog.counts` — an in-memory tally, O(categories) instead of O(rows).
Settled:

- **Seeded ONCE at construction** (`_load_counts`, beside `_load_head`) and
  incremented in `_advance`. `_advance` runs on the loop under `_chain_lock` and
  ONLY after a successful insert, on both tiers — so a failed Tier 1 write
  (raises), a failed Tier 2 write (`continue`s) and a dropped Tier 2 line (never
  built) are all correctly uncounted.
- **The tally is EXACTLY the table, not an approximation**, and the reason is the
  writer `flock`: nothing else can write this file while the process holds it, so
  "what was there at startup + what I have written since" IS the contents. Pinned
  by tests that assert `audit.counts == <the GROUP BY query>` after mixed-tier
  writes, after a restart, and after drops.
- **`AuditLog.read(fn)` was considered and REJECTED** — its own docstring already
  forbids it ("Do NOT add an unbounded or full-scan query to a caller of this
  method"). It holds `_chain_lock`, so a growing scan there would delay the
  crash-critical Tier 1 writes spec §6/§9 protect, in proportion to table size.
  Strictly worse than the bug it would be fixing.
- **The one full scan per process start is the accepted cost.** It happens inside
  `startup()`, which already migrates, takes the flock, and in sandbox/live makes a
  broker round trip. Once per restart is bounded; 1,500 times a session is not.
- **`session._counts(db_path)` is KEPT, for `tools.shadow` only** — it prints a
  summary AFTER `app.shutdown()`, when the `AuditLog` is closed and there is no
  in-memory tally left to read and no loop left to stall. It also still MUST NOT
  RAISE (the shutdown path runs when startup failed before the DB existed). The
  defect was never the function; it was the call site.
- **An AST test pins the call site**, not a grep: `run_session` must not call
  `_counts`. The two call sites are identical at the line level and only structure
  distinguishes them.

**The `{...}` block now renders `category=total(+N)`**, where N is the change since
the session started (`session._format_counts`, baseline snapshotted once the loop
is up). It IS still a cumulative ledger — but a frozen counter renders bare and a
moving one carries its delta, so movement is a visual property of the line rather
than something the reader reconstructs by diffing two timestamps by eye. That
ambiguity, not any of the numbers, is what produced three false "the system is
failing" diagnoses on 2026-08-05, one of which stopped a running session mid-market.
The durable fix is `/status`'s link-health fields (above) — this is the journal
staying honest while `journalctl` is still the shadow-phase habit.
