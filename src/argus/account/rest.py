"""Authoritative account state via REST (tastytrade `Account`).

`BrokerAccount` wraps a tastytrade `Account` + `Session` and maps SDK objects to
domain types. It is the authoritative source for reconciliation (positions) and
for seeding the live cache. The SDK objects and session are injected, so the
mapping is unit-testable against real `CurrentPosition`/`AccountBalance` objects
without a network.
"""

from __future__ import annotations

from typing import Any

from argus.account.models import Balances, Position, map_balances, map_position


class BrokerAccount:
    def __init__(self, session: Any, account: Any) -> None:
        # session: tastytrade Session; account: tastytrade Account.
        self._session = session
        self._account = account

    @property
    def account_number(self) -> str:
        return getattr(self._account, "account_number", "")

    async def fetch_positions(self) -> list[Position]:
        raw = await self._account.get_positions(self._session)
        return [map_position(cp) for cp in raw]

    async def fetch_balances(self) -> Balances:
        raw = await self._account.get_balances(self._session)
        return map_balances(raw)
