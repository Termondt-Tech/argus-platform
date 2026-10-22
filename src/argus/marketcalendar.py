"""Market-session calendar — when the always-on supervisor may run (scheduling).

The supervisor process is always-on; the market is not. This decides, for any ET
date, whether a session runs and the window it runs in. It is pure scheduling
correctness — the process must know when the market is closed — and is DISTINCT
from macro-event blackout days (FOMC/CPI/NFP), which are a deferred strategy/risk
decision, not a calendar fact.

Everything is anchored in exchange time (`clock.ET`), never host-local, for the
same reason the 0DTE chain is: a box in UTC would otherwise compute the wrong
"today" around the date boundary. `session_window` returns tz-aware ET datetimes,
so comparisons against `et_now()` are correct across DST.

Suppression is fail-closed toward NOT trading and toward an EARLY close:
- weekends and configured `holidays` -> no session;
- `half_days` -> the session ends at `half_day_close` (earlier than the regular
  close), never past it;
- a date in both `holidays` and `half_days` is a full closure (holidays win);
- a window that would open at or after its own close is suppressed entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from argus.clock import ET
from argus.config import MarketConfig

#: How long past the session close before that day's expiries count as SETTLED.
#: The same 15 minutes — and the same reasoning — as `tools/mark.py`'s
#: `_EARLIEST_FEED_MARK_ET`: "a quote before this is not a settlement price",
#: 16:00 plus a buffer for the closing index print to settle down. Defined once
#: here rather than twice, and applied to `close_time(d)` rather than to a
#: literal 16:00, so a half-day settles 15 minutes after ITS close.
SETTLEMENT_MARGIN = timedelta(minutes=15)


@dataclass(frozen=True)
class MarketCalendar:
    session_open: time
    session_close: time
    half_day_close: time
    holidays: frozenset[date]
    half_days: frozenset[date]

    @classmethod
    def from_config(cls, cfg: MarketConfig) -> "MarketCalendar":
        return cls(
            session_open=cfg.session_open,
            session_close=cfg.session_close,
            half_day_close=cfg.half_day_close,
            holidays=cfg.holidays,
            half_days=cfg.half_days,
        )

    def is_trading_day(self, d: date) -> bool:
        """Weekday and not a configured full-closure holiday.

        Holidays are checked here, so a date listed as both a holiday and a
        half-day resolves to a full closure — the safe direction.
        """
        return d.weekday() < 5 and d not in self.holidays

    def close_time(self, d: date) -> time:
        """The ET close for date `d`: the early close on a half-day, else regular."""
        return self.half_day_close if d in self.half_days else self.session_close

    def close_datetime(self, d: date) -> datetime:
        """The ET instant `d`'s session closes — the early close on a half-day.

        The anchor every derived mark below is measured from, extracted so there is
        exactly one `datetime.combine(..., close_time(d), tzinfo=ET)` in the module.
        Two independent copies of that line are two chances to anchor one of them on
        `session_close` instead, which is invisible on 250 days a year.
        """
        return datetime.combine(d, self.close_time(d), tzinfo=ET)

    def settlement_mark(self, d: date) -> datetime:
        """The ET instant from which `d`'s expiries may be treated as settled.

        `close_time(d)` + `SETTLEMENT_MARGIN`, so it follows the HALF-DAY calendar
        automatically — 13:15 ET on a half-day, 16:15 on a normal session. That is
        the whole reason this lives on the calendar rather than as a constant next
        to its caller: a hardcoded 16:15 is wrong on the two or three days a year
        the market closes at 13:00, and wrong in the unsafe direction (it would
        keep refusing rather than resolve early), but wrong all the same.
        """
        return self.close_datetime(d) + SETTLEMENT_MARGIN

    def flatten_mark(self, d: date, before_close_s: float) -> datetime:
        """The ET instant open positions on `d` must be flattened by. (2026-08-28)

        THE one place the position watchdog's time-based flatten trigger is built,
        and it lives here for the same reason `settlement_mark` does: the offset is
        applied to `close_time(d)`, never to a literal 16:00. 30 minutes before the
        close is **15:30 ET on a normal session and 12:30 on a half-day** — and a
        hardcoded 15:30 would fall two and a half hours AFTER a half-day's
        settlement, so the control would simply never fire on the compressed days it
        is most needed. Half-day correctness is a property of where the number is
        computed, not a branch anyone has to remember.

        Deliberately an OFFSET and not an absolute time. A config accepting either
        would have to define which wins on a half-day, and the answer is always the
        offset — so the absolute form is a key whose only distinct behaviour is the
        wrong one.
        → docs/decisions/2026-08-28-time-based-flatten-trigger.md
        """
        return self.close_datetime(d) - timedelta(seconds=before_close_s)

    def is_settled(self, d: date, now_et: datetime) -> bool:
        """Has `d`'s session closed AND settled, as of `now_et`?

        Inclusive at the mark (`>=`), matching the house convention for every other
        bound in this codebase (`staleness`, the chain's freshness bounds): the
        boundary instant itself is on the permissive side.

        `now_et` must be timezone-aware — `clock.et_now()`. A naive datetime raises
        on the comparison rather than silently being read as ET, which is the same
        discipline the rest of the module follows about never trusting host-local
        time. Any date other than `d` answers correctly for free: the comparison is
        against an absolute instant, so yesterday's session is settled all of today
        and tomorrow's is settled by nothing.
        """
        return now_et >= self.settlement_mark(d)

    def session_window(self, d: date) -> tuple[datetime, datetime] | None:
        """The (open, close) ET datetimes for `d`, or None if no session runs.

        None means "market closed" for scheduling purposes: a weekend, a holiday,
        or a window that would be degenerate (open at/after close). A half-day
        returns a window ending at the early close.
        """
        if not self.is_trading_day(d):
            return None
        open_dt = datetime.combine(d, self.session_open, tzinfo=ET)
        close_dt = datetime.combine(d, self.close_time(d), tzinfo=ET)
        if open_dt >= close_dt:
            return None
        return (open_dt, close_dt)
