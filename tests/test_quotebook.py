"""QuoteBook: staleness accounting, fail-closed on unknown symbols."""

from __future__ import annotations

import math

from argus.marketdata.book import QuoteBook


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_unknown_symbol_is_stale_and_infinite_age():
    book = QuoteBook(FakeClock())
    assert book.latest("SPX") is None
    assert book.age("SPX") == math.inf
    assert book.is_stale("SPX", 5.0) is True  # fail closed
    assert book.last_event_age() == math.inf


def test_fresh_quote_not_stale():
    clock = FakeClock(100.0)
    book = QuoteBook(clock)
    book.update("SPX", 1.0, 1.2, 3, 4)
    assert book.age("SPX") == 0.0
    clock.t = 101.0
    assert book.age("SPX") == 1.0
    assert book.is_stale("SPX", 2.0) is False


def test_quote_becomes_stale_past_threshold():
    clock = FakeClock(0.0)
    book = QuoteBook(clock)
    book.update("SPX", 1.0, 1.2, 3, 4)
    clock.t = 3.0
    assert book.is_stale("SPX", 2.0) is True  # 3s old, 2s tolerance


def test_last_event_age_tracks_any_symbol():
    clock = FakeClock(0.0)
    book = QuoteBook(clock)
    book.update("A", 1, 2, 1, 1)
    clock.t = 5.0
    book.update("B", 3, 4, 1, 1)  # a fresh event resets last_event_age
    clock.t = 6.0
    assert book.last_event_age() == 1.0
    assert book.age("A") == 6.0  # A itself is now old


def test_update_overwrites_and_refreshes():
    clock = FakeClock(0.0)
    book = QuoteBook(clock)
    book.update("SPX", 1.0, 1.2, 3, 4)
    clock.t = 10.0
    q = book.update("SPX", 1.1, 1.3, 5, 6)
    assert q.bid == 1.1 and q.ask == 1.3
    assert book.age("SPX") == 0.0
