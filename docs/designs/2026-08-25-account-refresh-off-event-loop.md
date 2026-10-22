# Design — move the account liveness refresh off the event loop

**Status: SHIPPED 2026-08-26. All six §14 decisions built as approved.**
The normative record is
[`docs/decisions/2026-08-26-account-refresh-off-the-event-loop.md`](../decisions/2026-08-26-account-refresh-off-the-event-loop.md),
which also carries **three implementation-level corrections to this document**:
`_Failure` (asyncio rebuilds a `TimeoutError` crossing a future boundary, so §6's
normalized timeout must be returned as a value, not raised), the in-flight guard
holding the CONCURRENT future rather than the asyncio one, and §4.4's `aclose()`
snippet cancelling the very close it queues. This file stays as the record of
what was *rejected* and why. **DEPLOYED, NOT PROVEN** — §11's production reading
is still owed.

---

**Status when written: APPROVED 2026-08-26 (Richard). Ready to implement — see §15.**
Target v0.3.10. Task prompt:
[`docs/tasks/argus_account_refresh_off_event_loop_prompt.md`](../tasks/argus_account_refresh_off_event_loop_prompt.md).

All six decisions in §14 were approved as written, with no amendments. Working
arrangement, decided at sign-off and recorded here because the repo has no
standing convention for it: **work on `main`, do not commit, do not push** —
Richard reviews the working tree and commits if approved.

**This document is the implementation spec.** No separate implementation prompt
exists, deliberately: a second copy of a spec is a copy that drifts. §15 is the
build order; §14 is what must not change without coming back here first.

> Read [the intra-session lag ramp (2026-08-10)](../decisions/2026-08-10-intra-session-lag-ramp.md)
> first. That fix is intact and this proposal does not touch it. This is the
> *other half* of the same event — the half that was never protected because
> nobody tested what a failure storm does to the connection pool.

---

## 0. What this changes, in one paragraph

The account liveness refresh (`AlertStreamerAccount._refresh_loop`, every 10s)
performs two HTTPS GETs against the broker. When the connection pool is cold —
which is its permanent state during a refresh-failure storm — each of those pays
a full TCP+TLS handshake **on the event loop**, and the handshake's CPU cost lands
at the same order of magnitude as the 50ms heartbeat breach threshold. This
proposal moves those two requests into a **dedicated, single-threaded, bounded
executor**, using a **synchronous `httpx2.Client`** that carries the **existing
session's bearer token** — one OAuth registration, one login, one refresh-token
path, and nothing anyio-shaped ever crossing a thread boundary. It adds **no
config key**, so the image deploys alone.

---

## 1. What is being fixed, and what is explicitly *not*

The measurement in the task prompt establishes two separate facts that are easy
to conflate. Being precise about which one this change addresses is the
difference between a fix and a hypothesis.

| Fact | This change |
|---|---|
| Handshakes on the loop cost 39.6ms at p99 / 49.6ms max on a *fast Mac against localhost*, versus 1.11ms / 1.13ms in a worker thread — **35x at p99** | **Fixed.** The cost moves off the loop. |
| An `asyncio.timeout` cancellation destroys both pooled connections; the pool sits at 0 for the duration of a storm, so *every* subsequent refresh is cold | **NOT fixed. Deliberately.** |

**This proposal does not save the connection pool, and no reasonable version of
it could.** A `httpx2` timeout — sync or async — closes the connection it fired
on, because a request abandoned mid-flight leaves the stream in a state that
cannot be reused. The pool will still empty during a storm and every refresh will
still be cold.

What changes is that **a cold refresh stops being the event loop's problem.** The
handshake still happens; it happens on a thread that nothing safety-critical is
waiting on. The r = 0.695 correlation between breaches and refresh failures
breaks not because the failures stop but because the failures stop *costing loop
time*.

This distinction matters for how the fix is verified. "Did the pool stay warm?"
is the wrong acceptance question and will read as a failure. The right one is
"did `LoopHeartbeat.breach_count` fall while `account.refresh_failed` volume
stayed the same?" — §11.

### 1.1 A second, unlisted cost that is being removed

Verified while reading, and not in the prompt:

```
anyio/_backends/_asyncio.py:2885   return await get_running_loop().getaddrinfo(...)
CPython asyncio/base_events.py     return await self.run_in_executor(None, socket.getaddrinfo, ...)
```

`httpcore`'s anyio backend resolves DNS through `anyio` → `loop.getaddrinfo` →
**the default `ThreadPoolExecutor`**. That is the same executor
`audit/log.py` dispatches every Tier 1 and Tier 2 write into (§4).

So today, **every cold broker connection already puts a `getaddrinfo` call into
the audit log's thread pool** — and during a storm that is one every 10 seconds,
each one able to block for the resolver's full retry ladder. The executor
contention that §4 exists to *prevent* is not hypothetical and not introduced by
this change; a weaker form of it is live in production right now.

The sync client resolves inline, inside its own dedicated thread. So the change
**removes** DNS work from the default executor rather than adding to it. That is
an argument for the recommended shape specifically, and it is developed in §4.

---

## 2. Verification of the prompt's reading of the code

Every claim the prompt asked to be confirmed independently, checked against
source. All four hold; two carry a correction worth having.

### 2.1 The seam — CONFIRMED, with one correction

[`account/port.py:34`](../../src/argus/account/port.py) defines
`AccountRefreshPort` (two async methods).
[`account/stream.py:108`](../../src/argus/account/stream.py) holds it as
`self._refresher`; `refresh_now()` (line 203) is the only caller.
`_refresh_loop` (line 314) is the only caller of `refresh_now`.

`AlertStreamerAccount` is constructed in **exactly one place** —
[`session.py:825`](../../src/argus/session.py) — with `refresher=broker`, where
`broker` is the same `BrokerAccount` that also serves reconciliation. So the
wiring change is a one-line substitution at one site.

**Correction to the prompt:** "A new implementation of this Protocol is the
natural shape" is right about the seam and wrong about the Protocol's *shape*.
`AccountRefreshPort` as written is two independent methods, and `refresh_now`
`gather`s them. That two-method surface is not neutral — it is what makes
concurrent dispatch the obvious implementation, and concurrent dispatch is
precisely what doubles the handshake count. §5 proposes collapsing it to one
method, and that is the only Protocol change in this design.

### 2.2 `asyncio.to_thread` cannot be cancelled — CONFIRMED, and sharper than stated

