"""The end-of-day report, rendered once and delivered without a human typing it.

READ-ONLY, like every section it is made of: the database is opened `mode=ro`, no
lock is taken, and **nothing here may ever write** — the write-mode `tools.mark`
needs the service stopped, and an unattended job must never stop the trading
process. `scripts/argus-eod.sh` runs this at 16:45 ET on trading days; by hand it
is the same command with `--out-dir` omitted.

    python -m argus.tools.eod --config /etc/argus/argus.live.toml
    python -m argus.tools.eod --config /etc/argus/argus.live.toml --date 2026-09-16

**Which day, when `--date` is omitted: the latest SETTLED session.** Not "today".
The timer is `Persistent=true`, so a run missed across a reboot fires at the next
boot — which may be the next morning, when "today" is a session that has not
happened yet and the day that was missed is yesterday. Resolving to the latest
settled session makes the catch-up report the day it was owed; and with
`--out-dir`, a day that already has a transcript is not rendered again, so a
catch-up can never overwrite the punctual report (whose scoring preview a later
run could not reproduce — `--from-feed` has no memory). **With `--out-dir` it
renders EVERY owed day** — each settled session since the newest report on disk,
up to `CATCH_UP_DAYS` back, oldest first (`owed_days`) — so an outage across two
sessions no longer reports the second and silently skips the first. An explicit
`--date` always renders: that is the re-render path.

**Two artifacts, and the second is the point.** A `.txt` transcript (what you used
to paste into a file) and a `.json` digest. The digest exists so the shell can build
the Slack message **without grepping the transcript**: a summary assembled by
`grep -A3 REALIZED` is a monitoring surface that goes quietly wrong the day a
heading is improved, and it fails in the reassuring direction — a real number lifted
off the wrong line. Same rule as `Order.reject_reason`: branch on the mechanism,
never on prose another program is free to reword. Every number in the digest is
computed from values here.

**Where the G1 numbers come from, and why not from `/status`.** `account.nlv` and
`account.daily_pnl` are the live cutover runbook's daily cross-check, and
`StatusPublisher` CLEARS the risk source when the session ends — so at 16:45 the
endpoint answers `phase: ended` with both fields null. They are read from
`gate.decision` instead (Tier 1, carries the whole `RiskSnapshot`), which gives the
day's PATH rather than one spot value — G1's question is *"does it move?"* — and
re-renders for a past date. **A day with no candidates has no such rows, and that
renders as "no series", never as `0.0`**: a structural zero shown as a number is
precisely the failure G1 exists to detect, and it is what cert looked like for
fourteen months.

**A blackout day is named, and read off the chain** (2026-09-18, `read_blackout_day`).
Live day 1 was an FOMC blackout and its digest said "no series (no candidates
evaluated…)" — true, and indistinguishable from a broken signal path. The report
date's blackout comes from `session.event_blackout` / `signal.none: event_blackout`
rows, never from `/status` (which answers for the poll's moment, not a re-rendered
date) and never from `blackout_dates.txt` (a regenerated file drops past dates).
Only the NEXT session's blackout comes from `/status`, because it is about the
future. An entry attempt on a blackout day is the one blackout reading that alarms.

**A failed section still ships the report.** Each section is an independent read,
and the flat check is above all the others — a broken `desk` must not cost you the
one line that says whether anything is still open. A section that raises has its
error pasted in place and is named in the digest's `failures`; only a failure to
produce or write the report at all is a non-zero exit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import traceback
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Callable

from argus._version import BUILD_VERSION
from argus.bootstrap import load_env
from argus.clock import ET, et_day_bounds_utc_iso, et_now
from argus.config import Config
from argus.control import KILL_TOKEN_VAR
from argus.marketcalendar import MarketCalendar
from argus.signal.strategy import EVENT_BLACKOUT
from argus.tools import desk, evaluability, regime

#: Bumped when the digest's shape changes in a way a reader could trip over. No
#: reader branches on it today — the Slack body is rendered here, not in the shell —
#: so it is for whoever next consumes the `.json` (the NAS, a notebook).
SCHEMA_VERSION = 1

REPORT_PREFIX = "argus-eod"

#: A regular session closes at 16:00 ET. Anything earlier is a half-day, and the
#: whole reason this file asks is `--from-feed`: it reads the CURRENT index quote as
#: the close, so on a 13:00 settlement a 16:45 read is three and a half hours of
#: drift presented as a settlement price. A human running the SOP knows what day it
#: is; an unattended job has to ask the calendar.
_REGULAR_CLOSE = time(16, 0)

GATE = "gate.decision"
UNWATCHABLE = "watchdog.unwatchable"
#: `session.EVENT_BLACKOUT_DAY`, spelled here rather than imported: `argus.session`
#: pulls in the broker SDK, which a read-only report has no business loading. A test
#: pins that the two spellings agree.
BLACKOUT_ANNOUNCED = "session.event_blackout"
SIGNAL_NONE = "signal.none"


# -- sections -----------------------------------------------------------------


@dataclass
class Section:
    """One rendered block of the transcript, and whether it came out."""

    key: str
    title: str
    text: str = ""
    failure: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass
class Report:
    trading_date: date
    sections: list[Section] = field(default_factory=list)
    digest: dict[str, Any] = field(default_factory=dict)
    #: `(key, one-line failure, traceback)` for each shared READ that raised. The
    #: reads several sections and the digest depend on (the ledger, G1, the
    #: blackout, the unwatchable count) run once, ahead of the sections — so they
    #: need the same guarantee a section has: a failure costs what depends on it and
    #: is named, never the whole report.
    read_failures: list[tuple[str, str, str]] = field(default_factory=list)

    def read(self, key: str, fn: Callable[[], Any]) -> Any:
        """One shared read, or `None` with the failure recorded.

        Every dependant must treat `None` as UNREAD — never as empty, zero or
        "nothing found": an unread ledger is not a flat one.
        """
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - a read must not take the report
            self.read_failures.append(
                (key, f"{type(exc).__name__}: {exc}", traceback.format_exc())
            )
            return None

    def run(self, key: str, title: str, fn: Callable[[], str]) -> Section:
        """Render one section, catching everything it can raise.

        `SystemExit` is caught deliberately and is not an oversight: the tools this
        composes report a bad input by raising it (`database not found`, `--from-feed`
        before 16:15), and one of those must cost its own section and nothing else.
        """
        section = Section(key=key, title=title)
        try:
            section.text = fn()
        except SystemExit as exc:  # a tool refusing its input
            section.failure = str(exc) or f"exited {exc.code}"
        except RuntimeError as exc:
            # This module's own refusals (no /status document, no journal file).
            # The message IS the explanation, so no traceback rides under it —
            # a stack trace makes an expected state read as a crash.
            section.failure = str(exc)
        except Exception as exc:  # noqa: BLE001 - a section must not take the report
            section.failure = f"{type(exc).__name__}: {exc}"
            section.text = traceback.format_exc()
        self.sections.append(section)
        return section

    @property
    def failures(self) -> list[str]:
        return [f"read {k}: {msg}" for k, msg, _ in self.read_failures] + [
            f"{s.key}: {s.failure}" for s in self.sections if not s.ok
        ]

    def transcript(self) -> str:
        out: list[str] = []
        if self.read_failures:
            # FIRST, above SUBJECT: every section below may be reading around one.
            out.append("=" * 78)
            out.append("READS THAT FAILED — sections depending on these are UNREAD")
            out.append("=" * 78)
            for key, msg, tb in self.read_failures:
                out.append(f"*** READ FAILED: {key}: {msg}")
                out.append(tb.rstrip("\n"))
                out.append("")
        for s in self.sections:
            out.append("=" * 78)
            out.append(s.title)
            out.append("=" * 78)
            if not s.ok:
                out.append(f"*** SECTION FAILED: {s.failure}")
                out.append("")
            out.append(s.text.rstrip("\n"))
            out.append("")
        return "\n".join(out) + "\n"


def _capture(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    """Run a printing entry point and return what it printed.

    stdout and stderr go to ONE buffer, in order: a tool's warning belongs beside
    the table it qualifies, not in a journal nobody is reading tonight.

    **Logging configuration is put back afterwards.** A composed tool may configure
    logging for the whole process — `tools.mark.main` calls `configure_logging`, i.e.
    `logging.basicConfig`, which installs a root handler bound to whatever
    `sys.stderr` is AT THAT MOMENT: this section's buffer. Left in place, every later
    section's log output went into that discarded buffer instead of its own, and the
    levels it set outlived the section. So the root handlers and every logger's level
    are restored to what they were before, and a logger first created during the
    call goes back to NOTSET.
    """
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    root_level = root.level
    levels_before = {
        name: lg.level
        for name, lg in logging.root.manager.loggerDict.items()
        if isinstance(lg, logging.Logger)
    }
    buf = StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            fn(*args, **kwargs)
    finally:
        for handler in list(root.handlers):
            if handler not in handlers_before:
                root.removeHandler(handler)
        for handler in handlers_before:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(root_level)
        for name, lg in list(logging.root.manager.loggerDict.items()):
            if isinstance(lg, logging.Logger):
                lg.setLevel(levels_before.get(name, logging.NOTSET))
    return buf.getvalue()


# -- the audit log reads this file does itself --------------------------------


@dataclass(frozen=True)
class G1Series:
    """The day's account path, off `gate.decision`'s `RiskSnapshot`."""

    rows: int
    nlv: tuple[float, ...]
    daily_pnl: tuple[float, ...]
    #: Rows whose snapshot carried NO account state — `RiskSnapshotBuilder.build`'s
    #: fallback, which writes `account_nlv = 0.0` AND `daily_pnl = 0.0` as ordinary
    #: numbers when there is no usable account snapshot. Excluded from both series and
    #: counted here instead: those zeros are the builder's placeholder, not readings.
    no_account: int = 0
    #: Rows whose snapshot says `daily_pnl_anchored: false` — `DailyPnl.value`'s
    #: placeholder 0.0 before the day has an anchor, which the breaker reads and
    #: cannot trip on. Their NLV is a real reading and is kept; their day P&L is
    #: excluded and counted here. Rows written before the key existed carry no flag
    #: and are read as before (2026-09-18).
    unanchored: int = 0

    @property
    def has_series(self) -> bool:
        return bool(self.nlv or self.daily_pnl)

    @property
    def pnl_moved(self) -> bool | None:
        """Did day P&L take more than one value? `None` when there is no series.

        The question G1 asks, and the reason this is a series rather than a spot
        read. A single value is not evidence either way — it is what cert produced
        structurally for fourteen months.
        """
        if not self.daily_pnl:
            return None
        return len(set(self.daily_pnl)) > 1


def read_g1(conn: sqlite3.Connection, start: str, end: str) -> G1Series:
    rows = conn.execute(
        "SELECT payload FROM audit_log WHERE category = ? AND ts >= ? AND ts < ? "
        "ORDER BY seq",
        (GATE, start, end),
    ).fetchall()
    nlv: list[float] = []
    pnl: list[float] = []
    no_account = 0
    unanchored = 0
    for (raw,) in rows:
        try:
            snapshot = json.loads(raw).get("snapshot") or {}
        except (TypeError, ValueError):
            continue
        account_nlv = snapshot.get("account_nlv")
        # NO ACCOUNT STATE: the builder's fallback writes nlv 0.0 and day P&L 0.0 as
        # plain numbers (the gate refuses the row as `invalid_account_nlv`). Taking
        # them as datapoints is the reassuring-when-wrong failure in its exact form:
        # a day P&L stuck at some constant c plus ONE such row reads as {c, 0.0} —
        # "MOVED, the breaker's input is live" — and the NLV path gains a 0.00 low.
        # A funded account never has a non-positive NLV, so this is the fallback's
        # signature, and it costs BOTH values of the row.
        if isinstance(account_nlv, (int, float)) and account_nlv <= 0:
            no_account += 1
            continue
        # `gate._num` stringifies non-finite values so the chain stays hashable, so
        # a non-float here is a real state (a never-connected stream reports inf),
        # not a parse failure. Skipped rather than coerced: an unreadable number
        # must not become a datapoint.
        if isinstance(account_nlv, (int, float)):
            nlv.append(float(account_nlv))
        # UNANCHORED: the day P&L is `DailyPnl`'s 0.0 placeholder, not a reading —
        # and since it is exactly what a dead input looks like, taking it as one
        # would let a mix of anchored values and placeholder zeros read as "MOVED".
        # `is False`, never falsy: a row written before the key existed says nothing.
        if snapshot.get("daily_pnl_anchored") is False:
            unanchored += 1
        elif isinstance(snapshot.get("daily_pnl"), (int, float)):
            pnl.append(float(snapshot["daily_pnl"]))
    return G1Series(
        rows=len(rows), nlv=tuple(nlv), daily_pnl=tuple(pnl), no_account=no_account,
        unanchored=unanchored,
    )


def count_unwatchable(conn: sqlite3.Connection, start: str, end: str) -> int:
    """`watchdog.unwatchable` records on the date — a position held but not managed.

    Counted from the CHAIN rather than from `/status`, because at report time the
    session has ended and `/status`'s `unmanaged` block is null (nothing publishing).
    An unwatchable position gets no trigger evaluated, no mark and no 15:30 flatten,
    so "there were none today" is a different and weaker claim than "there are none
    now" — this reports the one that is still knowable at 16:45.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE category = ? AND ts >= ? AND ts < ?",
        (UNWATCHABLE, start, end),
    ).fetchone()
    return int(row[0]) if row else 0


