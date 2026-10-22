"""DXLinkMarketData._ingest: wire (dxfeed.Quote) -> domain mapping, offline.

Uses real `dxfeed.Quote` objects (via model_construct, to skip the many required
wire fields) so the mapping is tested against the actual SDK type, not a mock.
"""

from __future__ import annotations

from decimal import Decimal

from tastytrade.dxfeed import Quote as DxQuote

from argus.marketdata.dxlink import DXLinkMarketData


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _md(clock):
    # session is unused by _ingest; pass a sentinel and never call start().
    return DXLinkMarketData(session=object(), clock=clock)


def _quote(**fields) -> DxQuote:
    base = dict(bid_price=Decimal("0"), ask_price=Decimal("0"), bid_size=Decimal("0"), ask_size=Decimal("0"))
    base.update(fields)
    return DxQuote.model_construct(**base)


def test_ingest_maps_decimal_prices_to_float():
    md = _md(FakeClock(50.0))
    md._ingest(_quote(event_symbol="SPX", bid_price=Decimal("1.20"), ask_price=Decimal("1.30"),
                      bid_size=Decimal("5"), ask_size=Decimal("7")))
    q = md.latest("SPX")
    assert q is not None
    assert (q.bid, q.ask, q.bid_size, q.ask_size) == (1.20, 1.30, 5.0, 7.0)
    assert isinstance(q.bid, float)
    assert md.age("SPX") == 0.0


def test_ingest_floors_nan_side_to_zero():
    md = _md(FakeClock())
    # A one-sided/NaN quote must not produce NaN in the book (would break the
    # zero-bid guard and JSON audit later). Missing side -> 0.0.
    md._ingest(_quote(event_symbol="SPXW", bid_price=Decimal("NaN"), ask_price=Decimal("2.50")))
    q = md.latest("SPXW")
    assert q is not None
    assert q.bid == 0.0
    assert q.ask == 2.50
    assert q.has_bid is False


def test_ingest_handles_none_prices():
    md = _md(FakeClock())
    md._ingest(_quote(event_symbol="X", bid_price=None, ask_price=None))
    q = md.latest("X")
    assert q is not None and q.bid == 0.0 and q.ask == 0.0


def test_ingest_updates_staleness():
    clock = FakeClock(0.0)
    md = _md(clock)
    md._ingest(_quote(event_symbol="SPX", bid_price=Decimal("1"), ask_price=Decimal("2")))
    clock.t = 3.0
    assert md.is_stale("SPX", 2.0) is True
    assert md.last_event_age() == 3.0