`asyncio.to_thread` and `loop.run_in_executor` both return a future wrapping a
`concurrent.futures.Future`. Cancelling the awaiting task calls `Future.cancel()`,
which **returns `False` for work already running** and does not interrupt it. The
awaiting coroutine raises `CancelledError`; the thread runs to completion,
unobserved.

Sharper: the abandonment is not only a leaked thread, it is a **leaked executor
slot**. With a bounded executor (§4) the next dispatch queues behind the
abandoned one. That is not a reason to avoid a bounded executor — it is the
reason the design needs an explicit in-flight guard (§3.4), which converts an
invisible queue into a loud, fast-failing, self-healing refusal.

### 2.3 Executor isolation is a correctness hazard — CONFIRMED

[`audit/log.py`](../../src/argus/audit/log.py) dispatches through
`asyncio.to_thread` at four sites: `record` (Tier 1, line 151), `_consume`
(Tier 2, line 195), `read` (line 408), `write` (line 443). All use the **default**
executor. A failed write latches `AuditLog.failed`, fires `on_failure` → kill
switch, and every subsequent `record()` raises `AuditWriteError`. There is no
auto-retry, by design.

The default executor also carries `notify.py:531` (the Slack POST),
`memprofile.py:199`, and — per §1.1 — asyncio's own DNS resolution.

Default sizing in CPython 3.12 is `min(32, os.cpu_count() + 4)`. On the droplet's
2 vCPU that is **6 workers**, shared by all of the above.

### 2.4 Error fidelity is load-bearing — CONFIRMED

`_note_refresh_failure` (line 241) emits `account.refresh_failed` with
`error_type`, `auth_error`, `blocking_entries`, `consecutive_failures`, `age_s`.
`is_auth_error` (line 73) matches substrings against `str(exc).lower()`. The
timeout-vs-502 split the prompt relies on is `error_type == "TimeoutError"`
versus `"TastytradeError"`. §6 preserves both strings exactly.

---

## 3. Design question 1 — which shape

### 3.1 What the SDK actually requires (the fact that decides this)

Both calls are trivial, and both are already public-API-parseable:

```python
# tastytrade/account.py:620
data = await session._get(f"/accounts/{self.account_number}/positions", params={...})
return [CurrentPosition(**i) for i in data["items"]]

# tastytrade/account.py:528
data = await session._get(f"/accounts/{self.account_number}/balances/{currency}")
return AccountBalance(**data)
```

`session._get` is `await self.refresh()` → `client.get(url)` → `validate_and_parse`.

So "bypassing the SDK's `get_positions`/`get_balances`" — the cost the prompt
attaches to shape (a) — is **two URL strings and one `params` dict**. The
response parsing is *not* reimplemented: `validate_and_parse`, `validate_response`,
`CurrentPosition` and `AccountBalance` are imported and reused verbatim, so
`map_position`/`map_balances` receive exactly the objects they receive today and
`TastytradeError` messages are byte-identical.

The prompt's cost estimate for (a) is therefore roughly 5x too high, and that
changes the ranking.

`httpx2 2.7.0` ships a synchronous `Client` with the same
`base_url`/`headers`/`timeout`/`limits` signature as `AsyncClient`, and per-request
`timeout=`. `tastytrade 13.2.0` exposes **no** sync path (`Session` holds an
`AsyncClient`; every accessor is a coroutine), which closes off the "does the SDK
have a sync path" branch of option (c).

### 3.2 (a) Sync `httpx2.Client` in a worker thread — **RECOMMENDED**

One long-lived `httpx2.Client`, used only from a dedicated single-worker
executor, carrying the live session's bearer token read fresh on every dispatch.

**For:**

- **Nothing anyio touches a thread.** The sync client is blocking sockets and
  `threading` primitives end to end — no event loop, no task group, no cancel
  scope. The `Session` object is never touched off-loop; a `str` crosses the
  boundary. This is the only one of the three shapes where the 2026-07-21 hazard
  class is *absent* rather than *managed* (§8).
- **One session, one login, structurally.** The thread has no refresh token, no
  provider secret, and no code path to `/oauth/token`. It cannot mint a token
  even if it wanted to (§9).
- **Removes DNS from the default executor** (§1.1) instead of leaving it there.
- **Timeouts are enforceable where they must be** — on the socket, per request,
  against a deadline (§3.3 / §5.2). This is the only shape where the timeout
  model is not a compromise.
- **The two calls serialize naturally**, which halves cold handshakes 2→1 as a
  side effect of the shape rather than as a separate change (the prompt's
  deferred item, resolved here as §5.2 requires).
- Small: one new module, ~120 lines, no new dependency (`httpx2` is already a
  transitive requirement of the SDK and already imported by
  `broker/session.py`).

**Against:**

- Two URL literals live in Argus instead of the SDK, and will drift silently if
  tastytrade moves the endpoints. Mitigated by a drift-guard test (§10.7) that
  compares the constructed `base_url`/headers against a real `tastytrade.Session`
  built offline, and by the fact that a moved endpoint 404s loudly on the first
  refresh rather than failing quietly.
- A **second connection pool** to the same host. Not a second session — a
  connection is not a login. Sized at 2 max / 2 keepalive (§5.4), so the process
  holds one extra idle TCP connection to cert in steady state.

### 3.3 (b) Dedicated thread with its own event loop + `AsyncClient` — **REJECTED**

**Why it looks attractive:** it keeps `Account.get_positions(session)` verbatim.

**Why that is worth almost nothing:** per §3.1, "verbatim" buys two URL strings.
The parsing, the models, and the error types are reusable in shape (a) anyway.

**What it costs:**

1. **It needs a second `Session`.** `httpx.AsyncClient` is bound to the loop it
   runs on, so the worker loop needs its own client, which means its own
   `Session`. The only way to get one without a second login is
   `Session.deserialize(session.serialize())` — which copies `session_token`
   *and* `refresh_token` *and* `provider_secret`. That clone's `_get` calls
   `await self.refresh()`, so **within ~14 minutes it mints its own token**,
   doubling `/oauth/token` pressure on the endpoint the constraints name as ban
   risk. Suppressing that means poisoning the clone's credentials or continuously
   re-syncing `session_expiration` — machinery whose failure mode is a silent
   second login.
2. **`Session` holds an `anyio.Lock`** created in `__init__` and acquired in
   `refresh()`. anyio synchronization primitives bind to the running async
   backend on first use. A `Session` object reachable from two loops is the
   2026-07-21 hazard class by construction, and the mitigation is "be careful,"
   which is the mitigation that failed in 2026-07-21.
