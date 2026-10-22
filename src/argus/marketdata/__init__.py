"""Market data layer.

Structured as a port (`MarketDataPort`) with interchangeable backings so the
signal engine, pre-trade gate, and execution loop only ever talk to the seam:

    MarketDataPort
      ├─ DXLinkMarketData  (tastytrade DXLink — sandbox now, live later)
      └─ FakeMarketData    (deterministic, clock-controlled — tests + simulator seed)

The deterministic `FakeMarketData` is the seed for the future tastytrade
simulator; growing it into a full `SimBroker` requires no change to any caller.
"""

from argus.marketdata.book import GreeksBook, QuoteBook
from argus.marketdata.fake import FakeMarketData
from argus.marketdata.models import OptionGreeks, Quote
from argus.marketdata.port import MarketDataPort

__all__ = [
    "Quote",
    "OptionGreeks",
    "QuoteBook",
    "GreeksBook",
    "MarketDataPort",
    "FakeMarketData",
]
