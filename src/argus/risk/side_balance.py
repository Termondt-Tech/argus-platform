"""The side-balance rule: which side a new spread may open on. (2026-09-26)

With more than one concurrent slot, the book should not lean one way. Two credit
verticals on the SAME side move together (measured +0.62 to +0.69 at settlement,
+0.65 to +0.69 under live exits) and one put plus one call offset (−0.26 to −0.59),
so book variance rises with the square of the imbalance
(docs/designs/2026-09-26-multi-spread-side-balance.md §3.2). The rule:

  1. **Admissibility.** An entry is allowed only if afterwards
     `|puts − calls| <= max_side_imbalance`. At two slots and an imbalance of 1 that
     is "at most one open spread per side".
  2. **Priority.** Among the sides that qualify on a tick, the one leaving the book
     more balanced goes first; a tie goes in `signal.sides` order (put first).

**Pure.** Counts in, a decision out. It never fetches, so the orchestrator (which
chooses) and `PreTradeGate` (which refuses) apply the SAME function to the SAME
`RiskSnapshot` and cannot disagree.

**Counted per side exactly as the concurrency cap counts in total**
(`RiskSnapshot.open_spread_equivalent`): the higher of the broker's legs (per right,
ceiling of half) and the ledger's trade ideas, plus opening orders still working. So
a chained pair — which nets a shared strike to zero at the broker — is still two
from the ledger, and a leg the ledger never booked still counts from the broker.
Never count by symbol alone (CLAUDE.md's fourth landmine).

**An idea whose side cannot be read is its own count, `unknown`, and admissibility
assumes the WORST side for it** (2026-09-27 review). The first cut counted it against
BOTH sides, which reads as fail-closed and is exactly neutral: adding one to each side
leaves `puts − calls` unchanged, so it changed no verdict — an unknown idea that was
really a put let a second put open. Whatever side the unknowns turn out to be, the
imbalance after the entry is at most `|known imbalance after| + unknown`, so that is
what is held to the band. It refuses only what some reading of the unknowns would
put outside it: with a put held and one unknown, a call is still admissible (the
book is 2–1 or 1–2 either way).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import TypeVar

PUT = "put"
CALL = "call"
SIDES = (PUT, CALL)
#: The key an idea, leg or order whose side cannot be read is counted under.
UNKNOWN = "unknown"

#: The gate's refusal reason, and the name `signal.candidate`'s `book` block uses.
REFUSAL = "side_imbalance_exceeded"

T = TypeVar("T")


def count_by_side(
    *,
    broker_legs: Mapping[str, int],
    ledger_ideas: Mapping[str, int],
    working_ideas: Mapping[str, int],
) -> dict[str, int]:
    """Open spreads per side, by `open_spread_equivalent`'s own formula, per side,
    plus `unknown`: the spreads whose side could not be read.

    `broker_legs` counts leg SYMBOLS per right (not contracts), exactly like
    `open_position_count`; a half-filled vertical rounds UP to one, the safe
    direction. Each input carries its unreadable ones under `UNKNOWN`, never on a side.

    `unknown` is an upper bound on how far they can move the imbalance, never the
    `max()` the sides take: `ceil(unknown legs / 2)` (any split of them across the two
    rights moves the difference by at most that) plus every unknown ledger and working
    idea (at most one each). Over-counting here can only refuse more.
    """
    counts = {
        side: max(math.ceil(broker_legs.get(side, 0) / 2), ledger_ideas.get(side, 0))
        + working_ideas.get(side, 0)
        for side in SIDES
    }
    counts[UNKNOWN] = (
        math.ceil(broker_legs.get(UNKNOWN, 0) / 2)
        + ledger_ideas.get(UNKNOWN, 0)
        + working_ideas.get(UNKNOWN, 0)
    )
    return counts


def imbalance_after(side: str, *, puts: int, calls: int) -> int:
    """`puts − calls` once one more spread opens on `side`."""
    return puts - calls + (1 if side == PUT else -1)


def admissible(
    side: str, *, puts: int, calls: int, unknown: int, max_imbalance: int
) -> bool:
    """Whether one more spread on `side` keeps `|puts − calls| <= max_imbalance` for
    EVERY side the `unknown` spreads could be on.

    `unknown` is REQUIRED (2026-09-27 review): a default of 0 is the silent way to
    re-open the neutral "count it on both sides" defect at a new call site.
    """
    return abs(imbalance_after(side, puts=puts, calls=calls)) + unknown <= max_imbalance


def rank(
    candidates: Sequence[T],
    *,
    side_of: Callable[[T], str],
    puts: int,
    calls: int,
    unknown: int,
    max_imbalance: int,
    sides_order: Sequence[str],
) -> list[T]:
    """The ADMISSIBLE candidates, most balancing first, ties in `sides_order`.

    Stable, so candidates on one side keep the order they arrived in. A side not in
    `sides_order` sorts last rather than raising (fail toward not choosing it).
    Priority is on the KNOWN counts; `unknown` only narrows what is admissible.
    """
    def key(c: T) -> tuple[int, int]:
        side = side_of(c)
        order = sides_order.index(side) if side in sides_order else len(sides_order)
        return abs(imbalance_after(side, puts=puts, calls=calls)), order

    return sorted(
        (c for c in candidates
         if admissible(side_of(c), puts=puts, calls=calls, unknown=unknown,
                       max_imbalance=max_imbalance)),
        key=key,
    )
