"""Account state layer.

Broker is the only source of truth (spec §4). This package provides:

- `models`: domain position/balance types, decoupled from the tastytrade SDK.
- `rest`: authoritative REST snapshots (positions, balances) — the basis of
  reconciliation.
- `cache` + `stream`: an `AlertStreamer`-fed live cache (no polling — listens for
  `PlacedOrder`/`CurrentPosition`/`AccountBalance` events, spec §5), with the same
  fail-closed staleness discipline as market data.
- `fake`: deterministic port impls for tests and the future simulator.
"""

from argus.account.cache import AccountStateCache
from argus.account.fake import FakeAccountState, FakeBrokerAccount
from argus.account.models import AccountSnapshot, Balances, Position
from argus.account.port import AccountStatePort, BrokerAccountPort

__all__ = [
    "Position",
    "Balances",
    "AccountSnapshot",
    "AccountStateCache",
    "AccountStatePort",
    "BrokerAccountPort",
    "FakeAccountState",
    "FakeBrokerAccount",
]
