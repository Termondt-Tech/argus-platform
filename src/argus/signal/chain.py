"""ChainSource port — supplies the 0DTE `ChainSnapshot` to the signal engine.

The real implementation (future) builds the snapshot from the tastytrade
instruments API (today's SPXW strikes/symbols) joined with live DXLink `Quote` and
`Greeks` events. It is a port so the strategy and loop stay testable with a
deterministic fake, and so the future simulator can feed synthetic chains.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from argus.signal.models import ChainSnapshot


@runtime_checkable
class ChainSource(Protocol):
    def snapshot(self) -> ChainSnapshot | None:
        """Current 0DTE chain, or None if not yet available / stale."""


class FakeChainSource:
    """Deterministic chain source for tests and the simulator seed."""

    def __init__(self, snapshot: ChainSnapshot | None = None) -> None:
        self._snapshot = snapshot

    def set(self, snapshot: ChainSnapshot | None) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> ChainSnapshot | None:
        return self._snapshot
