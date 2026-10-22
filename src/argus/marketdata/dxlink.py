"""DXLinkMarketData — the tastytrade DXLink implementation of MarketDataPort.

Design for testability: the wire→domain mapping (`_ingest`) is a pure method that
takes a real `dxfeed.Quote` and updates the shared `QuoteBook`. It is unit-tested
offline with constructed quotes. The connection management (`start`/`subscribe`/
`stop`) is a thin wrapper over `DXLinkStreamer` and is exercised against sandbox,
not in unit tests.

Retuning note (spec §5): TOMIC's `MarketDataService` gates many symbols slowly
behind a `Semaphore(3)`. This layer does the opposite — one underlying's option
chain, high frequency — so it holds a persistent subscription and consumes the
push stream rather than polling. `refresh_interval` is the per-subscription
conflation window passed to DXLink.

CONNECTION OWNERSHIP (learned the hard way, 2026-07-21). The streamer is entered
and exited inside ONE long-lived task via `async with` — never by driving
`__aenter__()`/`__aexit__()` from separate `start()`/`stop()` calls. The SDK
yields from inside an anyio `create_task_group()`, and an anyio cancel scope must
be exited by the task that entered it. Driving it by hand works right up until
the socket actually fails, at which point anyio unwinds from the wrong task and
one `WebSocketNetworkError` becomes an unrecoverable cascade of "generator didn't
stop after athrow()" that takes the process down.

The same task reconnects with backoff and re-subscribes the full symbol set. A
disconnect must NOT be fatal: this process is always-on during market hours and
may be holding a position. Trading safety during the gap is already handled
elsewhere and needs no help from here — quote age grows, and the fail-closed gate
refuses entries on stale data. So the rule is: don't trade while disconnected,
but don't die either.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import Greeks as DxGreeks
from tastytrade.dxfeed import Quote as DxQuote

from argus.marketdata.book import Clock, GreeksBook, QuoteBook
from argus.marketdata.models import OptionGreeks, Quote

logger = logging.getLogger("argus.marketdata.dxlink")

# telemetry(category, payload) — the AuditLog Tier 2 seam, injected so this
# module keeps no audit dependency.
Telemetry = Callable[[str, dict[str, Any]], Any]

# Reconnect policy. NOT risk thresholds: none of these can cause a bad trade,
# only a slower recovery. The threshold that governs whether stale data may be
# TRADED on is `staleness.market_data_max_age_s`, enforced by the pre-trade gate.
_RECONNECT_BACKOFF_BASE_S = 1.0
_RECONNECT_BACKOFF_MAX_S = 30.0
_CONNECT_TIMEOUT_S = 30.0
# A socket can stay open while delivering nothing. The gate already fails closed
# on the resulting staleness, so this only governs RECOVERY. It assumes a feed
# that ticks continuously (SPX during RTH); after hours the feed is legitimately
# quiet, and a long-running process outside market hours will reconnect on this
# interval until a session-aware guard exists.
_DEFAULT_STALL_RECONNECT_S = 60.0


class FeedStalled(RuntimeError):
    """Connected, but no events for long enough to presume the link is dead."""


def _to_float(value: Decimal | float | None) -> float | None:
    """Map a dxfeed price/size to a finite float, or None if absent/NaN."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class DXLinkMarketData:
    def __init__(
        self,
        session: object,
        *,
        clock: Clock,
        refresh_interval: float = 0.1,
        stall_reconnect_s: float = _DEFAULT_STALL_RECONNECT_S,
        reconnect_backoff_s: float = _RECONNECT_BACKOFF_BASE_S,
        reconnect_backoff_max_s: float = _RECONNECT_BACKOFF_MAX_S,
        telemetry: Telemetry | None = None,
        streamer_factory: Callable[[object], Any] = DXLinkStreamer,
    ) -> None:
        # `session` is a tastytrade Session; typed loosely to keep this module
        # free of a hard construction-time dependency on a live session.
        self._session = session
        self._clock = clock
        self._book = QuoteBook(clock)
        self._greeks = GreeksBook(clock)
        self._refresh_interval = refresh_interval
        self._stall_reconnect_s = stall_reconnect_s
        self._backoff_base = reconnect_backoff_s
        self._backoff_max = reconnect_backoff_max_s
        self._telemetry = telemetry
        # Injected so the reconnect path is unit-testable offline — the failure
        # it exists to survive is precisely the one sandbox won't reproduce.
        self._streamer_factory = streamer_factory
        self._streamer: DXLinkStreamer | None = None
        # One task owns the whole connection lifecycle — see the module docstring.
        self._runner: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._connected = asyncio.Event()
        self._reconnects = 0
        self._symbols: set[str] = set()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def queue_depths(self) -> dict[str, int]:
        """Buffered-but-unconsumed events per type, straight off the SDK's streams.

        DIAGNOSTIC for the intra-session lag ramp (CLAUDE.md). The SDK builds its
        per-event-type streams with `create_memory_object_stream(math.inf)` — an
        UNBOUNDED buffer — so anything produced faster than it is consumed grows
        without limit, and at `refresh_interval=0.1` across ~121 symbols x
        (Quote+Greeks) the producer is capable of ~2,400 events/second. A depth
        that climbs through the session IS the leak; one that sits near zero
        exonerates this path.

        Reaches into `_recv`, which is private SDK state, so it is wrapped
        completely: any failure yields `{}`. A diagnostic that can break the
        status endpoint — or the session — is worse than no diagnostic.
        """
        streamer = self._streamer
        if streamer is None:
            return {}
        depths: dict[str, int] = {}
        try:
            for event_cls, stream in getattr(streamer, "_recv", {}).items():
                stats = stream.statistics()
                depths[getattr(event_cls, "__name__", str(event_cls))] = int(
                    stats.current_buffer_used
                )
        except Exception:  # noqa: BLE001 - private SDK internals; never break the caller
            logger.debug("could not read dxlink queue depths", exc_info=True)
            return {}
        return depths

    @property
    def reconnects(self) -> int:
        return self._reconnects

    # -- wire -> domain (pure, unit-tested) --------------------------------

    def _ingest(self, event: DxQuote) -> Quote:
        # A missing/NaN side maps to 0.0 — consistent with how the pricing layer's
        # zero-bid guard (spec §7) will floor it. Sizes default to 0.0 when absent.
        bid = _to_float(event.bid_price)
        ask = _to_float(event.ask_price)
        bid_size = _to_float(event.bid_size)
        ask_size = _to_float(event.ask_size)
        return self._book.update(
            event.event_symbol,
            0.0 if bid is None else bid,
            0.0 if ask is None else ask,
            0.0 if bid_size is None else bid_size,
            0.0 if ask_size is None else ask_size,
        )

    def _ingest_greeks(self, event: DxGreeks) -> OptionGreeks:
        return self._greeks.update(
            event.event_symbol,
            _to_float(event.delta) or 0.0,
            _to_float(event.gamma) or 0.0,
            _to_float(event.theta) or 0.0,
            _to_float(event.vega) or 0.0,
            _to_float(event.volatility) or 0.0,
        )

    # -- MarketDataPort: reads ---------------------------------------------

    def greeks(self, symbol: str) -> OptionGreeks | None:
        return self._greeks.latest(symbol)

    def greeks_age(self, symbol: str) -> float:
        return self._greeks.age(symbol)

    def latest(self, symbol: str) -> Quote | None:
        return self._book.latest(symbol)

    def age(self, symbol: str) -> float:
        return self._book.age(symbol)

    def is_stale(self, symbol: str, max_age_s: float) -> bool:
        return self._book.is_stale(symbol, max_age_s)

    def last_event_age(self) -> float:
        return self._book.last_event_age()

    # -- MarketDataPort: connection ----------------------------------------

    async def start(self, timeout: float = _CONNECT_TIMEOUT_S) -> None:
        """Spawn the connection owner and wait for the first successful connect.

        A failure to connect at all is fatal (it is a credential/entitlement
        problem, not a blip) and raises. Every LATER failure is handled by the
        runner as a reconnect.
        """
        if self._runner is not None and not self._runner.done():
            return
        self._stopping.clear()
        self._connected.clear()
        self._runner = asyncio.create_task(self._run(), name="dxlink-runner")
        await self._await_connected(timeout)
        logger.info("dxlink market data started")

    async def _await_connected(self, timeout: float) -> None:
        assert self._runner is not None
        waiter = asyncio.create_task(self._connected.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, self._runner},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._runner in done:
                await self._runner  # re-raise whatever killed it
                raise RuntimeError("dxlink runner exited before connecting")
            if not done:
                raise TimeoutError(f"dxlink did not connect within {timeout:g}s")
        finally:
            waiter.cancel()

    async def subscribe(self, symbols: Iterable[str]) -> None:
        """Subscribe Quote (and Greeks — the signal engine selects strikes by delta).

        Greeks are only published for options; requesting them for an index/equity
        symbol simply yields no events, so a single subscribe path is safe.

        The symbol set is remembered and replayed on every reconnect, so a caller
        subscribes once and never has to care that the link dropped.
        """
        if self._runner is None:
            raise RuntimeError("start() must be called before subscribe()")
        new = [s for s in symbols if s not in self._symbols]
        if not new:
            return
        self._symbols.update(new)
        streamer = self._streamer
        if streamer is None:
            # Mid-reconnect: _resubscribe() will pick these up on the next connect.
            logger.info("queued %d symbol(s) for the next dxlink connect", len(new))
            return
        await self._send_subscribe(streamer, new)
        logger.info("subscribed to %d symbol(s) (quote+greeks)", len(new))

    async def _send_subscribe(self, streamer: DXLinkStreamer, symbols: list[str]) -> None:
        await streamer.subscribe(DxQuote, symbols, refresh_interval=self._refresh_interval)
        await streamer.subscribe(DxGreeks, symbols, refresh_interval=self._refresh_interval)

    async def _resubscribe(self, streamer: DXLinkStreamer) -> None:
        if self._symbols:
            await self._send_subscribe(streamer, sorted(self._symbols))

    # -- the connection owner ----------------------------------------------

    async def _run(self) -> None:
        """Own the streamer for the process lifetime, reconnecting as needed."""
        backoff = self._backoff_base
        connected_before = False
        while not self._stopping.is_set():
            try:
                # `async with`, NOT __aenter__/__aexit__ across calls. See the
                # module docstring: the SDK's anyio cancel scope must be exited
                # by the task that entered it.
                async with self._streamer_factory(self._session) as streamer:
                    self._streamer = streamer
                    await self._resubscribe(streamer)
                    self._connected.set()
                    backoff = self._backoff_base
                    if connected_before:
                        self._reconnects += 1
                        logger.info(
                            "dxlink reconnected (%d symbol(s) resubscribed, reconnect #%d)",
                            len(self._symbols), self._reconnects,
                        )
                        self._emit("marketdata.reconnected",
                                   {"symbols": len(self._symbols), "reconnects": self._reconnects})
                    connected_before = True
                    await self._pump(streamer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping.is_set():
                    break
                if not connected_before:
                    # Never connected: this is a credential/entitlement problem,
                    # not a blip. Surface it from start() instead of retrying
                    # forever behind a facade that looks like a working feed.
                    self._connected.clear()
                    self._streamer = None
                    raise
                logger.warning(
                    "dxlink link lost (%s: %s); reconnecting in %.0fs",
                    type(exc).__name__, exc, backoff,
                )
                self._emit(
                    "marketdata.disconnected",
                    {"error_type": type(exc).__name__, "error": str(exc)[:500],
                     "retry_in_s": backoff},
                )
            finally:
                self._connected.clear()
                self._streamer = None
            if self._stopping.is_set():
                break
            await self._sleep_or_stop(backoff)
            backoff = min(backoff * 2, self._backoff_max)

    async def _sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _pump(self, streamer: DXLinkStreamer) -> None:
        """Consume both feeds until stop is requested or the link fails."""
        tasks = [
            asyncio.create_task(self._consume(streamer, DxQuote, self._ingest),
                                name="dxlink-quotes"),
            asyncio.create_task(self._consume(streamer, DxGreeks, self._ingest_greeks),
                                name="dxlink-greeks"),
            asyncio.create_task(self._watch_for_stall(), name="dxlink-stall"),
            asyncio.create_task(self._stopping.wait(), name="dxlink-stop"),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    raise task.exception()  # type: ignore[misc]
        finally:
            # The listeners must be gone BEFORE the streamer context exits, or
            # they unwind against a half-closed websocket.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _consume(self, streamer: DXLinkStreamer, event_type, ingest) -> None:
        async for event in streamer.listen(event_type):
            ingest(event)

    async def _watch_for_stall(self) -> None:
        """Raise once the feed has been silent long enough to presume it's dead.

        A dropped socket usually raises, but it can also just go quiet. Silence
        is SAFE (the gate blocks on stale quotes) but not self-healing, so it
        gets escalated into a reconnect.
        """
        if self._stall_reconnect_s <= 0:
            await asyncio.Event().wait()  # disabled
        connected_at = self._clock()
        tick = min(self._stall_reconnect_s / 4.0, 5.0)
        while True:
            await asyncio.sleep(tick)
            # Measure from connect, not from the last event: right after a
            # reconnect there is legitimately no event yet.
            silence = min(self._book.last_event_age(), self._clock() - connected_at)
            if silence > self._stall_reconnect_s:
                raise FeedStalled(f"no dxlink events for {silence:.0f}s")

    def _emit(self, category: str, payload: dict[str, Any]) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry(category, payload)
        except Exception:  # telemetry must never break the feed it observes
            logger.debug("marketdata telemetry emit failed", exc_info=True)

    async def stop(self) -> None:
        self._stopping.set()
        runner, self._runner = self._runner, None
        if runner is not None:
            try:
                await runner
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("dxlink runner errored during stop", exc_info=True)
        logger.info("dxlink market data stopped")
