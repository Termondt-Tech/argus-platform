"""A broker that could not be ASKED, as distinct from one that answered and disagreed.

The 2026-08-26 incident: cert 502'd on `fetch_positions` at process start, the raw
`TastytradeError` fell to `startup()`'s generic handler, and the process exited into
Docker's blind restart — five `process.start` records in 33 seconds with no
`/status`, no `/kill` and no halt reason recorded anywhere. A `ReadTimeout` at the
per-day site the same evening was filed as `session.error`, the "reaching here means
a genuine bug" path, for something that is not a bug.

Two tests here carry more weight than the rest and should be read first:

- `test_a_real_mismatch_is_never_retried` — the invariant. A mismatch is a VALUE,
  so it must cost exactly ONE broker call however generous the retry budget is.
- `test_a_bug_in_the_compare_is_not_retried` — the other half. Only the port await
  is inside the retry's scope, so a defect in the comparison cannot be re-run.

No real sleeps anywhere: `argus.reconcile.asyncio.sleep` is monkeypatched, matching
`tests/test_shadow_tool.py`'s `_fetch_accounts` tests.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from argus.account.fake import FakeBrokerAccount
from argus.account.models import Position
from argus.app import Application
from argus.audit.log import AuditWriteError
from argus.clock import ET
from argus.db import apply_migrations, connect
from argus.reconcile import (
    NO_RETRY,
    BrokerUnreachable,
    InMemoryPositionStore,
    ReconcileRetry,
    Reconciler,
    reconcile_with_retry,
)
from support import make_config

#: One outer-retry interval, in seconds. Small enough that the Application tests
#: can serve it for real (they cannot fake `asyncio.sleep` — see `no_real_sleeps`).
_TICK = 0.01

TODAY = date(2026, 8, 26)
A_POSITION = Position("SPXW260826P05000000", "SPX", "Equity Option", -1, 1.20, 0.9)


class _Unreachable(Exception):
    """Stands in for a cert 502 / ReadTimeout — anything the fetch itself raises."""


class _FlakyBroker(FakeBrokerAccount):
    """Fails `fetch_positions` the first `fail_times` calls, then behaves normally."""

    def __init__(self, positions=None, *, fail_times: int = 0) -> None:
        super().__init__(positions)
        self.fail_times = fail_times
        self.calls = 0

    async def fetch_positions(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _Unreachable(f"502 Bad Gateway (call {self.calls})")
        return await super().fetch_positions()


def _migrated(cfg):
    """`_reconcile_or_abort` opens its own connection to the positions table.

    The Application tests get migrations from `startup()`; the per-day ones call the
    function directly, so they have to create the schema themselves.
    """
    conn = connect(cfg.audit.db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return cfg


def _config(tmp_path: Path, **overrides):
    audit = {**overrides.pop("audit", {}), "db_path": str(tmp_path / "argus.db")}
    heartbeat = {
        "interval_s": 0.01,
        "lag_alert_threshold_s": 0.02,
        "sustained_breach_count": 2,
        **overrides.pop("heartbeat", {}),
    }
    # Three outer attempts' worth, in units small enough to serve for real:
    # 1 + 1 <= 2.5 < 1 + 1 + 1, scaled by _TICK.
    reconcile = {
        "request_timeout_s": 10.0,
        "retry_interval_s": _TICK,
        "retry_budget_s": 2.5 * _TICK,
        **overrides.pop("reconcile", {}),
    }
    return make_config(audit=audit, heartbeat=heartbeat, reconcile=reconcile, **overrides)


class _FakeSleeps(list):
    """Every backoff, recorded rather than served — and a monotonic clock that moves
    with them, so the outer loop's BUDGET depletes in fake time too.

    Those two have to be the same fixture. A fake sleep with a real clock is a loop
    that never exhausts its budget, which in a test means spinning until something
    else notices; the budget and the sleeps must run off one notion of time.
    """

    elapsed = 0.0

    def now(self) -> float:
        return self.elapsed


@pytest.fixture
def no_real_sleeps(monkeypatch):
    """Fake time for both loops.

    **NOT autouse, and never used by a test that starts an `Application`.** Patching
    `argus.reconcile.asyncio.sleep` rebinds the attribute on the asyncio module
    itself, so it is process-wide for the test — which is fine for a bare
    `Reconciler` and fatal for a running supervisor, whose 10ms heartbeat becomes a
    tight loop that starves the event loop. Those tests use `fast_retry` instead.
    """
    slept = _FakeSleeps()

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        slept.elapsed += seconds

    monkeypatch.setattr("argus.reconcile.asyncio.sleep", fake_sleep)
    return slept


@pytest.fixture
def fast_retry(monkeypatch):
    """Collapse the inner backoff without touching `asyncio.sleep` globally.

    The outer interval is already tiny in `_config`; the inner one is a module
    function precisely so it can be replaced here on its own, leaving every other
    sleeper in the process — the heartbeat above all — running normally.
    """
    monkeypatch.setattr("argus.reconcile._inner_backoff_s", lambda _attempt: 0.0)


def _fake_time_config(tmp_path: Path, **overrides):
    """Production-shaped outer numbers, for the tests where time itself is fake.

    The `_TICK`-scaled defaults exist so the Application tests can serve their waits
    for real. Once `no_real_sleeps` is in play the clock is fake and the inner
    backoff (2s then 4s) is charged to it, which would swallow a 0.025s budget whole
    — so these tests use numbers of the same order as `argus.toml`'s and read like
    the thing they are pinning. 60s interval / 150s budget = three outer attempts.
    """
    reconcile = {"retry_interval_s": 60.0, "retry_budget_s": 150.0,
                 **overrides.pop("reconcile", {})}
    return _config(tmp_path, reconcile=reconcile, **overrides)


def _inputs(today: date = TODAY, settled: bool = False):
    return lambda: (today, settled)


def _cats(db_path: str) -> list[str]:
    conn = connect(db_path)
    try:
        return [r["category"] for r in conn.execute("SELECT category FROM audit_log ORDER BY seq")]
    finally:
        conn.close()


def _payload(db_path: str, category: str) -> dict:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload FROM audit_log WHERE category = ? ORDER BY seq LIMIT 1",
            (category,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no {category} record was written"
    return json.loads(row["payload"])


# -- the invariant: a retry can never reach a mismatch ------------------------


async def test_a_real_mismatch_is_never_retried(tmp_path: Path, fast_retry):
    """THE test. The broker answered; it just disagreed. That costs ONE call.

    A mismatch is a `ReconciliationResult` with ok=False — a VALUE — so it exits the
    retry on the first pass having spent no attempts. If this ever fails, the retry
    has started treating "the broker disagrees" as "the broker did not answer", which
    is the one thing the whole design forbids.
    """
    broker = _FlakyBroker([A_POSITION])  # never fails; disagrees with an empty store
    app = Application(
        _config(tmp_path, environment="sandbox"),
        reconciler=Reconciler(broker, InMemoryPositionStore()),
    )
    await app.startup()
    try:
        assert broker.calls == 1, "a mismatch must cost exactly one broker call"
        assert app._halted is not None, "and still refuse, exactly as before"
        assert app._halted.source == "reconciliation_mismatch"
    finally:
        await app.shutdown()
    cats = _cats(app._config.audit.db_path)
    assert "reconcile.result" in cats
    assert "reconcile.unreachable" not in cats, "not an unreachable broker"
    assert "reconcile.unreachable_attempt" not in cats


async def test_a_bug_in_the_compare_is_not_retried(tmp_path: Path, no_real_sleeps):
    """The retry's scope is drawn by POSITION in the call graph, not by exception type.

    Only the port await is inside the `try`, so a defect in the expected-position
    read cannot be re-run — it is not in scope. Retrying a bug would turn one clear
    traceback into three and then mislabel it as an unreachable broker.
    """
    class _BrokenStore(InMemoryPositionStore):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def open_positions(self):
            self.calls += 1
            raise KeyError("a genuine defect in the comparison")

    store = _BrokenStore()
    reconciler = Reconciler(FakeBrokerAccount([]), store)
    with pytest.raises(KeyError):
        await reconciler.reconcile(retry=ReconcileRetry(attempts=3))
    assert store.calls == 1
    assert no_real_sleeps == [], "and nothing backed off"


async def test_an_audit_write_failure_inside_a_port_call_is_not_retried(no_real_sleeps):
    """The one exception type that IS load-bearing, and why.

    `AuditedExecution.get_live_orders` writes a Tier 1 record inside the port call,
    so an `AuditWriteError` can surface from what looks like a pure read. A dead
    recorder halts the process and must never be re-attempted (CLAUDE.md: no
    auto-retry). It has to escape the retry untouched.
    """
    calls = {"n": 0}

    class _DeadRecorder(FakeBrokerAccount):
        async def fetch_positions(self):
            calls["n"] += 1
            raise AuditWriteError("seq collision")

    reconciler = Reconciler(_DeadRecorder([]), InMemoryPositionStore())
    with pytest.raises(AuditWriteError):
        await reconciler.reconcile(retry=ReconcileRetry(attempts=3))
    assert calls["n"] == 1, "the recorder is dead process-wide; asking again is forbidden"
    assert no_real_sleeps == []


# -- the inner retry ----------------------------------------------------------


async def test_a_transient_failure_inside_the_budget_never_surfaces(no_real_sleeps):
    broker = _FlakyBroker([], fail_times=2)
    result = await Reconciler(broker, InMemoryPositionStore()).reconcile(
        retry=ReconcileRetry(attempts=3)
    )
    assert result.ok is True
    assert broker.calls == 3
    assert no_real_sleeps == [2.0, 4.0], "the `_fetch_accounts` cadence, reused"


async def test_exhausted_inner_attempts_raise_broker_unreachable(no_real_sleeps):
    broker = _FlakyBroker([], fail_times=99)
    with pytest.raises(BrokerUnreachable) as exc:
        await Reconciler(broker, InMemoryPositionStore()).reconcile(
            retry=ReconcileRetry(attempts=3)
        )
    assert broker.calls == 3
    assert exc.value.leg == "positions"
    assert exc.value.attempts == 3
    assert "502" in str(exc.value), "the real cause is named, not swallowed"


async def test_a_bare_reconciler_is_unchanged(no_real_sleeps):
    """Tools and tests that never asked for a retry must not have acquired one."""
    broker = _FlakyBroker([], fail_times=1)
    with pytest.raises(BrokerUnreachable):
        await Reconciler(broker, InMemoryPositionStore()).reconcile()
    assert broker.calls == 1
    assert no_real_sleeps == []
    assert NO_RETRY.attempts == 1 and NO_RETRY.request_timeout_s is None


# -- the outer loop -----------------------------------------------------------


class _Audit:
    """Enough AuditLog for `reconcile_with_retry`: records, telemetry, no chain."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []
        self.telemetry_records: list[tuple[str, dict]] = []

    async def record(self, category: str, payload: dict) -> None:
        self.records.append((category, payload))

    def telemetry(self, category: str, payload: dict) -> None:
        self.telemetry_records.append((category, payload))

    async def read(self, fn):
        return None


