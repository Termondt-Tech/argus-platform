"""SessionScheduler: the pure `plan()` decision and the async day-to-day driver.

`plan()` is exhaustively testable with constructed clocks — before/at/after the
open and close, weekends, holidays, half-days. The async `run()` driver is
exercised with a fake clock that a fake `wait` advances, so multiple trading days
(including skipped weekends and holidays) play out deterministically and
instantly, with no real sleeping, no credentials, and no market.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

from argus.clock import ET
from argus.marketcalendar import MarketCalendar
from argus.scheduler import SessionScheduler
from support import make_config

_MON = date(2026, 7, 27)


def _dt(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)


def _scheduler(*, now=None, wait_fn=None, run_session=None, stop=None) -> SessionScheduler:
    async def _noop(_td, _close, _stop):  # default run_session
        return None

    return SessionScheduler(
        calendar=MarketCalendar.from_config(make_config().market),
        run_session=run_session or _noop,
        stop=stop if stop is not None else asyncio.Event(),
        now_et=now,
        wait_fn=wait_fn,
    )


# -- plan(): pure decision ----------------------------------------------------


def test_plan_before_open_sleeps_until_today_open():
    sched = _scheduler()
    dec = sched.plan(_dt(_MON, 8, 0))
    assert dec.run is False
    assert dec.wake_at == _dt(_MON, 9, 40)


def test_plan_in_window_runs_today():
    sched = _scheduler()
    dec = sched.plan(_dt(_MON, 11, 0))
    assert dec.run is True
    assert dec.trading_date == _MON
    assert dec.session_close == _dt(_MON, 16, 0)


def test_plan_at_open_boundary_runs():
    sched = _scheduler()
    assert sched.plan(_dt(_MON, 9, 40)).run is True


def test_plan_at_close_boundary_does_not_run():
    # The window is half-open [open, close): at exactly the close there is no
    # session, and the next wake is the next trading day's open.
    sched = _scheduler()
    dec = sched.plan(_dt(_MON, 16, 0))
    assert dec.run is False
    assert dec.wake_at == _dt(date(2026, 7, 28), 9, 40)  # Tuesday


def test_plan_after_close_sleeps_until_next_trading_day():
    sched = _scheduler()
    dec = sched.plan(_dt(_MON, 17, 0))
    assert dec.run is False
    assert dec.wake_at == _dt(date(2026, 7, 28), 9, 40)


def test_plan_on_weekend_sleeps_until_monday():
    sched = _scheduler()
    dec = sched.plan(_dt(date(2026, 7, 25), 12, 0))  # Saturday
    assert dec.run is False
    assert dec.wake_at == _dt(_MON, 9, 40)


def test_plan_on_holiday_skips_to_next_trading_day():
    sched = _scheduler()
    # Thanksgiving (Thu 2026-11-26); next trading day is the half day Fri 11-27.
    dec = sched.plan(_dt(date(2026, 11, 26), 11, 0))
    assert dec.run is False
    assert dec.wake_at == _dt(date(2026, 11, 27), 9, 40)


def test_plan_in_half_day_window_runs_with_early_close():
    sched = _scheduler()
    dec = sched.plan(_dt(date(2026, 11, 27), 11, 0))
    assert dec.run is True
    assert dec.session_close == _dt(date(2026, 11, 27), 13, 0)  # early close


def test_plan_after_half_day_close_skips_weekend_to_monday():
    sched = _scheduler()
    dec = sched.plan(_dt(date(2026, 11, 27), 13, 30))  # past the 13:00 early close
    assert dec.run is False
    assert dec.wake_at == _dt(date(2026, 11, 30), 9, 40)  # following Monday


# -- async run(): the day-to-day driver ---------------------------------------


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


async def _drive(start: datetime, *, stop_after: int) -> list[date]:
    """Run the scheduler from `start`, recording the trading date of each session.

    A fake `wait` advances the clock by exactly the requested delay, so the driver
    marches through real calendar time without sleeping. The run stops once
    `stop_after` sessions have run.
    """
    clock = _Clock(start)
    stop = asyncio.Event()
    ran: list[date] = []

    async def wait(seconds: float) -> None:
        clock.now += timedelta(seconds=seconds)

    async def run_session(trading_date, session_close, session_stop) -> None:
        ran.append(trading_date)
        if len(ran) >= stop_after:
            stop.set()

    sched = _scheduler(now=clock, wait_fn=wait, run_session=run_session, stop=stop)
    await asyncio.wait_for(sched.run(), timeout=2.0)
    return ran


async def test_driver_runs_one_session_per_day_and_skips_the_weekend():
    # Start Friday at the open; the next session must be the following Monday.
    ran = await _drive(_dt(date(2026, 7, 24), 9, 40), stop_after=2)
    assert ran == [date(2026, 7, 24), date(2026, 7, 27)]


async def test_driver_skips_a_midweek_holiday():
    # Wed 2026-11-25 -> (skip Thanksgiving Thu 11-26) -> half day Fri 11-27.
    ran = await _drive(_dt(date(2026, 11, 25), 9, 40), stop_after=2)
    assert ran == [date(2026, 11, 25), date(2026, 11, 27)]


async def test_driver_ends_an_in_flight_session_when_the_process_stops():
    # A session blocked on its stop event must be released when the PROCESS stop
    # fires mid-session (the scheduler's close timer + stop-bridge). now is pinned
    # mid-session and the real (non-fake) wait is used so nothing but the process
    # stop ends the session.
    stop = asyncio.Event()
    entered = asyncio.Event()

    async def run_session(_td, _close, session_stop):
        entered.set()
        await session_stop.wait()

    sched = _scheduler(now=lambda: _dt(_MON, 11, 0), run_session=run_session, stop=stop)
    task = asyncio.create_task(sched.run())
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    assert not task.done()
    stop.set()  # SIGTERM mid-session
    await asyncio.wait_for(task, timeout=1.0)


async def test_driver_exits_promptly_when_stopped_while_idle():
    # Start on a weekend so the first action is an idle sleep; a stop set during
    # that wait must end the loop without ever running a session.
    clock = _Clock(_dt(date(2026, 7, 25), 12, 0))  # Saturday
    stop = asyncio.Event()
    ran: list[date] = []

    async def wait(_seconds: float) -> None:
        stop.set()  # simulate SIGTERM landing during the idle wait

    async def run_session(trading_date, _close, _stop) -> None:
        ran.append(trading_date)

    sched = _scheduler(now=clock, wait_fn=wait, run_session=run_session, stop=stop)
    await asyncio.wait_for(sched.run(), timeout=2.0)
    assert ran == []


# -- whose session: a strategy's own window (2026-10-12) --------------------------------


def _fridays_only(day: date):
    """A strategy whose session is 15:15-16:30 on Fridays only."""
    if day.weekday() != 4:
        return None
    return _dt(day, 15, 15), _dt(day, 16, 30)


def test_the_default_window_is_the_markets_own():
    # The SPX book passes no window: plan() reads the calendar exactly as before.
    sched = _scheduler()
    assert sched._window == sched._calendar.session_window


def test_a_strategy_window_decides_when_its_session_runs():
    sched = SessionScheduler(
        calendar=MarketCalendar.from_config(make_config().market),
        run_session=lambda *_: None, stop=asyncio.Event(), window=_fridays_only)
    fri = date(2026, 7, 24)
    # A Monday inside the market's hours is NOT this strategy's session.
    dec = sched.plan(_dt(_MON, 11, 0))
    assert dec.run is False and dec.wake_at == _dt(date(2026, 7, 31), 15, 15)
    # Its own window, half-open, ending at its own end rather than the close.
    assert sched.plan(_dt(fri, 15, 14)).run is False
    dec = sched.plan(_dt(fri, 15, 15))
    assert dec.run is True and dec.trading_date == fri
    assert dec.session_close == _dt(fri, 16, 30)
    assert sched.plan(_dt(fri, 16, 20)).run is True       # past the close, still its session
    assert sched.plan(_dt(fri, 16, 30)).run is False


async def test_the_driver_runs_a_strategy_window_once_a_day_and_idles_between():
    clock = _Clock(_dt(_MON, 9, 0))
    stop = asyncio.Event()
    ran: list[tuple[date, datetime]] = []

    async def wait(seconds: float) -> None:
        clock.now += timedelta(seconds=seconds)

    async def run_session(trading_date, session_close, session_stop) -> None:
        ran.append((trading_date, clock.now))
        if len(ran) >= 2:
            stop.set()

    sched = SessionScheduler(
        calendar=MarketCalendar.from_config(make_config().market), run_session=run_session,
        stop=stop, now_et=clock, wait_fn=wait, window=_fridays_only)
    await asyncio.wait_for(sched.run(), timeout=2.0)
    assert ran == [(date(2026, 7, 31), _dt(date(2026, 7, 31), 15, 15)),
                   (date(2026, 8, 7), _dt(date(2026, 8, 7), 15, 15))]
