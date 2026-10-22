"""The automated EOD report (`argus.tools.eod`).

What these pin is the handful of properties that make an UNATTENDED report
trustworthy, which are not the same properties a hand-run tool needs:

- a failed section still ships the report, and the flat check is above all of them;
- "no series" is not `0.00` — the G1 reading a blackout day produces must not be
  pixel-identical to the reading a dead rail produces;
- the digest is computed from values, so the Slack line cannot be built by grepping
  a transcript;
- the arithmetic is `tools.desk`'s, once — a digest that scores the day a second way
  is a second scorer that nothing compares;
- and it can never take the writer lock, because the timer runs while the trading
  process holds it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time
from pathlib import Path

import pytest

from argus.clock import ET, et_day_bounds_utc_iso
from argus.config import Config
from argus.marketcalendar import MarketCalendar
from argus.tools import desk, eod

REPO = Path(__file__).resolve().parents[1]

TRADING_DATE = date(2026, 8, 13)          # a Thursday
OPENED_AT = "2026-08-13T14:00:08.900000Z"  # 10:00 ET
CLOSED_AT = "2026-08-13T14:21:58.500000Z"  # 10:21 ET
MARKED_AT = "2026-08-13T18:30:00.000000Z"  # 14:30 ET
GATED_AT = "2026-08-13T14:00:00.000000Z"

CANDIDATE = "20260813-call-7825-7830"


# -- fixtures -----------------------------------------------------------------


@pytest.fixture
def config(tmp_path: Path, db: sqlite3.Connection) -> Config:
    """A real dev profile, pointed at the test database.

    Loaded through `Config.load` rather than constructed, so the filename↔profile
    assertion is armed exactly as it is in production.
    """
    text = (REPO / "argus.dev.toml").read_text()
    db_path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    text = text.replace('db_path = "dev/argus-dev.db"', f'db_path = "{db_path}"')
    text = text.replace(
        'blackout_dates_path = "blackout_dates.txt"',
        f'blackout_dates_path = "{REPO / "blackout_dates.txt"}"',
    )
    path = tmp_path / "argus.dev.toml"
    path.write_text(text)
    return Config.load(path)


@pytest.fixture(autouse=True)
def _no_live_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any test whose `now_et` falls on the trading date reaches `_scoring`'s real
    branch, which is `tools.mark --from-feed` — a LIVE market-data session. Stubbed
    at the tool, so the tests of `_scoring`'s own skip branches still run for real."""
    from argus.tools import mark

    monkeypatch.setattr(mark, "main", lambda *a, **k: print("  (tools.mark stubbed)"))


@pytest.fixture
def calendar(config: Config) -> MarketCalendar:
    return MarketCalendar.from_config(config.market)


async def _fill(audit, *, direction, action, symbol, contracts, price, ts):
    audit._clock = lambda: ts
    await audit.record(
        "order.fill",
        {
            "broker_order_id": 1311951 if direction == "open" else 1311952,
            "external_identifier": f"argus-{direction}-{CANDIDATE}",
            "symbol": symbol,
            "action": action,
            "contracts": contracts,
            "signed_delta": contracts if action.startswith("Buy") else -contracts,
            "price": price,
            "position_before": 0,
            "position_after": 0,
            "status": "Filled",
            "candidate_id": None,
        },
    )


async def _open_position(audit, *, contracts=2):
    await _fill(audit, direction="open", action="Sell to Open",
                symbol="SPXW  260813C07825000", contracts=contracts, price=2.00,
                ts=OPENED_AT)
    await _fill(audit, direction="open", action="Buy to Open",
                symbol="SPXW  260813C07830000", contracts=contracts, price=1.45,
                ts=OPENED_AT)


async def _close_position(audit, *, contracts=2):
    await _fill(audit, direction="close", action="Buy to Close",
                symbol="SPXW  260813C07825000", contracts=contracts, price=4.20,
                ts=CLOSED_AT)
    await _fill(audit, direction="close", action="Sell to Close",
                symbol="SPXW  260813C07830000", contracts=contracts, price=3.00,
                ts=CLOSED_AT)


async def _mark(audit, *, cost_to_close=0.80, ts=MARKED_AT, contracts=2):
    audit._clock = lambda: ts
    await audit.record(
        "watchdog.check",
        {
            "candidate_id": CANDIDATE,
            "external_identifier": f"argus-open-{CANDIDATE}",
            "contracts": contracts,
            "entry_credit": 0.55,
            "cost_to_close": cost_to_close,
            "credit_stop": 1.10,
            "problems": [],
            "spot": 7799.63,
            "sustaining": {},
            "true_reasons": [],
            "unknown_reasons": [],
        },
    )


async def _gate(audit, *, nlv, daily_pnl, ts=GATED_AT, allowed=False, anchored=None):
    """One `gate.decision` — the Tier 1 row the G1 series is read from.

    `anchored=None` writes NO `daily_pnl_anchored` key: the shape of every row
    written before 2026-09-18, which must keep reading as it always did."""
    audit._clock = lambda: ts
    snapshot = {
        "open_position_count": 0,
        "ledger_spread_count": 0,
        "working_open_spread_count": 0,
        "open_spread_equivalent": 0,
        "daily_pnl": daily_pnl,
        "account_nlv": nlv,
        "market_data_age_s": 0.4,
        "account_stream_age_s": 1.2,
    }
    if anchored is not None:
        snapshot["daily_pnl_anchored"] = anchored
    await audit.record(
        "gate.decision",
        {
            "allowed": allowed,
            "reasons": [] if allowed else ["concurrent_limit"],
            "limits": {},
            "proposal": {"symbol": "SPXW", "contracts": 1, "max_loss": 500.0},
            "snapshot": snapshot,
        },
    )


# -- the arithmetic is desk's, once -------------------------------------------