async def test_the_outer_loop_recovers_and_says_nothing_terminal(no_real_sleeps, tmp_path):
    # Fails the first outer attempt's three inner attempts, succeeds on the next.
    broker = _FlakyBroker([], fail_times=3)
    audit = _Audit()
    alerts: list[str] = []
    result = await reconcile_with_retry(
        Reconciler(broker, InMemoryPositionStore()),
        config=_fake_time_config(tmp_path),
        audit=audit,
        alert=lambda kind, _d: alerts.append(kind),
        inputs=_inputs(),
        now=no_real_sleeps.now,
    )
    assert result.ok is True
    assert broker.calls == 4
    assert [c for c, _ in audit.records] == ["reconcile.result"]
    assert alerts == [], "a recovered blip pages nobody"
    # Per-attempt visibility is Tier 2, and it is there.
    assert [c for c, _ in audit.telemetry_records] == ["reconcile.unreachable_attempt"] * 3


async def test_the_outer_loop_gives_up_once_and_only_once(no_real_sleeps, tmp_path):
    """One Tier 1 record and one alert per EPISODE — never per attempt.

    30 attempts over 30 minutes each writing Tier 1 and paging is the 2026-08-14
    shape (34 restarts, ~68 Slack messages) rebuilt inside one process.
    """
    broker = _FlakyBroker([], fail_times=999)
    audit = _Audit()
    alerts: list[tuple[str, dict]] = []
    with pytest.raises(BrokerUnreachable):
        await reconcile_with_retry(
            Reconciler(broker, InMemoryPositionStore()),
            config=_fake_time_config(tmp_path),
            audit=audit,
            alert=lambda kind, d: alerts.append((kind, d)),
            inputs=_inputs(),
            now=no_real_sleeps.now,
        )
    assert [c for c, _ in audit.records] == ["reconcile.unreachable"]
    assert [k for k, _ in alerts] == ["reconcile_unreachable"]
    payload = audit.records[0][1]
    assert payload["gave_up"] == "budget_exhausted"
    assert payload["leg"] == "positions"
    assert payload["outer_attempts"] == 3   # 150s budget at a 60s interval
    assert broker.calls == 9                # 3 outer x 3 inner


