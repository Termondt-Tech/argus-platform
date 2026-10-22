"""Ad-hoc, time-boxed shadow run for development (spec §10 step 1).

A THIN wrapper over the same pipeline the always-on supervisor runs
(`argus.session.run_session`, via `Application.run_single_session`),
driven for `--minutes` instead of a full trading day. The loop logic lives in one
place only — this tool just bounds it in time and points it at a dev database.

    ./.venv/bin/python -m argus.tools.shadow --config argus.dev.toml --minutes 30

The ALWAYS-ON path is `python -m argus` (the `argus.service` unit): it runs one
session per trading day on the market calendar, at the open, until the close,
idling in between. Use THIS tool for a quick, bounded run you watch and discard —
and always with `argus.dev.toml`, so it never touches the live audit log.

NOTE on sandbox: cert quotes stream during weekday market hours but are 15-min
DELAYED (and go static outside hours). 0DTE contracts exist under the SPXW root.
A weekday run exercises the full pipeline for real, but decisions are made on
stale prices — so it says nothing about signal QUALITY. Measuring edge needs live
market data (`[marketdata] source = "live"`).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from argus.app import Application
from argus.bootstrap import configure_logging, load_env
from argus.config import Config
from argus.session import _counts

logger = logging.getLogger("argus.tools.shadow")


async def _run(minutes: float, config_path: str, account_number: str | None) -> None:
    config = Config.load(config_path)
    if config.environment == "live":
        raise SystemExit("refusing to run the shadow tool against a live session")

    # The CLI argument still wins; the CONFIG is the fallback. Passing None
    # unconditionally (the pre-2026-09-10 shape) would ignore a pinned config —
    # harmless while dev/sandbox ship unpinned, and wrong the moment anyone pins
    # one (2026-09-10, C9).
    app = Application(
        config, account_number=account_number or config.broker.account_number
    )
    await app.startup()
    try:
        await app.run_single_session(stop_after_s=minutes * 60)
    finally:
        await app.shutdown()

    print("\nstopped.")
    counts = _counts(config.audit.db_path)
    print(f"audit summary: {counts}" if counts else "audit summary: no audit records yet")


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description="Argus time-boxed shadow run (submits nothing)")
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument(
        "--config",
        required=True,
        help="path to the config file (REQUIRED — there is no default; a default "
        "path is how the live database gets opened by accident)",
    )
    parser.add_argument("--account", default=None)
    parser.add_argument("--verbose", action="store_true",
                        help="restore the full SDK websocket DEBUG firehose")
    args = parser.parse_args()
    configure_logging(args.verbose)
    try:
        asyncio.run(_run(args.minutes, args.config, args.account))
    except KeyboardInterrupt:
        print("\ninterrupted.")


if __name__ == "__main__":
    main()