async def test_the_digest_totals_are_the_desk_pages_totals(db, audit):
    """One computation, two renderings. A digest that re-derives the day's P&L is a
    SECOND scorer, and unlike `desk` vs `mark` — which the docstring says must agree
    — nothing would ever compare a Slack line to a page."""
    await _open_position(audit)
    await _close_position(audit)

    summary = desk.summarize(desk.load(db, TRADING_DATE))

    # The real 2026-08-13 numbers: 0.55 credit, 1.20 debit, 2 contracts.
    assert summary.realized == pytest.approx(-130.0)
    assert summary.closed[0].points == pytest.approx(-0.65)
    assert summary.flat is True
    assert summary.held_contracts == 0


async def test_an_unpriceable_position_refuses_the_flat_claim(db, audit):
    """"No fill prices were reported" is not "nothing is held". A flat check that
    answers True from a position it cannot read is the reassuring-when-wrong failure
    this surface exists to remove."""
    await _fill(audit, direction="open", action="Sell to Open",
                symbol="SPXW  260813C07825000", contracts=2, price=0.0, ts=OPENED_AT)

    summary = desk.summarize(desk.load(db, TRADING_DATE))
    if summary.unpriceable:
        assert summary.flat is False
    else:  # priced, but then it is held and equally not flat
        assert summary.held_contracts > 0
        assert summary.flat is False


async def test_a_held_position_is_not_flat_and_names_its_spent_exit(db, audit):
    await _open_position(audit)
    await _mark(audit)

    summary = desk.summarize(desk.load(db, TRADING_DATE))
    assert summary.flat is False
    assert summary.held_contracts == 2
    assert summary.unrealized == pytest.approx((0.55 - 0.80) * 2 * 100)


# -- G1: a structural zero must not render as a number ------------------------


async def test_g1_reads_the_days_account_path_from_gate_decisions(db, audit):
    await _gate(audit, nlv=40_000.0, daily_pnl=0.0)
    await _gate(audit, nlv=39_800.0, daily_pnl=-200.0, ts="2026-08-13T18:00:00.000000Z")

    start, end = et_day_bounds_utc_iso(TRADING_DATE)
    series = eod.read_g1(db, start, end)

    assert series.rows == 2
    assert series.nlv == (40_000.0, 39_800.0)
    assert series.pnl_moved is True
    text = eod._g1(series, None)
    assert "40,000.00" in text and "-200.00" in text


async def test_a_day_with_no_candidates_reports_NO_SERIES_not_zero(db, audit):
    """The blackout-day shape. `daily_pnl: 0.0` is exactly what cert produced
    structurally for fourteen months, so rendering a zero here would make the G1
    failure indistinguishable from a quiet day — which is the whole point of G1."""
    start, end = et_day_bounds_utc_iso(TRADING_DATE)
    series = eod.read_g1(db, start, end)

    assert series.rows == 0
    assert series.has_series is False
    assert series.pnl_moved is None

    text = eod._g1(series, None)
    assert "NO SERIES" in text
    # No value line at all — not a line of zeros. The prose says why.
    assert "account NLV" not in text
    assert "day P&L " not in text


async def test_one_value_all_session_is_called_out(db, audit):
    await _gate(audit, nlv=40_000.0, daily_pnl=0.0)
    await _gate(audit, nlv=40_000.0, daily_pnl=0.0, ts="2026-08-13T18:00:00.000000Z")

    start, end = et_day_bounds_utc_iso(TRADING_DATE)
    series = eod.read_g1(db, start, end)

    assert series.pnl_moved is False
    assert "ONE value all session" in eod._g1(series, None)


async def test_a_no_account_row_cannot_make_a_stuck_pnl_read_as_moved(db, audit):
    """`RiskSnapshotBuilder.build`'s no-account fallback writes nlv 0.0 AND day P&L
    0.0 as plain numbers. Taken as datapoints, a day P&L stuck at -120.0 plus one such
    row reads {-120.0, 0.0} — "MOVED, the breaker's input is live" — which is the G1
    failure reported as its all-clear."""
    await _gate(audit, nlv=40_000.0, daily_pnl=-120.0)
    await _gate(audit, nlv=0.0, daily_pnl=0.0, ts="2026-08-13T15:00:00.000000Z")
    await _gate(audit, nlv=40_000.0, daily_pnl=-120.0, ts="2026-08-13T18:00:00.000000Z")

    start, end = et_day_bounds_utc_iso(TRADING_DATE)
    series = eod.read_g1(db, start, end)

    assert series.rows == 3
    assert series.no_account == 1
    assert series.nlv == (40_000.0, 40_000.0)          # no 0.00 low
    assert series.daily_pnl == (-120.0, -120.0)
    assert series.pnl_moved is False
    text = eod._g1(series, None)
    assert "ONE value all session" in text
    assert "NO ACCOUNT STATE" in text


async def test_only_no_account_rows_is_no_series_and_says_why(db, audit):
    """Every row lacking account state is NO SERIES — but not the quiet kind: the
    candidates reached the gate, against no account."""
    await _gate(audit, nlv=0.0, daily_pnl=0.0)

    start, end = et_day_bounds_utc_iso(TRADING_DATE)
    series = eod.read_g1(db, start, end)

    assert series.has_series is False
    assert series.no_account == 1
    assert "NO ACCOUNT STATE" in eod._g1(series, None)
    digest_g1 = {"has_series": False, "no_account_rows": 1}
    assert "NO ACCOUNT STATE" in eod.slack_text({"g1": digest_g1})


async def test_a_nonfinite_snapshot_value_is_skipped_not_coerced(db, audit):
    """`gate._num` stringifies inf so the chain stays hashable. An unreadable number
    must not become a datapoint — it would read as a real account value."""
    await _gate(audit, nlv="inf", daily_pnl=-50.0)

    start, end = et_day_bounds_utc_iso(TRADING_DATE)
    series = eod.read_g1(db, start, end)

    assert series.rows == 1
    assert series.nlv == ()
    assert series.daily_pnl == (-50.0,)


