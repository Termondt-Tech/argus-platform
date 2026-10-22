"""A halt survives the process that imposed it, and only a human clears it.

The scenario these pin: the operator presses `POST /kill` at 10:00, walks away
trusting the halt is in force, and at 10:05 the process restarts — a container
OOM, a deploy (`argus-deploy.sh` restarts the service), a droplet reboot, a
crash-loop. `restart: unless-stopped` and systemd's `Restart=always` all do this.
Before 2026-09-01 the new process constructed `KillSwitch` with
`_engaged = False`, reconciled clean, started a session and **resumed entries** —
with no alert (nothing called `engage`, and the alert lives inside it) and a
green `/status` showing a null source, because the page gated the trip row it had
just read behind `ks.engaged`.

Two halves, and each needs the other:

  - **rehydration** restores the halt, and can only ever restore one;
  - **`POST /resume`** is the only thing that clears one — human, authenticated,
    confirmation-gated, audited, and the sole caller of `KillSwitch.disengage`
    anywhere in `src/`.

The restart tests are a SIMULATION in the same sense `test_day_risk_rehydrate.py`
is: two `Application` lifetimes over one database file, the writer lock released
between them. Unlike the breaker's, this one IS provable in cert — nothing here
depends on an NLV cert never marks — but the simulation is what pins it in CI.

→ docs/decisions/2026-09-01-kill-switch-persistence.md
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import fresh_loss_budget
from argus.app import Application
from argus.audit.log import AuditLog
from argus.clock import et_today
from argus.config import Config
from argus.control import (
    RESUME_PATH,
    TRIP_SOURCE,
    ControlServer,
)
from argus.db import apply_migrations, connect
from argus.reconcile import HaltGrade
from argus.risk.kill_state import (
    RESTORE_FAILED_CATEGORY,
    RESTORE_FAILED_SOURCE,
    RESTORED_CATEGORY,
    KillSwitchState,
    load_kill_switch_state,
)
from argus.risk.killswitch import RESUME_CATEGORY, TRIP_CATEGORY, KillSwitch
from support import make_config
from test_control import TOKEN, _bearer, http_request

_SRC = Path(__file__).resolve().parents[1] / "src" / "argus"


def _config(tmp_path: Path, **overrides) -> Config:
    audit = {**overrides.pop("audit", {}), "db_path": str(tmp_path / "argus.db")}
    return make_config(audit=audit, **overrides)


async def _noop_body(**_kwargs) -> None:
    """A session that does nothing. These tests are about the state a session is
    HANDED, not about what a session does with it."""


async def _run_one_process(cfg: Config, *, body=None, session: bool = False):
    """Start a process over `cfg`'s DB, optionally run one session, shut down.

    Each call is a distinct process lifetime against the same file — the writer
    lock releases on shutdown, so calling it twice IS the restart. Returns the
    (now shut down) Application so a test can read what it booted with.
    """
    app = Application(cfg, session_body=body or _noop_body)
    await app.startup()
    try:
        if session:
            await app._run_session_day(
                et_today(), app._now_et() + timedelta(hours=1), asyncio.Event()
            )
        return app
    finally:
        await app.shutdown()


def _rows(cfg: Config, category: str) -> list[dict]:
    conn = connect(cfg.audit.db_path)
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT seq, ts, payload FROM audit_log WHERE category = ? "
                "ORDER BY seq",
                (category,),
            ).fetchall()
        ]
    finally:
        conn.close()


async def _kill_then(cfg: Config, *, resume: bool = False, kill_again: bool = False):
    """Drive the durable state through one process, the way the endpoint does."""
    app = Application(cfg, session_body=_noop_body)
    await app.startup()
    try:
        await app.killswitch.engage("manual halt", source=TRIP_SOURCE)
        if resume:
            await app.killswitch.disengage(source=TRIP_SOURCE)
        if kill_again:
            await app.killswitch.engage("second halt", source=TRIP_SOURCE)
    finally:
        await app.shutdown()


# -- the restart matrix -------------------------------------------------------


async def test_a_kill_SURVIVES_a_restart(tmp_path: Path):
    """The test the whole change exists for.

    Without rehydration the second process comes up disengaged and its gate
    authorizes entries — silently, because nothing called `engage`.
    """
    cfg = _config(tmp_path)
    await _kill_then(cfg)

    app = await _run_one_process(cfg)

    assert app.killswitch.engaged is True, "the halt survived the restart"
    assert app.killswitch.reason == "manual halt", "with the ORIGINAL reason"
    assert app.killswitch.source == TRIP_SOURCE, "and the ORIGINAL attribution"
    assert app.killswitch.restored is True


async def test_the_restored_kill_actually_denies_at_the_gate(tmp_path: Path):
    """Restored state has to reach the RAIL, not just the object.

    The kill switch is only meaningful through the three points a trip stops new
    risk at (2026-08-03); this pins the first of them, which is the one every
    candidate crosses.
    """
    cfg = _config(tmp_path)
    await _kill_then(cfg)

    decisions: list = []

    async def gate_body(*, state, **_kwargs):
        from argus.risk.models import EntryProposal, RiskSnapshot

        decisions.append(
            await state.gate.evaluate(
                EntryProposal(symbol="SPXW", contracts=1, max_loss=425.0),
                # A snapshot that would otherwise pass every rail: only the
                # restored halt can deny this.
                RiskSnapshot(
                    open_position_count=0,
                    daily_pnl=0.0,
                    account_nlv=100_000.0,
                    market_data_age_s=0.5,
                    account_stream_age_s=1.0, loss_budget=fresh_loss_budget(),
                ),
            )
        )

    await _run_one_process(cfg, body=gate_body, session=True)

    assert decisions and decisions[0].allowed is False
    assert "kill_switch_engaged" in decisions[0].reasons


async def test_kill_then_resume_then_restart_comes_up_TRADING(tmp_path: Path):
    cfg = _config(tmp_path)
    await _kill_then(cfg, resume=True)

    app = await _run_one_process(cfg)

    assert app.killswitch.engaged is False
    assert app.killswitch.restored is False


async def test_kill_resume_kill_then_restart_stays_HALTED(tmp_path: Path):
    """Pins the `seq` comparison specifically.

    An implementation that asks "does a resume exist?" rather than "is the newest
    resume AFTER the newest trip?" passes every other case in this file and fails
    this one — with the failure in the unsafe direction.
    """
    cfg = _config(tmp_path)
    await _kill_then(cfg, resume=True, kill_again=True)

    app = await _run_one_process(cfg)

    assert app.killswitch.engaged is True
    assert app.killswitch.reason == "second halt"


async def test_an_unhalted_process_restarts_unattended_and_TRADES(tmp_path: Path):
    """The guarantee that keeps every deploy and overnight reboot working.

    The fix must be structurally incapable of imposing a halt on a process that
    was never halted — so this asserts it over both a clean shutdown and one with
    no `process.stop` at all, because engagement consults NEITHER.
    """
    cfg = _config(tmp_path)
    await _run_one_process(cfg, session=True)
    assert _rows(cfg, "process.stop"), "precondition: the first stop was clean"

    app = await _run_one_process(cfg)
    assert app.killswitch.engaged is False

    # Now an UNCLEAN one: startup() with no shutdown(), the way an OOM leaves it.
    crashed = Application(cfg, session_body=_noop_body)
    await crashed.startup()
    await crashed._teardown()  # release the writer lock without recording a stop

    app = await _run_one_process(cfg)
    assert app.killswitch.engaged is False, (
        "an unclean restart with no kill pressed must still trade — imposing a "
        "halt here would make every crash a manual /resume"
    )


async def test_a_halt_survives_the_DAY_ROLLOVER_too(tmp_path: Path):
    """The scoping rule, in the direction opposite the breaker's.

    `risk/rehydrate.py` bounds its queries to one ET day precisely so a latch
    cannot leak across days. This one must NEVER acquire that bound: a Friday
    `/kill` is still in force on Monday. A date range added to `kill_state` by a
    later "harmonisation" fails here.
    """
    cfg = _config(tmp_path)
    await _kill_then(cfg)
    # Backdate the trip a week: the read has no `ts` predicate at all, so this is
    # the mutation test for one being added.
    conn = connect(cfg.audit.db_path)
    try:
        conn.execute(
            "UPDATE audit_log SET ts = '2020-01-02T15:04:05.000Z' WHERE category = ?",
            (TRIP_CATEGORY,),
        )
        conn.commit()
    finally:
        conn.close()

    app = await _run_one_process(cfg)
    assert app.killswitch.engaged is True


async def test_a_rail_halt_restarts_MONITOR_ONLY_not_parked_and_not_trading(
    tmp_path: Path,
):
    """A reconcile-mismatch halt, restarted after the cause is fixed.

    Rehydration restores the KILL SWITCH and not the `HaltRecord`, deliberately:
    the halt's verdict is re-derived by the startup reconcile (which passes now),
    while the trip keeps refusing every entry until a human clears it. So the new
    process is engaged, NOT halted, the scheduler runs and the watchdog is built —
    monitor-only, not parked, not trading.
    """
    cfg = _config(tmp_path)
    app = Application(cfg, session_body=_noop_body)
    await app.startup()
    try:
        await app._halt_process(
            "broker reconciliation mismatch (1 discrepancy(ies))",
            source="reconciliation_mismatch",
        )
        assert app.halt_grade() is HaltGrade.ALL
    finally:
        await app.shutdown()

    restarted = await _run_one_process(cfg)

    assert restarted.killswitch.engaged is True, "the trip is restored"
    assert restarted.killswitch.source == "reconciliation_mismatch"
    assert restarted._halted is None, (
        "the HaltRecord is NOT rehydrated — the reconcile that produced it re-ran "
        "and passed, so this process is monitor-only rather than parked"
    )
    assert restarted.halt_grade() is None


# -- the restoration is not a trip -------------------------------------------


async def test_restoring_writes_NO_second_trip_record(tmp_path: Path):
    """Which is what keeps `/status`'s `since` the moment it was pressed.

    A restoration that called `engage` would stamp every reboot as a fresh trip,
    so the page would report the halt as starting whenever the box last restarted
    — and the `seq` comparison would be re-based on every restart.
    """
    cfg = _config(tmp_path)
    await _kill_then(cfg)
    original = _rows(cfg, TRIP_CATEGORY)
    assert len(original) == 1

    await _run_one_process(cfg)
    await _run_one_process(cfg)

    after = _rows(cfg, TRIP_CATEGORY)
    assert len(after) == 1, "two restarts, still exactly one trip"
    assert after[0]["ts"] == original[0]["ts"]


async def test_a_restored_start_records_and_pages_exactly_once(tmp_path: Path):
    cfg = _config(tmp_path)
    await _kill_then(cfg)

    alerts: list = []
    app = Application(
        cfg, session_body=_noop_body, alert=lambda kind, detail: alerts.append(kind)
    )
    await app.startup()
    await app.shutdown()

    restored = _rows(cfg, RESTORED_CATEGORY)
    assert len(restored) == 1
    payload = json.loads(restored[0]["payload"])
    assert payload["source"] == TRIP_SOURCE
    assert payload["reason"] == "manual halt"
    assert payload["trip_seq"] is not None
    assert alerts.count("kill_switch_restored") == 1
    assert "kill_switch_engaged" not in alerts, (
        "nothing was engaged — a halt was FOUND engaged, and the two read "
        "differently to anyone paging through history"
    )


async def test_an_ordinary_start_announces_nothing(tmp_path: Path):
    cfg = _config(tmp_path)
    alerts: list = []
    app = Application(
        cfg, session_body=_noop_body, alert=lambda kind, detail: alerts.append(kind)
    )
    await app.startup()
    await app.shutdown()

    assert _rows(cfg, RESTORED_CATEGORY) == []
    assert "kill_switch_restored" not in alerts


# -- fail closed --------------------------------------------------------------


async def test_an_unreadable_trip_payload_still_restores_the_halt(tmp_path: Path):
    """The row's EXISTENCE is the load-bearing fact; the payload is the story.

    Contrast `rehydrate._anchor_from`, which refuses: an unreadable anchor makes
    every later number wrong, so there is no safe assumption. Here there is one,
    and coming up disengaged because a reason would not parse is the bug.
    """
    cfg = _config(tmp_path)
    await _kill_then(cfg)
    conn = connect(cfg.audit.db_path)
    try:
        conn.execute(
            "UPDATE audit_log SET payload = 'not json' WHERE category = ?",
            (TRIP_CATEGORY,),
        )
        conn.commit()
    finally:
        conn.close()

    alerts: list = []
    app = Application(
        cfg, session_body=_noop_body, alert=lambda kind, detail: alerts.append(kind)
    )
    await app.startup()
    await app.shutdown()

    assert app.killswitch.engaged is True
    assert app.killswitch.reason, "check() asserts a reason exists whenever engaged"
    app.killswitch.check  # the object is usable, not a landmine
    with pytest.raises(Exception):
        app.killswitch.check()
    assert _rows(cfg, RESTORE_FAILED_CATEGORY), "the degradation is on the record"
    assert alerts.count("kill_switch_restored") == 1, "and pages ONCE, not twice"


async def test_an_unreadable_DATABASE_fails_closed_to_engaged(audit: AuditLog):
    """"I cannot say whether a kill is in force" must never resolve to "it isn't".

    And must not resolve to a park either: engaged is strictly weaker, keeps
    `/status` and the watchdog, and is the state the read was about to report.
    """

    async def boom(_fn):
        raise sqlite_error()

    def sqlite_error():
        import sqlite3

        return sqlite3.DatabaseError("database disk image is malformed")

    audit.read = boom  # type: ignore[method-assign]
    state = await load_kill_switch_state(audit)

    assert state.engaged is True
    assert state.reason and "unreadable" in state.reason
    assert state.source == RESTORE_FAILED_SOURCE
    assert state.restore_error


async def test_engaged_without_a_reason_is_refused_at_construction(audit: AuditLog):
    with pytest.raises(ValueError):
        KillSwitch(audit, initially_engaged=True, initial_reason=None)


# -- disengage ----------------------------------------------------------------


async def test_disengage_records_BEFORE_it_flips(audit: AuditLog):
    """The ordering rule, and the one mutation that matters.

    `engage` flips first because halting must not depend on a write. `disengage`
    is the UNSAFE direction: flipping first and failing the write would resume
    trading with nothing on the record authorizing it, and the next restart would
    re-halt. Writing first makes a failed clear a NO-OP.
    """
    ks = KillSwitch(audit)
    await ks.engage("manual halt", source=TRIP_SOURCE)

    async def fail(*_a, **_k):
        raise RuntimeError("disk full")

    audit.record = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await ks.disengage(source=TRIP_SOURCE)

    assert ks.engaged is True, "an unrecorded clear did not happen"
    assert ks.reason == "manual halt"


async def test_disengage_is_idempotent_and_writes_nothing_when_clear(audit: AuditLog):
    """A speculative resume row would sit in front of a FUTURE trip's restoration."""
    ks = KillSwitch(audit)
    assert await ks.disengage(source=TRIP_SOURCE) is False
    count = audit._conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE category = ?", (RESUME_CATEGORY,)
    ).fetchone()[0]
    assert count == 0


