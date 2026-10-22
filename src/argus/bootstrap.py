"""Process bootstrap helpers (env loading).

Loading `.env` is a local-dev convenience. Real env vars always win — `load_dotenv`
does not override values already present in the environment — so production secret
injection (systemd `EnvironmentFile`, container env, a secret manager) is
unaffected and takes precedence. Secrets never get logged here; only whether a
file was found.
"""

from __future__ import annotations

import logging

from dotenv import find_dotenv, load_dotenv

logger = logging.getLogger("argus.bootstrap")


def configure_logging(verbose: bool = False) -> None:
    """Console logging for the dev tools.

    The tastytrade SDK logs every websocket frame at DEBUG — hundreds of lines a
    second once an option chain is subscribed — which buries our own status
    output entirely. Default third-party loggers to WARNING and Argus's own to
    INFO; `verbose` restores the full firehose for protocol debugging.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("argus").setLevel(logging.DEBUG if verbose else logging.INFO)
    for noisy in ("tastytrade", "httpx", "httpx2", "httpcore", "websockets"):
        # Set explicitly: these set their own level, so basicConfig alone won't
        # quiet them.
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)


def load_env() -> bool:
    """Load `.env` from the project root (or nearest parent) into the environment.

    Returns True if a file was found and loaded. Existing env vars are not
    overridden.
    """
    path = find_dotenv(usecwd=True)
    if not path:
        logger.debug("no .env file found; relying on the ambient environment")
        return False
    load_dotenv(path, override=False)
    logger.info("loaded environment from %s", path)
    return True