# -- the calendar questions ---------------------------------------------------


def test_the_weekly_read_is_the_weeks_LAST_trading_day_not_friday(calendar):
    """Attached to the calendar rather than to `weekday() == 4`, so a Friday holiday
    does not silently skip the week — the same class of mistake as a literal 15:30
    flatten on a half-day."""
    good_friday = date(2026, 4, 3)
    assert calendar.is_trading_day(good_friday) is False
    assert eod.is_last_trading_day_of_week(calendar, date(2026, 4, 2)) is True

    assert eod.is_last_trading_day_of_week(calendar, date(2026, 8, 14)) is True   # Friday
    assert eod.is_last_trading_day_of_week(calendar, date(2026, 8, 13)) is False  # Thursday


def test_a_half_day_skips_the_from_feed_mark_and_says_how_to_re_render(calendar):
    """`--from-feed` reads the CURRENT quote as the close, so on a 13:00 settlement a
    16:45 report-time quote is hours of drift presented as a settlement price."""
    text = eod._scoring("cfg.toml", TRADING_DATE, TRADING_DATE, half_day=True, timeout=5.0)
    assert "SKIPPED" in text and "half-day" in text
    assert "--settlement" in text


def test_a_past_date_skips_from_feed_because_the_feed_cannot_reach_back():
    text = eod._scoring(
        "cfg.toml", TRADING_DATE, date(2026, 8, 14), half_day=False, timeout=5.0
    )
    assert "SKIPPED" in text and "--settlement" in text


def test_half_day_detection_uses_the_calendars_close_time(calendar):
    """A real half-day off the loaded profile's own list, never a literal 16:00."""
    half = date(2026, 11, 27)                       # the day after Thanksgiving
    assert calendar.close_time(half) == time(13, 0)
    assert eod.is_half_day(calendar, half) is True
    assert eod.is_half_day(calendar, TRADING_DATE) is False


# -- a failed section must not cost the report --------------------------------


async def test_a_failed_section_still_ships_the_report(db, audit, config, calendar):
    await _open_position(audit)
    await _close_position(audit)

    report = eod.build_report(
        config=config,
        config_path="cfg.toml",
        trading_date=TRADING_DATE,
        calendar=calendar,
        conn=db,
        status=None,
        status_error="ConnectionRefusedError: /status is down",
        backup_dir=None,
        journal_file=Path("/nonexistent/journal.txt"),
        weekly=False,
        mark_timeout=1.0,
        now_et=eod.et_now(),
    )

    keys = {s.key: s for s in report.sections}
    # The two that failed — and the flat check, which is above both, did not.
    assert keys["rails"].failure is not None
    assert keys["journal"].failure is not None
    assert keys["flat"].ok
    assert "FLAT" in keys["flat"].text

    assert report.digest["sections"]["rails"] == "failed"
    assert any("rails:" in f for f in report.digest["failures"])
    # The report exists, names the failure in place, and still carries the day.
    assert "SECTION FAILED" in report.transcript()
    assert report.digest["realized"] == pytest.approx(-130.0)


async def test_a_raising_section_is_caught_per_section(db, audit, config, calendar):
    """`SystemExit` too: the composed tools refuse bad input by raising it, and one
    refusal must cost its own section and nothing else."""
    report = eod.Report(trading_date=TRADING_DATE)

    def explode() -> str:
        raise SystemExit("database not found: /data/argus.db")

    section = report.run("boom", "BOOM", explode)
    assert section.ok is False
    assert "database not found" in section.failure


# -- the digest and the message it becomes ------------------------------------


BEFORE_SETTLEMENT = datetime(2026, 8, 13, 14, 0, tzinfo=ET)
AFTER_SETTLEMENT = datetime(2026, 8, 13, 16, 45, tzinfo=ET)


async def test_the_slack_line_leads_with_NOT_FLAT_before_settlement(
    db, audit, config, calendar
):
    await _open_position(audit)
    await _mark(audit)

    report = eod.build_report(
        config=config, config_path="cfg.toml", trading_date=TRADING_DATE,
        calendar=calendar, conn=db, status=None, status_error="skipped",
        backup_dir=None, journal_file=None, weekly=False, mark_timeout=1.0,
        now_et=BEFORE_SETTLEMENT,
    )
    text = eod.slack_text(report.digest)

    assert text.startswith(":rotating_light: NOT FLAT")
    assert "2 contract(s) OPEN" in text
    assert "no series" in text          # no gate rows in this fixture
    assert "```" in text                # the numbers ride in a code block


async def test_a_settled_hold_is_HELD_TO_EXPIRY_not_OPEN(db, audit, config, calendar):
    """At 16:45 a 0DTE spread nothing closed has EXPIRED. Paging it as "OPEN" in red
    on an ordinary held-to-expiry evening teaches the reader to skip the headline."""
    await _open_position(audit)
    await _mark(audit)

    report = eod.build_report(
        config=config, config_path="cfg.toml", trading_date=TRADING_DATE,
        calendar=calendar, conn=db, status=None, status_error="skipped",
        backup_dir=None, journal_file=None, weekly=False, mark_timeout=1.0,
        now_et=AFTER_SETTLEMENT,
    )
    text = eod.slack_text(report.digest)

    assert report.digest["settled"] is True
    assert report.digest["flat"] is False          # still not flat — still the headline
    assert text.startswith(":warning: HELD TO EXPIRY — 2 contract(s)")
    assert "OPEN" not in text.splitlines()[0]
    flat = next(s for s in report.sections if s.key == "flat")
    assert "HELD TO EXPIRY" in flat.text and "settlement against the broker" in flat.text


