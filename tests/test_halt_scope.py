"""P3 — a halt has a GRADE, and the grade decides what it stops (2026-08-29).

`reconcile.halt_grade` is the pure classifier; everything else here is the wiring
that consumes it. The property under test throughout is the one the design record
states as non-negotiable: **a position discrepancy of any kind halts everything**,
and only an order-level discrepancy that is one of Argus's OWN working OPENING
orders earns the narrower `HaltGrade.ENTRIES`, under which the scheduler keeps
running so held positions keep their watchdog, their marks and their 15:30 flatten.

→ docs/decisions/2026-08-29-halt-scope-split.md

Every config here overrides `audit.db_path` to a tmp file — the live `argus.db` is
production data and dev/test must never open it (CLAUDE.md).
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from argus.account.fake import FakeBrokerAccount
from argus.account.models import Position
from argus.app import Application, HaltRecord
from argus.db import apply_migrations, connect
from argus.execution.models import (
    LegAction,
    LiveOrders,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
)
from argus.execution.store import InMemoryOrderStore
from argus.reconcile import (
    Discrepancy,
    HaltGrade,
    InMemoryPositionStore,
    OrderDiscrepancy,
    ReconciliationResult,
    Reconciler,
    halt_grade,
)
from argus.session import SessionAborted, _reconcile_or_abort
from support import make_config

_SRC = Path(__file__).resolve().parents[1] / "src" / "argus"

# The 2026-08-28 order verbatim: Argus's own, opening, still working. The one real
# instance the rule has ever been shown, and the one it must admit.
_REAL = "argus-open-20260828-call-7765-7770"


def _order(identifier: str, kind: str = "unexpected_working_order") -> OrderDiscrepancy:
    return OrderDiscrepancy(
        broker_order_id=1424563,
        external_identifier=identifier,
        kind=kind,
        status="Received",
        disposition="working",
    )


def _result(
    positions: tuple[Discrepancy, ...] = (), orders: tuple[OrderDiscrepancy, ...] = ()
) -> ReconciliationResult:
    return ReconciliationResult(
        ok=not positions and not orders,
        discrepancies=positions,
        order_discrepancies=orders,
    )


# -- the classifier -----------------------------------------------------------


def test_the_one_real_instance_is_admitted():
    """2026-08-28's `1424563`: Argus's own, opening, working, nothing else wrong.

    A rule that failed its only observed instance would be worth noticing. This is
    not a validation set and the rule is written to be right rather than to fit it.
    """
    assert halt_grade(_result(orders=(_order(_REAL),))) is HaltGrade.ENTRIES


@pytest.mark.parametrize(
    "kind",
    ["unexpected_at_broker", "missing_at_broker", "quantity_mismatch"],
)
def test_every_position_discrepancy_halts_everything(kind):
    """No carve-out for ANY position kind, alone or alongside an eligible order.

    `missing_at_broker` is the one that would hurt: the watchdog would be watching a
    spread that is not there, and a triggered stop — or the flatten — would send a
    CLOSING order for a leg the account does not hold, which is an opening trade,
    unmanaged, on expiry day. The expiry carve-out has already run and declined to
    explain the row by the time this is asked.
    """
    d = Discrepancy("SPXW260828C07765000", kind, 1, 0)
    assert halt_grade(_result(positions=(d,))) is HaltGrade.ALL
    # And it is not rescued by the company it keeps.
    assert halt_grade(_result(positions=(d,), orders=(_order(_REAL),))) is HaltGrade.ALL


@pytest.mark.parametrize(
    "kind", ["unexpected_filled_order", "unmappable_broker_order"]
)
def test_only_a_WORKING_order_discrepancy_is_eligible(kind):
    """FILLED is a position claim wearing an order's clothes; UNMAPPABLE answers
    nothing at all. Neither is "no position implication", even with Argus's own
    opening identifier on it."""
    assert halt_grade(_result(orders=(_order(_REAL, kind=kind),))) is HaltGrade.ALL


@pytest.mark.parametrize(
    "identifier",
    [
        "argus-close-20260828-call-7765-7770",  # an exit Argus lost track of
        "someone-elses-order-1",                 # a human trading the account
        "",                                      # unparseable
        "argus-sideways-20260828-call-7765-7770",  # not a direction at all
    ],
)
def test_only_argus_own_OPENING_orders_are_eligible(identifier):
    assert halt_grade(_result(orders=(_order(identifier),))) is HaltGrade.ALL


def test_one_ineligible_order_disqualifies_the_whole_set():
    """The rule is over EVERY member, not over any member — a foreign order mixed in
    with Argus's own is somebody trading the account, and there is no bound on what
    they do next."""
    orders = (_order(_REAL), _order("someone-elses-order-1"))
    assert halt_grade(_result(orders=orders)) is HaltGrade.ALL


def test_a_not_ok_verdict_with_no_discrepancies_fails_closed():
    """Unreachable today (`ok` is derived from the two sets) and therefore exactly
    the shape that must fail closed if it ever becomes reachable."""
    impossible = ReconciliationResult(ok=False, discrepancies=(), order_discrepancies=())
    assert halt_grade(impossible) is HaltGrade.ALL


def test_a_clean_verdict_is_not_a_narrow_halt():
    """`ok` is not a halt at all. Callers gate on it first; answering ALL keeps the
    function total without inventing a third value nobody reads."""
    assert halt_grade(_result()) is HaltGrade.ALL


# -- the chained-verticals rule, which is why there is no symbol test ----------


def test_a_shared_strike_does_not_make_an_order_ineligible():
    """Two chained spreads share a strike; an unaccounted opening order ON that
    strike is still eligible, because the rule never looks at strikes.

    This is the case a "does this order touch a held position?" test would get
    WRONG in the dangerous direction: `positions` nets the shared strike to zero
    and `open_positions()` filters `signed_quantity != 0`, so the strike two live
    spreads are both standing on is ABSENT from the expected map — a symbol test
    would answer "no overlap" for precisely it.
    → docs/decisions/2026-08-17-overlapping-verticals.md
    """
    shared = _order("argus-open-20260828-call-7765-7770")
    assert halt_grade(_result(orders=(shared,))) is HaltGrade.ENTRIES


def test_the_classifier_compares_no_symbols_at_all():
    """Structural, so a future "tightening" that adds a strike comparison fails a
    test rather than a position. On the AST rather than the text: the docstring
    correctly discusses symbols at length and counting those would assert nothing.
    """
    tree = ast.parse((_SRC / "reconcile.py").read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "halt_grade"
    )
    # Strip the docstring; it is prose about symbols by design.
    body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    read = {
        node.attr
        for stmt in body
        for node in ast.walk(stmt)
        if isinstance(node, ast.Attribute)
    }
    for banned in ("symbol", "symbols", "legs", "strike", "short_strike", "long_strike"):
        assert banned not in read, (
            f"halt_grade reads `.{banned}` — the eligibility rule must never compare "
            "symbols or strikes; chained verticals net a shared strike to zero"
        )


# -- the grade lattice --------------------------------------------------------


def test_a_grade_tightens_and_never_relaxes():
    assert HaltGrade.ENTRIES.tighten(HaltGrade.ALL) is HaltGrade.ALL
    assert HaltGrade.ALL.tighten(HaltGrade.ENTRIES) is HaltGrade.ALL
    assert HaltGrade.ALL.tighten(HaltGrade.ALL) is HaltGrade.ALL
    assert HaltGrade.ENTRIES.tighten(HaltGrade.ENTRIES) is HaltGrade.ENTRIES


# -- `_halt_process` / `_mark_halted` ----------------------------------------


def _config(tmp_path: Path, **overrides):
    audit = {"db_path": str(tmp_path / "argus.db")}
    audit.update(overrides.pop("audit", {}))
    return make_config(environment="sandbox", audit=audit, **overrides)


def _prepare_db(path):
    conn = connect(path)
    apply_migrations(conn)
    conn.close()


def _clean_app(tmp_path: Path, **kw) -> Application:
    """An Application whose process-start reconcile is clean, so it starts."""
    return Application(
        _config(tmp_path),
        reconciler=Reconciler(FakeBrokerAccount([]), InMemoryPositionStore()),
        **kw,
    )


async def test_an_ENTRIES_halt_does_not_request_stop(tmp_path: Path):
    """The one thing the narrow grade skips. Everything else is identical: the kill
    switch is engaged, the halt is latched, and the process will not enter again."""
    app = _clean_app(tmp_path)
    await app.startup()
    try:
        await app._halt_process("order-level (test)", source="reconciliation_mismatch",
                                grade=HaltGrade.ENTRIES)
        assert app.killswitch.engaged is True     # nothing enters, either way
        assert app.halt_grade() is HaltGrade.ENTRIES
        assert app._stop.is_set() is False        # ... and the session lives on
    finally:
        await app.shutdown()


async def test_an_ALL_halt_still_requests_stop(tmp_path: Path):
    """The negative control. Either half alone proves nothing."""
    app = _clean_app(tmp_path)
    await app.startup()
    try:
        await app._halt_process("everything (test)", source="audit_log")
        assert app.killswitch.engaged is True
        assert app.halt_grade() is HaltGrade.ALL
        assert app._stop.is_set() is True
    finally:
        await app.shutdown()


async def test_the_default_grade_is_ALL(tmp_path: Path):
    """Every caller that does not pass one keeps stopping everything, and so does a
    future halt source nobody graded."""
    app = _clean_app(tmp_path)
    await app.startup()
    try:
        await app._halt_on_audit_failure("disk full (test)")
        assert app.halt_grade() is HaltGrade.ALL
        assert app._stop.is_set() is True
    finally:
        await app.shutdown()


async def test_a_narrow_halt_escalates_and_keeps_the_first_reason(tmp_path: Path):
    """The two fields have opposite update rules, and both are asserted here.

    `reason` is first-writer-wins — a cascade's first cause is the useful one.
    `grade` is strictest-wins — the second condition genuinely does stop everything,
    and inheriting the first one's narrower scope would leave the watchdog running
    through a state nobody cleared it for.
    """
    app = _clean_app(tmp_path)
    await app.startup()
    try:
        await app._halt_process("order-level (test)", source="reconciliation_mismatch",
                                grade=HaltGrade.ENTRIES)
        first_at = app._halted.at
        await app._halt_process("audit log unwritable (test)", source="audit_log")
        assert app._halted.reason == "order-level (test)"       # first cause kept
        assert app._halted.source == "reconciliation_mismatch"
        assert app._halted.at == first_at
        assert app.halt_grade() is HaltGrade.ALL                # scope escalated
        assert app._stop.is_set() is True
    finally:
        await app.shutdown()


async def test_a_wide_halt_is_never_relaxed_by_a_later_narrow_verdict(tmp_path: Path):
    """The direction that must be impossible — it is what makes re-deriving the
    grade on an already-halted process safe."""
    app = _clean_app(tmp_path)
    await app.startup()
    try:
        await app._halt_process("everything (test)", source="audit_log")
        await app._halt_process("order-level (test)", source="reconciliation_mismatch",
                                grade=HaltGrade.ENTRIES)
        assert app.halt_grade() is HaltGrade.ALL
    finally:
        await app.shutdown()


# -- the announcement ---------------------------------------------------------


@pytest.mark.parametrize("grade", [HaltGrade.ALL, HaltGrade.ENTRIES])
async def test_a_halt_is_announced_exactly_once_in_both_grades(tmp_path: Path, grade):
    """The announcement moved to the halt EDGE because an ENTRIES halt never reaches
    `_await_exit_while_halted` — leaving it there would have made the NARROW grade
    the silent one, which is precisely backwards. Exactly one is still the
    requirement: the 2026-08-14 storm was ~68 Slack messages for one condition."""
    alerts: list[str] = []
    app = _clean_app(tmp_path, alert=lambda kind, detail: alerts.append(kind))
    await app.startup()
    try:
        await app._halt_process("test", source="reconciliation_mismatch", grade=grade)
        assert alerts.count("process_halted") == 1
        rows = app.audit._conn.execute(
            "SELECT payload FROM audit_log WHERE category='process.halted'"
        ).fetchall()
        assert len(rows) == 1
        assert f'"grade": "{grade}"' in rows[0]["payload"]
    finally:
        await app.shutdown()


async def test_a_cascading_halt_still_announces_once(tmp_path: Path):
    """A mismatch stops the day and the audit log may latch failed on the way down.
    One halt that widened, not two halts."""
    alerts: list[str] = []
    app = _clean_app(tmp_path, alert=lambda kind, detail: alerts.append(kind))
    await app.startup()
    try:
        await app._halt_process("order-level (test)", source="reconciliation_mismatch",
                                grade=HaltGrade.ENTRIES)
        await app._halt_process("audit log unwritable (test)", source="audit_log")
        assert alerts.count("process_halted") == 1
        rows = app.audit._conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE category='process.halted'"
        ).fetchone()
        assert rows["c"] == 1
    finally:
        await app.shutdown()


# -- end to end: does a session actually run? --------------------------------


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _dt(d: date, hh: int, mm: int) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo("America/New_York"))


async def _wait_until(predicate, *, timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


class _MismatchReconciler:
    """A reconciler that returns a fixed verdict. `order_store` mirrors the real
    one's public attribute so the reset path is exercised identically."""

    order_store = None

    def __init__(self, result: ReconciliationResult) -> None:
        self._result = result

    async def reconcile(self, **_kw) -> ReconciliationResult:
        return self._result