@dataclass(frozen=True)
class BlackoutDay:
    """Was the REPORT'S date an event-blackout day? Read off the chain.

    From the chain, never from `/status`'s `today_blacked_out`: that key answers for
    the moment `/status` was polled, and this report may be a next-morning catch-up
    or a `--date` re-render of a day long past. Not from `blackout_dates.txt`
    either — the generator writes a FORWARD horizon, so a regenerated file drops
    the very past dates a re-render asks about.

    Two sources, because they cover different history. `session.event_blackout` is
    written at session start from v0.4.4 on, and is the session's own verdict. A
    `signal.none` refused as `event_blackout` exists for every blackout day there
    has ever been, but only from 10:00 ET, when the entry window opens. Either one
    is proof; the report names which it saw.
    """

    announced: bool
    suppressed_ticks: int

    @property
    def blacked_out(self) -> bool:
        return self.announced or self.suppressed_ticks > 0

    def evidence(self) -> str:
        parts = []
        if self.announced:
            parts.append("session.event_blackout at session start")
        if self.suppressed_ticks:
            parts.append(f"{self.suppressed_ticks} tick(s) refused as event_blackout")
        return "; ".join(parts)


def read_blackout_day(conn: sqlite3.Connection, start: str, end: str) -> BlackoutDay:
    announced = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE category = ? AND ts >= ? AND ts < ?",
        (BLACKOUT_ANNOUNCED, start, end),
    ).fetchone()
    ticks = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE category = ? AND ts >= ? AND ts < ? "
        "AND json_extract(payload, '$.reason') = ?",
        (SIGNAL_NONE, start, end, EVENT_BLACKOUT),
    ).fetchone()
    return BlackoutDay(
        announced=bool(announced and announced[0]),
        suppressed_ticks=int(ticks[0]) if ticks else 0,
    )


