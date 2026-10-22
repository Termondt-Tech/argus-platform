"""Exchange-time helpers.

The trading day is defined in US/Eastern, never the host's local timezone. A box
running in UTC or PT would otherwise compute the wrong "today" for the 0DTE chain
around the date boundary — subscribing to yesterday's or tomorrow's expiry, which
fails in the least obvious way possible (an empty chain and no signals, with
nothing looking broken).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

#: The `ts` format every audit record is stored in: UTC, microsecond precision,
#: FIXED WIDTH. The fixed width is what makes a lexicographic `ts >= ?` / `ts < ?`
#: comparison in SQL equal a chronological one, which is how every day-bounded
#: query against `audit_log` works. Defined once, here, because both the writer
#: (`audit.log`) and the day-bounded readers (`status`, `risk.rehydrate`) must
#: agree on it — two independent copies drifting would silently return no rows.
UTC_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def et_now() -> datetime:
    return datetime.now(ET)


def et_today() -> date:
    """The current trading date in exchange time."""
    return et_now().date()


def et_time_of_day() -> time:
    """Current ET wall-clock time — what entry windows are expressed in."""
    return et_now().time()


def et_day_bounds_utc_iso(day: date) -> tuple[str, str]:
    """`[start, end)` of one ET calendar day, as UTC `ts` strings to compare.

    The trading day is an ET calendar day and audit `ts` is UTC, so "records
    belonging to this trading date" is a half-open range over the stored strings:
    `ts >= start AND ts < end`. A session runs entirely inside one ET day, which
    is what makes the range exact rather than approximate.

    Both bounds, not just the lower one, because the callers that ask about a
    SPECIFIC trading date (rather than "today, so far") need the day to be closed
    at the top too — that upper bound is the episode boundary that stops one
    trading day's state from being read as another's.
    """
    start_et = datetime.combine(day, time.min, tzinfo=ET)
    end_et = datetime.combine(day + timedelta(days=1), time.min, tzinfo=ET)
    return (
        start_et.astimezone(timezone.utc).strftime(UTC_TS_FMT),
        end_et.astimezone(timezone.utc).strftime(UTC_TS_FMT),
    )