def test_an_undated_run_reports_the_latest_SETTLED_session(calendar):
    """`Persistent=true` can fire the next MORNING; "today" is then a session that
    has not happened, and the owed day is yesterday."""
    thu, fri = date(2026, 8, 13), date(2026, 8, 14)
    assert eod.latest_settled_session(calendar, datetime(2026, 8, 13, 16, 45, tzinfo=ET)) == thu
    assert eod.latest_settled_session(calendar, datetime(2026, 8, 14, 8, 0, tzinfo=ET)) == thu
    assert eod.latest_settled_session(calendar, datetime(2026, 8, 14, 16, 0, tzinfo=ET)) == thu
    assert eod.latest_settled_session(calendar, datetime(2026, 8, 16, 12, 0, tzinfo=ET)) == fri
    # A half-day settles at 13:15, not 16:15.
    half = date(2026, 11, 27)
    assert eod.latest_settled_session(calendar, datetime(2026, 11, 27, 14, 0, tzinfo=ET)) == half


async def test_an_undated_catch_up_never_overwrites_a_days_report(
    tmp_path, db, audit, config, monkeypatch
):
    """The punctual report carries a `--from-feed` scoring preview a later run cannot
    reproduce, so a catch-up for an already-reported day writes nothing. An explicit
    `--date` always renders — that is the re-render path."""
    monkeypatch.setattr(eod, "et_now", lambda: AFTER_SETTLEMENT)
    out = tmp_path / "reports"
    out.mkdir()
    txt = out / f"argus-eod-{TRADING_DATE}.txt"
    txt.write_text("the punctual report\n")
    args = ["--config", str(tmp_path / "argus.dev.toml"), "--out-dir", str(out), "--no-status"]

    assert eod.main(args) == 0
    assert txt.read_text() == "the punctual report\n"
    assert not (out / f"argus-eod-{TRADING_DATE}.json").exists()

    assert eod.main(args + ["--date", TRADING_DATE.isoformat()]) == 0
    assert txt.read_text() != "the punctual report\n"


async def test_the_slack_line_names_the_transcript_on_the_HOST(tmp_path, db, audit, config):
    """Inside the container the report is at /reports; on the droplet that path does
    not exist, and the Slack line is what tells you where to look."""
    out = tmp_path / "reports"
    eod.main([
        "--config", str(tmp_path / "argus.dev.toml"),
        "--date", TRADING_DATE.isoformat(),
        "--out-dir", str(out),
        "--host-report-dir", "/path/to/backups/reports",
        "--no-status",
    ])
    digest = json.loads((out / f"argus-eod-{TRADING_DATE}.json").read_text())
    assert digest["transcript"] == f"/path/to/backups/reports/argus-eod-{TRADING_DATE}.txt"


def test_the_slack_line_names_every_rail_that_is_off():
    digest = {
        "trading_date": "2026-09-17",
        "flat": True,
        "held_contracts": 0,
        "spent_exits": ["x"],
        "unwatchable_records": 1,
        "unpriceable": [],
        "g1": {"has_series": False},
        "rails": {
            "status_read": True,
            "kill_switch_engaged": True,
            "kill_switch_since": "2026-09-17T15:00:00Z",
            "halted": True,
            "halt_grade": "ENTRIES",
            "breaker_tripped": True,
            "watchdog_degraded": True,
            "blackout_state": "expired",
        },
        "snapshot": {"newest": None, "age_h": None},
        "failures": ["execution: boom"],
    }
    text = eod.slack_text(digest)

    for expected in (
        "kill switch ENGAGED", "HALTED", "breaker TRIPPED", "watchdog DEGRADED",
        "spent exit attempt", "unwatchable record", "snapshot stale or missing",
        "blackout list expired", "1 section(s)/read(s) FAILED",
    ):
        assert expected in text, expected


async def test_the_digest_is_json_serializable_and_carries_its_schema(
    db, audit, config, calendar
):
    """The shell reads this file. A digest that cannot round-trip through JSON is a
    delivery failure at 16:45 with nobody watching."""
    await _gate(audit, nlv=40_000.0, daily_pnl=-120.0)
    report = eod.build_report(
        config=config, config_path="cfg.toml", trading_date=TRADING_DATE,
        calendar=calendar, conn=db, status=None, status_error="skipped",
        backup_dir=None, journal_file=None, weekly=False, mark_timeout=1.0,
        now_et=eod.et_now(),
    )

    round_tripped = json.loads(json.dumps(report.digest))
    assert round_tripped["schema"] == eod.SCHEMA_VERSION
    assert round_tripped["trading_date"] == "2026-08-13"
    assert round_tripped["environment"] == config.environment
    assert round_tripped["g1"]["pnl_last"] == -120.0
    # The subject is named in the digest as well as the transcript: a digest that
    # does not say which world it describes can be read as the wrong one.
    assert round_tripped["config_file"] == "argus.dev.toml"
    assert round_tripped["db_path"] == config.audit.db_path
    assert json.loads(json.dumps(eod.slack_payload(report.digest)))["text"]


# -- the rails this job runs under --------------------------------------------


async def test_a_non_trading_day_writes_nothing_and_exits_clean(tmp_path, config):
    """The guard is inside the tool, so `Persistent=true` can catch up a missed run
    without a Saturday reboot producing a report about nothing."""
    out = tmp_path / "reports"
    code = eod.main([
        "--config", str(config.source_path) if hasattr(config, "source_path") else
        str(tmp_path / "argus.dev.toml"),
        "--date", "2026-08-15",          # a Saturday
        "--out-dir", str(out),
        "--no-status",
    ])

    assert code == 0
    assert not out.exists() or not list(out.glob("*"))


