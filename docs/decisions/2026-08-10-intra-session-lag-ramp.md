# The intra-session lag ramp — RESOLVED 2026-08-10 (measured 2026-08-05)

*The +0.21s/hour event-loop lag ramp: resolution, attribution caveat, and the elimination trail.*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


**Fixed. Read this subsection before the elimination trail below, which is kept
because most of it is durable evidence about what this process does NOT do.**

Measured 2026-08-10, the first full-workload session after the fixes (133+
`shadow.would_submit`, the full `signal.candidate`/`gate.decision`/
`shadow.would_submit` Tier 1 triple at 5s cadence):

| hour | baseline (07-28..08-06) | 2026-08-10 (full session) |
| --- | --- | --- |
| 09h | 0.065-0.103s, n=8-13 | 0.204s, n=2 |
| 10h | 0.164-0.195s, n~360 | **zero** |
| 11h | 0.362-0.418s, n~387 | **zero** |
| 12h | 0.592-0.673s, n~383 | **zero** |
| 13h | 0.797-0.880s, n~356 | **zero** |
| 14h | 1.008-1.120s, n~220-372 | **zero** |
| 15h | 1.247-1.366s, n~372 | **zero** |

**~2,100 breach episodes per session became 2**, both inside the first twenty
minutes, peak 0.265s against a baseline that reached a 1.3s mean and 2.2s peak by
15:00. Memory over the same session: VmRSS 185.7 MB (10:09, the post-chain-load
peak) -> 139.9 MB (12:29) -> 139.9 MB (15:44) — **+16 kB across the final 3h15m**,
i.e. the 09:40 transient is reclaimed and nothing accumulates after it. VmData
258.9 -> 212.8 -> 212.8 MB. fds/sockets pinned at 17/8 all day.

**ATTRIBUTION — the `keepalive_expiry` fix, held at high but not total
confidence.** Two changes shipped together on 08-06 (v0.2.4 keepalive +
`ARGUS_LOOP_DEBUG`, v0.2.5 non-blocking docker logging), so production data alone
cannot separate them. The argument for keepalive:
  - The ramp's period was locked to ~11s = `account_refresh_s` (10.0) + its work,
    and **a successful refresh writes no log line** — so blocking log delivery can
    AMPLIFY a stall that exists (`loop_lag_breach` does log) but cannot originate
    one on a silent 10s cycle.
  - The keepalive fix acts on exactly that event, removing ~4,500 TLS connection
    setups per session.
  - The floor dropped too, not just the slope: 09h went n=13 -> n=2, before any
    ramp has developed. A floor-only change would still have breached later as
    the ramp lifted cost back over the 50ms threshold; 10h-12h are empty.
  - `ARGUS_LOOP_DEBUG` adds overhead, so it can only have made things worse.

**THE MECHANISM IS NOT PROVEN, and do not write it up as if it were.** Why
~4,500 connection setups produced a LINEAR, session-length ramp that reset on
process restart is unexplained. The shape that fits is per-connection bookkeeping
accumulating in httpcore's pool; the offline harness did not reproduce it, but
that harness compressed 6.5 hours into ~10 seconds and is blind to exactly this.
To attribute definitively, revert ONLY the log driver (a compose change, no
rebuild) and run one session — deliberately not done, because both changes are
independently justified and the pre-sandbox checklist outranks the curiosity.

**Two traps this investigation walked into, both worth not repeating:**
  - **A quiet day is not a fixed day.** 2026-08-07 showed the ramp gone and was
    read as a fix. It was NFP — a `[events]` blackout date — so `evaluate()`
    returned `EVENT_BLACKOUT` before any candidate existed and the ENTIRE Tier 1
    signal path (~11,000 awaited `to_thread` SQLite writes per session) never ran.
    **Always check `would_submit today` > 0 and the blackout file before drawing a
    conclusion from a lag reading.** No day in the 07-28..08-06 baseline was a
    blackout (the file was generated 08-04), so no historical comparison exists.
  - **`/status` `breaches`/`sustained`/`max_lag` are PROCESS-lifetime, not
    session** — `LoopHeartbeat` is built once in `startup()`. Subtract the prior
    session's totals before comparing, or a 3-day-old process reads as a disaster.

