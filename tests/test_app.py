"""Application supervisor: startup/shutdown wiring, audit boundaries, stall trip,
and the always-on session scheduler (day rollover, holiday suppression, clean
stop). The scheduler tests inject a fake session body + fake clock/wait, so they
run offline with no credentials and no live market."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from argus.app import Application
from argus.clock import ET
from argus.config import Config
from support import make_config
from argus.db import connect



def _config(tmp_path: Path, **overrides) -> Config:
    audit = {**overrides.pop("audit", {}), "db_path": str(tmp_path / "argus.db")}
    # These tests induce real loop stalls, so they need a fast heartbeat; an
    # explicit heartbeat override from a test still wins.
    heartbeat = {
        "interval_s": 0.01,
        "lag_alert_threshold_s": 0.02,
        "sustained_breach_count": 2,
        **overrides.pop("heartbeat", {}),
    }
    return make_config(audit=audit, heartbeat=heartbeat, **overrides)


async def test_startup_records_process_start_and_wires_collaborators(tmp_path: Path):
    app = Application(_config(tmp_path))
    await app.startup()
    try:
        # Shadow with no broker: reconcile.skipped (seq 1) then process.start (seq 2).
        # The authoritative reconcile now runs per-session inside the body; process
        # start still records the skip for the no-broker boundary.
        assert app.audit.last_seq == 2
        assert app.killswitch.engaged is False
        assert app.heartbeat.breach_count == 0
        # The session scheduler is wired (per-day risk state is built inside it,
        # not at process scope — that is the day-rollover contract).
        assert app._scheduler is not None
        app.audit.verify_chain()
    finally:
        await app.shutdown()


async def test_shutdown_records_process_stop_and_closes_cleanly(tmp_path: Path):
    cfg = _config(tmp_path)
    app = Application(cfg)
    await app.startup()
    await app.shutdown()

    # Reopen the db file independently and confirm the boundary events persisted
    # and the chain is intact after a clean shutdown.
    conn = connect(cfg.audit.db_path)
    rows = conn.execute("SELECT category FROM audit_log ORDER BY seq").fetchall()
    categories = [r["category"] for r in rows]
    conn.close()
    # `process.start` comes FIRST, before the reconcile verdict: the control
    # surface binds before the reconciliation rail runs (2026-08-17), so by the
    # time anything is reconciled the process genuinely has started.
    assert categories[0] == "process.start"
    assert "reconcile.skipped" in categories
    assert categories[-1] == "process.stop"


async def test_shutdown_is_safe_before_startup(tmp_path: Path):
    app = Application(_config(tmp_path))
    await app.shutdown()  # no-op, must not raise


async def test_non_shadow_startup_reconciles_for_real_and_refuses_if_it_cannot(
    tmp_path: Path, monkeypatch
):
    """Sandbox/live must not start without having ASKED the broker.

    Before chunk 5 this raised "requires a reconciler" and there was no way to build
    one — the process boots credential-free. Now it builds one itself
    (`session.startup_reconcile`, a short-lived trading session), so the guard rail
    moved: the failure to prove is no longer "you gave me no reconciler" but "I could
    not reach the broker, therefore I will not start". Both are refusals to resume
    blind; only the second one is reachable now.
    """
    # No credentials in the test environment -> the session cannot open. Cleared
    # explicitly so a developer's real .env cannot turn this into a live login.
    # Every trading slot, both worlds — not just the one this config selects. The
    # comment above is the right instinct and the old two-name list was the wrong
    # size for it: a test that later builds a `live` config would have read the
    # developer's real live registration.
    for var in (
        "TASTYTRADE_TRADING_CERT_CLIENT_SECRET",
        "TASTYTRADE_TRADING_CERT_REFRESH_TOKEN",
        "TASTYTRADE_TRADING_LIVE_CLIENT_SECRET",
        "TASTYTRADE_TRADING_LIVE_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    app = Application(_config(tmp_path, environment="sandbox"))
    with pytest.raises(Exception) as excinfo:
        await app.startup()
    # Whatever the broker layer raised, it must NOT be a silently-clean startup.
    assert not isinstance(excinfo.value, AssertionError)
    # And nothing is left running: startup tears down on any failure after resources
    # exist, so a second attempt is not blocked by a still-held writer lock.
    app2 = Application(_config(tmp_path, environment="shadow"))
    await app2.startup()
    await app2.shutdown()


async def test_clean_reconciliation_allows_startup(tmp_path: Path):
    from argus.account.fake import FakeBrokerAccount
    from argus.reconcile import InMemoryPositionStore, Reconciler

    # Broker flat, expected flat -> clean -> process starts.
    reconciler = Reconciler(FakeBrokerAccount([]), InMemoryPositionStore())
    app = Application(_config(tmp_path, environment="sandbox"), reconciler=reconciler)
    await app.startup()
    try:
        cats = [
            r["category"]
            for r in app.audit._conn.execute("SELECT category FROM audit_log ORDER BY seq")
        ]
        assert "reconcile.result" in cats
        assert "process.start" in cats
    finally:
        await app.shutdown()


async def test_reconciliation_mismatch_HALTS_startup_but_keeps_the_process_alive(tmp_path: Path):
    """The rail still refuses the day; it no longer kills the process to do it.

    Until 2026-08-17 this raised out of `startup()`, which tore down the control
    server and exited — and because the bind came AFTER reconciliation, the restart
    never got the surface back either. On 2026-08-14 that ran 34 times with
    `/status` and `/kill` unreachable throughout. The refusal is now a PARKED
    process: halted, observable, and unable to trade.
    """
    from argus.account.models import Position
    from argus.account.fake import FakeBrokerAccount
    from argus.reconcile import InMemoryPositionStore, Reconciler

    broker = FakeBrokerAccount(
        [Position("SPXW260718P05000000", "SPX", "Equity Option", -1, 1.20, 0.9)]
    )
    cfg = _config(tmp_path, environment="sandbox")
    app = Application(cfg, reconciler=Reconciler(broker, InMemoryPositionStore()))

    await app.startup()  # no longer raises
    try:
        assert app._halted is not None, "the rail refused"
        assert app._halted.source == "reconciliation_mismatch"
        assert app._killswitch is not None and app._killswitch.engaged
        # Alive and serving: this is the entire point of the change.
        assert app._control is not None and app._control.bound_address is not None
        snapshot = await app._control._status_service.assemble()
        assert snapshot.halted is True
        assert snapshot.halt_source == "reconciliation_mismatch"

        # And it cannot trade, even via the direct single-session entry point that
        # bypasses the scheduler.
        await app._run_session_day(
            date(2026, 8, 17), app._now_et() + timedelta(hours=1), asyncio.Event()
        )
    finally:
        await app.shutdown()

    conn = connect(cfg.audit.db_path)
    cats = [r["category"] for r in conn.execute("SELECT category FROM audit_log ORDER BY seq")]
    conn.close()
    assert "reconcile.result" in cats
    assert "process.start" in cats, "the process really did start; it then halted"
    assert "session.configured" not in cats, "and never ran a session"


async def test_shadow_startup_reconcile_mismatch_proceeds_but_loudly(tmp_path: Path):
    """Consistency with the per-session path: at PROCESS start too, a shadow
    mismatch is lenient — the process starts (nothing executes in shadow) and the
    kill switch stays disengaged. Only sandbox/live refuse startup
    (`test_reconciliation_mismatch_refuses_startup`). In production shadow wires no
    process-start reconciler at all; this pins the branch that keeps the two call
    sites gated the same way.

    "Proceeds" must mean loud-and-recorded, NOT silently ignored: a mismatch is
    safety-relevant even in shadow. So it still fires the alert AND writes the
    Tier-1 verdict with the discrepancy detail — the same visibility the
    per-session path has, minus the abort."""
    from argus.account.models import Position
    from argus.account.fake import FakeBrokerAccount
    from argus.reconcile import InMemoryPositionStore, Reconciler

    broker = FakeBrokerAccount(
        [Position("SPXW260718P05000000", "SPX", "Equity Option", -1, 1.20, 0.9)]
    )
    alerts: list[str] = []
    app = Application(
        _config(tmp_path, environment="shadow"),
        reconciler=Reconciler(broker, InMemoryPositionStore()),
        alert=lambda kind, _detail: alerts.append(kind),
    )
    await app.startup()  # must NOT raise — shadow is lenient
    try:
        assert "process.start" in [
            r["category"]
            for r in app.audit._conn.execute("SELECT category FROM audit_log ORDER BY seq")
        ]                                          # started despite the mismatch
        assert app.killswitch.engaged is False     # not a hard halt
        # Loud, not silent: the alert fired and the verdict is durably on the
        # record with the discrepancy detail (ok=false), same as per-session.
        assert "reconciliation_mismatch" in alerts
        verdict = app.audit._conn.execute(
            "SELECT payload FROM audit_log WHERE category='reconcile.result'"
        ).fetchone()["payload"]
        assert '"ok": false' in verdict
        assert "unexpected_at_broker" in verdict
    finally:
        await app.shutdown()


async def test_sustained_stall_trips_killswitch_when_enabled(tmp_path: Path):
    cfg = _config(
        tmp_path,
        heartbeat={
            "interval_s": 0.01,
            "lag_alert_threshold_s": 0.02,
            "sustained_breach_count": 3,
            "trip_on_sustained_stall": True,
        },
    )
    app = Application(cfg)
    await app.startup()
    try:
        assert app.killswitch.engaged is False
        # One long block produces a run of consecutive catch-up breaches, which
        # crosses the sustained threshold and, with the flag on, trips the switch.
        time.sleep(0.1)
        await _wait_until(lambda: app.killswitch.engaged, timeout=1.0)
        assert app.killswitch.engaged is True
        assert app.killswitch.reason is not None and "stall" in app.killswitch.reason
    finally:
        await app.shutdown()


async def test_sustained_stall_does_not_trip_when_disabled(tmp_path: Path):
    app = Application(_config(tmp_path))  # trip flag defaults False in _BASE
    await app.startup()
    try:
        time.sleep(0.1)
        # Give the heartbeat time to observe the stall; it should alert but not trip.
        await _wait_until(lambda: app.heartbeat.breach_count >= 1, timeout=1.0)
        assert app.killswitch.engaged is False
    finally:
        await app.shutdown()


async def _wait_until(predicate, *, timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within timeout")


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _dt(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)


# -- the parked (halted-but-alive) state --------------------------------------
#
# The 2026-08-14 incident: a reconcile mismatch raised out of `startup()`, the
# process exited, Docker restarted it into the same mismatch, and because the
# control server bound AFTER the reconciliation rail, `/status` and `/kill` were
# unreachable for all 34 iterations while Slack took ~68 messages. A halt must
# refuse to trade WITHOUT killing the surface the operator halts and diagnoses
# from.


async def test_a_halted_process_parks_alive_and_a_signal_releases_it(tmp_path: Path):
    from argus.account.models import Position
    from argus.account.fake import FakeBrokerAccount
    from argus.reconcile import InMemoryPositionStore, Reconciler

    alerts: list[str] = []
    broker = FakeBrokerAccount(
        [Position("SPXW260718P05000000", "SPX", "Equity Option", -1, 1.20, 0.9)]
    )
    app = Application(
        _config(tmp_path, environment="sandbox"),
        reconciler=Reconciler(broker, InMemoryPositionStore()),
        alert=lambda kind, detail: alerts.append(kind),
    )

    task = asyncio.create_task(app.run())
    try:
        await _wait_until(lambda: alerts.count("process_halted") == 1, timeout=2.0)
        # PARKED: not finished, and still serving.
        assert not task.done(), "a halt must not exit the process"
        assert app._control is not None and app._control.bound_address is not None
        assert (await app._control._status_service.assemble()).halted is True
        # Announced once — not once per restart, which is what the storm was.
        assert alerts.count("process_halted") == 1
    finally:
        app.request_exit()
        await asyncio.wait_for(task, timeout=2.0)

    assert task.done() and task.exception() is None


async def test_an_in_session_halt_parks_too_rather_than_exiting(tmp_path: Path):
    """The other way in. A mismatch found by the PER-SESSION reconcile halts a
    process that is already fully up; it must park on the same terms as the
    startup path, through the same single announcement point."""
    clock = _Clock(_dt(date(2026, 7, 24), 9, 40))

    async def wait(seconds: float) -> None:
        clock.now += timedelta(seconds=seconds)

    alerts: list[str] = []
    holder: dict[str, Application] = {}
    sessions: list[date] = []

    async def body(*, config, audit, state, stop, trading_date, alert, account_number):
        sessions.append(trading_date)
        await holder["app"]._halt_process(
            "broker reconciliation mismatch (test)", source="reconciliation_mismatch"
        )

    app = Application(
        _config(tmp_path),
        session_body=body,
        now_et=clock,
        wait_fn=wait,
        alert=lambda kind, detail: alerts.append(kind),
    )
    holder["app"] = app

    task = asyncio.create_task(app.run())
    try:
        await _wait_until(lambda: "process_halted" in alerts, timeout=2.0)
        assert not task.done()
        assert app._halted is not None
        assert app.killswitch.engaged is True
    finally:
        app.request_exit()
        await asyncio.wait_for(task, timeout=2.0)

    # One session attempted, then never again — the scheduler does not roll into
    # the next trading day on a halted process.
    assert sessions == [date(2026, 7, 24)]


async def test_the_first_halt_reason_wins_over_a_cascading_one(tmp_path: Path):
    """A halt often cascades (a mismatch stops the day, the audit log then latches
    failed on the way down). The cause is the first reason, not the symptom."""
    app = Application(_config(tmp_path))
    await app.startup()
    try:
        await app._halt_process("the actual cause", source="reconciliation_mismatch")
        await app._halt_process("a downstream symptom", source="audit_log")
        assert app._halted is not None
        assert app._halted.reason == "the actual cause"
        assert app._halted.source == "reconciliation_mismatch"
    finally:
        await app.shutdown()


# -- always-on session scheduler (fake body + fake clock, no broker) ----------


async def test_day_rollover_rebuilds_risk_state_and_keeps_killswitch(tmp_path: Path):
    """The core rollover contract: per-day breaker/day-P&L are rebuilt fresh, but
    a kill-switch trip survives into the next day (a rollover must never un-halt)."""
    clock = _Clock(_dt(date(2026, 7, 24), 9, 40))  # Friday, at the open

    async def wait(seconds: float) -> None:
        clock.now += timedelta(seconds=seconds)

    seen: list[dict] = []
    holder: dict[str, Application] = {}

    async def body(*, config, audit, state, stop, trading_date, alert, account_number):
        app = holder["app"]
        seen.append(
            {
                "date": trading_date,
                "breaker": state.breaker,
                "day_pnl": state.day_pnl,
                "ks_on_entry": app.killswitch.engaged,
            }
        )
        if len(seen) == 1:
            # Day 1 spends its risk budget and engages the manual halt.
            await state.breaker.evaluate(-10 * config.risk.daily_max_loss)
            state.day_pnl.anchor_to(100_000.0)
            await app.killswitch.engage("manual (test)", source="test")
        if len(seen) >= 2:
            app.request_stop()

    app = Application(_config(tmp_path), session_body=body, now_et=clock, wait_fn=wait)
    holder["app"] = app
    await asyncio.wait_for(app.run(), timeout=2.0)

    assert [s["date"] for s in seen] == [date(2026, 7, 24), date(2026, 7, 27)]  # Fri, Mon
    # Fresh INSTANCES per day — the rollover re-instantiates, it does not reset.
    assert seen[0]["breaker"] is not seen[1]["breaker"]
    assert seen[0]["day_pnl"] is not seen[1]["day_pnl"]
    # Day 1 tripped/anchored; day 2 starts clean.
    assert seen[0]["breaker"].tripped is True
    assert seen[1]["breaker"].tripped is False
    assert seen[1]["day_pnl"].anchored is False
    # The kill switch is process-lifetime: off on day 1's entry, still ON on day 2.
    assert seen[0]["ks_on_entry"] is False
    assert seen[1]["ks_on_entry"] is True
    assert app.killswitch.engaged is True


async def test_midweek_holiday_starts_no_session(tmp_path: Path):
    """Thanksgiving (Wed -> holiday Thu -> half-day Fri): the holiday runs nothing."""
    clock = _Clock(_dt(date(2026, 11, 25), 9, 40))  # Wednesday, at the open

    async def wait(seconds: float) -> None:
        clock.now += timedelta(seconds=seconds)

    ran: list[date] = []
    holder: dict[str, Application] = {}

    async def body(*, stop, trading_date, **_kw):
        ran.append(trading_date)
        if len(ran) >= 2:
            holder["app"].request_stop()

    app = Application(_config(tmp_path), session_body=body, now_et=clock, wait_fn=wait)
    holder["app"] = app
    await asyncio.wait_for(app.run(), timeout=2.0)

    # Wed 11-25 then straight to the half day Fri 11-27 — Thu 11-26 is suppressed.
    assert ran == [date(2026, 11, 25), date(2026, 11, 27)]


async def test_stopped_while_idle_shuts_down_cleanly(tmp_path: Path):
    """SIGTERM while idling outside market hours: no session, clean process.stop."""
    clock = _Clock(_dt(date(2026, 7, 25), 12, 0))  # Saturday — the first act is a sleep
    cfg = _config(tmp_path)
    ran: list = []
    holder: dict[str, Application] = {}

    async def wait(_seconds: float) -> None:
        holder["app"].request_stop()  # SIGTERM lands during the idle wait

    async def body(**_kw):
        ran.append(1)

    app = Application(cfg, session_body=body, now_et=clock, wait_fn=wait)
    holder["app"] = app
    await asyncio.wait_for(app.run(), timeout=2.0)

    assert ran == []  # never entered a session
    conn = connect(cfg.audit.db_path)
    categories = [r["category"] for r in conn.execute("SELECT category FROM audit_log ORDER BY seq")]
    conn.close()
    assert categories[-1] == "process.stop"


async def test_unexpected_session_error_does_not_stop_the_supervisor(tmp_path: Path):
    """A single day's bug must not end the always-on process — it idles to the
    next trading day and records the failure (the timer model's per-day isolation).

    And the record has to SAY WHAT FAILED. On 2026-08-26 a `ReadTimeout` out of the
    per-day reconcile landed here and `session.error` carried only the trading date,
    so the only route to the cause was `journalctl` on the droplet. Both fields match
    `account.refresh_failed`'s shape, including its 500-char bound.
    """
    clock = _Clock(_dt(date(2026, 7, 24), 9, 40))  # Friday

    async def wait(seconds: float) -> None:
        clock.now += timedelta(seconds=seconds)

    dates: list[date] = []
    holder: dict[str, Application] = {}
    alerts: list[tuple[str, dict]] = []

    async def body(*, trading_date, **_kw):
        dates.append(trading_date)
        if len(dates) == 1:
            # Unexpected, not a SessionAborted. Long on purpose: the message is
            # truncated, not dropped and not written whole.
            raise RuntimeError("boom in day 1 " + "x" * 900)
        holder["app"].request_stop()

    app = Application(
        _config(tmp_path),
        session_body=body,
        now_et=clock,
        wait_fn=wait,
        alert=lambda kind, detail: alerts.append((kind, detail)),
    )
    holder["app"] = app
    await asyncio.wait_for(app.run(), timeout=2.0)

    # Day 1 blew up, but the supervisor carried on to Monday.
    assert dates == [date(2026, 7, 24), date(2026, 7, 27)]
    conn = connect(app._config.audit.db_path)
    rows = [
        (r["category"], r["payload"])
        for r in conn.execute("SELECT category, payload FROM audit_log ORDER BY seq")
    ]
    conn.close()
    cats = [c for c, _ in rows]
    assert "session.error" in cats
    assert cats[-1] == "process.stop"

    detail = json.loads(next(d for c, d in rows if c == "session.error"))
    assert detail["trading_date"] == "2026-07-24"
    assert detail["error_type"] == "RuntimeError"
    assert detail["error"].startswith("boom in day 1 ")
    assert len(detail["error"]) == 500, "truncated at the same bound, not dropped"
    # The Slack payload is the same dict — an alert that says only the date is the
    # half of this defect an operator actually sees first.
    assert alerts == [("session_error", detail)]


# -- kill-switch control surface wiring (spec §6) -----------------------------


async def test_startup_refuses_without_kill_token(tmp_path: Path, monkeypatch):
    """An unauthenticated halt control is a broken safety rail — no token, no boot.

    The token is resolved before any resource is acquired, so the failure leaves
    nothing half-open: setting the token and retrying then starts cleanly."""
    from argus.control import KILL_TOKEN_VAR, MissingKillTokenError

    monkeypatch.delenv(KILL_TOKEN_VAR, raising=False)  # conftest sets it; remove it
    cfg = _config(tmp_path)
    app = Application(cfg)  # kill_token=None -> reads the (now absent) env var
    with pytest.raises(MissingKillTokenError):
        await app.startup()

    # Nothing was acquired (the token check is first): a retry with the token set
    # succeeds, which also proves the DB writer lock was never taken.
    monkeypatch.setenv(KILL_TOKEN_VAR, "now-present")
    app2 = Application(cfg)
    await app2.startup()
    await app2.shutdown()


async def test_startup_binds_control_and_kill_endpoint_trips(tmp_path: Path):
    """Full supervisor wiring: the endpoint is live after startup and a real HTTP
    POST /kill over loopback trips the process-lifetime kill switch."""
    from test_control import http_request

    app = Application(_config(tmp_path), kill_token="known-token")
    await app.startup()
    try:
        host, port = app.control.bound_address
        assert app.killswitch.engaged is False
        resp = await http_request(
            host, port, "POST", "/kill",
            headers=["Authorization: Bearer known-token"],
        )
        assert resp.code == 200
        assert app.killswitch.engaged is True

        # process.start recorded the bound endpoint (no token) for the trail.
        row = app.audit._conn.execute(
            "SELECT payload FROM audit_log WHERE category = 'process.start'"
        ).fetchone()
        assert f"{host}:{port}" in row["payload"]
        assert "known-token" not in row["payload"]
    finally:
        await app.shutdown()


async def test_kill_with_failed_audit_write_halts_the_whole_process(tmp_path: Path, monkeypatch):
    """A /kill whose Tier 1 trip write fails must not just 500 — it must halt the
    PROCESS, uniformly with every other audit-write failure.

    `KillSwitch.engage` swallows the AuditWriteError (the halt must stand even
    unrecorded), so it never propagates out of the endpoint. The guarantee is
    instead carried by the audit log's on_failure hook, which engages the switch
    AND requests process stop — so even hit while idle (no session loop to catch
    the error), the supervisor still winds down for a restart with a working
    recorder rather than idling on with a dead one."""
    from test_control import http_request
    import sqlite3

    def boom(_rec):
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    app = Application(_config(tmp_path), kill_token="tok")
    await app.startup()  # records process.start while the writer is still healthy
    try:
        host, port = app.control.bound_address
        monkeypatch.setattr(app.audit, "_insert", boom)  # break the recorder now
        resp = await http_request(
            host, port, "POST", "/kill", headers=["Authorization: Bearer tok"]
        )
        assert resp.code == 500                     # not a false 200
        assert app.killswitch.engaged is True       # trading halted
        assert app.audit.failed is not None          # recorder latched failed
        assert app._stop.is_set()                    # process asked to stop for restart
    finally:
        await app.shutdown()


async def test_startup_refuses_on_unbindable_bind_host(tmp_path: Path):
    """A missing interface (Tailscale down / wrong IP) fails startup loud and tears
    down — it never falls back to a wider bind, and it frees the writer lock."""
    from argus.control import ControlServerBindError

    # 192.0.2.1 (TEST-NET-1) is not an address on this host.
    cfg = _config(tmp_path, server={"bind_host": "192.0.2.1", "bind_port": 0})
    app = Application(cfg)
    with pytest.raises(ControlServerBindError):
        await app.startup()
    assert app._started is False

    # The lock was released on teardown: a fresh app on the same DB can start.
    good = Application(_config(tmp_path))  # tmp_path -> same db_path
    await good.startup()
    await good.shutdown()


async def test_halt_process_engages_killswitch_and_requests_stop(tmp_path: Path):
    """The ONE halt pairing (spec §6): engage the kill switch AND request process
    stop, the mechanism a sandbox/live reconcile mismatch reuses — identical to the
    audit-write-failure halt (`test_kill_with_failed_audit_write_...`), factored
    into one helper so the two can't drift. The trip is attributed and carries the
    mismatch detail, which is exactly what surfaces on /status for free."""
    app = Application(_config(tmp_path))
    await app.startup()
    try:
        assert app.killswitch.engaged is False
        assert app._stop.is_set() is False

        await app._halt_process(
            "broker reconciliation mismatch (1 discrepancy(ies))",
            source="reconciliation_mismatch",
            detail={"ok": False, "discrepancies": [
                {"symbol": "SPXW260718P05000000", "kind": "unexpected_at_broker",
                 "expected_qty": 0, "broker_qty": -1},
            ]},
        )

        assert app.killswitch.engaged is True       # new entries halted
        assert app._stop.is_set() is True           # process asked to stop for restart
        row = app.audit._conn.execute(
            "SELECT payload FROM audit_log WHERE category = 'kill_switch.trip' "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        assert "reconciliation_mismatch" in row["payload"]
        assert "unexpected_at_broker" in row["payload"]  # detail rides along
    finally:
        await app.shutdown()


async def test_run_single_session_runs_the_body_once(tmp_path: Path):
    """The tools.shadow path: one session, time-boxed, same per-day state build."""
    seen: list = []

    async def body(*, stop, trading_date, state, **_kw):
        seen.append((trading_date, state))
        await stop.wait()  # a session ends when its stop fires (here, the timer)

    app = Application(_config(tmp_path), session_body=body)
    await app.startup()
    try:
        await asyncio.wait_for(app.run_single_session(stop_after_s=0.05), timeout=2.0)
    finally:
        await app.shutdown()
    assert len(seen) == 1


# -- the alerting path (chunk 4's retrofit) -----------------------------------


async def test_startup_wires_the_slack_notifier_when_a_webhook_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The `AlertFn` seam has existed since `alerts.py` was written and nothing was
    ever plugged into it. Wiring it HERE is what gets every existing consumer — audit
    queue depth and write failure, heartbeat stalls, reconcile mismatches, session
    errors — onto a phone without each having to know how."""
    from argus.notify import WEBHOOK_ENV_VAR

    posts: list[str] = []
    monkeypatch.setenv(WEBHOOK_ENV_VAR, "https://hooks.slack.com/services/T/B/secret")
    monkeypatch.setattr(
        "argus.notify._urllib_post",
        lambda url, body, timeout_s: posts.append(body),
    )

    app = Application(_config(tmp_path))
    await app.startup()
    try:
        assert app._notifier is not None
        # The process-wide alert sink now fans out to the log AND Slack.
        app._alert("daily_loss_breach", {"pnl": -1000.0})
    finally:
        await app.shutdown()
    assert any("daily_loss_breach" in body for body in posts)


async def test_startup_without_a_webhook_behaves_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A missing Slack webhook is a missing NOTIFICATION channel, not a broken safety
    rail — unlike ARGUS_KILL_TOKEN it must never refuse startup."""
    from argus.alerts import log_alert
    from argus.notify import WEBHOOK_ENV_VAR

    monkeypatch.delenv(WEBHOOK_ENV_VAR, raising=False)
    app = Application(_config(tmp_path))
    await app.startup()
    try:
        assert app._notifier is None
        assert app._alert is log_alert
    finally:
        await app.shutdown()


async def test_an_injected_alert_sink_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Tools and tests pass their own sink; startup must not override it."""
    from argus.notify import WEBHOOK_ENV_VAR

    monkeypatch.setenv(WEBHOOK_ENV_VAR, "https://hooks.slack.com/services/T/B/secret")
    seen: list[str] = []
    app = Application(_config(tmp_path), alert=lambda k, d: seen.append(k))
    await app.startup()
    try:
        assert app._notifier is None
        app._alert("x", {})
        assert seen == ["x"]
    finally:
        await app.shutdown()


async def test_shutdown_closes_the_notifier_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Alerts raised during teardown must still be deliverable."""
    from argus.notify import WEBHOOK_ENV_VAR

    posts: list[str] = []
    monkeypatch.setenv(WEBHOOK_ENV_VAR, "https://hooks.slack.com/services/T/B/secret")
    monkeypatch.setattr(
        "argus.notify._urllib_post",
        lambda url, body, timeout_s: posts.append(body),
    )
    app = Application(_config(tmp_path))
    await app.startup()
    notifier = app._notifier
    assert notifier is not None
    await app.shutdown()
    assert app._notifier is None
    assert notifier.queue_depth == 0


# -- gen-2 GC tuning: the measured cause of the intra-session lag ramp ---------


def test_tune_gc_freezes_the_startup_heap_and_slows_gen2(monkeypatch):
    """Both levers, and neither may be silently dropped.

    The ramp is gen-2 GC: its FREQUENCY comes from the DXLink allocation rate
    (~2,420 events/s -> a pass every ~7s) and its COST is linear in live objects
    (4ms at 50k, 198ms at 2M, x~4 on the droplet). freeze() attacks the cost;
    the threshold attacks the frequency.
    """
    import gc as gc_mod

    from argus.app import tune_gc
    from argus.config import GcConfig

    calls = {"freeze": 0}
    original = gc_mod.get_threshold()
    monkeypatch.setattr(gc_mod, "freeze", lambda: calls.__setitem__("freeze", 1))
    try:
        tune_gc(GcConfig())
        assert calls["freeze"] == 1, "the startup heap must be frozen out of gen-2"
        gen0, gen1, gen2 = gc_mod.get_threshold()
        assert gen2 == GcConfig().gen2_threshold
        # gen0/gen1 are cheap and collect the quote churn that SHOULD be collected;
        # slowing them would grow the heap for no benefit.
        assert (gen0, gen1) == original[:2]
    finally:
        gc_mod.set_threshold(*original)


def test_gc_tuning_can_be_fully_backed_out_from_config(monkeypatch):
    """The escape hatch, and the whole reason these are config rather than
    constants: `argus.toml` is bind-mounted, so restoring stock CPython behaviour
    is a RESTART, not an image rebuild. That matters while the underlying
    accumulation is still unfound."""
    import gc as gc_mod

    from argus.app import tune_gc
    from argus.config import GcConfig

    calls = {"freeze": 0}
    original = gc_mod.get_threshold()
    monkeypatch.setattr(gc_mod, "freeze", lambda: calls.__setitem__("freeze", 1))
    try:
        tune_gc(GcConfig(freeze_on_startup=False, gen2_threshold=10))
        assert calls["freeze"] == 0, "freeze must be skippable"
        assert gc_mod.get_threshold() == (original[0], original[1], 10)
    finally:
        gc_mod.set_threshold(*original)


def test_tune_gc_never_materializes_the_object_graph():
    """`len(gc.get_objects())` inside the function whose job is removing stalls
    would itself be a multi-hundred-ms stall. Pinned by AST."""
    import ast
    import inspect

    import argus.app as app_mod

    tree = ast.parse(inspect.getsource(app_mod))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "tune_gc")
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "get_objects" not in attrs
    assert "get_referrers" not in attrs


# -- the loop-debug instrument (ARGUS_LOOP_DEBUG) -------------------------


async def test_loop_debug_is_off_unless_asked():
    """It must ship dark: a deploy that does not set the var pays nothing, and
    asyncio debug mode must not be left on by accident — it is diagnostic-only."""
    from argus.app import enable_loop_debug

    assert enable_loop_debug(env={}) is None
    assert asyncio.get_running_loop().get_debug() is False


async def test_loop_debug_enables_named_slow_callback_logging():
    from argus.app import DEFAULT_SLOW_CALLBACK_S, enable_loop_debug

    loop = asyncio.get_running_loop()
    try:
        assert enable_loop_debug(env={"ARGUS_LOOP_DEBUG": "1"}) == DEFAULT_SLOW_CALLBACK_S
        assert loop.get_debug() is True
        assert loop.slow_callback_duration == DEFAULT_SLOW_CALLBACK_S
    finally:
        loop.set_debug(False)


async def test_loop_debug_threshold_is_overridable():
    from argus.app import enable_loop_debug

    loop = asyncio.get_running_loop()
    try:
        assert enable_loop_debug(
            env={"ARGUS_LOOP_DEBUG": "on", "ARGUS_SLOW_CALLBACK_S": "0.75"}
        ) == 0.75
        assert loop.slow_callback_duration == 0.75
    finally:
        loop.set_debug(False)


async def test_loop_debug_refuses_a_threshold_that_would_flood():
    """<=0 logs EVERY callback. On a 2,420 event/sec feed that buries the stall
    this exists to find, so it falls back rather than obeying."""
    from argus.app import DEFAULT_SLOW_CALLBACK_S, enable_loop_debug

    loop = asyncio.get_running_loop()
    try:
        for bad in ("0", "-1", "not-a-number"):
            assert enable_loop_debug(
                env={"ARGUS_LOOP_DEBUG": "1", "ARGUS_SLOW_CALLBACK_S": bad}
            ) == DEFAULT_SLOW_CALLBACK_S
    finally:
        loop.set_debug(False)
