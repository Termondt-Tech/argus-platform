"""What a held-to-expiry spread was worth, from the settlement price.

A 0DTE spread the 15:30 flatten could not close (its modal regime on a winning
day: no market for a spread that is nearly worthless) expires. That produces no
fill to price from, and an OTM expiry produces no broker transaction either — only
ITM options cash-settle — so without this module its result is knowable but never
reported. → docs/decisions/2026-09-22-valuing-a-held-to-expiry-trade.md

**Its own module, deliberately not part of `broker_ledger`.** That module is about
what the BROKER said; this one is about what the INDEX did. Keeping them apart is
what stops a settlement estimate drifting into the broker comparison, which is
fills-only by rule: the broker has no row for an OTM expiry, so a settlement number
there would make every such day `DIFF` for a difference that is correct.

**Not a scorer.** `settle_trade` calls `shadow.outcome.settlement_liability` and
`shadow.outcome.classify` — the math `tools.mark` already uses — and re-implements
neither. An AST test holds that.

**The price is the day's LAST RECORDED SPOT** off the watchdog's own marks, so the
ledger still opens no market-data session and any past day re-renders to the
identical number. It is a PROXY — SPXW settles on the official Cboe print, not on
the last streamed index tick — which is why the basis is named on every row, and why
an official print in the settlements file wins over it. A recorded spot too far
from the close is REFUSED rather than used: a spread held to expiry is watched to
the close, so an early last spot means something else went wrong, and a guessed
settlement would hide it.

Unknown stays blank, never zero: every refusal below returns a price of `None`
with a basis that says why.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, NamedTuple

from argus.clock import ET, UTC_TS_FMT, et_day_bounds_utc_iso
from argus.marketcalendar import MarketCalendar
from argus.shadow.outcome import classify, settlement_liability
from argus.signal.models import SPX_MULTIPLIER
from argus.tools.desk import MARK_CATEGORIES

logger = logging.getLogger(__name__)

#: A recorded spot older than this at the close is refused, not used. A module
#: constant on the `status.BLACKOUT_WARN_DAYS` precedent: a new REQUIRED config key
#: would force a coupled deploy for a reporting nicety.
SETTLEMENT_SPOT_MAX_AGE_MIN = 15

#: `trades.gross_basis` — how a trade's gross was arrived at. Cell values; nothing
#: branches on them but this module, `tools.ledger` and the Dashboard.
FILLS = "fills"                                 # closed in full, on its own fills
SETTLEMENT_SPOT = "settlement_spot"             # valued at the last recorded spot
SETTLEMENT_OFFICIAL = "settlement_official"     # valued at the settlements file's print
SPOT_TOO_EARLY = "spot_too_early"               # newest spot too far before the close
NO_SPOT = "no_spot"                             # no spot recorded on that date at all
SETTLEMENT_FILE_ERROR = "settlement_file_error"  # the override for the date is unusable

#: The bases that carry a number. Everything else is a refusal.
VALUED_AT_SETTLEMENT = (SETTLEMENT_SPOT, SETTLEMENT_OFFICIAL)


@dataclass(frozen=True)
class Settlement:
    """One trading date's settlement price, or the reason there is none."""

    price: float | None
    basis: str
    #: When the spot was recorded (ET). `None` for an official print or a refusal
    #: that never found a spot.
    spot_at_et: datetime | None = None

    @property
    def valued(self) -> bool:
        return self.price is not None


# -- reads --------------------------------------------------------------------


def read_settlement_spots(
    conn: sqlite3.Connection, trading_date: date
) -> tuple[float, datetime] | None:
    """The newest recorded `spot` on `trading_date` (ET), with when it was recorded.

    Across BOTH `MARK_CATEGORIES`, newest by `seq`, for the reason `desk.read_marks`
    does: a spent exit moves a position's marks to `watchdog.check_spent` and never
    back, so reading `watchdog.check` alone freezes at the moment the exit died —
    and a held-to-expiry spread is exactly the position most likely to have one.

    A tick that could not read spot writes null (`LegQuotes.spot` fails closed past
    `market_data_max_age_s`, and `payload_number` nulls a non-finite value), so the
    usability filter is in SQL: a null or non-positive spot is skipped and the query
    falls through to the newest row that HAS one, never taken as the last price. The
    same fail-closed read is why a row's `ts` is, to within that bound, the QUOTE's
    time — which is what makes the age check in `resolve_settlement` meaningful.

    Cost: the `(category, ts)` index bounds the scan to the day, and SQLite then sorts
    the day's matching rows for `seq` (a temp B-tree) — it does not stop at the first
    one. ~20-35 ms for a ~9,000-mark day on the 2026-09 snapshots, and it only runs on
    a day that held something to the close.
    """
    start, end = et_day_bounds_utc_iso(trading_date)
    placeholders = ",".join("?" * len(MARK_CATEGORIES))
    row = conn.execute(
        "SELECT ts, json_extract(payload, '$.spot') FROM audit_log "
        f"WHERE category IN ({placeholders}) AND ts >= ? AND ts < ? "
        "AND json_type(payload, '$.spot') IN ('real', 'integer') "
        "AND json_extract(payload, '$.spot') > 0 "
        "ORDER BY seq DESC LIMIT 1",
        (*MARK_CATEGORIES, start, end),
    ).fetchone()
    if row is None:
        return None
    ts, spot = row
    price = float(spot)
    if not math.isfinite(price):
        return None
    at = datetime.strptime(ts, UTC_TS_FMT).replace(tzinfo=timezone.utc).astimezone(ET)
    return price, at


