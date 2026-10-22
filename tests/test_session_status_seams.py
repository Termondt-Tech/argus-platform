"""The seams that carry live trading-side state onto `/status`.

`status.py` deliberately imports nothing from `execution/`, so every live watchdog
row on the page crosses a converter in `session.py`: the watchdog produces an
`ImpairedPosition` / `SpentExitAttempt`, the session copies it into the
`PositionEvaluability` / `UnclosablePosition` the snapshot renders. The copy is the
only place those two halves meet, and it is the one place nothing was testing.

**The defect these tests exist for.** The `spent_exits` seam was written on
2026-08-21 (A2b) with the five fields `SpentExitAttempt` had then. Three more —
`cost_to_close`, `credit_stop`, `still_triggered` — were added to it on 2026-08-28,
by the pass whose entire point was that a held, triggered spread had gone 76 minutes
with no surface able to say what it was worth. The seam was never updated, and
`UnclosablePosition` declares all three with `None` defaults, so nothing raised and
nothing warned: `/status` rendered `mark: unpriced` for every spent exit, always, for
five days. `tests/test_status.py` covered the row thoroughly and hand-built the
`UnclosablePosition` with the fields populated — which is exactly the construction
that cannot see this.

So both halves of the coverage here are deliberate:

  - the BEHAVIOURAL tests drive a real `PositionWatchdog` all the way to rendered
    HTML and never construct a status-layer row by hand;
  - the STRUCTURAL sweep pins the shape — each converter's target construction must
    name every field of its source type — which is what catches the NEXT field added
    to a source dataclass, on the day it is added rather than five days later.

Neither alone is enough. The sweep cannot see a mis-wired copy
(`credit_stop=p.cost_to_close` names every field and is wrong); the behavioural
tests cannot see a field nobody has thought to assert on yet.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect

import argus.execution.watchdog as wd
import argus.session as session_mod
import argus.status as status
from argus.audit.log import AuditLog
from argus.execution.entry import EntriesEnded
from argus.execution.residual_orders import OutstandingResidual
from argus.execution.watchdog import (
    ImpairedPosition,
    SpentExitAttempt,
    UnwatchablePosition,
)
from argus.status import (
    EntriesEndedToday,
    PositionEvaluability,
    ResidualOrderRow,
    UnclosablePosition,
    UnmanagedPosition,
)

from tests.test_status import NOW, _make_service, _risk
from tests.test_watchdog import (
    LONG_STREAM,
    SHORT_STREAM,
    ExitOutcome,
    FakeClock,
    FakeSpreadSource,
    OpenSpreadScan,
    RecordingExecutor,
    _degraded_tick,
    _spend_the_attempt,
    _triggered_and_priceable,
    build_quotes,
    build_watchdog,
    spread,
)


# -- driving the real seam ----------------------------------------------------


async def _render(db, *, watchdog=None):
    """Assemble a snapshot with the session's OWN converters wired to `watchdog`.

    The two sources are built exactly as `run_session` builds them — the module-level
    converter applied to the watchdog's live accessor — so what is under test is the
    thing that actually runs, not a re-statement of it in the test. `run_session`
    itself needs a broker login and cannot be driven here; that its closures call
    these converters, inside the `trading is not None` guard, is pinned structurally
    in `tests/test_session_trading_side.py`.
    """
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0, positions=2),
        evaluability_source=(
            None if watchdog is None
            else (lambda: session_mod._to_evaluability(watchdog.impaired_positions()))
        ),
        spent_exit_source=(
            None if watchdog is None
            else (lambda: session_mod._to_unclosable(watchdog.spent_exit_attempts()))
        ),
        watchdog_health_source=(
            None if watchdog is None
            else (lambda: session_mod._to_watchdog_health(watchdog.watchdog_health()))
        ),
        profit_target_source=(
            None if watchdog is None
            else (lambda: session_mod._to_profit_target(watchdog.profit_target_state()))
        ),
    )
    try:
        snap = await svc.assemble()
        return snap, snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()


async def _spent_watchdog(*, priced: bool):
    """A watchdog holding one position whose single exit attempt is spent.

    `priced` decides whether a further evaluation happens after the attempt, which is
    what produces a mark. Unpriced is not a contrivance — it is the state the row was
    in for the whole 76-minute episode that produced the mark fields.
    """
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.NOT_RESOLVED)
    if priced:
        # The market comes back INSIDE the stop. Nothing acts on it — the attempt is
        # gone — but it is the reading that changes what a human should do, and the
        # one `outcome` alone cannot express.
        clock.advance(1.5)
        market.set(SHORT_STREAM, 1.00, 1.10)
        market.set(LONG_STREAM, 0.30, 0.50)
        market.set("SPX", 5100.0, 5100.0)
        await watchdog.check_once()
    clock.advance(900.0)
    return watchdog


async def test_the_spent_exit_row_carries_the_watchdog_s_own_mark(db):
    """THE regression. Everything below was true of the watchdog since 2026-08-28 and
    reached `/status` as `mark: unpriced` regardless, because the seam copied five of
    eight fields.

    Asserted from a real watchdog through to the rendered page: no `UnclosablePosition`
    is constructed anywhere in this test, which is the only way the defect is visible.
    """
    watchdog = await _spent_watchdog(priced=True)

    # What the watchdog itself holds — the input side of the seam.
    (source,) = watchdog.spent_exit_attempts()
    assert (source.cost_to_close, source.credit_stop) == (0.65, 1.70)
    assert source.still_triggered is False

    _snap, js, html_out = await _render(db, watchdog=watchdog)

    (row,) = js["watchdog"]["spent_exit_attempts"]
    assert (row["cost_to_close"], row["credit_stop"]) == (0.65, 1.70)
    assert row["still_triggered"] is False
    # And the five that were already crossing, so a "fix" that swapped one set for
    # the other is not mistaken for this one.
    assert row["candidate_id"] == "20260729-put-x"
    assert row["external_identifier"] == spread().external_identifier
    assert row["outcome"] == "not_resolved"
    assert row["contracts"] == 2
    # 900s idle plus the 1.5s tick that produced the mark, on the watchdog's own
    # monotonic clock — measured from the ATTEMPT, so it never restarts.
    assert row["spent_for_s"] == 901.5

    # The page, which is what an operator on a phone actually reads.
    assert "mark: 0.65 vs stop 1.70" in html_out
    assert "back inside the stop" in html_out
    assert "mark: unpriced" not in html_out


async def test_a_still_triggered_mark_reads_as_the_emergency_it_is(db):
    """The third `still_triggered` state, and the one the row exists for: the stop
    fired, nothing automated will try again, and it is STILL through."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _triggered_and_priceable(clock, market)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED),
    )
    await _spend_the_attempt(watchdog, clock, market, ExitOutcome.NOT_RESOLVED)
    clock.advance(1.5)
    _triggered_and_priceable(clock, market)
    await watchdog.check_once()

    _snap, js, html_out = await _render(db, watchdog=watchdog)
    assert js["watchdog"]["spent_exit_attempts"][0]["still_triggered"] is True
    assert "STILL THROUGH THE STOP" in html_out