@asynccontextmanager
async def _run_until_halted(tmp_path: Path, result: ReconciliationResult):
    """Start an Application whose PROCESS-START reconcile returns `result`, run it,
    and report which trading days got a session.

    A context manager rather than a bare generator so the process is signalled and
    awaited even when an assertion inside the block fails — otherwise a failing test
    leaks a running `Application` into the next one.
    """
    clock = _Clock(_dt(date(2026, 7, 24), 9, 40))  # Friday, at the open

    async def wait(seconds: float) -> None:
        clock.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    sessions: list[date] = []
    alerts: list[str] = []
    holder: dict[str, Application] = {}

    async def body(*, trading_date, **_kw):
        sessions.append(trading_date)
        if len(sessions) >= 2:
            holder["app"].request_stop()

    app = Application(
        _config(tmp_path),
        reconciler=_MismatchReconciler(result),
        session_body=body,
        now_et=clock,
        wait_fn=wait,
        alert=lambda kind, detail: alerts.append(kind),
    )
    holder["app"] = app
    task = asyncio.create_task(app.run())
    try:
        await _wait_until(lambda: alerts.count("process_halted") == 1, timeout=3.0)
        await asyncio.sleep(0.05)
        yield app, sessions, task
    finally:
        app.request_exit()
        await asyncio.wait_for(task, timeout=3.0)


