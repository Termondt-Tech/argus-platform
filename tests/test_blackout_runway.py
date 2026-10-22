"""The macro-event blackout list's expiry, on `/status` and at startup.

`blackout_dates.txt` is a FINITE generated file — 5 months of FOMC/CPI/NFP/PCE dates
as generated 2026-08-04, ending 2026-12-23 — and the day it runs out Argus resumes
trading those days as ordinary days. **Nothing inside the process can tell that from a
quiet calendar**: the blackout's only observable is entries not happening, so an
exhausted list and a month with no macro events produce identical behaviour, identical
logs and identical audit records. The 2026-08-04 decision was a calendar reminder;
this is the field that replaces it.

Two properties are load-bearing and both are tested here:

  - it is SILENT while the runway is healthy. A permanent amber banner for a
    condition that is fine eleven months of the year trains the eye to skip the row on
    the one month it is not.
  - it never becomes a rail. An unreadable file is captured as page state, not raised
    — startup must not acquire a new way to refuse.

And since 2026-09-18 it answers the question the horizon never did: **is TODAY a
blackout day?** On 2026-09-11 (CPI) the page read `state: ok` and announced a 09:40
session open with no hint that the session would take no entries, and until 10:00 ET
the audit trail said only `outside_entry_window`. The day-level half is tested at the
bottom of this file, the session-start record beside it.
"""

from __future__ import annotations

import pathlib
from datetime import date, datetime, timedelta

import pytest

from argus.app import Application, read_blackout_runway
from argus.audit.log import AuditLog
from argus.clock import ET
from argus.config import EventConfig
from argus.session import EVENT_BLACKOUT_DAY, _announce_blackout_day
from argus.status import BLACKOUT_WARN_DAYS, BlackoutRunway

from tests.support import make_config
from tests.test_status import NOW, _make_service

TODAY = NOW.date()


def _events(tmp_path: pathlib.Path, body: str, *, enabled: bool = True) -> EventConfig:
    path = tmp_path / "blackout_dates.txt"
    path.write_text(body)
    return EventConfig(blackout_dates_path=str(path), enabled=enabled)


def _file(*dates: date) -> str:
    return "# generated\n\n" + "\n".join(d.isoformat() for d in dates) + "\n"


async def _snapshot(db, runway, *, next_open=None):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    extra = {} if next_open is None else {"next_open": lambda: next_open}
    svc, _ss, _ks = _make_service(audit, blackout=runway, **extra)
    try:
        snap = await svc.assemble()
        return snap, snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()


# -- reading the list ---------------------------------------------------------


def test_the_horizon_is_the_max_date_in_the_file_argus_actually_loads(tmp_path):
    """`fomc_dates.txt` is an input to the GENERATOR, never to the runtime — the
    strategy sees one frozenset from `[events] blackout_dates_path` and nothing else.
    Reading the generator's inputs here would report a runway the running strategy
    does not have."""
    events = _events(
        tmp_path, _file(date(2026, 9, 16), date(2026, 12, 23), date(2026, 10, 28))
    )
    runway = read_blackout_runway(events)
    assert (runway.enabled, runway.last_date, runway.date_count) == (
        True, date(2026, 12, 23), 3,
    )
    assert runway.error is None


def test_no_events_section_reports_disabled_rather_than_exhausted():
    """`[events]` absent and `enabled = false` are the same runtime fact — no
    stand-down — and neither is an expiry problem."""
    assert read_blackout_runway(None).state(TODAY) == "disabled"


def test_a_disabled_section_still_reports_disabled(tmp_path):
    events = _events(tmp_path, _file(date(2026, 12, 23)), enabled=False)
    assert read_blackout_runway(events).state(TODAY) == "disabled"


def test_an_unreadable_file_is_captured_as_state_never_raised(tmp_path):
    """Startup must not acquire a new way to refuse. The condition is already fatal
    where it counts — `load_blackout_dates` raises at SESSION start — so raising here
    too would cost the operator the page that explains why the day aborted."""
    events = EventConfig(
        blackout_dates_path=str(tmp_path / "does-not-exist.txt"), enabled=True
    )
    runway = read_blackout_runway(events)
    assert runway.state(TODAY) == "unreadable"
    assert runway.date_count is None and runway.error