async def test_an_unpriced_tick_still_reaches_the_page_as_unpriced(db):
    """The other direction, and it must not regress while the fix lands: `None` means
    that tick could not price the position. It renders as `unpriced` — never as a
    placeholder number, on a row whose whole purpose is telling a human what to do by
    hand."""
    watchdog = await _spent_watchdog(priced=False)
    assert watchdog.spent_exit_attempts()[0].cost_to_close is None

    _snap, js, html_out = await _render(db, watchdog=watchdog)
    row = js["watchdog"]["spent_exit_attempts"][0]
    assert (row["cost_to_close"], row["credit_stop"], row["still_triggered"]) == (
        None, None, None,
    )
    assert "mark: unpriced" in html_out


async def test_the_evaluability_row_carries_every_field_through_the_same_seam(db):
    """The sibling seam, checked the same way rather than trusted. It copies all five
    of `ImpairedPosition`'s fields and is complete — this is what says so on the day
    a sixth is added."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),))
    )
    await _degraded_tick(watchdog, market)
    clock.advance(1200.0)
    await _degraded_tick(watchdog, market)

    _snap, js, html_out = await _render(db, watchdog=watchdog)
    (row,) = js["watchdog"]["impaired_positions"]
    assert row["external_identifier"] == spread().external_identifier
    assert row["candidate_id"] == "20260729-put-x"
    assert row["unknown_reasons"] == ["credit_multiple", "profit_target"]
    assert row["blind"] is False
    assert row["impaired_for_s"] == 1200.0
    assert "degraded" in html_out and "20m 0s" in html_out


async def test_no_watchdog_still_publishes_neither_row(db):
    """Shadow. The seam is not wired at all, and both rows must read "not
    applicable" rather than the calm empty tuple a live watchdog produces."""
    snap, js, html_out = await _render(db, watchdog=None)
    assert (snap.impaired_positions, snap.spent_exit_attempts) == (None, None)
    assert (snap.watchdog_health, js["watchdog"]["health"]) == (None, None)
    assert js["watchdog"] == {
        "health": None,
        "impaired_positions": None,
        "spent_exit_attempts": None,
        # Shadow has no switch to report and nothing it could have acted on
        # (2026-09-23) — null, never `enabled: false`, which would be a claim.
        "profit_target": None,
        # Same for the flatten deferral (2026-09-25): no watchdog, no mode.
        "flatten_deferral": None,
    }
    assert "no watchdog" in html_out