async def test_an_order_level_mismatch_at_process_start_still_runs_the_watchdog(
    tmp_path: Path,
):
    """**Finding 7, closed.** On 2026-08-28 this exact verdict parked the process
    before any session existed, so `phase: idle`, `trading_date: null`, and two live
    0DTE spreads went unwatched three and three-quarter hours from settlement.

    The session is what owns the watchdog, so "the watchdog runs" IS "a session
    runs" — and the halt is still permanent, still engaged, still HALTED on the page.
    """
    result = _result(orders=(_order(_REAL),))
    async with _run_until_halted(tmp_path, result) as (app, sessions, _task):
        assert app.halt_grade() is HaltGrade.ENTRIES
        assert app.killswitch.engaged is True     # nothing enters, ever
        assert sessions, "a monitor-only session must run — the watchdog lives in it"
        snap = await app.status_service.assemble()
        assert snap.halted is True                # NOT softened
        assert snap.halt_grade == "entries"
        assert "MONITOR-ONLY" in snap.to_html()


async def test_a_position_level_mismatch_at_process_start_runs_nothing(tmp_path: Path):
    """The negative control, and the half that must not move. Either test alone
    proves nothing."""
    d = Discrepancy("SPXW260828C07765000", "missing_at_broker", 1, 0)
    result = _result(positions=(d,), orders=(_order(_REAL),))
    async with _run_until_halted(tmp_path, result) as (app, sessions, task):
        assert app.halt_grade() is HaltGrade.ALL
        assert sessions == []
        assert not task.done(), "an ALL halt parks; it does not exit"
        snap = await app.status_service.assemble()
        assert snap.halt_grade == "all"
        assert "restart required" in snap.to_html()


