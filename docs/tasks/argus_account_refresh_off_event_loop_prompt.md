# Argus: Move the Account Liveness Refresh Off the Event Loop

## Priority

**DESIGN FIRST — do not implement without Richard's sign-off on the design.**
This touches broker I/O on an always-on trading process, and two of the three
plausible implementations carry hazards that have already cost this system a
production incident (2026-07-21) or are explicitly flagged as ban risk in
`CLAUDE.md`. The problem is understood and measured; the *implementation shape*
is the open question. Target v0.3.10 or later.

## The problem, measured

TLS handshakes performed on the event loop stall it long enough to breach the
50ms heartbeat threshold. Measured locally 2026-08-25, real TLS, real
`argus.heartbeat.LoopHeartbeat`, two concurrent requests per refresh (exactly
what `refresh_now()` does):

| | p50 | p99 | max |
|---|---|---|---|
| handshakes ON the event loop (today) | 1.077ms | **39.587ms** | **49.573ms** |
| same work in a worker thread (`asyncio.to_thread`) | 1.041ms | **1.110ms** | **1.129ms** |

**35x reduction at p99.** Those ON-loop figures are from a fast Mac against
localhost — no WAN RTT, a single-cert chain. The droplet's vCPU is roughly 4x
slower with a real certificate chain and real network latency, which puts it
well past 50ms and into the observed 50–250ms band.

**Why this matters now.** `refresh_now()` wraps its two concurrent requests in
`asyncio.timeout(5.0)`. When that fires it cancels them mid-flight, and that
**destroys both pooled connections** — measured: pool goes 2 idle → 0, and stays
at 0 for the duration of a failure storm, so every subsequent refresh pays a
full TCP+TLS setup. Confirmed in production on 2026-08-25: breaches correlate
with refresh failures at **r = 0.695**, 82% of minutes within ±1, near 1:1
(121 breaches / 138 failures over a 40-minute window).

**Important scoping fact: only TIMEOUTS destroy the pool. 502s do not** —
measured: after two 502 responses the pool was still 2 connections / 2 idle,
because a 502 is a complete HTTP response and the connection is reusable. On
2026-08-24 the split was 420 `TimeoutError` vs 117 502s.

**This is not a regression.** `broker/session.py` and `account/stream.py` are
byte-identical across v0.3.5→v0.3.8. The 2026-08-10 `keepalive_expiry` fix
(`docs/decisions/2026-08-10-intra-session-lag-ramp.md`, **RESOLVED** — read it
in full) is intact and still correct; it simply only ever protected the SUCCESS
path. Nobody tested what a failure storm does to the pool. The trigger is
external cert instability (refresh failures jumped ~5x on 2026-08-24: 62–124 per
session → 537, then 604).

## The seam

`argus/account/port.py` already defines the Protocol this plugs into:

```python
@runtime_checkable
class AccountRefreshPort(Protocol):
    """What the liveness refresher needs: a full authoritative re-read."""
    async def fetch_positions(self) -> list[Position]: ...
    async def fetch_balances(self) -> Balances: ...
```

`account/rest.py`'s `BrokerAccount` is today's implementation (thin: calls
`self._account.get_positions(self._session)` and maps to domain types).
`AccountStateCache` holds it as `self._refresher` and calls it from
`refresh_now()`. A new implementation of this Protocol is the natural shape —
**confirm that read against the code before committing to it.**

## Design questions to answer — this is the actual work

### 1. Which implementation shape, and what does it cost?

Three candidates. Evaluate all three; recommend one with reasoning. None is
obviously right.

- **(a) Sync `httpx2.Client` in a worker thread, reusing the existing Session's
  bearer token.** Avoids a second OAuth login entirely. Cost: bypasses the SDK's
  `get_positions`/`get_balances`, so the request construction and response
  parsing would be reimplemented against `map_position`/`map_balances`. Work out
  how the token is obtained from the existing `Session` and — critically — how a
  token REFRESH mid-session is observed by the thread-side client. Getting this
  wrong means the refresher silently 401s forever.
- **(b) Dedicated worker thread running its own event loop + its own
  `AsyncClient`.** Keeps the SDK path. But `httpx.AsyncClient` is **not safe to
  share across event loops**, so the thread needs its own client, which likely
  means its own `Session` — see the ban-risk constraint below.
- **(c) Something else you find while reading.** If the SDK exposes a sync path,
  or if there is a way to keep one client and only move the handshake, say so.

### 2. Timeout semantics — `asyncio.to_thread` CANNOT be cancelled

This is the sharpest constraint and it changes the whole error path. Today's
`async with asyncio.timeout(5.0)` cancels the in-flight requests. A thread cannot
be cancelled: cancelling the awaiting task abandons the *result* while the thread
keeps running to completion. So:

- The timeout must move ONTO the sync client (per-request), not stay as an
  `asyncio.timeout` wrapper — otherwise a hung request leaks a thread that never
  ends.
- Work out what `staleness.account_refresh_timeout_s` (5.0) means in the new
  shape, per request vs total, and whether serializing the two calls (rather than
  `gather`) changes the budget. **Note the existing constraint**: that same key is
  also the watchdog's book-confirm bound since 2026-08-21
  (`docs/decisions/2026-08-21-book-confirm-timeout-bound.md`), so changing its
  meaning has a second consumer.

### 3. ⚠️ Executor isolation — a correctness hazard, not a nicety