async def test_the_outer_loop_stops_when_the_operator_says_stop(no_real_sleeps, tmp_path):
    """`/kill` during the retry means stop trying to start trading."""
    stop = asyncio.Event()
    broker = _FlakyBroker([], fail_times=999)

    async def fake_sleep(_seconds: float) -> None:
        stop.set()   # the operator presses kill during the first wait

    audit = _Audit()
    with pytest.raises(BrokerUnreachable):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("argus.reconcile.asyncio.sleep", fake_sleep)
            await reconcile_with_retry(
                Reconciler(broker, InMemoryPositionStore()),
                config=_fake_time_config(tmp_path, reconcile={"retry_budget_s": 100_000.0}),
                audit=audit,
                alert=lambda _k, _d: None,
                inputs=_inputs(),
                stop=stop,
            )
    assert audit.records[0][1]["gave_up"] == "stop_requested"
    assert broker.calls == 3, "and it did NOT ask again after being told to stop"


async def test_the_clock_is_re_read_every_outer_attempt(no_real_sleeps, tmp_path):
    """A 30-minute budget can cross the ET date rollover and the settlement boundary.

    An evening restart is precisely when a process-start reconcile runs (2026-08-25),
    so a date captured before the loop is a live hazard, not a theoretical one.
    """
    seen: list[tuple[date, bool]] = []
    days = [(date(2026, 8, 26), False), (date(2026, 8, 26), True), (date(2026, 8, 27), False)]

    def inputs():
        value = days[len(seen)] if len(seen) < len(days) else days[-1]
        seen.append(value)
        return value

    with pytest.raises(BrokerUnreachable):
        await reconcile_with_retry(
            Reconciler(_FlakyBroker([], fail_times=999), InMemoryPositionStore()),
            config=_fake_time_config(tmp_path),
            audit=_Audit(),
            alert=lambda _k, _d: None,
            inputs=inputs,
            now=no_real_sleeps.now,
        )
    assert seen == days, "read once per attempt, not captured once"


