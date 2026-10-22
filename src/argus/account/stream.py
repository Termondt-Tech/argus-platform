"""AlertStreamerAccount — AlertStreamer-fed live account state (spec §5).

Listens for account events instead of polling: `CurrentPosition` and
`AccountBalance` maintain the live cache; `PlacedOrder` events are the streamed
fill confirmations that replace order polling (spec §9 — duplicate/runaway order
mitigation). One `listen()` async iterator runs per alert class.

As with the DXLink adapter, the wire→domain ingestion (`_ingest_*`) is pure and
unit-tested; the connection management is a thin wrapper over `AlertStreamer`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from tastytrade import AlertStreamer
from tastytrade.account import AccountBalance, CurrentPosition
from tastytrade.order import PlacedOrder

from argus.account.cache import AccountStateCache, Clock
from argus.account.models import AccountSnapshot, map_balances, map_position
from argus.account.port import declared_error_class, refresh_leg

logger = logging.getLogger("argus.account.stream")

# on_order(placed_order) -> awaitable; hook for the execution engine / fill audit.
OnOrder = Callable[[PlacedOrder], Awaitable[None]]
# reauth() -> awaitable; re-mints the session token after it expires.
Reauth = Callable[[], Awaitable[None]]
# telemetry(category, payload); the AuditLog Tier 2 seam, injected so this module
# stays free of an audit dependency.
Telemetry = Callable[[str, dict[str, Any]], Any]

# Base of the failure backoff ladder, doubled per consecutive failure and capped
# at the normal refresh interval. Internal retry policy, not a risk parameter.
_RETRY_BACKOFF_BASE_S = 1.0
_RETRY_BACKOFF_MAX_DOUBLINGS = 6

# Stream reconnect policy (see the connection section below).
_RECONNECT_BACKOFF_BASE_S = 1.0
_RECONNECT_BACKOFF_MAX_S = 30.0
_CONNECT_TIMEOUT_S = 30.0

# An expired token is a different failure class from a transient link fault:
# retrying the same dead token just burns the staleness budget, so it gets a
# re-auth instead. The SDK surfaces the reason only in the message text of a
# generic TastytradeError, so substring matching is the only signal available.
_AUTH_ERROR_MARKERS = ("token_invalid", "invalid_credentials", "token has expired", "unauthorized")


def _unconsumed_alert_types() -> tuple[Any, ...]:
    """Every `MAP_ALERTS` type this class does NOT have a listener for.

    Derived from the SDK's own map rather than hardcoded, so a tastytrade release
    that adds a tenth alert type gets drained automatically instead of silently
    reintroducing the leak. Returns `()` if the SDK's internals move — draining is
    an optimization, not a rail, and must never stop the session starting.
    """
    try:
        from tastytrade.streamer import MAP_ALERTS
    except Exception:  # noqa: BLE001 - SDK layout is not ours to depend on
        return ()
    consumed = {CurrentPosition, AccountBalance, PlacedOrder}
    return tuple(cls for cls in MAP_ALERTS.values() if cls not in consumed)


#: Resolved once at import. See `_drain_unconsumed`.
_UNCONSUMED_ALERTS: tuple[Any, ...] = _unconsumed_alert_types()


class RefreshBackstopError(Exception):
    """The refresher did not return within `refresh_interval_s`. It broke its contract.

    `AccountRefreshPort` implementations own their own bound (see the Protocol).
    This is the structural backstop behind that contract, and
    `refresh_interval_s` is the principled number for it rather than an arbitrary
    multiple: **if an attempt has not returned by the time the next one is due,
    stop waiting for it.** It is exactly 2x the implementation's own budget,
    because the loader already guarantees
    `account_refresh_timeout_s < account_refresh_s`.

    It should never fire. Its own type is the point — a bare `TimeoutError` here
    would be indistinguishable in `error_type` from the refresher's ordinary
    budget timeout, and this means something completely different.
    """

    argus_error_class = "backstop"


def is_auth_error(exc: BaseException) -> bool:
    """True if `exc` looks like an expired/invalid session token.

    Matching wide is deliberate: a spurious re-auth costs one extra round trip,
    whereas a missed one leaves the account rail blind until it recovers by luck.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