`audit/log.py` dispatches **every** audit write through `asyncio.to_thread`
(both Tier 1 and Tier 2). `asyncio.to_thread` uses the loop's DEFAULT
`ThreadPoolExecutor`. If refresh threads hang (which is precisely the failure
mode being fixed — cert timing out) and accumulate, they can **starve the audit
log's writes**. A failed audit write latches `AuditLog.failed`, engages the kill
switch, and halts trading (`CLAUDE.md`, non-negotiables).

**So: a dedicated, bounded executor for the refresh, never the shared default.**
Size it deliberately and say why. Confirm this reading of `audit/log.py` yourself
rather than trusting this prompt.

### 4. Error fidelity

`_note_refresh_failure` branches on `is_auth_error(exc)` and records
`error_type`, `auth_error`, and `blocking_entries` into Tier 2 telemetry that is
now load-bearing diagnostics (it is how this whole investigation was resolved).
Whatever shape you choose must preserve those distinctions — including the
timeout-vs-502 split, which the measurement above shows is the difference
between "pool destroyed" and "pool intact." Losing that distinction would blind
the next investigation.

### 5. Scope — which callers move?

`fetch_positions()` has other callers: reconciliation, and the watchdog's
book-confirm (`_cache_is_over_reporting`, awaited INSIDE `check_once`, which is
a documented blind window). Decide and justify whether this change is scoped to
the liveness refresh only, or extends. **Recommendation to evaluate, not
assume:** scope it narrowly first — the liveness refresh is the one on a fixed
10s cadence and therefore the one generating breach volume.

## Constraints

- 🚨 **Do not create a second OAuth session/login.** `CLAUDE.md`: *"each failed
  OAuth refresh accrues toward tastytrade's undocumented IP-block threshold."*
  The endpoint in question is already unstable; doubling login pressure on it is
  not acceptable. If your chosen shape needs a second client, it must still use
  ONE authenticated session.
- 🚨 **The 2026-07-21 hazard class.** See `marketdata/dxlink.py`'s module
  docstring: the SDK yields from inside an anyio `create_task_group()`, and an
  anyio cancel scope must be exited by the task that entered it — *"works right up
  until the socket actually fails, at which point anyio unwinds from the wrong
  task"* and takes the process down. Moving broker I/O across thread/loop
  boundaries is the same family of bug. Any design crossing that boundary must
  state explicitly how it avoids this.
- **Do not change `keepalive_expiry` or the pool bounds** in `broker/session.py`.
  That fix is intact, correct, and derived from `account_refresh_s` on purpose.
- **Do not floor the retry backoff** as a substitute for this fix. Analyzed and
  rejected 2026-08-25: `_retry_delay()` backing off from 1s exists to protect the
  30s staleness budget, and flooring it at `refresh_interval_s` roughly halves how
  many consecutive failures can be tolerated before `account_stream_max_age_s`
  blocks entries — trading loop lag for blocked entries. (23% of failures already
  carried `blocking_entries: true` on 2026-08-25.)
- **`refresh_now()`'s fail-closed contract is unchanged**: on failure the cache is
  left UNTOUCHED so staleness keeps growing and the gate fails closed. Preserve it.
- Never touch `argus.db`; all dev/test runs use `argus.dev.toml`.

## Testing

- Extend the existing account-refresh tests rather than forking a file.
- **A regression test for the executor isolation** (design question 3): prove that
  hung refresh work cannot starve audit writes. This is the highest-value test in
  the change — it guards a path that halts trading.
- Preserve the existing fail-closed tests: a failed refresh must still leave the
  cache untouched and let staleness grow.
- Error-fidelity tests: timeout, 502, and auth failure must each still produce the
  correct `error_type` / `auth_error` / `blocking_entries` telemetry.
- A local before/after loop-lag measurement reproducing the harness above
  (real `LoopHeartbeat`, real TLS) is the acceptance criterion for the fix
  actually working. The scratch harness from 2026-08-25 is a starting point;
  rebuild it as a proper test if it earns a permanent home.
- Full suite: `./.venv/bin/python -m pytest`. Confirm the AST guards
  (`test_no_leaks.py`, `test_session_trading_side.py`, `test_order_build.py`)
  stay green.

## Deferred / explicitly out of scope

- Serializing positions/balances instead of `gather` (measured: halves handshakes
  12→6). Worth doing, but it interacts with the timeout budget (a slow first call
  starves the second) and should be decided **as part of** design question 2
  rather than shipped separately.
- Error-type-aware backoff (longer for timeouts, fast for 502s). A good idea on
  its own evidence, but it becomes largely unnecessary if the handshake cost is
  off the loop. Revisit after.
- Anything in `broker/session.py`'s keepalive/pool configuration.
- The cert instability itself — external, unfixable here.

## Deliverable

**A written design first**, covering: the chosen shape and why the other two were
rejected, how the token is obtained and kept fresh without a second login, the
timeout model, the executor isolation decision and its sizing, and how the anyio
boundary hazard is avoided. Get Richard's sign-off on that before writing code.

Then the implementation, its tests, and a decision record in `docs/decisions/`
capturing the measurement above and the design reasoning — this is a safety-
adjacent path and the *why* needs to outlive the diff. Add the row to
`docs/decisions/README.md`.

Confirm with Richard whether to work on a branch or directly on `main`, and
whether to commit — no standing convention is recorded for this repo either way;
do not assume. Do not push regardless, unless explicitly told to.