def test_a_malformed_line_is_unreadable_too_rather_than_a_partial_list(tmp_path):
    """`load_blackout_dates` is fail-closed on a bad line rather than skipping it —
    a silently-dropped FOMC date defeats the point — so this reads as unreadable, not
    as a short list."""
    events = _events(tmp_path, "2026-09-16\nnot-a-date\n")
    assert read_blackout_runway(events).state(TODAY) == "unreadable"


# -- the thresholds -----------------------------------------------------------


@pytest.mark.parametrize(
    ("offset_days", "state"),
    [
        (365, "ok"),
        (BLACKOUT_WARN_DAYS + 1, "ok"),
        (BLACKOUT_WARN_DAYS, "expiring"),      # inclusive: the warning starts here
        (1, "expiring"),
        (0, "expiring"),                       # the last covered day is still covered
        (-1, "exhausted"),
    ],
)
def test_the_state_walks_ok_to_expiring_to_exhausted(offset_days, state):
    runway = BlackoutRunway(
        enabled=True, last_date=TODAY + timedelta(days=offset_days), date_count=7
    )
    assert runway.state(TODAY) == state


def test_an_empty_but_valid_file_is_exhausted_not_ok(tmp_path):
    """`load_blackout_dates` accepts a file of pure comments, and the generator's own
    refusal to write an empty one covers GENERATION — not a file hand-trimmed
    afterwards. A list covering nothing is the exact condition this field exists for,
    so it must not read as healthy."""
    events = _events(tmp_path, "# every date has been removed\n")
    runway = read_blackout_runway(events)
    assert (runway.date_count, runway.last_date) == (0, None)
    assert runway.state(TODAY) == "exhausted"


def test_the_countdown_is_derived_live_rather_than_captured():
    """The process runs for weeks. A days-remaining computed at startup would be
    wrong by however long it has been up — the same live-derivation rule
    `next_session_open` follows."""
    runway = BlackoutRunway(enabled=True, last_date=date(2026, 12, 23), date_count=20)
    assert runway.days_remaining(date(2026, 12, 1)) == 22
    assert runway.days_remaining(date(2026, 12, 23)) == 0
    assert runway.days_remaining(date(2026, 12, 24)) == -1


# -- the page -----------------------------------------------------------------


async def test_a_healthy_runway_is_silent(db):
    """The design constraint. Eleven months of the year this row must carry no colour
    and no badge — otherwise the amber that appears in month twelve says nothing."""
    _snap, js, html_out = await _snapshot(
        db, BlackoutRunway(enabled=True, last_date=TODAY + timedelta(days=140),
                           date_count=20)
    )
    assert js["event_blackout"]["state"] == "ok"
    assert js["event_blackout"]["days_remaining"] == 140
    assert js["event_blackout"]["today_blacked_out"] is False

    cell = html_out[html_out.index("Event blackout"):].split("</tr>")[0]
    assert "20 dates" in cell and "140 days" in cell
    for loud in ("EXPIRING", "EXHAUSTED", "class='warn'", "class='bad'", "badge"):
        assert loud not in cell, f"a healthy runway must not render {loud!r}"
    # And an ordinary day adds nothing anywhere else on the page either.
    assert "NONE TODAY" not in html_out
    assert "no entries that day" not in html_out


async def test_an_expiring_list_says_when_and_says_how_to_fix_it(db):
    """Amber, not red: nothing is broken yet. And it names the tool, because an
    operator reading this on a phone should not have to go and find it."""
    _snap, js, html_out = await _snapshot(
        db, BlackoutRunway(enabled=True, last_date=TODAY + timedelta(days=12),
                           date_count=4)
    )
    assert js["event_blackout"] == {
        "state": "expiring",
        "enabled": True,
        "last_date": (TODAY + timedelta(days=12)).isoformat(),
        "days_remaining": 12,
        "date_count": 4,
        "warn_within_days": BLACKOUT_WARN_DAYS,
        "error": None,
        "today_blacked_out": False,
        "next_session_blacked_out": False,
    }
    cell = html_out[html_out.index("Event blackout"):].split("</tr>")[0]
    assert "EXPIRING" in cell and "class='warn'" in cell
    assert "argus.tools.blackout_dates" in cell
    assert "class='bad'" not in cell