# -- the control surface ------------------------------------------------------


def fetch_status(config: Config, token: str | None, timeout: float) -> dict[str, Any]:
    """`GET /status` as JSON. Raises; the caller turns that into a failed section.

    Read-only, and the same endpoint a phone polls. The token is never logged and
    never lands in the report — it is read from the environment like every other
    credential.
    """
    if not token:
        raise RuntimeError(
            f"{KILL_TOKEN_VAR} is not set, so /status cannot be read. It reaches the "
            "container through .env; the rails section needs it."
        )
    url = f"http://{config.server.bind_host}:{config.server.bind_port}/status"
    request = urllib.request.Request(  # noqa: S310 - fixed scheme, config-supplied host
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode())


# -- the calendar questions ---------------------------------------------------


def is_half_day(calendar: MarketCalendar, d: date) -> bool:
    return calendar.close_time(d) < _REGULAR_CLOSE


def is_last_trading_day_of_week(calendar: MarketCalendar, d: date) -> bool:
    """Is the next trading day in a different ISO week?

    The weekly read is attached to this rather than to `weekday() == 4`, because a
    Friday holiday would then silently skip the week — the same class of mistake as
    a literal 15:30 flatten on a half-day. Looks ahead 10 days, which covers any
    holiday run the calendar can produce.
    """
    week = d.isocalendar()[:2]
    for ahead in range(1, 11):
        nxt = d + timedelta(days=ahead)
        if calendar.is_trading_day(nxt):
            return nxt.isocalendar()[:2] != week
    return True


def latest_settled_session(calendar: MarketCalendar, now_et: datetime) -> date:
    """The most recent trading day whose session has SETTLED as of `now_et`.

    `MarketCalendar.is_settled`, never a literal 16:15 — a half-day settles at
    13:15. Looks back 15 days, which covers any holiday run the calendar can hold.
    """
    for back in range(0, 16):
        d = now_et.date() - timedelta(days=back)
        if calendar.is_trading_day(d) and calendar.is_settled(d, now_et):
            return d
    raise SystemExit(f"no settled trading day in the 15 days before {now_et:%Y-%m-%d}")


#: How far back an undated run will catch up missed reports, in calendar days. A
#: week covers a long weekend plus a multi-day droplet outage; anything older is
#: NAMED as not caught up rather than rendered, because a re-render that far back is
#: a deliberate `--date` act. `scripts/argus-eod.sh` collects the journal over this
#: same span (plus a day of margin) — change one, change the other.
CATCH_UP_DAYS = 7


@dataclass(frozen=True)
class OwedDays:
    """Which settled sessions an undated run owes a report, oldest first."""

    render: tuple[date, ...]
    #: Trading days in the gap since the last report that are older than
    #: `CATCH_UP_DAYS` — named in the output, never silently dropped.
    too_old: tuple[date, ...] = ()


def _reported_dates(out_dir: Path) -> set[date]:
    found: set[date] = set()
    for path in out_dir.glob(f"{REPORT_PREFIX}-*.txt"):
        try:
            found.add(date.fromisoformat(path.stem[len(REPORT_PREFIX) + 1:]))
        except ValueError:
            continue
    return found


def owed_days(calendar: MarketCalendar, now_et: datetime, out_dir: Path) -> OwedDays:
    """Every settled session since the newest existing report that has none yet.

    The render-once rule used to be "the latest settled session, if unreported" —
    so an outage across two sessions reported the second and skipped the first
    without a word. This is the gap since the last report instead. With NO report
    on disk at all (a fresh install) it is the latest session alone: a first run
    must not back-post a week of digests about days before it was deployed.
    """
    latest = latest_settled_session(calendar, now_et)
    reported = _reported_dates(out_dir) if out_dir.is_dir() else set()
    if not reported:
        return OwedDays(render=(latest,))
    since = max(reported)
    horizon = latest - timedelta(days=CATCH_UP_DAYS)
    render: list[date] = []
    too_old: list[date] = []
    d = since + timedelta(days=1)
    while d <= latest:
        if calendar.is_trading_day(d) and d not in reported:
            (render if d >= horizon else too_old).append(d)
        d += timedelta(days=1)
    return OwedDays(render=tuple(render), too_old=tuple(too_old))


# -- section renderers --------------------------------------------------------


def _header(config: Config, trading_date: date, calendar: MarketCalendar,
            blackout: BlackoutDay | None = None, blackout_unread: bool = False) -> str:
    """Name the subject, every run.

    `argus-snapshot.sh`'s rule: a report that does not say which world it describes
    is one hand-typed profile away from being a confident account of the wrong
    database. The cert config is still on the live droplet and still loads.

    A blackout day is named HERE, beside the date, because it changes how every
    section below reads: no entries, no gate rows, no G1 series — all by design.
    """
    source = config.source
    profile = source.profile if source else None
    lines = [
        f"  trading date   {trading_date}  ({'half-day' if is_half_day(calendar, trading_date) else 'regular'}"
        f", close {calendar.close_time(trading_date):%H:%M} ET)",
    ]
    if blackout is not None and blackout.blacked_out:
        lines.append(
            "  EVENT BLACKOUT no entries by design — the session still ran the watchdog\n"
            f"                 and the close ({blackout.evidence()})"
        )
    elif blackout_unread:
        lines.append(
            "  EVENT BLACKOUT UNREAD — the blackout read failed, so this report cannot say\n"
            "                 whether no entries were by design"
        )
    lines += [
        f"  environment    {config.environment}",
        f"  profile        {profile or '(none — ad-hoc --config path, asserts nothing)'}",
        f"  config         {source.filename if source else '?'} @{source.sha if source else '?'}",
        f"  database       {config.audit.db_path}",
        f"  build          {BUILD_VERSION}",
        f"  generated      {et_now():%Y-%m-%d %H:%M:%S} ET",
    ]
    return "\n".join(lines)