3. **A second lifecycle to own.** `Session.__asynccontextmanager__` wraps
   `async with self._client` behind anyio's `AsyncContextManagerMixin` — the same
   enter-and-exit-in-one-task rule `dxlink.py` and `stream.py` already carry, now
   on a loop in a thread, with startup, shutdown, and exception propagation all
   crossing the boundary.
4. **Cancellation gets worse, not better.** `run_coroutine_threadsafe` returns a
   `concurrent.futures.Future`; `asyncio.wrap_future` makes it awaitable;
   cancelling it schedules a cancellation into the *other* loop, mid-TLS, into
   anyio cancel scopes. That is literally the "unwinds from the wrong task"
   sentence in `marketdata/dxlink.py`'s docstring.
5. It does **not** remove DNS from a default executor — it moves it to the worker
   loop's default executor, i.e. a *third* thread pool.

Five costs, all in the hazard classes this system has already been burned by, to
avoid writing two URL strings. Reject.

### 3.4 (c) Everything else found while reading

| Candidate | Verdict |
|---|---|
| **SDK sync path** | Does not exist in 13.2.0. |
| **Keep one client, move only the handshake** | No supported hook. httpcore's async backend performs the TLS handshake inline via `anyio`'s `TLSStream.wrap`; there is no seam between "open socket" and "handshake" that a caller can reach. |
| **Prevent the pool from emptying (drop the tight timeout)** | Falls to `broker.http_timeout_s` = 30s, which exceeds `account_refresh_s` and blows the 30s staleness budget on one hung request. And httpx's own read timeout closes the connection regardless. Reject. |
| **HTTP/2 (`http2=True`)** | Multiplexes both GETs onto one connection — but the handshake stays on the loop, so it addresses the wrong axis. tastytrade's h2 support is unverified. Orthogonal; not a substitute. |
| **TLS session resumption** | Would genuinely cheapen a *resumed* handshake, but httpx/httpcore does not thread an `ssl.SSLSession` through to `wrap_socket`, so there is nothing to enable. And it still leaves the (cheaper) cost on the loop. |
| **A general "broker I/O thread" owning all REST** | The architecturally clean long-term shape, and the right answer if REST volume grows. Rejected **on blast radius**: it would put order submission and the price-walking loop behind a new boundary on a live-fire system approaching a phase gate. Recorded as the direction, not the change. |

### 3.5 One piece of machinery shape (a) needs: the in-flight guard

Because a cancelled `run_in_executor` abandons a running thread (§2.2), the
refresher tracks whether a dispatch is outstanding:

- `fetch_snapshot()` refuses to dispatch while a prior future is unfinished, and
  raises `RefreshBusyError("outstanding for %.1fs")` immediately.
- The refresh loop therefore keeps ticking, keeps recording, and keeps escalating
  through the existing `blocking_entries` path — instead of silently stalling
  inside an await that will never return.
- With `max_workers=1` this makes queue depth **structurally zero**: at most one
  job is ever submitted, so an abandoned thread can never accumulate a burst of
  stale work that all fires at once when it unsticks.

This is the same discipline as `DuplicateOrderGuard`, and the same shape as the
rule established on 2026-08-20 and 2026-08-21: **bound the silence, never the
retry.** The guard does not retry anything and does not cancel anything. It makes
an invisible wedge loud.

---

## 4. Design question 3 — executor isolation and its sizing

*(Taken before question 2 because the timeout model depends on it.)*

### 4.1 Decision: a dedicated `ThreadPoolExecutor(max_workers=1)`

```python
ThreadPoolExecutor(max_workers=1, thread_name_prefix="argus-acct-refresh")
```

Owned by the refresher, closed from `session.py`'s existing `AsyncExitStack`.
The prefix is not decoration — it is what makes a `py-spy dump` on the droplet
attribute a wedged thread without guessing.

### 4.2 Why not the default executor

Because the default executor is where a failed audit write comes from, and a
failed audit write halts trading with no auto-retry. Six workers on the droplet,
shared by Tier 1 writes, Tier 2 writes, `/status` reads, order-ledger writes, the
Slack POST, and DNS. The refresh is the highest-frequency I/O in the process and
the one whose failure mode is "hangs." It does not belong in that pool.

### 4.3 Why exactly one worker

**Sufficiency.** `_refresh_loop` is strictly sequential — `await sleep(delay)`,
then `await refresh_now()` to completion, then sleep again. Two refreshes can
never be in flight. The in-flight guard (§3.5) makes that true even under
cancellation. `max_workers=1` is not a constraint on the workload; it is the
exact shape of it.

**Blast radius.** Bounded at 1, the worst case for the whole mechanism is
*one* OS thread, forever. It cannot starve anything, cannot grow, and cannot
interact with any other subsystem. That property is worth more than throughput
this workload does not need.

**Failure direction.** With one worker, an abandoned thread means the next
refresh fails fast on the guard. Staleness grows. At 30s
(`account_stream_max_age_s`) the gate blocks entries and the telemetry carries
`blocking_entries: true`. That is fail-closed, into a rail that already exists,
already alerts, and already appears on `/status` — rather than into a new,
unmonitored one. **Failing into an existing rail beats failing into a new one.**

**Why not 2.** A second worker is only useful if work overlaps, and overlap is
the condition we have made impossible. Its only real effect would be to let a
wedge become two wedges before the symptom is visible — it converts a loud,
fast-failing refusal into a quiet degradation. Reject.

**Recovery.** The one call in the sync path that httpx cannot bound is
`socket.getaddrinfo` (the connect timeout starts after resolution). glibc bounds
it at `RES_TIMEOUT × RES_DFLRETRY × nameservers` — ~10s typical, tens of seconds
pathological, but **not unbounded**. So a wedge unwinds on its own and one worker
recovers. Per §1.1 this exposure already exists today, in the *audit log's* pool;
this change relocates it to a thread whose only job is to be stuck.

### 4.4 Shutdown

```python
async def aclose(self):
    self._executor.submit(self._client.close)         # queued behind any in-flight job
    self._executor.shutdown(wait=False, cancel_futures=True)
```

`wait=False` because process shutdown must not block on a hung request.
`cancel_futures=True` drops anything queued but unstarted. The client **must** be
closed rather than left to GC: `_run_session_day` builds a session per trading
day, so leaking a client leaks a socket per day in a process designed to run for
weeks.