# -- process start: the park --------------------------------------------------


async def test_exhausted_at_process_start_PARKS_and_keeps_serving(tmp_path: Path, fast_retry):
    """The 2026-08-26 defect, closed.

    Until then this raised out of `startup()`, tore the process down and exited;
    Docker restarted it into the same wall five times in 33 seconds, with the
    control surface down for every one of them and no halt reason recorded
    anywhere. Same assertions as the 2026-08-17 mismatch park, one source string
    apart — because a broker that cannot be ASKED and one that ANSWERED WRONG are
    both terminal refusals to resume, and only one of them used to be observable.
    """
    broker = _FlakyBroker([], fail_times=999)
    cfg = _config(tmp_path, environment="sandbox")
    app = Application(cfg, reconciler=Reconciler(broker, InMemoryPositionStore()))

    await app.startup()  # must NOT raise
    try:
        assert app._halted is not None
        assert app._halted.source == "broker_unreachable"
        assert "unreachable" in app._halted.reason
        assert app._killswitch is not None and app._killswitch.engaged
        # Alive and serving — the whole point.
        assert app._control is not None and app._control.bound_address is not None
        snapshot = await app._control._status_service.assemble()
        assert snapshot.halted is True
        assert snapshot.halt_source == "broker_unreachable"
        # And the page is not still claiming a reconcile is in progress.
        assert snapshot.phase == "idle"

        # It cannot trade, even via the direct entry point that skips the scheduler.
        await app._run_session_day(
            TODAY, app._now_et() + timedelta(hours=1), asyncio.Event()
        )
    finally:
        await app.shutdown()

    cats = _cats(cfg.audit.db_path)
    assert "process.start" in cats, "the process really did start; it then halted"
    assert "reconcile.unreachable" in cats
    assert cats.count("reconcile.unreachable") == 1
    assert "reconcile.result" not in cats, "there was never a verdict to record"
    assert "session.configured" not in cats
    assert _payload(cfg.audit.db_path, "reconcile.unreachable")["leg"] == "positions"