async def test_disengage_records_what_it_cleared_and_pages(audit: AuditLog):
    alerts: list = []
    ks = KillSwitch(audit, alert=lambda kind, detail: alerts.append((kind, detail)))
    await ks.engage("broker reconciliation mismatch", source="reconciliation_mismatch")
    assert await ks.disengage(source=TRIP_SOURCE, detail={"peer": "192.0.2.7"}) is True

    assert ks.engaged is False and ks.reason is None and ks.restored is False
    row = audit._conn.execute(
        "SELECT payload FROM audit_log WHERE category = ? ORDER BY seq DESC LIMIT 1",
        (RESUME_CATEGORY,),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["source"] == TRIP_SOURCE
    assert payload["cleared_source"] == "reconciliation_mismatch"
    assert payload["cleared_reason"] == "broker reconciliation mismatch"
    assert payload["detail"]["peer"] == "192.0.2.7"
    assert [k for k, _ in alerts] == ["kill_switch_engaged", "kill_switch_disengaged"]
    audit.verify_chain()


async def test_a_kill_after_a_resume_is_NOT_restored(audit: AuditLog):
    """`restored` describes the CURRENT engagement, so /status cannot claim an
    operator's fresh press was inherited from a previous process."""
    ks = KillSwitch(audit, initially_engaged=True, initial_reason="Friday's halt")
    assert ks.restored is True
    await ks.disengage(source=TRIP_SOURCE)
    await ks.engage("a new halt", source=TRIP_SOURCE)
    assert ks.engaged is True and ks.restored is False


# -- POST /resume, over a real socket ----------------------------------------


@contextlib.asynccontextmanager
async def resume_harness(tmp_path: Path, *, halt=None):
    """The `test_control.py` harness plus the halt seam `/resume` reports through.

    Real TCP into a real `ControlServer` and a real `KillSwitch`, for the reason
    `test_kill_scope.py` exists: `test_control.py` wires a bare switch with
    nothing behind it, so nothing had ever exercised the route end to end.
    """
    conn = connect(tmp_path / "argus.db")
    apply_migrations(conn)
    audit = AuditLog(conn, tier2_queue_maxsize=100, tier2_queue_alert_depth=80)
    audit.start()
    alerts: list = []
    sink = lambda kind, detail: alerts.append(kind)  # noqa: E731
    ks = KillSwitch(audit, alert=sink)
    server = ControlServer(
        host="127.0.0.1", port=0, token=TOKEN, killswitch=ks, audit=audit,
        alert=sink, halt_state=lambda: halt,
    )
    await server.start()
    host, port = server.bound_address
    try:
        yield SimpleNamespace(
            server=server, killswitch=ks, audit=audit, conn=conn,
            host=host, port=port, alerts=alerts,
        )
    finally:
        await server.stop()
        await audit.aclose()
        conn.close()


async def test_resume_clears_the_halt_and_records_it(tmp_path: Path):
    async with resume_harness(tmp_path) as h:
        await h.killswitch.engage("manual halt", source=TRIP_SOURCE)
        resp = await http_request(
            h.host, h.port, "POST", f"{RESUME_PATH}?confirm=yes", headers=_bearer(TOKEN)
        )

        assert resp.code == 200
        body = json.loads(resp.text)
        assert body["status"] == "resumed"
        assert body["restart_required"] is False
        assert h.killswitch.engaged is False
        row = h.conn.execute(
            "SELECT payload FROM audit_log WHERE category = ? ORDER BY seq DESC LIMIT 1",
            (RESUME_CATEGORY,),
        ).fetchone()
        assert row is not None, "200 means durably recorded"
        assert json.loads(row["payload"])["detail"]["peer"] == "127.0.0.1"
        assert "kill_switch_disengaged" in h.alerts


async def test_resume_WITHOUT_confirmation_is_refused(tmp_path: Path):
    """The only route that ENABLES risk is deliberately not one-tap."""
    async with resume_harness(tmp_path) as h:
        await h.killswitch.engage("manual halt", source=TRIP_SOURCE)
        for path in (RESUME_PATH, f"{RESUME_PATH}?confirm=", f"{RESUME_PATH}?confirm=no",
                     f"{RESUME_PATH}?reason=oops", f"{RESUME_PATH}?confirm=YES",
                     # Ambiguous, and an ambiguity must never resolve toward
                     # resuming trading.
                     f"{RESUME_PATH}?confirm=no&confirm=yes"):
            resp = await http_request(h.host, h.port, "POST", path, headers=_bearer(TOKEN))
            assert resp.code == 400, path
            assert h.killswitch.engaged is True, path
        count = h.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE category = ?", (RESUME_CATEGORY,)
        ).fetchone()[0]
        assert count == 0