async def test_the_status_row_names_a_degraded_watchdog(db):
    """T16 (2026-09-10). A real `PositionWatchdog` driven to failure, through the
    session's own converters, to the rendered page: DEGRADED, in red, with the
    error type — because a watchdog exception no longer parks the process, and
    `phase: running` is no longer evidence that anything is being watched."""

    class Broken:
        async def open_spreads(self, *, today):
            raise RuntimeError("ledger read blew up")

    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    watchdog, _, _, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
    )
    watchdog._spreads = Broken()
    await watchdog.check_once()
    await watchdog._settle_pass()

    _snap, js, html_out = await _render(db, watchdog=watchdog)
    health = js["watchdog"]["health"]
    assert health["degraded"] is True
    assert health["error_type"] == "RuntimeError"
    assert health["failures"] == 1
    assert "DEGRADED" in html_out
    assert "RuntimeError" in html_out
    assert "ledger read blew up" in html_out
    # THE sentence: everything below this row is as of the last clean pass.
    assert "as of the last clean pass" in html_out


async def test_the_status_row_says_recovered_while_still_halted(db):
    """T17 (2026-09-10). The amber third state, end to end. The halt is permanent
    and the failure is not, so without this row the operator sees a healthy
    watchdog and a HALTED badge whose reason string is the only surviving trace
    of why."""

    class Broken:
        async def open_spreads(self, *, today):
            raise RuntimeError("ledger read blew up")

    clock = FakeClock()
    _, _, quotes = build_quotes(clock)
    watchdog, _, _, halts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
    )
    watchdog._spreads = Broken()
    await watchdog.check_once()
    await watchdog._settle_pass()
    assert len(halts) == 1

    # The read recovers; the next pass is clean. The halt is NOT reversed.
    watchdog._spreads = FakeSpreadSource(OpenSpreadScan(spreads=(spread(),)))
    watchdog.request_refresh()
    await watchdog.check_once()
    await watchdog._settle_pass()
    assert len(halts) == 1

    _snap, js, html_out = await _render(db, watchdog=watchdog)
    health = js["watchdog"]["health"]
    assert (health["degraded"], health["failures"]) == (False, 1)
    assert health["last_failure_for_s"] is not None
    assert "RECOVERED" in html_out
    assert "entries remain halted" in html_out
    assert "POST /resume" in html_out


# -- the structural sweep -----------------------------------------------------
#
# What the behavioural tests above cannot cover: a field added to a source dataclass
# that nobody has written an assertion for yet. That is precisely how the mark went
# missing — the field arrived, the seam did not change, and no existing test knew to
# look. `credit_payload()` and `reject_reason` are already held this way; a silent
# under-copy between two dataclasses with `None` defaults is the same class of defect
# and deserves the same kind of pin.