**Where the call goes.** `session.py` has no `AsyncExitStack` — teardown is the
explicit, order-critical `finally` block at
[`session.py:1175`](../../src/argus/session.py), whose ordering comments are
load-bearing (watchdog before the streams, because an in-flight exit still needs
quotes and the order feed). The refresher's `aclose()` goes **immediately after
`await acct_stream.stop()`**: that call cancels `self._tasks`, which is the refresh
loop, so by the time it returns no new dispatch can occur and closing the client
cannot race one. It must stay **before** `_safe_aclose(tt)`, so the `Session` the
token is read from outlives its last reader.

(NB `stack` in `session.py` is `build_execution_stack(...)` — the execution stack,
not an exit stack. It has no role here.)

**Exit-latency note.** `ThreadPoolExecutor` threads are non-daemon and are joined
at interpreter exit, so a wedged refresh can delay process exit by up to its own
bound (~5s, or the resolver's ladder in the pathological case). Headroom exists:
`docker-compose.yml` `stop_grace_period: 30s`, `argus.service`
`TimeoutStopSec=90s`. If this ever proves tight, the fallback is a single daemon
`threading.Thread` with a work queue instead of an executor — more machinery,
same semantics. Not proposed now.

---

## 5. Design question 2 — the timeout model

### 5.1 What `staleness.account_refresh_timeout_s` means after this change: **nothing new**

Its config comment already reads *"Per-attempt bound on the REST re-seed"* and
*"THE per-attempt bound for a `fetch_positions()` round trip, wherever one is
awaited."* This design keeps both readings literally true:

- **Refresher:** total budget for one attempt, spent across the two requests in
  order against a monotonic deadline.
- **Watchdog** (`_cache_is_over_reporting`, [`watchdog.py:1999`](../../src/argus/execution/watchdog.py)):
  unchanged, untouched, still an `asyncio.timeout` around one on-loop
  `fetch_positions()`. For a single request, "total budget" and "per-request
  bound" are the same number.

**So the second consumer named in the prompt sees no semantic change at all**,
and the 2026-08-21 reasoning about the watchdog's blind window
([book-confirm timeout bound](../decisions/2026-08-21-book-confirm-timeout-bound.md))
stands verbatim. Nobody tuning this key for one consumer surprises the other.

The alternative — making it a per-*request* bound — was rejected: two requests at
5s each is a 10s worst case, equal to `account_refresh_s`, which breaks the
invariant the loader enforces (`account_refresh_timeout_s < account_refresh_s`)
and leaves a failed attempt with no room to retry before the next is due.

### 5.2 Serialized, deadline-split — and why that resolves the prompt's deferred item

The prompt defers "serialize instead of `gather`" and asks that it be decided
*as part of* this question. It is not really separable: **in a single worker
thread the two calls are sequential whether or not you call it a decision.**
Concurrency would require two threads, two connections, and two handshakes —
re-creating the cost being removed.

```python
deadline = time.monotonic() + budget_s          # budget_s = account_refresh_timeout_s

positions = GET /accounts/{n}/positions      timeout = remaining(deadline)
balances  = GET /accounts/{n}/balances/USD   timeout = remaining(deadline)
```

Consequences, stated plainly:

- **Cold-start handshakes halve, 2 → 1.** Both requests reuse one pooled
  connection. This is the prompt's deferred "12 → 6 handshakes" win, arriving as
  a property of the shape.
- **Positions goes first**, deliberately: it is the larger payload and the slower
  call, so if the budget is going to run out it should run out on the cheap one.
- **"A slow first call starves the second" is not a regression.** Today, `gather`
  under one 5s wrapper means a slow positions call cancels *both* at 5s and the
  whole attempt fails. Under the deadline the whole attempt also fails, having
  spent the same total budget. The only behavioural difference is that a case
  where positions returns at 4.0s now leaves 1.0s for a balances call that costs
  ~1ms on a warm connection — i.e. strictly more cases succeed.
- **A floor prevents doomed requests.** If remaining budget is below
  `_MIN_REQUEST_BUDGET_S` (proposed: **0.25s**) the second request is not issued
  at all; the attempt raises `TimeoutError` immediately. Issuing a request certain
  to time out in milliseconds costs a connection and a socket for no information.
- **All-or-nothing is preserved.** If either leg fails the whole attempt fails
  and the cache is left untouched (§7). Partial seeding is *not* introduced —
  that would be a change to the fail-closed contract, not a timeout decision.

### 5.3 Where the bound lives, and the backstop

**The `asyncio.timeout` wrapper in `refresh_now()` is removed.** It has to be:
it cannot bound thread work, and firing it abandons a running thread.

That leaves a contract question — a port implementation that hangs without
bounding itself would wedge `refresh_now()` forever, silently. Two mechanisms:

1. **Contract, stated in the Protocol** (§5.5): implementations own their bound.
   Enforced by a test, not by the type system. Honest about that.
2. **A structural backstop in `refresh_now()`, sized at `refresh_interval_s`.**

```python
try:
    async with asyncio.timeout(self._refresh_interval):     # 10.0s
        positions, balances = await self._refresher.fetch_snapshot()
except TimeoutError as exc:
    raise RefreshBackstopError(...) from exc                # distinct error_type
```

`refresh_interval_s` is the principled number here, not an arbitrary multiple:
**if an attempt has not returned by the time the next one is due, stop waiting
for it.** It is exactly 2x the implementation's own budget, because the loader
already guarantees `account_refresh_timeout_s < account_refresh_s`. It derives
from existing config rather than hardcoding, in the same spirit as
`KEEPALIVE_REFRESH_MULTIPLE`.

It should never fire. When it does, it means an implementation broke its
contract, so it raises a **distinct** exception type — `RefreshBackstopError`, not
a bare `TimeoutError` — and that name lands in `error_type` telemetry where it is
unmistakable. Its cost is one abandoned thread, absorbed by the in-flight guard
(§3.5) rather than accumulating.

The consequence of removing the wrapper: `AlertStreamerAccount`'s
`refresh_timeout_s` parameter no longer has a job and is **removed**, with the
budget passed to the refresher at construction instead. Keeping a parameter that
silently does nothing is exactly the kind of expired comment this repo's rules
already name as a hazard.

### 5.4 Client configuration

```python
httpx2.Client(
    base_url = CERT_URL if session.is_test else API_URL,   # tastytrade module constants
    headers  = {"Accept": "application/json",
                "Content-Type": "application/json",
                **({} if session.is_test else {"Accept-Version": API_VERSION})},
    timeout  = httpx2.Timeout(config.broker.http_timeout_s),   # client-wide default;
                                                               # every request overrides
    limits   = httpx2.Limits(
        max_connections=2,
        max_keepalive_connections=2,
        keepalive_expiry=config.staleness.account_refresh_s * KEEPALIVE_REFRESH_MULTIPLE,
    ),
)
```

- Built from **public** SDK surface (`tastytrade.CERT_URL` / `API_URL` /
  `API_VERSION`, `session.is_test`), never `session._client`. The `Accept-Version`
  rule is mirrored from the SDK and guarded by a drift test (§10.7).
- `keepalive_expiry` **imports** `KEEPALIVE_REFRESH_MULTIPLE` from
  `broker/session.py` rather than restating it. The constraint forbids *changing*
  that fix; deriving from the same constant is how the two stay unable to drift.
- `max_connections=2` where 1 would nominally suffice: the second leg needs a
  connection when the first leg's was destroyed by a timeout. Steady-state
  observed pool is 1.
- No `Authorization` in the client headers — it is set **per request**, from the
  token read at dispatch time (§9). This is the load-bearing detail.

### 5.5 Protocol change

```python
@runtime_checkable
class AccountRefreshPort(Protocol):
    """What the liveness refresher needs: ONE full authoritative re-read.

    ONE method, not two, deliberately. The two-method form invited the caller to
    `gather` them — which is what put two concurrent TLS handshakes on the event
    loop every 10 seconds. One call is one attempt, one budget, one connection,
    and the ordering of the two requests is the implementation's business.

    **IMPLEMENTATIONS OWN THEIR OWN TIMEOUT.** `AlertStreamerAccount.refresh_now`
    no longer wraps this in `asyncio.timeout`: the live implementation runs on a
    worker thread, and a cancelled await abandons a thread that keeps running.
    A port that can hang without bounding itself wedges the liveness refresh.
    `refresh_now` keeps a backstop at `refresh_interval_s`, which exists to make
    a broken implementation LOUD, not to be the bound.
    """
    async def fetch_snapshot(self) -> tuple[list[Position], Balances]: ...
```

`BrokerAccountPort.fetch_positions` is **untouched** — reconciliation, the tools,
`session.py`'s initial seed, and the watchdog confirm all keep working exactly as
they do (§7).

`BrokerAccount` deliberately does **not** gain `fetch_snapshot`. It has no bound
of its own, so it must not structurally satisfy a Protocol whose contract is
"bound yourself." After this change it cannot be wired as the refresher by
accident — the same style of enforcement as `trading_session` raising for a
market-data session.

---

## 6. Design question 4 — error fidelity

### 6.1 Existing fields are preserved exactly

| Condition | Today | Proposed | `error_type` | `auth_error` |
|---|---|---|---|---|
| Attempt exceeded budget | `asyncio.timeout` → `TimeoutError` | `httpx2.{Connect,Read,Write,Pool}Timeout`, **normalized** to builtin `TimeoutError` | `TimeoutError` — **unchanged** | `False` |
| 502 from cert | `TastytradeError` via `validate_response` | identical (`validate_response` reused verbatim) | `TastytradeError` — unchanged | `False` |
| 401 / expired token | `TastytradeError("token_invalid: …")` | identical | `TastytradeError` — unchanged | `True` |
| Connection reset / protocol error | `httpx2.ReadError` etc. | identical | e.g. `ReadError` — unchanged | `False` |
| Malformed payload | `pydantic.ValidationError` | identical | unchanged | `False` |
| Backstop fired (new) | — | `RefreshBackstopError` | new, deliberately distinct | `False` |
| Prior dispatch outstanding (new) | — | `RefreshBusyError` | new, deliberately distinct | `False` |

**The normalization is the one judgement call.** httpx timeouts are
`httpx2.TimeoutException` subclasses, not builtin `TimeoutError`, so left alone
`error_type` would change from `"TimeoutError"` to `"ReadTimeout"` and every
existing query — including the ones that resolved this investigation — would
silently stop matching. So the adapter raises:

```python
raise TimeoutError(f"{leg}: {type(exc).__name__}: {exc}") from exc
```

The specific httpx class survives in the message and in `__cause__`; the string
`error_type` matches on does not move. None of the httpx timeout class names
contain an `_AUTH_ERROR_MARKERS` substring, so `is_auth_error` is unaffected.

### 6.2 Three fields added, all Tier 2, all cheap

- **`leg`** — `"positions"` or `"balances"`. `gather` loses this today; the
  serialized shape has it for free, and it distinguishes "the account endpoint is
  sick" from "the whole API is sick." Carried in the message prefix and emitted
  as its own field.
- **`elapsed_s`** — measured around the await in `refresh_now`. Distinguishes
  "timed out at the budget" from "failed fast", which the coarse `error_type`
  cannot.
- **`error_class`** — coarse and stable: `"timeout" | "http" | "transport" |
  "auth" | "busy" | "backstop" | "other"`, derived from the exception type.
  Exists so the *next* investigation does not depend on third-party class names
  staying put, which is the fragility `error_type` alone carries.

### 6.3 What is deliberately not added

**Connection-pool introspection.** The 2026-08-25 investigation needed pool
depth, and reading it means reaching into `client._transport._pool` — private
`httpcore` state on a 10-second path. Rejected: once the handshake is off the
loop, pool depth stops being a *safety* signal and becomes a performance
curiosity, and the question it answered is closed. If it is ever needed again it
belongs in the §11 manual instrument, fully wrapped in the style of
`queue_depths()`, not in the hot path.

**Per-success telemetry.** ~2,300 refreshes a session; a success line would be
~2,300 Tier 2 rows/day for information `LoopHeartbeat` already carries. The
existing design emits only on failure and on recovery. Unchanged.

---

## 7. Design question 5 — scope

**Recommendation: narrow. Only `AlertStreamerAccount`'s refresher moves.**

Everything else keeps calling `BrokerAccount.fetch_positions()` on the loop, unchanged:

| Caller | Frequency | Moves? |
|---|---|---|
| `AlertStreamerAccount._refresh_loop` | **every 10s, ~2,300/session** | **Yes** |
| `reconcile.py:835` (`Reconciler`) | startup + day rollover — a handful/day | No |
| `session.py:839` (initial seed) | once per session day | No |
| `watchdog._cache_is_over_reporting` (via `session.py:989`) | once per distinct disagreement shape, per `book_confirm_ttl_s` | No |
| `tools/account.py`, `tools/order_smoke.py` | manual | No |

Reasoning:

1. **Frequency is the whole problem.** The refresh is ~99% of REST call volume
   and the only fixed-cadence caller. The others are rare by design — the
   watchdog's confirm was made rare *on purpose* by the 2026-08-21 per-shape
   cache. Moving them buys almost no loop time.
2. **The §4.3 sizing argument depends on narrow scope.** `max_workers=1` is
   sound because the refresh loop is the single sequential caller. The watchdog's
   confirm can fire *while* a refresh is in flight, so admitting it would require
   `max_workers=2` and a fresh derivation of everything in §4 — for a caller that
   runs a few times a day.
3. **Moving the watchdog's confirm would not fix what is wrong with it anyway.**
   That await is a documented *blind window* because `check_once` awaits it — the
   loop is not blocked there, it is *waiting*. A thread does not shorten the wait;
   it only removes ~40ms of handshake CPU. Real, second-order, and not what
   generates breach volume.

**Follow-up, recorded not scheduled:** if the watchdog's confirm is ever moved,
it needs its own decision — executor sizing, the interaction with the in-flight
guard, and whether it shares the refresher's client at all.

---

## 8. The 2026-07-21 anyio hazard — how it is avoided

The constraint: the SDK yields from inside an anyio `create_task_group()`, and an
anyio cancel scope must be exited by the task that entered it. Moving broker I/O
across a thread/loop boundary is the same family.

**In the recommended shape the hazard is absent, not managed:**

| Thing | Where it lives | Ever crosses the boundary? |
|---|---|---|
| `tastytrade.Session` (anyio `Lock`, `AsyncContextManagerMixin`) | main event loop only | **No** |
| `Session._client` (`httpx2.AsyncClient`, anyio transport) | main event loop only | **No** |
| `AlertStreamer` (anyio task group) | its own owner task, unchanged | **No** |
| `session.session_token` | read on the loop, passed as an argument | **Yes — a `str`** |
| `httpx2.Client` (sync; blocking sockets, `threading.Lock`) | worker thread only | **No** |

There is no second event loop, no `run_coroutine_threadsafe`, no
`wrap_future`, no cancellation scheduled into another loop, and no anyio object
constructed in one context and used in another. The only value crossing is an
immutable string.

The sync `httpx2.Client` uses `httpcore`'s **sync** backend — plain
`socket`/`ssl` with `threading` primitives. anyio is not on that code path at all.

`AlertStreamerAccount`'s existing ownership rule ("ONE task holds the streamer
via `async with`", `stream.py:348-359`) is untouched. The refresh task remains a
sibling in `self._tasks`, deliberately outside the runner so it survives a
reconnect — unchanged.

---

## 9. The OAuth constraint — one session, one login

**The thread caches the CLIENT; it never caches the TOKEN.**

```python
async def fetch_snapshot(self):
    await self._session.refresh()               # SDK's own no-op unless <60s of life left
    token = self._session.session_token         # read AFTER, on the loop
    return await loop.run_in_executor(self._executor, self._fetch_sync, token)
```

Why this closes the "silently 401s forever" hazard the prompt names:

- **Argus mints tokens in exactly one place, on the loop, through the one
  `Session`.** `Session.refresh()` (SDK `session.py:341`) is guarded by the SDK's
  own `anyio.Lock` and its own 60s expiry buffer, so this is a real network call
  roughly **once every 14 minutes** (`expires_in` defaults to 900s) and a
  comparison the rest of the time. Against today's ~12 handshakes/minute, that is
  a rounding error left on the loop, on a warm pooled connection.
- **The token cannot be stale by construction.** It is read *after* a refresh
  that guarantees ≥60s of remaining life, and the dispatched work is bounded at
  5s. There is no window in which the thread can present an expired token.
- **A server-side revocation still heals.** The thread gets a 401, the SDK's own
  `validate_response` produces `TastytradeError("token_invalid: …")`,
  `is_auth_error` returns True, `_refresh_loop` calls `_try_reauth` →
  `tt.refresh(force=True)`, and the *next* dispatch reads the new token. Existing
  machinery, unchanged, and it works precisely because the token is re-read on
  every dispatch instead of captured at construction.
- **The thread has no second login path.** It holds no refresh token, no provider
  secret, and never touches `/oauth/token`. Guarded by a test asserting the sync
  client never issues a request to that path (§10.6).
- **A failed mint fails the attempt before dispatching**, which is recorded and
  handled by the existing failure ladder.

**One extra connection pool is not one extra session.** Connections are not
logins; tastytrade's ban threshold counts failed OAuth refreshes, and this design
does not add any. Additional TCP connections to cert in steady state: **one**.

---

## 10. Test plan

Extending [`tests/test_account_liveness.py`](../../tests/test_account_liveness.py)
(24 tests today) plus one new file for the refresher. No forked file.

The sync client is driven by **`httpx2.MockTransport`** throughout: no network,
fully deterministic, and it exercises the *real* `validate_and_parse` and the
*real* pydantic models, so the error-fidelity assertions are meaningful rather
than mocked.

### 10.1 Executor isolation — the highest-value test, with teeth

The prompt names this the most valuable test in the change. A single positive
assertion would be a tautology (nothing proves the harness could ever have
failed), so it is a **pair**:

```
test_a_wedged_refresh_cannot_delay_an_audit_write
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))   # deterministic
    wedge one refresh on a threading.Event the test never sets
    await asyncio.wait_for(audit.record("test", {}), timeout=1.0)  # must PASS
    finally: release the event, join

test_the_isolation_test_has_teeth                                  # the negative
    same setup, but the refresher is constructed to use the DEFAULT executor
    await asyncio.wait_for(audit.record(...), timeout=1.0)         # must RAISE TimeoutError
    finally: release the event, join
```

The second test is what makes the first one a regression guard rather than a
decoration. Both release the wedge in a `finally` so no worker thread outlives
the test.

Plus a cheap structural assertion: the refresher's dispatch target is its own
executor and is not `loop._default_executor`.

### 10.2 Fail-closed contract preserved

Existing tests adapted to `fetch_snapshot`, unchanged in intent:

- a failed refresh leaves the cache **untouched** and lets `age()` keep growing
- a failed refresh **never evicts** (`test_failed_refresh_never_evicts`)
- `refresh_now()` returns `False` and increments `consecutive_refresh_failures`
- the backoff ladder (1/2/4/8, capped at the interval) and its reset on recovery

### 10.3 Error fidelity — table-driven, against real parsing

One parametrized test over `MockTransport` responses, asserting the emitted
`account.refresh_failed` payload for each row of §6.1:

| Transport behaviour | Asserted `error_type` | `auth_error` | `error_class` |
|---|---|---|---|
| raises `httpx2.ReadTimeout` | `TimeoutError` | `False` | `timeout` |
| 502 with a cert-shaped error body | `TastytradeError` | `False` | `http` |
| 401 with `token_invalid` body | `TastytradeError` | `True` | `auth` |
| raises `httpx2.ConnectError` | `ConnectError` | `False` | `transport` |

Plus: `blocking_entries` still flips on `age_s > stale_after_s`, and a
`token_invalid` still drives `_try_reauth` while a 502 still does not
(existing tests, retained).

### 10.4 The budget

- total wall time of a failing attempt ≤ `budget + ε`
- the second leg is **not issued** when remaining budget < `_MIN_REQUEST_BUDGET_S`
  (assert the MockTransport request log has one entry, not two)
- positions is requested **before** balances
- both requests go over **one** connection in the happy path

### 10.5 Token freshness — the "silently 401s forever" guard

- `session.refresh()` is awaited before **every** dispatch
- after the session's `session_token` changes mid-run, the **next** request
  carries the new bearer value (asserted off the MockTransport's captured headers)
- a raising `session.refresh()` fails the attempt **without dispatching**

### 10.6 No second login

The MockTransport asserts on path: any request to `/oauth/token` from the sync
client fails the test outright.

### 10.7 SDK drift guard

Construct a real `tastytrade.Session` offline (its `__init__` does no I/O) for
both `is_test=True` and `is_test=False`, and assert the refresher's `base_url`
and non-auth headers match it exactly — including the `Accept-Version`-only-when-
not-test rule. This is what catches an SDK upgrade moving the endpoint or the
header set.

### 10.8 In-flight guard and backstop

- cancelling a `refresh_now()` mid-dispatch, then calling again, fails fast with
  `RefreshBusyError` and does **not** submit a second job
- a port that hangs past `refresh_interval_s` raises `RefreshBackstopError`,
  which lands in `error_type` and is **not** classified as an auth error

### 10.9 Loop-lag, without TLS — the deterministic version

The property under test is "the blocking work does not stall the loop," and that
is provable without a certificate: a `MockTransport` handler that calls
`time.sleep(0.2)` inside a real `LoopHeartbeat` at production settings
(`interval_s=0.1`, `lag_alert_threshold_s=0.05`).

- **before shape** (on-loop equivalent): `max_lag` ≥ 0.2s, breaches recorded
- **after shape** (threaded): `max_lag` stays well under the 50ms threshold,
  zero breaches

Fast, hermetic, no network, and it fails if anyone ever puts the work back on the
loop.

### 10.10 Full suite, the AST guards, and the leak guard

`./.venv/bin/python -m pytest`. The two **AST** guards —
`test_session_trading_side.py` (the trading side is built inside a conditional)
and `test_order_build.py` (no per-leg orders, no Complex Order API anywhere in
`src/`) — must stay green. Neither touches this path.

`test_no_leaks.py` is **not** an AST guard (the task prompt groups it as one); it
is the standing resource-property guard written after the 2026-08-10 lag ramp,
and it is the one of the three that is actually *relevant here*. Its docstring
says the warm-up → hammer → count → hammer → count pattern "generalizes to any new
hot path", and this change adds one: ~2,300 dispatches a session through a new
client, a new executor, and a new exception path. **A refresher case belongs in
that file**, hammering `fetch_snapshot` against a `MockTransport` and asserting
bounded object growth across the second window. Skipping it would leave the
highest-frequency new code path in the process as the only hot path without a leak
guard — in a file that exists precisely because a slow accumulator on this exact
10-second event went undetected for eight days.

---

## 11. Acceptance criterion — the real one, and where it lives

§10.9 proves the work is off the loop. It does **not** produce the production
numbers, because a `MockTransport` has no certificate chain and no WAN RTT.

Proposal: **`src/argus/tools/loop_lag.py`**, a manual instrument in the same
class as `tools/evaluability.py` and the other smoke tools — the repo's existing
convention for "measurements that cannot be unit tests."

- runs a real `LoopHeartbeat` at production settings
- drives N refreshes through the on-loop and off-loop shapes against real TLS
- default target is a local TLS stub (reproducing the 2026-08-25 harness);
  `--live-cert` points it at the cert API using the read-only `GET` pair
- prints p50 / p99 / max lag per shape, plus handshake count
- `--config` is **required**, per CLAUDE.md — and it opens no database at all

**The production acceptance question is not "is the pool warm."** It is:

> Across one full session, did `LoopHeartbeat.breach_count` fall **while**
> `account.refresh_failed` volume stayed comparable?

That is the shape that separates "the cost moved off the loop" from "the cert
happened to be healthy that day" — and, per the 2026-08-10 lag-ramp record's own
warning, it must be read against `would_submit today > 0` and the blackout file,
and against process-lifetime rather than session-lifetime heartbeat counters.

---

## 12. Change surface

**No new config key. The image deploys alone** — unlike
`watchdog.exit_unattempted_max_s` and `watchdog.book_confirm_ttl_s`, this needs
no coupled config deploy. Every number is either existing config
(`account_refresh_timeout_s`, `account_refresh_s`, `broker.http_timeout_s`) or a
module constant with the same justification `KEEPALIVE_REFRESH_MULTIPLE` carries:
it tunes transport, not trading, so it stays out of the fail-closed loader.

**New**

- `src/argus/account/refresher.py` — `ThreadedAccountRefresher`,
  `RefreshBusyError`, `_MIN_REQUEST_BUDGET_S` (~120 lines)
- `src/argus/tools/loop_lag.py` — manual instrument (§11)
- tests: `tests/test_account_refresher.py`; extensions to
  `tests/test_account_liveness.py` and a refresher case in `tests/test_no_leaks.py`

**Changed**

- `account/port.py` — `AccountRefreshPort` → single `fetch_snapshot()`, with the
  timeout-ownership contract in the docstring
- `account/stream.py` — `refresh_now()` loses `gather` and the 5s wrapper, gains
  the `refresh_interval_s` backstop and `elapsed_s`; `_note_refresh_failure`
  gains `leg`/`elapsed_s`/`error_class`; `refresh_timeout_s` parameter removed;
  `RefreshBackstopError` defined here
- `account/fake.py` — `FakeBrokerAccount.fetch_snapshot`
- `session.py` — one construction site (`refresher=ThreadedAccountRefresher(...)`
  at line 825) plus one `await refresher.aclose()` in the `finally` block at
  line 1175, positioned per §4.4

**Untouched, by constraint**

- `broker/session.py` — `keepalive_expiry`, `Limits`, pool bounds. The new client
  *imports* `KEEPALIVE_REFRESH_MULTIPLE`; it changes nothing there.
- `_retry_delay()` — the backoff is not floored (analysed and rejected 2026-08-25:
  it trades loop lag for blocked entries, and 23% of failures already carried
  `blocking_entries: true`)
- `BrokerAccountPort`, `reconcile.py`, `watchdog.py`, all `tools/*`

---

## 13. Residual risks, stated rather than buried

1. **`socket.getaddrinfo` is not bounded by httpx.** A pathological resolver can
   hold the single worker for tens of seconds. Consequence: refreshes stop,
   staleness grows, entries block — fail-closed, loudly, with no effect on the
   audit log, the watchdog, `/status`, or `/kill`. **Strictly better than today**,
   where the same call occupies a slot in the audit log's own pool (§1.1).
2. **Two URL literals can drift from the SDK.** Guarded by §10.7 and by the fact
   that a moved endpoint 404s on the first refresh.
3. **A bound-less port implementation is prevented by contract and test, not by
   the type system.** The `refresh_interval_s` backstop makes it loud rather than
   silent. If Richard wants this structural instead, the alternative is a
   `self_bounded` marker on the Protocol — more ceremony, same guarantee.
4. **Exit latency** can grow by up to the refresh budget if a thread is wedged at
   shutdown. Headroom exists (§4.4); the daemon-thread fallback is named.
5. **This has no cert coverage worth the name.** Sandbox proves API contracts and
   wiring, never fill behaviour or realistic latency — and the specific thing
   being fixed is a *latency* property. The droplet's own before/after over a full
   session (§11) is the only real evidence, and it should be read after one
   session, not one hour.

---

## 14. Sign-off checklist — APPROVED 2026-08-26, no amendments

Six decisions. **All approved as written.** Each is now a constraint on the
implementation, not a proposal: changing any of them means coming back to this
document, not deciding it at the keyboard.

1. **Shape (a)** — sync `httpx2.Client` on a worker thread, SDK parsing reused,
   (b) and (c) rejected per §3.3/§3.4.
2. **`account_refresh_timeout_s` becomes a TOTAL budget for a serialized pair**,
   with a deadline split and a 0.25s floor. The watchdog's use of the key is
   semantically unchanged. §5.1–5.2.
3. **A dedicated `ThreadPoolExecutor(max_workers=1)`**, never the default. §4.
4. **`AccountRefreshPort` collapses to one `fetch_snapshot()`**, implementations
   own their bound, `refresh_now`'s wrapper becomes a backstop at
   `refresh_interval_s`. §5.3, §5.5.
5. **Narrow scope** — only the liveness refresh moves. §7.
6. **Telemetry gains `leg` / `elapsed_s` / `error_class`**; every existing
   `error_type` string is preserved, including the timeout normalization. §6.

**Working arrangement (Richard, 2026-08-26):** work on `main`, **do not commit**,
**do not push**. The working tree is the review surface; Richard commits if
approved. Note this leaves the change interleaved with the v0.3.10 prep already
uncommitted on `main` — that is accepted, not overlooked.

---

## 15. Implementation order

Six steps. The ordering is chosen so that **every step before the live path is
touched is independently verifiable**, and so the one breaking change lands
once, in the open, rather than in pieces.

| # | Build | Gate before moving on |
|---|---|---|
| 1 | `account/refresher.py` — `ThreadedAccountRefresher`, `RefreshBusyError`, `_MIN_REQUEST_BUDGET_S`. Executor is a **constructor argument** (see below). | `tests/test_account_refresher.py` covering §10.3–10.8. Hermetic, `MockTransport`, no live path touched. |
| 2 | The executor-isolation pair (§10.1) and the `test_no_leaks.py` refresher case (§10.10). | Both green, **and the negative test genuinely fails** when pointed at the default executor. A teeth test that cannot fail is decoration. |
| 3 | `account/port.py` (`fetch_snapshot`), `account/stream.py` (drop `gather` + wrapper, add backstop, `refresh_timeout_s` removed, telemetry fields), `account/fake.py`. Adapt the existing liveness tests. | **Full suite green.** This is the breaking step; nothing else should be in flight while it lands. |
| 4 | `session.py` — the construction site at 825 and the `aclose()` at 1175 (§4.4). | Full suite green, plus §10.9's deterministic loop-lag test. |
| 5 | `tools/loop_lag.py` (§11). | Runs against the local TLS stub and reproduces the 2026-08-25 shape of the result. |
| 6 | Decision record in `docs/decisions/` + its row in `docs/decisions/README.md` + this file marked shipped. | — |

**Why the executor is a constructor argument.** §10.1's negative test has to be
able to build a refresher that uses the *default* executor, or it cannot
demonstrate the starvation it exists to rule out. That affordance is test-only;
`session.py` never passes it. Worth stating because "just hardcode the executor
inside the class" is the obvious simplification and it silently removes the
teeth from the highest-value test in the change.

**What cannot be verified locally at all.** The production before/after (§11).
Read it after **one full session**, not one hour, and against
`would_submit today > 0` and the blackout file — per the 2026-08-10 lag-ramp
record's own two traps. Until that reading exists, the decision record in step 6
says the fix is *deployed*, not that it is *proven*.

### 15.1 Five ways this drifts from the approved design

Each of these is a plausible-looking local improvement that silently undoes a
decision in §14. None may be taken without coming back here.

1. **Do not add a config key.** Every number derives from existing config or is a
   module constant justified like `KEEPALIVE_REFRESH_MULTIPLE`. A new REQUIRED
   key would couple this to a config deploy, which §12 specifically avoids.
2. **Do not touch `broker/session.py`.** Import `KEEPALIVE_REFRESH_MULTIPLE`;
   change nothing there. The 2026-08-10 fix is intact and out of scope.
3. **Do not re-add `asyncio.timeout` around the executor dispatch.** It cannot
   bound thread work, and firing it abandons a running thread. The backstop lives
   in `refresh_now` at `refresh_interval_s` and nowhere else (§5.3).
4. **Do not give `BrokerAccount` a `fetch_snapshot`.** It has no bound of its own,
   so it must not structurally satisfy a Protocol whose contract is "bound
   yourself" (§5.5). Adding it re-opens the accident the Protocol change closes.
5. **Do not floor the retry backoff** while in here. Analysed and rejected
   2026-08-25: it trades loop lag for blocked entries, and 23% of failures already
   carried `blocking_entries: true`.