async def test_resume_needs_the_token_and_never_logs_it(tmp_path: Path, caplog):
    async with resume_harness(tmp_path) as h:
        await h.killswitch.engage("manual halt", source=TRIP_SOURCE)
        for headers in ([], _bearer("wrong-token")):
            resp = await http_request(
                h.host, h.port, "POST", f"{RESUME_PATH}?confirm=yes", headers=headers
            )
            assert resp.code == 401
            assert h.killswitch.engaged is True

        assert "wrong-token" not in caplog.text
        assert "wrong-token" not in resp.text
        rows = h.conn.execute("SELECT payload FROM audit_log").fetchall()
        assert not any("wrong-token" in r["payload"] for r in rows)


async def test_resume_when_nothing_is_engaged_is_a_no_op(tmp_path: Path):
    async with resume_harness(tmp_path) as h:
        resp = await http_request(
            h.host, h.port, "POST", f"{RESUME_PATH}?confirm=yes", headers=_bearer(TOKEN)
        )
        assert resp.code == 200
        assert json.loads(resp.text)["status"] == "not_engaged"
        count = h.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE category = ?", (RESUME_CATEGORY,)
        ).fetchone()[0]
        assert count == 0, "no speculative resume row"


async def test_resume_rejects_the_wrong_method_and_neighbouring_paths(tmp_path: Path):
    async with resume_harness(tmp_path) as h:
        await h.killswitch.engage("manual halt", source=TRIP_SOURCE)
        resp = await http_request(
            h.host, h.port, "GET", f"{RESUME_PATH}?confirm=yes", headers=_bearer(TOKEN)
        )
        assert resp.code == 405 and resp.headers.get("allow") == "POST"
        for path in ("/resumex", "/resume/", "/resume/now"):
            resp = await http_request(h.host, h.port, "POST", path, headers=_bearer(TOKEN))
            assert resp.code == 404, path
        assert h.killswitch.engaged is True