async def test_the_report_writes_nothing_to_the_audit_chain(
    tmp_path, db, audit, config
):
    """The timer runs while the trading process holds the writer lock. Every read is
    `mode=ro`; a single write here would be the 2026-07-21 failure on a schedule."""
    await _open_position(audit)
    await _close_position(audit)
    before = db.execute("SELECT COUNT(*), MAX(seq) FROM audit_log").fetchone()

    out = tmp_path / "reports"
    code = eod.main([
        "--config", str(tmp_path / "argus.dev.toml"),
        "--date", TRADING_DATE.isoformat(),
        "--out-dir", str(out),
        "--no-status",
    ])

    assert code == 0
    assert db.execute("SELECT COUNT(*), MAX(seq) FROM audit_log").fetchone() == before

    txt = out / f"argus-eod-{TRADING_DATE}.txt"
    js = out / f"argus-eod-{TRADING_DATE}.json"
    slack = out / f"argus-eod-{TRADING_DATE}.slack.json"
    assert txt.exists() and js.exists() and slack.exists()
    assert json.loads(js.read_text())["transcript"] == str(txt)
    # The Slack payload is a ready-to-POST body, so the shell escapes nothing.
    assert set(json.loads(slack.read_text())) == {"text"}


async def test_running_main_does_not_leak_the_real_env_into_the_suite(tmp_path, config):
    """`main()` calls `load_env()`, which writes `.env` into `os.environ` outside
    monkeypatch. Before conftest's `_no_real_env` guard, the two `main()` tests above
    leaked the developer's real alert webhook into every later test, and the
    startup-based ones posted to Slack (2026-09-18)."""
    import os

    eod.main([
        "--config", str(tmp_path / "argus.dev.toml"),
        "--date", "2026-08-15",
        "--no-status",
    ])
    assert "ARGUS_SLACK_WEBHOOK_URL" not in os.environ
    assert not [k for k in os.environ if k.startswith("TASTYTRADE_")]


def test_the_report_never_opens_the_database_read_write():
    source = Path(eod.__file__).read_text()
    assert "mode=ro" in source
    assert "AuditLog" not in source, (
        "importing the audit log is how a reader becomes a writer; the report has "
        "no business holding one"
    )


# -- a blackout day is named, from the chain (2026-09-18) ----------------------


async def _blackout_evidence(audit, *, announced=True, ticks=2,
                             day_prefix="2026-08-13"):
    """What a blackout session leaves in the chain: the v0.4.4 session-start row,
    and `signal.none` refusals once the entry window opens."""
    if announced:
        audit._clock = lambda: f"{day_prefix}T13:40:05.000000Z"   # 09:40 ET
        await audit.record(
            "session.event_blackout", {"trading_date": day_prefix}
        )
    for i in range(ticks):
        audit._clock = lambda i=i: f"{day_prefix}T14:00:{i:02d}.000000Z"  # 10:00 ET
        await audit.record(
            "signal.none", {"reason": "event_blackout", "today": day_prefix}
        )


def test_the_category_spelled_here_is_the_one_the_session_writes():
    """`tools.eod` spells it rather than importing `argus.session` (the broker SDK).
    Two spellings of one category are how a reader silently stops finding its rows."""
    from argus.session import EVENT_BLACKOUT_DAY

    assert eod.BLACKOUT_ANNOUNCED == EVENT_BLACKOUT_DAY


async def test_the_blackout_is_read_off_the_chain_for_the_reports_date(db, audit):
    """Either source is proof, and only this date's rows count. An ordinary
    `signal.none` is not evidence of anything."""
    start, end = et_day_bounds_utc_iso(TRADING_DATE)
    assert eod.read_blackout_day(db, start, end).blacked_out is False

    audit._clock = lambda: "2026-08-13T14:00:00.000000Z"
    await audit.record("signal.none", {"reason": "outside_entry_window"})
    await _blackout_evidence(audit, day_prefix="2026-08-12")        # yesterday
    assert eod.read_blackout_day(db, start, end).blacked_out is False

    await _blackout_evidence(audit, announced=False, ticks=3)
    day = eod.read_blackout_day(db, start, end)
    assert (day.blacked_out, day.announced, day.suppressed_ticks) == (True, False, 3)


async def test_the_session_start_row_alone_is_enough(db, audit):
    """A blackout day before 10:00 — or one whose Tier 2 ticks were dropped — has
    only the session's own announcement, and that is the verdict."""
    await _blackout_evidence(audit, ticks=0)
    start, end = et_day_bounds_utc_iso(TRADING_DATE)
    day = eod.read_blackout_day(db, start, end)
    assert (day.blacked_out, day.announced) == (True, True)
    assert "session.event_blackout" in day.evidence()


async def test_a_blackout_day_is_named_in_the_report_and_the_digest(
    db, audit, config, calendar
):
    """Live day 1's shape: flat, no entries, no gate rows. Before this the digest
    said "no series (no candidates evaluated…)" — true, and indistinguishable from a
    day the signal path was broken."""
    await _blackout_evidence(audit)
    report = eod.build_report(
        config=config, config_path="cfg.toml", trading_date=TRADING_DATE,
        calendar=calendar, conn=db, status=None, status_error="skipped",
        backup_dir=None, journal_file=None, weekly=False, mark_timeout=1.0,
        now_et=AFTER_SETTLEMENT,
    )
    sections = {s.key: s.text for s in report.sections}
    assert "EVENT BLACKOUT" in sections["subject"]
    assert "2 tick(s) refused" in sections["subject"]
    assert "WAS an event-blackout day" in sections["g1"]
    assert report.digest["blackout_day"] == {
        "blacked_out": True, "announced": True, "suppressed_ticks": 2,
    }

    text = eod.slack_text(report.digest)
    assert text.startswith(":white_check_mark: FLAT")      # a blackout is not an alarm
    assert "EVENT BLACKOUT, no entries by design" in text
    assert "no series (event blackout" in text
    assert "on an EVENT BLACKOUT day" not in text


