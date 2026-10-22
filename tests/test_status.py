"""The read-only monitoring surface (spec §3/§5): status assembly + the GET
/status (JSON) and GET / (HTML) routes on the existing control server.

The HTTP tests drive the SAME `ControlServer` as `/kill` through a real TCP
socket (reusing `test_control.http_request`), so the added routes are exercised
over the wire behind the shared bearer auth — never an in-process shortcut. The
assembly tests exercise `StatusService.assemble` directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import fresh_loss_budget
from argus.audit.log import AuditLog
from argus.clock import ET
from argus.control import ControlServer
from argus.db import apply_migrations, connect
from argus.heartbeat import LoopHeartbeat
from argus.risk.breaker import BREACH_CATEGORY, DailyLossBreaker
from argus.risk.killswitch import KillSwitch
from argus.risk.models import RiskSnapshot
from argus.status import (
    LinkHealth,
    PositionEvaluability,
    SessionPhase,
    SessionStatus,
    StatusService,
    UnclosablePosition,
    _DbStatus,
    _et_day_start_utc_iso,
    gc_snapshot,
    read_rss_bytes,
)
from test_control import TOKEN, http_request

# A fixed "now" so the ET-day boundary (and thus the would_submit filter) and
# uptime are deterministic. Noon ET, mid-session.
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=ET)
STARTED = datetime(2026, 7, 26, 9, 40, tzinfo=ET)
NEXT_OPEN = datetime(2026, 7, 27, 9, 40, tzinfo=ET)


def _risk(*, md_age: float, acct_age: float, nlv: float = 100_000.0,
          pnl: float = 0.0, positions: int = 0) -> RiskSnapshot:
    return RiskSnapshot(
        open_position_count=positions,
        daily_pnl=pnl,
        account_nlv=nlv,
        market_data_age_s=md_age,
        account_stream_age_s=acct_age, loss_budget=fresh_loss_budget(),
    )


def _make_service(
    audit: AuditLog,
    *,
    session: SessionStatus | None = None,
    killswitch: KillSwitch | None = None,
    now: datetime = NOW,
    next_open=lambda: NEXT_OPEN,
    read_rss=lambda: None,
    environment: str = "shadow",
    halt_state=lambda: None,
    breaker: DailyLossBreaker | None = None,
    # None = "ask `argus._version`", which in a checkout is the `unknown` fallback.
    # Injected the same way every other piece of config is, so no test needs a
    # git-tagged tree to assert on a stamped build.
    version: str | None = None,
    # None on all three = a config with no file behind it (`Config.from_dict`),
    # which is what every construction site predating the fields does.
    config_profile: str | None = None,
    config_filename: str | None = None,
    config_sha: str | None = None,
    # None = unpinned, which is what every construction site predating the field
    # does and the NORMAL cert reading; live cannot reach it (config.py refuses).
    broker_account_number: str | None = None,
    # None = the caller published no runway, which is what every construction site
    # predating the field does and must keep doing.
    blackout=None,
    # A moving clock, for tests that need time to pass between calls (the trailing
    # rates). None = the fixed `now` above, which is what every other test wants.
    clock=None,
    # The trailing rates' DURATION clock (seconds). None = the real monotonic clock,
    # which no test that leaves the rates alone needs to fake.
    monotonic=None,
) -> tuple[StatusService, SessionStatus, KillSwitch]:
    clock = clock or (lambda: now)
    ss = session or SessionStatus(now=clock)
    ks = killswitch or KillSwitch(audit)
    hb = LoopHeartbeat(interval_s=1.0, lag_threshold_s=1.0, sustained_breach_count=3)
    svc = StatusService(
        read_rss=read_rss,
        environment=environment,
        version=version,
        config_profile=config_profile,
        config_filename=config_filename,
        config_sha=config_sha,
        broker_account_number=broker_account_number,
        blackout=blackout,
        started_at=STARTED,
        session_status=ss,
        killswitch=ks,
        heartbeat=hb,
        audit=audit,
        market_data_max_age_s=2.0,
        account_stream_max_age_s=5.0,
        audit_queue_alert_depth=80,
        audit_queue_maxsize=100,
        next_open=next_open,
        now=clock,
        **({} if monotonic is None else {"monotonic": monotonic}),
        halt_state=halt_state,
        # Omitted by default, which is the "no session has built today's breaker"
        # state — the same default the service itself carries.
        breaker=lambda: breaker,
    )
    return svc, ss, ks


@contextlib.asynccontextmanager
async def server_harness(tmp_path: Path):
    """A control server WITH a status service wired, over loopback."""
    conn = connect(tmp_path / "argus.db")
    apply_migrations(conn)
    audit = AuditLog(conn, tier2_queue_maxsize=100, tier2_queue_alert_depth=80)
    audit.start()
    svc, ss, ks = _make_service(audit)
    server = ControlServer(
        host="127.0.0.1", port=0, token=TOKEN, killswitch=ks, audit=audit,
        status_service=svc,
    )
    await server.start()
    host, port = server.bound_address
    try:
        yield SimpleNamespace(
            server=server, audit=audit, conn=conn, session=ss, killswitch=ks,
            host=host, port=port,
        )
    finally:
        await server.stop()
        await audit.aclose()
        conn.close()


def _bearer(token: str = TOKEN) -> list[str]:
    return [f"Authorization: Bearer {token}"]


# -- assembly: fields --------------------------------------------------------


async def test_assemble_no_session_reports_idle_and_null_freshness(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    try:
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.phase == "idle"
    assert snap.environment == "shadow"
    assert snap.uptime_s == pytest.approx((NOW - STARTED).total_seconds())
    assert snap.next_session_open == NEXT_OPEN
    # No active session -> nothing live to measure.
    assert snap.market_data_age_s is None
    assert snap.account_data_age_s is None
    assert snap.market_data_stale is None
    assert snap.account_nlv is None
    assert snap.killswitch_engaged is False
    # None, NOT False: no session has built today's breaker, so there is no rail
    # to report on. See `test_breaker_tripped_is_none_when_no_breaker_is_armed`.
    assert snap.breaker_tripped is None
    assert snap.would_submit_today_raw == 0


async def test_assemble_running_surfaces_live_ages_and_staleness(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    assert ss.phase is SessionPhase.LOGGING_IN
    # MD fresh (1.0 <= 2.0), account stale (9.0 > 5.0).
    ss.session_running(lambda: _risk(md_age=1.0, acct_age=9.0, nlv=250_000.0, pnl=-125.0, positions=1))
    try:
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.phase == "running"
    assert snap.market_data_age_s == 1.0 and snap.market_data_stale is False
    assert snap.account_data_age_s == 9.0 and snap.account_data_stale is True
    assert snap.account_nlv == 250_000.0
    assert snap.daily_pnl == -125.0
    assert snap.open_position_count == 1
    # The loss budget rides the SAME risk snapshot the gate is handed (2026-09-21).
    assert snap.loss_budget == fresh_loss_budget()
    assert snap.to_json_dict()["loss_budget"]["remaining"] == 1000.0
    assert "Loss budget" in snap.to_html()


async def test_open_positions_are_shown_in_both_units(db):
    """The broker counts LEGS and every risk cap is stated in SPREADS. A bare "2" on
    the phone is ambiguous in exactly the way that put a leg count on the wrong side
    of max_concurrent_positions until 2026-08-13."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(lambda: _risk(md_age=1.0, acct_age=1.0, positions=2))
    try:
        snap = await svc.assemble()
        js, html_out = snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()

    assert js["account"]["open_position_count"] == 2      # original key, still legs
    assert js["account"]["open_spread_equivalent"] == 1
    assert "2 legs (1 spread)" in html_out


