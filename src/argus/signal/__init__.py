"""Signal engine.

Produces 0DTE SPX credit-spread entry candidates from an option-chain snapshot.
Structured as a pluggable `Strategy` over a `ChainSnapshot`, so the (transparent,
config-driven) baseline rule can be tuned or replaced from shadow-mode data
without touching the orchestration loop or the risk gate.

IMPORTANT: the baseline `CreditSpreadStrategy` is NOT a validated edge — it is a
mechanism for generating candidates whose quality shadow mode exists to measure
(spec §10, §11). Real parameters come from your own shadow/backtest data.
"""

from argus.signal.chain import ChainSource, FakeChainSource
from argus.signal.models import ChainSnapshot, OptionQuote, SpreadSignal, StrategyResult
from argus.signal.strategy import CreditSpreadStrategy, Strategy

__all__ = [
    "OptionQuote",
    "ChainSnapshot",
    "SpreadSignal",
    "StrategyResult",
    "Strategy",
    "CreditSpreadStrategy",
    "ChainSource",
    "FakeChainSource",
]