async def test_a_transient_failure_at_process_start_never_halts(tmp_path: Path, fast_retry):
    broker = _FlakyBroker([], fail_times=2)
    alerts: list[str] = []
    app = Application(
        _config(tmp_path, environment="sandbox"),
        reconciler=Reconciler(broker, InMemoryPositionStore()),
        alert=lambda kind, _d: alerts.append(kind),
    )
    await app.startup()
    try:
        assert app._halted is None
        assert app._killswitch is not None and not app._killswitch.engaged
        assert "reconcile_unreachable" not in alerts
        assert (await app._control._status_service.assemble()).phase == "idle"
    finally:
        await app.shutdown()
    cats = _cats(app._config.audit.db_path)
    assert "reconcile.result" in cats
    assert "reconcile.unreachable" not in cats


async def test_shadow_is_lenient_about_an_unreachable_broker_too(tmp_path: Path, fast_retry):
    """Consistency with the mismatch path, which shadow has always been lenient about.

    In production shadow wires no process-start reconciler at all; this pins the
    injected branch, so the two failure classes stay gated the same way rather than
    drifting into one halting where the other does not.
    """
    broker = _FlakyBroker([], fail_times=999)
    app = Application(
        _config(tmp_path, environment="shadow"),
        reconciler=Reconciler(broker, InMemoryPositionStore()),
    )
    await app.startup()   # must NOT raise and must NOT halt
    try:
        assert app._halted is None
        assert app._killswitch is not None and not app._killswitch.engaged
    finally:
        await app.shutdown()
    assert "reconcile.unreachable" in _cats(app._config.audit.db_path)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def test_a_park_is_not_an_exit_and_the_supervisor_waits_for_a_signal(tmp_path: Path, fast_retry):
    """`run()` must park on this halt, exactly as it parks on a mismatch.

    Docker's `unless-stopped` sees a healthy container instead of restarting into
    the same wall — which is what removes the restart loop rather than capping it.
    """
    app = Application(
        _config(tmp_path, environment="sandbox"),
        reconciler=Reconciler(_FlakyBroker([], fail_times=999), InMemoryPositionStore()),
        now_et=_Clock(datetime(2026, 8, 26, 9, 40, tzinfo=ET)),
    )
    runner = asyncio.create_task(app.run())
    try:
        for _ in range(200):
            if app._halted is not None and app._control is not None:
                break
            await asyncio.sleep(0.01)
        assert app._halted is not None and app._halted.source == "broker_unreachable"
        assert not runner.done(), "parked, not exited"
        assert app._control.bound_address is not None, "and still serving"
        app.request_exit()
        await asyncio.wait_for(runner, timeout=2.0)
    finally:
        if not runner.done():
            runner.cancel()
    cats = _cats(app._config.audit.db_path)
    assert cats.count("process.halted") == 1, "announced exactly once"


# -- per trading day: abort, never a bare session.error -----------------------


async def test_exhausted_per_day_aborts_the_day_and_never_halts(tmp_path: Path, fast_retry):
    """The other half of 2026-08-26: a `ReadTimeout` filed as "a genuine bug".

    It is now a clean, ALERTED abort. Alerted matters: `session.aborted` fires no
    Slack alert of its own, so an unreachable broker that only ended there would be
    quieter than the `session.error` it replaces — a regression against the very
    thing this change exists to improve.
    """
    from argus import session as session_mod

    audit = _Audit()
    alerts: list[tuple[str, dict]] = []
    cfg = _migrated(_config(tmp_path, environment="sandbox"))
    halts: list[str] = []

    async def halt(reason, *, source, grade=None, detail=None):
        halts.append(source)

    with pytest.raises(session_mod.SessionAborted) as exc:
        await session_mod._reconcile_or_abort(
            _FlakyBroker([], fail_times=999),
            cfg,
            audit,          # type: ignore[arg-type]
            lambda kind, d: alerts.append((kind, d)),
            halt,
            trading_date=TODAY,
        )
    assert "could not reach the broker" in str(exc.value)
    assert isinstance(exc.value.__cause__, BrokerUnreachable)
    assert halts == [], "unknown is not known-wrong; the process is not halted"
    assert [k for k, _ in alerts] == ["reconcile_unreachable"]
    assert [c for c, _ in audit.records] == ["reconcile.unreachable"]


