"""Option-chain diagnostic.

Answers "why did the shadow run find no 0DTE contracts?" by dumping what the
broker's chain endpoint actually returns: which expirations exist, their roots
(SPX vs SPXW), strike coverage, and whether streamer symbols are populated
(a contract with no streamer symbol can never be quoted, so it gets dropped).

    ./.venv/bin/python -m argus.tools.chain
    ./.venv/bin/python -m argus.tools.chain --underlying SPX --limit 12
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from tastytrade.instruments import get_option_chain

from argus.bootstrap import configure_logging, load_env
from argus.broker import SessionRole, TastytradeSession, market_data_credentials
from argus.clock import et_today
from argus.config import Config
from argus.signal.contracts import map_option


async def _run(underlying: str, config_path: str, limit: int) -> None:
    config = Config.load(config_path)
    if config.environment == "live":
        raise SystemExit("refusing to run the chain tool against a live session")

    tt = TastytradeSession(
        config, market_data_credentials(config), role=SessionRole.MARKET_DATA
    )
    print(f"connecting (is_test={tt.is_test}) ...")
    await tt.open()
    try:
        today = et_today()
        print(f"ET trading date: {today} ({today.strftime('%A')})\n")

        chain = await get_option_chain(tt.market_data_session, underlying)
        if not chain:
            raise SystemExit(f"chain endpoint returned NOTHING for {underlying}")

        expirations = sorted(chain)
        print(f"{underlying}: {len(expirations)} expiration(s) available")
        print(f"  earliest: {expirations[0]}   latest: {expirations[-1]}\n")

        for exp in expirations[:limit]:
            options = chain[exp]
            roots = sorted({getattr(o, "root_symbol", "?") or "?" for o in options})
            strikes = sorted({float(o.strike_price) for o in options})
            mapped = [m for m in (map_option(o) for o in options) if m is not None]
            no_streamer = len(options) - len(mapped)
            marker = "  <-- TODAY (0DTE)" if exp == today else ""
            print(
                f"  {exp} ({exp.strftime('%a')}): {len(options):5d} contracts  "
                f"roots={roots}  strikes {strikes[0]:g}-{strikes[-1]:g}  "
                f"unusable(no streamer_symbol)={no_streamer}{marker}"
            )
        if len(expirations) > limit:
            print(f"  ... and {len(expirations) - limit} more")

        print()
        if today in chain:
            sample = [m for m in (map_option(o) for o in chain[today]) if m is not None][:3]
            print(f"0DTE PRESENT for {today}: {len(chain[today])} contracts. Samples:")
            for c in sample:
                print(f"   {c.right:4s} {c.strike:g}  occ={c.symbol}  streamer={c.streamer_symbol}")
        else:
            print(f"NO 0DTE: the chain has no expiration dated {today}.")
            future = [e for e in expirations if e >= today]
            if future:
                print(f"   nearest available expiry on/after today: {future[0]} "
                      f"({future[0].strftime('%A')})")
            print("   => cert's instrument data likely doesn't carry today's weekly "
                  "expiry, which is a sandbox data limitation, not a code issue.")
    finally:
        await tt.aclose()


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description="tastytrade option-chain diagnostic")
    parser.add_argument("--underlying", default="SPXW",
                        help="option ROOT (SPXW carries 0DTE; SPX is monthlies only)")
    parser.add_argument(
        "--config",
        required=True,
        help="path to the config file (REQUIRED — there is no default; a default "
        "path is how the live database gets opened by accident)",
    )
    parser.add_argument("--limit", type=int, default=10, help="expirations to list")
    parser.add_argument("--verbose", action="store_true",
                        help="restore the full SDK websocket DEBUG firehose")
    args = parser.parse_args()
    configure_logging(args.verbose)
    asyncio.run(_run(args.underlying, args.config, args.limit))


if __name__ == "__main__":
    main()
