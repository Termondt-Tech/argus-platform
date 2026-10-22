"""The strike ladder, recorded once a minute. (2026-09-29)

TELEMETRY: Tier 2 `signal.ladder`. Nothing in `src/` reads it back (AST-pinned).

**Why it exists.** `signal.candidate` records only the pair the selector chose, and
`signal.none` only its near miss, so no other strike's quote has ever been on the
record. Every question about a different strike has so far been answerable only
through model marks, whose error is the size of the effects being measured. That
covers:
- another `target_short_delta`;
- `min_credit`'s refusals (would the next strike in have paid?);
- what a far out-of-the-money spread was really quoted at when the 15:30 flatten
  fired.
→ the objective audit's P9 (its due-diligence note is in the private research record)

**What a row holds.**
- Per side: every tradeable OUT-OF-THE-MONEY strike (relative to the forward the
  selector uses), nearest first, up to `LADDER_MAX_STRIKES_PER_SIDE`. For each strike:
  bid, ask, and the mid-implied delta and vol.
- The mid-implied numbers come from the SAME functions the short-strike selector runs
  (`strategy._mid_pricing`, `strategy._solve_side`), so a ladder strike's delta is
  exactly the delta the selector would have seen.
- The legs are the snapshot's own, already admitted as quote-fresh by `live_chain`: the
  set the strategy saw on that tick.
- Columnar arrays, one per field, to keep a 40-strike side small.

**When.** The first tick of every ET minute the loop has a chain, all session: before
the entry window, through the flatten, and on blackout days. The minute is claimed
before the row is built, so a failure writes nothing for that minute, logs once, and is
never retried on every tick.

**Never costs a decision.** `maybe_record` swallows every exception from building the
row. A telemetry defect must not take the tick's entry evaluation with it, and
`AuditLog.telemetry` never raises.
"""

from __future__ import annotations

import logging
from datetime import date, time
from typing import Any

from argus.audit.log import AuditLog
from argus.signal.models import CALL, PUT, ChainSnapshot, RegimeSnapshot, payload_number
# The selector's own pricing, imported rather than re-derived: the ladder's mid
# deltas must be the ones `CreditSpreadStrategy` selects on, or the record would
# describe a selector that does not exist.
from argus.signal.strategy import _mid_pricing, _solve_side

logger = logging.getLogger(__name__)

LADDER_CATEGORY = "signal.ladder"

#: The widest a side gets. 40 five-point strikes is 200 points, past the tradeable
#: 0DTE wing on any recorded day. `truncated` says when it was not.
LADDER_MAX_STRIKES_PER_SIDE = 40


def build_ladder(chain: ChainSnapshot, regime: RegimeSnapshot | None) -> dict[str, Any]:
    """One `signal.ladder` payload. Pure; every number through `payload_number`.

    A side with no forward and no usable spot records no strikes (nothing can be
    called out of the money), and `cause` says why. A strike whose mid does not
    invert records `null` for its delta and vol, never a placeholder.
    """
    pricing = _mid_pricing(chain, regime)
    reference = pricing.forward if pricing.forward is not None else chain.underlying_price
    payload: dict[str, Any] = {
        "underlying_price": payload_number(chain.underlying_price),
        "forward": payload_number(pricing.forward),
        "forward_source": pricing.forward_source,
        "cause": pricing.cause,
        "minutes_to_settlement": payload_number(
            regime.minutes_to_settlement if regime is not None else None
        ),
        "atm_iv": payload_number(regime.atm_iv if regime is not None else None),
        "captured_at": payload_number(chain.captured_at),
        "max_strikes_per_side": LADDER_MAX_STRIKES_PER_SIDE,
    }
    for side in (PUT, CALL):
        legs = [o for o in chain.by_right(side) if o.tradeable]
        solved = _solve_side(legs, side, pricing).solved
        otm = sorted(
            (o for o in legs if (o.strike > reference if side == CALL else o.strike < reference)),
            key=lambda o: abs(o.strike - reference),
        )
        kept = otm[:LADDER_MAX_STRIKES_PER_SIDE]
        payload[side] = {
            "strike": [payload_number(o.strike) for o in kept],
            "bid": [payload_number(o.bid) for o in kept],
            "ask": [payload_number(o.ask) for o in kept],
            "mid_delta": [
                payload_number(solved[o.strike].delta if o.strike in solved else None)
                for o in kept
            ],
            "mid_iv": [
                payload_number(solved[o.strike].iv if o.strike in solved else None)
                for o in kept
            ],
            "otm_strikes": len(otm),
            "truncated": len(otm) - len(kept),
        }
    return payload


class LadderRecorder:
    """Writes `signal.ladder` on the first tick of each ET minute. The loop's only
    handle on this module; it returns nothing a caller could decide on."""

    def __init__(self, audit: AuditLog) -> None:
        self._audit = audit
        self._minute: tuple[date, int, int] | None = None

    def maybe_record(
        self,
        chain: ChainSnapshot,
        regime: RegimeSnapshot | None,
        *,
        today: date,
        now_et: time,
    ) -> None:
        minute = (today, now_et.hour, now_et.minute)
        if minute == self._minute:
            return
        self._minute = minute  # claimed first: a failed build is not retried per tick
        try:
            payload = build_ladder(chain, regime)
        except Exception:  # noqa: BLE001 - telemetry must never cost a decision
            logger.exception("strike ladder telemetry failed; skipping this minute")
            return
        self._audit.telemetry(LADDER_CATEGORY, payload)