async def test_a_transient_failure_per_day_lets_the_session_proceed(tmp_path: Path, fast_retry):
    from argus import session as session_mod

    audit = _Audit()
    cfg = _migrated(_config(tmp_path, environment="sandbox"))
    broker = _FlakyBroker([], fail_times=3)   # one whole outer attempt lost
    await session_mod._reconcile_or_abort(
        broker, cfg, audit, lambda _k, _d: None, None, trading_date=TODAY  # type: ignore[arg-type]
    )
    assert broker.calls == 4
    assert [c for c, _ in audit.records] == ["reconcile.result"]


async def test_a_per_day_mismatch_still_halts_with_zero_retry(tmp_path: Path, fast_retry):
    """The mismatch path at THIS call site is untouched, and still costs one call."""
    from argus import session as session_mod

    audit = _Audit()
    cfg = _migrated(_config(tmp_path, environment="sandbox"))
    broker = _FlakyBroker([A_POSITION])
    halts: list[str] = []

    async def halt(reason, *, source, grade=None, detail=None):
        halts.append(source)

    with pytest.raises(session_mod.SessionAborted):
        await session_mod._reconcile_or_abort(
            broker, cfg, audit, lambda _k, _d: None, halt, trading_date=TODAY  # type: ignore[arg-type]
        )
    assert broker.calls == 1
    assert halts == ["reconciliation_mismatch"]


# -- the other two legs of "could not ask" at process start -------------------
#
# `_fetch_accounts` already had the 3-attempt retry and already raised the domain
# exception — and still killed the process, because `SessionAborted` out of
# `startup()` lands on the same `except BaseException` the raw `TastytradeError`
# did. Fixing only the reconcile fetch would have left two thirds of the incident
# in place. `tt.open()` is the third and had no retry at all.


class _FakeTT:
    """Enough `TastytradeSession` for `startup_reconcile`'s first two steps."""

    def __init__(self, *_a, open_raises: Exception | None = None, **_kw) -> None:
        self._open_raises = open_raises
        self.closed = False

    async def open(self):
        if self._open_raises is not None:
            raise self._open_raises
        return object()

    async def aclose(self) -> None:
        self.closed = True

    @property
    def trading_session(self):
        return object()


async def test_a_login_failure_at_process_start_is_could_not_ask(monkeypatch, tmp_path):
    """And gets NO retry, deliberately — each failed OAuth refresh accrues ban risk.

    `_login_with_retry` spaces its attempts a flat 10 MINUTES apart for exactly that
    reason, so retrying a login on the reconcile's 60s cadence would be five times
    more aggressive than a number that was chosen against an IP-block threshold.
    The type change is the whole fix here: it routes the login leg into the park
    instead of tearing the process down as an unknown failure.
    """
    from argus import session as session_mod

    opened = {"n": 0}

    def _session(*_a, **_kw):
        opened["n"] += 1
        return _FakeTT(open_raises=RuntimeError("502 from /oauth/token"))

    monkeypatch.setattr(session_mod, "trading_credentials", lambda _cfg: object())
    monkeypatch.setattr(session_mod, "TastytradeSession", _session)

    with pytest.raises(BrokerUnreachable) as exc:
        await session_mod.startup_reconcile(
            config=_config(tmp_path, environment="sandbox"),
            audit=_Audit(),        # type: ignore[arg-type]
            alert=lambda _k, _d: None,
        )
    assert exc.value.leg == "login"
    assert opened["n"] == 1, "no retry on the token endpoint"
    assert isinstance(exc.value.__cause__, RuntimeError)