# -- the exit-triggers row (live watchdog evaluability, added 2026-08-13) -----
#
# THREE states that must not look alike: no watchdog publishing (shadow — nothing
# is watched, the question does not apply), a live watchdog with nothing impaired,
# and something actually impaired. The audit tail cannot carry this on a phone:
# payloads sort their keys, `unknown_reasons` sorts last, and the HTML tail cuts a
# payload at 240 chars, so a degraded position's reasons are always truncated away.


def _impaired(*, blind: bool = False, for_s: float = 1200.0) -> PositionEvaluability:
    return PositionEvaluability(
        external_identifier="argus-open-20260813-call-7835-7840",
        candidate_id="20260813-call-7835-7840",
        unknown_reasons=(
            ("credit_multiple", "underlying_near_short_strike") if blind
            else ("credit_multiple",)
        ),
        blind=blind,
        impaired_for_s=for_s,
    )


async def _snapshot_with(db, evaluability):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0, positions=2),
        evaluability_source=(None if evaluability is None else (lambda: evaluability)),
    )
    try:
        snap = await svc.assemble()
        return snap, snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()


async def test_no_watchdog_publishing_is_not_the_same_as_nothing_wrong(db):
    """Shadow has no watchdog at all. Rendering that as "all evaluable" would tell
    the operator every position is fine when nothing is being watched."""
    snap, js, html_out = await _snapshot_with(db, None)
    assert snap.impaired_positions is None
    assert js["watchdog"]["impaired_positions"] is None
    assert "no watchdog" in html_out
    assert "all evaluable" not in html_out


async def test_a_live_watchdog_with_nothing_impaired_says_so(db):
    _snap, js, html_out = await _snapshot_with(db, ())
    assert js["watchdog"]["impaired_positions"] == []
    assert "all evaluable" in html_out


async def test_a_degraded_position_shows_its_reasons_and_its_age(db):
    """The duration is the operational signal: "degraded right now" and "degraded for
    20 minutes and still not clearing" call for different responses."""
    _snap, js, html_out = await _snapshot_with(db, (_impaired(for_s=1200.0),))

    (row,) = js["watchdog"]["impaired_positions"]
    assert row["unknown_reasons"] == ["credit_multiple"]
    assert row["blind"] is False
    assert row["impaired_for_s"] == 1200.0
    assert row["candidate_id"] == "20260813-call-7835-7840"

    assert "20260813-call-7835-7840" in html_out
    assert "degraded" in html_out and "credit_multiple" in html_out
    assert "20m 0s" in html_out


async def test_a_blind_position_is_styled_as_the_emergency_it_is(db):
    """Degraded keeps the underlying backstop live and is deliberately not an
    emergency; blind means nothing is watching the position at all."""
    _snap, js, html_out = await _snapshot_with(db, (_impaired(blind=True, for_s=45.0),))

    (row,) = js["watchdog"]["impaired_positions"]
    assert row["blind"] is True
    assert row["unknown_reasons"] == ["credit_multiple", "underlying_near_short_strike"]
    assert "BLIND" in html_out
    assert "class='bad'" in html_out


# -- the exit-attempts row (spent exits, added 2026-08-21, A2b) ---------------
#
# The successor state to the row above, and it needed a row of its own because a
# spent attempt LEAVES the one above: `check_once` pops it out of `_impaired`, so
# until this existed the page rendered a position with the stop already fired, no
# trigger being evaluated and nothing automated left to try — as an ordinary open
# position. The phone page was the only surviving signal.


def _spent(
    *, outcome: str = "not_resolved", for_s: float = 900.0, credit_trigger_enabled: bool = True
) -> UnclosablePosition:
    return UnclosablePosition(
        external_identifier="argus-open-20260821-put-6300-6295",
        candidate_id="20260821-put-6300-6295",
        contracts=2,
        outcome=outcome,
        spent_for_s=for_s,
        credit_trigger_enabled=credit_trigger_enabled,
    )


async def _snapshot_with_spent(db, spent, *, evaluability=()):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0, positions=2),
        evaluability_source=(None if evaluability is None else (lambda: evaluability)),
        spent_exit_source=(None if spent is None else (lambda: spent)),
    )
    try:
        snap = await svc.assemble()
        return snap, snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()


async def test_no_watchdog_publishing_does_not_claim_every_attempt_is_intact(db):
    snap, js, html_out = await _snapshot_with_spent(db, None, evaluability=None)
    assert snap.spent_exit_attempts is None
    assert js["watchdog"]["spent_exit_attempts"] is None
    assert "all intact" not in html_out


async def test_a_live_watchdog_with_no_spent_attempts_says_so(db):
    _snap, js, html_out = await _snapshot_with_spent(db, ())
    assert js["watchdog"]["spent_exit_attempts"] == []
    assert "all intact" in html_out
    assert "NO EXIT LEFT" not in html_out


async def test_a_spent_attempt_is_visible_and_names_its_outcome(db):
    """The whole point: it must be READABLE on the page, not merely present in the
    JSON. The outcome is the operator's next move — a walk that timed out leaves an
    order that may still fill, a refused cross leaves nothing working at all."""
    _snap, js, html_out = await _snapshot_with_spent(
        db, (_spent(outcome="not_resolved", for_s=900.0),)
    )

    (row,) = js["watchdog"]["spent_exit_attempts"]
    assert row["candidate_id"] == "20260821-put-6300-6295"
    assert row["outcome"] == "not_resolved"
    assert row["contracts"] == 2
    assert row["spent_for_s"] == 900.0

    assert "NO EXIT LEFT" in html_out
    assert "not_resolved" in html_out
    assert "15m 0s" in html_out
    assert "needs a human" in html_out


async def test_with_the_credit_trigger_off_the_row_names_no_credit_stop(db):
    """Richard, 2026-09-27 (the v0.5.2 review's Q4): while the credit trigger is off,
    a page does not show `credit_stop` as the stop. The row says the trigger is off;
    `still_triggered` (the strike-touch stop) still says where the position stands."""
    row = dataclasses.replace(
        _spent(credit_trigger_enabled=False), cost_to_close=1.75, still_triggered=True
    )
    _snap, js, html_out = await _snapshot_with_spent(db, (row,))
    assert "mark: 1.75 (credit trigger off) — STILL THROUGH THE STOP" in html_out
    assert "vs stop" not in html_out
    (out,) = js["watchdog"]["spent_exit_attempts"]
    assert out["credit_stop"] is None and out["credit_trigger_enabled"] is False


