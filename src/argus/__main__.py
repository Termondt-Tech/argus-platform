"""Entry point: `python -m argus` — the always-on supervisor, for every strategy.

Loads config and runs the `SessionScheduler` until a stop signal: one session per
trading day, on the market calendar, idling in between.

**Which strategy:** `ARGUS_STRATEGY`, a literal in the strategy's own compose
file (its identity, never a per-host choice). `STRATEGIES` is the registry: a
literal table, no discovery (2026-10-07, "platform by extraction"). UNSET is the
SPX book, exactly as before the registry existed, which is not a default config:
it still needs `ARGUS_ENV` and a matching `argus.<profile>.toml`, and no other
strategy's container mounts one. Each strategy's entry point imports its package
INSIDE the function, so the SPX process never loads another strategy's code
(`tests/test_strategy_registry.py`). → docs/decisions/2026-10-22-one-platform-a-strategy-per-process.md

**There is no default config path.** `ARGUS_ENV` names the PROFILE
(`live` -> `argus.live.toml`, resolved against `ARGUS_CONFIG_DIR`), or
`ARGUS_CONFIG` gives an explicit path; neither set is a `ConfigError`, not a
fallback to `argus.toml`. See `config.resolve_config_path` for why that default
was removed rather than made safer.

The account number is passed EXPLICITLY rather than read from `config` inside
`Application`: the injection seam is what `tools/shadow.py` uses, and a supervisor
that quietly re-derived it would give two callers two different answers. Dropping
this argument restores the `accounts[0]` lottery silently — `tests/test_main_wiring.py`
is what stops a tidy from doing that.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable

from argus.app import Application
from argus.bootstrap import load_env
from argus.config import Config, ConfigError

# The tastytrade SDK logs every outgoing websocket frame at DEBUG — including the
# AlertStreamer subscription frame carrying a live `Bearer <session_token>`. Now
# that the always-on loop actually opens those streams, an operator setting
# ARGUS_LOG_LEVEL=DEBUG would land that token in stdout and journald. Pin these
# loggers to WARNING regardless of the root level so Argus's own DEBUG stays
# usable without leaking a credential. (Matches bootstrap.configure_logging.)
_NOISY_LOGGERS = ("tastytrade", "httpx", "httpx2", "httpcore", "websockets")


#: Strategy -> its always-on entry point. Unset `ARGUS_STRATEGY` is the SPX book.
STRATEGIES: dict[str, Callable[[], None]] = {}


def main() -> None:
    load_env()
    strategy = os.environ.get("ARGUS_STRATEGY")
    if strategy is not None:
        run = STRATEGIES.get(strategy)
        if run is None:
            raise ConfigError(
                f"ARGUS_STRATEGY={strategy!r} is not a strategy this build runs: "
                f"{sorted(STRATEGIES)} (unset is the SPX book)"
            )
        run()
        return
    logging.basicConfig(
        level=os.environ.get("ARGUS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    config = Config.load()
    app = Application(config, account_number=config.broker.account_number)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
