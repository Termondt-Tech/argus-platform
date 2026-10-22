"""MarketCalendar: trading-day, holiday, half-day, and window correctness.

Pure scheduling logic — no async, no credentials, no market. The support config's
`[market]` section carries a few representative 2026 holidays/half-days to assert
against; direct construction covers the edge cases (overlap, degenerate window).
"""

from __future__ import annotations

from datetime import date, time

from argus.clock import ET
from argus.marketcalendar import MarketCalendar
from support import make_config

# 2026 reference dates (2026-07-25 is a Saturday):
_MON = date(2026, 7, 27)          # ordinary trading day
_SAT = date(2026, 7, 25)
_SUN = date(2026, 7, 26)
_HOLIDAY = date(2026, 11, 26)     # Thanksgiving (Thu) — full closure in support config
_HALF_DAY = date(2026, 11, 27)    # day after Thanksgiving (Fri) — half day
_XMAS = date(2026, 12, 25)        # Christmas (Fri) — full closure


def _calendar() -> MarketCalendar:
    return MarketCalendar.from_config(make_config().market)


def test_ordinary_weekday_is_a_trading_day():
    cal = _calendar()
    assert cal.is_trading_day(_MON) is True
    window = cal.session_window(_MON)
    assert window is not None
    open_dt, close_dt = window
    assert (open_dt.hour, open_dt.minute) == (9, 40)
    assert (close_dt.hour, close_dt.minute) == (16, 0)
    # Anchored in exchange time, never host-local.
    assert open_dt.tzinfo is ET and close_dt.tzinfo is ET


def test_weekend_has_no_session():
    cal = _calendar()
    assert cal.is_trading_day(_SAT) is False
    assert cal.is_trading_day(_SUN) is False
    assert cal.session_window(_SAT) is None
    assert cal.session_window(_SUN) is None


def test_configured_holiday_on_a_weekday_has_no_session():
    cal = _calendar()
    # Thanksgiving is a Thursday — a weekday, so only the holiday list suppresses it.
    assert _HOLIDAY.weekday() < 5
    assert cal.is_trading_day(_HOLIDAY) is False
    assert cal.session_window(_HOLIDAY) is None
    assert cal.session_window(_XMAS) is None


def test_half_day_closes_early():
    cal = _calendar()
    assert cal.is_trading_day(_HALF_DAY) is True
    assert cal.close_time(_HALF_DAY) == time(13, 0)
    window = cal.session_window(_HALF_DAY)
    assert window is not None
    _, close_dt = window
    assert (close_dt.hour, close_dt.minute) == (13, 0)   # NOT 16:00


def test_close_time_is_regular_on_a_normal_day():
    cal = _calendar()
    assert cal.close_time(_MON) == time(16, 0)


def test_settlement_mark_is_the_days_own_close_plus_the_margin():
    """The calendar half of the same-day expiry carve-out (2026-08-25).

    Anchored on `close_time(d)`, so it moves with a half-day rather than sitting
    at a literal 16:15. The rule that CONSUMES this — and the inclusive-boundary
    and evening-restart cases — is tested in `test_expired_positions.py`; this is
    here so a change to `SETTLEMENT_MARGIN` fails next to the calendar too.
    """
    cal = _calendar()
    normal = cal.settlement_mark(_MON)
    assert (normal.hour, normal.minute) == (16, 15)
    assert normal.tzinfo is ET

    early = cal.settlement_mark(_HALF_DAY)
    assert (early.hour, early.minute) == (13, 15)


def test_holiday_wins_over_half_day_on_overlap():
    # A date listed as BOTH a holiday and a half day is a full closure.
    d = date(2026, 12, 24)  # a Thursday
    cal = MarketCalendar(
        session_open=time(9, 40),
        session_close=time(16, 0),
        half_day_close=time(13, 0),
        holidays=frozenset({d}),
        half_days=frozenset({d}),
    )
    assert cal.is_trading_day(d) is False
    assert cal.session_window(d) is None


def test_degenerate_window_is_suppressed():
    # A half-day whose early close precedes the open must not run (fail toward
    # NOT trading), even though the date is otherwise a weekday.
    d = date(2026, 7, 27)  # Monday
    cal = MarketCalendar(
        session_open=time(14, 0),
        session_close=time(16, 0),
        half_day_close=time(13, 0),   # earlier than the 14:00 open
        holidays=frozenset(),
        half_days=frozenset({d}),
    )
    assert cal.session_window(d) is None