async def test_an_ordinary_day_says_nothing_about_blackouts(db, audit, config, calendar):
    report = eod.build_report(
        config=config, config_path="cfg.toml", trading_date=TRADING_DATE,
        calendar=calendar, conn=db, status=None, status_error="skipped",
        backup_dir=None, journal_file=None, weekly=False, mark_timeout=1.0,
        now_et=AFTER_SETTLEMENT,
    )
    assert report.digest["blackout_day"]["blacked_out"] is False
    assert "BLACKOUT" not in report.transcript()
    assert "BLACKOUT" not in eod.slack_text(report.digest)


def _digest(**over):
    base = {
        "trading_date": "2026-09-30", "flat": True, "held_contracts": 0,
        "entry_attempts": 0, "g1": {"has_series": False},
        "rails": {"status_read": True},
        "snapshot": {"newest": "argus-x.db", "age_h": 1.0},
    }
    base.update(over)
    return base


def test_an_entry_on_a_blackout_day_is_the_one_blackout_alarm():
    """The strategy refuses every candidate on a blackout day, so an entry attempt
    on one means the rail did not hold. Unreachable by design — hence it pages."""
    text = eod.slack_text(_digest(
        entry_attempts=1, blackout_day={"blacked_out": True},
    ))
    assert ":exclamation:" in text
    assert "1 entry attempt(s) on an EVENT BLACKOUT day" in text


def test_the_digest_warns_when_the_NEXT_session_is_blacked_out():
    """At 16:45 the next session is tomorrow's, and whether it takes entries is the
    one blackout fact about the future — so only `/status` can give it. A heads-up
    line, never an alarm, and absent on an ordinary evening."""
    rails = {"status_read": True, "blackout_next_session": True,
             "next_session_open": "2026-09-30T09:40:00-04:00"}
    text = eod.slack_text(_digest(trading_date="2026-09-29", rails=rails))
    assert "next        EVENT BLACKOUT — no entries at the 2026-09-30T09:40" in text
    assert ":exclamation:" not in text

    for quiet in (False, None):
        rails["blackout_next_session"] = quiet
        assert "next        " not in eod.slack_text(_digest(rails=rails))


def test_the_rails_section_reads_the_next_session_off_status():
    status = {
        "event_blackout": {"state": "ok", "next_session_blacked_out": True},
        "session": {"next_session_open": "2026-09-30T09:40:00-04:00"},
    }
    assert "next session   EVENT BLACKOUT — no entries" in eod._rails(status)
    status["event_blackout"]["next_session_blacked_out"] = False
    assert "not a blackout day" in eod._rails(status)
    # A pre-v0.4.4 page has no such key: say nothing rather than guess.
    del status["event_blackout"]["next_session_blacked_out"]
    assert "next session" not in eod._rails(status)


# -- the journal sweep is scoped to the REPORT'S date (review fix, 2026-09-18) --


def _journal_file(tmp_path: Path) -> Path:
    """`journalctl -o short-iso` lines as the shell collects them: a 36 h window, so
    the punctual 16:45 run also sees YESTERDAY'S session."""
    path = tmp_path / "journal.txt"
    path.write_text(
        # the day before — yesterday's session, inside the 36 h window
        "2026-08-12T11:04:05-0400 argus-droplet argus[812]: watchdog.fatal: boom\n"
        # the report's date, both offset spellings journalctl has used
        "2026-08-13T10:15:00-0400 argus-droplet argus[901]: UNWATCHABLE POSITION x\n"
        "2026-08-13T15:31:00-04:00 argus-droplet argus[901]: HALTED y\n"
        # 23:30 ET on the 13th is the 14th in UTC — the ET date decides
        "2026-08-14T03:30:00+0000 argus-droplet argus[901]: BOOK DISAGREEMENT z\n"
        # no readable timestamp: kept, and counted
        "garbled line mentioning HALTED\n"
        "\n"
    )
    return path


def test_the_journal_sweep_keeps_only_the_reports_date(tmp_path):
    sweep = eod.read_journal(_journal_file(tmp_path), TRADING_DATE)

    assert len(sweep.lines) == 4
    assert not any("watchdog.fatal" in ln for ln in sweep.lines)   # yesterday's
    assert any("BOOK DISAGREEMENT" in ln for ln in sweep.lines)    # ET 23:30 on the 13th
    assert sweep.other_days == 1
    assert sweep.undated == 1

    text = eod._journal(_journal_file(tmp_path), TRADING_DATE, 40)
    assert f"4 matching line(s) on {TRADING_DATE}" in text
    assert "1 matching line(s) from other dates" in text
    assert "no readable timestamp" in text
    assert "watchdog.fatal" not in text


def test_yesterdays_hits_alone_read_clean_today(tmp_path):
    path = tmp_path / "journal.txt"
    path.write_text("2026-08-12T11:04:05-0400 host argus[1]: watchdog.fatal: boom\n")

    text = eod._journal(path, TRADING_DATE, 40)
    assert text.startswith(f"  clean — no matching lines on {TRADING_DATE}")


async def test_the_digest_counts_the_same_date_scoped_lines(
    db, audit, config, calendar, tmp_path
):
    report = eod.build_report(
        config=config, config_path="cfg.toml", trading_date=TRADING_DATE,
        calendar=calendar, conn=db, status=None, status_error="skipped",
        backup_dir=None, journal_file=_journal_file(tmp_path), weekly=False,
        mark_timeout=1.0, now_et=AFTER_SETTLEMENT,
    )
    assert report.digest["journal_hits"] == 4


# -- a composed tool cannot keep the process's logging (review fix, 2026-09-18) --


