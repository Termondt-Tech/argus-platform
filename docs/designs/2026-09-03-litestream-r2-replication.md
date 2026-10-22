# Litestream → Cloudflare R2 replication for the audit chain (2026-09-03)

**Status: PROPOSAL, awaiting sign-off. Nothing here is built.** Written to
`docs/tasks/argus_litestream_r2_replication_prompt.md` (private record, not published),
which says design first. On sign-off this ships and a decision record lands in
`docs/decisions/`; this file stays, marked with its outcome, as the record of
what was rejected.

**The recommendation in one paragraph.** Pin **Litestream v0.3.14** (not TOMIC's
v0.3.13, not the v0.5 line) as a checksum-verified static binary in the image,
run it as `litestream replicate -exec "python -m argus"` behind a new
`scripts/argus-entrypoint.sh` that branches supervisor-vs-tool, probes the
single-writer flock **before** litestream touches R2, restores only on a missing
DB, and gates a restored chain on `verify_chain()`. Replicate one database to one
R2 bucket at `sync-interval: 5s`, `checkpoint-interval` left at the default `1m`
pending measurement, `retention: 72h`. Ship it as **two deploys, not one**: the
first adds the Tier 1 write-latency instrument that does not exist today and
establishes a clean baseline; the second adds Litestream and is judged against
it. Replication health is monitored from **outside** the trading process by two
new systemd timers — an hourly lag check and a nightly restore-and-verify drill
that measures RPO and RTO every night as a side effect. **The check is hourly,
not every 15 minutes, for a cost reason that is not obvious: `ListObjects` is a
Class A operation and `sync-interval` sets how many objects it pages over, so a
naive health check was the single largest line on the R2 bill (§8.1). Two config
values came out of costing it — `sync-interval: 5s` (§1's stated RPO target, not
TOMIC's `1s`) and `retention: 72h` — and together they take Class A from ~98% of
R2's free allowance to ~13% (§8.2).** The NAS path is retired
only after five green trading days and one full DR drill, so there is never a
window with zero off-box copies.

**One finding that changes the shape of the task**, stated up front because it
inverts a non-negotiable if it is not designed for: with `-exec`, **a Litestream
startup failure prevents Argus from starting at all**, turning a replication
problem into a crash loop with no `/status` and no `/kill` — the 2026-08-14
shape. §6.3 is the answer. Everything else in this design is subordinate to the
Tier 1 bar in §4; this one is subordinate to the kill switch.

---

## 1. The gap, and the RPO this targets

Two copies today, both interim:

| Copy | Where | Tail-loss window |
|---|---|---|
| The live chain | `argus-state` Docker volume, droplet local disk | — |
| Nightly snapshot | `/path/to/backups`, NAS-pulled by rsync at 16:30 ET | **up to ~24 h** |

`audit.synchronous = FULL` protects against a *host crash rolling back committed
transactions*. It does nothing for *volume or droplet loss*, and the tail is
exactly what reconciliation reads back as expected state: lose it and the next
boot compares the broker against a record quietly missing its most recent
entries — either a false mismatch that hard-halts the open, or a "clean"
reconcile against a position the record has forgotten.

**RPO target: ≤ 5 seconds of committed chain, measured, for the volume-loss
case.** Derived, not chosen: the fastest thing this record has to keep up with is
the price-walking loop's reprice cycle and the watchdog's exit path, which write
Tier 1 records at ~0.2/s. Five seconds is at most one or two records, which is
under the granularity at which the *broker* is authoritative anyway — a restored
gap of one record still hard-halts on reconcile, which is correct, and the point
of shrinking the window is that the halt becomes rare rather than certain.
`sync-interval: 5s` is set to that target rather than below it — an earlier
draft used TOMIC's `1s`, which cost 4x the R2 operations and 5x the restore time
for at most one extra Tier 1 record of tail (§8.2).

**RTO target: a full restore completes inside `TimeoutStartSec` (300 s), with
margin.** §12.3.

This does **not** replace reconciliation. A restored tail-gap still hard-halts.
The upgrade shrinks the window; it does not skip the step.

---

## 2. Which Litestream

| Option | Verdict |
|---|---|
| **v0.3.14** (2026-03-26) | **RECOMMENDED.** |
| v0.3.13 (2023-10-24) | TOMIC's pin. Rejected — see below. |
| v0.5.x (v0.5.17, 2026-08-31) | Rejected for the first deploy. Separate decision. |

**v0.3.14 over v0.3.13.** v0.3.14 is a single-commit release and the commit is
[PR #1214, "Fix panic on restore when max WAL index is less than min"](https://github.com/benbjohnson/litestream/pull/1214).
That is a **restore-path panic**. For a system whose entire value is the restore,
taking the one fix that exists on the restore path is not a preference. It is
otherwise byte-for-byte the same generation of the tool TOMIC has run in
production, so the "pattern is proven" argument is fully preserved. v0.3.14 is
the last release on the 0.3 line.

**Not v0.5.x**, for this deploy. v0.5 is a rewrite: a different on-disk and
remote format (LTX), a different compaction/retention model (`l0-retention`,
`snapshot.interval`/`snapshot.retention` at the top level rather than per
replica), different config keys, and an actively moving release cadence — four
releases since June 2026, the latest three days before this was written. The
first thing this system must be able to do is restore, and the argument for a
2023-vintage binary is precisely that its restore path has three years of
production behind it. Moving to v0.5 is a real upgrade to weigh later (it fixes
the generation-churn model and gets better compaction), and it should be weighed
on its own, against a proven v0.3 baseline, with its own DR drill. **Not
alongside the first deploy of replication.**

---

## 3. What Litestream actually does to this database

Verified against
[`v0.3.14` source](https://github.com/benbjohnson/litestream/tree/v0.3.14), not
inferred from the docs, because three of these facts change the design.

**It writes to the database file.** Litestream creates two tables in the
monitored DB:

- `_litestream_seq (id INTEGER PRIMARY KEY, seq INTEGER)` — bumped with
  `INSERT … ON CONFLICT (id) DO UPDATE SET seq = seq + 1` to *force a WAL write*
  at generation init and around checkpoints. **Committed.**
- `_litestream_lock (id INTEGER)` — `INSERT INTO _litestream_lock (id) VALUES (1)`
  inside a transaction, purely to promote it to a write lock. **Rolled back**;
  no rows persist.

Consequences, each checked:

| Surface | Effect |
|---|---|
| `verify_chain()` | **None.** Reads `audit_log` only. |
| `apply_migrations()` | **None.** Keyed on `schema_migrations`; extra tables are invisible to it. |
| `tests/test_migrations.py` | **None.** Asserts table *membership*, never equality. |
| `db/lock.py` flock | **None.** The flock is on the `argus.db.lock` sidecar, not the DB file, and it guards the chain's `seq` allocation — a thing Litestream never touches. |
| Reconciliation (`positions`) | **None.** |
| "byte-identical" assumptions | **Broken, by design.** The DB under Litestream is not byte-comparable to its pre-Litestream state. Nothing in Argus compares bytes. |

The non-negotiable — *"Litestream is never handed a path that writes
`audit_log`"* — holds, and now has a precise reading: Litestream writes **into the
file**, never **into the chain**. It assigns no `seq`, computes no hash, and
takes no part in the single-writer invariant. §6.5 pins that structurally.

**It disables autocheckpoint on its own connection** (`PRAGMA wal_autocheckpoint = 0`)
and takes over checkpointing:

| Mode | Fired when | Blocks Argus's writer? |
|---|---|---|
| PASSIVE | shadow WAL ≥ `min-checkpoint-page-count` (default 1000 pages ≈ 4 MB) | No |
| **RESTART** | shadow WAL ≥ `max-checkpoint-page-count` (10000), **or `checkpoint-interval` has elapsed (default 1m)** | **Yes** |
| TRUNCATE | WAL > `truncate-page-count` (500000 ≈ 2 GB) | **Yes** |

**Its own connection uses a 1 s busy timeout, and that is not configurable in
v0.3** — `BusyTimeout = 1 * time.Second` is a package constant, and `DBConfig`
has no `busy-timeout` YAML key (verified: the struct carries `path`,
`meta-path`, `monitor-interval`, `checkpoint-interval`,
`min-checkpoint-page-count`, `max-checkpoint-page-count`, `replicas`, and
nothing else). This answers design question 6's sub-question outright: **pinning
Litestream's busy timeout higher is not an available knob.** The asymmetry it
leaves is the one you want — Argus waits 5 s, Litestream gives up after 1 s and
retries on its next tick.

**`-exec` signal handling, verified in `cmd/litestream/main.go`:**

```go
case sig := <-signalCh:
    slog.Info("signal received, litestream shutting down")
    if c.cmd != nil {
        slog.Info("sending signal to exec process")
        if err := c.cmd.Process.Signal(sig); err != nil { ... }
        slog.Info("waiting for exec process to close")
        if err := <-c.execCh; err != nil && ... { ... }
    }
}
// Gracefully close.
if e := c.Close(); e != nil && err == nil { err = e }
```

and `DB.Close()` performs `db.Sync(ctx)` then `r.Sync(ctx)` for each replica. So
the shutdown ordering the prompt asks for — **Argus drains Tier 2 and closes
SQLite → Litestream final sync → exit** — is what `-exec` does, in that order,
for free. Note also that **litestream waits for the child with no timeout**: the
only bound on shutdown is `stop_grace_period` / `TimeoutStopSec`. §7.2.

**Credentials are never logged.** The s3 startup line prints
`bucket`, `path`, `region`, `endpoint` only; `access-key-id` and
`secret-access-key` appear in no log statement at `info`. §9.3.

---

## 4. The Tier 1 non-degradation analysis

This is the acceptance question. Everything else is subordinate to it.

### 4.1 The contention surface, enumerated

`AuditLog.record()` holds `_chain_lock`, then `await asyncio.to_thread(self._insert)`;
`_insert` runs `BEGIN` / `INSERT` / `COMMIT` on the one writer connection with
`synchronous = FULL`. The loop stays free. **The awaiting caller does not** —
the watchdog logging a stop-loss exit does not proceed until that returns. Three
places Litestream can extend it:

1. **A RESTART checkpoint (the dominant one).** RESTART waits for readers, then
   holds the write lock while WAL frames are copied back into the main DB and
   the WAL is restarted. A Tier 1 `COMMIT` arriving in that window waits, up to
   `busy_timeout` = 5000 ms. At the default `checkpoint-interval: 1m` this fires
   **~390 times per 6.5 h session**. Stall duration is proportional to WAL size
   at checkpoint: one minute of Argus writes at ~2.3 records/sec of small rows is
   on the order of **100 KB**, so the expected copy-back is sub-millisecond to
   low single-digit milliseconds. **Expected, not measured — this is the number
   §4.5 exists to get.**
2. **The `_litestream_seq` write.** One row, one committed transaction, taken at
   generation init and around checkpoints. Sub-millisecond, rare.
3. **The `_litestream_lock` write-lock promotion.** Held only long enough to
   coordinate the checkpoint; rolled back.

The 5 s `busy_timeout` is the ceiling, and the non-negotiable already says a
Tier 1 write waiting anywhere near it during a volatility spike is a failure of
this design. It is a ceiling, not a budget.

### 4.2 What is *not* contention, and why

- **The sync path.** `sync-interval` governs reading `-wal` frames and PUTting
  them to R2. Reading the WAL file takes no write lock. Shortening
  `sync-interval` improves RPO and costs R2 operations; it does **not** buy
  Tier 1 latency. The write-lock cost is concentrated in the checkpoint path,
  governed by `checkpoint-interval`. Do not conflate the two knobs.
- **Long-lived readers on Argus's own connection.** `connect()` uses
  `isolation_level=None` (autocommit), so a `SELECT` holds a read transaction
  only for the statement. `AuditLog.read()`'s contract already forbids unbounded
  queries, and `write()`'s already requires bounded transactions. Nothing in the
  supervisor opens an unbounded reader. **Confirmed as the prompt asks.**
- **The one long read that does exist is `verify_chain()`**, a full-table scan
  with a `sha256` per row. It runs in the nightly snapshot job today and in
  tests — never on the live connection during a session. §11.2 keeps it that way
  (the nightly drill verifies a *restored scratch copy*, in a throwaway
  container, never the live DB). §6.4 adds one more call site, in the entrypoint,
  **before Litestream starts**.
- **Read-only analysis tools.** `tools.desk` / `tools.mark` open
  `file:argus.db?mode=ro`. A long scan there holds a read lock, which makes a
  RESTART checkpoint wait, which makes Litestream retry on the next tick and the
  WAL grow. Bounded and self-correcting, but it is a **new** reason not to run a
  full-table analysis scan against the live DB during a session. Runbook note.

### 4.3 `wal_autocheckpoint` on Argus's connection: leave it alone

Litestream's own tips recommend `PRAGMA wal_autocheckpoint = 0` on the
application connection for high-throughput systems, so the app does not
checkpoint between Litestream's checkpoints (an app checkpoint that restarts the
WAL forces Litestream into a new generation and a full re-snapshot).

**Recommendation: do NOT set it. `connection.py` is unchanged.**

Two reasons, in order:

1. **The failure direction is wrong.** With autocheckpoint disabled and
   Litestream dead, *nothing* checkpoints: the WAL grows without bound until the
   disk fills, and a full disk takes SQLite down with it. That is a replication
   failure taking the primary out — the exact inversion of the non-negotiable.
   The app's own 1000-page autocheckpoint is the safety net for precisely the
   case where Litestream is not running.
2. **It costs nothing to leave on.** Argus's WAL grows at roughly 100 KB/minute;
   the 1000-page (≈4 MB) threshold is ~40 minutes away, and Litestream's 1-minute
   RESTART checkpoint restarts the WAL long before it. In steady state the app
   autocheckpoint **never fires**. Generation churn is therefore not a real cost
   here — it is a cost for write-heavy applications, which this is not.

If the measurement shows generation churn anyway, the correct response is to
find out *why the app reached 1000 pages inside a checkpoint interval*, not to
disable the net.

### 4.4 The instrument gap, and why this is two deploys

**Nothing in Argus measures Tier 1 awaited write latency today.**
`tools.evaluability` gives the write-rate split by tier; `tools.loop_lag`
measures loop lag, which is precisely the thing `to_thread` protects and which
will therefore under-report this. The p50 0.081 ms / p99 1.86 ms figures in
`connection.py` and `argus.toml` came from a bench harness, not from the running
process. **You cannot judge a regression in a number you do not collect.**

**Deploy A — the instrument. `src/` only, no config key.**
`AuditLog.record()` gains a `time.perf_counter()` pair around the existing
`await asyncio.to_thread(self._insert, rec)` and accumulates into a fixed-bucket
histogram (O(1) per write, no allocation, no new records — this must not double
the write volume it is measuring). Surfaced on `/status` as
`audit.write_latency_ms {p50, p99, p99_9, max, n}`, session-scoped, reset at the
day rollover. It is an in-memory read of numbers the process already holds — the
same category as `gc_snapshot()`, and explicitly **not** a DB query, so
`AuditLog.read()`'s bounded-query contract is untouched.

It is worth having regardless of this task, and it is the only thing that makes
the bar below checkable.

**Then 2–3 clean baseline cert sessions**, with no Litestream anywhere, and with
the lag-ramp diagnostics **off** (§4.7).

**Deploy B — Litestream.** Judged against those sessions.

### 4.5 The pass/fail bar, stated before measurement

Tier 1 awaited write latency, per session, Litestream running, against the
Deploy-A baseline:

| Metric | Baseline (to be measured) | **Bar** |
|---|---|---|
| p50 | ~0.08 ms expected | **≤ 0.20 ms** |
| p99 | ~1.9 ms expected | **≤ 5.0 ms** |
| p99.9 | — | **≤ 50 ms** |
| max | — | **≤ 250 ms** |
| count ≥ 1000 ms | 0 | **exactly 0** |

The last row is the one that matters. `busy_timeout` is 5000 ms; a single
Tier 1 write that waits a full second is one fifth of the way to a hard timeout
and is already a watchdog that could not record a stop-loss exit for a second.
**One such wait fails the deploy**, regardless of the percentiles.

Secondary bars, all per session, all against the same baseline:

- **Loop-lag breach episodes must not rise.** `LoopHeartbeat.breach_count` and
  the journal's `loop_lag_breach` line count, per hour, within the baseline's
  observed range. No new `sustained` stall episodes.
- **The Tier 2 drain still completes inside `stop_grace_period`.** Measured from
  the journal: `argus shutting down` → `litestream shut down` → container exit.
- **RSS.** Argus's own RSS delta over a session unchanged ± 20 MB against
  baseline (there is a known +80 MB/51.7 h open question; this must not be
  confounded with it). Litestream's RSS measured separately from the cgroup — it
  is a second process, not a leak in the first.
- **Replication actually works**: `litestream generations` lag < 5 s throughout
  the session, and the nightly drill's restore verifies clean.

### 4.6 The measurement plan

Cert, throwaway R2 bucket (`argus-audit-dev`), never the live DB, never the
production bucket.

| # | What | How |
|---|---|---|
| M1 | Bench, off-box | `tools.loop_lag` (and `--live-cert`) with and without a Litestream process replicating a **copy** of the DB. Isolates the checkpoint effect from everything else. |
| M2 | Baseline sessions | 2–3 full cert sessions on Deploy A. Collect: `/status` latency histogram at close, `LoopHeartbeat.breach_count`, `tools.evaluability --config argus.dev.toml` write-rate split, RSS at open and close. |
| M3 | Litestream sessions | 2–3 full cert sessions on Deploy B against `argus-audit-dev`. Same five readings. |
| M4 | Checkpoint attribution | Correlate Litestream's checkpoint log lines against the latency histogram's tail and against `loop_lag_breach` timestamps. If the tail is not aligned with checkpoints, the cause is elsewhere and the tuning in §4.7 will not help. |
| M5 | Cost | Count R2 Class A operations for one session from the Cloudflare dashboard; compare against §8's estimate. |

**Split by PID when a day spans a deploy** — the 2026-08-26 rule. A whole-day
aggregate mixes code versions.

### 4.7 The lag-ramp confounder

There is an unresolved ~1 s loop stall once per ~11 s, growing through a session
(`lag-ramp-handoff.md` (private record, not published),
[the decision record](../decisions/2026-08-10-intra-session-lag-ramp.md)).
**Litestream checkpoints are a new candidate cause class for that symptom
family, previously absent by construction**: a periodic, growing-with-WAL-size
event that blocks a writer.

Three rules this design commits to:

1. **The baseline in M2 is taken with the ramp diagnostics OFF**
   (`ARGUS_LOOP_DEBUG=0`, `ARGUS_TRACEMALLOC=0`). Both cost real overhead, and
   `ARGUS_LOOP_DEBUG` makes log volume ramp *with the thing being measured*.
2. **Litestream must not be added to a session that is being used to
   investigate the ramp**, and the ramp investigation must not read a
   Litestream session as evidence about pre-Litestream behaviour.
3. **If the ramp is still open at deploy time, Deploy B gets its own before/after
   PID-split read**, the way the account-refresh deploy got one — an intraday
   deploy splitting one trading day across both versions under identical broker
   conditions is the strongest reading available, and it is cheap.

### 4.8 If the bar fails

In order, with the direction reasoned in advance so the tuning is not a
fishing trip:

1. **The lever is `checkpoint-interval`, and it moves DOWN, not up.** Stall
   *duration* scales with WAL size at checkpoint; stall *frequency* scales with
   1/`checkpoint-interval`. Total blocked time is roughly constant either way —
   what changes is the **tail**, and the bar is a p99/max bar. Shorter interval =
   more, shorter stalls = a better tail. Try `30s`, then `15s`. There is a floor
   (each checkpoint has fixed lock-acquisition and fsync overhead), and M4's
   attribution says when it has been reached.
2. **Then `min-checkpoint-page-count`**, raised, so PASSIVE checkpoints (which do
   not block writers) do more of the work between RESTARTs. Marginal at this
   write rate.
3. **Then the shape.** If checkpoint contention is irreducible, move Litestream
   out of the container entirely and run it as a host systemd unit against the
   Docker volume's path (§5, shape C). It does not remove SQLite-level
   contention — same file, same locks — but it removes the shared cgroup and the
   shared lifecycle, and it makes the checkpoint schedule independently
   tunable and independently killable. **It is a worse operational shape and a
   better isolation shape; only reach for it with M4 in hand.**
4. **Then reconsider the RPO.** A 5-minute RPO with a checkpoint every 5 minutes
   is still a ~288× improvement on 24 h and is strictly better than today. Do
   not abandon replication because the tightest setting failed.

---

## 5. Container / process shape

Three shapes were weighed.

### Shape A — `litestream replicate -exec "python -m argus"` — **RECOMMENDED**

Litestream is the parent, Argus is its child, one container.

**For:**

- **The shutdown ordering is correct for free**, verified in source (§3):
  SIGTERM → forwarded to Argus → Argus drains Tier 2 and closes SQLite →
  Litestream final `db.Sync` + `replica.Sync` → exit. Any other shape has to
  *construct* that ordering, and the thing being ordered is the tail this whole
  project exists to protect.
- **The restore ordering is correct for free**: restore happens in the entrypoint,
  before Python starts, before the flock, before `_load_head()`.
- **One lifecycle.** They cannot drift into "Litestream replicating a DB nobody
  is writing" or "Argus writing with nothing replicating" — the two silent
  states that make a backup fictional.
- **`init: true`, `network_mode: host`, `stop_grace_period` are container-level
  and completely untouched.** Docker-init stays PID 1, litestream is PID 2,
  Argus is PID 3, and signals traverse all three.

**Against, stated plainly:**

- **A Litestream crash takes Argus down ungracefully.** Docker-init exits when
  its direct child (litestream) exits; the container stops and the cgroup is
  killed, so Argus can be SIGKILLed without its Tier 2 drain. Cost: Tier 2
  telemetry for the tail of that session. **Not** Tier 1 — `synchronous = FULL`
  means every Tier 1 record is already on disk. `restart: unless-stopped` brings
  it back and reconciliation runs. Accepted.
- **A Litestream *startup* failure prevents Argus from starting at all.** This is
  the serious one and it is answered in §6.3, not accepted.

### Shape B — sidecar compose service

**Rejected.** It buys crash isolation and loses the two orderings that matter:

- Shutdown: compose stops services without a guarantee that Litestream's final
  sync happens *after* Argus has closed SQLite. The window it opens is exactly
  the tail.
- Restore: needs external sequencing (`depends_on` + healthcheck, or an init
  container), which is more moving parts guarding a step that `-exec` does
  positionally.
- Two lifecycles that can silently diverge (above).
- It also gives a second container `network_mode: host` for no reason, or a
  second network namespace for no reason.

### Shape C — Litestream as a host systemd unit

**Rejected for now; retained as §4.8's fallback.** Best isolation from the
trading process, and it is the shape to reach for if measurement fails. Costs:
it must reach into `/var/lib/docker/volumes/argus-state/_data/`, a path Docker
does not promise; `docker compose down -v` removes the volume out from under it;
the binary needs its own supply-chain story outside the digest-pinned image;
restore-on-boot becomes an operator step rather than a property; and it does not
remove SQLite-level checkpoint contention, which is the thing being isolated
from. Name it, do not build it.

---

## 6. The entrypoint

New file `scripts/argus-entrypoint.sh`, `COPY`'d to `/usr/local/bin/argus-entrypoint`
and set as `ENTRYPOINT`. `CMD` stays `["python", "-m", "argus"]`.

### 6.1 The branch

```sh
#!/bin/sh
set -eu

DB=/data/argus.db
CFG=/etc/litestream.yml

# The SUPERVISOR is the exact three-token default command and nothing else.
# Everything unrecognized runs DIRECTLY, with no restore and no replication —
# the fail-safe direction, because the worst case of guessing "tool" is a
# one-shot run that is not replicated, and the worst case of guessing
# "supervisor" is a SECOND litestream writing into the same R2 path, which the
# Litestream docs call out as replica corruption.
if [ "$#" -ne 3 ] || [ "$1" != "python" ] || [ "$2" != "-m" ] || [ "$3" != "argus" ]; then
    exec "$@"
fi
```

`docker compose run --rm argus python -m argus.tools.quotes SPX` → four-plus
tokens, `argv[3]` is `argus.tools.quotes` → direct exec, no Litestream, no
restore. The retired shadow override
(`python -m argus.tools.shadow --minutes 390`) → same. `docker compose run --rm argus`
with no command → the default `python -m argus` → **supervisor branch**, which is
exactly the case §6.2 exists for.

*(The existing gotcha is unchanged: every `argus.tools.*` still needs
`--config /etc/argus/argus.toml` inside the container.)*

### 6.2 The flock probe, before Litestream touches R2

The single-writer flock is taken by Python, several frames after Litestream has
already started replicating. So today's clean refusal — "another Argus process
is already writing to this database" — would, under Shape A, happen **after** a
second Litestream had written a new generation into the production R2 path.

```sh
# Probe the real lock, with the real class, so it cannot drift from db/lock.py.
# The process exits immediately and the kernel releases; this is a TOCTOU
# window of milliseconds against an operator error, not a race against a peer.
if ! python -c "from argus.db.lock import DatabaseLock; DatabaseLock('$DB').acquire()"; then
    echo "[entrypoint] refusing to start: the write lock is held. Not starting litestream." >&2
    exit 1
fi
```

This is not belt-and-braces. It converts the most likely operator error — running
the supervisor twice — from *replica corruption* into the same clean refusal it
produces today.

### 6.3 The Litestream pre-flight, and the unreplicated fallback

**The non-negotiable: a failed *replication* does not halt trading.** Under
Shape A, `litestream replicate` exiting at startup means Argus never starts, and
`restart: unless-stopped` makes that a crash loop — active(running) the whole
time, no `/status`, no `/kill`. That is the 2026-08-14 incident with a new
cause.

```sh
LITESTREAM=1
for var in LITESTREAM_BUCKET LITESTREAM_ENDPOINT \
           LITESTREAM_ACCESS_KEY_ID LITESTREAM_SECRET_ACCESS_KEY; do
    eval "val=\${$var:-}"
    [ -n "$val" ] || { echo "[entrypoint] $var is unset" >&2; LITESTREAM=0; }
done
# Parses the config and errors on a bad one, without touching R2.
litestream databases -config "$CFG" >/dev/null 2>&1 || LITESTREAM=0

if [ "$LITESTREAM" -eq 0 ]; then
    echo "[entrypoint] !! LITESTREAM PREFLIGHT FAILED — STARTING ARGUS UNREPLICATED !!" >&2
    exec "$@"
fi
```

**Trading continues; the replica-check timer (§11.1) pages within the hour.**
That is the correct division: a missing replica is an alert, a missing kill
switch is not survivable.

Two supports for it, because a pre-flight only covers the failures it thought
of:

- **`scripts/argus-deploy.sh`'s red-flag grep gains
  `LITESTREAM PREFLIGHT FAILED|litestream shut down`**, so a deploy that lands in
  either state fails loudly at deploy time rather than at 09:30 ET.
- **AT-7 (§13) is a hard acceptance test**: unset `LITESTREAM_BUCKET`, restart,
  and the container must come up unreplicated and serve `/status`. If it
  crash-loops, this design is not done.

### 6.4 Restore, and the `verify_chain` gate on a restored chain

```sh
if [ ! -f "$DB" ]; then
    # A missing DB with live sidecars is a TORN state, not a fresh volume.
    # Restoring over it would be guessing. Refuse.
    if [ -e "$DB-wal" ] || [ -e "$DB-shm" ]; then
        echo "[entrypoint] $DB is missing but its WAL/SHM sidecars exist — refusing." >&2
        exit 1
    fi
    echo "[entrypoint] no local database — attempting restore from replica ..."
    # -parallelism 32, not the default 8: the restore fans out over WAL
    # INDEXES and the object count is set by sync-interval (§12.3). It is the
    # free RTO lever — no storage, no operations, no retention depth.
    litestream restore -if-replica-exists -parallelism 32 -config "$CFG" "$DB"
    if [ -f "$DB" ]; then
        echo "[entrypoint] RESTORED $DB FROM REPLICA — verifying chain ..."
        python -m argus.tools.verify_chain --db "$DB"     # exits non-zero on a break
        echo "[entrypoint] restored chain verified."
    else
        echo "[entrypoint] no replica found (first run) — starting with a fresh database."
    fi
fi

exec litestream replicate -config "$CFG" -exec "$*"
```

**Why `verify_chain` on the restore path and nowhere else.** A restored DB whose
chain is broken reaches `_load_head()`, becomes the new head, and Argus writes
onto it — producing a log that verifies from that point forward and means
nothing before it. Verifying at boot is the only place that is catchable.

**Why not on every boot.** `verify_chain()` is O(rows) with a JSON parse and a
`sha256` per row. At the observed rate (~2.3 records/sec × 6.5 h ≈ 54k
records/day) that scan grows without bound, and it would land on *every* restart
— including the mid-session recovery restart where seconds matter and where
nothing has been restored. Pay it only when the file's provenance actually
changed.

`tools.verify_chain` is a new, trivially small one-shot tool (open read-only,
`AuditLog(...).verify_chain()`, exit 0/1) that also gives §11.2 and the operator
a supported on-demand check. It replaces the inline `python - <<PY` heredoc in
`argus-snapshot.sh`.

**Restore does not need the flock** — it runs before Litestream and before
Python, and §6.2 has already established nobody else holds it.

### 6.5 The structural checks

Cheap, in the style of the existing AST tests:

1. **`litestream.yml` names exactly one database, and it is `/data/argus.db`.**
   Parse the YAML in a test; assert `len(dbs) == 1` and the path. Litestream is
   never handed a second path, and never a path that is not the one the flock
   protects.
2. **`litestream replicate` appears in exactly one place in
   `scripts/argus-entrypoint.sh`, and it is lexically after the flock probe and
   after the pre-flight.** A text/AST-shaped assertion on the script, in the
   spirit of the `NOT_ATTEMPTED`-above-the-walk test.
3. **The Dockerfile pins both a version and a SHA256, and no executable path
   installs Litestream without verifying the checksum** — the mirror of the
   existing "no executable path builds the image without `ARGUS_VERSION`" test.
4. **`connection.py` does not set `wal_autocheckpoint`** (§4.3's decision, held
   by a test rather than by this sentence).

---

## 7. Boot and shutdown ordering

### 7.1 Boot

| # | Step | Precondition it needs | Holds? |
|---|---|---|---|
| 1 | Entrypoint: branch supervisor-vs-tool | `$@` is the compose command | yes |
| 2 | Entrypoint: flock probe | nothing | yes |
| 3 | Entrypoint: Litestream pre-flight | env vars present, config parses | yes; fails → unreplicated fallback |
| 4 | Entrypoint: `restore -if-replica-exists` | DB missing, no sidecars, flock free | yes (2) |
| 5 | Entrypoint: `verify_chain` on a restored DB | a restore happened | yes (4) |
| 6 | `litestream replicate -exec` starts | config valid | yes (3) |
| 7 | Argus `startup()`: kill token, alert sink | env | unchanged |
| 8 | `DatabaseLock.acquire()` | nobody else holds it | yes (2), and Litestream never takes it |
| 9 | `connect()` + `apply_migrations()` | DB exists and is readable | yes (4/5) |
| 10 | `AuditLog.__init__` → `_load_head()` | the chain is intact | **yes, and this is what (5) buys** |
| 11 | Kill-switch rehydration, control bind | audit log open | unchanged |
| 12 | Broker reconciliation | control surface already bound | unchanged — **still mandatory, still hard-halts on a restored tail-gap** |

Litestream starts at step 6, *before* the flock at step 8. That is safe only
because of step 2, which is why step 2 exists.

### 7.2 Shutdown, and the timeout budget

```
systemd/docker SIGTERM
  → docker-init (PID 1)
    → litestream (PID 2): "signal received" → forwards SIGTERM
      → argus (PID 3): drains Tier 2 (queue.join), records process.stop,
                       closes the control server, closes SQLite, releases flock
      → litestream: waits for the child (NO TIMEOUT), then Close():
                    db.Sync() + replica.Sync()  ← the final sync
      → exit
```

**Recommend: `stop_grace_period` 30s → 45s, `TimeoutStopSec` 90s → 120s.**

The reason is not that shutdown got slow; it is that a **network round trip was
appended to the very end of it**, after the drain, where the 30 s was already
sized for the drain alone. Overrunning means SIGKILL during the final sync,
which loses exactly the tail Litestream exists to preserve. `TimeoutStopSec` must
stay comfortably above `stop_grace_period` plus compose's own teardown, which is
what 120 s preserves.

Both are measured in M2/M3 (`argus shutting down` → `litestream shut down` →
container exit) and the numbers go in the runbook. If the observed worst case is
well under 30 s, the increase can be dropped at sign-off — but it should be
decided from the measurement, not from optimism.

---

## 8. `litestream.yml`

Repo root, beside `docker-compose.yml`; `COPY`'d to `/etc/litestream.yml` in the
image. Values from env. **It does not go through `config.py`** — that loader is
fail-closed over *trading* parameters and this is infrastructure config, the same
category as `docker-compose.yml`: versioned by git on the droplet, read by a
different process, and with no trading semantics. Confirmed as the prompt asks.

```yaml
# Argus — Litestream replication of the hash-chained audit log to Cloudflare R2.
#
# INFRASTRUCTURE config, not trading config. It is NOT read by argus/config.py
# and carries no trading parameter. Every value that is a secret comes from the
# environment (docker-compose `env_file: .env`); nothing here is a literal
# credential. Litestream expands ${VAR} across the whole file before parsing.
#
# NOTE: v0.3 has no `busy-timeout` key. Litestream's own connection is pinned at
# 1s in the binary; Argus's is 5s (db/connection.py). That asymmetry is the one
# we want — Litestream yields first.

logging:
  # Pinned, not defaulted. `debug` has NOT been audited for credential content,
  # and this process's stdout goes to the json-file log on disk AND to journald
  # — the same reasoning as ARGUS_LOG_LEVEL. Do not raise this on the droplet.
  level: info
  type: text

# Metrics endpoint deliberately UNSET. Two reasons: (1) `network_mode: host`
# means a `:9091` bind lands on the PUBLIC interface, and config.py already
# forbids a wildcard bind for exactly that reason; (2) it reports Litestream's
# own opinion of its health, which is the thing that is wrong in the classic
# silently-stalled-replica failure. The check that matters asks R2 — §11.1.

dbs:
  - path: /data/argus.db

    # DEFAULT, PENDING MEASUREMENT (§4.5/§4.8). This is the knob that touches the
    # Tier 1 write path: it sets how often a RESTART checkpoint takes the write
    # lock. If the p99 bar fails, this moves DOWN (30s, then 15s) — shorter
    # interval means a smaller WAL per checkpoint, so more but shorter stalls,
    # which is what a p99/max bar wants. Never up.
    checkpoint-interval: 1m

    # Defaults, stated rather than omitted so a future reader knows they were
    # considered. At ~100 KB of WAL per minute the 1000-page (~4MB) PASSIVE
    # threshold is essentially never reached before the interval above fires.
    min-checkpoint-page-count: 1000
    max-checkpoint-page-count: 10000
    monitor-interval: 1s

    replicas:
      - type: s3
        name: r2
        bucket: ${LITESTREAM_BUCKET}
        path: argus-audit
        endpoint: ${LITESTREAM_ENDPOINT}
        region: auto                       # R2 requires the literal "auto"
        access-key-id: ${LITESTREAM_ACCESS_KEY_ID}
        secret-access-key: ${LITESTREAM_SECRET_ACCESS_KEY}

        # RPO. 5s of committed chain, which at the observed 0.2 Tier 1
        # records/sec is AT MOST ONE Tier 1 record — and it is the RPO target
        # §1 already states. 1s was the first draft, taken from TOMIC, and it
        # was cut for cost: this value sets the OBJECT COUNT in the bucket, and
        # every object is a Class A PUT plus a share of every ListObjects that
        # pages over it. 1s = 56% of R2's free Class A allowance; 5s = 13%.
        # It also cuts worst-case RTO from ~88s to ~18s (§12.3).
        # Do NOT lower this to "improve" the RPO without re-reading §8.2.
        sync-interval: 5s

        # 3 days of point-in-time restore. The nightly restore-and-verify drill
        # (§11.2) catches a chain break within 24h, so 72h is that detection
        # latency plus a long weekend. 168h was the first draft and was CUT for a
        # cost reason, not a durability one: retention sets how many WAL objects
        # live in the bucket, and every ListObjects over them is a Class A
        # operation — 168h more than doubles the health check's bill and pushes
        # the account past the free tier on its own. §8.1.
        retention: 72h
        retention-check-interval: 1h

        # One full snapshot a day bounds restore time (RTO): a restore replays
        # WAL from the newest snapshot forward, so without this the replay grows
        # with the retention window. Aligned with the nightly drill.
        snapshot-interval: 24h
```

### 8.1 What this costs, and whether it stays inside R2's free tier

R2's forever-free monthly allowances are **10 GB-month storage, 1,000,000
Class A operations, 10,000,000 Class B operations, and unmetered egress**. Paid
rates past them are $0.015/GB-month, $4.50/million Class A, $0.36/million
Class B. `DeleteObject` is free. The allowance is a monthly deduction from
usage, not a hard cap — exceeding it bills the excess rather than failing writes.

**Class A operations are the binding constraint, not storage — and the
monitoring design is what makes them binding.**

Litestream PUTs a WAL segment only when there are new frames, so the write term
is bounded by *session* seconds, not wall-clock seconds; the audit log is
essentially silent outside a session (the heartbeat's `loop_lag_breach` goes
through `AlertFn`, not the audit log; the idle scheduler writes nothing). That
term is ~491k/month and was never the problem.

**The problem is that `ListObjects` is Class A and the bucket holds one object
per sync interval.** *(The figures in this subsection are the FIRST DRAFT's —
`sync-interval: 1s`, `retention: 168h`, a 15-minute health check. They are kept
because the mechanism is the point; §8.2 is what is actually recommended.)*
At `1s` that is ~23,400 WAL objects a day into R2, and at `retention: 168h` the
bucket holds **~164,000 of them**. And
`litestream generations` — §11.1's health check — pages through all of them:
`GenerationTimeBounds` calls `maxWALSegment`, whose iterator is a
`ListObjectsPages` over `generations/<gen>/wal/` (verified in
`s3/replica_client.go:617`). At 1000 keys per page that is **~164 Class A
operations per health check**.

| Source | Class A / month |
|---|---|
| WAL segment PUTs — 1/s × 23,400 s × 21 trading days | 491,400 |
| **§11.1 health check at 96/day × ~164 list pages** | **~472,000** |
| Nightly drill's restore listings | ~5,000 |
| Hourly retention enforcement, daily snapshot multipart | ~13,000 |
| **Total** | **≈ 980,000 — AT the 1,000,000 free-tier line** |

**That is at the line, not comfortably under it**, and it goes over on a 23-day
month or any day the estimate is light. Then the **billable-unit rounding**
applies: the doc's own example is *"one million and one operations, you will be
billed for two million operations"*, so crossing costs **$4.50/month at best and
$9.00 at worst** — the page is genuinely ambiguous about whether the free
allowance is deducted before or after the round-up, and the difference is
$4.50/month. Storage is the cheap dimension; **operations are the one that
crosses first, and it is our own monitoring that pushes it over.**

**The fix is in §11.1, not in `sync-interval`**, and it is cheap:

| Lever | Class A/month for the check |
|---|---|
| As first drafted: `generations`, 96/day, 168h retention | ~472,000 |
| Hourly instead of every 15 min | ~118,000 |
| Hourly **and** `retention: 72h` (~70k objects → ~70 pages) | **~50,000** |
| Local metadata stat + hourly R2 check | ~50,000 |

Hourly plus `retention: 72h` brings the total to **~560,000, back to 56% of the
allowance** — and it costs almost nothing operationally, because §11.3's
`/status` row already answers "is Litestream locally alive" continuously and for
free. The R2 round trip only has to answer "did it *land*", and that question
does not need asking every 15 minutes.

Class B is unaffected: dominated by §11.2's nightly restore replaying WAL
segments at ~702,000/month, **7%** of the 10 M allowance. Egress is free, which
is the only reason a nightly full-restore drill is affordable at all; on S3 it
would be the largest line on the bill.

**Storage: the slower of the two, and it crosses later.** Two facts govern it.
First, **snapshots and WAL segments are LZ4-compressed** (`.snapshot.lz4`,
`.wal.lz4`), so R2 holds roughly `DB_size / compression` per snapshot — 2–4× on
JSON-heavy SQLite pages, **unmeasured here**. Second, retention keeps
`retention / snapshot-interval + 1` snapshots, so at `72h`/`24h` that is **~4
compressed copies of the database**.

Measured row size from a real chain is **~618 logical bytes**, and the rate is
~54k records/day, so the DB grows **~33 MB/day ≈ 700 MB/month** — and nothing
prunes it (§19.7). At an assumed 3× compression:

| DB size | Replica (≈3× LZ4, 4 snapshots + 3 days WAL) | Reached at roughly |
|---|---|---|
| 1.0 GB | ~1.4 GB | month 1.5 |
| 2.0 GB | ~2.7 GB | month 3 |
| 3.5 GB | ~4.7 GB | month 5 |
| 7.0 GB | ~9.4 GB — **at the 10 GB limit** | **month 10** |

Cutting retention to 72h roughly doubles the storage runway as a side effect of
fixing the operations bill — the same change helps both dimensions, because both
scale with what is retained.

**So the corrected answer is: operations cross first and they cross soon** — at
the drafted 15-minute cadence, immediately. Storage crosses somewhere around
month 10 and costs $0.015/GB-month past it. **Do not read "the excess is cents"
as a general statement about R2**: it is true of storage and false of
operations, where the rounding rule means the first operation past a million
costs $4.50–$9.00 and the *next* 999,999 cost nothing more. Operations are
lumpy; storage is smooth.

### 8.2 Staying free: the config lever first, then the provider

**The cost above was mostly a configuration choice, and it was my own.** §1 sets
the RPO target at **≤ 5 seconds** and §8 then configured `sync-interval: 1s`,
which is TOMIC's value, not a value derived from Argus's target. At the observed
0.2 Tier 1 records/sec, **5 seconds is at most one Tier 1 record** — the same
answer, five times cheaper on every dimension that scales with object count:

| `sync-interval` | Class A / month | % of free | Worst-case RTO (p=8, 30 ms) |
|---|---|---|---|
| 1s (first draft) | 560,000 | 56% | 88 s |
| 2s | 290,000 | 29% | 44 s |
| **5s — RECOMMENDED** | **127,000** | **13%** | **18 s** |
| 10s | 73,000 | 7% | 9 s |

**`sync-interval: 5s` removes the operations problem outright** — 13% of the
allowance leaves room for the cadence to be wrong by 4× — and it takes RTO
comfortably inside the 120 s bar at the *pessimistic* latency, which
`-parallelism 32` (D10b) then makes irrelevant. Storage is unchanged: the same
bytes in fewer, larger objects.

**That leaves storage as the only paid exposure, at roughly month 10**, and its
real lever is not a provider — it is
[the audit-log growth task](../tasks/argus_audit_log_growth_prompt.md), where 82%
of the bytes are one telemetry category.

**If the net is widened anyway**, the shortlist. Litestream v0.3 supports
`file`, `s3`, `gcs`, `abs` and `sftp` replicas, and **several replicas per
database**, which matters more than the ranking:

| Target | Operations | Free storage | Egress | Verdict |
|---|---|---|---|---|
| **Cloudflare R2** (current) | $4.50/M Class A | 10 GB | **unmetered** | Keep. Unmetered egress is what makes a *nightly full-restore drill* affordable — the design's most distinctive choice. |
| **Backblaze B2** | **free — Class A/B/C went to $0 in May 2026**; only Class D bills | 10 GB | free to **3× stored**, then $0.01/GB | Genuinely better for a write- and list-heavy replica. But the nightly drill downloads the whole replica ~30×/month, which blows 3× and bills ~$0.38/month. **Ops-free vs egress-free is a real trade, not a tie.** |
| **SFTP → the WD My Cloud NAS** | **none** | NAS disk | none | **Zero marginal cost, hardware already owned and already in the backup path.** One serious objection, below. |
| Scaleway Object Storage | metered | 75 GB | metered | Biggest free storage; EU, ID verification. A fallback if storage becomes the binding cost. |
| Storj | metered | trial only | metered | 30-day trial, not a durable free tier. Reject. |
| Wasabi, Hetzner | — | none | — | Minimum monthly spend. Reject. |
| AWS S3, GCS, Azure Blob | metered | 5 GB, mostly 12-month | **metered** | Metered egress against a nightly restore drill. Reject. |

**The recommendation is not "switch" — it is `sync-interval: 5s`, keep R2, and
consider the NAS as a SECOND replica.** Two replicas is free in Litestream (one
`replicas:` list, two entries), it serves §14's "never a window with zero
off-box copies" directly rather than by procedure, and it reframes the NAS
deprecation: **§14 retires the pull MECHANISM, not the NAS.** The NAS stops being
fed by an rsync cron and starts being fed continuously by the same process that
feeds R2.

> **The objection that makes this Richard's call, not mine.** The current NAS
> design has an explicit security property: **the droplet holds no NAS
> credentials.** The NAS initiates the pull, against an `rrsync -ro` forced
> command. A Litestream SFTP replica **inverts that** — the droplet would hold a
> key that can *write* to the NAS, so a compromised droplet reaches the backup.
> That is a real regression and it is precisely the property the current design
> was built around. Mitigations exist (a dedicated key, an `internal-sftp`
> chroot, NAS-side versioning, and Tailscale so nothing is exposed publicly),
> but a consumer NAS may not support all of them. **Do not treat "we already
> trust the NAS" as settling it — the direction of trust is what changes.**

**Three things to measure rather than believe.** The LZ4 ratio is a guess and it
moves the storage table by a factor of two. The prompt's "~14k rows/day (~7 MB)"
is **stale** — it predates the always-on execution phase and understates growth
~4×. And the free-allowance-versus-rounding composition is genuinely ambiguous
in Cloudflare's own docs (§19.8). Take a `du -sh` of the droplet's `argus.db`,
**one full month of real R2 operation counts from the Cloudflare dashboard**, and
the actual billed line, before confirming `retention: 72h` and the health-check
cadence.

**The knob that is genuinely coupled is `snapshot-interval`, and it is coupled to
RTO** — see §12.3. `sync-interval` sets the number of WAL objects a restore must
replay, and `snapshot-interval` sets how many accumulate before the replay
resets. Both feed cost as well, which is why §8.2 settles the value once for all
three consequences rather than tuning it per section.

---

## 9. Credentials and bucket scoping

### 9.1 Two buckets, not two prefixes

**`argus-audit-prod` and `argus-audit-dev`, separate buckets, separate tokens.**

R2's API-token model is **bucket-granular** — "Object Read & Write" is scoped to
specific buckets, not to prefixes within one. A prefix-scoped token is not
something R2 offers, so a shared bucket would mean the dev/throwaway credential
could write over the production replica. Two buckets is the only scoping that
actually holds. This also satisfies "the live `argus.db` is never a Litestream
source during development": the dev credential physically cannot reach the
production bucket.

Token scope: **Object Read & Write on that one bucket, nothing else.** Read is
needed for `restore`, write for `replicate`; no admin, no account-level, no other
bucket. Two tokens, minted separately.

### 9.2 `.env.example` block

Placed after the alerting block, before "RUNTIME / DEPLOYMENT", matching the
existing style:

```sh
# ===========================================================================
# LITESTREAM → CLOUDFLARE R2 (continuous replication of the audit chain)
# ===========================================================================
# Consumed by `litestream replicate` (scripts/argus-entrypoint.sh), NOT by
# Argus — nothing in argus/config.py reads these. They are expanded into
# /etc/litestream.yml at process start.
#
# REQUIRED for replication, but NOT required for the process to start: with any
# of them unset the entrypoint logs LITESTREAM PREFLIGHT FAILED and runs Argus
# UNREPLICATED. That is deliberate — a missing replica is an alert, not a halt
# (CLAUDE.md: "a failed audit write halts trading; a failed replication does
# not"). The replica-health timer pages within the hour.
#
# The access key pair IS a credential: it grants read+write to the bucket
# holding the entire hash-chained trading record. Scope the R2 API token to
# Object Read & Write on ONE bucket and nothing else. Never in argus.toml,
# never on a command line, never logged — Litestream's own startup line prints
# bucket/path/region/endpoint only, and `logging.level` is pinned to `info` in
# litestream.yml because `debug` has not been audited for credential content.
#
# The endpoint embeds your Cloudflare ACCOUNT ID, which is why it lives here
# and not in the committed litestream.yml.
#
# Development and testing use a SEPARATE bucket and a SEPARATE token
# (argus-audit-dev). The live chain is never a Litestream source during
# development — CLAUDE.md, "NEVER TOUCH THE LIVE DATABASE".
LITESTREAM_BUCKET=
LITESTREAM_ENDPOINT=https://<cloudflare-account-id>.r2.cloudflarestorage.com
LITESTREAM_ACCESS_KEY_ID=
LITESTREAM_SECRET_ACCESS_KEY=
```

### 9.3 Does Litestream log the secret?

**No, at `info`.** Verified in `cmd/litestream/replicate.go` (v0.3.14): the s3
branch logs `"replicating to", "bucket", …, "path", …, "region", …, "endpoint", …`
and there is no log statement anywhere in the startup path that carries
`AccessKeyID` or `SecretAccessKey`. `logging.level` is pinned to `info` in the
config rather than left to the default, and the runbook gets the same treatment
`ARGUS_LOG_LEVEL=DEBUG` has: **do not raise it on a deployed box** — `debug` has
not been audited for credential content, and container stdout lands on disk and
in journald.

The existing gotcha is unchanged and now covers two more secrets: **credentials
are visible in `docker inspect argus` and `/proc/<pid>/environ`** to anyone with
root or docker-group access. Inherent to `env_file:`.

### 9.4 Network

Outbound HTTPS to `<account>.r2.cloudflarestorage.com` from the host network
namespace. ufw's default-allow-outgoing covers it; **no inbound port is opened**
(the metrics endpoint is off), so neither ufw nor the DigitalOcean cloud firewall
changes. **§20 is the full droplet/tailnet pass** — including two couplings that
will present as an R2 outage and are not (Tailscale owns DNS; `network_mode: host`
inherits host routing), the CPU channel §4 does not cover, and the boot-time
`verify_chain` cost that eventually exceeds `TimeoutStartSec`.

---

## 10. The Dockerfile change, and the supply-chain reconciliation

Added to **stage 2 (runtime)**, before `USER argus`, as a self-contained block
with no toolchain left behind:

```dockerfile
# --- Litestream: continuous replication of the audit chain to R2 -------------
# A single static Go binary, pinned by VERSION and verified by SHA256. The hash
# is the point: GitHub release assets are mutable in principle and this project
# publishes no checksum file, so the hash is computed once, by hand, from a
# trusted machine and pinned here. `sha256sum -c` FAILS THE BUILD on a mismatch,
# exactly as --require-hashes does for the Python closure.
#
# v0.3.14, not TOMIC's v0.3.13: v0.3.14 is a single-commit release fixing a
# PANIC ON RESTORE (PR #1214). For a tool whose entire value is the restore,
# that is not optional. Not v0.5.x — a format-and-config rewrite that gets its
# own evaluation against a proven v0.3 baseline.
ARG LITESTREAM_VERSION=0.3.14
ARG LITESTREAM_SHA256=<PIN AT IMPLEMENTATION — see below>
ARG TARGETARCH
USER root
RUN set -eux; \
    arch="$(case "${TARGETARCH:-amd64}" in amd64) echo amd64;; arm64) echo arm64;; \
             *) echo "unsupported arch ${TARGETARCH}" >&2; exit 1;; esac)"; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL -o /tmp/litestream.tar.gz \
      "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-${arch}.tar.gz"; \
    echo "${LITESTREAM_SHA256}  /tmp/litestream.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/litestream.tar.gz -C /usr/local/bin litestream; \
    rm -f /tmp/litestream.tar.gz; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*; \
    litestream version

COPY litestream.yml /etc/litestream.yml
COPY scripts/argus-entrypoint.sh /usr/local/bin/argus-entrypoint
RUN chmod 0755 /usr/local/bin/argus-entrypoint
USER argus

ENTRYPOINT ["/usr/local/bin/argus-entrypoint"]
CMD ["python", "-m", "argus"]
```

**Pinning the hash.** No checksums file is published for these releases and the
GitHub API returns no asset digest, so the value must be computed once:

```bash
curl -fsSL https://github.com/benbjohnson/litestream/releases/download/v0.3.14/litestream-v0.3.14-linux-amd64.tar.gz | sha256sum
```

Corroboration available without downloading: the GitHub API reports
`litestream-v0.3.14-linux-amd64.tar.gz` at **9,637,170 bytes** and
`linux-arm64` at **8,834,385 bytes**. **This design deliberately does not invent
a hash** — pinning it is an implementation step, and the build must fail without
it (structural check §6.5.3).

**Cross-arch note.** The dev Mac is arm64 and the droplet is amd64, and the base
image is a multi-arch manifest, so the block selects by `TARGETARCH` and needs a
hash per architecture (two `ARG`s, or a small case block). Hardcoding amd64
would break every local build.

**The supply-chain reconciliation, written out as the prompt requires.**
[`docs/decisions/monitoring-surface.md`](../decisions/monitoring-surface.md)'s
"stdlib-only apart from the tastytrade SDK" is a statement about the **Python
dependency tree**: what `--require-hashes` locks, what can execute inside the
interpreter, what can pull a C extension, and what has to be audited when it
moves. **Litestream is not in that tree.** It is a single static Go binary, in a
separate process, with no Python bindings and no import path — the same category
as a pinned system package such as `ca-certificates`, and it is pinned harder
than one (an exact version and an exact SHA256, rather than a distribution's
moving pointer). It does not weaken the stdlib-only statement, and it does not
change it. **The decision record must say this explicitly**, because the next
person to read that sentence will otherwise read the Dockerfile and conclude it
has drifted.

---

## 11. Failure behaviour and monitoring

**An unmonitored replica is not a backup.** The classic Litestream failure is not
a crash; it is a replica that quietly stops advancing and is discovered weeks
later. Two checks, both **outside the Argus process** — and the decisive reason
for "outside" is that a check living inside Argus dies with Argus, which is one
of the states that most needs to page.

### 11.1 The hourly lag check — the rail

`scripts/argus-replica-check.sh` + `argus-replica-check.service` / `.timer`,
**hourly**, root.

```
docker compose run --rm --entrypoint litestream argus \
    generations -config /etc/litestream.yml /data/argus.db
```

`--entrypoint litestream` bypasses `argus-entrypoint` entirely: no branch, no
restore, no second replicate process. The command asks **R2**, not Litestream's
local opinion, which is the whole point. Output columns are
`name  generation  lag  start  end`; `lag` is how far the replica is behind the
primary.

> **Why hourly and not every 15 minutes — this check is the R2 bill.**
> `generations` computes `lag` via `GenerationTimeBounds` → `maxWALSegment`,
> whose iterator is a `ListObjectsPages` over `generations/<gen>/wal/`
> (`s3/replica_client.go:617`). It enumerates **every WAL object in every
> retained generation**, 1000 keys per page, and `ListObjects` is **Class A**.
> At the drafted `sync-interval: 1s` that was ~23,400 objects a day — ~70 Class A
> operations per run at `retention: 72h`, ~164 at 168h — and at 96 runs a day the
> check alone was ~472,000 operations a month, **past the free tier on its own**
> (§8.1). At `5s` and hourly it is ~15 pages per run and ~11,000 a month.
>
> Hourly costs ~50,000/month for the same information. Nothing is lost: §11.3's
> `/status` row answers "is Litestream locally alive and advancing" continuously
> and for free, off a file stat. The R2 round trip only has to answer "did it
> **land**", and a replica that stopped landing an hour ago is discovered an
> hour later — against a failure whose defining property is going unnoticed for
> *weeks*, that is the same answer at a ninth of the cost.
>
> **The general rule to carry: in this bucket, listing is expensive and the
> object count is set by `sync-interval`.** Any future check that lists must
> justify its cadence, and the cheap probe is always the local metadata.

Alerts on:

- the command failing (R2 unreachable, credentials rejected, container broken);
- **no generation at all** for the `r2` replica — the "it never started" case;
- `lag` above threshold.

**The threshold is not guessed here.** `lag` behaviour when the DB is idle
(overnight, weekends) has not been observed on this system, and a threshold
picked from a guess is how a check becomes noise and then gets ignored. The
first week's readings calibrate it. **Provisional: a 1-hour lag, market hours
only, for the first week; then set from the observed distribution and record the
number.** Same discipline as every other bound in this codebase.

**Alert path: `curl` to `$ARGUS_SLACK_WEBHOOK_URL`, read from `/opt/argus/.env`,
directly from the script — deliberately NOT through Argus's `AlertFn` seam.** The
seam is the right funnel for everything inside the process and the wrong one
here for one reason: this check must fire when the process is dead.

**Coalescing**: a state file under `/var/lib/argus-replica-check/`, posting on
the **rising edge** and then hourly while the condition persists, plus one
recovery message — the `escalation.py` rising/overdue shape, in shell. Without
it, an R2 outage posts 96 identical messages a day and the channel learns to
ignore it (the 2026-08-05 alert storm, one layer out). A failed run also leaves
the unit in `systemctl --failed`.

### 11.2 The nightly restore-and-verify drill — the deep check

`scripts/argus-replica-verify.sh` + unit + timer, **16:45 ET**, replacing
`argus-snapshot.{sh,service,timer}` (which ran at 16:30).

This is the prompt's **option (b)**, and it is the recommendation, because it
answers three questions the other options answer none or one of:

| | (a) drop it, rely on boot+on-demand | (b) restore latest replica to scratch, verify | (c) `verify_chain` on the live DB |
|---|---|---|---|
| Is the chain intact? | only after a restore that may never happen | **yes, nightly** | yes |
| **Is R2 actually restorable?** | no | **yes — this is what catches a silently stalled replica** | no |
| Cost to the Tier 1 path | none | **none** (throwaway container, scratch file) | **a full-table read lock on the live DB** |
| Free instrumentation | none | **measured RPO and RTO, every night** | none |

(c) is rejected outright: a growing full-table scan holding a read lock on the
live connection is the thing `AuditLog.read()`'s contract exists to forbid, and
it would also stall Litestream's checkpoints (§4.2).

Steps, each a failure gate:

1. `litestream restore -config /etc/litestream.yml -parallelism 32 -o /path/to/backups/verify-<ts>.db /data/argus.db`
   — **timed**, and with the SAME `-parallelism` the entrypoint uses, or the
   nightly number is not the RTO the boot path would see. The elapsed seconds
   are the measured **RTO**, logged nightly; log the segment count alongside it,
   because RTO/segment is the portable number and the total is not.
2. `python -m argus.tools.verify_chain --db /path/to/backups/verify-<ts>.db`
   — non-zero exit fails the unit.
3. `MAX(seq)` on the restored copy vs `MAX(seq)` on the live DB (opened
   `mode=ro`). **The gap is the measured RPO**, logged nightly. This is the
   single most informative number the whole design produces, and it is free.
4. Retain 2 verified copies on the host bind mount, rotate the rest. Not a
   backup strategy — a spot-check artifact.
5. Any failure → non-zero exit → failed unit → Slack, same path as §11.1.

`/path/to/backups` is therefore **kept**, repurposed from "the thing the NAS
pulls" to "the drill's scratch path". `argus-snapshot.sh` is deleted; its
`verify_chain` role moves here and gets stronger (it now also proves the replica
restores).

### 11.3 `/status`

**Yes, one row — and it must be honest about what it does not know.**

`status.py`'s bar for a field is "bounded, index-backed, rare read". A
replication row can meet it as a **file stat**, in exactly the shape
`read_rss_bytes` already establishes: read the mtime of the newest file under
Litestream's local metadata directory (`/data/.argus.db-litestream/`), report its
age. No subprocess, no DB query, no network, no allocation to speak of. Wrapped
in try/except and returning `None`, because it does file I/O — the explicit
carve-out `read_rss_bytes` documents, and the reason the five `unmanaged`
accessors are *not* wrapped.

**What it can say and what it cannot**, because a row that overclaims is worse
than no row (2026-09-02: the reassuring-when-wrong failure):

- It says **"Litestream is locally alive and its shadow WAL is advancing."**
- It does **not** say "R2 is current". Only §11.1 says that.
- The label must carry that distinction, and the states are:
  **green** = advancing within threshold; **amber** = unreadable / `None` /
  no metadata directory (never green, never absent); **red** = present but not
  advancing during market hours.
- On a process running **unreplicated** via §6.3's fallback, the metadata
  directory does not exist → amber, which is correct and is exactly the state an
  operator should see.

`BLACKOUT_WARN_DAYS`'s precedent applies to the threshold: a **module constant**,
not a config key, because a new REQUIRED key would force a coupled deploy for a
monitoring nicety.

### 11.4 Litestream cannot reach R2

**Never a halt.** The local chain and `synchronous = FULL` are intact; the
primary is unaffected. Litestream logs the sync error and retries on its next
interval; the WAL grows until the connection returns, and the app's own
autocheckpoint (§4.3) is the backstop if it does not.

**This is an assertion to test, not to assume** — AT-6 (§13): blackhole the R2
endpoint mid-session and confirm (a) Argus keeps trading, (b) Litestream keeps
running rather than exiting, (c) §11.1 pages within the hour, (d) replication
resumes without intervention when the block is lifted.

---

## 12. Disaster recovery

### 12.1 The fresh-volume drill (run on a Saturday, cert)

1. `sudo systemctl stop argus.service`
2. `sudo docker compose down` ; `sudo docker volume rm argus-state`
3. `sudo systemctl start argus.service`
4. **Confirm in the journal**: `no local database — attempting restore from
   replica`, then `RESTORED … — verifying chain`, then `restored chain verified`.
5. **Confirm migrations re-check clean** — `apply_migrations` applies nothing
   (the restored DB already carries `schema_migrations`), and `argus build <tag>`
   is the first line.
6. **Confirm `verify_chain` passed** (step 4's line) and `MAX(seq)` matches the
   pre-teardown value within the RPO.
7. **THE ARGUS-SPECIFIC STEP, which TOMIC has no analogue for: confirm broker
   reconciliation runs and behaves correctly against the restored state.**
   Broker is the only source of truth. With a flat book: `broker reconciliation
   clean`. **With a position open** (do this deliberately, on a Saturday, with a
   cert position held over): confirm the restored `positions` rows match the
   broker, and — the case that matters — confirm that a *deliberately* truncated
   restore (a point-in-time restore to before the fill, §12.2) **still
   hard-halts**. A restored tail-gap must halt. If it reconciles clean against a
   record that has forgotten a position, this design has made things worse, not
   better.
8. Confirm `/status` and `/kill` from the phone.

### 12.2 Point-in-time restore to a scratch path

**Never over the live DB in place.** The live file is the primary; a restore over
it is destroying the authoritative copy to recover from the replica of it.

```bash
sudo docker compose run --rm --entrypoint litestream \
    -v /path/to/backups:/scratch argus \
    restore -config /etc/litestream.yml -parallelism 32 \
            -timestamp 2026-09-03T18:30:00Z \
            -o /scratch/argus-pitr.db /data/argus.db
sudo docker compose run --rm -v /path/to/backups:/scratch argus \
    python -m argus.tools.verify_chain --db /scratch/argus-pitr.db
```

Then inspect with `tools.desk` / `tools.mark` / `sqlite3 file:...?mode=ro`
against the scratch copy. Promoting a scratch copy to primary is a separate,
deliberate operator procedure (stop the service, move the live file aside — never
delete — put the copy in place, restart, **reconcile**), and it is a runbook step
with its own warnings, not a script.

### 12.3 RTO

**`retention` does not affect RTO — `snapshot-interval` does.** Stated first
because the two are easy to conflate: `restore` finds the newest snapshot at or
before the target and replays WAL *forward* from it, so the work is set by how
much WAL has accrued **since that snapshot**. Retention governs how far *back*
you can restore to, which is PITR depth, a different question. **Cutting
retention 168h → 72h (§8.1) changed the R2 bill and the storage runway and
changed RTO by nothing.**

**RTO is driven by the OBJECT COUNT, not the byte count**, and `sync-interval`
sets it: one WAL segment object per 5 seconds of session activity. Worst case with a
24 h snapshot interval is a full session's worth — **~4,680 objects to GET and apply**
at `sync-interval: 5s` — it was ~23,400 at the drafted `1s`, and cutting that
interval is what moved RTO from marginal to comfortable (§8.2). This is the real
RTO term and it was invisible in a byte-based estimate.

**How `restore` actually parallelises, because it is not what the flag name
suggests.** `Replica.Restore` fans out over **WAL indexes**, not segments: each
worker calls `downloadWAL(generation, index, offsets, …)`, which opens a reader
for **every segment at that index, in order, serially** (`replica.go:1364`).
A new index is created on each WAL restart — i.e. **on each checkpoint** — so at
`checkpoint-interval: 1m` a session produces ~390 indexes of ~60 segments each.
390 indexes is comfortably above `DefaultRestoreParallelism = 8`, so the default
is not starved; but note the coupling: **`checkpoint-interval` sets the restore's
parallelism ceiling.** §4.8's Tier 1 tuning moves it *down*, which only adds
headroom, so the two levers do not fight. Raising it far enough to leave fewer
than 8 indexes would silently serialise the restore.

Aggregate wall clock is therefore `total_segments / parallelism × per-GET
latency`, and the droplet→R2 latency is **unmeasured**:

| | 20 ms/GET | 30 ms | 50 ms |
|---|---|---|---|
| `-parallelism 8` (default) | 12 s | 18 s | 29 s |
| `-parallelism 32` | 3 s | 5 s | 8 s |

plus ~5–10 s for the compressed snapshot and ~10–30 s applying segments locally.

**The bar**: a full restore must complete inside `TimeoutStartSec` (300 s) **with
margin** — call it ≤ 120 s, since the restore is one step of a boot that also
migrates, verifies the chain, binds the control surface and reconciles with the
broker. At `sync-interval: 5s` the default parallelism already clears the bar on the
pessimistic latency; at the drafted `1s` it did not. The number is still
measured nightly rather than assumed.

**The lever is `-parallelism`, and it is free.** Raising it costs no storage, no
operations and no retention depth — it is a flag on the restore command, set in
the entrypoint (§6.4) and in the drill (§11.2). **An earlier draft of this
section recommended shortening `snapshot-interval` instead; that was wrong** —
it is the expensive lever, because retained snapshots are
`retention / snapshot-interval + 1` and each is a compressed copy of the whole
DB. At `72h`/`4h` that is 19 snapshots rather than 4, taking the replica from
~1.4 GB to ~6.3 GB at a 1 GB database and pulling the storage crossing in from
month 10 to roughly month 3. Reach for `snapshot-interval` only after
`-parallelism` is exhausted, and note that it must go **below ~6.5 h to bind at
all** — WAL only accrues during a session, so a 12 h interval can still straddle
a whole one and buy nothing.

The nightly log makes the trend visible long before it is a problem — the second
reason to prefer option (b) in §11.2.

---

## 13. Acceptance tests

Gates on the Deploy-B sign-off, in addition to §4.5's bar.

| # | Test | Pass |
|---|---|---|
| AT-1 | Normal boot, DB present | No restore attempted; `replicating to bucket=… path=argus-audit`; Argus starts; `/status` green |
| AT-2 | `docker compose run --rm argus python -m argus.tools.quotes SPX --config /etc/argus/argus.toml` while the service is up | Runs; **no** restore, **no** second litestream process; service unaffected |
| AT-3 | `docker compose run --rm argus` (default command) while the service is up | Refuses on the flock probe **before** litestream starts; R2 untouched (no new generation) |
| AT-4 | Fresh volume (§12.1) | Restore + `verify_chain` + migrations + reconcile, all clean |
| AT-4b | Same drill, **timed**, worst case (restore a full session's WAL) | Restore completes ≤ 120 s at the entrypoint's `-parallelism`. Record seconds, segment count and per-GET latency — §12.3's table is unmeasured |
| AT-5 | Graceful stop | `argus shutting down` → Tier 2 drained → `litestream shut down` → exit, all inside `stop_grace_period`; final sync visible in R2 |
| AT-6 | R2 blackholed mid-session | Argus keeps trading; litestream stays up; §11.1 pages within the hour; resumes on unblock |
| AT-7 | `LITESTREAM_BUCKET` unset | `LITESTREAM PREFLIGHT FAILED`; Argus comes up **unreplicated**; `/status` serves; **no crash loop** |
| AT-8 | Corrupt the restored chain (flip a byte in a payload on a scratch copy, restore path pointed at it) | `verify_chain` fails; the entrypoint exits non-zero; Argus never opens it |
| AT-9 | Deliberately truncated PITR restore with a position open | Broker reconciliation **hard-halts**. A restored tail-gap must never reconcile clean |
| AT-10 | Chain verification, live | `MAX(seq)` on the nightly restored copy is within the RPO of the live DB |

---

## 14. The NAS deprecation plan

### 14.1 What goes, what stays

| Item | Fate |
|---|---|
| `scripts/argus-snapshot.sh` | **DELETE.** Its backup role is replaced by continuous replication; its `verify_chain` role moves to §11.2 and gets stronger. |
| `argus-snapshot.service` / `.timer` | **DELETE** (disable on the droplet first). |
| `scripts/argus-pull.sh` | **DELETE** (NAS-side). |
| `scripts/argus-pull-daemon.sh` | **DELETE** (NAS-side). |
| `scripts/argus-pull-watchdog.sh` | **DELETE** (NAS-side). |
| `scripts/pull-snapshots.example.sh` | **DELETE** (NAS-side). |
| `/path/to/backups` (host dir) | **KEEP**, repurposed as §11.2's scratch path. |
| The `rrsync -ro` forced-command key in `authorized_keys` | **REMOVE** (droplet operator step). |
| NAS cron entries | **REMOVE** (NAS operator step). |
| A local integrity job | **KEPT, in trimmed form** — §11.2, option (b). |
| `scripts/argus-replica-check.{sh,service,timer}` | **NEW** — §11.1. |
| `scripts/argus-replica-verify.{sh,service,timer}` | **NEW** — §11.2. |
| `src/argus/tools/verify_chain.py` | **NEW** — §6.4, small. |

### 14.2 The migration order — never a window with zero off-box copies

| Phase | Action | Off-box copies |
|---|---|---|
| 0 | R2 buckets + tokens minted. **Deploy A** (the latency instrument). 2–3 baseline cert sessions. | NAS |
| 1 | Local drill on the dev Mac: `argus.dev.toml`, `dev/` DB, **`argus-audit-dev` bucket**. Replicate, kill, restore, verify. | NAS |
| 2 | **Deploy B** on the droplet → `argus-audit-prod`. `argus-snapshot.timer` **still enabled**. New timers enabled. | **NAS + R2** |
| 3 | Five consecutive green trading days: §4.5's bar met, §11.1 quiet, §11.2 clean nightly, RPO/RTO within target. Plus one full §12.1 DR drill on a Saturday, including AT-9. | **NAS + R2** |
| 4 | **Only now**: disable and remove `argus-snapshot.timer`/`.service`, remove the NAS cron, remove the `rrsync` key, delete the six scripts. | R2 |
| 5 | Keep the last NAS-pulled snapshot set, cold, for 30 days. Then delete. | R2 (+ cold) |

**Prove before you tear down.** Phase 4 is the only destructive step and it is
gated on a passed restore drill, not on the new path merely appearing to work.

### 14.3 Operator cleanup (runbook steps, not code)

**Droplet:**
```bash
sudo systemctl disable --now argus-snapshot.timer
sudo rm -f /etc/systemd/system/argus-snapshot.{service,timer}
sudo systemctl daemon-reload
# Remove the rrsync forced-command line from the backup user's authorized_keys.
# Do NOT remove /path/to/backups — argus-replica-verify.sh uses it as scratch.
```

**NAS (WD My Cloud EX2 Ultra):** remove the cron entries; delete the pull
scripts and the SSH key used for the forced command; keep the pulled snapshots
for 30 days per phase 5.

---

## 15. Documentation updates (on ship, not now)

| File | Change |
|---|---|
| `docs/decisions/<date>-litestream-r2-replication.md` | **NEW.** The decision record: the gap, the RPO/RTO targets, the Tier 1 argument **with the measured numbers**, the shape and its rationale, every config value with its reason, the failure/monitoring design, the NAS deprecation and its migration order, and the supply-chain reconciliation (§10) written out. |
| `docs/decisions/README.md` | New row under a durability/operations heading. Update the "open questions" list. |
| `docs/designs/README.md` | Mark this file SHIPPED with a link to the decision record. |
| `CLAUDE.md` | Current phase (if it lands before the flip); the file-map lines for `docker-compose.yml`, `Dockerfile`, `argus.service`, `scripts/`, and the new `litestream.yml` / entrypoint; a sentence under **"Live requires durable audit writes"** stating that Litestream is *adjacent* to it and does not replace per-commit fsync — **both, always**; and a new landmine: *never set `wal_autocheckpoint = 0` on Argus's connection* (§4.3), whose failure mode is unbounded WAL growth taking SQLite down when the replicator dies. |
| `docs/argus-deployment-runbook.md` | §11 verify (the new boot lines); §12 enable (the two new timers); **§13 rewritten** — the NAS section becomes the §14.3 cleanup plus a short historical note; §15 mark the Litestream item **DONE** and re-note the DO-backup item per §16; §17 add the `.env` keys as a coupled-deploy input, the `stop_grace_period`/`TimeoutStopSec` change, the DR drill (§12.1), the PITR procedure (§12.2), and **replace** the "Litestream is not in the container" gotcha with the new set (the entrypoint branch, `--entrypoint litestream` for tool access, `logging.level` never `debug`, and the new read-lock caveat on long analysis scans during a session). |
| `README.md` | §4 "Backups" rewritten around continuous replication + the two timers; the container-gotchas list updated the same way. |
| `docs/architecture/01-c4-context.md` | Replace the `Off-box backup (NAS)` external system with `Cloudflare R2 (audit replica)`; the `Rel` becomes continuous, droplet-initiated (the NAS pull was NAS-initiated — the direction reverses). |
| `docs/architecture/02-c4-containers.md` | Remove the `snapshot` container; add Litestream as a process **inside** the argus container (parent of the supervisor), and the two host timers as scheduled containers. |
| `docs/architecture/03-c4-components.md` | Only if the `/status` replication row warrants a component note; likely not. |
| `docs/architecture/04-uml-sequences.md` | **Yes — the boot sequence changes.** Add the entrypoint's flock probe / pre-flight / restore / verify steps ahead of `startup()`, and add the shutdown sequence with the final sync. |
| `docs/decisions/monitoring-surface.md` | One paragraph: the stdlib-only statement is about the Python dependency tree; a pinned static Go binary in a separate process does not change it (§10). |
| `docs/0dte_spx_desk_spec.md` | §5's reuse table — point the "SQLite + Litestream" row at the new decision record. |
| `docs/operator-cheatsheet.md` | The two new timers' status commands, and the PITR one-liner. |
| `scripts/argus-deploy.sh` | Add `LITESTREAM PREFLIGHT FAILED\|litestream shut down` to the red-flag grep. `BUILD_INPUTS` gains `litestream.yml` and `scripts/argus-entrypoint.sh` — both are baked into the image, so a change to either **must** force a rebuild, and today's list would silently skip it on a "config-only" release. Note that the first Litestream deploy is a **coupled image + `.env` move** (new required-for-replication env keys), like every coupled config deploy in CLAUDE.md. |

---

## 16. What this closes, and one thing it does not

**Closes `docs/argus-deployment-runbook.md` §15's first open item** —
"Litestream → Cloudflare R2 replication: deferred to pre-live" — and with it the
last before-go-live durability gate.

**§14's DigitalOcean droplet-backup item: the calculus changes, but do not fold
it in.** Its value was largely "the volume is the only copy of the audit chain".
That is no longer true, so a droplet snapshot now buys **rebuild time**, not
data — and the box is already reproducible from git + the pinned image + `.env`.
Its priority drops. Note, do not build.

**But that sentence exposes one thing this design does not cover, and it should
be said plainly: `.env` is not replicated by anything.** It is gitignored, it is
not in the image, and it is now the **only irreplaceable state on the droplet
that has no off-box copy** — a refresh token, the kill-switch bearer token, the
Slack webhook, and (after this) the R2 credentials. Losing the droplet means
re-minting all of them. That is a separate, small, and entirely offline problem
(an encrypted copy in a password manager), and it is the one remaining thing a
droplet snapshot would cover that R2 does not. **Flagged, out of scope, worth its
own line in §15's open items.**

---

## 17. Decisions requiring sign-off

| # | Decision | §|
|---|---|---|
| D1 | **Pin Litestream v0.3.14**, not v0.3.13, not v0.5.x. | 2 |
| D2 | **Shape A (`replicate -exec`)**, accepting that a Litestream crash costs an ungraceful Argus stop (Tier 2 telemetry only), and rejecting the sidecar because it loses the shutdown and restore orderings. | 5 |
| D3 | **Two deploys**: Deploy A adds a Tier 1 write-latency histogram to `AuditLog` + `/status` and establishes a clean baseline; Deploy B adds Litestream and is judged against it. `src/` only, no config key, either time. | 4.4 |
| D4 | **The pass/fail bar** as stated — p99 ≤ 5.0 ms, max ≤ 250 ms, and **zero** Tier 1 waits ≥ 1000 ms, the last being an absolute. | 4.5 |
| D5 | **`connection.py` is unchanged**; `wal_autocheckpoint` stays at SQLite's default despite Litestream's own recommendation, because the recommended setting's failure mode is unbounded WAL growth when the replicator dies. Held by a test and a CLAUDE.md landmine. | 4.3 |
| D6 | **The entrypoint probes the flock before Litestream starts**, using the real `DatabaseLock` class. | 6.2 |
| D7 | **A Litestream pre-flight failure runs Argus UNREPLICATED and loudly**, never a crash loop. AT-7 is a hard gate. | 6.3 |
| D8 | **`verify_chain()` gates a restored chain, in the entrypoint, and runs nowhere else on the boot path.** New `tools.verify_chain`. | 6.4 |
| D9 | **`sync-interval: 5s`** — set to §1's stated RPO target instead of TOMIC's `1s`; at 0.2 Tier 1 records/sec it costs at most one record of tail and takes Class A from 56% to **13%** of the free allowance and worst-case RTO from 88 s to 18 s. Plus **`checkpoint-interval: 1m` pending measurement, tuning direction DOWN**, **`retention: 72h`** (cut from 168h for cost, not durability) and **`snapshot-interval: 24h`** with **`-parallelism 32` on every restore**. | 8, 8.1, 8.2 |
| D9b | **Stay on R2, and fix the config rather than the provider.** The operations bill was a configuration choice, not a property of R2. Evaluated: **Backblaze B2** (Class A/B/C went free May 2026 — better for a write/list-heavy replica, but metered egress past 3x stored bills the nightly full-restore drill), Scaleway (75 GB free), Storj (trial only), Wasabi/Hetzner (minimum spend), AWS/GCS/Azure (metered egress — reject). **R2's unmetered egress is what makes a nightly restore drill affordable**, and that drill is this design's strongest guarantee. | 8.2 |
| D9c | **OPEN — Richard's call: add the NAS as a SECOND Litestream replica over SFTP.** Free, hardware already owned, and it reframes §14 as retiring the pull *mechanism* rather than the NAS. **But it inverts the credential direction**: today the droplet holds no NAS credentials and the NAS pulls against an `rrsync -ro` forced command; an SFTP replica gives the droplet a key that can WRITE to the backup. Not recommended or rejected here — the trade is a security property against a durability one. | 8.2 |
| D10 | **Two R2 buckets, not two prefixes**, because R2 tokens are bucket-granular. Metrics endpoint **off**. `logging.level: info` pinned. | 8, 9 |
| D10b | **`-parallelism` raised on every restore, 32 as an upper bound to MEASURE, not to ship** (§20.3 — on 2 vCPU it may be CPU-bound and no faster than 8) (entrypoint, nightly drill, PITR), not the default 8. `restore` fans out over WAL *indexes* and the segment count is set by `sync-interval`, so it is the cheapest insurance against a slow droplet-to-R2 link. It is the only RTO lever that costs nothing — `snapshot-interval` costs storage and an earlier draft wrongly reached for it first. | 12.3 |
| D10c | **The control surface is DOWN for the duration of a boot restore** (entrypoint restores before Python starts). Accepted, not worked around — the kill switch halts new *entries* and a process that has not started cannot enter — but stated explicitly rather than discovered, and another reason to keep RTO low. | 20.4 |
| D10d | **Fetch and verify the Litestream tarball in the BUILDER stage**, `COPY --from=builder` into runtime — not `apt-get install curl` in the runtime stage as §10 first drafted. | 20.8 |
| D11 | **`stop_grace_period` 30s → 45s and `TimeoutStopSec` 90s → 120s**, or the measured equivalent, because a network round trip is now appended to the end of shutdown. | 7.2 |
| D12 | **Monitoring lives outside the trading process**: an **hourly** `generations` lag check that curls Slack directly, plus the nightly restore-and-verify drill (option **b**), which also measures RPO and RTO every night. Hourly rather than 15-minute because that check lists every WAL object in the bucket and `ListObjects` is Class A — at 96 runs/day it is the largest line on the R2 bill and crosses the free tier by itself. | 11.1, 8.1 |
| D13 | **`/status` gains one replication row**, file-stat shaped, amber-when-unknown, explicitly *not* claiming R2 is current. Threshold is a module constant, not a config key. | 11.3 |
| D14 | **The NAS path is retired only at phase 4**, after five green trading days and one full DR drill including AT-9. `/path/to/backups` is kept as scratch. | 14 |

---

## 18. Rejected, and why

| Shape | Why not |
|---|---|
| Sidecar Litestream container | Loses the shutdown-ordering and restore-ordering guarantees that are the point; two lifecycles that silently diverge. §5B |
| Litestream as a host systemd unit | Reaches into Docker's internal volume path; `down -v` removes the volume under it; separate supply-chain story; restore becomes manual. Retained as §4.8's fallback if the Tier 1 bar fails. §5C |
| `wal_autocheckpoint = 0` on Argus's connection | Litestream's own recommendation, but its failure mode is unbounded WAL growth → full disk → SQLite down, when the replicator dies. Wrong failure direction. §4.3 |
| `verify_chain()` on every boot | O(rows) with a `sha256` per row, growing daily, on every restart including mid-session recovery, to check a file whose provenance did not change. §6.4 |
| `verify_chain()` on the live DB on a cadence (option c) | A growing full-table read lock on the writer's connection — the thing `AuditLog.read()`'s contract forbids — and it would stall Litestream's checkpoints too. §11.2 |
| Dropping the local integrity job entirely (option a) | Litestream replicates bytes; a broken chain replicates just as faithfully as an intact one. And it never answers "is R2 restorable?", which is the failure this whole section exists for. §11.2 |
| Litestream's Prometheus metrics endpoint | Under `network_mode: host` a `:9091` bind lands on the public interface, which `config.py`'s posture already forbids; and it reports Litestream's own opinion, which is exactly what is wrong in a silently stalled replica. §8 |
| Alerting through Argus's `AlertFn` seam | The check must fire when the Argus process is dead. §11.1 |
| One bucket, two prefixes | R2 API tokens are bucket-granular; the dev credential would be able to write the production replica. §9.1 |
| Shipping the instrument and Litestream together | No clean baseline, and it lands on top of an open lag-ramp investigation. §4.4, §4.7 |

---

## 19. Open questions this design does not close

1. **The actual checkpoint stall distribution.** §4.1's "sub-millisecond to low
   single-digit ms" is arithmetic on WAL size, not a measurement. M3/M4 settle it.
2. **`generations` lag behaviour on an idle database** (overnight, weekends).
   §11.1's threshold is provisional for one week by design.
3. **The real replica size** — specifically the **LZ4 compression ratio**, which
   is assumed at 3× and moves §8.1's storage table by a factor of two, and
   therefore whether `retention: 72h` is proportionate. §8.1 says take a `du`
   plus a full month of real R2 usage.
4. **Whether the intra-session lag ramp is affected.** §4.7 commits to not
   confounding it; it does not commit to explaining it.
5. **The v0.5 upgrade.** Its own decision, against a proven v0.3 baseline.
6. **`.env` has no off-box copy.** §16.
7. **The audit log grows without bound, and nothing prunes it.** ~700 MB/month
   measured (§8.1). This design does not create the problem — it *surfaces* it,
   because R2 storage now multiplies the DB by the snapshot count, and because
   restore time scales with it. It is a droplet-disk item before it is an R2
   item. **Pruning is not a small change**: `verify_chain()` asserts `seq`
   contiguity from 1, so rows cannot simply be deleted — any archival scheme has
   to roll to a new database and keep the old one, which is its own design.
   **Has its own task prompt**
   ([`argus_audit_log_growth_prompt.md`](../tasks/argus_audit_log_growth_prompt.md)),
   deliberately sequenced AFTER this deploy and deliberately not coupled to it —
   but note the numbers flow both ways: if `watchdog.check` volume drops, §8.1's
   storage table and §12.3's RTO object count are stale, and whichever ships
   second re-reads the other's. It should also get a line in the runbook's §15.
8. **How R2's free allowance composes with its billable-unit rounding.** The
   pricing page's own example — *"one million and one operations, you will be
   billed for two million operations"* — does not say whether the 1M free
   allowance is deducted before or after the round-up. The difference is
   $4.50/month at the point of crossing. **Settle it from a real invoice, not
   from the doc**, and do not size the health-check cadence against the
   optimistic reading.
9. **Droplet→R2 per-object GET latency**, which is the whole of §12.3's RTO
   table and is currently a 20–50 ms guess. It decides whether the default
   restore parallelism meets the 120 s bar or misses it by 20%. AT-4b measures
   it once; the nightly drill tracks it thereafter.

---

## 20. The droplet and the tailnet

Everything above is about Litestream. This section is about the box it runs on.
Checked against the runbook's own provisioning: **Ubuntu 24.04, Basic Premium
AMD, 2 vCPU / 4 GB / NVMe**, ufw `default allow outgoing`, a DO Cloud Firewall
with **outbound left at defaults** (§2/§3), Tailscale for admin access (§6), and
DO Monitoring alerting on CPU / memory / disk **> 70% for 5 min** (§17).

### 20.1 Nothing needs to open, and that is worth stating

- **No inbound rule changes, ufw or DO.** Litestream opens no listener (the
  metrics endpoint is off, §8) and only makes outbound HTTPS to
  `<account>.r2.cloudflarestorage.com`, which both firewalls already permit.
  Verified against the runbook rather than assumed.
- **The kill-switch bind is untouched.** `network_mode: host` is a container
  property; adding a parent process does not change which interface
  `control.py` binds. §5.
- **Outbound transfer is a non-issue.** Uploads are a few hundred MB/month
  against the 4 TB included; the nightly drill's downloads are *inbound* and
  unmetered by DO as well as by R2.

### 20.2 Two couplings that will bite in a way that looks like an R2 outage

- **Tailscale owns DNS.** `tailscale up` on Ubuntu hands `/etc/resolv.conf` to
  systemd-resolved with MagicDNS in front. So a tailscaled failure or a MagicDNS
  change **breaks name resolution for R2**, and replication stops for a reason
  that has nothing to do with Cloudflare. The runbook's replication-troubleshooting
  entry must say: **check `resolvectl status` and `tailscale status` before
  blaming R2.** This coupling did not exist before — Argus's only other outbound
  dependency is tastytrade, and a broker outage is loud in a way a stalled
  replica is not.
- **`network_mode: host` means Litestream inherits the host routing table.**
  If an exit node is ever enabled on this droplet as a *client*
  (`tailscale up --exit-node=…`, or `--accept-routes` advertising a default
  route), **R2 traffic would leave over the tailnet.** Neither is configured
  today. **Landmine line for CLAUDE.md**, because the failure is silent and the
  command that causes it is an ordinary Tailscale admin action.

### 20.3 CPU: a second Tier 1 channel §4 did not cover

§4 frames the Tier 1 risk entirely as **SQLite write-lock contention**. On a
2-vCPU box there is a second channel: Litestream LZ4-compresses and uploads on
**the same two cores the event loop runs on**, in the same cgroup, and the
heartbeat's breach threshold is **50 ms**. At `sync-interval: 5s` the steady
state is trivial. Two moments are not:

1. **The daily snapshot** — a full LZ4 compression of the whole DB, growing
   ~700 MB/month. If it lands inside the session it is a multi-second CPU burst
   beside the watchdog. Litestream's `snapshot-interval` is relative to
   generation start, **not wall-clock**, so it *cannot be pinned outside market
   hours** — which means M4's checkpoint attribution must also look for a
   once-a-day CPU spike, not only for checkpoint locks.
2. **`-parallelism 32` on a restore.** D10b justified 32 on *latency* grounds,
   assuming CPU is free. On 2 vCPU, 32 concurrent TLS streams plus LZ4
   decompression may be CPU-bound and **no faster than 8**. **Correction to
   D10b: treat 32 as an upper bound to measure, not a value to ship.** AT-4b
   should sweep 8 / 16 / 32 and take the best. A restore is also the one moment
   the box is doing nothing else, so a saturated CPU there is acceptable in a
   way it never is mid-session.

This also matters for the lag-ramp confounder (§4.7): a CPU-competing sibling
process is a *new* candidate cause for that symptom family, distinct from the
checkpoint-lock class already named.

### 20.4 The control surface is DOWN during a boot restore

The entrypoint restores **before Python starts**, so `/status` and `/kill` are
unavailable for the whole restore — inside `TimeoutStartSec` (300 s).

This is a deliberate trade and it must be stated rather than discovered. It does
**not** violate the always-on non-negotiable's purpose: the kill switch exists to
halt *new entries*, and a process that has not started cannot enter. Nothing is
being managed during the window. But it does extend the "no `/status`" window
that the 2026-08-14 incident made a named concern, and it is another reason to
keep RTO low — which `sync-interval: 5s` already does (18 s, §12.3).

**It only happens on a restore**, i.e. a fresh or lost volume, which is the one
boot where an operator is already watching.

### 20.5 Boot budget, and the cost that grows

The restore path's `TimeoutStartSec` budget is restore + `verify_chain` +
migrations + control bind + broker reconcile. **`verify_chain` is the term that
grows**, and it was measured for this design rather than assumed — `json.loads`
plus a `sha256` per row, **~10.7 µs/row** on a dev Mac:

| Table size | Reached at | `verify_chain` (dev Mac) | at 2× slower |
|---|---|---|---|
| 500k rows | today-ish | 5 s | 11 s |
| 4M rows | ~1 year of live trading | 43 s | **86 s** |
| 20M rows | ~4 years | 214 s | **428 s — over `TimeoutStartSec` alone** |

So D8's boot-time verify is comfortable now, tight in a year, and **broken in
about three** — on the droplet, at the current write rate. Three consequences:

1. **Measure it on the droplet before shipping**, not on a Mac, and record the
   number.
2. **`TimeoutStartSec` may need raising with the table**, and that is a systemd
   change nobody will remember to make. The nightly drill (§11.2) should log its
   own `verify_chain` seconds so the trend is visible, exactly as it does for RTO.
3. **This is the sixth cost linear in table size**, and the strongest single
   argument for
   [the audit-log growth task](../tasks/argus_audit_log_growth_prompt.md) — it is
   the one where the growth eventually breaks a rail rather than raising a bill.

### 20.6 Disk

80 GB, with DO Monitoring already alerting at **70%** — which is the existing
rail for all of this and needs no new work. The budget to be aware of:

| | |
|---|---|
| Live DB + WAL | ~700 MB/month, unbounded (§19.7) |
| Nightly drill's retained scratch copies | **2 × DB** — the retention count is a decision, not an accident |
| Restore temp files | `downloadWAL` writes `<db>-<index>-wal` beside the DB; needs ≈ WAL-since-snapshot free |
| Images, journal, json-file logs | ~2 GB, already bounded |

Comfortable for years at 80 GB. **The point is that the drill's scratch
retention multiplies the growth problem by three**, so if §11.2's retention is
ever raised from 2, it is raised against a number that is itself growing.

### 20.7 Clock

Litestream's `retention` enforcement and `-timestamp` PITR are **wall-clock
dependent**. Argus itself is not — it computes ET explicitly (`clock.py`) and
never reads host-local time — so this is a **new** clock dependency the system
did not previously have. `systemd-timesyncd` is on by default on Ubuntu 24.04
and nothing needs configuring; it earns a line in the runbook because a skewed
clock silently changes what retention *deletes* and what a PITR restore *returns*,
and neither failure announces itself.

### 20.8 One Dockerfile correction

§10's block installs `curl` in the **runtime** stage and purges it afterwards.
That leaves apt lists and an extra layer in the image the trading process runs
from, and it contradicts stage 1's own stated purpose ("leave no toolchain
behind"). **Fetch and checksum-verify the tarball in the BUILDER stage, then
`COPY --from=builder /usr/local/bin/litestream /usr/local/bin/litestream`.** The
verification is identical, the runtime image gains one static binary and nothing
else, and the two-stage discipline already in the file is preserved.