def classify_error(exc: BaseException) -> str:
    """Coarse, STABLE failure class for `account.refresh_failed`.

    Exists so the next investigation does not depend on third-party class names
    staying put, which is the fragility `error_type` alone carries: a tastytrade
    or httpx release that renames `ReadTimeout` silently breaks every audit query
    written against it.

    **`auth` wins over whatever the port declared**, deliberately. The SDK raises
    one `TastytradeError` for every non-2xx and the auth split lives in the
    MESSAGE, so a 401 arrives tagged `http`; resolving it here is what keeps
    `error_class` and `auth_error` consistent by construction rather than by two
    call sites agreeing.
    """
    if is_auth_error(exc):
        return "auth"
    declared = declared_error_class(exc)
    if declared is not None:
        return declared
    # A port that declares nothing (a fake, a future implementation) still gets a
    # useful answer for the one class that matters most.
    return "timeout" if isinstance(exc, TimeoutError) else "other"


class AlertStreamerAccount:
    def __init__(
        self,
        session: Any,
        account: Any,
        *,
        clock: Clock,
        on_order: OnOrder | None = None,
        refresher: Any = None,
        refresh_interval_s: float = 10.0,
        position_evict_after_s: float | None = None,
        stale_after_s: float | None = None,
        reauth: Reauth | None = None,
        telemetry: Telemetry | None = None,
        streamer_factory: Callable[[Any], Any] = AlertStreamer,
        reconnect_backoff_s: float = _RECONNECT_BACKOFF_BASE_S,
        reconnect_backoff_max_s: float = _RECONNECT_BACKOFF_MAX_S,
    ) -> None:
        self._session = session
        self._account = account
        self._clock = clock
        self._cache = AccountStateCache(clock)
        self._on_order = on_order
        # Liveness floor (see refresh_now). `refresher` is an AccountRefreshPort.
        self._refresher = refresher
        self._refresh_interval = refresh_interval_s
        # NB there is no `refresh_timeout_s` here any more. The per-attempt bound
        # belongs to the REFRESHER now (2026-08-26) — see `refresh_now`.
        # How stale a cache entry absent from REST must be before `seed` drops it.
        # None disables eviction entirely (the pre-2026-08-19 behaviour).
        self._evict_after = position_evict_after_s
        # The gate's tolerance, for log severity only: below it a failure is a
        # blip, above it entries are actually being blocked.
        self._stale_after = stale_after_s
        self._reauth = reauth
        self._telemetry = telemetry
        # Injected so the reconnect path is unit-testable offline.
        self._streamer_factory = streamer_factory
        self._backoff_base = reconnect_backoff_s
        self._backoff_max = reconnect_backoff_max_s
        self._consecutive_failures = 0
        self._first_failure_at: float | None = None
        self._last_error: BaseException | None = None
        self._streamer: AlertStreamer | None = None
        # One task owns the streamer lifecycle; _tasks holds the siblings that
        # must outlive a reconnect (the REST liveness refresh).
        self._runner: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._connected = asyncio.Event()
        self._reconnects = 0
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def consecutive_refresh_failures(self) -> int:
        return self._consecutive_failures

    # -- wire -> domain (pure, unit-tested) --------------------------------

    def _ingest_position(self, cp: CurrentPosition) -> None:
        self._cache.update_position(map_position(cp))

    def _ingest_balance(self, bal: AccountBalance) -> None:
        self._cache.update_net_liq(map_balances(bal).net_liquidating_value)

    def seed(self, positions: list, balances) -> None:
        """Seed the cache from an authoritative REST snapshot.

        The stream only pushes on *change*, so a freshly-connected session can sit
        with no state for a long time. Seeding from REST means the risk snapshot
        is usable immediately instead of failing closed on an empty cache.

        **Authoritative means the whole set, not just the rows it contains.** This
        merged the snapshot in and never subtracted until 2026-08-19, on the belief
        that a closed position arrives as a zero-quantity row. It does not — the
        REST positions endpoint simply omits it. So one missed close frame on the
        stream left a cache entry no amount of re-seeding could clear, and every
        subsequent refresh stepped past it while refreshing the real rows around it.
        `evict_absent` is the subtraction, age-gated so a position the broker's book
        has not caught up on yet is never mistaken for a ghost.
        → docs/decisions/2026-08-19-account-cache-ghost-positions.md
        """
        for position in positions:
            self._cache.update_position(position)
        self._cache.update_net_liq(balances.net_liquidating_value)
        self._evict_absent(positions)

    def _evict_absent(self, positions: list) -> None:
        """Subtract what the snapshot did not report. Records every drop.

        Disabled (threshold None) leaves the pre-2026-08-19 merge-only behaviour, so
        every construction site that predates the parameter keeps working unchanged.

        A drop is WARN, not error: by the time an entry has aged past the threshold
        without appearing in a single snapshot, removing it is the correct outcome
        and the cache is being repaired. What deserves attention is the RATE — a
        healthy session evicts nothing, because the stream's zero-quantity frames
        arrive and `update_position` clears them first.
        """
        if self._evict_after is None:
            return
        dropped = self._cache.evict_absent(
            {p.symbol for p in positions}, older_than_s=self._evict_after
        )
        for entry in dropped:
            logger.warning(
                "account cache: dropped %s (qty %+d), absent from REST for %.1fs — "
                "a close event was missed on the stream",
                entry.position.symbol, entry.position.signed_quantity, entry.age_s,
            )
            self._emit(
                "account.position_evicted",
                {
                    "symbol": entry.position.symbol,
                    "signed_quantity": entry.position.signed_quantity,
                    "age_s": entry.age_s,
                    "evict_after_s": self._evict_after,
                },
            )

    async def refresh_now(self) -> bool:
        """Re-seed from REST — the liveness floor. Returns True if it succeeded.

        The AlertStreamer pushes only on CHANGE, so a quiet (e.g. flat) account
        emits nothing and "time since last event" grows without bound. Without
        this, the staleness gate misreads that silence as a dead link and blocks
        every entry — worst exactly when flat, i.e. when you want the first entry
        of the day.

        One attempt. The retry policy lives in `_refresh_loop` so this stays a
        single, testable round trip.

        **THE PER-ATTEMPT BOUND IS THE PORT'S, NOT THIS METHOD'S** (2026-08-26).
        This used to `gather` two calls under one `asyncio.timeout`, and both
        halves of that had to go: the `gather` is what put two concurrent TLS
        handshakes on the loop every 10s, and the wrapper cannot bound an
        implementation that runs on a worker thread — firing it abandons a thread
        that keeps running while reporting success at cancelling it. Never re-add
        it. The `asyncio.timeout` below is a BACKSTOP at the refresh interval:
        it exists to make a port that broke its contract loud, and if it ever
        fires the bug is in the port.
        → docs/designs/2026-08-25-account-refresh-off-event-loop.md

        On failure the cache is deliberately left UNTOUCHED, so staleness keeps
        growing and the gate fails closed. A genuinely broken link must still
        look broken.
        """
        if self._refresher is None:
            return False
        started = self._clock()
        backstop = asyncio.timeout(self._refresh_interval)
        try:
            async with backstop:
                positions, balances = await self._refresher.fetch_snapshot()
        except TimeoutError as exc:
            # `expired()` is what separates OUR wrapper firing from the port
            # raising its own (normalized, deliberately builtin) budget timeout.
            # Conflating them would report a contract breach as a routine 5s
            # timeout, which is the one thing this backstop exists to surface.
            if backstop.expired():
                exc = RefreshBackstopError(
                    f"account refresh did not return within {self._refresh_interval:g}s "
                    "(the refresh interval) — the port did not bound itself"
                )
            self._note_refresh_failure(exc, self._clock() - started)
            return False
        except Exception as exc:
            self._note_refresh_failure(exc, self._clock() - started)
            return False
        self.seed(positions, balances)
        self._note_refresh_success()
        return True

    def _note_refresh_failure(self, exc: BaseException, elapsed_s: float = 0.0) -> None:
        self._consecutive_failures += 1
        self._last_error = exc
        if self._first_failure_at is None:
            self._first_failure_at = self._clock()
        age = self.age()
        # Below the gate's tolerance this is a blip the retry will absorb; above
        # it, entries are being blocked right now and it deserves a traceback.
        blocking = self._stale_after is not None and age > self._stale_after
        if blocking:
            logger.error(
                "account liveness refresh failing: %s (consecutive=%d, age=%.1fs > %.1fs "
                "— entries are BLOCKED)",
                exc, self._consecutive_failures, age, self._stale_after,
                exc_info=exc,
            )
        else:
            logger.warning(
                "account liveness refresh failed (consecutive=%d, age=%.1fs): %s: %s",
                self._consecutive_failures, age, type(exc).__name__, exc,
            )
        self._emit(
            "account.refresh_failed",
            {
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "consecutive_failures": self._consecutive_failures,
                "age_s": round(age, 3),
                "auth_error": is_auth_error(exc),
                "blocking_entries": blocking,
                # Three fields added 2026-08-26, all cheap and all Tier 2.
                # `leg`: which half failed — `gather` could not tell you, and it
                #   separates "the account endpoint is sick" from "the whole API
                #   is sick". None when the port did not attribute it.
                "leg": refresh_leg(exc),
                # `elapsed_s`: "timed out at the budget" vs "failed fast", which
                #   the coarse `error_type` cannot distinguish.
                "elapsed_s": round(elapsed_s, 3),
                # `error_class`: coarse and OURS, so the next investigation does
                #   not hinge on a third-party class name staying put.
                "error_class": classify_error(exc),
            },
        )

    def _note_refresh_success(self) -> None:
        if self._consecutive_failures:
            outage = self._clock() - (self._first_failure_at or self._clock())
            logger.info(
                "account liveness refresh recovered after %d failure(s) over %.1fs",
                self._consecutive_failures, outage,
            )
            self._emit(
                "account.refresh_recovered",
                {"consecutive_failures": self._consecutive_failures, "outage_s": round(outage, 3)},
            )
        self._consecutive_failures = 0
        self._first_failure_at = None
        self._last_error = None

    def _emit(self, category: str, payload: dict[str, Any]) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry(category, payload)
        except Exception:  # telemetry must never break the rail it observes
            logger.debug("account telemetry emit failed", exc_info=True)

    async def _try_reauth(self) -> bool:
        if self._reauth is None:
            return False
        try:
            await self._reauth()
        except Exception as exc:
            logger.error("account session re-auth failed: %s: %s", type(exc).__name__, exc)
            self._emit("account.reauth_failed", {"error": str(exc)[:500]})
            return False
        logger.info("account session re-authenticated after a token error")
        self._emit("account.reauth", {})
        return True

    def _retry_delay(self) -> float:
        doublings = min(max(self._consecutive_failures - 1, 0), _RETRY_BACKOFF_MAX_DOUBLINGS)
        return min(_RETRY_BACKOFF_BASE_S * (2 ** doublings), self._refresh_interval)

    async def _refresh_loop(self) -> None:
        """Periodic re-seed, with fast retry on failure.

        Sleeping the full interval after a failure is wrong: against a 30s
        staleness tolerance a 10s interval gives a transient 502 a third of the
        budget and two of them all of it. A failed attempt therefore backs off
        from 1s, doubling, capped at the normal interval — several attempts
        inside the window a single one used to occupy.
        """
        delay = self._refresh_interval
        while True:
            await asyncio.sleep(delay)
            if await self.refresh_now():
                delay = self._refresh_interval
                continue
            # An expired token won't heal on its own; re-mint it and retry now
            # rather than spending the backoff ladder on a dead credential.
            if self._last_error is not None and is_auth_error(self._last_error):
                if await self._try_reauth() and await self.refresh_now():
                    delay = self._refresh_interval
                    continue
            delay = self._retry_delay()

    # -- AccountStatePort --------------------------------------------------

    def snapshot(self) -> AccountSnapshot | None:
        return self._cache.snapshot()

    def age(self) -> float:
        return self._cache.age()

    def is_stale(self, max_age_s: float) -> bool:
        return self._cache.is_stale(max_age_s)

    # -- connection --------------------------------------------------------
    #
    # Same ownership rule as the DXLink adapter (see marketdata/dxlink.py): ONE
    # task holds the streamer via `async with`, because the SDK yields from
    # inside an anyio task group whose cancel scope must be exited by the task
    # that entered it. Driving __aenter__/__aexit__ across start()/stop() looks
    # fine until the socket fails, then unwinds from the wrong task and takes
    # the process down.
    #
    # The liveness refresh deliberately does NOT live under the runner: a REST
    # re-seed is exactly what keeps account state usable while the stream is
    # down, so it must survive a reconnect rather than being torn down with it.

    async def start(self, timeout: float = _CONNECT_TIMEOUT_S) -> None:
        if self._runner is not None and not self._runner.done():
            return
        self._stopping.clear()
        self._connected.clear()
        self._runner = asyncio.create_task(self._run(), name="alert-runner")
        await self._await_connected(timeout)
        if self._refresher is not None:
            self._tasks = [
                asyncio.create_task(self._refresh_loop(), name="account-liveness-refresh")
            ]
        logger.info(
            "alert streamer started for account %s",
            getattr(self._account, "account_number", "?"),
        )

    async def _await_connected(self, timeout: float) -> None:
        assert self._runner is not None
        waiter = asyncio.create_task(self._connected.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, self._runner}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if self._runner in done:
                await self._runner
                raise RuntimeError("alert streamer runner exited before connecting")
            if not done:
                raise TimeoutError(f"alert streamer did not connect within {timeout:g}s")
        finally:
            waiter.cancel()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def reconnects(self) -> int:
        return self._reconnects

    def queue_depths(self) -> dict[str, int]:
        """Buffered-but-unconsumed alerts per type, straight off the SDK's streams.

        DIAGNOSTIC for the intra-session lag ramp (CLAUDE.md), and the reason
        `_drain_unconsumed` below exists. `AlertStreamer._queues` is a
        `defaultdict` of `create_memory_object_stream(math.inf)` — UNBOUNDED — and
        `_map_message` pushes EVERY type in `MAP_ALERTS` into it. This process
        consumes three. A depth that climbs through the session is the leak.

        Private SDK state, so fully wrapped: any failure yields `{}`.
        """
        streamer = self._streamer
        if streamer is None:
            return {}
        depths: dict[str, int] = {}
        try:
            for name, stapled in getattr(streamer, "_queues", {}).items():
                depths[str(name)] = int(
                    stapled.receive_stream.statistics().current_buffer_used
                )
        except Exception:  # noqa: BLE001 - private SDK internals; never break the caller
            logger.debug("could not read alert queue depths", exc_info=True)
            return {}
        return depths

    async def _run(self) -> None:
        backoff = self._backoff_base
        connected_before = False
        while not self._stopping.is_set():
            try:
                async with self._streamer_factory(self._session) as streamer:
                    self._streamer = streamer
                    await streamer.subscribe_accounts([self._account])
                    self._connected.set()
                    backoff = self._backoff_base
                    if connected_before:
                        self._reconnects += 1
                        logger.info("alert streamer reconnected (#%d)", self._reconnects)
                        self._emit("account.stream_reconnected",
                                   {"reconnects": self._reconnects})
                    connected_before = True
                    await self._pump(streamer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping.is_set():
                    break
                if not connected_before:
                    # Never connected — a credential/entitlement problem, not a
                    # blip. Surface it from start() rather than retrying forever.
                    self._connected.clear()
                    self._streamer = None
                    raise
                # Not fatal: the REST liveness refresh keeps account state fresh
                # while the stream is down, and the gate blocks if it can't.
                logger.warning(
                    "alert streamer link lost (%s: %s); reconnecting in %.0fs",
                    type(exc).__name__, exc, backoff,
                )
                self._emit(
                    "account.stream_disconnected",
                    {"error_type": type(exc).__name__, "error": str(exc)[:500],
                     "retry_in_s": backoff},
                )
            finally:
                self._connected.clear()
                self._streamer = None
            if self._stopping.is_set():
                break
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self._backoff_max)

    async def _pump(self, streamer: AlertStreamer) -> None:
        tasks = [
            asyncio.create_task(self._listen_positions(streamer), name="alert-positions"),
            asyncio.create_task(self._listen_balances(streamer), name="alert-balances"),
            asyncio.create_task(self._listen_orders(streamer), name="alert-orders"),
            asyncio.create_task(self._stopping.wait(), name="alert-stop"),
            *self._discard_tasks(streamer),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    raise task.exception()  # type: ignore[misc]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _listen_positions(self, streamer: AlertStreamer) -> None:
        async for cp in streamer.listen(CurrentPosition):
            self._ingest_position(cp)

    async def _listen_balances(self, streamer: AlertStreamer) -> None:
        async for bal in streamer.listen(AccountBalance):
            self._ingest_balance(bal)

    async def _listen_orders(self, streamer: AlertStreamer) -> None:
        async for order in streamer.listen(PlacedOrder):
            if self._on_order is not None:
                await self._on_order(order)

    def _discard_tasks(self, streamer: AlertStreamer) -> list[asyncio.Task[None]]:
        """One task per alert type this process does not use, consuming and DISCARDING.

        `AlertStreamer._queues` is a `defaultdict` of memory object streams built
        with `math.inf` capacity, and `_map_message` pushes every type in
        `MAP_ALERTS` into it — nine types, of which this class consumes three
        (`CurrentPosition`, `AccountBalance`, `PlacedOrder`). The other six have
        no reader, so whatever the broker sends in them is retained, as parsed
        pydantic objects, for the entire life of the process. Nothing bounds it
        and nothing surfaces it.

        That is a leak in a process designed to run for weeks, and it is the kind
        that gets worse exactly as the session goes on. Draining costs one task
        per type and zero decisions: these messages carry nothing Argus acts on,
        which is precisely why nothing was reading them.

        NOT a substitute for knowing what is actually growing —
        `queue_depths()` is the measurement. This is the belt: even if these
        queues turn out to be empty in production, an unread unbounded queue is
        not something to leave in a long-running trading process.

        These belong to `_pump`'s task set, NOT to `self._tasks`. They are bound to
        one streamer, so they must die with it — parking them on `_tasks` (which
        deliberately survives a reconnect, for the REST refresher) would leave a
        dead listener per reconnect, which is the same class of accumulation this
        method exists to remove.
        """
        return [
            asyncio.create_task(
                self._discard(streamer, alert_cls),
                name=f"alert-discard-{getattr(alert_cls, '__name__', alert_cls)}",
            )
            for alert_cls in _UNCONSUMED_ALERTS
        ]

    async def _discard(self, streamer: AlertStreamer, alert_cls: Any) -> None:
        try:
            async for _ in streamer.listen(alert_cls):
                pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a discarded feed must never kill the session
            logger.debug("discard listener for %s ended", alert_cls, exc_info=True)

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        runner, self._runner = self._runner, None
        if runner is not None:
            try:
                await runner
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("alert streamer runner errored during stop", exc_info=True)
        logger.info("alert streamer stopped")