def test_a_captured_tools_logging_config_does_not_outlive_its_section(monkeypatch):
    """`tools.mark.main` calls `logging.basicConfig` inside `_capture`, binding a root
    handler to THAT section's buffer. Later sections' log lines then vanished into it."""
    import logging

    root = logging.getLogger()
    # The production shape: `python -m argus.tools.eod` starts with NO root handlers,
    # so a warning reaches `logging.lastResort`, which writes to the CURRENT
    # `sys.stderr` — the section's buffer. pytest's own capture handlers would
    # otherwise swallow it here. Restored by monkeypatch.
    monkeypatch.setattr(root, "handlers", [])
    handlers_before = list(root.handlers)
    level_before = root.level
    argus_level_before = logging.getLogger("argus").level

    def configures_logging() -> None:
        logging.basicConfig(level=logging.DEBUG, force=False)
        root.addHandler(logging.StreamHandler())  # bound to the redirected stderr
        logging.getLogger("argus").setLevel(logging.DEBUG)
        logging.getLogger("argus.eodtest.fresh").setLevel(logging.DEBUG)
        logging.getLogger("argus.eodtest").warning("inside the first section")

    first = eod._capture(configures_logging)
    assert "inside the first section" in first

    assert root.handlers == handlers_before
    assert root.level == level_before
    assert logging.getLogger("argus").level == argus_level_before
    assert logging.getLogger("argus.eodtest.fresh").level == logging.NOTSET

    def logs_something() -> None:
        logging.getLogger("argus.eodtest").warning("inside the second section")

    second = eod._capture(logs_something)
    # The second section's log line lands in ITS OWN section, not in the first
    # section's discarded buffer through a handler left bound to it.
    assert "inside the second section" in second


# -- catch-up renders every OWED day; delivery is retried (review fix, 2026-09-18) --


def _touch_report(out: Path, d: date) -> None:
    out.mkdir(exist_ok=True)
    (out / f"argus-eod-{d}.txt").write_text("reported\n")


def test_a_fresh_install_owes_only_the_latest_session(tmp_path, calendar):
    """No report on disk: a first run must not back-post a week of digests about days
    before it was deployed."""
    owed = eod.owed_days(calendar, datetime(2026, 8, 14, 16, 45, tzinfo=ET), tmp_path / "r")
    assert owed.render == (date(2026, 8, 14),)
    assert owed.too_old == ()


def test_an_outage_across_two_sessions_owes_both_oldest_first(tmp_path, calendar):
    """Reported Tuesday; down Wednesday and Thursday evenings; back Friday morning.
    The old rule reported Thursday and skipped Wednesday without a word."""
    out = tmp_path / "r"
    _touch_report(out, date(2026, 8, 11))            # Tue
    owed = eod.owed_days(calendar, datetime(2026, 8, 14, 8, 0, tzinfo=ET), out)
    assert owed.render == (date(2026, 8, 12), date(2026, 8, 13))   # Wed, Thu
    # Friday has not settled at 08:00, so it is not owed yet.


def test_an_already_reported_latest_session_owes_nothing(tmp_path, calendar):
    out = tmp_path / "r"
    _touch_report(out, date(2026, 8, 13))
    owed = eod.owed_days(calendar, datetime(2026, 8, 13, 16, 45, tzinfo=ET), out)
    assert owed.render == ()


def test_a_gap_older_than_the_catch_up_window_is_named_not_rendered(tmp_path, calendar):
    out = tmp_path / "r"
    _touch_report(out, date(2026, 7, 31))
    now = datetime(2026, 8, 13, 16, 45, tzinfo=ET)
    owed = eod.owed_days(calendar, now, out)
    horizon = date(2026, 8, 13) - __import__("datetime").timedelta(days=eod.CATCH_UP_DAYS)
    assert owed.render and all(d >= horizon for d in owed.render)
    assert owed.too_old and all(d < horizon for d in owed.too_old)
    assert owed.render[-1] == date(2026, 8, 13)


async def test_main_renders_every_owed_day_and_writes_the_transcript_last(
    tmp_path, db, audit, config, monkeypatch
):
    monkeypatch.setattr(eod, "et_now", lambda: datetime(2026, 8, 14, 8, 0, tzinfo=ET))
    out = tmp_path / "reports"
    _touch_report(out, date(2026, 8, 11))
    args = ["--config", str(tmp_path / "argus.dev.toml"), "--out-dir", str(out), "--no-status"]

    assert eod.main(args) == 0
    for d in ("2026-08-12", "2026-08-13"):
        assert (out / f"argus-eod-{d}.txt").exists()
        assert (out / f"argus-eod-{d}.slack.json").exists()
    # No temporary transcript left behind: the rename completed.
    assert not list(out.glob(".*partial"))


# -- a failed shared read costs its dependants, not the report (review fix) ----


async def test_a_failed_ledger_read_still_ships_the_report(
    db, audit, config, calendar, monkeypatch
):
    def boom(*a, **k):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(desk, "load", boom)
    report = eod.build_report(
        config=config, config_path="cfg.toml", trading_date=TRADING_DATE,
        calendar=calendar, conn=db, status=None, status_error="skipped",
        backup_dir=None, journal_file=None, weekly=False, mark_timeout=1.0,
        now_et=AFTER_SETTLEMENT,
    )
    flat = next(s for s in report.sections if s.key == "flat")
    assert flat.ok is False and "flat state is UNKNOWN" in flat.failure
    g1 = next(s for s in report.sections if s.key == "g1")
    assert g1.ok                                    # its own read succeeded
    assert report.digest["flat"] is None            # UNREAD, never False or True
    assert report.digest["realized"] is None
    assert any(f.startswith("read ledger:") for f in report.digest["failures"])
    text = eod.slack_text(report.digest)
    assert text.startswith(":warning: FLAT STATE UNKNOWN")
    transcript = report.transcript()
    assert transcript.index("READS THAT FAILED") < transcript.index("ARGUS EOD — SUBJECT")
    assert json.loads(json.dumps(report.digest))    # still serializable


