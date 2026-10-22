"""Deterministic fakes for the account layer — tests and simulator seed.

`FakeAccountState` is a clock-controlled `AccountStatePort`. `FakeBrokerAccount`
is a `BrokerAccountPort` returning canned positions — enough to drive the
reconciler through every discrepancy path without a broker.
"""

from __future__ import annotations

from argus.account.models import AccountSnapshot, Balances, Position


class FakeAccountState:
    def __init__(self, start_time: float = 0.0) -> None:
        self._now = start_time
        self._positions: dict[str, Position] = {}
        self._net_liq: float | None = None
        self._last_event_at: float | None = None

    # virtual-time controls
    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move virtual time backwards")
        self._now += seconds

    def set_position(self, position: Position) -> None:
        self._last_event_at = self._now
        if position.signed_quantity == 0:
            self._positions.pop(position.symbol, None)
        else:
            self._positions[position.symbol] = position

    def set_net_liq(self, value: float) -> None:
        self._last_event_at = self._now
        self._net_liq = value

    # AccountStatePort
    def snapshot(self) -> AccountSnapshot | None:
        if self._last_event_at is None:
            return None
        return AccountSnapshot(
            positions=tuple(self._positions.values()),
            net_liquidating_value=self._net_liq,
            captured_at=self._now,
        )

    def age(self) -> float:
        import math

        if self._last_event_at is None:
            return math.inf
        return max(0.0, self._now - self._last_event_at)

    def is_stale(self, max_age_s: float) -> bool:
        import math

        age = self.age()
        return not (math.isfinite(age) and age <= max_age_s)


class FakeBrokerAccount:
    def __init__(
        self,
        positions: list[Position] | None = None,
        net_liquidating_value: float = 100_000.0,
    ) -> None:
        self._positions = list(positions or [])
        self._net_liq = net_liquidating_value

    def set_positions(self, positions: list[Position]) -> None:
        self._positions = list(positions)

    def set_net_liq(self, value: float) -> None:
        self._net_liq = value

    # BrokerAccountPort — what reconciliation and the initial seed use.
    async def fetch_positions(self) -> list[Position]:
        return list(self._positions)

    async def fetch_balances(self) -> Balances:
        return Balances(net_liquidating_value=self._net_liq)

    # AccountRefreshPort — one call, one attempt. The REAL `BrokerAccount`
    # deliberately does NOT have this (it has no bound of its own, and the port's
    # contract is "bound yourself"); the fake does, because a fake in a test is
    # instantaneous and has nothing to bound.
    async def fetch_snapshot(self) -> tuple[list[Position], Balances]:
        return list(self._positions), Balances(net_liquidating_value=self._net_liq)
