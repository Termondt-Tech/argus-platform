"""The account liveness refresh, moved off the event loop.

WHY THIS MODULE EXISTS. `AlertStreamerAccount._refresh_loop` fires every 10s and
does two HTTPS GETs against the broker. When the connection pool is cold — its
PERMANENT state during a refresh-failure storm, because an `asyncio.timeout`
cancellation destroys the connection it fired on — each of those pays a full
TCP+TLS handshake **on the event loop**, and the handshake's CPU cost lands at
the same order of magnitude as the 50ms heartbeat breach threshold. Measured
against a local TLS stub: 39.6ms p99 on-loop versus 1.11ms in a worker thread.

**This does not save the connection pool, and no version of it could.** A httpx
timeout closes the connection it fired on either way; the pool still empties
during a storm and every refresh is still cold. What changes is that a cold
refresh stops being the EVENT LOOP's problem. Read the acceptance criterion
accordingly: "did `LoopHeartbeat.breach_count` fall while `account.refresh_failed`
volume stayed the same?", never "did the pool stay warm?".
→ docs/designs/2026-08-25-account-refresh-off-event-loop.md

Three properties hold this together, and each one is load-bearing:

1. **Nothing anyio-shaped crosses the thread boundary.** The `tastytrade.Session`
   (an `anyio.Lock`, an `AsyncContextManagerMixin`) and its `AsyncClient` stay on
   the main loop; a `str` bearer token is the only value that crosses. The sync
   `httpx2.Client` is `httpcore`'s SYNC backend — plain sockets and `threading`
   primitives, anyio absent from the code path entirely. That is the 2026-07-21
   hazard class AVOIDED rather than managed.
2. **One session, one login.** The thread holds no refresh token, no provider
   secret, and no code path to `/oauth/token`. Tokens are minted in exactly one
   place — `Session.refresh()`, on the loop — and re-read on EVERY dispatch, so
   a server-side revocation heals through the existing `_try_reauth` ladder
   instead of 401ing forever against a token captured at construction.
3. **The two calls serialize**, against one monotonic deadline. That halves cold
   handshakes 2 -> 1 as a property of the shape, and it is what lets
   `staleness.account_refresh_timeout_s` stay a TOTAL per-attempt budget — the
   same reading the watchdog's `_cache_is_over_reporting` already gives it.
   **The deadline is OURS and is enforced between legs; within one request it is
   approximated**, because httpx offers per-phase ceilings and no total. A scalar
   `timeout=` sets all four phases independently and let one leg spend the budget
   twice over — see `_CONNECT_BUDGET_S` for what that cost on 2026-08-26 and how
   the phases are bounded now. Worst case per leg is `remaining + 1.5s`.

The SDK's `Account.get_positions`/`get_balances` are not reachable from here (both
are coroutines on the async `Session`), so this reimplements the two URLs and
NOTHING else: `validate_and_parse`, `CurrentPosition`, `AccountBalance`,
`map_position` and `map_balances` are the SDK's and ours verbatim, so
`TastytradeError` messages are byte-identical to what the async path produced.
Two URL literals is the entire cost, and `tests/test_account_refresher.py` guards
them against SDK drift.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from typing import Any

import httpx2
from tastytrade import API_URL, API_VERSION, CERT_URL
from tastytrade.account import AccountBalance, CurrentPosition
from tastytrade.utils import TastytradeError, validate_and_parse

from argus.account.models import Balances, Position, map_balances, map_position
from argus.account.port import tag_error
from argus.broker.session import KEEPALIVE_REFRESH_MULTIPLE

logger = logging.getLogger("argus.account.refresher")

#: Below this much remaining budget the second leg is not issued at all.
#:
#: A request certain to time out in milliseconds costs a connection and a socket
#: for no information — and, during a storm, destroys the connection the NEXT
#: attempt would have reused. The attempt fails as a timeout either way; this only
#: decides whether it spends a socket on the way there.
#:
#: A module constant rather than a config key, for the reason
#: `KEEPALIVE_REFRESH_MULTIPLE` is one: it tunes transport and nothing about
#: trading, so it stays out of the fail-closed loader and a performance fix stays
#: uncoupled from a config deploy.
_MIN_REQUEST_BUDGET_S = 0.25

#: Pool bounds for the refresher's OWN client. One connection suffices in steady
#: state (both legs reuse it); the second exists for the case where the first
#: leg's connection was destroyed by its own timeout and the second leg still has
#: budget left. Deliberately NOT the SDK's 100/20 — this client serves exactly
#: one caller making exactly two sequential requests.
_POOL_MAX_CONNECTIONS = 2
_POOL_MAX_KEEPALIVE_CONNECTIONS = 2

#: The currency the SDK's `get_balances` defaults to. One value, one endpoint.
_BALANCES_CURRENCY = "USD"

#: Ceilings for the connection-establishment phases of ONE request. READ is NOT
#: among them — it keeps the leg's whole remaining budget.
#:
#: **httpx's scalar `timeout=X` is not a total.** It sets connect, read, write and
#: pool to X INDEPENDENTLY, so `timeout=remaining` let a single leg spend
#: `remaining` connecting and `remaining` AGAIN reading. That is how a module whose
#: own docstring promises "one monotonic deadline" produced attempts that outran
#: `refresh_now`'s 10s backstop five times on 2026-08-26 — and the backstop firing
#: is defined right there as meaning the bug is in the port. It was.
#:
#: httpx cannot express "these phases share a pool of seconds", so the deadline is
#: made real the only way the library allows: READ keeps the full remaining budget,
#: because it is the phase the budget exists for (the server thinking), and the
#: other three get small fixed ceilings on top. Worst case per leg is therefore
#: `remaining + 1.5s` rather than `4 x remaining` — comfortably inside the backstop
#: at any sane `account_refresh_timeout_s`.
#:
#: **Sized from evidence, not taste.** Across the six-hour cert outage of
#: 2026-08-26 (~4 refresh failures/min, 1,464 of them) there was not one
#: ConnectTimeout: every failure was a read timeout or a fast 502. Connect is
#: empirically not where this endpoint spends its time, so bounding it tightly
#: costs nothing and stops it stacking on top of read.
_CONNECT_BUDGET_S = 1.0
#: A GET with no body — request headers only.
_WRITE_BUDGET_S = 0.25
#: Our OWN pool: 2 connections, one caller, one request at a time (the in-flight
#: guard enforces that). A wait here means something is already wrong.
_POOL_BUDGET_S = 0.25


class _Failure:
    """A failure carried back across the thread boundary as a VALUE, not raised.

    This exists for one specific, non-obvious reason. `asyncio` does not hand an
    executor's exception through unchanged: `futures._convert_future_exc` REBUILDS
    three types as `exc_class(*exc.args)`, and one of them is
    `concurrent.futures.TimeoutError` — which, since 3.11, *is* the builtin
    `TimeoutError`. So a `TimeoutError` raised in the worker arrives on the loop as
    a DIFFERENT OBJECT with no `__cause__` and none of its attributes.

    That collides head-on with the error model: the timeout is normalized to
    builtin `TimeoutError` precisely so `error_type` keeps matching existing audit
    queries, and it carries `leg` and `error_class` as attributes. Raised across
    the boundary, the most common failure mode in a storm would silently lose two
    of its three telemetry fields and the httpx class in `__cause__`.

    Returning it sidesteps the converter entirely; `fetch_snapshot` re-raises the
    original object on the loop, traceback intact.
    """

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class RefreshBusyError(RuntimeError):
    """A prior dispatch is still running; this attempt was refused, not queued.

    `asyncio.to_thread` / `run_in_executor` CANNOT be cancelled: cancelling the
    awaiting task calls `concurrent.futures.Future.cancel()`, which returns False
    for work already running and does not interrupt it. The coroutine raises
    `CancelledError`; the thread runs to completion, unobserved — and with a
    single worker the next dispatch would queue silently behind it.

    So the refresher refuses instead. Same discipline as `DuplicateOrderGuard`,
    and the same rule the 2026-08-20/21 passes settled: **bound the silence, never
    the retry.** This cancels nothing and retries nothing; it converts an
    invisible wedge into a loud, fast-failing, self-healing refusal that ages the
    cache into `blocking_entries` — an existing rail, already alerted, already on
    `/status`. Failing into an existing rail beats failing into a new one.
    """

    #: Must stay spelled the same as `port.ERROR_CLASS_ATTR` — a class attribute
    #: cannot reference it, so a test asserts the two agree.
    argus_error_class = "busy"


def build_client(
    session: Any,
    *,
    http_timeout_s: float,
    keepalive_expiry_s: float,
    transport: httpx2.BaseTransport | None = None,
    base_url: str | None = None,
    verify: Any = None,
) -> httpx2.Client:
    """The sync client, built from PUBLIC SDK surface only — never `session._client`.

    Mirrors `tastytrade.Session.__init__` exactly, including the rule that
    `Accept-Version` is not accepted in sandbox. `tests/test_account_refresher.py`
    diffs this against a real `Session` for both `is_test` values, so an SDK
    release that moves the endpoint or changes the header set fails a test rather
    than a session.

    **No `Authorization` header here.** It is set per request from the token read
    at dispatch time — see `ThreadedAccountRefresher.fetch_snapshot`. Caching the
    client while never caching the token is the whole trick.

    `transport`, `base_url` and `verify` are TEST/MEASUREMENT seams and nothing
    else: `MockTransport` for the hermetic suite, and a local TLS stub with its
    own self-signed certificate for `tools/loop_lag.py`, which is the only way to
    measure handshake cost against real TLS without pointing at cert. `session.py`
    passes none of the three, so the live path always derives the endpoint from
    `session.is_test` — the sandbox-first guarantee is untouched.
    """
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if not session.is_test:  # not accepted in sandbox — the SDK's own rule
        headers["Accept-Version"] = API_VERSION
    extra: dict[str, Any] = {} if verify is None else {"verify": verify}
    return httpx2.Client(
        base_url=base_url or (CERT_URL if session.is_test else API_URL),
        headers=headers,
        **extra,
        # Client-wide DEFAULT only: every request below overrides it with what is
        # left of the attempt's budget. It exists so a request issued by some
        # future code path here can never inherit httpx's silent 5s default.
        timeout=httpx2.Timeout(http_timeout_s),
        limits=httpx2.Limits(
            max_connections=_POOL_MAX_CONNECTIONS,
            max_keepalive_connections=_POOL_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=keepalive_expiry_s,
        ),
        transport=transport,
    )


class ThreadedAccountRefresher:
    """`AccountRefreshPort` over a sync httpx client on a dedicated worker thread.

    Owns its own single-worker executor. **Never the default one**, which is where
    `audit/log.py` dispatches every Tier 1 and Tier 2 write: a failed audit write
    latches, engages the kill switch, and halts trading with no auto-retry, and
    the default executor is 6 workers on the droplet's 2 vCPU shared by Tier 1
    writes, Tier 2 writes, `/status` reads, the Slack POST — and asyncio's own
    `getaddrinfo`. The refresh is the highest-frequency I/O in the process and
    the one whose failure mode is "hangs". It does not belong in that pool.

    One worker, exactly: `_refresh_loop` is strictly sequential and the in-flight
    guard keeps it so even under cancellation, so `max_workers=1` is the shape of
    the workload rather than a constraint on it — and it bounds the worst case for
    this whole mechanism at ONE OS thread, forever.
    """

    def __init__(
        self,
        session: Any,
        account_number: str,
        *,
        budget_s: float,
        http_timeout_s: float,
        refresh_interval_s: float,
        executor: Executor | None = None,
        transport: httpx2.BaseTransport | None = None,
        base_url: str | None = None,
        verify: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._account_number = account_number
        # TOTAL budget for one attempt, spent across the two requests in order
        # against a monotonic deadline — `staleness.account_refresh_timeout_s`.
        self._budget_s = budget_s
        self._clock = clock
        self._client = build_client(
            session,
            http_timeout_s=http_timeout_s,
            # Imported, never restated: `broker/session.py` derives the SDK
            # client's keepalive from the same constant, so the two cannot drift
            # into the 2026-08-06 trap where the pool expired before every refresh.
            keepalive_expiry_s=refresh_interval_s * KEEPALIVE_REFRESH_MULTIPLE,
            transport=transport,
            base_url=base_url,
            verify=verify,
        )
        # A CONSTRUCTOR ARGUMENT because the executor-isolation test needs to build
        # a refresher that uses the DEFAULT executor — without that affordance the
        # negative half of that pair cannot demonstrate the starvation it exists to
        # rule out, and a teeth test that cannot fail is decoration. Test-only:
        # `session.py` never passes it.
        self._owns_executor = executor is None
        self._executor: Executor = executor or ThreadPoolExecutor(
            max_workers=1,
            # Not decoration: this is what lets a `py-spy dump` on the droplet
            # attribute a wedged thread without guessing.
            thread_name_prefix="argus-acct-refresh",
        )
        # The CONCURRENT future, never the asyncio one that wraps it. Cancelling
        # the awaiting task marks the asyncio future done immediately while the
        # thread runs on — guarding on that would be guarding on nothing.
        self._inflight: Future[tuple[list[Position], Balances]] | None = None
        self._inflight_since = 0.0
        self._closed = False

    # -- AccountRefreshPort ------------------------------------------------

    async def fetch_snapshot(self) -> tuple[list[Position], Balances]:
        """One attempt: positions then balances, bounded by this port's own budget.

        The caller does NOT wrap this in `asyncio.timeout` — it cannot bound
        thread work, and firing it abandons a running thread. The bound is the
        deadline in `_fetch_sync`; `refresh_now`'s backstop exists only to make a
        broken implementation loud.
        """
        if self._closed:
            raise RuntimeError("account refresher is closed")
        inflight = self._inflight
        if inflight is not None and not inflight.done():
            raise RefreshBusyError(
                "a prior account refresh has been outstanding for "
                f"{self._clock() - self._inflight_since:.1f}s and cannot be cancelled; "
                "refusing to queue behind it"
            )

        # Minted on the LOOP, through the one Session, guarded by the SDK's own
        # anyio lock and its 60s expiry buffer — so this is a real network call
        # roughly once every 14 minutes and a float comparison the rest of the
        # time. Read AFTER the refresh, so the token handed across has >=60s of
        # life against a dispatch bounded at ~5s: it cannot be stale by
        # construction, and a failed mint fails the attempt before dispatching.
        await self._session.refresh()
        token = self._session.session_token

        future = self._executor.submit(self._fetch_sync, token)
        self._inflight = future
        self._inflight_since = self._clock()
        try:
            result = await asyncio.wrap_future(future)
            if isinstance(result, _Failure):
                raise result.exc  # re-raised HERE, on the loop — see `_Failure`
            return result
        finally:
            # Only clear it if the work is genuinely over. On cancellation the
            # thread keeps running and the reference is what makes the NEXT call
            # refuse instead of queueing.
            if future.done():
                self._inflight = None

    # -- the worker thread -------------------------------------------------

    def _fetch_sync(self, token: str) -> tuple[list[Position], Balances] | _Failure:
        """Both legs, in order, against one deadline. Runs OFF the event loop.

        Returns a `_Failure` rather than raising — see that class for why the
        difference is load-bearing rather than stylistic.
        """
        deadline = self._clock() + self._budget_s
        auth = {"Authorization": f"Bearer {token}"}
        try:
            # POSITIONS FIRST, deliberately: it is the larger payload and the
            # slower call, so if the budget is going to run out it should run out
            # on the cheap one.
            positions = self._leg(
                "positions",
                f"/accounts/{self._account_number}/positions",
                auth,
                deadline,
                lambda data: [map_position(CurrentPosition(**i)) for i in data["items"]],
            )
            balances = self._leg(
                "balances",
                f"/accounts/{self._account_number}/balances/{_BALANCES_CURRENCY}",
                auth,
                deadline,
                lambda data: map_balances(AccountBalance(**data)),
            )
        except Exception as exc:  # noqa: BLE001 - carried back verbatim, then re-raised
            # ALL-OR-NOTHING. Either leg failing fails the whole attempt, leaving
            # the cache untouched and staleness growing — the fail-closed
            # contract, unchanged. Partial seeding is not introduced here; that
            # would be a change to that contract, not a timeout decision.
            return _Failure(exc)
        return positions, balances

    def _leg(
        self,
        leg: str,
        url: str,
        auth: dict[str, str],
        deadline: float,
        parse: Callable[[dict[str, Any]], Any],
    ) -> Any:
        remaining = deadline - self._clock()
        if remaining < _MIN_REQUEST_BUDGET_S:
            raise tag_error(
                TimeoutError(
                    f"{leg}: {max(remaining, 0.0):.3f}s of budget left, below the "
                    f"{_MIN_REQUEST_BUDGET_S:g}s floor — not issued"
                ),
                leg=leg, error_class="timeout",
            )
        try:
            # NOT a scalar. See `_CONNECT_BUDGET_S`: a scalar sets all four phases
            # independently, which is what let one leg outrun the whole attempt's
            # budget. `remaining` is the READ ceiling — the phase this budget is
            # about — and the other three are bounded so they cannot stack.
            response = self._client.get(
                url,
                headers=auth,
                timeout=httpx2.Timeout(
                    remaining,
                    connect=min(_CONNECT_BUDGET_S, remaining),
                    write=min(_WRITE_BUDGET_S, remaining),
                    pool=min(_POOL_BUDGET_S, remaining),
                ),
            )
        except httpx2.TimeoutException as exc:
            # THE ONE JUDGEMENT CALL IN THE ERROR MODEL. httpx timeouts are
            # `TimeoutException` subclasses, not builtin `TimeoutError`, so left
            # alone `error_type` would change from "TimeoutError" to "ReadTimeout"
            # and every existing audit query — including the ones that resolved
            # this investigation — would silently stop matching. The specific
            # httpx class survives in the message and in `__cause__`; the string
            # `error_type` matches on does not move. None of those class names
            # contain an `_AUTH_ERROR_MARKERS` substring, so `is_auth_error` is
            # unaffected.
            raise tag_error(
                TimeoutError(f"{leg}: {type(exc).__name__}: {exc}"),
                leg=leg, error_class="timeout",
            ) from exc
        except httpx2.TransportError as exc:
            raise tag_error(exc, leg=leg, error_class="transport")
        try:
            return parse(validate_and_parse(response))
        except TastytradeError as exc:
            # The SDK's own message, byte-identical — including the
            # "token_invalid: ..." text `is_auth_error` matches on.
            raise tag_error(exc, leg=leg, error_class="http")
        except Exception as exc:  # noqa: BLE001 - malformed payload, missing key
            raise tag_error(exc, leg=leg, error_class="other")

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Close the client and release the worker. Never blocks on a hung request.

        `_run_session_day` builds a session per trading day, so the client MUST be
        closed rather than left to GC — leaking one leaks a socket per day in a
        process designed to run for weeks.

        Called from `session.py`'s teardown immediately after `acct_stream.stop()`,
        which cancels the refresh loop, so by then no new dispatch can start.
        """
        if self._closed:
            return
        self._closed = True
        inflight = self._inflight
        wedged = inflight is not None and not inflight.done()
        if wedged:
            # A dispatch is still running. Closing the pool out from under it
            # would be closing sockets another thread is reading, so queue the
            # close behind it instead — and do NOT cancel_futures, which would
            # cancel the very close we just queued. The worker is non-daemon, so
            # it runs the close whenever the request finally unwinds.
            logger.warning("account refresher closing with a dispatch still in flight")
            try:
                self._executor.submit(self._client.close)
            except RuntimeError:  # executor already shut down by its owner
                pass
        else:
            self._client.close()
        if self._owns_executor:
            # wait=False: process shutdown must not block on a hung request.
            self._executor.shutdown(wait=False, cancel_futures=not wedged)
