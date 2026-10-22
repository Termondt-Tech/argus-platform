"""AccountStateCache: live state + fail-closed staleness."""

from __future__ import annotations

import math

from argus.account.cache import AccountStateCache
from argus.account.models import Position


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _pos(symbol, qty):
    return Position(symbol, "SPX", "Equity Option", qty, 1.20, 0.9)


def test_no_events_is_stale_and_no_snapshot():
    cache = AccountStateCache(FakeClock())
    assert cache.snapshot() is None
    assert cache.age() == math.inf
    assert cache.is_stale(5.0) is True


def test_position_and_net_liq_snapshot():
    clock = FakeClock(100.0)
    cache = AccountStateCache(clock)
    cache.update_position(_pos("SPXW1", -1))
    cache.update_net_liq(100_000.0)
    snap = cache.snapshot()
    assert snap is not None
    assert snap.open_position_count == 1
    assert snap.net_liquidating_value == 100_000.0
    assert snap.position_map() == {"SPXW1": -1}


def test_flat_position_event_closes_it():
    cache = AccountStateCache(FakeClock())
    cache.update_position(_pos("SPXW1", -1))
    cache.update_position(_pos("SPXW1", 0))  # closed
    assert cache.snapshot().open_position_count == 0
    assert cache.snapshot().position_map() == {}


def test_staleness_tracks_last_event():
    clock = FakeClock(0.0)
    cache = AccountStateCache(clock)
    cache.update_net_liq(50_000.0)
    clock.t = 6.0
    assert cache.age() == 6.0
    assert cache.is_stale(5.0) is True
    assert cache.is_stale(7.0) is False
