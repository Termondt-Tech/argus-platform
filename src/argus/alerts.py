"""Alerting seam.

Everything that needs to raise a human-facing alert (audit queue depth, event-loop
stalls, later: fill failures, reconciliation mismatches) calls an `AlertFn`. For
now the default just logs; the phone-reachable alerting path (spec §6, §9) plugs
in here later by passing a different `AlertFn` at process startup.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("argus.alert")

AlertFn = Callable[[str, dict[str, Any]], None]


def log_alert(kind: str, detail: dict[str, Any]) -> None:
    logger.warning("ALERT %s %s", kind, detail)