The rest of this section is the elimination trail. It stays because the negatives
are durable: **GC (all generations), Python object leaks, pydantic-core, sqlite,
OpenSSL, THP/compaction, CPU steal, CFS throttling, glibc arena growth, and
fd/socket leaks are all ruled out by measurement** — a useful map for whoever
chases the next stall.

Found while verifying the Slack-storm fix, from `journalctl` history rather than
from a hypothesis.

Mean event-loop lag per hour, extracted from every `loop_lag_breach` record on the
droplet (`n` is breach episodes in that hour):

| hour | Jul 28 | Jul 29 | Jul 30 | Jul 31 | Aug 3 | Aug 4 |
| --- | --- | --- | --- | --- | --- | --- |
| 09h | 0.065 | 0.099 | 0.092 | 0.073 | 0.098 | 0.103 |
| 10h | 0.176 | 0.179 | 0.195 | 0.189 | 0.176 | 0.164 |
| 11h | 0.405 | 0.400 | 0.400 | 0.418 | 0.377 | 0.362 |
| 12h | 0.637 | 0.645 | 0.673 | 0.636 | 0.611 | 0.592 |
| 13h | 0.859 | 0.858 | 0.874 | 0.880 | 0.797 | 0.805 |
| 14h | 1.094 | 1.120 | 1.112 | 1.095 | 1.019 | 1.008 |
| 15h | 1.290 | 1.312 | 1.366 | 1.315 | 1.247 | 1.223 |

Peak single readings reach **2.2-2.3s**. Roughly 350-400 breach episodes per hour
throughout, i.e. one every ~9-10s, at a frequency that stays flat while the
MAGNITUDE ramps. Six sessions, near-identical to three decimals.

What is established, and what is not:

- **It tracks PROCESS UPTIME, not time of day.** 2026-08-05's process was restarted
  at 11:47 and the ramp restarted with it: realigned by uptime, ~0.7h in reads
  0.154s (normal 0.18s), ~1.7h in reads 0.336s (normal 0.40s), ~2.7h in reads
  0.514s (normal 0.64s). Same curve, shifted. **A restart clears it** — the
  signature of accumulation, not of load or market conditions.
- **It is PRE-EXISTING and predates the whole execution-engine merge.** Nothing in
  chunks 1-5, the notifier, the heartbeat change or the counts change causes it;
  the ramp is identical on 07-28, before any of it.
- **The mechanism is NOT known.** Ruled out by reading: `QuoteBook`/`GreeksBook` are
  latest-only dicts, every `append` in the signal/risk/marketdata path is a per-call
  local, `AuditLog.counts` is a ~17-key dict copy. Leading hypothesis is object
  accumulation producing growing gen-2 GC pauses; a fixed-frequency, growing-cost
  event also fits the ~10s account liveness refresh. **Do not "fix" this from a
  hypothesis — instrument first.**
- **A single `docker stats` reading (114 MiB at ~2.5h uptime) proves nothing.** A
  trend is what discriminates.

