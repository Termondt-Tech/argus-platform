"""Streamer connection ownership and reconnect.

Reproduces the 2026-07-21 shadow-run failure offline. A dropped DXLink websocket
(`WebSocketNetworkError` in the SDK's reader) took the whole process down, via a
cascade of `Attempted to exit cancel scope in a different task than it was
entered in`. Two defects, one test file:

1. The streamer was entered with `__aenter__()` in `start()` and exited with
   `__aexit__()` in `stop()` — different tasks. The SDK yields from inside an
   anyio task group, and anyio cancel scopes are strictly task-bound, so the
   moment there was a real exception to unwind, it unwound from the wrong task.
2. Nothing reconnected. An always-on process that may be holding a 0DTE position
   cannot exit because a socket blipped.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from argus.marketdata.dxlink import DXLinkMarketData, FeedStalled
from tastytrade.dxfeed import Quote as DxQuote


class FakeStreamer:
    """Stands in for DXLinkStreamer, recording which task enters and exits it."""

    instances: list["FakeStreamer"] = []

    def __init__(self, session, *, fail_after=None, enter_error=None):
        self.session = session
        self.subscribed: list[tuple[type, tuple[str, ...]]] = []
        self.entered_in = None
        self.exited_in = None
        self._fail_after = fail_after      # seconds until the "socket" dies
        self._enter_error = enter_error
        self._queues: dict[type, asyncio.Queue] = {}
        FakeStreamer.instances.append(self)

    async def __aenter__(self):
        if self._enter_error is not None:
            raise self._enter_error
        self.entered_in = asyncio.current_task()
        return self

    async def __aexit__(self, *exc):
        self.exited_in = asyncio.current_task()
        return False

    async def subscribe(self, event_type, symbols, refresh_interval=None):
        self.subscribed.append((event_type, tuple(symbols)))

    def _queue(self, event_type) -> asyncio.Queue:
        return self._queues.setdefault(event_type, asyncio.Queue())

    async def push(self, event_type, event) -> None:
        await self._queue(event_type).put(event)

    async def listen(self, event_type):
        if self._fail_after is not None and event_type is DxQuote:
            await asyncio.sleep(self._fail_after)
            raise ConnectionError("WebSocketNetworkError")
        while True:
            yield await self._queue(event_type).get()


@pytest.fixture(autouse=True)
def _reset_streamers():
    FakeStreamer.instances.clear()
    yield
    FakeStreamer.instances.clear()


def _md(clock=None, **kw):
    kw.setdefault("streamer_factory", FakeStreamer)
    kw.setdefault("stall_reconnect_s", 0.0)  # off unless a test asks for it
    kw.setdefault("reconnect_backoff_s", 0.01)  # keep the ladder inside a test
    kw.setdefault("reconnect_backoff_max_s", 0.02)
    return DXLinkMarketData(object(), clock=clock or (lambda: 0.0), **kw)


def _quote(symbol="SPX", bid=100.0, ask=100.5):
    # model_construct: the real wire type has many required fields the mapping
    # doesn't read (matches tests/test_dxlink_ingest.py).
    return DxQuote.model_construct(
        event_symbol=symbol, bid_price=Decimal(str(bid)), ask_price=Decimal(str(ask)),
        bid_size=Decimal("1"), ask_size=Decimal("1"),
    )


# -- defect 1: cancel-scope discipline -------------------------------------


async def test_streamer_is_entered_and_exited_by_the_same_task():
    # THE regression test. anyio cancel scopes are task-bound; entering in
    # start()'s task and exiting in stop()'s is what turned one socket error
    # into an unrecoverable teardown cascade.
    md = _md()
    await md.start()
    await md.subscribe(["SPX"])
    await md.stop()

    streamer = FakeStreamer.instances[0]
    assert streamer.entered_in is not None
    assert streamer.entered_in is streamer.exited_in


async def test_same_task_discipline_holds_across_a_reconnect():
    md = _md(streamer_factory=lambda s: FakeStreamer(s, fail_after=0.01))
    await md.start()
    await asyncio.sleep(0.1)
    await md.stop()

    assert len(FakeStreamer.instances) >= 2
    for streamer in FakeStreamer.instances:
        if streamer.entered_in is not None and streamer.exited_in is not None:
            assert streamer.entered_in is streamer.exited_in


# -- defect 2: reconnect ---------------------------------------------------


async def test_a_dropped_link_reconnects_instead_of_killing_the_process():
    md = _md(streamer_factory=lambda s: FakeStreamer(s, fail_after=0.01))
    await md.start()
    assert md.connected is True
    await asyncio.sleep(0.15)
    # The runner is still alive and has rebuilt the connection.
    assert md.reconnects >= 1
    assert md._runner is not None and not md._runner.done()
    await md.stop()


async def test_symbols_are_resubscribed_after_a_reconnect():
    # A caller subscribes once; surviving the drop is this layer's job.
    md = _md(streamer_factory=lambda s: FakeStreamer(s, fail_after=0.05))
    await md.start()
    await md.subscribe(["SPX", "SPXW 7450"])
    await asyncio.sleep(0.2)
    await md.stop()

    second = FakeStreamer.instances[1]
    resubscribed = {syms for _, syms in second.subscribed}
    assert ("SPX", "SPXW 7450") in resubscribed


async def test_subscribing_mid_reconnect_is_queued_not_lost():
    md = _md(streamer_factory=lambda s: FakeStreamer(s, fail_after=0.01))
    await md.start()
    await asyncio.sleep(0.02)  # in the reconnect gap
    await md.subscribe(["SPX"])
    await asyncio.sleep(0.15)
    await md.stop()

    assert any(
        ("SPX",) in {syms for _, syms in s.subscribed} for s in FakeStreamer.instances
    )


async def test_reconnect_is_reported_as_telemetry():
    events: list[tuple[str, dict]] = []
    md = _md(
        streamer_factory=lambda s: FakeStreamer(s, fail_after=0.01),
        telemetry=lambda c, p: events.append((c, p)),
    )
    await md.start()
    await asyncio.sleep(0.15)
    await md.stop()

    categories = [c for c, _ in events]
    assert "marketdata.disconnected" in categories
    assert "marketdata.reconnected" in categories
    disconnect = next(p for c, p in events if c == "marketdata.disconnected")
    assert disconnect["error_type"] == "ConnectionError"


async def test_telemetry_failure_cannot_break_the_feed():
    def _explode(category, payload):
        raise RuntimeError("audit is down")

    md = _md(streamer_factory=lambda s: FakeStreamer(s, fail_after=0.01),
             telemetry=_explode)
    await md.start()
    await asyncio.sleep(0.1)
    assert md._runner is not None and not md._runner.done()
    await md.stop()


# -- startup and shutdown --------------------------------------------------


async def test_failure_to_connect_at_all_is_fatal():
    # A first-connect failure is a credential/entitlement problem, not a blip —
    # it must surface loudly instead of being retried behind a silent facade.
    md = _md(streamer_factory=lambda s: FakeStreamer(s, enter_error=RuntimeError("bad token")))
    with pytest.raises(RuntimeError, match="bad token"):
        await md.start()


async def test_stop_is_clean_and_idempotent():
    md = _md()
    await md.start()
    await md.stop()
    await md.stop()  # must not raise
    assert md.connected is False


async def test_stop_during_a_reconnect_backoff_does_not_hang():
    md = _md(streamer_factory=lambda s: FakeStreamer(s, fail_after=0.01))
    await md.start()
    await asyncio.sleep(0.02)  # mid-backoff
    await asyncio.wait_for(md.stop(), timeout=1.0)


# -- quotes still flow ------------------------------------------------------


async def test_quotes_ingest_through_the_runner():
    md = _md()
    await md.start()
    await md.subscribe(["SPX"])
    await FakeStreamer.instances[0].push(DxQuote, _quote("SPX", 7480.0, 7480.5))
    await asyncio.sleep(0.05)
    assert md.latest("SPX") is not None
    assert md.latest("SPX").bid == 7480.0
    await md.stop()


# -- stall detection --------------------------------------------------------


async def test_a_silent_but_open_link_is_escalated_to_a_reconnect():
    # The nastier failure: socket open, no events. Safe (the gate blocks on
    # stale quotes) but not self-healing, so it must force a reconnect.
    now = 0.0
    md = _md(clock=lambda: now, stall_reconnect_s=0.04)
    await md.start()
    await md.subscribe(["SPX"])
    now = 100.0  # jump the clock well past the stall threshold
    await asyncio.sleep(0.1)
    assert md.reconnects >= 1
    await md.stop()


async def test_a_fresh_connection_is_not_instantly_declared_stalled():
    # last_event_age() is +inf before the first event; measuring from that alone
    # would make every new connection look dead on arrival.
    md = _md(clock=lambda: 0.0, stall_reconnect_s=0.05)
    await md.start()
    await asyncio.sleep(0.15)
    assert md.reconnects == 0
    await md.stop()


async def test_stall_watchdog_disabled_by_zero():
    md = _md(clock=lambda: 0.0, stall_reconnect_s=0.0)
    await md.start()
    await asyncio.sleep(0.1)
    assert md.reconnects == 0
    await md.stop()


async def test_feed_stalled_is_reported_by_type():
    events: list[tuple[str, dict]] = []
    now = 0.0
    md = _md(clock=lambda: now, stall_reconnect_s=0.04,
             telemetry=lambda c, p: events.append((c, p)))
    await md.start()
    now = 100.0
    await asyncio.sleep(0.1)
    await md.stop()
    assert any(
        p.get("error_type") == FeedStalled.__name__
        for c, p in events if c == "marketdata.disconnected"
    )
