"""`_end_entries_today` ends ENTRIES and nothing else (2026-09-01).

Until this landed, `EntryExecutor._end_day` ended today's entries by setting the
SESSION stop event. That event is read by four things with four meanings — the
watchdog's scan loop, an exit walk in flight, `run_session`'s teardown, and the
scheduler's "this day is done" — so a soft, entry-side stand-down on a transient
cert blip took a held 0DTE spread's virtual stop, its mark and its 15:30 flatten
for the rest of expiry day, and cancelled an exit already walking, which returns
`ABORTED` -> `NOT_RESOLVED` and thereby SPENDS that position's one exit attempt.

The tests are grouped the way the design argued the case:

  1. the watchdog, the exits and the flatten SURVIVE it (the whole point);
  2. entries really do END, with no retry of anything (what must not change);
  3. it latches NO durable state and is structurally incapable of becoming the
     second stop event P3 considered and rejected;
  4. the day still ends at the real close, and tomorrow starts clean;
  5. `/status` distinguishes it from P3's PERMANENT monitor-only day.

→ docs/decisions/2026-09-01-end-day-keeps-the-watchdog.md
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
from datetime import datetime

import pytest
from tastytrade.utils import TastytradeError

from argus.audit.log import AuditLog
from argus.clock import ET
from argus.db.connection import connect
from argus.db.migrations import apply_migrations
from argus.execution.entry import EntriesEnded, EntryExecutor
from argus.execution.loop import PriceWalkingLoop, WalkPolicy
from argus.execution.watchdog import PositionWatchdog, TriggerReason
from argus.status import EntriesEndedToday

from tests.support import make_config
from tests.test_entry import (
    MAX_STREAK,
    PROPOSAL,
    RISK,
    DeadGuardBroker,
    _drive_streak,
    _nth_candidate,
    build_entry,
    make_signal,
)
from tests.test_status import NOW, _make_service, _risk
from tests.test_watchdog import (
    FakeClock,
    FakeETClock,
    OpenSpreadScan,
    RecordingExecutor,
    _quiet_and_priceable,
    _triggered_and_priceable,
    build_quotes,
    build_watchdog,
    spread,
)

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "argus"

#: One scan cycle, small enough that a handful of them are a fraction of a second.
#: The tests below drive the watchdog's REAL `run()` loop, because the gate the old
#: stand-down closed lives there and not in `check_once`.
_TICK_S = 0.02
_FAST_SCAN = make_config(watchdog={"cadence_s": 0.001})


async def _end_entries(h) -> None:
    """Drive the guard-unreachable path all the way to the stand-down."""
    await _drive_streak(h, MAX_STREAK)
    assert h.entry.entries_ended is not None, "precondition: entries really ended"


def _dead_guard_harness():
    return build_entry(
        DeadGuardBroker(TastytradeError("502 Bad Gateway")), dry_run=False
    )


# =============================================================================
# 1. The watchdog, the exits and the flatten survive it
# =============================================================================


async def test_a_triggered_spread_is_still_exited_after_entries_end():
    """THE test. A held spread whose virtual stop fires keeps being closed.

    The watchdog and the entry executor share ONE `stop` event in production
    (`session._build_trading_side` passes the same object to both), which is what
    made this reachable at all.

    Driven through the watchdog's OWN `run()` loop rather than by calling
    `check_once` in a bare `for`, deliberately: the gate that the old stand-down
    closed is `while not self._stop.is_set()` in `run`, and `check_once` never reads
    the event. A test that calls `check_once` directly passes with the regression
    fully restored, which is a test of the wrong thing.
    """
    h = _dead_guard_harness()
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    executor = RecordingExecutor()
    watchdog, _audit, _alerts, _halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, config=_FAST_SCAN,
    )
    watchdog._stop = h.stop          # the production wiring: ONE event, both readers

    await _end_entries(h)

    _triggered_and_priceable(clock, market)
    watchdog.start()
    try:
        # The credit trigger, sustained past its debounce, on the loop's own cadence.
        for _ in range(8):
            clock.advance(1.5)
            _triggered_and_priceable(clock, market)
            await asyncio.sleep(_TICK_S)
        await watchdog._drain_exits()
    finally:
        await watchdog.stop()

    assert executor.calls, (
        "the virtual stop must still reach the exit executor; before 2026-09-01 the "
        "entry path's stand-down stopped the scan loop outright"
    )


async def test_the_flatten_still_fires_at_the_mark_after_entries_end():
    """`FLATTEN_BEFORE_CLOSE` is the trigger nothing can blind — except this.

    A winning 0DTE spread is nearly worthless by 15:30, which is what makes it
    winning; the flatten is how it gets closed rather than held into PM settlement.
    A held spread that lost it because the duplicate guard could not reach cert at
    11:00 is the live-flip concern this whole change exists for.
    """
    h = _dead_guard_harness()
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    executor = RecordingExecutor()
    watchdog, _audit, _alerts, _halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=executor, et_clock=FakeETClock(hour=15, minute=30),
        config=_FAST_SCAN,
    )
    watchdog._stop = h.stop

    await _end_entries(h)

    # Through `run()`, for the reason given on the test above.
    _quiet_and_priceable(market)                 # no PRICE trigger; the clock alone
    watchdog.start()
    try:
        for _ in range(4):
            clock.advance(1.5)
            _quiet_and_priceable(market)
            await asyncio.sleep(_TICK_S)
        await watchdog._drain_exits()
    finally:
        await watchdog.stop()

    assert len(executor.calls) == 1
    assert executor.calls[0][1] == frozenset({TriggerReason.FLATTEN_BEFORE_CLOSE})


async def test_an_exit_walk_in_flight_is_not_aborted():
    """The harm the finding did not name, and the sharpest of the three.

    `PriceWalkingLoop` reads the same event through `_stop_reason`, so the old
    stand-down cancelled a working CLOSE and returned `ABORTED` — which
    `ExitExecutor` maps to `NOT_RESOLVED`, which is not in `_RETRYABLE_EXITS`. The
    position's one exit attempt was therefore spent on the same tick as the monitor
    that would have paged about it was torn down.
    """
    h = _dead_guard_harness()
    walk = PriceWalkingLoop(
        execution=h.entry._execution,
        audit=h.audit,
        policy=WalkPolicy.for_exit(make_config()),
        guard=h.entry._guard,
        stop=h.stop,
    )

    await _end_entries(h)

    assert walk._stop_reason() is None, (
        "an exit already walking must keep walking; ending entries is not a reason "
        "to cancel the order closing a position"
    )
    h.stop.set()
    assert walk._stop_reason() == "stop requested", (
        "and the real close must still reach it — the event keeps its meaning"
    )


async def test_the_watchdog_scan_task_keeps_running():
    """Not just `check_once` when called: the loop that calls it."""
    h = _dead_guard_harness()
    clock = FakeClock()
    _market, _, quotes = build_quotes(clock)
    watchdog, _audit, _alerts, _halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
    )
    watchdog._stop = h.stop
    watchdog.start()
    try:
        await _end_entries(h)
        for _ in range(5):
            await asyncio.sleep(0)
        assert watchdog._task is not None and not watchdog._task.done(), (
            "the scan loop is `while not self._stop.is_set()`; ending entries "
            "through that event ended the loop"
        )
    finally:
        await watchdog.stop()


# =============================================================================
# 2. Entries really do end
# =============================================================================


async def test_no_further_candidate_is_routed_for_the_rest_of_the_day():
    """A DIFFERENT, perfectly good candidate gets nothing: no build, no guard call,
    no walk. The signal loop keeps proposing — that is deliberate, it is shadow's
    proven `submit=None` shape and the day's telemetry — and the entry seam declines.
    """
    h = _dead_guard_harness()
    await _end_entries(h)

    h.broker.remaining = 0                     # the broker recovers; nothing changes
    polls_before = h.broker.polls
    await h.submit(signal=_nth_candidate(9, h.clock.now))

    assert h.broker.submitted == [], "no order may be routed after the stand-down"
    assert h.broker.polls == polls_before, (
        "and the guard must not even be ASKED — that is what `no auto-retry` means"
    )
    payload = h.audit.payload("entry.entries_ended_today")
    assert payload["kind"] == "entry_guard_unreachable"
    assert payload["candidate_id"]


async def test_the_streak_is_not_reset_and_the_latch_never_clears():
    h = _dead_guard_harness()
    await _end_entries(h)
    first = h.entry.entries_ended

    for _ in range(5):
        await h.submit(signal=_nth_candidate(9, h.clock.now))

    assert h.entry._guard_unreachable_streak == MAX_STREAK, "the streak stands"
    assert h.entry.entries_ended is first, (
        "the latch is set once and never replaced; there is no path that clears it"
    )
    assert h.audit.events.count("entry.aborted_day") == 1, "one day-end, one record"
    assert h.audit.events.count("session.entries_ended") == 1


async def test_the_declined_tick_is_tier_2_and_the_edge_is_tier_1():
    """Volume shape, on `entry.already_attempted`'s precedent. The loop keeps
    proposing every `signal.cadence_s`, so a Tier 1 record per declined tick would
    out-volume every real decision. The crash-relevant fact — entries ended — is
    written once, Tier 1."""
    h = _dead_guard_harness()
    await _end_entries(h)
    await h.submit(signal=_nth_candidate(9, h.clock.now))

    assert "tier2:entry.entries_ended_today" in h.audit.events
    assert "session.entries_ended" in h.audit.events, "Tier 1, not telemetry"
    assert "tier2:session.entries_ended" not in h.audit.events


def test_the_refusal_sits_below_the_kill_switch_and_above_the_build():
    """Asserted on the AST, not on behaviour, so a reorder is caught even when both
    orders happen to pass today.

    Three things depend on the position. The kill switch is the operator's rail and
    nothing reorders in front of it. Nothing below may run, since `build_order` is
    the very thing that ended entries at one of the two call sites. And the guard
    must never be re-asked.
    """
    tree = ast.parse((_SRC / "execution" / "entry.py").read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "submit"
    )

    def _line_of(pred, what: str) -> int:
        lines = [n.lineno for n in ast.walk(fn) if pred(n)]
        assert lines, f"`submit` no longer references {what} at all"
        return min(lines)

    killswitch = _line_of(
        lambda n: isinstance(n, ast.Attribute) and n.attr == "engaged",
        "the kill switch",
    )
    latch = _line_of(
        lambda n: isinstance(n, ast.Attribute) and n.attr == "_entries_ended",
        "the entries-ended latch",
    )
    attempted = _line_of(
        lambda n: isinstance(n, ast.Attribute) and n.attr == "_attempted",
        "`_attempted`",
    )
    build = _line_of(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "build_order",
        "`build_order`",
    )
    assert killswitch < latch < attempted < build, (
        "order must be: kill switch, then the entries-ended latch, then "
        f"`_attempted`, then `build_order` (got {killswitch}, {latch}, "
        f"{attempted}, {build})"
    )


# =============================================================================
# 3. No durable state, and it can never become a second stop event
# =============================================================================


async def test_it_engages_nothing_and_writes_no_halt_state():
    h = _dead_guard_harness()
    await _end_entries(h)

    assert h.halt.calls == [], "no halt, of any grade"
    assert not h.killswitch.engaged, "and no kill switch — this clears by itself"
    assert "kill_switch.trip" not in h.audit.events


def test_the_latch_cannot_be_awaited_and_cannot_be_cleared():
    """The structural half of the argument that this is NOT the second stop event
    P3 rejected ("a second event means every one of those consumers picks a side,
    and picking wrong fails silently"). It narrows the ACTION, never the EVENT.
    """
    assert not hasattr(EntriesEnded, "wait"), "nothing may await it"
    assert not hasattr(EntriesEnded, "set")
    assert not hasattr(EntriesEnded, "clear"), "entries do not resume today"
    assert not hasattr(EntriesEnded, "is_set")
    assert EntriesEnded.__dataclass_params__.frozen

    prop = inspect.getattr_static(EntryExecutor, "entries_ended")
    assert isinstance(prop, property) and prop.fset is None, (
        "read-only: `_end_entries_today` is the only writer anywhere, and a setter "
        "is the first step toward a second caller narrowing the session"
    )


def test_the_entry_path_never_sets_the_session_stop_event():
    """The one-line regression, pinned where it cannot be re-introduced by a
    'restore the old behaviour' edit. `stop` stays a READ — it goes to the entry
    walk so the close reaches an order in flight, and it bounds the guard's retry
    backoff."""
    tree = ast.parse((_SRC / "execution" / "entry.py").read_text())
    sets = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "set"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_stop"
    ]
    assert sets == [], (
        "entry.py must never set the session stop event: the watchdog's scan loop, "
        "an exit walk in flight and the session teardown all read it"
    )


def test_the_latch_is_per_executor_so_it_cannot_outlive_a_trading_day():
    """The transience is STRUCTURAL — `EntryExecutor` is built once per trading day
    in `session._build_trading_side`, so tomorrow's starts at None. A class
    attribute or a home on the `Application` would make it durable by accident."""
    assert "_entries_ended" not in vars(EntryExecutor)
    a, b = _dead_guard_harness(), _dead_guard_harness()
    a.entry._entries_ended = EntriesEnded(
        kind="entry_build_failed", candidate_id="c", error_type="TypeError",
        since=datetime(2026, 9, 1, 11, 0, tzinfo=ET),
    )
    assert b.entry.entries_ended is None, "tomorrow starts clean, with no human step"


# =============================================================================
# 4. The day still ends at the real close
# =============================================================================


async def test_the_close_still_stops_everything():
    """The scheduler's own `session_stop` is untouched and still means what it
    always did — which is the other half of why the entry path must not set it."""
    h = _dead_guard_harness()
    clock = FakeClock()
    _market, _, quotes = build_quotes(clock)
    watchdog, _audit, _alerts, _halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
    )
    watchdog._stop = h.stop
    watchdog.start()
    try:
        await _end_entries(h)
        h.stop.set()                            # the ET close, as the scheduler sets it
        await asyncio.wait_for(watchdog._task, timeout=1.0)
    finally:
        await watchdog.stop()
    assert watchdog._task is None or watchdog._task.done()


# =============================================================================
# 5. Both call sites, identically
# =============================================================================


async def test_entry_build_failed_gets_the_same_posture():
    """A code defect, not a broker one — and if the defect is in `build_order` the
    EXIT path shares it (`watchdog._build_exit_order` calls the same function). That
    is an argument for keeping the watchdog, not against: a failing exit lands in
    the spent-attempt escalation, which is loud and visible on `/status`, where a
    torn-down watchdog is silent."""
    from argus.risk.models import EntryProposal

    h = build_entry()
    broken = EntryProposal(symbol="x", contracts=None, max_loss=840.0)
    await h.entry.submit(make_signal(captured_at=h.clock.now), broken, RISK)

    ended = h.entry.entries_ended
    assert ended is not None and ended.kind == "entry_build_failed"
    assert not h.stop.is_set(), "the watchdog survives a code defect too"
    assert h.halt.calls == [], "still SOFT"
    assert h.audit.payload("session.entries_ended")["kind"] == "entry_build_failed"

    # And it really does stop routing.
    await h.entry.submit(make_signal(captured_at=h.clock.now), PROPOSAL, RISK)
    assert h.broker.submitted == []


@pytest.mark.parametrize(
    "kind", ["entry_guard_unreachable", "entry_build_failed"]
)
async def test_the_edge_record_names_what_survives(kind):
    from argus.risk.models import EntryProposal

    if kind == "entry_guard_unreachable":
        h = _dead_guard_harness()
        await _end_entries(h)
    else:
        h = build_entry()
        await h.entry.submit(
            make_signal(captured_at=h.clock.now),
            EntryProposal(symbol="x", contracts=None, max_loss=840.0),
            RISK,
        )

    payload = h.audit.payload("session.entries_ended")
    assert payload["kind"] == kind
    assert payload["entries"] == "ended"
    assert payload["watchdog"] == "running"
    assert payload["clears"] == "next_trading_day"
    assert payload["trading_date"]
    assert "entries_ended_today" in h.alerts, (
        "a DISTINCT alert kind, on `_abandon_guard`'s stated rule: read on a phone, "
        "'the day is over' and 'entries are done, the watchdog is still working' "
        "must not look alike"
    )


# =============================================================================
# 6. /status — and how it differs from P3's PERMANENT monitor-only day
# =============================================================================


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "argus.db")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


_ENDED = EntriesEndedToday(
    kind="entry_guard_unreachable",
    candidate_id="SPXW-20260901-PUT-5",
    error_type="TastytradeError",
    since=datetime(2026, 9, 1, 11, 42, 7, tzinfo=ET),
)


async def _status(db, ended, *, halt_state=lambda: None):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit, halt_state=halt_state)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0, positions=2),
        entries_ended_source=(None if ended is None else (lambda: ended)),
    )
    try:
        snap = await svc.assemble()
        return snap, snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()


async def test_live_entries_render_no_row_at_all(db):
    snap, js, html_out = await _status(db, None)
    assert snap.entries_ended is None
    assert js["entries_ended"] is None
    assert "ENDED FOR TODAY" not in html_out


async def test_a_stood_down_day_says_what_clears_it(db):
    """THE sentence. The operator at 15:00 on expiry day already knows entries are
    off; what they need to know is whether they have to do something tonight."""
    snap, js, html_out = await _status(db, _ENDED)

    assert snap.phase == "running", "the session IS running; the row qualifies it"
    assert not snap.halted
    assert js["entries_ended"]["kind"] == "entry_guard_unreachable"
    assert js["entries_ended"]["clears"] == "next_trading_day"
    assert js["entries_ended"]["watchdog"] == "running"
    assert "ENDED FOR TODAY — watchdog still running" in html_out
    assert "Clears automatically at the next trading day" in html_out
    assert "still watched, closed and flattened" in html_out
    assert "restart required" not in html_out.lower(), (
        "that is HALTED's clause, and confusing the two is the whole risk"
    )


async def test_it_is_rendered_differently_from_P3s_permanent_monitor_only(db):
    """Same runtime posture, opposite clearing rule. One `monitor_only` flag would
    answer 'are entries off?' and lose 'what clears it'."""
    from argus.app import HaltRecord
    from argus.reconcile import HaltGrade

    halted = HaltRecord(
        reason="broker reconciliation mismatch (1 discrepancy(ies))",
        source="reconciliation_mismatch",
        at=NOW,
        grade=HaltGrade.ENTRIES,
    )
    _snap, js, html_out = await _status(db, None, halt_state=lambda: halted)

    assert js["halted"]["grade"] == "entries"
    assert js["entries_ended"] is None, "two fields, never one"
    assert "NO ENTRIES — watchdog still running" in html_out
    assert "estart required once resolved" in html_out
    assert "ENDED FOR TODAY" not in html_out


async def test_both_can_render_at_once_and_halted_comes_first(db):
    """A transient stand-down followed by a halt. The reverse cannot arise: a halted
    process comes up with `submit=None`, so the entry path is never called."""
    from argus.app import HaltRecord
    from argus.reconcile import HaltGrade

    halted = HaltRecord(
        reason="watchdog failed", source="watchdog", at=NOW, grade=HaltGrade.ALL
    )
    _snap, js, html_out = await _status(db, _ENDED, halt_state=lambda: halted)

    assert js["halted"]["halted"] and js["entries_ended"] is not None
    assert html_out.index("WILL NOT TRADE") < html_out.index("ENDED FOR TODAY"), (
        "the stronger claim reads first"
    )


async def test_the_row_clears_when_the_session_ends(db):
    """Same rule as the spent-exit row: it is a handle into a per-session object.
    The day's record survives where it belongs — the counts block and the audit
    tail both outlive the session."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0, positions=2),
        entries_ended_source=lambda: _ENDED,
    )
    try:
        assert (await svc.assemble()).entries_ended is not None
        ss.session_finished(ended=True)
        snap = await svc.assemble()
        assert snap.entries_ended is None
        assert snap.phase == "ended"
    finally:
        await audit.aclose()
