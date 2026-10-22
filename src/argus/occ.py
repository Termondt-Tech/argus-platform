"""OCC option-symbol parsing — the expiry a leg symbol carries.

Argus keys positions and order legs by the broker's OCC symbol, and for a
`positions` row that string is the ONLY place an expiry survives: migration 0002
gives the table `symbol`, `underlying`, `signed_quantity`, `average_open_price`
and `opened_at`, and nothing else. Reconciliation needs the expiry to tell a 0DTE
position that expired away from one that has genuinely gone missing at the
broker (`reconcile.expiry_verdicts`), so it reads it back out of the symbol.

The format is the 21-character OCC standard, as tastytrade emits it on both
`CurrentPosition.symbol` and `Option.symbol`:

    SPXW  260812P05000000
    |----|                 root, left-justified, space-padded to 6
          |----|           expiry, YYMMDD
                |          right, C or P
                 |------|  strike x 1000, zero-padded to 8

The match anchors on the fixed-width 15-character TAIL rather than on the
padding, so an unpadded root (`SPXW260812P05000000`) parses identically. That is
not hypothetical tolerance: both spellings appear in captured data in this repo's
own tests, and which one a given SDK call returns is not something reconciliation
should depend on.

Expiry and STRIKE are exposed; the right is not. Until 2026-08-25 this module
said "only the expiry", on the reasoning that strike and right are already
carried as typed fields everywhere they are needed (`OptionContract`,
`SpreadSignal`, `OrderLeg`) — so re-deriving them from a string would be a second
and weaker source for data the system already holds.

**That premise expired when a new caller appeared for which it is false.** An
`order.fill` audit record carries `symbol` and nothing else about the instrument:
no strike, no right, and `candidate_id` NULL by construction. Post-close scoring
has to decide which LEG of a vertical a fill was, and the symbol is the only
place that survives in the append-only log. So the strike joins the expiry as a
field with nowhere else to live — for that reader, not for the typed paths, which
must keep using their own fields.

The right stayed unexposed until 2026-09-26, on the same reasoning (`candidate_key`
already carries the side). **That premise expired the same way the strike's did:**
the side-balance rule counts positions per side, and a broker leg the ledger never
booked carries its symbol and nothing else — no candidate key to read a side from.
It must still count against its side (fail closed on what the ledger cannot see),
so the right joins as a field for THAT reader. Typed paths keep their own fields.
"""

from __future__ import annotations

import re
from datetime import date

#: Root, then the fixed 15-char tail: YYMMDD + C/P + 8-digit strike. The root is
#: non-greedy but the tail is anchored and fixed-width, so the split is
#: unambiguous even for a root that itself contains digits.
_OCC = re.compile(
    r"^(?P<root>.+?)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<right>[CP])(?P<strike>\d{8})$"
)

#: OCC encodes the strike in thousandths. Integer division keeps the parse exact
#: for every strike SPXW actually lists (whole points and half points alike).
_STRIKE_SCALE = 1000


def occ_expiry(symbol: str) -> date | None:
    """The expiration date `symbol` carries, or None if it carries none.

    **None means "cannot say", never "no expiry", and every caller must fail
    closed on it.** The one caller today is reconciliation deciding whether to
    stand a halt down; an unreadable symbol there keeps the halt, which is the
    only safe reading of "Argus cannot tell what this row is".

    Never raises. A malformed symbol, an impossible date (month 13, 31 February),
    an empty string and a non-option symbol all return None — this feeds a safety
    decision and must not be able to take reconciliation itself down.

    The two-digit year is read as 20YY, which is the OCC convention and is
    unambiguous until 2100.
    """
    if not symbol:
        return None
    match = _OCC.match(symbol.strip())
    if match is None:
        return None
    try:
        return date(2000 + int(match["yy"]), int(match["mm"]), int(match["dd"]))
    except ValueError:
        return None


def occ_strike(symbol: str) -> float | None:
    """The strike `symbol` carries, or None if it carries none.

    **Same fail-closed contract as `occ_expiry`: None means "cannot say".** Its
    caller (post-close scoring, deciding which leg of a vertical a fill was) drops
    the fill rather than guessing a role, because a fill attributed to the wrong
    leg nets a real movement into the wrong term.

    Never raises, for the same reason `occ_expiry` does not: a malformed symbol,
    an empty string and a non-option symbol all return None.

    Exact by construction — the field is 8 digits of thousandths, so this is an
    integer parse followed by a division by 1000, never a float parse of a decimal
    string. Callers still compare with a tolerance, because the strike they
    compare AGAINST came from `float()` on a `%g`-formatted candidate key.
    """
    if not symbol:
        return None
    match = _OCC.match(symbol.strip())
    if match is None:
        return None
    return int(match["strike"]) / _STRIKE_SCALE


def occ_right(symbol: str) -> str | None:
    """`"call"` or `"put"` for `symbol`, or None if it carries none.

    **Same fail-closed contract as `occ_expiry`: None means "cannot say".** Its
    caller (the risk snapshot's per-side count, for broker legs the ledger may not
    have booked) counts an unreadable leg against BOTH sides rather than neither.

    Never raises, for the same reason the others do.
    """
    if not symbol:
        return None
    match = _OCC.match(symbol.strip())
    if match is None:
        return None
    return "call" if match["right"] == "C" else "put"