async def test_a_monitor_only_process_parks_the_moment_it_escalates(tmp_path: Path):
    """A day running under ENTRIES that then hits a wider condition stops for real:
    `request_stop` ends the scheduler loop, and `run()` falls into the same park it
    has used since 2026-08-17."""
    clock = _Clock(_dt(date(2026, 7, 24), 9, 40))

    async def wait(seconds: float) -> None:
        clock.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    sessions: list[date] = []
    holder: dict[str, Application] = {}

    async def body(*, trading_date, **_kw):
        sessions.append(trading_date)
        # The wider condition, mid-session.
        await holder["app"]._halt_process("audit log unwritable (test)", source="audit_log")

    app = Application(
        _config(tmp_path),
        reconciler=_MismatchReconciler(_result(orders=(_order(_REAL),))),
        session_body=body,
        now_et=clock,
        wait_fn=wait,
    )
    holder["app"] = app
    task = asyncio.create_task(app.run())
    try:
        await _wait_until(lambda: bool(sessions), timeout=3.0)
        await _wait_until(lambda: app.halt_grade() is HaltGrade.ALL, timeout=3.0)
        await asyncio.sleep(0.05)
        assert app._stop.is_set() is True
        assert sessions == [date(2026, 7, 24)]   # and never a second day
        assert not task.done(), "escalation parks, it does not exit"
    finally:
        app.request_exit()
        await asyncio.wait_for(task, timeout=3.0)