async def test_the_query_split_did_NOT_widen_the_kill_route(tmp_path: Path):
    """`/kill?anything` has always been a 404 and still is.

    Adding a route that takes a query string must not relax the matching of the
    safety-critical one as a side effect.
    """
    async with resume_harness(tmp_path) as h:
        resp = await http_request(
            h.host, h.port, "POST", "/kill?confirm=yes", headers=_bearer(TOKEN)
        )
        assert resp.code == 404
        assert h.killswitch.engaged is False


async def test_a_failed_resume_write_leaves_the_halt_IN_FORCE(tmp_path: Path):
    """500 means the clear did not happen — not that it might have."""
    async with resume_harness(tmp_path) as h:
        await h.killswitch.engage("manual halt", source=TRIP_SOURCE)

        async def fail(*_a, **_k):
            from argus.audit.log import AuditWriteError

            raise AuditWriteError("chain is broken")

        h.audit.record = fail  # type: ignore[method-assign]
        resp = await http_request(
            h.host, h.port, "POST", f"{RESUME_PATH}?confirm=yes", headers=_bearer(TOKEN)
        )

        assert resp.code == 500
        assert json.loads(resp.text) == {"status": "engaged", "audit": "write_failed"}
        assert h.killswitch.engaged is True
        assert "kill_resume_unrecorded" in h.alerts


