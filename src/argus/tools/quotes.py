"""Sandbox connectivity smoke tool.

Connects to tastytrade (sandbox, because config `environment` is shadow/sandbox),
subscribes to the symbols you pass, and prints live quotes + measured age for a
few seconds. This is the manual step that validates the market-data adapter and
your app registration against the real endpoint — the part that can't be
unit-tested.

Usage (with your credentials in .env or the environment):

    TT_SECRET=... TT_REFRESH=... ./.venv/bin/python -m argus.tools.quotes SPX .SPX
    ./.venv/bin/python -m argus.tools.quotes --seconds 15 SPXW250718P05000000

It never places an order and never leaves shadow/sandbox.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from argus.bootstrap import configure_logging, load_env
from argus.broker import SessionRole, TastytradeSession, market_data_credentials
from argus.config import Config
from argus.marketdata.dxlink import DXLinkMarketData


async def _run(symbols: list[str], seconds: float, config_path: str) -> None:
    config = Config.load(config_path)
    if config.environment == "live":
        raise SystemExit("refusing to run the quote smoke tool against a live session")

    tt = TastytradeSession(
        config, market_data_credentials(config), role=SessionRole.MARKET_DATA
    )
    print(
        f"connecting market data: source={config.marketdata.source} "
        f"(is_test={tt.is_test}, READ-ONLY) ..."
    )
    try:
        session = await tt.open()  # constructs, enters client context, fetches token
    except Exception as exc:  # noqa: BLE001 — surface a concise message, not a traceback
        raise SystemExit(f"login failed (check credentials / registration): {exc}") from exc
    if not await tt.validate():
        await tt.aclose()
        raise SystemExit("session token obtained but /sessions/validate failed")
    print("session valid.")

    md = DXLinkMarketData(tt.market_data_session, clock=time.monotonic)
    await md.start()
    try:
        await md.subscribe(symbols)
        print(f"subscribed to {symbols}; streaming for {seconds:.0f}s ...")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            for sym in symbols:
                q = md.latest(sym)
                if q is None:
                    print(f"  {sym}: (no quote yet)  age={md.age(sym)}")
                else:
                    print(
                        f"  {sym}: bid={q.bid} ask={q.ask} "
                        f"bidsz={q.bid_size} asksz={q.ask_size} age={md.age(sym):.3f}s"
                    )
    finally:
        await md.stop()
        await tt.aclose()
        print("stopped.")


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description="tastytrade sandbox quote smoke tool")
    parser.add_argument("symbols", nargs="+", help="dxfeed streamer symbols to subscribe to")
    parser.add_argument("--seconds", type=float, default=10.0, help="how long to stream")
    parser.add_argument(
        "--config",
        required=True,
        help="path to the config file (REQUIRED — there is no default; a default "
        "path is how the live database gets opened by accident)",
    )
    parser.add_argument("--verbose", action="store_true",
                        help="restore the full SDK websocket DEBUG firehose")
    args = parser.parse_args()
    configure_logging(args.verbose)
    asyncio.run(_run(args.symbols, args.seconds, args.config))


if __name__ == "__main__":
    main()