async def test_a_failed_blackout_read_says_unread_not_ordinary(
    db, audit, config, calendar, monkeypatch
):
    def boom(*a, **k):
        raise sqlite3.OperationalError("malformed JSON")

    monkeypatch.setattr(eod, "read_blackout_day", boom)
    monkeypatch.setattr(eod, "read_g1", boom)
    monkeypatch.setattr(eod, "count_unwatchable", boom)
    report = eod.build_report(
        config=config, config_path="cfg.toml", trading_date=TRADING_DATE,
        calendar=calendar, conn=db, status=None, status_error="skipped",
        backup_dir=None, journal_file=None, weekly=False, mark_timeout=1.0,
        now_et=AFTER_SETTLEMENT,
    )
    subject = next(s for s in report.sections if s.key == "subject")
    assert "EVENT BLACKOUT UNREAD" in subject.text
    assert report.digest["blackout_day"]["blacked_out"] is None
    assert report.digest["g1"] is None
    assert report.digest["unwatchable_records"] is None
    flat = next(s for s in report.sections if s.key == "flat")
    assert flat.ok and "count UNREAD" in flat.text
    text = eod.slack_text(report.digest)
    assert "G1          UNREAD" in text
    assert "unwatchable count UNREAD" in text
    assert "no series" not in text


# -- an UNANCHORED day P&L is a placeholder, never a reading (2026-09-18) ------


async def test_unanchored_pnl_values_are_excluded_and_cannot_read_as_moved(db, audit):
    """`DailyPnl.value` is 0.0 before an anchor. Anchored -120.0 stuck all day, plus
    two unanchored placeholder zeros, used to read {-120.0, 0.0}: "MOVED"."""
    await _gate(audit, nlv=40_000.0, daily_pnl=0.0, anchored=False)
    await _gate(audit, nlv=40_000.0, daily_pnl=0.0, anchored=False,
                ts="2026-08-13T14:30:00.000000Z")
    await _gate(audit, nlv=39_880.0, daily_pnl=-120.0, anchored=True,
                ts="2026-08-13T15:00:00.000000Z")
    await _gate(audit, nlv=39_880.0, daily_pnl=-120.0, anchored=True,
                ts="2026-08-13T18:00:00.000000Z")

    start, end = et_day_bounds_utc_iso(TRADING_DATE)
    series = eod.read_g1(db, start, end)

    assert series.unanchored == 2
    assert series.daily_pnl == (-120.0, -120.0)
    assert len(series.nlv) == 4                       # the NLV is a real reading
    assert series.pnl_moved is False
    text = eod._g1(series, None)
    assert "ONE value all session" in text and "UNANCHORED" in text


async def test_an_all_unanchored_day_has_NO_PNL_READINGS_not_one_value(db, audit):
    await _gate(audit, nlv=40_000.0, daily_pnl=0.0, anchored=False)
    await _gate(audit, nlv=40_010.0, daily_pnl=0.0, anchored=False,
                ts="2026-08-13T18:00:00.000000Z")

    start, end = et_day_bounds_utc_iso(TRADING_DATE)
    series = eod.read_g1(db, start, end)

    assert series.has_series is True                  # an NLV path exists
    assert series.daily_pnl == ()
    assert series.pnl_moved is None
    text = eod._g1(series, None)
    assert "NO READINGS" in text
    assert "ONE value all session" not in text
    g1 = {"has_series": True, "pnl_moved": None, "unanchored_rows": 2,
          "nlv_first": 40_000.0, "nlv_last": 40_010.0}
    line = eod.slack_text({"g1": g1})
    assert "NO DAY-P&L READINGS" in line and "2 UNANCHORED" in line
    assert "ONE VALUE ALL SESSION" not in line


async def test_a_row_predating_the_flag_is_read_as_before(db, audit):
    await _gate(audit, nlv=40_000.0, daily_pnl=-50.0)          # no key at all
    start, end = et_day_bounds_utc_iso(TRADING_DATE)
    series = eod.read_g1(db, start, end)
    assert series.daily_pnl == (-50.0,)
    assert series.unanchored == 0


# -- an unsettled day is never written as the day's report (re-review, 2026-09-18) --


async def test_a_mid_session_preview_is_printed_not_filed(
    tmp_path, db, audit, config, monkeypatch, capsys
):
    """`ARGUS_EOD_DATE=<today>` at 14:00 used to write the day's report: render-once
    then treated the day as done, and the 16:45 run found nothing owed — leaving a
    pre-settlement `NOT FLAT — OPEN` as the day's record."""
    out = tmp_path / "reports"
    args = ["--config", str(tmp_path / "argus.dev.toml"), "--out-dir", str(out),
            "--no-status"]

    monkeypatch.setattr(eod, "et_now", lambda: BEFORE_SETTLEMENT)
    assert eod.main(args + ["--date", TRADING_DATE.isoformat()]) == 0
    printed = capsys.readouterr().out
    assert "has not settled yet" in printed
    assert "ARGUS EOD — SUBJECT" in printed                     # the preview happened
    assert not out.exists() or not list(out.glob(f"argus-eod-{TRADING_DATE}*"))

    # And the punctual run still owes the day.
    monkeypatch.setattr(eod, "et_now", lambda: AFTER_SETTLEMENT)
    assert eod.main(args) == 0
    assert (out / f"argus-eod-{TRADING_DATE}.txt").exists()


async def test_a_settled_explicit_date_is_still_written(tmp_path, db, audit, config,
                                                       monkeypatch):
    monkeypatch.setattr(eod, "et_now", lambda: AFTER_SETTLEMENT)
    out = tmp_path / "reports"
    assert eod.main(["--config", str(tmp_path / "argus.dev.toml"), "--out-dir", str(out),
                     "--no-status", "--date", TRADING_DATE.isoformat()]) == 0
    assert (out / f"argus-eod-{TRADING_DATE}.txt").exists()