def _flat_check(summary: desk.DeskSummary, unwatchable_today: int | None,
                status: dict[str, Any] | None, settled: bool) -> str:
    lines: list[str] = []
    if summary.flat:
        lines.append("  FLAT — the ledger holds no open contracts for this date.")
    elif settled and summary.held_contracts and not summary.unpriceable:
        # After settlement a 0DTE hold is not OPEN — it expired, and "OPEN" in red
        # on an ordinary held-to-expiry evening is how a digest teaches its reader
        # to stop reading the headline. It is still the headline: nothing closed it,
        # and whether it settled in the money is a cash number to check.
        lines.append(
            f"  HELD TO EXPIRY — {summary.held_contracts} contract(s) were not closed by an\n"
            "  order and settled at the close. Check the settlement against the broker:\n"
            "  in the money is a cash-settled loss the ledger does not show yet."
        )
    else:
        lines.append(f"  NOT FLAT — {summary.held_contracts} contract(s) still open.")
        for h in summary.held:
            spent = "" if h.spent_exit is None else f"   EXIT SPENT ({h.spent_exit})"
            lines.append(f"    {h.candidate_id:32} {h.contracts:>3} contract(s){spent}")
        for u in summary.unpriceable:
            lines.append(f"    {u.candidate_id:32} UNPRICEABLE — {u.why}")

    if summary.spent_exits:
        lines.append("")
        lines.append(
            f"  {len(summary.spent_exits)} position(s) with a SPENT exit attempt. A spent exit "
            "still\n  marks every tick; it just will not send again outside the retry budget\n"
            "  and the flatten's one-shot allowance. Closing it can be a MANUAL action."
        )

    if unwatchable_today is None:
        lines.append("")
        lines.append(
            "  `watchdog.unwatchable` count UNREAD — the read failed (see READS THAT\n"
            "  FAILED). A position held but not managed cannot be ruled out; check the\n"
            "  broker by hand."
        )
    elif unwatchable_today:
        lines.append("")
        lines.append(
            f"  {unwatchable_today} `watchdog.unwatchable` record(s) today. A position the ledger\n"
            "  could not turn into a spread gets no trigger, no mark and NO 15:30\n"
            "  flatten, and on 0DTE it rides to settlement. Check the broker by hand."
        )

    # Only meaningful on a mid-session run; after the close the publisher is torn
    # down and both lists are null, which is not the same as empty.
    if status is not None:
        unmanaged = status.get("unmanaged") or {}
        positions, orders = unmanaged.get("positions"), unmanaged.get("orders")
        if positions is None and orders is None:
            lines.append("")
            lines.append(
                "  /status publishes no unmanaged block (no active session at report "
                "time).\n  That is the expected shape after the close — it is not a "
                "statement that\n  nothing is unmanaged, which is why the count above "
                "comes from the chain."
            )
        else:
            lines.append("")
            lines.append(
                f"  /status live: {len(positions or [])} unmanaged position(s), "
                f"{len(orders or [])} residual order(s) "
                f"(mode {unmanaged.get('cancel_mode')})"
            )
    return "\n".join(lines)


def _g1(series: G1Series, status: dict[str, Any] | None,
        blackout: BlackoutDay | None = None) -> str:
    lines: list[str] = []
    if not series.has_series:
        lines.append(
            f"  NO SERIES — {series.rows} gate.decision row(s) on this date carried an\n"
            "  account snapshot. Nothing was evaluated against the account, so there is\n"
            "  no NLV or day-P&L path to read. This is the expected shape on a blackout\n"
            "  or no-candidate day, and it is NOT a reading of 0.00 — a structural zero\n"
            "  rendered as a number is the exact failure G1 exists to detect."
        )
        # Which of the two it was, when the chain can say. A no-candidate day and
        # a blackout day render the same "no series"; only one of them is planned.
        if blackout is not None and blackout.blacked_out:
            lines.append(
                "\n  This WAS an event-blackout day, so no series is the designed outcome.\n"
                "  G1 is still unread — the first real read is a day that holds a position."
            )
        if series.no_account:
            lines.append(
                f"\n  {series.no_account} of those row(s) reached the gate with NO ACCOUNT STATE\n"
                "  (the snapshot's 0.0 NLV / 0.0 day-P&L placeholder), so candidates WERE\n"
                "  evaluated — against no account. That is not a quiet day; check why the\n"
                "  account snapshot was unusable."
            )
    else:
        nlv, pnl = series.nlv, series.daily_pnl
        lines.append(f"  from {series.rows} gate.decision row(s)")
        if series.no_account:
            lines.append(
                f"  ({series.no_account} excluded: NO ACCOUNT STATE — the snapshot's 0.0\n"
                "  placeholder, not a reading of the account)"
            )
        if series.unanchored:
            lines.append(
                f"  ({series.unanchored} day-P&L value(s) excluded: UNANCHORED — no day anchor,\n"
                "  so the breaker read a placeholder 0.00 and could not trip)"
            )
        if nlv:
            lines.append(
                f"  account NLV     first {nlv[0]:>12,.2f}   last {nlv[-1]:>12,.2f}   "
                f"min {min(nlv):>12,.2f}   max {max(nlv):>12,.2f}"
            )
        if pnl:
            lines.append(
                f"  day P&L         first {pnl[0]:>12,.2f}   last {pnl[-1]:>12,.2f}   "
                f"min {min(pnl):>12,.2f}   max {max(pnl):>12,.2f}"
            )
        lines.append("")
        if series.pnl_moved is None:
            # An NLV path with no day-P&L reading at all — every row unanchored or
            # unreadable. Not "one value": there is no value to call constant.
            lines.append(
                "  day P&L has NO READINGS — every row was unanchored or unreadable, so\n"
                "  the breaker's input was never live today. That is a G1 finding on any\n"
                "  day the gate was reached; check why the day P&L was not anchored."
            )
        elif series.pnl_moved:
            lines.append(
                "  day P&L MOVED during the session — the breaker's input is live.\n"
                "  Cross-check the last value against tastytrade's own day P&L by hand."
            )
        else:
            lines.append(
                "  day P&L held ONE value all session. On a day that held a position\n"
                "  that is the G1 failure: the rail's input is dead and the breaker is\n"
                "  as inert as it is in cert, with real money behind it. On a flat day\n"
                "  it is legitimate and proves nothing."
            )

    if status is not None:
        account = status.get("account") or {}
        lines.append("")
        lines.append(
            f"  /status at report time: nlv {account.get('nlv')}, "
            f"daily_pnl {account.get('daily_pnl')} "
            f"(phase {(status.get('session') or {}).get('phase')})"
        )
        lines.append(
            "  Both are null after the close by design — the session clears the risk\n"
            "  source on its way out. The series above is the durable half."
        )
    return "\n".join(lines)


def _rails(status: dict[str, Any] | None) -> str:
    if status is None:
        raise RuntimeError("no /status document (see the failure above)")
    kill = status.get("kill_switch") or {}
    halted = status.get("halted") or {}
    breaker = status.get("daily_loss_breaker") or {}
    watchdog = (status.get("watchdog") or {}).get("health")
    blackout = status.get("event_blackout")
    entries_ended = status.get("entries_ended")

    lines = [
        f"  environment    {status.get('environment')}   build {status.get('version')}   "
        f"config {(status.get('config') or {}).get('file')} "
        f"@{(status.get('config') or {}).get('sha')}",
        f"  kill switch    engaged={kill.get('engaged')}  source={kill.get('source')}  "
        f"since={kill.get('since')}  restored={kill.get('restored')}",
        f"  halted         {halted.get('halted')}  grade={halted.get('grade')}  "
        f"source={halted.get('source')}",
        f"  loss breaker   tripped={breaker.get('tripped')}"
        "   (null = no breaker armed for today)",
    ]
    if watchdog is None:
        lines.append("  watchdog       not publishing (no active session at report time)")
    else:
        lines.append(
            f"  watchdog       degraded={watchdog.get('degraded')}  "
            f"failures={watchdog.get('failures')}  where={watchdog.get('where')}"
        )
    if entries_ended is not None:
        lines.append(
            f"  entries ended  {entries_ended.get('kind')} since {entries_ended.get('since')} "
            f"(watchdog {entries_ended.get('watchdog')}, clears {entries_ended.get('clears')})"
        )
    if blackout is None:
        lines.append("  blackout list  not published")
    else:
        lines.append(
            f"  blackout list  state={blackout.get('state')}  "
            f"last={blackout.get('last_date')}  "
            f"{blackout.get('days_remaining')} day(s) of runway  "
            f"({blackout.get('date_count')} dates)"
        )
        # The one blackout fact that is about the FUTURE, and so the one only
        # `/status` can give: whether the next session takes entries. At 16:45 the
        # next session is tomorrow's. `.get()`: a pre-v0.4.4 page has no such key.
        nxt = blackout.get("next_session_blacked_out")
        if nxt is not None:
            lines.append(
                "  next session   "
                + ("EVENT BLACKOUT — no entries" if nxt else "not a blackout day")
                + f"   (opens {(status.get('session') or {}).get('next_session_open')})"
            )
    return "\n".join(lines)