async def test_an_exhausted_list_says_what_is_actually_happening_now(db):
    """RED, and in words. "Exhausted" alone is a fact about a file; what the operator
    needs is the consequence — macro-event days are being traded as ordinary days."""
    _snap, js, html_out = await _snapshot(
        db, BlackoutRunway(enabled=True, last_date=TODAY - timedelta(days=3),
                           date_count=20)
    )
    assert js["event_blackout"]["state"] == "exhausted"
    assert js["event_blackout"]["days_remaining"] == -3
    cell = html_out[html_out.index("Event blackout"):].split("</tr>")[0]
    assert "EXHAUSTED" in cell and "class='bad'" in cell
    assert "traded as ordinary days" in cell


async def test_an_unreadable_list_also_predicts_the_day_aborting(db):
    """The same file the page cannot read is the one `run_session` loads, and there it
    is a hard `ConfigError`. Saying so turns a silent day-abort into an expected one."""
    _snap, js, html_out = await _snapshot(
        db, BlackoutRunway(enabled=True, error="blackout dates file not found: /x")
    )
    assert js["event_blackout"]["state"] == "unreadable"
    cell = html_out[html_out.index("Event blackout"):].split("</tr>")[0]
    assert "UNREADABLE" in cell and "abort" in cell


async def test_a_disabled_blackout_says_entries_are_not_suppressed(db):
    """Not a warning — it is a deliberate config choice — but it is the only surface
    that says the stand-down is off at all."""
    _snap, js, html_out = await _snapshot(db, BlackoutRunway(enabled=False))
    assert js["event_blackout"]["state"] == "disabled"
    cell = html_out[html_out.index("Event blackout"):].split("</tr>")[0]
    assert "NOT suppressed" in cell
    assert "class='bad'" not in cell and "class='warn'" not in cell


async def test_a_caller_that_publishes_nothing_renders_a_dash(db):
    """Every construction site predating the field, which must keep working."""
    snap, js, html_out = await _snapshot(db, None)
    assert (snap.blackout, snap.blackout_state, snap.blackout_days_remaining) == (
        None, None, None,
    )
    assert js["event_blackout"] is None
    assert "Event blackout" in html_out


# -- the startup log ----------------------------------------------------------


async def test_startup_logs_the_runway_at_the_severity_it_deserves(
    tmp_path: pathlib.Path, caplog
):
    """Field AND log line — the shape the existing staleness/heartbeat observability
    already uses, rather than picking one. INFO on a healthy runway is not the same as
    silent: it is what lets a deploy be checked against the file that shipped with it.
    """
    path = tmp_path / "blackout_dates.txt"
    path.write_text(_file(date(2027, 12, 23)))
    cfg = make_config(
        audit={"db_path": str(tmp_path / "argus.db")},
        events={"enabled": True, "blackout_dates_path": str(path)},
    )
    app = Application(cfg, now_et=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=ET))
    with caplog.at_level("INFO", logger="argus.app"):
        await app.startup()
    try:
        (line,) = [r for r in caplog.records if "event blackout" in r.getMessage()]
        assert line.levelname == "INFO"
        assert "2027-12-23" in line.getMessage()

        snap = await app.status_service.assemble()
        assert snap.blackout_state == "ok"
    finally:
        await app.shutdown()


