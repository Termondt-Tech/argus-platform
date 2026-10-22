"""FakeMarketData: deterministic virtual-time behavior and port conformance."""

from __future__ import annotations

import math

import pytest

from argus.marketdata import FakeMarketData, MarketDataPort


async def test_conforms_to_port():
    md = FakeMarketData()
    assert isinstance(md, MarketDataPort)


async def test_push_and_read_with_virtual_time():
    md = FakeMarketData(start_time=1000.0)
    md.push("SPX", bid=1.0, ask=1.2, bid_size=5, ask_size=7)
    q = md.latest("SPX")
    assert q is not None and q.bid == 1.0 and q.ask == 1.2
    assert md.age("SPX") == 0.0
    md.advance(2.5)
    assert md.age("SPX") == 2.5
    assert md.is_stale("SPX", 2.0) is True
    assert md.is_stale("SPX", 3.0) is False


async def test_unknown_symbol_fail_closed():
    md = FakeMarketData()
    assert md.latest("NOPE") is None
    assert md.age("NOPE") == math.inf
    assert md.is_stale("NOPE", 1.0) is True


async def test_advance_rejects_negative():
    md = FakeMarketData()
    with pytest.raises(ValueError):
        md.advance(-1.0)


async def test_lifecycle_and_subscription():
    md = FakeMarketData()
    assert md.running is False
    await md.start()
    assert md.running is True
    await md.subscribe(["SPX", "SPXW"])
    assert md.subscribed == frozenset({"SPX", "SPXW"})
    await md.stop()
    assert md.running is False