# -- the per-session reconcile's own refusal ---------------------------------


_UNEXPECTED_POSITION = Position("SPXW260718P05000000", "SPX", "Equity Option", -1, 1.2, 0.9)


class _StubAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []
        self.telemetry_records: list[tuple[str, dict]] = []

    async def record(self, category, payload):
        self.records.append((category, payload))

    def telemetry(self, category, payload):
        self.telemetry_records.append((category, payload))

    async def read(self, fn):
        """No clean-reconciliation marker on record, so the sandbox-reset check
        refuses to clear anything — which is what it should do here: this test is
        about the halt grade, not about the reset rail."""
        return None


class _HaltSpy:
    def __init__(self, latched: HaltGrade | None = None) -> None:
        self.calls: list[dict] = []
        self._latched = latched

    async def __call__(self, reason, *, source, grade=HaltGrade.ALL, detail=None):
        self.calls.append({"reason": reason, "source": source, "grade": grade,
                           "detail": detail})
        # Mirrors `_mark_halted`: strictest wins.
        self._latched = grade if self._latched is None else self._latched.tighten(grade)

    def latched(self) -> HaltGrade | None:
        return self._latched


class _StubExecution:
    """Just enough of `OrderExecutionPort` for `Reconciler` — it calls exactly one
    method. A whole `ExecutionStack` here would be scaffolding, not coverage."""

    def __init__(self, orders: tuple[Order, ...]) -> None:
        self._orders = orders

    async def get_live_orders(self) -> LiveOrders:
        return LiveOrders(orders=self._orders, unmappable=())


class _StubStack:
    """`_reconcile_or_abort` reads exactly `.execution` and `.store` off it."""

    def __init__(self, orders: tuple[Order, ...]) -> None:
        self.execution = _StubExecution(orders)
        self.store = InMemoryOrderStore()


def _working_order(identifier: str) -> Order:
    """A live opening order at the broker that Argus's ledger has no row for."""
    return Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  C7765", LegAction.SELL_TO_OPEN, 1, 1),
            OrderLeg("SPXW  C7770", LegAction.BUY_TO_OPEN, 1, 1),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=identifier,
        broker_order_id=1424563,
        status=OrderStatus.LIVE,
    )


async def test_a_per_session_order_level_mismatch_continues_monitor_only(tmp_path: Path):
    """The real thing, through the real comparison: an unaccounted WORKING opening
    order of Argus's own, a flat broker, an empty ledger.

    It halts the process (permanently, kill switch and all) and then RETURNS True,
    so the session keeps going with its entry hook unwired and the watchdog runs.
    """
    cfg = _config(tmp_path)
    _prepare_db(cfg.audit.db_path)
    audit, halt, alerts = _StubAudit(), _HaltSpy(), []

    monitor_only = await _reconcile_or_abort(
        FakeBrokerAccount([]), cfg, audit, lambda k, d: alerts.append(k), halt,
        stack=_StubStack((_working_order(_REAL),)),
        trading_date=date(2026, 8, 28),
        halted_grade=halt.latched,
    )

    assert monitor_only is True
    assert len(halt.calls) == 1
    assert halt.calls[0]["grade"] is HaltGrade.ENTRIES
    assert halt.calls[0]["detail"]["halt_grade"] == "entries"
    # Loud in the trail and on a phone — overriding the parked default, even
    # narrowly, is its own event.
    assert any(c == "session.monitor_only" for c, _ in audit.records)
    assert "session_monitor_only" in alerts
    assert "reconciliation_mismatch" in alerts


async def test_the_same_order_but_CLOSING_aborts_the_day(tmp_path: Path):
    """The negative control on the identifier rule, through the same path. An exit
    Argus placed and lost track of is a position changing hands."""
    cfg = _config(tmp_path)
    _prepare_db(cfg.audit.db_path)
    halt = _HaltSpy()

    with pytest.raises(SessionAborted):
        await _reconcile_or_abort(
            FakeBrokerAccount([]), cfg, _StubAudit(), lambda *_: None, halt,
            stack=_StubStack((_working_order("argus-close-20260828-call-7765-7770"),)),
            trading_date=date(2026, 8, 28),
            halted_grade=halt.latched,
        )
    assert halt.calls[0]["grade"] is HaltGrade.ALL