async def test_an_exhausted_list_warns_at_startup_and_still_starts(
    tmp_path: pathlib.Path, caplog
):
    """WARNING, and the process comes up anyway. Visibility, not a rail: refusing to
    start over a stale calendar file would take the phone-reachable surface down with
    it, which is the trade `_halt_process` already refuses to make."""
    path = tmp_path / "blackout_dates.txt"
    path.write_text(_file(date(2026, 1, 5)))
    cfg = make_config(
        audit={"db_path": str(tmp_path / "argus.db")},
        events={"enabled": True, "blackout_dates_path": str(path)},
    )
    app = Application(cfg, now_et=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=ET))
    with caplog.at_level("INFO", logger="argus.app"):
        await app.startup()
    try:
        (line,) = [r for r in caplog.records if "event blackout" in r.getMessage()]
        assert line.levelname == "WARNING"
        assert "EXHAUSTED" in line.getMessage()
        snap = await app.status_service.assemble()
        assert snap.blackout_state == "exhausted"
    finally:
        await app.shutdown()


# -- the DAY, not just the file (2026-09-18) ----------------------------------


def _today_runway(*dates: date, last: date | None = None) -> BlackoutRunway:
    """A healthy (`ok`) runway holding `dates`, so only the day-level answer varies."""
    return BlackoutRunway(
        enabled=True,
        last_date=last or TODAY + timedelta(days=140),
        date_count=len(dates),
        dates=frozenset(dates),
    )


def test_the_runway_carries_the_set_the_strategy_is_built_from(tmp_path):
    """Same loader, same file, same set — `read_blackout_runway` used to compute
    `max()` and `len()` and throw the set itself away, which is why the page could
    never say whether today was covered."""
    days = (date(2026, 9, 11), date(2026, 9, 16), date(2026, 12, 24))
    runway = read_blackout_runway(_events(tmp_path, _file(*days)))
    assert runway.dates == frozenset(days)
    assert runway.covers(date(2026, 9, 11)) is True
    assert runway.covers(date(2026, 9, 14)) is False


def test_disabled_covers_nothing_and_unreadable_cannot_say(tmp_path):
    """Disabled is a DEFINITE answer — nothing is suppressed. Unreadable is not an
    answer at all, and must not render as "an ordinary day"."""
    assert read_blackout_runway(None).covers(TODAY) is False
    missing = EventConfig(blackout_dates_path=str(tmp_path / "gone.txt"), enabled=True)
    assert read_blackout_runway(missing).covers(TODAY) is None


async def test_a_blackout_day_is_named_on_the_page_and_is_not_amber(db):
    """The 2026-09-11 page, fixed. Three places, one fact:

      - an `Entries` row under the phase, where the entries-ended stand-down already
        lives, because `idle`/`running` on its own is true and misleading;
      - `IN FORCE TODAY` ahead of the runway, the row that used to read calm;
      - `today_blacked_out: true` in the JSON, beside the `state` that stays `ok`.

    GREY, never amber: planned, expected, nothing to do. And `next_session_open` is
    NOT moved — the session still opens; it just takes no entries.
    """
    snap, js, html_out = await _snapshot(db, _today_runway(TODAY))
    assert js["event_blackout"]["state"] == "ok"
    assert js["event_blackout"]["today_blacked_out"] is True
    assert js["session"]["next_session_open"] == snap.next_session_open.isoformat()

    entries = html_out[html_out.index("<th>Entries</th>"):].split("</tr>")[0]
    assert "NONE TODAY — event blackout" in entries
    assert "session still runs" in entries

    cell = html_out[html_out.index("Event blackout"):].split("</tr>")[0]
    # The badge LEADS the runway; the runway is still there.
    assert cell.index("IN FORCE TODAY") < cell.index("1 date, through")
    for row in (entries, cell):
        assert "class='warn'" not in row and "class='bad'" not in row
        assert "badge warn" not in row and "badge bad" not in row


async def test_the_next_session_says_when_it_will_take_no_entries(db):
    """The field that WAS the complaint. Mid-session, `next_session_open` is
    tomorrow's open, so this is a separate question from "today" — asked of the
    next session's own date."""
    nxt = datetime(2026, 9, 16, 9, 40, tzinfo=ET)
    _snap, js, html_out = await _snapshot(
        db, _today_runway(nxt.date()), next_open=nxt
    )
    assert js["event_blackout"]["today_blacked_out"] is False
    assert js["event_blackout"]["next_session_blacked_out"] is True

    row = html_out[html_out.index("Next session open"):].split("</tr>")[0]
    assert "event blackout: no entries that day" in row
    assert "<th>Entries</th>" not in html_out, "today is an ordinary day"