**Why it is a blocker rather than an annoyance.** The ramp peaks in the FINAL HOUR,
which is exactly what spec §4 built the watchdog for ("gamma risk in the final hour
doesn't care whether entry logic is running"). At 15:00 with a 0DTE position open, a
1.3s mean / 2.2s peak freeze means the watchdog cannot evaluate a trigger, the
price-walking loop cannot honour a COA cycle boundary (the whole reason
`cycle_wait_s` combines both COA figures), and `POST /kill` does not answer. Every
safety rail is least responsive in the hour they matter most. Shadow holds nothing,
so today it costs only accuracy of the freshness ages; sandbox/live is a different
question and this must be understood before that flip.

**The instrumentation is deployed** (`status.py`): `/status` now reports RSS, RSS at
process start, growth, growth/hour, GC counts and cumulative collections per
generation, plus a gen-2/hour rate. Read it late in a session and compare the gen-2
rate against the lag. `read_rss_bytes` parses `/proc/self/statm` and returns None
rather than raising anywhere it cannot (dev Macs); `gc_snapshot` is O(1) and an AST
test forbids `gc.get_objects()`/`get_referrers()` in this module — materializing the
object graph inside the endpoint added to diagnose stalls would be the joke that
writes itself.

Still worth ruling out, testable offline: whether the tastytrade `DXLinkStreamer`
buffers per-event-type without bound at 120 symbols x (quote+greeks).

## The REST refresh path is RULED OUT, and gen-2 is not the whole story (2026-08-06)

First session running `tune_gc`. Two findings, one from production and one from
offline measurement. Neither closes the ramp; both narrow it.

- **gen-2 GC cannot be causing the breaches.** `/status` at 10:23 ET reported
  **2 gen-2 collections in 17h54m** (0.1/h) — the tuning working exactly as
  designed — against **126 breach episodes in a 43-minute session**. Total
  collections across ALL generations for the whole process life was 392, and
  gen-0/gen-1 are microsecond passes. So a non-GC stall source exists and is
  active. This does NOT retire gen-2 as a past contributor: under stock CPython it
  would have run ~500/hour, the right order for the baseline's 350-400 episodes/
  hour. Read it as **at least two sources**, one of which is now largely removed.
- **THE RAMP SURVIVED THE GC FIX INTACT — `tune_gc` is a reduction, not a cure.**
  The full session was a clean intervention experiment: gen-2 fell from ~500/hour
  to **2 collections in 22 hours** and the curve did not move. 09h 0.086 / 10h
  0.165 / 11h 0.374 / 12h 0.582 / 13h 0.831 / 14h 1.027 — every hour inside the
  pre-tuning baseline band, and episode counts (360/387/383/356) inside the
  baseline's 350-400. Keep the tuning (it removes ~500 real pauses an hour); stop
  calling it the mechanism. `tune_gc`'s own docstring has been corrected.
- **The period pins the trigger to the account refresh, and measures it.**
  Inter-breach gaps over the session: n=1719, median 10.79s, **mode 11s**, with
  harmonics at 21/31/41/51s (one, two, three, four missed detections) and 87% in
  the 10-12s band. `_refresh_loop` sleeps 10.0s and THEN awaits `refresh_now`, so
  the period is `10.0 + work`, i.e. the gap is itself a measurement of the stall:
  10.31s at baseline (CLAUDE.md's earlier figure) -> ~11s now, and the 14h lag mean
  is 1.027s. Same number. Nothing else in the process runs on that cadence
  (heartbeat 0.1s, dxlink stall watch 5s, signal loop 5s, memprofile 1800s).
- **There is NO Python object leak.** tracemalloc reports #38-#44, 11:28 -> 14:28,
  total growth **+3054/3063/3058/3060/3062/3099/3069 KiB** — flat to ±20 KiB
  across three hours during which lag went 0.374s -> 1.027s. Every top site frozen
  to the object: `linecache` 936.5 KiB/9,984 (the profiler's own source cache,
  saturated), `importlib` 591.6 KiB/4,749 (startup), `dxlink.py:77` 25.6 KiB/1,092
  (the resident quote book), `asyncio/locks.py:167` 31.9 KiB/86. Only
  `json/decoder` fluctuates (45-52 KiB), which is in-flight parsing, not growth.
- **RSS growth is real but far too small to be the cause.** 233.9 MB at 10:23 ->
  248.6 MB at 14:46 = **+3.4 MB/h in-session**, and the lifetime rate FELL (7.3 ->
  6.6 MB/h), so most of the 146 MB predates the session. 15 MB of extra heap does
  not triple a fixed CPU task. Memory is looking like a red herring, not a lead.
- **What that leaves.** tracemalloc sees Python allocations only, so a native
  accumulator would be invisible — and the heaviest native workload here is
  **pydantic-core (a compiled Rust extension) validating ~2,420 DXLink
  `Quote`/`Greeks` events per second**, plus OpenSSL and sqlite3. Unconfirmed;
  listed so it is not re-derived from scratch.
- **`sustained` is now 1,062 in one session** (it was 1 at 10:23 — the warmup).
  `[heartbeat] trip_on_sustained_stall` would fire a thousand times a day, not
  once. The standing warning against flipping it is now understated, not overblown.

## Stop narrowing by elimination — ask the runtime (`ARGUS_LOOP_DEBUG`)

Everything above is an exclusion. `app.enable_loop_debug()` is the one diagnostic
that returns a NAME: `loop.set_debug(True)` makes `_run_once` time every callback
and log any exceeding `slow_callback_duration` as
`Executing <Task ... coro=<thing() at file:line>> took 1.234 seconds`. Verified
end-to-end locally against a synthetic 0.6s block; it named the coroutine.

- **Env-gated and inert by default**, same pattern as `ARGUS_TRACEMALLOC`:
  `ARGUS_LOOP_DEBUG=1`, threshold via `ARGUS_SLOW_CALLBACK_S` (default 0.25s,
  chosen well above the heartbeat's 0.05s so the ~1s stall is not buried).
  A `<= 0` or unparseable threshold falls back rather than obeying — on a 2,420
  event/sec feed, logging every callback floods out the thing being hunted.
- **Called LAST in `startup()`**, after `tune_gc` and the profiler, so debug mode's
  per-callback overhead never applies to startup — the 09:40 chain load is already
  the largest stall of the day and does not need inflating.
- **`asyncio` is deliberately NOT in `__main__.py`'s `_NOISY_LOGGERS`**, and slow
  callbacks log at WARNING, so they reach the journal at the default INFO root.
- **Diagnostic only.** Debug mode also enables coroutine origin tracking and
  non-threadsafe-call checks. Run it a session or two, read the names, turn it off.
- **"Flat frequency" was SATURATION, not a clock.** 3600 / 10.31s = 349, i.e. the
  baseline's 350-400 episodes/hour is exactly one breach per account liveness
  refresh. Today's rate climbed 39/h (09:40-10:00) -> ~308/h (10:00-10:22) as
  per-refresh cost grew into the 50ms threshold. The frequency was never evidence
  for a fixed-period collector; it was the refresh period, saturated.
- **The account REST refresh path does not accumulate.** Measured, not read: the
  real SDK path (`Session` + `Account.get_positions`/`get_balances` through
  `asyncio.gather`, exactly as `refresh_now` calls it) against a local stub, 4,000
  refreshes = ~1.8 sessions — per-call cost flat (2.00 -> 1.95ms), objects flat
  (98,287 -> 98,273), RSS flat. Repeated over real TLS at 2,400 refreshes/arm with
  a `LoopHeartbeat`-equivalent probe: flat with a handshake every call (4.11 ->
  4.34ms), flat with certificate chain verification added, flat pooled (1.28 ->
  1.31ms), **and still flat with `gc.freeze()` + `gen2_threshold=100` applied** —
  which mattered because a connection is a cyclic object graph and gen-2 now runs
  0.1/h, so retention was a live worry. Zero breaches in any arm.
  **BOUND THE CLAIM CAREFULLY — this rules out PER-CALL accumulation and nothing
  else.** Each arm ran back-to-back, so 2,400 refreshes completed in ~10 SECONDS of
  wall clock: a ~2,000x compression that is blind to anything driven by elapsed
  time, and blind to the refresh running CONCURRENTLY with 2,420 market-data
  events/sec, which is the only way it ever runs in production. It does NOT
  exonerate the refresh as the place the cost lands — see the period evidence
  below, which says the cost lands there for certain.
- **RSS is not readable as leak evidence while `ARGUS_TRACEMALLOC` is on.** 233.9MB
  against the ~114MB pre-mitigation figure is not a comparison — the profiler
  roughly doubles memory at 8 frames and its own trace table is not counted in its
  own diff. Python-level growth was +2.4 MiB against +131 MB RSS. Same caveat on
  the 7,325ms `max_lag`: it is PROCESS-lifetime (`LoopHeartbeat` is built once in
  `startup()`), appears in no hourly max, and is most likely the 09:40 chain load
  (~455ms of pydantic parsing) times the profiler's allocation tax.
- **An idle process accumulates nothing.** Report #34, seventeen hours after the
  baseline and before the session opened, was +593 KiB and essentially all
  `linecache` — the profiler caching source lines to render its own reports,
  frozen at the identical 569.7 KiB / 6,127 objs in the next report. The
  accumulator is load-driven.
- **`dxlink.py:77 float(value)` at +1,088 objects is the resident quote book, not
  a leak**: 121 symbols x 4 `_to_float` fields on `Quote` + 120 x 5 on `Greeks` =
  1,084. Production confirmation of the offline "ingest is clean" result.

## keepalive_expiry vs account_refresh_s — the same trap as `timeout` (fixed 2026-08-06)

Found while ruling the refresh path out, and it is a real defect independent of the
ramp. `httpx2.DEFAULT_LIMITS.keepalive_expiry` is **5.0s**; `[staleness]
account_refresh_s` is **10.0s**; `broker/session.py` forwarded only `timeout`. So
every liveness refresh found its pooled connection already expired — and
`refresh_now` fires positions and balances CONCURRENTLY, so it opened two fresh
HTTPS connections and paid two full TLS handshakes, every 10 seconds. ~4,500
handshakes a session, on the event loop.

Measured: **4.2ms per refresh with a handshake every time vs 1.3ms pooled — 3.2x**,
~3ms of avoidable CPU per refresh locally and roughly 4x that on the droplet's
vCPU, on the exact ~10s-period event whose cost sits at the 50ms breach threshold.
Real cost is higher: the stub has no RTT and a one-cert chain.

- **It is NOT the ramp** — the same harness showed the path flat. It is the
  cheapest available reduction on the path that GENERATES the breaches.
- **Derived from `account_refresh_s`** (`KEEPALIVE_REFRESH_MULTIPLE = 3.0`), never
  hardcoded, so the two cannot silently cross again — which is exactly how this
  arose. Needs no upper bound of its own: the loader already validates
  `account_refresh_s < account_stream_max_age_s`.
- **A module constant, not a config key**, for the reason `notify.py`'s
  `DEFAULT_THROTTLE_S` is one: it tunes transport and nothing about trading, so it
  stays out of the fail-closed loader, and a new REQUIRED key would couple a
  performance fix to a config deploy.
- **The pool bounds MUST be restated.** `Limits(keepalive_expiry=x)` leaves
  `max_connections`/`max_keepalive_connections` at None, which means UNLIMITED, not
  "keep httpx's default" — passing `limits` at all replaces the whole object. Same
  family of trap as the silent 5s `timeout`: an override that quietly widens
  something else. `POOL_MAX_CONNECTIONS = 100` / `POOL_MAX_KEEPALIVE_CONNECTIONS =
  20` are httpx's own defaults, restated. A test pins them.
- **`from httpx2 import Limits` is a direct import of a transitive dep**, accepted:
  we are configuring the SDK's own HTTP client and already pass httpx kwargs
  (`timeout`) through `client_kwargs`, so this makes an existing coupling explicit
  rather than creating one. No new package enters the `--require-hashes` image.
- **The regression test asserts a RELATION** (`keepalive_expiry >
  account_refresh_s`), not a literal, because the defect was the two values
  crossing and only a relation catches them crossing again.
- **OPEN, and it does not block the fix:** whether `api.cert.tastyworks.com` itself
  holds an idle connection past 10s. If it closes first the saving evaporates,
  though the pool bounds and the derived coupling stay right either way. **DXLink's
  negotiated `keepaliveTimeout` does NOT answer this** — that is a `wss://` endpoint
  on a different protocol, while this governs the REST HTTPS pool
  (`get_positions` -> `session._get` -> `AsyncClient(base_url=CERT_URL)`). Two facts
  checked in SDK 13.2.0 while confirming that: `_reader` sends a hardcoded
  `keepaliveTimeout: 60` and DISCARDS the server's reply (`elif message["type"] ==
  "SETUP" ... : pass`), and `_heartbeat` is a hardcoded `sleep(30)`, not half a
  negotiated value — so logging the server's figure needs a patched `_reader`.
- **Follow-on worth checking**, surfaced by the same reading: since the SDK swallows
  KEEPALIVE frames, they never reach Argus, so `dxlink.py`'s `_watch_for_stall`
  timer is reset ONLY by `FEED_DATA`. On a continuous 0DTE chain it never fires; on
  a quiet feed (the cert feed is static on weekends) it would reconnect a healthy
  link. Worth checking against when the all-time `marketdata.disconnected: 20`
  happened.
