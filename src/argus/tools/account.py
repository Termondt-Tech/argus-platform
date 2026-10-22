"""Sandbox account + reconciliation smoke tool.

Connects to tastytrade (sandbox), fetches the authoritative positions and
balances, and runs reconciliation against an (empty, by default) expected-position
store — showing exactly what startup's reconciliation rail would decide. Places
no orders.

Usage (with your credentials in .env):

    ./.venv/bin/python -m argus.tools.account
    ./.venv/bin/python -m argus.tools.account --account 5WX00000
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from tastytrade import Account

from argus.account.rest import BrokerAccount
from argus.bootstrap import load_env
from argus.broker import TastytradeSession, trading_credentials
from argus.config import Config
from argus.reconcile import InMemoryPositionStore, Reconciler


async def _run(account_number: str | None, config_path: str) -> None:
    config = Config.load(config_path)
    if config.environment == "live":
        raise SystemExit("refusing to run the account smoke tool against a live session")

    tt = TastytradeSession(config, trading_credentials(config))
    print(f"connecting (is_test={tt.is_test}, env={config.environment}) ...")
    try:
        session = await tt.open()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"login failed: {exc}") from exc

    try:
        accounts = await Account.get(session)
        if not accounts:
            # Diagnose: no accounts at all, or accounts that exist but are closed?
            closed = await Account.get(session, include_closed=True)
            if closed:
                print(f"found {len(closed)} account(s), but none are OPEN:")
                for a in closed:
                    print(f"  {a.account_number} (closed)")
                raise SystemExit("no open trading accounts for this cert login")
            raise SystemExit(
                "this cert/sandbox login has NO trading accounts. tastytrade's cert "
                "environment is separate from production, so the registration needs a "
                "sandbox account provisioned before Argus can reconcile against it."
            )
        print(f"found {len(accounts)} account(s): {', '.join(a.account_number for a in accounts)}")
        if account_number:
            accounts = [a for a in accounts if a.account_number == account_number]
            if not accounts:
                raise SystemExit(f"account {account_number} not found in this login")
        account = accounts[0]
        print(f"using account: {account.account_number}")

        broker = BrokerAccount(session, account)
        positions = await broker.fetch_positions()
        balances = await broker.fetch_balances()
        print(f"net liquidating value: {balances.net_liquidating_value}")
        print(f"broker positions ({len(positions)}):")
        for p in positions:
            print(f"  {p.symbol}  signed_qty={p.signed_quantity}  open={p.average_open_price}")

        # Expected store is empty here (order-recording not built yet), so any open
        # broker position will show as an unexpected-at-broker discrepancy.
        reconciler = Reconciler(broker, InMemoryPositionStore())
        result = await reconciler.reconcile()
        print(f"\nreconciliation ok={result.ok}")
        for d in result.discrepancies:
            print(f"  {d.kind}: {d.symbol} expected={d.expected_qty} broker={d.broker_qty}")
        if not result.ok:
            print("\n(startup would REFUSE to resume on this mismatch — spec §6)")
    finally:
        await tt.aclose()


def main() -> None:
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="tastytrade sandbox account/reconciliation tool")
    parser.add_argument("--account", default=None, help="specific account number (default: first)")
    parser.add_argument(
        "--config",
        required=True,
        help="path to the config file (REQUIRED — there is no default; a default "
        "path is how the live database gets opened by accident)",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.account, args.config))


if __name__ == "__main__":
    main()