async def test_an_unreadable_list_leaves_the_day_unknown_rather_than_ordinary(db):
    _snap, js, html_out = await _snapshot(
        db, BlackoutRunway(enabled=True, error="blackout dates file not found: /x")
    )
    assert js["event_blackout"]["today_blacked_out"] is None
    assert js["event_blackout"]["next_session_blacked_out"] is None
    assert "NONE TODAY" not in html_out


async def test_startup_reads_today_off_the_file_it_loaded(tmp_path: pathlib.Path):
    """End to end through `Application.startup`: the ET date of the app's clock is in
    the file, so the page says so — with no new startup log line (the runway line
    stays the only one; the per-day line belongs to the SESSION)."""
    path = tmp_path / "blackout_dates.txt"
    path.write_text(_file(date(2026, 9, 2), date(2026, 12, 24)))
    cfg = make_config(
        audit={"db_path": str(tmp_path / "argus.db")},
        events={"enabled": True, "blackout_dates_path": str(path)},
    )
    app = Application(cfg, now_et=lambda: datetime(2026, 9, 2, 8, 45, tzinfo=ET))
    await app.startup()
    try:
        snap = await app.status_service.assemble()
        assert snap.blackout_state == "ok"
        assert snap.blackout_today is True
    finally:
        await app.shutdown()


# -- the session's half: said at session start, not at 10:00 -----------------


def _blackout_rows(db) -> list[tuple[int, str, dict]]:
    import json

    return [
        (tier, cat, json.loads(payload))
        for tier, cat, payload in db.execute(
            "SELECT tier, category, payload FROM audit_log WHERE category = ?",
            (EVENT_BLACKOUT_DAY,),
        )
    ]


async def test_a_blackout_day_is_recorded_at_session_start(db, capsys):
    """Tier 1, once, naming the day — and a journal line. Before this, the first
    evidence in either place was `signal.none: event_blackout` at 10:00:00 ET, because
    the entry-window check runs first (correctly) and hides it all morning."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    try:
        day = date(2026, 9, 16)
        assert await _announce_blackout_day(
            audit, trading_date=day, blackout_dates=frozenset({day})
        ) is True
    finally:
        await audit.aclose()
    assert _blackout_rows(db) == [(1, EVENT_BLACKOUT_DAY, {"trading_date": "2026-09-16"})]
    assert "2026-09-16 IS A BLACKOUT DAY" in capsys.readouterr().out


async def test_an_ordinary_day_writes_nothing(db, capsys):
    """A row whose PRESENCE is the answer — so an ordinary day must add none, or the
    row stops meaning anything."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    try:
        assert await _announce_blackout_day(
            audit,
            trading_date=date(2026, 9, 14),
            blackout_dates=frozenset({date(2026, 9, 16)}),
        ) is False
    finally:
        await audit.aclose()
    assert _blackout_rows(db) == []
    assert "BLACKOUT DAY" not in capsys.readouterr().out


def test_the_session_announces_off_the_same_set_the_strategy_holds():
    """The announcement is only the session's verdict if it reads the frozenset the
    strategy is built from. Pinned structurally in `run_session`: both calls take the
    one local `blackout_dates`, so they cannot drift onto two different reads."""
    import ast
    import inspect

    from argus import session as session_mod

    tree = ast.parse(inspect.getsource(session_mod.run_session))

    def _kw_names(func_name: str) -> list[str]:
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                if name == func_name:
                    out += [
                        kw.value.id for kw in node.keywords
                        if kw.arg == "blackout_dates" and isinstance(kw.value, ast.Name)
                    ]
        return out

    assert _kw_names("CreditSpreadStrategy") == ["blackout_dates"]
    assert _kw_names("_announce_blackout_day") == ["blackout_dates"]