async def test_an_exhausted_account_fetch_is_could_not_ask_not_a_config_error(
    monkeypatch, tmp_path
):
    """`SessionAborted` used to carry both meanings out of here. It cannot any more.

    `_fetch_accounts` raises it on RETRY EXHAUSTION (transient), and the
    no-matching-account check raises it for a WRONG ACCOUNT NUMBER (permanent). An
    outer loop must retry the first and must never spend 30 minutes on the second,
    so they stopped sharing a type.
    """
    from argus import session as session_mod

    monkeypatch.setattr(session_mod, "trading_credentials", lambda _cfg: object())
    monkeypatch.setattr(session_mod, "TastytradeSession", lambda *_a, **_kw: _FakeTT())

    async def _exhausted(*_a, **_kw):
        raise session_mod.SessionAborted("could not fetch accounts after 3 attempts")

    monkeypatch.setattr(session_mod, "_fetch_accounts", _exhausted)
    with pytest.raises(BrokerUnreachable) as exc:
        await session_mod.startup_reconcile(
            config=_config(tmp_path, environment="sandbox"),
            audit=_Audit(),        # type: ignore[arg-type]
            alert=lambda _k, _d: None,
        )
    assert exc.value.leg == "accounts"


async def test_a_wrong_account_number_stays_a_permanent_session_aborted(
    monkeypatch, tmp_path
):
    from argus import session as session_mod

    monkeypatch.setattr(session_mod, "trading_credentials", lambda _cfg: object())
    monkeypatch.setattr(session_mod, "TastytradeSession", lambda *_a, **_kw: _FakeTT())

    class _Acct:
        account_number = "5WX00001"

    async def _accounts(*_a, **_kw):
        return [_Acct()]

    monkeypatch.setattr(session_mod, "_fetch_accounts", _accounts)
    with pytest.raises(session_mod.SessionAborted) as exc:
        await session_mod.startup_reconcile(
            config=_config(tmp_path, environment="sandbox"),
            audit=_Audit(),        # type: ignore[arg-type]
            alert=lambda _k, _d: None,
            account_number="NOT-THIS-ONE",
        )
    assert not isinstance(exc.value, BrokerUnreachable)
    assert "no matching account" in str(exc.value)


async def test_both_of_those_park_the_process_rather_than_exiting_it(
    monkeypatch, tmp_path: Path
):
    """Whatever `startup_reconcile` refuses with, the process stays up and says why.

    Before 2026-08-26 every one of these exited, and Docker restarted into it.
    """
    import argus.app as app_mod
    from argus import session as session_mod

    for raiser, expect_reason in (
        (BrokerUnreachable("login", 1, RuntimeError("502")), "unreachable"),
        (session_mod.SessionAborted("no matching account at process start"), "could not start"),
    ):
        async def _boom(*_a, _exc=raiser, **_kw):
            raise _exc

        monkeypatch.setattr(app_mod, "startup_reconcile", _boom)
        app = Application(_config(tmp_path / str(id(raiser)), environment="sandbox"))
        (tmp_path / str(id(raiser))).mkdir(exist_ok=True)
        await app.startup()          # must NOT raise
        try:
            assert app._halted is not None
            assert app._halted.source == "broker_unreachable"
            assert expect_reason in app._halted.reason
            assert app._control is not None and app._control.bound_address is not None
        finally:
            await app.shutdown()


async def test_a_kill_during_the_60s_WAIT_costs_no_further_broker_call(
    no_real_sleeps, tmp_path
):
    """The other stop path: set during the outer wait, not during an inner backoff.

    Caught at the top of the loop rather than by asking the broker once more and
    discovering it in the handler — a round trip nobody wants after being told to
    stop, and 30 seconds of one on a wedged endpoint.
    """
    stop = asyncio.Event()
    broker = _FlakyBroker([], fail_times=999)

    async def fake_sleep(seconds: float) -> None:
        no_real_sleeps.append(seconds)
        no_real_sleeps.elapsed += seconds
        if seconds == 60.0:          # the OUTER wait, not the 2s/4s inner backoff
            stop.set()

    audit = _Audit()
    with pytest.raises(BrokerUnreachable):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("argus.reconcile.asyncio.sleep", fake_sleep)
            await reconcile_with_retry(
                Reconciler(broker, InMemoryPositionStore()),
                config=_fake_time_config(tmp_path, reconcile={"retry_budget_s": 100_000.0}),
                audit=audit,
                alert=lambda _k, _d: None,
                inputs=_inputs(),
                stop=stop,
                now=no_real_sleeps.now,
            )
    assert audit.records[0][1]["gave_up"] == "stop_requested"
    assert broker.calls == 3, "one outer attempt's inner burst, and nothing after it"