@pytest.mark.parametrize("grade", [HaltGrade.ALL, HaltGrade.ENTRIES])
async def test_resume_on_a_halted_process_clears_durably_and_says_restart(
    tmp_path: Path, grade: HaltGrade
):
    """Both grades: the durable clear always happens, and the response tells the
    truth about who can act on it.

    Refusing here was considered and rejected — clearing the durable state is the
    whole point of the route, and refusing would force an operator to SSH to reach
    something a phone should reach. The `HaltRecord` stays one-way.
    """
    halt = SimpleNamespace(reason="rail refused", source="reconciliation_mismatch",
                           grade=grade)
    async with resume_harness(tmp_path, halt=halt) as h:
        await h.killswitch.engage("manual halt", source=TRIP_SOURCE)
        resp = await http_request(
            h.host, h.port, "POST", f"{RESUME_PATH}?confirm=yes", headers=_bearer(TOKEN)
        )

        body = json.loads(resp.text)
        assert resp.code == 200
        assert body["restart_required"] is True
        assert body["halt_grade"] == str(grade)
        assert h.killswitch.engaged is False, "the durable state IS cleared"


async def test_resume_then_restart_comes_up_trading_after_a_park(tmp_path: Path):
    """The runbook, end to end: assess → /resume → restart, one restart."""
    cfg = _config(tmp_path)
    app = Application(cfg, session_body=_noop_body)
    await app.startup()
    try:
        await app._halt_process("audit log unwritable (disk)", source="audit_log")
        await app.killswitch.disengage(source=TRIP_SOURCE)
    finally:
        await app.shutdown()

    restarted = await _run_one_process(cfg)
    assert restarted.killswitch.engaged is False
    assert restarted._halted is None