def _backup(backup_dir: Path, now: datetime) -> str:
    """The nightly snapshot, read at 16:45 — after the 16:30 timer has run.

    Deliberately part of THIS report rather than a thing to check tomorrow: the
    snapshot is the only off-volume copy of a real-money audit chain, and its
    failure mode is silence.
    """
    if not backup_dir.is_dir():
        raise RuntimeError(f"{backup_dir} is not a directory (is it mounted?)")
    files = sorted(backup_dir.glob("argus-*.db"), key=lambda p: p.stat().st_mtime)
    if not files:
        return (
            f"  NO SNAPSHOTS in {backup_dir}. The audit chain has no off-volume copy.\n"
            "  Check `systemctl status argus-snapshot.service`."
        )
    newest = files[-1]
    age_h = (now.timestamp() - newest.stat().st_mtime) / 3600.0
    lines = [
        f"  newest         {newest.name}",
        f"  age            {age_h:.1f}h",
        f"  kept           {len(files)} snapshot(s) in {backup_dir}",
    ]
    if age_h > 26.0:
        lines.append(
            "\n  STALE — older than one nightly cycle plus slack. The 16:30 job is not\n"
            "  producing, so the NAS has nothing new to pull however healthy its end looks."
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class JournalSweep:
    """The shell's pre-grepped journal lines, scoped to the REPORT'S date.

    The shell collects a window wider than one day (`CATCH_UP_DAYS` plus a day, so a
    catch-up still covers every owed session) because it does not know which dates
    this module will resolve. So the date filter lives HERE: a line counts only if its own
    `journalctl -o short-iso` timestamp falls on `trading_date` in ET. Without it,
    every punctual report repeated the previous day's hits as today's.

    A line whose timestamp cannot be read is KEPT, and counted as `undated`: failing
    toward showing an alarm-pattern line is the safe direction, and the count says
    the filter could not speak for it.
    """

    lines: tuple[str, ...]
    other_days: int
    undated: int


def _journal_line_date(line: str) -> date | None:
    """The ET date of one `journalctl -o short-iso` line, or None if unreadable."""
    stamp = line.split(" ", 1)[0]
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if at.tzinfo is None:
        return None
    return at.astimezone(ET).date()


def read_journal(journal_file: Path, trading_date: date) -> JournalSweep:
    kept: list[str] = []
    other_days = undated = 0
    for ln in journal_file.read_text().splitlines():
        if not ln.strip():
            continue
        day = _journal_line_date(ln)
        if day is None:
            undated += 1
            kept.append(ln)
        elif day == trading_date:
            kept.append(ln)
        else:
            other_days += 1
    return JournalSweep(lines=tuple(kept), other_days=other_days, undated=undated)


def _journal(journal_file: Path | None, trading_date: date, max_lines: int) -> str:
    """The host journal sweep, collected by the shell and scoped to the date here.

    `journalctl` exists only on the host, so the shell greps and this renders. The
    patterns live there for that reason and are listed in `scripts/argus-eod.sh`.
    """
    if journal_file is None:
        return "  not collected (no --journal-file; this is the by-hand path)"
    if not journal_file.exists():
        raise RuntimeError(f"{journal_file} does not exist")
    sweep = read_journal(journal_file, trading_date)
    out: list[str] = []
    if not sweep.lines:
        out.append(f"  clean — no matching lines on {trading_date}")
    else:
        shown = sweep.lines[:max_lines]
        out.append(f"  {len(sweep.lines)} matching line(s) on {trading_date}:")
        out += [f"  {ln}" for ln in shown]
        if len(sweep.lines) > len(shown):
            out.append(
                f"  … {len(sweep.lines) - len(shown)} more (full journal on the droplet)"
            )
    if sweep.undated:
        out.append(
            f"  ({sweep.undated} line(s) above had no readable timestamp and are shown "
            "unfiltered)"
        )
    if sweep.other_days:
        out.append(
            f"  ({sweep.other_days} matching line(s) from other dates in the collection "
            "window left out)"
        )
    return "\n".join(out)


def _scoring(config_path: str, trading_date: date, today: date,
             half_day: bool, timeout: float) -> str:
    """`tools.mark --dry-run`, or the reason it was not run.

    Never the write path — that takes the writer lock, which needs the service
    stopped. Marking for real stays a human act on a weekend with the book flat.
    """
    hand = (
        "      python -m argus.tools.mark --config <config> "
        f"--date {trading_date} --settlement <SPX close> --dry-run"
    )
    if half_day:
        return (
            "  SKIPPED — half-day. `--from-feed` reads the CURRENT index quote as the\n"
            "  close, and this session settled early, so a report-time quote is hours of\n"
            "  drift presented as a settlement price. Re-render by hand with the close:\n"
            f"{hand}"
        )
    if trading_date != today:
        return (
            "  SKIPPED — not today. `--from-feed` cannot reach back; the feed has no\n"
            "  memory of past closes. Re-render by hand with the close:\n"
            f"{hand}"
        )
    # Imported here rather than at module scope: `tools.mark` reaches the market-data
    # session, and a by-hand run of a section that does not need it should not pay
    # for that import or its credentials.
    from argus.tools import mark as mark_tool

    return _capture(
        mark_tool.main,
        [
            "--config", config_path,
            "--date", trading_date.isoformat(),
            "--dry-run",
            "--from-feed",
            "--timeout", str(timeout),
        ],
    )


# -- assembly -----------------------------------------------------------------


def build_report(
    *,
    config: Config,
    config_path: str,
    trading_date: date,
    calendar: MarketCalendar,
    conn: sqlite3.Connection,
    status: dict[str, Any] | None,
    status_error: str | None,
    backup_dir: Path | None,
    status_skipped: bool = False,
    journal_file: Path | None,
    weekly: bool,
    mark_timeout: float,
    now_et: datetime,
    journal_max_lines: int = 40,
) -> Report:
    """Render every section and the digest. Pure of I/O except the reads passed in."""
    report = Report(trading_date=trading_date)
    start, end = et_day_bounds_utc_iso(trading_date)
    half_day = is_half_day(calendar, trading_date)
    settled = calendar.is_settled(trading_date, now_et)

    # The SHARED reads, each guarded (`Report.read`): a failure costs the sections
    # that depend on it and is named at the top of the transcript and in the
    # digest's `failures` — it no longer takes the whole report down with it.
    blackout = report.read("blackout", lambda: read_blackout_day(conn, start, end))
    report.run(
        "subject", "ARGUS EOD — SUBJECT",
        lambda: _header(config, trading_date, calendar, blackout,
                        blackout_unread=blackout is None),
    )

    day = report.read("ledger", lambda: desk.load(conn, trading_date))
    summary = None if day is None else report.read("ledger", lambda: desk.summarize(day))
    unwatchable_today = report.read("unwatchable", lambda: count_unwatchable(conn, start, end))
    series = report.read("g1", lambda: read_g1(conn, start, end))

    def flat() -> str:
        if summary is None:
            raise RuntimeError(
                "not rendered — the ledger read failed (see READS THAT FAILED). The "
                "flat state is UNKNOWN: check the broker by hand."
            )
        return _flat_check(summary, unwatchable_today, status, settled)

    def g1() -> str:
        if series is None:
            raise RuntimeError(
                "not rendered — the gate.decision read failed (see READS THAT FAILED)."
            )
        return _g1(series, status, blackout)

    report.run("flat", "1. FLAT CHECK", flat)
    report.run("g1", "2. G1 — ACCOUNT NLV AND DAY P&L", g1)

    def rails() -> str:
        # A SKIP is not a failure: `--no-status` is the by-hand path, and rendering
        # it as a failed section would teach the reader that a red section on this
        # page is normal. A read that was ATTEMPTED and failed IS a failure —
        # unverified rails on a live account is exactly what this page is for.
        if status_skipped:
            return (
                f"  not read ({status_error}). The kill switch, the breaker, the\n"
                "  watchdog row and the blackout runway are UNVERIFIED for this run."
            )
        if status_error is not None:
            raise RuntimeError(status_error)
        return _rails(status)

    report.run("rails", "3. RAILS", rails)
    report.run(
        "execution", "4. EXECUTION (tools.desk)",
        lambda: _capture(desk.main, ["--config", config_path, "--date", trading_date.isoformat()]),
    )
    report.run(
        "scoring", "5. SCORING PREVIEW (tools.mark --dry-run)",
        lambda: _scoring(config_path, trading_date, now_et.date(), half_day, mark_timeout),
    )
    report.run(
        "evaluability", "6. EVALUABILITY (tools.evaluability)",
        lambda: _capture(
            evaluability.main,
            ["--config", config_path, "--date", trading_date.isoformat()],
        ),
    )
    report.run(
        "journal", "7. JOURNAL SWEEP",
        lambda: _journal(journal_file, trading_date, journal_max_lines),
    )
    report.run(
        "backup", "8. BACKUP",
        lambda: (
            _backup(backup_dir, now_et)
            if backup_dir is not None
            else "  not checked (no --backup-dir; this is the by-hand path)"
        ),
    )
    if weekly:
        report.run(
            "regime", "9. REGIME (weekly — last trading day of the week)",
            lambda: _capture(regime.main, ["--config", config_path]),
        )

    report.digest = _digest(
        config=config,
        trading_date=trading_date,
        summary=summary,
        day=day,
        series=series,
        blackout=blackout,
        unwatchable_today=unwatchable_today,
        status=status,
        backup_dir=backup_dir,
        journal_file=journal_file,
        weekly=weekly,
        half_day=half_day,
        settled=settled,
        report=report,
        now_et=now_et,
    )
    return report


def _digest(
    *,
    config: Config,
    trading_date: date,
    summary: desk.DeskSummary | None,
    day: desk.DeskDay | None,
    series: G1Series | None,
    unwatchable_today: int | None,
    status: dict[str, Any] | None,
    backup_dir: Path | None,
    blackout: BlackoutDay | None = None,
    journal_file: Path | None,
    weekly: bool,
    half_day: bool,
    settled: bool,
    report: Report,
    now_et: datetime,
) -> dict[str, Any]:
    """Every value the Slack line needs, computed HERE and never parsed back out."""
    kill = (status or {}).get("kill_switch") or {}
    halted = (status or {}).get("halted") or {}
    breaker = (status or {}).get("daily_loss_breaker") or {}
    blackout_status = (status or {}).get("event_blackout") or {}
    watchdog = ((status or {}).get("watchdog") or {}).get("health")

    newest_snapshot, snapshot_age_h = None, None
    if backup_dir is not None and backup_dir.is_dir():
        files = sorted(backup_dir.glob("argus-*.db"), key=lambda p: p.stat().st_mtime)
        if files:
            newest_snapshot = files[-1].name
            snapshot_age_h = round(
                (now_et.timestamp() - files[-1].stat().st_mtime) / 3600.0, 1
            )

    # The SAME date-scoped read the transcript section renders, so the digest's count
    # and the page's list cannot disagree.
    journal_hits = None
    if journal_file is not None and journal_file.exists():
        journal_hits = len(read_journal(journal_file, trading_date).lines)

    source = config.source
    return {
        "schema": SCHEMA_VERSION,
        "trading_date": trading_date.isoformat(),
        "generated_at_et": now_et.isoformat(),
        "half_day": half_day,
        "settled": settled,
        "weekly": weekly,
        "environment": config.environment,
        "profile": source.profile if source else None,
        "config_file": source.filename if source else None,
        "config_sha": source.sha if source else None,
        "build": BUILD_VERSION,
        "db_path": config.audit.db_path,
        # -- the headline ------------------------------------------------------
        # A failed read is `None` throughout — UNREAD, never empty or zero. `flat`
        # None is the "FLAT STATE UNKNOWN" headline.
        "flat": None if summary is None else summary.flat,
        "held_contracts": None if summary is None else summary.held_contracts,
        "held": [] if summary is None else [
            {
                "candidate_id": h.candidate_id,
                "contracts": h.contracts,
                "spent_exit": h.spent_exit,
            }
            for h in summary.held
        ],
        "unpriceable": [] if summary is None else [u.candidate_id for u in summary.unpriceable],
        "spent_exits": [] if summary is None else list(summary.spent_exits),
        "unwatchable_records": unwatchable_today,
        # -- the day -----------------------------------------------------------
        "entry_attempts": None if day is None else len(day.entries),
        "closed_positions": None if summary is None else len(summary.closed),
        "realized": None if summary is None else round(summary.realized, 2),
        "unrealized": None if summary is None else round(summary.unrealized, 2),
        "fees": None if summary is None else round(summary.fees, 2),
        "total": None if summary is None else round(summary.total, 2),
        "gate_allowed": None if day is None else day.allowed,
        "gate_denials": None if day is None else dict(day.denials),
        # Off the CHAIN for this report's date (`read_blackout_day`), not off
        # `/status`, which answers for the moment it was polled. None = unread.
        "blackout_day": {
            "blacked_out": None if blackout is None else blackout.blacked_out,
            "announced": None if blackout is None else blackout.announced,
            "suppressed_ticks": None if blackout is None else blackout.suppressed_ticks,
        },
        # -- G1 ----------------------------------------------------------------
        "g1": None if series is None else {
            "rows": series.rows,
            "no_account_rows": series.no_account,
            "unanchored_rows": series.unanchored,
            "has_series": series.has_series,
            "pnl_moved": series.pnl_moved,
            "nlv_first": series.nlv[0] if series.nlv else None,
            "nlv_last": series.nlv[-1] if series.nlv else None,
            "nlv_min": min(series.nlv) if series.nlv else None,
            "nlv_max": max(series.nlv) if series.nlv else None,
            "pnl_first": series.daily_pnl[0] if series.daily_pnl else None,
            "pnl_last": series.daily_pnl[-1] if series.daily_pnl else None,
            "pnl_min": min(series.daily_pnl) if series.daily_pnl else None,
            "pnl_max": max(series.daily_pnl) if series.daily_pnl else None,
        },
        # -- rails -------------------------------------------------------------
        "rails": {
            "status_read": status is not None,
            "kill_switch_engaged": kill.get("engaged"),
            "kill_switch_since": kill.get("since"),
            "halted": halted.get("halted"),
            "halt_grade": halted.get("grade"),
            "breaker_tripped": breaker.get("tripped"),
            "watchdog_degraded": None if watchdog is None else watchdog.get("degraded"),
            "blackout_state": blackout_status.get("state"),
            "blackout_days_remaining": blackout_status.get("days_remaining"),
            # The next session, as of the /status poll. None = unread, or a page
            # older than v0.4.4.
            "blackout_next_session": blackout_status.get("next_session_blacked_out"),
            "next_session_open": ((status or {}).get("session") or {}).get(
                "next_session_open"
            ),
        },
        # -- the job itself ----------------------------------------------------
        "snapshot": {"newest": newest_snapshot, "age_h": snapshot_age_h},
        "journal_hits": journal_hits,
        "sections": {s.key: ("ok" if s.ok else "failed") for s in report.sections},
        "failures": report.failures,
    }


# -- the digest, as something a phone can read ---------------------------------


def slack_text(digest: dict[str, Any]) -> str:
    """The digest as a Slack message. Built from VALUES, never from the transcript.

    Deliberately rendered here rather than in the shell: `jq` is not guaranteed on
    the host, bash JSON-escaping is its own class of bug, and — the real reason —
    the moment the shell has to pick numbers out of text, it is parsing prose to
    decide what to say. The shell POSTs a file this writes and makes no decisions.
    """
    flat = digest.get("flat")
    failures = digest.get("failures") or []
    rails = digest.get("rails") or {}
    g1_unread = "g1" in digest and digest["g1"] is None
    g1 = digest.get("g1") or {}
    snapshot = digest.get("snapshot") or {}

    if flat is True:
        head = ":white_check_mark: FLAT"
    elif flat is False and digest.get("unpriceable"):
        head = (
            f":rotating_light: FLAT STATE UNREADABLE — {len(digest['unpriceable'])} "
            "unpriceable position(s)"
        )
    elif flat is False and digest.get("settled"):
        head = (
            f":warning: HELD TO EXPIRY — {digest.get('held_contracts')} contract(s) "
            "settled, not closed by an order"
        )
    elif flat is False:
        head = f":rotating_light: NOT FLAT — {digest.get('held_contracts')} contract(s) OPEN"
    else:
        head = ":warning: FLAT STATE UNKNOWN"

    alarms: list[str] = []
    if rails.get("kill_switch_engaged"):
        alarms.append(f"kill switch ENGAGED (since {rails.get('kill_switch_since')})")
    if rails.get("halted"):
        alarms.append(f"HALTED (grade {rails.get('halt_grade')})")
    if rails.get("breaker_tripped"):
        alarms.append("daily-loss breaker TRIPPED")
    if rails.get("watchdog_degraded"):
        alarms.append("watchdog DEGRADED")
    if digest.get("spent_exits"):
        alarms.append(f"{len(digest['spent_exits'])} spent exit attempt(s)")
    if "unwatchable_records" in digest and digest["unwatchable_records"] is None:
        alarms.append("unwatchable count UNREAD")
    elif digest.get("unwatchable_records"):
        alarms.append(f"{digest['unwatchable_records']} unwatchable record(s)")
    if digest.get("unpriceable"):
        alarms.append(f"{len(digest['unpriceable'])} unpriceable position(s)")
    if rails.get("status_read") is False:
        alarms.append("/status unread — rails unverified")
    age = snapshot.get("age_h")
    if snapshot.get("newest") is None or (age is not None and age > 26):
        alarms.append("snapshot stale or missing")
    if rails.get("blackout_state") not in (None, "ok", "silent"):
        alarms.append(f"blackout list {rails.get('blackout_state')}")
    blackout_day = (digest.get("blackout_day") or {}).get("blacked_out") is True
    # The one blackout reading that IS an alarm: the strategy refuses every
    # candidate on a blackout day, so an entry attempt on one means the rail did not
    # hold. Not a state this system can reach by design — which is why it pages.
    if blackout_day and digest.get("entry_attempts"):
        alarms.append(
            f"{digest['entry_attempts']} entry attempt(s) on an EVENT BLACKOUT day"
        )

    # G1 is a LINE, not a number: on a day that held a position, "one value all
    # session" is the finding, and a bare last-value would render that identically
    # to a healthy day.
    if g1_unread:
        g1_line = "UNREAD (the gate.decision read failed — see the transcript)"
    elif not g1.get("has_series") and blackout_day:
        g1_line = "no series (event blackout — no entries by design; G1 still unread)"
    elif not g1.get("has_series"):
        g1_line = "no series (no candidates evaluated against the account)"
    else:
        pnl_moved = g1.get("pnl_moved")
        moved = (
            "NO DAY-P&L READINGS" if pnl_moved is None
            else "moved" if pnl_moved else "ONE VALUE ALL SESSION"
        )
        g1_line = (
            f"nlv {g1.get('nlv_first')} -> {g1.get('nlv_last')} | "
            f"day P&L {g1.get('pnl_first')} -> {g1.get('pnl_last')} ({moved})"
        )
    if g1.get("no_account_rows"):
        g1_line += f" [{g1['no_account_rows']} row(s) with NO ACCOUNT STATE excluded]"
    if g1.get("unanchored_rows"):
        g1_line += f" [{g1['unanchored_rows']} UNANCHORED day-P&L value(s) excluded]"

    body = [
        f"date        {digest.get('trading_date')}"
        f"{'  (half-day)' if digest.get('half_day') else ''}",
        f"world       {digest.get('environment')}  {digest.get('build')}  "
        f"{digest.get('config_file')}@{digest.get('config_sha')}",
        f"entries     {digest.get('entry_attempts')} attempt(s), "
        f"{digest.get('closed_positions')} closed, {digest.get('held_contracts')} held"
        f"{'  — EVENT BLACKOUT, no entries by design' if blackout_day else ''}",
        f"P&L         realized {digest.get('realized')}  "
        f"unrealized {digest.get('unrealized')}  fees {digest.get('fees')}  "
        f"total {digest.get('total')}",
        f"G1          {g1_line}",
        f"rails       kill={rails.get('kill_switch_engaged')} "
        f"halted={rails.get('halted')} breaker={rails.get('breaker_tripped')} "
        f"blackout={rails.get('blackout_days_remaining')}d",
        # Only when the next session IS blacked out: a heads-up, not a status line,
        # so an ordinary evening adds nothing. From /status, as of the poll.
        *(
            [f"next        EVENT BLACKOUT — no entries at the {rails.get('next_session_open')} session"]
            if rails.get("blackout_next_session") is True
            else []
        ),
        f"backup      {snapshot.get('newest')} ({age}h)",
        f"journal     {digest.get('journal_hits')} matching line(s)",
        f"report      {digest.get('transcript')}",
    ]

    lines = [f"{head}  —  *Argus EOD {digest.get('trading_date')}*"]
    if alarms:
        lines.append(":exclamation: " + "; ".join(alarms))
    if failures:
        lines.append(
            f":x: {len(failures)} section(s)/read(s) FAILED: " + "; ".join(failures)
        )
    lines.append("```\n" + "\n".join(body) + "\n```")
    return "\n".join(lines)


def slack_payload(digest: dict[str, Any]) -> dict[str, Any]:
    return {"text": slack_text(digest)}


# -- entry point --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        required=True,
        help="path to the config file (REQUIRED — there is no default; a default "
        "path is how the live database gets opened by accident)",
    )
    parser.add_argument(
        "--date",
        help="ET trading date (YYYY-MM-DD); default the latest SETTLED session — "
        "with --out-dir, every settled session since the newest report there",
    )
    parser.add_argument(
        "--out-dir",
        help="write <prefix>-<date>.txt and .json here; omitted prints the transcript",
    )
    parser.add_argument(
        "--host-report-dir",
        help="the --out-dir as the HOST sees it, for the path the Slack line names "
        "(the container's /reports means nothing on the droplet)",
    )
    parser.add_argument("--backup-dir", help="where the nightly snapshots land")
    parser.add_argument("--journal-file", help="file of pre-grepped journal lines")
    parser.add_argument(
        "--weekly",
        choices=("auto", "yes", "no"),
        default="auto",
        help="run the regime section; auto = the week's LAST trading day (default)",
    )
    parser.add_argument("--no-status", action="store_true", help="skip the /status read")
    parser.add_argument("--status-timeout", type=float, default=10.0)
    parser.add_argument("--mark-timeout", type=float, default=30.0)
    parser.add_argument(
        "--force",
        action="store_true",
        help="report even when the date is not a trading day",
    )
    args = parser.parse_args(argv)

    load_env()
    config = Config.load(args.config)
    calendar = MarketCalendar.from_config(config.market)
    explicit = args.date is not None
    now = et_now()

    if explicit:
        dates: tuple[date, ...] = (date.fromisoformat(args.date),)
    elif args.out_dir:
        # The catch-up rule (module docstring): an undated run renders each OWED day
        # once — every settled session since the newest report on disk, oldest
        # first — so an outage across two sessions no longer reports the second and
        # silently skips the first.
        owed = owed_days(calendar, now, Path(args.out_dir))
        if owed.too_old:
            print(
                f"argus-eod: {len(owed.too_old)} unreported session(s) older than "
                f"{CATCH_UP_DAYS} days NOT caught up: "
                + ", ".join(d.isoformat() for d in owed.too_old)
                + " — re-render by hand with ARGUS_EOD_DATE if wanted."
            )
        if not owed.render:
            latest = latest_settled_session(calendar, now)
            print(f"argus-eod: {latest} is already reported — nothing to do.")
            return 0
        dates = owed.render
    else:
        dates = (latest_settled_session(calendar, now),)

    # THE TRADING-DAY GUARD, here rather than in `OnCalendar`. The timer fires every
    # day so `Persistent=true` can catch up a missed run, and one authority answers
    # "was today a session?" — the same calendar the scheduler uses. Only an explicit
    # date can reach it; the resolved ones are trading days by construction.
    if explicit and not calendar.is_trading_day(dates[0]) and not args.force:
        print(f"argus-eod: {dates[0]} is not a trading day — nothing to report.")
        return 0

    # AN UNSETTLED DAY IS NEVER WRITTEN TO --out-dir. A file there is the day's
    # report: the render-once rule treats it as done and the script posts it. A
    # preview taken mid-session (`ARGUS_EOD_DATE=<today>` at 14:00) would otherwise
    # stand as the day's report — `NOT FLAT — OPEN`, scoring refused before 16:15 —
    # and the 16:45 run would find nothing owed. So it is PRINTED instead: the
    # preview still happens, and the punctual report is still owed.
    out_dir = args.out_dir
    if explicit and out_dir and not calendar.is_settled(dates[0], now):
        print(
            f"argus-eod: {dates[0]} has not settled yet — printing this preview "
            f"instead of writing it to {args.host_report_dir or out_dir}, so it cannot "
            "stand as the day's report. Re-run after settlement for the real one."
        )
        out_dir = None

    # ONE `/status` read for the run. It describes NOW, so on a catch-up of an older
    # day its rails are the current rails — which is what the RAILS section says.
    status: dict[str, Any] | None = None
    status_error: str | None = None
    if args.no_status:
        status_error = "skipped (--no-status)"
    else:
        try:
            status = fetch_status(config, os.environ.get(KILL_TOKEN_VAR), args.status_timeout)
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            status_error = f"{type(exc).__name__}: {exc}"

    db_path = Path(config.audit.db_path)
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for trading_date in dates:
            weekly = (
                is_last_trading_day_of_week(calendar, trading_date)
                if args.weekly == "auto"
                else args.weekly == "yes"
            )
            report = build_report(
                config=config,
                config_path=args.config,
                trading_date=trading_date,
                calendar=calendar,
                conn=conn,
                status=status,
                status_error=status_error,
                status_skipped=args.no_status,
                backup_dir=Path(args.backup_dir) if args.backup_dir else None,
                journal_file=Path(args.journal_file) if args.journal_file else None,
                weekly=weekly,
                mark_timeout=args.mark_timeout,
                now_et=et_now(),
            )
            _emit(report, trading_date, out_dir, args.host_report_dir)
    finally:
        conn.close()
    return 0


