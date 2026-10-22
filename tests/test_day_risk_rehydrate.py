"""Same-day restart must not re-arm the daily-loss breaker.

The scenario these pin, and the reason they exist as a restart SIMULATION rather
than as an observation: the breaker trips at 11:20, the process restarts at 11:40
(crash, deploy, `systemctl restart`), and before 2026-08-20 the fresh process came
up untripped with day P&L re-anchored to the post-loss NLV — so the day's loss
budget silently reset and the breaker could not even re-trip on the loss already
taken. Cert cannot surface any of this: day P&L is derived from NLV, cert never
marks NLV, so `daily_pnl` is identically 0.0 there and the breaker never trips at
all. A sandbox session that restarts and looks fine is not evidence.

The episode boundary matters in BOTH directions and each direction has its own
test here: within a trading day the latch must survive a restart; across the day
rollover it must not be inherited.

And what the desk SEES has to match what the gate does. `/status` derived
`breaker_tripped` from "was a breach logged today?", so the restart above rendered
a red TRIPPED off the surviving row while the gate authorized entries — the page
reassuring an operator at the one moment it should not. It now reads the live
breaker object, trading-date-scoped; the pairing is tested here rather than in
`test_status.py` because the divergence only exists across a restart.

→ docs/decisions/2026-08-20-daily-loss-breaker-restart-rehydration.md
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from support import fresh_loss_budget, loss_budget_source
from argus.account.fake import FakeAccountState
from argus.app import Application
from argus.audit.log import AuditLog
from argus.clock import ET, et_day_bounds_utc_iso, et_today
from argus.config import Config
from argus.marketdata import FakeMarketData
from argus.risk.breaker import BREACH_CATEGORY, DailyLossBreaker
from argus.risk.models import EntryProposal, RiskSnapshot
from argus.risk.rehydrate import (
    ANCHOR_CATEGORY,
    RESTORED_CATEGORY,
    DayRiskRehydrationError,
    DayRiskState,
    _anchor_from,
    load_day_risk_state,
    record_day_pnl_anchor,
)
from argus.risk.snapshot import DailyPnl, RiskSnapshotBuilder
from support import make_config

# The day's opening NLV, and a loss that clears the 1000.0 `daily_max_loss` in
# `support.CONFIG` with room to spare.
_OPEN_NLV = 100_000.0
_LOSS = -1500.0


def _config(tmp_path: Path, **overrides) -> Config:
    audit = {**overrides.pop("audit", {}), "db_path": str(tmp_path / "argus.db")}
    return make_config(audit=audit, **overrides)


def _session_body(*, start_nlv: float = _OPEN_NLV, loss: float | None = None, captured: list):
    """A fake body that does the two things the real one does to day-risk state.

    Deliberately routed through `RiskSnapshotBuilder.anchor_day_pnl_if_possible`
    and `record_day_pnl_anchor` — the same calls `session.run_session` makes — so
    these tests exercise the real anchoring path (including its already-anchored
    guard) rather than a hand-set anchor that could agree with a broken one.

    Each run appends a record of the state AS HANDED IN — before the body touches
    it — because that is what the rehydration produced. Reading the flags after
    the body has evaluated the breaker would pass on the strength of the
    re-evaluation and prove nothing about the latch.
    """

    async def body(*, config, audit, state, stop, trading_date, alert=None, account_number=None):
        captured.append(
            {
                "state": state,
                "tripped_at_entry": state.breaker.tripped,
                "anchor_at_entry": state.day_pnl.anchor,
            }
        )
        market = FakeMarketData()
        market.push("SPX", 5000.0, 5000.5)
        account = FakeAccountState()
        account.set_net_liq(start_nlv)
        builder = RiskSnapshotBuilder(
            market, account, state.day_pnl, reference_symbols=("SPX",), loss_budget=loss_budget_source()
        )
        if builder.anchor_day_pnl_if_possible():
            await record_day_pnl_anchor(
                audit, trading_date=trading_date, anchor_nlv=state.day_pnl.anchor
            )
        if loss is not None:
            # The day moves against us AFTER the anchor is set, which is the only
            # ordering that produces a real day P&L.
            account.set_net_liq(start_nlv + loss)
        # Feed the breaker whatever day P&L the (possibly rehydrated) anchor
        # produces — exactly what the gate does on every proposal.
        await state.breaker.evaluate(builder.build().daily_pnl)

    return body


async def _run_one_session(cfg: Config, body, trading_date: date):
    """Start a process, run ONE session for `trading_date`, shut it down again.

    Each call is a distinct process lifetime against the same database file — the
    writer lock is released on shutdown, so calling it twice IS the restart.
    """
    app = Application(cfg, session_body=body)
    await app.startup()
    try:
        await app._run_session_day(
            trading_date, app._now_et() + timedelta(hours=1), asyncio.Event()
        )
        return app
    finally:
        await app.shutdown()


# -- the restart scenario -------------------------------------------------


async def test_a_same_day_restart_comes_up_STILL_TRIPPED(tmp_path: Path):
    """Trip, restart, and the day's budget is still spent.

    This is the test the fix exists for. Without the rehydration the second
    session's breaker is untripped AND its anchor is the post-loss NLV, so
    `daily_pnl` reads ~0 and it cannot even re-trip on the loss already taken.
    """
    cfg = _config(tmp_path)
    today = et_today()

    first: list = []
    await _run_one_session(cfg, _session_body(loss=_LOSS, captured=first), today)
    assert first[0]["tripped_at_entry"] is False, "precondition: the day started clean"
    assert first[0]["state"].breaker.tripped is True, "precondition: the day tripped"
    assert first[0]["state"].day_pnl.anchor == _OPEN_NLV

    # --- restart, same trading day. The account really is down by now, so a
    # process that re-anchored would take the POST-loss NLV as its baseline. ---
    second: list = []
    await _run_one_session(
        cfg, _session_body(start_nlv=_OPEN_NLV + _LOSS, captured=second), today
    )

    assert second[0]["tripped_at_entry"] is True, (
        "the latch survived the restart — tripped as CONSTRUCTED, before anything "
        "re-evaluated it"
    )
    assert second[0]["anchor_at_entry"] == _OPEN_NLV, (
        "and the anchor is still the day's OPENING NLV, not the post-loss one"
    )
    assert second[0]["state"].day_pnl.value(_OPEN_NLV + _LOSS) == _LOSS, (
        "so the day's loss is still measured in full"
    )


async def test_the_restored_trip_actually_denies_at_the_gate(tmp_path: Path):
    """Rehydrated state has to reach the rail, not just the object.

    The breaker is only meaningful through `PreTradeGate`, so the restart is
    proven at the decision, not at the flag.
    """
    cfg = _config(tmp_path)
    today = et_today()
    await _run_one_session(cfg, _session_body(loss=_LOSS, captured=[]), today)

    decisions: list = []

    async def gate_body(*, state, **_kwargs):
        decisions.append(
            await state.gate.evaluate(
                EntryProposal(symbol="SPXW", contracts=1, max_loss=425.0),
                RiskSnapshot(
                    open_position_count=0,
                    # A snapshot that would otherwise pass every rail, INCLUDING a
                    # healthy day P&L: only the restored latch can deny this.
                    daily_pnl=0.0,
                    account_nlv=_OPEN_NLV,
                    market_data_age_s=0.5,
                    account_stream_age_s=1.0, loss_budget=fresh_loss_budget(),
                ),
            )
        )

    await _run_one_session(cfg, gate_body, today)
    assert decisions[0].allowed is False
    assert "daily_loss_breaker_tripped" in decisions[0].reasons


async def test_a_restart_BEFORE_any_trip_is_untripped_and_correctly_anchored(
    tmp_path: Path,
):
    """The fix must not make every restart look tripped.

    A restart on a quiet day restores the anchor and nothing else — the day is
    still tradeable, which is the property a too-eager "assume tripped" would
    destroy.
    """
    cfg = _config(tmp_path)
    today = et_today()

    first: list = []
    await _run_one_session(cfg, _session_body(captured=first), today)
    assert first[0]["state"].breaker.tripped is False

    second: list = []
    await _run_one_session(cfg, _session_body(captured=second), today)
    assert second[0]["tripped_at_entry"] is False, "a quiet restart is still tradeable"
    assert second[0]["anchor_at_entry"] == _OPEN_NLV, "and it still knows the baseline"


async def test_a_NEW_trading_day_does_not_inherit_the_trip(tmp_path: Path):
    """The episode boundary. A daily budget that never re-arms is a broken rail.

    Same database, same process code, one day later: the day-bounded read finds
    neither yesterday's breach nor yesterday's anchor, so the new day starts fresh
    and un-anchored — the day-rollover contract, unchanged.
    """
    cfg = _config(tmp_path)
    today = et_today()
    await _run_one_session(cfg, _session_body(loss=_LOSS, captured=[]), today)

    tomorrow: list = []
    app = Application(cfg, session_body=_session_body(captured=tomorrow))
    await app.startup()
    try:
        await app._run_session_day(
            today + timedelta(days=1), app._now_et() + timedelta(hours=1), asyncio.Event()
        )
    finally:
        await app.shutdown()

    assert tomorrow[0]["tripped_at_entry"] is False, "the day re-armed"
    assert tomorrow[0]["anchor_at_entry"] is None, "and re-anchors from scratch"


async def test_a_session_for_a_PAST_date_does_not_see_todays_state(tmp_path: Path):
    """The upper bound of the day range, pinned in the other direction.

    Without `ts < day_end` a query keyed to an earlier trading date would sweep up
    every later day's rows — the same leak as the lower bound, mirrored.
    """
    cfg = _config(tmp_path)
    await _run_one_session(cfg, _session_body(loss=_LOSS, captured=[]), et_today())

    app = Application(cfg, session_body=_session_body(captured=[]))
    await app.startup()
    try:
        state = await load_day_risk_state(
            app.audit, trading_date=et_today() - timedelta(days=1)
        )
    finally:
        await app.shutdown()
    assert state == DayRiskState(breaker_tripped=False, anchor_nlv=None)


async def test_repeated_restarts_all_rehydrate_to_the_FIRST_anchor(tmp_path: Path):
    """Three sessions, one anchor. The baseline must not ratchet.

    If a later `day_pnl.anchored` row could win, each restart would re-anchor
    closer to the current NLV and the day's measured loss would shrink toward zero
    one restart at a time — the original bug, just slower.
    """
    cfg = _config(tmp_path)
    today = et_today()
    for run in range(3):
        captured: list = []
        await _run_one_session(
            cfg,
            # Each restart sees a further-depressed account, so a re-anchoring bug
            # would show as a DIFFERENT baseline on each pass.
            _session_body(start_nlv=_OPEN_NLV + run * _LOSS, loss=_LOSS, captured=captured),
            today,
        )
        assert captured[0]["state"].day_pnl.anchor == _OPEN_NLV

    app = Application(cfg, session_body=_session_body(captured=[]))
    await app.startup()
    try:
        rows = app.audit._conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE category = ?",
            (ANCHOR_CATEGORY,),
        ).fetchone()["n"]
    finally:
        await app.shutdown()
    assert rows == 1, "the anchor is recorded ONCE for the day, by the first session"


async def test_the_restart_is_recorded_and_alerted(tmp_path: Path):
    """A restart into a spent budget is otherwise invisible in the trail.

    The breaker writes no second `daily_loss.breach` (the latch is idempotent) and
    the anchor is not re-recorded, so without `day_risk.restored` an 11:40 restart
    reads exactly like a process that ran since the open.
    """
    cfg = _config(tmp_path)
    today = et_today()
    await _run_one_session(cfg, _session_body(loss=_LOSS, captured=[]), today)

    alerts: list = []
    app = Application(cfg, session_body=_session_body(loss=_LOSS, captured=[]))
    await app.startup()
    app._alert = lambda kind, detail: alerts.append((kind, detail))
    try:
        await app._run_session_day(
            today, app._now_et() + timedelta(hours=1), asyncio.Event()
        )
        row = app.audit._conn.execute(
            "SELECT tier, payload FROM audit_log WHERE category = ? ORDER BY seq DESC LIMIT 1",
            (RESTORED_CATEGORY,),
        ).fetchone()
        assert row is not None and row["tier"] == 1
        app.audit.verify_chain()
    finally:
        await app.shutdown()

    assert [k for k, _ in alerts] == ["daily_loss_breaker_restored"]


async def test_an_anchor_only_restart_records_but_does_not_page(tmp_path: Path):
    """A quiet restart is bookkeeping, not an incident.

    Alerting on every same-day restart would train the desk to ignore the one
    alert that matters — the restored TRIP.
    """
    cfg = _config(tmp_path)
    today = et_today()
    await _run_one_session(cfg, _session_body(captured=[]), today)

    alerts: list = []
    app = Application(cfg, session_body=_session_body(captured=[]))
    await app.startup()
    app._alert = lambda kind, detail: alerts.append((kind, detail))
    try:
        await app._run_session_day(
            today, app._now_et() + timedelta(hours=1), asyncio.Event()
        )
        assert (
            app.audit._conn.execute(
                "SELECT 1 FROM audit_log WHERE category = ? LIMIT 1", (RESTORED_CATEGORY,)
            ).fetchone()
            is not None
        )
    finally:
        await app.shutdown()
    assert alerts == []


async def test_the_days_FIRST_session_restores_nothing_and_stays_quiet(tmp_path: Path):
    """No record, no alert, nothing to restore — the common path is unchanged."""
    cfg = _config(tmp_path)
    alerts: list = []
    app = Application(cfg, session_body=_session_body(captured=[]))
    await app.startup()
    app._alert = lambda kind, detail: alerts.append((kind, detail))
    try:
        await app._run_session_day(
            et_today(), app._now_et() + timedelta(hours=1), asyncio.Event()
        )
        assert (
            app.audit._conn.execute(
                "SELECT 1 FROM audit_log WHERE category = ? LIMIT 1", (RESTORED_CATEGORY,)
            ).fetchone()
            is None
        )
    finally:
        await app.shutdown()
    assert alerts == []


# -- what the operator actually sees --------------------------------------


async def test_status_reports_the_LIVE_breaker_and_agrees_with_the_gate(tmp_path: Path):
    """The page and the rail, across the same restart, must say the same thing.

    Paired with the rehydration above, and the reason both landed together:
    `/status` derived `breaker_tripped` from "was a `daily_loss.breach` logged
    today?" until 2026-08-20, so the restart that came up UNTRIPPED also rendered a
    red TRIPPED off the surviving row. The page announced a halt at precisely the
    moment the process was still authorizing entries — the worst direction for that
    error to point, and the one an operator glancing at a phone would trust.

    Nothing in the second session's body touches the breaker, so the True here can
    only have come from the rehydrated latch being published to the surface.
    """
    cfg = _config(tmp_path)
    today = et_today()
    await _run_one_session(cfg, _session_body(loss=_LOSS, captured=[]), today)

    seen: list = []

    async def peek(**_kwargs):
        # Read the surface from INSIDE the session — when an operator actually
        # looks at it, not after everything has been torn down.
        seen.append(await app.status_service.assemble())

    app = Application(cfg, session_body=peek)
    await app.startup()
    try:
        before = await app.status_service.assemble()
        await app._run_session_day(
            today, app._now_et() + timedelta(hours=1), asyncio.Event()
        )
        rail = app._breaker_for_today()
        assert rail is not None and rail.tripped is True, (
            "precondition: the day came up rehydrated-tripped"
        )
        # The assertion this test exists for: not "both happen to be True" but
        # "the page reports the object the gate holds".
        assert seen[0].breaker_tripped is rail.tripped
    finally:
        await app.shutdown()

    assert before.breaker_tripped is None, (
        "before the day's breaker exists the page claims nothing — not 'not tripped'"
    )
    assert seen[0].breaker_tripped is True


async def test_status_does_not_report_YESTERDAYS_breaker(tmp_path: Path):
    """The rollover from the page's side: a spent budget must not haunt tomorrow.

    `_day_breaker` holds the LAST breaker built and nothing rebuilds it between the
    close and the next session, so an unscoped read would render Tuesday's TRIPPED
    all through Wednesday's pre-open. Same misreport, pointing the other way — and
    the reason `_breaker_for_today` is trading-date-scoped rather than a bare
    reference. The latch is per trading date; so is the report of it.
    """
    cfg = _config(tmp_path)
    today = et_today()
    day = [today]
    app = Application(
        cfg,
        session_body=_session_body(loss=_LOSS, captured=[]),
        # A clock the test owns outright, so the rollover is a date change and not
        # a timedelta that lands differently across a DST boundary.
        now_et=lambda: datetime.combine(day[0], time(12, 0), tzinfo=ET),
    )
    await app.startup()
    try:
        await app._run_session_day(
            today, app._now_et() + timedelta(hours=1), asyncio.Event()
        )
        same_day = await app.status_service.assemble()
        day[0] = today + timedelta(days=1)  # the clock rolls past ET midnight
        next_day = await app.status_service.assemble()
        assert app._breaker_for_today() is None
    finally:
        await app.shutdown()

    assert same_day.breaker_tripped is True, "it reports the trip for the day that tripped"
    assert next_day.breaker_tripped is None, "and stops the moment that day is over"


# -- fail closed ----------------------------------------------------------


async def test_a_rehydration_failure_HALTS_rather_than_trading_untripped(
    tmp_path: Path, monkeypatch
):
    """"Cannot say whether the budget is spent" must never resolve to "it isn't".

    The read shares the audit log's own connection, so a failure here means the
    recorder is compromised too — already a halting condition everywhere else.
    """
    import argus.app as app_module

    async def _boom(*_args, **_kwargs):
        raise DayRiskRehydrationError("simulated unreadable audit log")

    monkeypatch.setattr(app_module, "load_day_risk_state", _boom)

    ran: list = []

    async def body(**kwargs):
        ran.append(kwargs["trading_date"])

    app = Application(_config(tmp_path), session_body=body)
    await app.startup()
    try:
        await app._run_session_day(
            et_today(), app._now_et() + timedelta(hours=1), asyncio.Event()
        )
        assert ran == [], "the session never started"
        assert app._halted is not None
        assert app._halted.source == "day_risk_rehydrate"
        assert app.killswitch.engaged is True
        # And it PARKS rather than exits: the surface stays reachable (CLAUDE.md).
        assert app._control is not None and app._control.bound_address is not None
    finally:
        await app.shutdown()


async def test_an_unreadable_anchor_record_refuses_rather_than_re_anchoring(
    audit: AuditLog,
):
    """A row that exists but cannot be read is NOT the same as no row.

    Falling back to the fresh-start path there would re-anchor to a post-loss NLV
    — the exact bug — so the parse refuses and the supervisor halts.
    """
    # A structurally valid Tier 1 record whose payload is missing the value.
    await audit.record(ANCHOR_CATEGORY, {"trading_date": et_today().isoformat()})
    with pytest.raises(DayRiskRehydrationError):
        await load_day_risk_state(audit, trading_date=et_today())


def test_a_non_finite_anchor_is_refused():
    # `AuditLog.record` rejects non-finite JSON on the way in (`allow_nan=False`),
    # so this branch is only reachable from a row written by something else. The
    # parser is checked directly rather than by forging a row into the chain.
    with pytest.raises(DayRiskRehydrationError):
        _anchor_from('{"anchor_nlv": Infinity}')
    assert _anchor_from('{"anchor_nlv": 100000.0}') == 100_000.0


# -- the reader in isolation ----------------------------------------------


async def test_load_finds_nothing_on_an_empty_log(audit: AuditLog):
    state = await load_day_risk_state(audit, trading_date=et_today())
    assert state == DayRiskState(breaker_tripped=False, anchor_nlv=None)
    assert state.restored is False


async def test_load_reads_back_a_breach_and_an_anchor(audit: AuditLog):
    await record_day_pnl_anchor(audit, trading_date=et_today(), anchor_nlv=_OPEN_NLV)
    await DailyLossBreaker(audit, daily_max_loss=1000.0).evaluate(_LOSS)

    state = await load_day_risk_state(audit, trading_date=et_today())
    assert state == DayRiskState(breaker_tripped=True, anchor_nlv=_OPEN_NLV)
    assert state.restored is True
    # DailyPnl reconstructed from it measures the day's loss in full.
    assert DailyPnl(anchor_nlv=state.anchor_nlv).value(_OPEN_NLV + _LOSS) == _LOSS


async def test_the_FIRST_anchor_of_the_day_wins_over_a_later_one(audit: AuditLog):
    """Defence in depth, and the reason `ORDER BY seq ASC` is not decoration.

    Today the already-anchored guard means exactly one `day_pnl.anchored` row is
    ever written per day, so the ordering is unobservable in normal operation —
    which is precisely why it needs a test. A second row can arrive from a
    database written before this fix existed, or from any future path that anchors
    again; taking the LATEST would re-anchor to a post-loss NLV and reopen the bug
    silently, with every other test still green.
    """
    today = et_today()
    await record_day_pnl_anchor(audit, trading_date=today, anchor_nlv=_OPEN_NLV)
    await record_day_pnl_anchor(audit, trading_date=today, anchor_nlv=_OPEN_NLV + _LOSS)

    state = await load_day_risk_state(audit, trading_date=today)
    assert state.anchor_nlv == _OPEN_NLV, "the day's opening reference, not the later one"


async def test_the_breach_category_read_is_the_one_the_breaker_writes(audit: AuditLog):
    """The literal is shared, not re-spelled — a rename must not silently unwire it."""
    await DailyLossBreaker(audit, daily_max_loss=1000.0).evaluate(_LOSS)
    row = audit._conn.execute(
        "SELECT category FROM audit_log ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert row["category"] == BREACH_CATEGORY


def test_the_day_bounds_are_half_open_and_adjacent():
    day = date(2026, 8, 20)
    start, end = et_day_bounds_utc_iso(day)
    assert start == "2026-08-20T04:00:00.000000Z"  # ET is UTC-4 in August
    assert end == "2026-08-21T04:00:00.000000Z"
    # Adjacent days tile without gap or overlap, which is what makes the trading
    # date an exact partition of the audit trail.
    assert et_day_bounds_utc_iso(day - timedelta(days=1))[1] == start