# -- structural ---------------------------------------------------------------


def test_disengage_is_reachable_from_the_RESUME_HANDLER_and_nowhere_else():
    """Rehydration only ever RESTORES a halt. The automated halted→trading
    transition `2026-08-26-reconcile-broker-unreachable.md` refused is still not
    built, and this is what keeps it that way as the codebase grows."""
    callers = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "disengage":
                callers.append(path.relative_to(_SRC).as_posix())
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
                node.name == "disengage"
            ):
                callers.append(path.relative_to(_SRC).as_posix())

    assert set(callers) == {"control.py", "risk/killswitch.py"}, (
        f"`disengage` is referenced in {sorted(set(callers))} — the ONLY caller may "
        "be the /resume handler. A rail that clears a halt on its own is the "
        "automated halted→trading transition the design refuses."
    )


def test_the_kill_state_read_has_no_date_bound():
    """The breaker's read is day-bounded and this one must never be.

    A `ts` predicate here would make a Friday `/kill` expire at midnight — a
    per-day latch on a rail that has none, arriving by way of a tidy-up that made
    two similar-looking modules look alike.
    """
    source = (_SRC / "risk" / "kill_state.py").read_text()
    tree = ast.parse(source)
    queries = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "SELECT" in node.value
    ]
    assert queries, "the read is a SQL string literal; find it before asserting on it"
    for q in queries:
        assert "ts >" not in q and "ts <" not in q and "ts BETWEEN" not in q, q
        assert "ORDER BY seq DESC LIMIT 1" in q, (
            "each lookup must be the newest row of one category and nothing more"
        )
    assert "et_day_bounds_utc_iso" not in source


