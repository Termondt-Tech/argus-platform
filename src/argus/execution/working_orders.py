"""Working-order exposure for the pre-trade concurrency cap (Finding 6, 2026-08-28).

`RiskSnapshot.open_spread_equivalent` counts FILLED positions — broker legs and
the ledger's per-idea count. Neither sees an order that has been sent and has not
filled yet. Normally the price-walking loop owns that window for a few seconds;
when a walk abandons an order whose death it cannot confirm (`_request_cancel` is
best-effort — 2026-08-28's order 1424563 sat `working` for two hours after a
failed cancel), the window runs to the close. Had it filled it would have been a
third spread against a cap of two.

This module turns `get_live_orders()` — the SAME broker-derived source the
duplicate guard and restart-time reconciliation already trust — into a count of
distinct trade ideas with an OPENING order still working, which the watchdog polls
on its refresh cadence and feeds to the gate.

**Opening orders only.** An exit's own order is CLOSING a position that
`open_spread_equivalent` already counts; adding it would UNDER-count available
concurrency for new entries at exactly the wrong moment (and entries and exits are
different call sites anyway — the gate only ever runs before an entry). The filter
is `direction is OrderDirection.OPEN`.

**Per trade idea, by `external_identifier`.** A walk reprices by cancel+replace —
a new broker order id each step, the same `external_identifier` — so several rungs
can momentarily read WORKING under one idea, and that is still one spread. This
matches how every other concurrency count in the system is defined
(`docs/decisions/2026-08-17-overlapping-verticals.md`).

**Unmappable orders count too.** An order Argus cannot read is "working or worse"
for every purpose the concurrency rail has — the same fail-closed posture
`DuplicateOrderGuard` takes for an unmappable order under the identifier it is
checking. Direction is unknowable for these, so they are counted regardless.

**The relabelling residual is CLOSED (2026-09-03).** `map_placed_order` now takes
`direction` from the identifier Argus minted and falls back to `infer_direction`
only for foreign orders
(docs/decisions/2026-08-25-relabelled-closing-legs-open.md), so a relabelled
closing order no longer reads OPEN here. This module needed NO code change — it
reads `Order.direction`, which that fix corrected by construction — and
`tests/test_working_orders.py` proves it on the 2026-09-01 shape rather than
assuming it.
"""

from __future__ import annotations

from argus.execution.build import parse_candidate_key
from argus.execution.models import (
    LiveOrders,
    OrderDirection,
    is_argus_identifier,
    parse_external_identifier,
)
from argus.risk.loss_budget import CONTRACT_MULTIPLIER, Exposure, trade_idea_key


def open_working_spread_equivalents(live: LiveOrders) -> int:
    """Distinct Argus trade ideas with an OPENING order still working at the broker.

    Counts toward `max_concurrent_positions` on top of the position-derived count —
    see the module docstring for why opening-only and why per-identifier.
    """
    ideas: set[str] = set()
    for order in live.orders:
        if (
            order.is_working
            and order.direction is OrderDirection.OPEN
            and is_argus_identifier(order.external_identifier)
        ):
            ideas.add(order.external_identifier)
    for unreadable in live.unmappable:
        # No direction to filter on; an unreadable order is working-or-worse.
        if is_argus_identifier(unreadable.external_identifier):
            ideas.add(unreadable.external_identifier)
    return len(ideas)



def side_of_identifier(identifier: str | None) -> str | None:
    """`"put"` / `"call"` from an identifier ARGUS MINTED, or None if it says none.

    The side comes from the candidate key inside the identifier, never from the
    broker's leg actions (which the broker relabels). None means "cannot say", and
    every per-side count counts such an idea as `"unknown"` (2026-09-27 review).
    """
    parsed = parse_external_identifier(identifier or "")
    if parsed is None:
        return None
    key = parse_candidate_key(parsed[1])
    return None if key is None else key[1]


def open_working_spreads_by_side(live: LiveOrders) -> dict[str, int]:
    """`open_working_spread_equivalents`, split by side, for the side-balance rule.

    Same filter, same per-identifier dedup. An identifier whose side cannot be read
    (a malformed key) is counted under `"unknown"`, never on a side: it is
    working-or-worse exposure on a side nobody knows, and the side rule assumes the
    worst side for it. Counting it on BOTH sides, as the first cut did, is neutral for
    a rule on `puts − calls` (2026-09-27 review).
    """
    ideas: dict[str, set[str]] = {"put": set(), "call": set(), "unknown": set()}

    def add(identifier: str) -> None:
        ideas[side_of_identifier(identifier) or "unknown"].add(identifier)

    for order in live.orders:
        if (
            order.is_working
            and order.direction is OrderDirection.OPEN
            and is_argus_identifier(order.external_identifier)
        ):
            add(order.external_identifier)
    for unreadable in live.unmappable:
        if is_argus_identifier(unreadable.external_identifier):
            add(unreadable.external_identifier)
    return {side: len(ids) for side, ids in ideas.items()}

def open_working_worst_case(live: LiveOrders) -> Exposure:
    """Worst case of the OPENING orders still working, per trade idea. (2026-09-21)

    The loss budget's half of this module: each working opening order counted as if
    it filled in full at ZERO credit, `width × 100 × contracts` — the most it could
    add to the day's worst case. Same filter as the count above (Argus's own, opening,
    working); per idea the LARGEST rung, since a walk's rungs are one trade idea and
    a replacement carries the remainder rather than adding to it.

    Fails closed as a whole: an unmappable Argus order, or one whose width or trade
    idea cannot be read, is exposure that cannot be bounded, and the answer is
    "cannot say" rather than a partial sum.
    """
    for unreadable in live.unmappable:
        if is_argus_identifier(unreadable.external_identifier):
            return Exposure.cannot_say("working_order_unmappable")
    worst: dict[str, float] = {}
    for order in live.orders:
        if not (
            order.is_working
            and order.direction is OrderDirection.OPEN
            and is_argus_identifier(order.external_identifier)
        ):
            continue
        key = trade_idea_key(order.external_identifier)
        width = order.width
        if key is None or width is None or width <= 0:
            return Exposure.cannot_say("working_order_unreadable")
        dollars = float(width * CONTRACT_MULTIPLIER * order.contracts)
        worst[key] = max(worst.get(key, 0.0), dollars)
    return Exposure(by_idea=worst)
