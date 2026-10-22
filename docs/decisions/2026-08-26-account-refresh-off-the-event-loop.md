# The account liveness refresh runs off the event loop (2026-08-26)

**Status: DEPLOYED and PROVEN in production, 2026-08-26.** Built to the approved
design at
[`docs/designs/2026-08-25-account-refresh-off-event-loop.md`](../designs/2026-08-25-account-refresh-off-event-loop.md),
which remains the authority on *why* each decision is what it is. This record is
what was built, what changed from the design during the build, and what the
production reading measured. Target v0.3.10. **No config key — the image deploys
alone.**

The production before/after (§11 of the design) was read the same evening and is
recorded below: **~115x fewer event-loop breaches per minute while
`account.refresh_failed` volume held flat.** Both halves of the acceptance
question, on the droplet, under load. → [The production reading](#the-production-reading-2026-08-26)

---

## What changed, in one paragraph

`AlertStreamerAccount._refresh_loop` fires every 10s and did two HTTPS GETs
**concurrently, on the event loop**. When the connection pool is cold — its
permanent state during a refresh-failure storm, because a httpx timeout destroys
the connection it fired on — each of those paid a full TCP+TLS handshake on the
loop, at a CPU cost of the same order as the 50ms heartbeat breach threshold.
Those two requests now run **serialized, against one monotonic deadline, on a
dedicated single-worker `ThreadPoolExecutor`, through a synchronous
`httpx2.Client` that carries the existing session's bearer token**. One OAuth
registration, one login, one refresh-token path, and nothing anyio-shaped ever
crosses a thread boundary.

**What this does NOT do, and no version of it could: save the connection pool.**
The pool still empties during a storm and every refresh is still cold. A cold
refresh simply stops being the event loop's problem. Read the acceptance question
accordingly — "did `LoopHeartbeat.breach_count` fall while `account.refresh_failed`
volume stayed comparable?", **never** "did the pool stay warm?", which will read
as a failure.

---

## The six approved decisions, as built

| # | Decision | Where it lives |
|---|---|---|
| 1 | Sync `httpx2.Client` on a worker thread; SDK parsing reused verbatim | `account/refresher.py` |
| 2 | `account_refresh_timeout_s` is a TOTAL budget for a serialized pair, deadline-split, with a 0.25s floor | `refresher._fetch_sync` / `_leg` |
| 3 | A dedicated `ThreadPoolExecutor(max_workers=1)`, never the default | `ThreadedAccountRefresher.__init__` |
| 4 | `AccountRefreshPort` collapses to one `fetch_snapshot()`; implementations own their bound; `refresh_now` keeps a backstop at `refresh_interval_s` | `account/port.py`, `account/stream.py` |
| 5 | Narrow scope — only the liveness refresh moves | `session.py` (one construction site) |
| 6 | Telemetry gains `leg` / `elapsed_s` / `error_class`; every existing `error_type` string preserved | `stream._note_refresh_failure` |

Two URL literals now live in Argus rather than the SDK
(`/accounts/{n}/positions`, `/accounts/{n}/balances/USD`). That is the entire cost
of shape (a): `validate_and_parse`, `CurrentPosition`, `AccountBalance`,
`map_position` and `map_balances` are all reused, so `TastytradeError` messages
are byte-identical to what the async path produced. Guarded by two drift tests —
one diffing `base_url`/headers against a real offline `tastytrade.Session` for
both `is_test` values, one asserting the SDK still calls those two paths.

---

## Three things the build found that the design did not

### 1. asyncio REBUILDS a `TimeoutError` that crosses a future boundary

This is the one that would have shipped a silent hole.

`asyncio.futures._convert_future_exc` reconstructs three exception types as
`exc_class(*exc.args)` when an executor's exception is chained onto an asyncio
future, and one of them is `concurrent.futures.TimeoutError` — which, since 3.11,
**is** the builtin `TimeoutError`.

The design's error model normalizes httpx timeouts to builtin `TimeoutError`
precisely so `error_type` keeps reading `"TimeoutError"` for every audit query
written before this change (§6.1), and carries `leg` and `error_class` as
attributes on the exception. Raised across the thread boundary, that exception
arrives on the loop as a **different object**: no `__cause__`, no attributes. The
most common failure mode in a storm — the one the whole change is about — would
have lost two of its three new telemetry fields and the httpx class in
`__cause__`, silently, with no test failing unless it asserted exactly that.

**Fix:** `_fetch_sync` never raises. It returns the exception as a value
(`_Failure`), and `fetch_snapshot` re-raises the original object on the loop.
The converter is never involved. `error_type`, `leg`, `error_class` and
`__cause__` all survive.

### 2. The in-flight guard must hold the CONCURRENT future, not the asyncio one

`§3.5`'s guard refuses to dispatch while a prior one is outstanding, because a
cancelled `run_in_executor` abandons a thread that keeps running. But cancelling
the awaiting task marks the **asyncio** future done immediately while the worker
runs on — so a guard keyed on that future is a guard on nothing.

`fetch_snapshot` therefore calls `executor.submit()` directly and wraps the result
with `asyncio.wrap_future`, keeping the `concurrent.futures.Future` as the guard's
subject. `cancel()` on it returns `False` for running work, which is exactly the
signal wanted: still outstanding.

### 3. `aclose()` cannot both queue the close and `cancel_futures=True`

The design's §4.4 snippet submits `self._client.close` and then calls
`shutdown(wait=False, cancel_futures=True)` — which would cancel the close it just
queued if the worker had not picked it up yet.

`aclose()` now branches on whether a dispatch is actually in flight. Idle (the
overwhelmingly common case): close the client directly, then
`shutdown(wait=False, cancel_futures=True)`. Wedged: queue the close behind the
running job — never close a pool another thread is reading — and shut down
**without** `cancel_futures`, because the queued close is work that must still
run. The worker is non-daemon, so it does.

---

## What was added beyond the design, and why

**Two measurement seams on `build_client`** — `base_url` and `verify`. The design
already has `transport=` (for `MockTransport`) and `executor=` (for the isolation
test's negative half). `tools/loop_lag.py` needs to point the real refresher at a
local TLS stub with its own self-signed certificate, and there is no way to
measure handshake cost against real TLS without that. `session.py` passes none of
the four, so the live path always derives its endpoint from `session.is_test` —
the sandbox-first guarantee is untouched.

**A dense lag sampler in `tools/loop_lag.py`, beside the real heartbeat.**
`LoopHeartbeat` at production settings samples every 100ms against a 50ms
threshold. Over a few hundred attempts that is a few hundred samples, and it
cannot RESOLVE a per-attempt effect of a few milliseconds — it reports `0`
breaches for both shapes whether or not a difference exists. That is the detector
doing its job (it is an alarm, not a profiler) and a useless instrument. The real
heartbeat still runs at production settings and its breach count is reported; the
percentile columns come from a 1ms sampler measuring the same quantity.

---

## What the local TLS stub actually measures — read this before quoting a number

The design cites **39.6ms p99 on-loop versus 1.11ms threaded, a 35x multiple**,
measured 2026-08-25. `tools/loop_lag.py` reproduces the *direction* and **not the
magnitude**: on a dev Mac against a single self-signed RSA-2048 certificate over
loopback, across several runs on 2026-08-26 at 150–500 attempts with a cold pool,
on-loop p99 came in between **1.6x and 5x** the threaded shape (0.84–2.65ms vs
0.48–0.58ms), and neither shape produced a single 50ms breach.

That is not a contradiction and not a reason to doubt the change; it is what a
loopback handshake against a one-cert chain costs on fast hardware. But **it does
mean the local stub cannot be cited as confirmation of the 35x figure**, and a run
of it is not evidence the production problem is fixed. Use `--live-cert` for a
closer proxy, read the direction rather than any single run's multiple, and treat
the droplet's own before/after as the only real evidence.

---

## Test coverage, and the one test that matters most

`tests/test_account_refresher.py` (30 tests) drives the real sync client through
`httpx2.MockTransport` — no network, and the real `validate_and_parse` and real
pydantic models run, so the error-fidelity assertions mean something.

**The executor-isolation PAIR is the highest-value test in the change**, because
the default executor is where `audit/log.py` dispatches every Tier 1 and Tier 2
write, and a failed audit write halts trading with no auto-retry. A single
positive assertion there would be a tautology. Both halves run through **one
harness** with a single parameter — `share_the_default` — so "the only difference
is which executor the refresher uses" is a fact about the code rather than a
claim in a comment:

- `test_a_wedged_refresh_cannot_delay_an_audit_write` — own executor: the Tier 1
  write completes in well under 1s while a refresh is wedged forever.
- `test_the_isolation_test_has_teeth` — default executor: the same write times
  out. **Verified failing in the intended direction**, not assumed to.

`tests/test_no_leaks.py` gains a refresher case, hammering `fetch_snapshot` over
two windows with **a quarter of the requests failing on a 502** — the exception
path is the one that runs during a storm, and a tagged exception, a `_Failure`
wrapper, a `__cause__` chain and a traceback are each a plausible retainer. (The
first version of that test measured +14 objects/iteration; every one of them was
a `httpx2.Request` the test's own recording harness was holding.)

`test_the_blocking_work_no_longer_stalls_the_event_loop` proves the property
deterministically without a certificate: a handler that sleeps 200ms, inside a
real `LoopHeartbeat` at production settings. On-loop: max lag ≥ 0.15s, breaches
recorded. Threaded: max lag under the 50ms threshold, zero breaches.

Full suite: **1,522 passing.** Both AST guards (`test_session_trading_side.py`,
`test_order_build.py`) untouched and green.

---

## Rules this establishes

- **Never re-add an `asyncio.timeout` around the executor dispatch.** It cannot
  bound thread work, and firing it abandons a running thread while reporting
  success at cancelling it. The backstop lives in `refresh_now` at
  `refresh_interval_s` and nowhere else.
- **Never give `BrokerAccount` a `fetch_snapshot`.** It has no bound of its own,
  so it must not structurally satisfy a Protocol whose contract is "bound
  yourself" — the same style of enforcement as `trading_session` raising for a
  market-data session.
- **An exception that crosses an executor boundary is not the object you raised**
  if it is a `TimeoutError`, a `CancelledError` or an `InvalidStateError`. Carry
  it back as a value when its identity, attributes or `__cause__` matter.
- **Bound the silence, never the retry** — the 2026-08-20/21 rule, applied again.
  `RefreshBusyError` cancels nothing and retries nothing. It makes an invisible
  wedge loud, and the staleness it lets grow feeds `blocking_entries`, an existing
  rail that already alerts and already appears on `/status`. **Failing into an
  existing rail beats failing into a new one.**
- **`error_type` is a third-party class name and will move.** `leg` and
  `error_class` are ours. New telemetry that must survive a dependency upgrade
  goes in fields we control.

---

## The production reading (2026-08-26)

**Read the same evening, on the droplet, and it settles the acceptance question in
both halves.** The deploy landed intraday, so a single trading day contains both
code versions under the *same* broker conditions — a controlled A/B nobody planned
and could not have arranged deliberately.

Split by PID, because that is what separates the two code versions (the old one
shows `asyncio.gather` / `rest.py::fetch_balances` in its tracebacks, the new one
`refresher.py::fetch_snapshot`):

| | code | window | refresh failures | **fail/min** | breach episodes | breaches | **breaches/min** |
|---|---|---|---|---|---|---|---|
| `pid 432478` | old, on-loop `gather` | 09:40:44 → 11:47:53 (127.2 min) | 508 | **3.99** | **140** | 187 | **1.47** |
| `pid 440595` | new, threaded | 12:02:40 → 15:59:54 (237.2 min) | 956 | **4.04** | **1** | 3 | **0.013** |

- **`account.refresh_failed` volume held flat** — 3.99/min → 4.04/min. This is the
  control, and it is the half that *must not* improve: the design says explicitly
  that the pool still empties and every refresh is still cold.
- **Event-loop breaches fell ~115x per minute** — 140 episodes to 1.
- **The single new-code episode is at 12:02:40, BEFORE that process's first refresh
  failure at 12:03:09.** It is the chain-subscription startup stall (the documented
  `trip_on_sustained_stall` landmine), not a refresh-induced breach. Across the
  whole four-hour session the loop breached **zero** times from account refreshes.

**The conditions made this a better test than a quiet day would have been.** Cert
was in a sustained outage — its internal authorization service
(`/customers/me/permitted?resource-subject=/accounts/…/positions`) was 502ing
through their consul mesh, with the nginx version flapping between `1.31.0` and
`1.31.3` on consecutive responses. ~4 failures/min for six hours is precisely the
failure storm the design named as the condition that keeps the pool permanently
cold, i.e. the worst case for the old shape and the case the fix exists for.

**One caveat considered and disproven by the data.** The old window contains the
09:40 open, where chain subscription stalls the loop every morning by design — a
breach cluster that would land entirely on the "before" side and could flatter the
result. It does not: the old process's episodes run right through to its last
recovery at **11:47:53**, spread across the full 2h07m rather than clustered at
the open.

**Method, recorded so the next investigation does not re-derive it.** Breaches are
alerted, never audited, and only the *recovery* is logged
(`heartbeat._observe` → `"event-loop lag recovered after N consecutive breach(es)"`).
So the journal is the only source, one line per EPISODE carrying its run length:

```
sudo journalctl -u argus.service --since today --no-pager -o short-iso \
  | grep "event-loop lag recovered after" \
  | awk '{p=$0; sub(/.*argus\[/,"",p); sub(/\].*/,"",p);
          n=$0; sub(/.*recovered after /,"",n); sub(/ consecutive.*/,"",n);
          if(!(p in f)) f[p]=$1; l[p]=$1; ep[p]++; br[p]+=n}
     END{for(p in ep) printf "pid=%-8s episodes=%-6d breaches=%-7d %s -> %s\n",
                             p, ep[p], br[p], f[p], l[p]}' | sort -k4
```

Episode counts are exact; summed run lengths are a LOWER bound, because an episode
still open at process exit never logs its recovery. **Always split by PID** — a
whole-day aggregate silently mixes code versions, which is how the first pass at
this data produced three "failure categories" that turned out to be two code
versions wearing different error messages.

## The residual the production reading exposed (2026-08-26, fixed same evening)

**`refresh_now`'s backstop fired five times, and it is documented as meaning the
bug is in the port.** It was.

`_leg` passed `timeout=remaining` — a SCALAR. httpx's `Timeout(X)` sets connect,
read, write and pool to X **independently**, so one leg could spend `remaining`
connecting and `remaining` again reading. The module docstring promised "one
monotonic deadline"; between legs that was true, within a single request it never
was, and a 5s "total" could reach ~20s. That is how an attempt outran a backstop
set at 2x the budget — and it is also the likeliest source of the one
`RefreshBusyError` the same day, since a leg that overruns holds the single worker
past the next dispatch.

**Fixed by bounding the phases that were never the point.** READ keeps the whole
remaining budget — it is the phase the budget exists for, the server thinking — and
connect/write/pool get small FIXED ceilings (1.0 / 0.25 / 0.25) that do not scale
with the budget. Worst case per leg is `remaining + 1.5s` instead of `4 x
remaining`.

**Sized from the same outage that exposed it**: across ~1,464 refresh failures on
2026-08-26 there was not one ConnectTimeout — every failure was a read timeout or a
fast 502. Connect is empirically not where this endpoint spends time, so a tight
ceiling there costs nothing and stops it stacking onto read. Deliberately NOT
tightening read: successful attempts are not timed anywhere (see below), so the
cost of shortening it cannot be quantified, and shortening the phase that is
actually failing during an active outage is the wrong direction.

**The test that existed asserted only the READ phase**, which is exactly why this
was invisible for the whole of v0.3.10. Three tests now pin the other three,
including that the ceilings do not scale with a larger budget; reverting the fix
fails two of them.

## What is still owed

1. ~~**The production reading.**~~ **Done 2026-08-26** — see above. Both halves of
   the acceptance question met, at ~115x on the breach side with failure volume
   held.
2. **`socket.getaddrinfo` remains unbounded by httpx** (the connect timeout starts
   after resolution). A pathological resolver can hold the single worker for tens
   of seconds; refreshes stop, staleness grows, entries block — fail-closed, with
   no effect on the audit log, the watchdog, `/status` or `/kill`. **Strictly
   better than before**, where the same call occupied a slot in the audit log's
   own pool via asyncio's DNS path.
3. **Exit latency** can grow by up to the refresh budget if a thread is wedged at
   shutdown (`ThreadPoolExecutor` threads are non-daemon and joined at interpreter
   exit). Headroom exists: `stop_grace_period: 30s`, `TimeoutStopSec=90s`. The
   fallback, if it ever proves tight, is a single daemon thread with a work queue.
4. **No cert coverage worth the name.** Sandbox proves API contracts and wiring,
   never realistic latency — and the thing being fixed is a latency property.