async def test_a_clean_verdict_is_not_a_monitor_only_day(tmp_path: Path):
    cfg = _config(tmp_path)
    _prepare_db(cfg.audit.db_path)
    halt = _HaltSpy()

    monitor_only = await _reconcile_or_abort(
        FakeBrokerAccount([]), cfg, _StubAudit(), lambda *_: None, halt,
        halted_grade=halt.latched,
    )
    assert monitor_only is False
    assert halt.calls == []


async def test_a_per_session_position_mismatch_still_aborts_the_day(tmp_path: Path):
    """Unchanged, and the halt it asks for is the wide one."""
    cfg = _config(tmp_path)
    _prepare_db(cfg.audit.db_path)
    audit, halt, alerts = _StubAudit(), _HaltSpy(), []

    with pytest.raises(SessionAborted):
        await _reconcile_or_abort(
            FakeBrokerAccount([_UNEXPECTED_POSITION]), cfg, audit,
            lambda k, d: alerts.append(k), halt, halted_grade=halt.latched,
        )
    assert len(halt.calls) == 1
    assert halt.calls[0]["grade"] is HaltGrade.ALL
    assert halt.calls[0]["detail"]["halt_grade"] == "all"


async def test_without_the_seam_a_mismatch_always_aborts(tmp_path: Path):
    """Fail-closed default: every existing caller (tests, tools, shadow) that never
    heard of the seam keeps getting "abort", whatever the verdict grades to."""
    cfg = _config(tmp_path)
    _prepare_db(cfg.audit.db_path)
    halt = _HaltSpy()

    with pytest.raises(SessionAborted):
        await _reconcile_or_abort(
            FakeBrokerAccount([_UNEXPECTED_POSITION]), cfg, _StubAudit(),
            lambda *_: None, halt,   # no halted_grade
        )


async def test_an_already_wide_halt_wins_over_a_narrow_verdict(tmp_path: Path):
    """The reason the decision is read back from the LATCHED state rather than from
    the locally-computed grade, and the only test that can tell them apart.

    THIS verdict grades to `ENTRIES` — one of Argus's own working opening orders,
    nothing else wrong — while an earlier halt has already latched `ALL`.
    `HaltGrade.tighten` refuses to relax, so the day must abort. Deriving the
    decision from the local computation instead would resume a session on a process
    that had already refused everything.
    """
    cfg = _config(tmp_path)
    _prepare_db(cfg.audit.db_path)
    halt = _HaltSpy(latched=HaltGrade.ALL)

    with pytest.raises(SessionAborted):
        await _reconcile_or_abort(
            FakeBrokerAccount([]), cfg, _StubAudit(), lambda *_: None, halt,
            stack=_StubStack((_working_order(_REAL),)),
            trading_date=date(2026, 8, 28),
            halted_grade=halt.latched,
        )
    # The verdict really was the narrow one; only the latched state overrode it.
    assert halt.calls[0]["grade"] is HaltGrade.ENTRIES


# -- the entry hook is withheld STRUCTURALLY, not only by the kill switch ------


def test_the_entry_hook_is_gated_on_monitor_only():
    """`submit` must be a CONDITIONAL on `monitor_only`, not an unconditional
    `trading.entry.submit`.

    The kill switch already refuses every candidate on a halted process, twice — at
    the gate and at `EntryExecutor.submit`'s pre-build re-check. This asserts the
    third, structural refusal, on the AST, because "nothing submits an order outside
    `trades_for_real`" is asserted rather than trusted and this deserves the same
    posture. A mutation that wires the hook under a halt fails here.
    """
    tree = ast.parse((_SRC / "session.py").read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_session"
    )
    assigns = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "submit" for t in n.targets)
    ]
    conditional = [a for a in assigns if isinstance(a.value, ast.IfExp)]
    assert conditional, "run_session must assign `submit` through a conditional"
    names = {
        n.id
        for a in conditional
        for n in ast.walk(a.value)
        if isinstance(n, ast.Name)
    }
    assert "monitor_only" in names, (
        "the entry hook must be gated on `monitor_only`; a halted process must not "
        "be handed a submit callable at all"
    )


def test_the_halt_record_defaults_to_the_strict_grade():
    """A `HaltRecord` built positionally — by a test, a tool, or a future caller —
    is the process-wide halt, never the narrow one."""
    rec = HaltRecord(reason="r", source="s", at=_dt(date(2026, 7, 24), 9, 40))
    assert rec.grade is HaltGrade.ALL