#: converter name -> (source dataclass, target dataclass). Every module-level
#: converter in `session.py`'s `/status` seam block belongs here.
_SEAMS = {
    "_to_unclosable": (SpentExitAttempt, UnclosablePosition),
    "_to_evaluability": (ImpairedPosition, PositionEvaluability),
    "_to_entries_ended": (EntriesEnded, EntriesEndedToday),
    # Added 2026-09-02 with the `unmanaged` section. The completeness test below
    # FAILED on the commit that introduced them, before either was written into this
    # map — which is the whole reason it exists.
    "_to_unmanaged_positions": (UnwatchablePosition, UnmanagedPosition),
    "_to_residual_rows": (OutstandingResidual, ResidualOrderRow),
    # Added 2026-09-10 with the Watchdog row. `test_the_sweep_covers_every_converter_
    # in_the_seam_block` fails until this entry exists — which is the mechanism
    # working, exactly as it did for the two above.
    "_to_watchdog_health": (wd.WatchdogHealth, status.WatchdogHealth),
    # Added 2026-09-23 with the Profit target row: the state and its nested rows,
    # each through its own converter so neither is an unswept inline copy.
    "_to_profit_target": (wd.ProfitTargetState, status.ProfitTargetStatus),
    "_to_retired_profit_target": (wd.RetiredProfitTarget, status.RetiredProfitTargetRow),
    # Added 2026-09-25 with the Flatten deferral row: the state and its rows, each
    # through its own converter, on the profit target's pattern.
    "_to_flatten_deferral": (wd.FlattenDeferralState, status.FlattenDeferralStatus),
    "_to_flatten_deferral_row": (wd.FlattenDeferralRow, status.FlattenDeferralRowStatus),
}


def _target_keywords(converter_name: str, target_name: str) -> set[str]:
    """Keyword names of the `Target(...)` call inside the named converter."""
    tree = ast.parse(inspect.getsource(getattr(session_mod, converter_name)).lstrip())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == target_name
    ]
    assert len(calls) == 1, f"{converter_name} must build exactly one {target_name}"
    (call,) = calls
    assert not call.args, f"{converter_name} must pass every field by keyword"
    assert all(kw.arg is not None for kw in call.keywords), (
        f"{converter_name} must not use **kwargs — the point is naming every field"
    )
    return {kw.arg for kw in call.keywords}


def test_every_status_seam_names_every_field_of_its_source():
    """The defect, generalized: a source dataclass gains a field, the seam does not,
    and the target's `None` default swallows it at every layer.

    Field-for-field is the contract for these converters specifically. They exist
    only because `status.py` must not import from `execution/` — there is no
    filtering or shaping to do, so anything the source knows the page is entitled to,
    and a deliberate omission would need to be argued for here rather than merely
    happening.
    """
    for converter, (source, target) in _SEAMS.items():
        source_fields = {f.name for f in dataclasses.fields(source)}
        target_fields = {f.name for f in dataclasses.fields(target)}
        assert source_fields == target_fields, (
            f"{source.__name__} and {target.__name__} have drifted apart: "
            f"{source_fields ^ target_fields}"
        )
        assert _target_keywords(converter, target.__name__) == source_fields, (
            f"session._{converter} does not copy every field of {source.__name__} "
            "— the mark was lost this way on 2026-08-28"
        )


def test_the_sweep_covers_every_converter_in_the_seam_block():
    """Otherwise Task 3's new seams (an unwatchable position, a residual order) get
    added beside these and inherit none of the protection — which is exactly how the
    block acquired one uncovered converter in the first place."""
    converters = {
        name
        for name, obj in vars(session_mod).items()
        if name.startswith("_to_") and inspect.isfunction(obj)
        and obj.__module__ == session_mod.__name__
    }
    assert converters == set(_SEAMS), (
        "a converter was added to session.py's /status seam block without being "
        f"added to _SEAMS: {converters ^ set(_SEAMS)}"
    )