def read_settlement_overrides(path: Path) -> dict[date, float | None]:
    """`date,price` per line from the persistent settlements file.

    `#` starts a comment; blank lines are ignored. **A malformed line costs its own
    DATE, not the run**: a readable date with an unusable price maps to `None`, which
    the resolver turns into `settlement_file_error` for that date rather than falling
    back to the recorded spot (the operator said the spot was wrong for that day).
    A date listed twice with different prices is the same refusal — two answers are
    not an answer. A line whose DATE cannot be read cannot cost any date; it is
    logged and dropped.

    An unreadable FILE raises `OSError`; the caller decides what that costs.
    """
    out: dict[date, float | None] = {}
    # UTF-8 explicitly, never the locale's: the file is written on the host and read
    # in the container, and the two need not agree on a default.
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        date_part, _, price_part = (p.strip() for p in line.partition(","))
        try:
            # Strictly YYYY-MM-DD: `fromisoformat` also takes `20260922`, and an
            # operator file is the wrong place to be generous about formats.
            d = datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            logger.warning("settlements %s:%d: unreadable date %r, line ignored",
                           path, lineno, date_part)
            continue
        price: float | None
        try:
            price = float(price_part)
        except ValueError:
            price = None
        if price is None or not math.isfinite(price) or price <= 0.0:
            logger.warning("settlements %s:%d: unusable price %r for %s, date refused",
                           path, lineno, price_part, d)
            price = None
        if d in out and out[d] != price:
            logger.warning("settlements %s:%d: %s listed twice with different prices, "
                           "date refused", path, lineno, d)
            price = None
        out[d] = price
    return out


# -- the decision -------------------------------------------------------------


def resolve_settlement(
    conn: sqlite3.Connection,
    trading_date: date,
    calendar: MarketCalendar,
    overrides: Mapping[date, float | None] | None,
) -> Settlement:
    """The settlement price for one date, or the reason it is refused.

    `overrides` is `None` when a settlements file was NAMED but could not be read:
    then no date can be trusted to be absent from it, so every date is refused
    rather than silently valued from the recorded spot. An empty mapping is "no file",
    and every date falls through to the spot.
    """
    if overrides is None:
        return Settlement(price=None, basis=SETTLEMENT_FILE_ERROR)
    if trading_date in overrides:
        price = overrides[trading_date]
        if price is None:
            return Settlement(price=None, basis=SETTLEMENT_FILE_ERROR)
        return Settlement(price=price, basis=SETTLEMENT_OFFICIAL)

    spot = read_settlement_spots(conn, trading_date)
    if spot is None:
        return Settlement(price=None, basis=NO_SPOT)
    price, at = spot
    # The close from the CALENDAR, never a literal 16:00: a half-day settles at 13:00.
    earliest = calendar.close_datetime(trading_date) - timedelta(
        minutes=SETTLEMENT_SPOT_MAX_AGE_MIN
    )
    if at < earliest:
        logger.warning("settlement %s: last recorded spot %.2f at %s is more than %d min "
                       "before the close; not used", trading_date, price,
                       at.strftime("%H:%M:%S"), SETTLEMENT_SPOT_MAX_AGE_MIN)
        return Settlement(price=None, basis=SPOT_TOO_EARLY, spot_at_et=at)
    return Settlement(price=price, basis=SETTLEMENT_SPOT, spot_at_et=at)


class SettledTrade(NamedTuple):
    """What `contracts` held to expiry were worth at settlement."""

    #: Dollars: the opening credit on those contracts less what they owed.
    gross: float
    #: `classify`'s `full_credit` | `partial` | `max_loss`.
    result: str
    #: What those contracts OWED at settlement, in dollars — the one part of a
    #: held trade's gross that is not cash its fills moved. `tools.ledger`'s
    #: fills-vs-P&L invariant subtracts exactly this.
    liability: float


def settle_trade(
    side: str,
    short_strike: float,
    long_strike: float,
    contracts: int,
    entry_credit: float,
    settlement: float,
) -> SettledTrade:
    """`contracts` held to expiry, valued at `settlement`.

    `Outcome`'s own second term — "settlement on the contracts HELD" — on the
    REALIZED credit the ledger already carries.
    """
    points = settlement_liability(side, short_strike, long_strike, settlement)
    liability = points * contracts * SPX_MULTIPLIER
    return SettledTrade(
        gross=entry_credit * contracts * SPX_MULTIPLIER - liability,
        result=classify(points, abs(long_strike - short_strike)),
        liability=liability,
    )