def test_the_state_object_reports_a_clear_read_and_a_degraded_one_differently():
    """`restore_error` is what separates "a human halted this" from "the recorder
    could not say, so we assumed the worst" — both engage, and only one is a
    fault to chase."""
    clean = KillSwitchState(engaged=True, reason="manual halt", source=TRIP_SOURCE)
    assert "restore_error" not in clean.to_payload()
    degraded = KillSwitchState(engaged=True, reason="?", restore_error="boom")
    assert degraded.to_payload()["restore_error"] == "boom"


# -- /status ------------------------------------------------------------------


async def test_status_reports_a_restored_kill_instead_of_rendering_green(
    tmp_path: Path,
):
    """The misreport this closes: after a restart-while-halted the page showed
    `armed (not engaged)` with a null source — the reassuring reading at the one
    moment it is wrong."""
    cfg = _config(tmp_path)
    await _kill_then(cfg)

    app = Application(cfg, session_body=_noop_body)
    await app.startup()
    try:
        snap = await app.status_service.assemble()
    finally:
        await app.shutdown()

    kill = snap.to_json_dict()["kill_switch"]
    assert kill["engaged"] is True
    assert kill["source"] == TRIP_SOURCE
    assert kill["since"] is not None, "the ORIGINAL press, read off the trip row"
    assert kill["restored"] is True
    html = snap.to_html()
    assert "ENGAGED (RESTORED)" in html
    assert "POST /resume clears it" in html


def test_the_status_service_reads_restored_off_the_object_not_a_row():
    """Same rule the breaker and `killswitch_since` are already governed by: a row
    outlives the process that wrote it; live state comes from the object."""
    source = (_SRC / "status.py").read_text()
    assert "killswitch_restored=ks.restored" in source
    # And the field never became a second DB read: nothing named `restored`
    # appears anywhere between the DB reader and the service that calls it.
    db_read = source.split("def _read_status_db")[1].split("class StatusService")[0]
    assert "restored" not in db_read


def test_status_service_still_gates_the_trip_row_on_the_live_switch():
    """The gate is now CORRECT, not obsolete.

    The defect was never this line — it was that `ks.engaged` could be false while
    a halt was in force. Removing the gate would introduce the mirror bug: a
    RESUMED trip rendering as a live one.
    """
    source = (_SRC / "status.py").read_text()
    assert "trip = db.kill_trip if ks.engaged else None" in source
