"""The fail-closed staleness rule — one definition, used everywhere.

Market data, account state, greeks, and the pre-trade gate all gate on "is this
too old to act on?" (spec §6). That predicate is safety-critical, so it lives in
exactly one place rather than being re-implemented per call site where the
variants could quietly diverge.

Fail closed: anything not within [0, max_age] is stale — including +inf (never
seen), NaN, and negative ages (clock skew).
"""

from __future__ import annotations

import math


def is_stale(age_s: float, max_age_s: float) -> bool:
    return not (math.isfinite(age_s) and 0.0 <= age_s <= max_age_s)