async def test_with_the_credit_trigger_on_the_row_still_reads_against_the_stop(db):
    row = dataclasses.replace(_spent(), cost_to_close=1.75, credit_stop=1.70,
                              still_triggered=True)
    _snap, js, html_out = await _snapshot_with_spent(db, (row,))
    assert "mark: 1.75 vs stop 1.70 — STILL THROUGH THE STOP" in html_out
    (out,) = js["watchdog"]["spent_exit_attempts"]
    assert out["credit_stop"] == 1.70 and out["credit_trigger_enabled"] is True


async def test_a_spent_position_is_distinguishable_from_an_ordinary_watched_one(db):
    """The exact defect. Everything else about this snapshot is calm — positions
    open, every trigger evaluable — and the ONE position that has no automated path
    to close has to be tellable apart from that at a glance, in both renderings."""
    _snap, js, html_out = await _snapshot_with_spent(db, (_spent(),), evaluability=())

    # JSON: the calm row and the alarming one are separate keys with separate shapes.
    assert js["watchdog"]["impaired_positions"] == []
    assert len(js["watchdog"]["spent_exit_attempts"]) == 1

    # HTML: the exit-triggers row still says everything is evaluable — correctly, a
    # position nobody evaluates is not an impaired one — so the distinction cannot
    # rest on that row, and the new one has to carry it in WORDS as well as colour.
    assert "all evaluable" in html_out
    assert "Exit attempts" in html_out
    spent_cell = html_out[html_out.index("Exit attempts"):]
    assert "class='bad'" in spent_cell
    assert "NO EXIT LEFT" in spent_cell


async def test_the_row_carries_the_live_mark(db):
    """2026-08-28. Whoever reads this row is deciding whether to close the spread by
    hand, and that decision is made on what it costs NOW against the stop that fired
    — not on the fact that it fired. Before the watchdog kept pricing a spent
    position, the page could not answer that at all."""
    _snap, js, html_out = await _snapshot_with_spent(
        db,
        (dataclasses.replace(
            _spent(), cost_to_close=0.70, credit_stop=1.70, still_triggered=False
        ),),
    )

    (row,) = js["watchdog"]["spent_exit_attempts"]
    assert (row["cost_to_close"], row["credit_stop"]) == (0.70, 1.70)
    assert row["still_triggered"] is False

    assert "mark: 0.70 vs stop 1.70" in html_out
    # The state that changes what a human should do, and that `outcome` cannot say.
    assert "back inside the stop" in html_out


async def test_an_unpriced_mark_says_so_rather_than_showing_a_number(db):
    """A 0.0 cost-to-close on a row this severe is not a missing value, it is a wrong
    one — and it is the kind that gets acted on. A blank is nearly as bad: the row
    reads as having nothing to say."""
    _snap, js, html_out = await _snapshot_with_spent(db, (_spent(),))

    assert js["watchdog"]["spent_exit_attempts"][0]["cost_to_close"] is None
    assert "mark: unpriced" in html_out


async def test_a_clean_exit_never_reaches_the_row(db):
    """`spent_exit_attempts()` is scoped to what the ledger still says is held, so a
    filled exit that booked simply is not in it. Nothing here filters by outcome —
    a FILLED whose row never cleared is a real stub position and must still show."""
    _snap, js, _html = await _snapshot_with_spent(db, ())
    assert js["watchdog"]["spent_exit_attempts"] == []

    _snap, js, html_out = await _snapshot_with_spent(db, (_spent(outcome="filled"),))
    assert js["watchdog"]["spent_exit_attempts"][0]["outcome"] == "filled"
    assert "NO EXIT LEFT" in html_out


async def test_session_end_clears_the_spent_row_rather_than_tallying_it(db):
    """Session scoping, and it is not a nicety. The watchdog is rebuilt per trading
    day, so carrying this past the close would make the row a running ledger of every
    position that ever ended badly — the process-lifetime failure this page has
    already been fixed for once, at `killswitch_since` and `daily_loss.breach`."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0, positions=2),
        spent_exit_source=lambda: (_spent(),),
    )
    try:
        assert len((await svc.assemble()).spent_exit_attempts) == 1
        ss.session_finished(ended=True)
        ended = await svc.assemble()
        # None, not (): after the close there is no watchdog to answer the question,
        # and "" would assert that every held position still has its exit attempt.
        assert ended.spent_exit_attempts is None
        assert "NO EXIT LEFT" not in ended.to_html()

        ss.session_starting(NOW.date())          # and the next day starts clean
        assert (await svc.assemble()).spent_exit_attempts is None
    finally:
        await audit.aclose()


async def test_session_finished_clears_live_risk(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(lambda: _risk(md_age=1.0, acct_age=1.0))
    ss.session_finished(ended=True)
    try:
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.phase == "ended"
    assert snap.market_data_age_s is None  # risk source cleared on finish
    assert snap.account_nlv is None


async def test_never_connected_stream_reports_inf_age_and_stale(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(lambda: _risk(md_age=float("inf"), acct_age=float("inf")))
    try:
        snap = await svc.assemble()
        js = snap.to_json_dict()
    finally:
        await audit.aclose()

    # Running, but the feed never delivered: age is +inf -> stale True, and the
    # JSON collapses the non-finite age to null (phase + stale disambiguate).
    assert snap.market_data_stale is True
    assert js["market_data"]["age_s"] is None
    assert js["market_data"]["stale"] is True


# -- link health --------------------------------------------------------------
#
# Added 2026-08-05. `/status` reported how OLD the data was but nothing about the
# links carrying it, so "is my account link healthy?" could only be answered from
# the session's journal line — an ALL-TIME ledger that got misread as an active
# fault three times in one day. These are the fields that answer it directly.


def _links(
    *, md_connected=True, md_reconnects=0, acct_connected=True, acct_reconnects=0, refresh_fails=0
) -> LinkHealth:
    return LinkHealth(
        market_data_connected=md_connected,
        market_data_reconnects=md_reconnects,
        account_connected=acct_connected,
        account_reconnects=acct_reconnects,
        account_refresh_consecutive_failures=refresh_fails,
    )


async def test_link_health_is_surfaced_while_a_session_runs(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0),
        lambda: _links(md_reconnects=3, acct_connected=False, refresh_fails=2),
    )
    try:
        snap = await svc.assemble()
        js = snap.to_json_dict()
    finally:
        await audit.aclose()

    assert snap.market_data_connected is True
    assert snap.market_data_reconnects == 3
    assert snap.account_connected is False
    assert snap.account_refresh_consecutive_failures == 2
    assert js["market_data"]["connected"] is True
    assert js["market_data"]["reconnects"] == 3
    assert js["account_data"]["connected"] is False
    assert js["account_data"]["refresh_consecutive_failures"] == 2


async def test_link_health_is_read_live_not_cached_at_publish_time(db):
    """A link that drops after the session started must show as down.

    The publisher takes a CALLABLE for the same reason `risk_source` is one: a
    snapshot captured at publish time would report the state at 09:40 forever.
    """
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    live = {"connected": True, "reconnects": 0}
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0),
        lambda: _links(md_connected=live["connected"], md_reconnects=live["reconnects"]),
    )
    try:
        assert (await svc.assemble()).market_data_connected is True
        live["connected"] = False
        live["reconnects"] = 1
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.market_data_connected is False
    assert snap.market_data_reconnects == 1


async def test_link_health_clears_with_the_session(db):
    """Same lifecycle as the risk source: stale link counts off a torn-down feed
    would be worse than no counts at all."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(lambda: _risk(md_age=1.0, acct_age=1.0), lambda: _links(md_reconnects=4))
    ss.session_finished(ended=True)
    try:
        snap = await svc.assemble()
        js = snap.to_json_dict()
    finally:
        await audit.aclose()

    assert snap.market_data_connected is None
    assert snap.market_data_reconnects is None
    assert snap.account_refresh_consecutive_failures is None
    assert js["account_data"]["connected"] is None