def _emit(report: Report, trading_date: date, out_dir_arg: str | None,
          host_report_dir: str | None) -> None:
    """Print the transcript, or write the three artifacts.

    **The `.txt` is written LAST, and atomically.** It is the render-once key: its
    existence is what makes an undated run treat the day as reported. Written first,
    a crash between it and the Slack payload left a day marked done with nothing to
    deliver; written last via a rename, a day is either fully written or owed.
    """
    transcript = report.transcript()
    if not out_dir_arg:
        print(transcript, end="")
        return

    out_dir = Path(out_dir_arg)
    out_dir.mkdir(parents=True, exist_ok=True)
    txt = out_dir / f"{REPORT_PREFIX}-{trading_date}.txt"
    js = out_dir / f"{REPORT_PREFIX}-{trading_date}.json"
    slack = out_dir / f"{REPORT_PREFIX}-{trading_date}.slack.json"
    report.digest["transcript"] = str(
        Path(host_report_dir) / txt.name if host_report_dir else txt
    )
    js.write_text(json.dumps(report.digest, indent=2, sort_keys=True) + "\n")
    # A ready-to-POST webhook body, so delivery is `curl --data-binary @file` and the
    # shell never escapes JSON by hand. The transcript is NOT in it: a webhook cannot
    # upload a file and a long code block truncates, so Slack carries the digest and
    # the droplet + NAS carry the text.
    slack.write_text(json.dumps(slack_payload(report.digest)) + "\n")
    partial = txt.with_name(f".{txt.name}.partial")
    partial.write_text(transcript)
    partial.replace(txt)
    # One line for the journal. The report itself is a file and Slack's business;
    # duplicating it into journald would double the box's log volume for nothing.
    print(
        f"argus-eod: wrote {txt} "
        f"(flat={report.digest['flat']}, failures={len(report.failures)})"
    )


if __name__ == "__main__":
    raise SystemExit(main())