async def test_a_session_without_a_link_source_still_assembles(db):
    """`link_source` is optional, so the pre-existing two-arg construction in
    tools and tests keeps working — it degrades to null fields, never an error."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(lambda: _risk(md_age=1.0, acct_age=1.0))
    try:
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.phase == "running"
    assert snap.market_data_age_s == 1.0     # risk still reported
    assert snap.market_data_connected is None  # links simply absent


async def test_html_flags_a_down_link_but_not_a_recovered_one(db):
    """Colour follows what is ACTIONABLE. A reconnect COUNT is the recovery logic
    working and must stay muted — colouring it would recreate the exact false
    positive these rows exist to remove."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0),
        lambda: _links(md_reconnects=20, acct_connected=False, refresh_fails=3),
    )
    try:
        html_out = (await svc.assemble()).to_html()
    finally:
        await audit.aclose()

    assert "DISCONNECTED" in html_out                      # account link is down
    assert "refresh failing ×3" in html_out
    # 20 market-data reconnects: reported, muted, NOT flagged.
    assert "reconnects 20 (recovered)" in html_out
    assert "<span class='muted'>reconnects 20 (recovered)</span>" in html_out


# -- memory + GC instrumentation ----------------------------------------------
#
# Added 2026-08-05 to diagnose the intra-session lag ramp: loop lag climbs
# ~+0.21s/hour from ~0.07s at the open to a 1.3s mean (2.2s peak) by 15:00,
# identically on every session from Jul 28 to Aug 4, and RESETS on process
# restart. The reset is what points at accumulation rather than load. These fields
# exist to identify the mechanism from one `/status` poll.


def test_rss_reader_parses_the_second_field_of_statm(tmp_path):
    """`/proc/self/statm` is seven PAGE counts; resident is the second."""
    import os
    statm = tmp_path / "statm"
    statm.write_text("29193 27924 4321 1 0 12034 0\n")
    assert read_rss_bytes(str(statm)) == 27924 * os.sysconf("SC_PAGE_SIZE")


@pytest.mark.parametrize("content", ["", "garbage", "1", "x y z"])
def test_rss_reader_never_raises_on_bad_input(tmp_path, content: str):
    """A DIAGNOSTIC on a read-only endpoint must never be able to break the page,
    let alone the process. Missing /proc (dev Macs), truncated reads, junk."""
    statm = tmp_path / "statm"
    statm.write_text(content)
    assert read_rss_bytes(str(statm)) is None


def test_rss_reader_returns_none_when_proc_is_absent():
    assert read_rss_bytes("/definitely/not/here") is None


def test_gc_snapshot_never_materializes_the_object_graph():
    """`len(gc.get_objects())` would be a multi-hundred-ms stall INSIDE the
    endpoint added to diagnose stalls. Pinned by AST, not by grep."""
    import ast
    import inspect

    import argus.status as status_mod

    tree = ast.parse(inspect.getsource(status_mod))
    attrs = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "get_objects" not in attrs
    assert "get_referrers" not in attrs  # same hazard, worse


def test_gc_snapshot_returns_three_generations():
    counts, collections = gc_snapshot()
    assert len(counts) == 3 and len(collections) == 3
    assert all(isinstance(n, int) and n >= 0 for n in counts + collections)


async def test_memory_growth_is_reported_from_a_single_poll(db):
    """The operator should not have to difference two polls by hand — that is the
    same trap the journal counts block set."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    rss = iter([60 * 1024 * 1024, 114 * 1024 * 1024])  # at construction, then at poll
    svc, _, _ = _make_service(audit, read_rss=lambda: next(rss))
    try:
        snap = await svc.assemble()
        js = snap.to_json_dict()
    finally:
        await audit.aclose()

    assert snap.rss_at_start_bytes == 60 * 1024 * 1024
    assert snap.rss_bytes == 114 * 1024 * 1024
    assert snap.rss_growth_bytes == 54 * 1024 * 1024
    assert js["process"]["rss_growth_bytes"] == 54 * 1024 * 1024
    # The RATE is not a single-poll figure any more: NOW - STARTED is 2h20m, and the
    # old `growth / uptime` would have printed 23 MB/h here out of a delta that may
    # be all startup cost. With no sampled window there is no rate (2026-09-01).
    assert js["process"]["rss_growth_bytes_per_hour"] is None
    assert js["process"]["gc_gen2_per_hour"] is None


async def test_rates_are_withheld_until_uptime_makes_them_meaningful(db):
    """A three-second-old process would report an absurd MB/hour. A rate needs a
    denominator worth dividing by."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    just_started = STARTED + timedelta(seconds=30)
    svc, _, _ = _make_service(
        audit, now=just_started, read_rss=lambda: 60 * 1024 * 1024
    )
    try:
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.uptime_s == 30.0
    assert snap.rss_growth_per_hour is None
    assert snap.gc_gen2_per_hour is None
    assert snap.rss_growth_bytes == 0  # the absolute figure is still honest


async def test_status_degrades_cleanly_where_proc_is_unavailable(db):
    """Dev machines are not Linux. A missing reading must render, not explode."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, _, _ = _make_service(audit, read_rss=lambda: None)
    try:
        snap = await svc.assemble()
        js, html_out = snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()

    assert snap.rss_bytes is None and snap.rss_growth_bytes is None
    assert js["process"]["rss_bytes"] is None
    assert "/proc unavailable" in html_out
    # GC stats are platform-independent and must still be there.
    assert js["process"]["gc_collections"] and len(js["process"]["gc_collections"]) == 3


async def test_html_surfaces_gen2_because_that_is_the_diagnostic(db):
    """Gen-2 is the generation whose pause scales with live-object count, so a
    rising gen-2 rate beside a rising loop lag is what identifies the mechanism."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    mono = [0.0]
    svc, _, _ = _make_service(
        audit, read_rss=lambda: 120 * 1024 * 1024, monotonic=lambda: mono[0]
    )
    try:
        svc.sample()  # one full window of history, so there is a rate to show
        mono[0] = 3600.0
        html_out = (await svc.assemble()).to_html()
    finally:
        await audit.aclose()

    assert "gen2" in html_out
    assert "gen2/h over last 60 min" in html_out
    assert "120.0 MB" in html_out


# -- assembly: DB-derived fields ---------------------------------------------


async def test_would_submit_today_raw_counts_only_todays_records(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, _, _ = _make_service(audit)
    try:
        # Two of today's would_submit records, recorded through the real writer.
        await audit.record("shadow.would_submit", {"n": 1})
        await audit.record("shadow.would_submit", {"n": 2})
        # A raw row dated just BEFORE today's ET-day boundary must NOT be counted,
        # and a same-category row from another day proves the date filter bites.
        boundary = _et_day_start_utc_iso(NOW)
        db.execute(
            "INSERT INTO audit_log (seq, ts, tier, category, payload, prev_hash, hash) "
            "VALUES (?,?,?,?,?,?,?)",
            (9001, "2026-07-25T23:59:59.000000Z", 1, "shadow.would_submit", "{}", "p" * 64, "h" * 64),
        )
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert boundary.startswith("2026-07-26T04:")  # ET midnight (EDT) in UTC
    assert snap.would_submit_today_raw == 2


async def test_breaker_tripped_tracks_the_live_breaker(db):
    """The reported state follows the rail the gate holds, tick for tick."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    breaker = DailyLossBreaker(audit, daily_max_loss=1000.0)
    svc, _, _ = _make_service(audit, breaker=breaker)
    try:
        snap_before = await svc.assemble()
        await breaker.evaluate(-5000.0)  # breaches -> the latch flips
        snap_after = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap_before.breaker_tripped is False
    assert snap_after.breaker_tripped is True


async def test_breaker_tripped_is_none_when_no_breaker_is_armed(db):
    """No session has built today's breaker: neither True nor False is honest.

    None is what `/status` must say before the process's first session, and on a
    process halted before `_run_session_day` ever gets to build one. Flattening it
    to False would report an armed rail with an unspent budget — a claim about
    something that does not exist.
    """
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, _, _ = _make_service(audit)  # no breaker injected
    try:
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.breaker_tripped is None
    assert snap.to_json_dict()["daily_loss_breaker"] == {"tripped": None}
    assert "not armed" in snap.to_html()


async def test_breaker_state_is_read_from_the_object_never_from_the_audit_log(db):
    """The regression guard, and it must fail if anyone re-derives this from a row.

    A `daily_loss.breach` record is written for TODAY while the live breaker is left
    untripped — exactly the divergence a same-day restart produced before
    `risk.rehydrate` existed, and the one that rendered a red TRIPPED on the page
    while the gate was still authorizing entries. The rail is the authority: the
    page must say what the gate would say, not what the ledger remembers.

    The reverse direction is pinned too — a tripped rail with no row on disk (the
    latch flipped, the record still queued) reports TRIPPED — so this cannot be
    satisfied by a log read that happens to agree today.
    """
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    breaker = DailyLossBreaker(audit, daily_max_loss=1000.0)
    svc, _, _ = _make_service(audit, breaker=breaker)
    try:
        # A breach on the record, no trip on the rail.
        await audit.record(BREACH_CATEGORY, {"daily_pnl": -5000.0, "threshold": 1000.0})
        snap_row_only = await svc.assemble()

        # And the mirror: a tripped rail whose own record we never wrote.
        tripped_only = DailyLossBreaker(audit, daily_max_loss=1000.0, initially_tripped=True)
        svc_tripped, _, _ = _make_service(audit, breaker=tripped_only)
        snap_rail_only = await svc_tripped.assemble()
    finally:
        await audit.aclose()

    assert snap_row_only.breaker_tripped is False, "a breach ROW is not a tripped RAIL"
    assert breaker.tripped is False, "precondition: the live rail never tripped"
    assert snap_rail_only.breaker_tripped is True

    # Structural, so the revert has nothing to reach for: the DB round trip carries
    # no breaker state at all any more.
    assert not any("breaker" in f for f in _DbStatus.__dataclass_fields__)


async def test_killswitch_source_and_since_come_from_trip_record(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    ks = KillSwitch(audit)
    svc, _, _ = _make_service(audit, killswitch=ks)
    try:
        await ks.engage("manual pull", source="http_endpoint")
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.killswitch_engaged is True
    assert snap.killswitch_reason == "manual pull"
    assert snap.killswitch_source == "http_endpoint"
    assert snap.killswitch_since is not None and snap.killswitch_since.endswith("Z")


async def test_a_previous_trip_does_not_show_as_a_live_one(db):
    """The trip row outlives the halt it records. `source`/`since` next to
    `engaged: false` read as a halt that is not happening — same shape `halted`
    already gets right.

    **The scenario reaching this state changed on 2026-09-01 and the rule did
    not.** It used to be a restart: the switch came up disengaged while the trip
    row sat on disk, which was itself the defect (a halt an operator pressed
    stopped being in force). A restart now RESTORES the trip, so the only way to
    have a trip row and no halt is the one that should exist — a human resumed it.
    → docs/decisions/2026-09-01-kill-switch-persistence.md
    """
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    ks = KillSwitch(audit)
    try:
        await ks.engage("yesterday's pull", source="http_endpoint")
        await ks.disengage(source="http_endpoint")
        svc, _, _ = _make_service(audit, killswitch=ks)
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.killswitch_engaged is False
    assert snap.killswitch_source is None
    assert snap.killswitch_since is None
    kill = snap.to_json_dict()["kill_switch"]
    assert kill == {
        "engaged": False, "reason": None, "source": None, "since": None,
        "restored": False,
    }
    # The trip is not erased — it is still in the audit tail, where history belongs.
    assert any(r["category"] == "kill_switch.trip" for r in snap.recent)


async def test_reconciliation_mismatch_trip_surfaces_with_no_special_casing(db):
    """A `source="reconciliation_mismatch"` trip flows through the SAME source-
    attribution plumbing as /kill's — no /status change needed — and its detail
    rides along in the recent tail, so a phone glance tells the whole story."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    ks = KillSwitch(audit)
    svc, _, _ = _make_service(audit, killswitch=ks)
    try:
        await ks.engage(
            "broker reconciliation mismatch (1 discrepancy(ies))",
            source="reconciliation_mismatch",
            detail={"ok": False, "discrepancies": [
                {"symbol": "SPXW260718P05000000", "kind": "unexpected_at_broker",
                 "expected_qty": 0, "broker_qty": -1},
            ]},
        )
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.killswitch_engaged is True
    assert snap.killswitch_source == "reconciliation_mismatch"
    # The trip is in the recent tail with the discrepancy detail intact.
    trip = next(r for r in snap.recent if r["category"] == "kill_switch.trip")
    assert "unexpected_at_broker" in trip["payload"]
    # And it renders into both views without special handling.
    assert "reconciliation_mismatch" in snap.to_html()
    assert snap.to_json_dict()["kill_switch"]["source"] == "reconciliation_mismatch"


async def test_recent_tail_is_most_recent_first(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, _, _ = _make_service(audit)
    try:
        await audit.record("first", {"i": 1})
        await audit.record("second", {"i": 2})
        await audit.record("third", {"i": 3})
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    cats = [r["category"] for r in snap.recent]
    assert cats[:3] == ["third", "second", "first"]
    assert snap.recent[0]["seq"] > snap.recent[1]["seq"]


# -- HTTP: JSON --------------------------------------------------------------


async def test_get_status_json_ok_with_token(tmp_path: Path):
    async with server_harness(tmp_path) as h:
        resp = await http_request(h.host, h.port, "GET", "/status", headers=_bearer())
        assert resp.code == 200
        assert resp.headers["content-type"] == "application/json"
        body = json.loads(resp.text)
        assert body["environment"] == "shadow"
        assert body["session"]["phase"] == "idle"
        assert body["kill_switch"]["engaged"] is False
        assert "would_submit_today_raw" in body["shadow"]
        assert isinstance(body["recent"], list)


async def test_get_status_reflects_live_session_and_kill(tmp_path: Path):
    async with server_harness(tmp_path) as h:
        h.session.session_starting(NOW.date())
        h.session.session_running(lambda: _risk(md_age=0.5, acct_age=0.5))
        await h.killswitch.engage("test halt", source="test")
        resp = await http_request(h.host, h.port, "GET", "/status", headers=_bearer())
        body = json.loads(resp.text)
        assert body["session"]["phase"] == "running"
        assert body["market_data"]["age_s"] == 0.5
        assert body["kill_switch"]["engaged"] is True
        assert body["kill_switch"]["source"] == "test"


# -- HTTP: HTML --------------------------------------------------------------


async def test_get_root_returns_html(tmp_path: Path):
    async with server_harness(tmp_path) as h:
        resp = await http_request(h.host, h.port, "GET", "/", headers=_bearer())
        assert resp.code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "<!doctype html>" in resp.text.lower()
        assert "argus" in resp.text
        # The viewport meta makes it phone-legible; no external asset references.
        assert "viewport" in resp.text
        assert "http://" not in resp.text and "https://" not in resp.text
        assert "<script" not in resp.text.lower()


async def test_html_escapes_audit_payload(tmp_path: Path):
    async with server_harness(tmp_path) as h:
        # A payload with markup must be escaped, never rendered as live HTML.
        await h.audit.record("evil.category", {"note": "<script>alert(1)</script>"})
        resp = await http_request(h.host, h.port, "GET", "/", headers=_bearer())
        assert "<script>alert(1)</script>" not in resp.text
        assert "&lt;script&gt;" in resp.text


# -- HTTP: auth + routing (parity with /kill) --------------------------------


@pytest.mark.parametrize("path", ["/status", "/"])
async def test_status_routes_require_token(tmp_path: Path, path: str):
    async with server_harness(tmp_path) as h:
        resp = await http_request(h.host, h.port, "GET", path)  # no Authorization
        assert resp.code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("path", ["/status", "/"])
async def test_status_routes_reject_bad_token(tmp_path: Path, path: str):
    async with server_harness(tmp_path) as h:
        resp = await http_request(h.host, h.port, "GET", path, headers=_bearer("wrong"))
        assert resp.code == 401


async def test_post_status_is_404_not_405(tmp_path: Path):
    # /status is a GET-only route; a POST to it is simply not found (it falls
    # through to the /kill path check, which 404s a non-/kill path).
    async with server_harness(tmp_path) as h:
        resp = await http_request(h.host, h.port, "POST", "/status", headers=_bearer())
        assert resp.code == 404


async def test_unknown_get_path_is_404(tmp_path: Path):
    async with server_harness(tmp_path) as h:
        resp = await http_request(h.host, h.port, "GET", "/nope", headers=_bearer())
        assert resp.code == 404


async def test_kill_route_unaffected_by_status_service(tmp_path: Path):
    # Adding the read routes must not change /kill: GET /kill still 405s, and a
    # POST still trips.
    async with server_harness(tmp_path) as h:
        get = await http_request(h.host, h.port, "GET", "/kill", headers=_bearer())
        assert get.code == 405 and get.headers.get("allow") == "POST"
        assert h.killswitch.engaged is False

        post = await http_request(h.host, h.port, "POST", "/kill", headers=_bearer())
        assert post.code == 200 and h.killswitch.engaged is True


# -- the token never leaks ---------------------------------------------------


async def test_token_never_appears_in_status_responses(tmp_path: Path, caplog):
    # Use a distinctive token and a kill trip whose record is in the recent tail;
    # the token must appear in NEITHER rendering, NOR the logs.
    async with server_harness(tmp_path) as h:
        await h.killswitch.engage("pulled", source="http_endpoint")
        with caplog.at_level(logging.DEBUG, logger="argus.control"):
            js = await http_request(h.host, h.port, "GET", "/status", headers=_bearer())
            html_resp = await http_request(h.host, h.port, "GET", "/", headers=_bearer())
        assert js.code == 200 and html_resp.code == 200
        assert TOKEN not in js.raw.decode("latin-1")
        assert TOKEN not in html_resp.raw.decode("latin-1")
        assert TOKEN not in caplog.text


# -- stream backlogs: the lag-ramp instrument ---------------------------------


async def test_stream_backlogs_are_surfaced_and_only_nonzero_ones_are_flagged(db):
    """Both SDK streamers buffer into UNBOUNDED memory object streams. A climbing
    depth is the accumulation behind the intra-session lag ramp; near-zero
    exonerates the streaming path. Nine zeros every poll would bury the one
    number that matters, so only non-zero depths render."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0),
        lambda: LinkHealth(
            market_data_connected=True, market_data_reconnects=0,
            account_connected=True, account_reconnects=0,
            account_refresh_consecutive_failures=0,
            market_data_queue_depths={"Quote": 0, "Greeks": 0},
            account_queue_depths={"TradingStatus": 41_233, "Order": 0},
        ),
    )
    try:
        snap = await svc.assemble()
        js, html_out = snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()

    assert js["account_data"]["queue_depths"]["TradingStatus"] == 41233
    assert js["market_data"]["queue_depths"] == {"Quote": 0, "Greeks": 0}
    assert "acct.TradingStatus 41,233" in html_out
    assert "Quote" not in html_out.split("Stream backlogs")[1].split("</tr>")[0]


async def test_all_drained_reads_clean_rather_than_blank(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0),
        lambda: LinkHealth(
            market_data_connected=True, market_data_reconnects=0,
            account_connected=True, account_reconnects=0,
            account_refresh_consecutive_failures=0,
            market_data_queue_depths={"Quote": 0}, account_queue_depths={"Order": 0},
        ),
    )
    try:
        html_out = (await svc.assemble()).to_html()
    finally:
        await audit.aclose()
    assert "all drained" in html_out


def test_every_alert_type_the_sdk_buffers_is_either_consumed_or_discarded():
    """THE regression guard. `AlertStreamer._queues` is a defaultdict of
    inf-capacity streams and `_map_message` fills one per type in MAP_ALERTS. Any
    type with no reader is retained for the life of the process. If tastytrade
    adds a tenth alert type, this must keep holding."""
    from tastytrade.account import AccountBalance, CurrentPosition
    from tastytrade.order import PlacedOrder
    from tastytrade.streamer import MAP_ALERTS

    from argus.account.stream import _UNCONSUMED_ALERTS

    consumed = {CurrentPosition, AccountBalance, PlacedOrder}
    assert set(MAP_ALERTS.values()) == consumed | set(_UNCONSUMED_ALERTS)
    assert not (consumed & set(_UNCONSUMED_ALERTS))  # never both


# -- per-category session counts (added 2026-08-11) ------------------------
#
# `would_submit_today_raw` counts ONE shadow-only category, so it reads 0 forever
# in sandbox and live — the only environments where orders exist. The phone-
# reachable surface could not answer "has anything executed today?" at all, while
# the journal could. These pin the fix.


async def test_session_counts_report_the_delta_not_the_all_time_total(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit, environment="sandbox")
    try:
        # Pre-session history: must NOT appear as if it happened today.
        await audit.record("entry.dry_run", {"n": 1})
        await audit.record("entry.dry_run", {"n": 2})
        ss.session_starting(NOW.date())
        ss.session_running(lambda: _risk(md_age=1.0, acct_age=1.0),
                           None, audit.counts)
        await audit.record("entry.dry_run", {"n": 3})
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.session_counts["entry.dry_run"] == 1, (
        "3 all-time, 1 this session — the delta is what answers 'today'"
    )


async def test_session_counts_omit_categories_that_did_not_move(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit, environment="sandbox")
    try:
        await audit.record("account.refresh_failed", {"n": 1})
        ss.session_starting(NOW.date())
        ss.session_running(lambda: _risk(md_age=1.0, acct_age=1.0),
                           None, audit.counts)
        await audit.record("entry.dry_run", {"n": 1})
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert "entry.dry_run" in snap.session_counts
    assert "account.refresh_failed" not in snap.session_counts, (
        "a frozen counter must render as absent, not as a stale-looking number — "
        "the ambiguity that caused three false diagnoses on 2026-08-05"
    )


async def test_session_counts_are_empty_before_the_first_session(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit, environment="sandbox")
    try:
        await audit.record("entry.dry_run", {"n": 1})
        snap_idle = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap_idle.session_counts == {}, "no baseline => no delta, not a total"


async def test_session_counts_survive_the_end_of_the_session(db):
    """The EOD read is exactly when this gets looked at. (2026-08-19)

    Live sources are cleared at session end because they are handles into a feed
    that no longer exists. The counts baseline is not a handle — it is a static
    snapshot, and the totals it subtracts from are still in the audit DB. Clearing
    it left `/status` unable to answer "what happened today?" forty minutes after
    the close, which is the gap this field was added to fill.
    """
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit, environment="sandbox")
    try:
        ss.session_starting(NOW.date())
        ss.session_running(lambda: _risk(md_age=1.0, acct_age=1.0),
                           None, audit.counts)
        await audit.record("entry.dry_run", {"n": 1})
        ss.session_finished(ended=True)
        snap_after = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap_after.session_counts == {"entry.dry_run": 1}
    # The live sources still read as "no active session" — the asymmetry is the point.
    assert snap_after.market_data_age_s is None
    assert snap_after.open_position_count is None


async def test_session_counts_reach_both_renderings(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit, environment="sandbox")
    try:
        ss.session_starting(NOW.date())
        ss.session_running(lambda: _risk(md_age=1.0, acct_age=1.0),
                           None, audit.counts)
        await audit.record("entry.dry_run", {"n": 1})
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.to_json_dict()["audit"]["session_counts"]["entry.dry_run"] == 1
    html_out = snap.to_html()
    assert "entry.dry_run" in html_out and "+1" in html_out


async def test_would_submit_row_warns_that_it_is_shadow_only_outside_shadow(db):
    """A bare 0 there reads as 'found nothing' when it means 'wrong counter for
    this environment'. That trap cost a real question on 2026-08-11."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, _, _ = _make_service(audit, environment="sandbox")
    shadow_svc, _, _ = _make_service(audit, environment="shadow")
    try:
        sandbox_html = (await svc.assemble()).to_html()
        shadow_html = (await shadow_svc.assemble()).to_html()
    finally:
        await audit.aclose()

    assert "shadow-only counter" in sandbox_html
    assert "shadow-only counter" not in shadow_html, "in shadow it IS the counter"


async def test_an_empty_baseline_is_a_real_baseline_not_an_absent_one(db):
    """Regression: `if counts_baseline` treated {} as "no baseline", so a session
    starting against a fresh audit DB reported no counts for its whole life."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit, environment="sandbox")
    try:
        assert audit.counts == {}, "precondition: nothing recorded yet"
        ss.session_starting(NOW.date())
        ss.session_running(lambda: _risk(md_age=1.0, acct_age=1.0), None, audit.counts)
        await audit.record("entry.dry_run", {"n": 1})
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.session_counts == {"entry.dry_run": 1}


async def test_session_counts_render_name_sorted_in_both_views(db):
    """Stable position across reloads, and the same order in JSON and HTML — a
    count-sorted list would bury entry.dry_run (~7) under
    entry.already_attempted (~250) and reshuffle on every refresh."""
    audit = AuditLog(db, tier2_queue_maxsize=1000, tier2_queue_alert_depth=800)
    audit.start()
    svc, ss, _ = _make_service(audit, environment="sandbox")
    try:
        ss.session_starting(NOW.date())
        ss.session_running(lambda: _risk(md_age=1.0, acct_age=1.0), None, audit.counts)
        for _ in range(7):
            await audit.record("entry.dry_run", {})
        for _ in range(30):
            audit.telemetry("entry.already_attempted", {})
        await asyncio.sleep(0.2)
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    html_out = snap.to_html()
    assert html_out.index("entry.already_attempted") < html_out.index("entry.dry_run")
    assert list(snap.to_json_dict()["audit"]["session_counts"]) == sorted(
        snap.session_counts
    ), "JSON and HTML must order identically — one assembly, two renderings"


# -- the halted state on the page ---------------------------------------------


async def test_a_halted_process_says_so_in_both_renderings(db):
    """The reason a halted process stays alive at all is that this page answers.

    HALTED is strictly stronger than an engaged kill switch and must not be read
    off it: `/kill` at 10:00 engages the switch and leaves a process that resumes
    tomorrow, while halted means no further sessions and a restart will not help.
    """
    from argus.app import HaltRecord

    halt = HaltRecord(
        reason="broker reconciliation mismatch (3 discrepancy(ies))",
        source="reconciliation_mismatch",
        at=NOW,
    )
    audit = AuditLog(db, tier2_queue_maxsize=1000, tier2_queue_alert_depth=800)
    audit.start()
    svc, _, _ = _make_service(audit, halt_state=lambda: halt)
    try:
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.halted is True
    assert snap.halt_source == "reconciliation_mismatch"
    assert snap.halted_since == NOW.isoformat()

    blob = snap.to_json_dict()["halted"]
    assert blob == {
        "halted": True,
        "reason": "broker reconciliation mismatch (3 discrepancy(ies))",
        "source": "reconciliation_mismatch",
        "since": NOW.isoformat(),
        # The default grade. A record built without one is the process-wide halt
        # this page has always described.
        "grade": "all",
    }

    html_out = snap.to_html()
    assert "HALTED" in html_out
    assert "restart required" in html_out
    assert "reconciliation_mismatch" in html_out


async def test_a_healthy_process_carries_no_halt_row(db):
    audit = AuditLog(db, tier2_queue_maxsize=1000, tier2_queue_alert_depth=800)
    audit.start()
    svc, _, _ = _make_service(audit)
    try:
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.halted is False and snap.halt_reason is None
    assert snap.to_json_dict()["halted"]["halted"] is False
    assert "WILL NOT TRADE" not in snap.to_html()


# -- the config fingerprint: the version stamp's missing half ---------------


async def test_the_config_fingerprint_reaches_json_and_html(db):
    """Driven end to end from a REAL `Config.load` rather than a hand-built row.

    2026-09-02's lesson, applied ahead of time: a copy into a target with `None`
    defaults cannot fail loudly, so it has to be pinned by driving real objects
    through to rendered output. A hand-built snapshot here would pass forever
    while `Application.startup` quietly stopped passing the values.
    """
    from argus.config import Config

    cfg = Config.load("argus.live.toml")
    assert cfg.source is not None

    audit = AuditLog(db, tier2_queue_maxsize=1000, tier2_queue_alert_depth=800)
    audit.start()
    svc, _, _ = _make_service(
        audit,
        config_profile=cfg.source.profile,
        config_filename=cfg.source.filename,
        config_sha=cfg.source.sha,
    )
    try:
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.to_json_dict()["config"] == {
        "profile": "live",
        "file": "argus.live.toml",
        "sha": cfg.source.sha,
    }
    html_out = snap.to_html()
    assert "argus.live.toml" in html_out
    assert cfg.source.sha in html_out
    # The expected state is plain text: no badge on a row that is right on every
    # ordinary deploy. (`_config_cell`'s colour rule, same as `_version_cell`'s.)
    assert "no profile in the filename" not in html_out


async def test_a_profileless_config_says_the_check_is_not_armed(db):
    """`argus.toml` and an ad-hoc `--config` path are legal and assert NOTHING —
    which is the one thing about them worth saying out loud on the page."""
    audit = AuditLog(db, tier2_queue_maxsize=1000, tier2_queue_alert_depth=800)
    audit.start()
    svc, _, _ = _make_service(
        audit, config_profile=None, config_filename="argus.toml", config_sha="abc123def456"
    )
    try:
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.to_json_dict()["config"]["profile"] is None
    assert "no profile in the filename" in snap.to_html()


async def test_no_config_file_renders_as_a_fact_not_a_default(db):
    """A `Config.from_dict` construction (tools, tests). Muted, and it cannot
    happen to a deployed process — so it must not read as a fault."""
    audit = AuditLog(db, tier2_queue_maxsize=1000, tier2_queue_alert_depth=800)
    audit.start()
    svc, _, _ = _make_service(audit)
    try:
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    assert snap.to_json_dict()["config"] == {"profile": None, "file": None, "sha": None}
    assert "not a deployed state" in snap.to_html()


# -- the traded account: the pin's confirmable half (2026-09-10, C9) -----------
#
# The cutover is the one moment "which account is this process trading?" is the
# whole question, and until this row existed the answer was unobservable: the
# process took accounts[0], whichever account the login happened to list first.
# → docs/designs/2026-09-10-pinning-the-traded-account.md §3.5


async def test_the_account_row_names_a_pinned_account(db):
    """The number an operator reads back at the cutover, beside Credentials —
    the same place they confirm which registration the process authenticated
    with."""
    audit = AuditLog(db, tier2_queue_maxsize=1000, tier2_queue_alert_depth=800)
    audit.start()
    svc, _, _ = _make_service(audit, broker_account_number="5WX00000")
    try:
        snap = await svc.assemble()
        html_out = snap.to_html()
    finally:
        await audit.aclose()

    assert "5WX00000" in html_out
    assert "pinned" in html_out


async def test_the_account_row_says_why_it_is_unpinned(db):
    """MUTED, never amber (design D7): unpinned is the intended, permanent cert
    state and unreachable in live, so colour here would be a badge that means
    nothing on the day it finally matters. It SAYS WHY instead of showing a dash,
    so an operator on the droplet knows the state is deliberate."""
    from argus.status import _account_cell

    audit = AuditLog(db, tier2_queue_maxsize=1000, tier2_queue_alert_depth=800)
    audit.start()
    svc, _, _ = _make_service(audit, broker_account_number=None)
    try:
        snap = await svc.assemble()
        html_out = snap.to_html()
    finally:
        await audit.aclose()

    assert "accounts[0]" in html_out
    assert "not pinned" in html_out
    # D7's assertion — the one that stops the row being re-coloured. On the CELL,
    # not the page: colour elsewhere on the page is some other row's business,
    # and only this row's colour is settled here.
    cell = _account_cell(None)
    assert "accounts[0]" in cell
    assert "warn" not in cell


async def test_the_payload_carries_the_pin_separately_from_the_number(db):
    """`pinned` is its own boolean so a deploy script can assert the RAIL without
    knowing the value — `account.pinned == true` is the cutover's post-deploy
    check (design §7). It rides in the existing `account` block, ahead of the
    measurements: identity first, then what the account holds."""
    audit = AuditLog(db, tier2_queue_maxsize=1000, tier2_queue_alert_depth=800)
    audit.start()
    svc, _, _ = _make_service(audit, broker_account_number="5WX00000")
    try:
        snap = await svc.assemble()
        js = snap.to_json_dict()
    finally:
        await audit.aclose()

    assert js["account"]["pinned"] is True
    assert js["account"]["number"] == "5WX00000"

    # Unpinned: the cert state, and the pre-2026-09-10 behaviour for everyone.
    audit2 = AuditLog(db, tier2_queue_maxsize=1000, tier2_queue_alert_depth=800)
    audit2.start()
    try:
        svc2, _, _ = _make_service(audit2)
        snap2 = await svc2.assemble()
        js2 = snap2.to_json_dict()
    finally:
        await audit2.aclose()

    assert js2["account"]["pinned"] is False
    assert js2["account"]["number"] is None
    # The measurement half of the block is untouched by the pin half.
    assert js2["account"]["nlv"] is None
    assert js2["account"]["open_position_count"] is None


async def test_an_unanchored_day_pnl_renders_unanchored_never_as_zero(db):
    """The runbook's day-1 G1 cross-check reads `account.daily_pnl`. Before an anchor
    exists that value is `DailyPnl`'s placeholder 0.0 — rendered as a number, it is
    the G1 failure presented as a reading. (2026-09-18)"""
    from dataclasses import replace as dc_replace

    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: dc_replace(_risk(md_age=1.0, acct_age=1.0), daily_pnl_anchored=False)
    )
    try:
        snap = await svc.assemble()
        js, html_out = snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()

    assert js["account"]["daily_pnl"] is None
    assert js["account"]["daily_pnl_anchored"] is False
    assert js["account"]["nlv"] == 100_000.0       # the NLV is a real reading
    assert "UNANCHORED" in html_out


async def test_an_anchored_day_pnl_still_renders_its_value(db):
    from dataclasses import replace as dc_replace

    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: dc_replace(_risk(md_age=1.0, acct_age=1.0, pnl=-125.0),
                           daily_pnl_anchored=True)
    )
    try:
        snap = await svc.assemble()
        js, html_out = snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()

    assert js["account"]["daily_pnl"] == -125.0
    assert js["account"]["daily_pnl_anchored"] is True
    assert "UNANCHORED" not in html_out
