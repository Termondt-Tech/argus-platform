"""`/status` names what Argus is holding but not managing.

Two states in which Argus holds something nothing is driving, and until 2026-09-02
each paged Tier 1 once per episode and then appeared **nowhere** on the page:

  - an **UNWATCHABLE position** (`OpenSpreadScan.unattributed` / `.malformed`).
    `check_once` iterates `self._scan.spreads`, so such a position gets no trigger
    evaluated, no mark, and **no `FLATTEN_BEFORE_CLOSE`** — on 0DTE it rides to
    settlement — while still consuming a concurrency slot through the broker's leg
    count. It is strictly worse than a BLIND impaired position or a spent exit
    attempt, both of which the page already names.
  - a **residual order** (P5's `ResidualOrderRecheck`), left working at the broker by
    a finished walk.

The third and fourth instances of one pattern this page has already been fixed for
twice — `daily_loss.breach` read off a log row, and a spent exit rendering as an
ordinary open position — and closed as ONE decision, because the open decision asks
for one.

Design: `docs/designs/2026-09-02-status-unmanaged-positions.md`.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from argus.audit.log import AuditLog
from argus.execution.models import OrderDirection
from argus.execution.residual_orders import ResidualSighting
from argus.execution.store import OpenSpreadScan
from argus.execution.watchdog import PositionWatchdog
from argus.session import _to_residual_rows, _to_unmanaged_positions
from argus.status import (
    MAX_UNMANAGED_ROWS,
    MAX_UNMANAGED_SYMBOLS,
    _RESIDUAL_READ_GAPS,
)

from tests.test_residual_orders import OPEN_ID, make_recheck, register
from tests.test_status import NOW, _make_service, _risk
from tests.test_watchdog import (
    FakeClock,
    build_quotes,
    build_watchdog,
    spread,
)

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "argus"

#: One malformed entry of the shape `store.py` writes for a lopsided remainder — the
#: one that carries per-symbol counts, so the flattening is exercised rather than
#: assumed.
LOPSIDED = {
    "broker_order_id": 4242,
    "external_identifier": "argus-open-20260902-put-5000-4995",
    "candidate_id": "20260902-put-5000-4995",
    "problem": "outstanding legs are not one short and one long",
    "outstanding": {"SPXW  260902P05000000": 2},
}
DIVERGENT = {
    "broker_order_id": 4243,
    "external_identifier": "argus-open-20260902-call-5100-5105",
    "candidate_id": "20260902-call-5100-5105",
    "problem": "ledger holdings disagree with the positions table",
    "divergent": {"SPXW  260902C05100000": {"attributed": 2, "positions": 1}},
}
GHOST = "SPXW  260902P04900000"


# -- driving the real seams ---------------------------------------------------


async def _render(db, *, watchdog=None, recheck=None):
    """Assemble a snapshot with the session's OWN converters wired to live objects.

    Built exactly as `run_session` builds them — the module-level converter applied to
    the accessor — so what is under test is the thing that runs. `run_session` itself
    needs a broker login; that its closures call these converters inside the
    `trading is not None` guard is pinned structurally in
    `tests/test_session_trading_side.py`.
    """
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0, positions=2),
        unmanaged_source=(
            None if watchdog is None
            else (lambda: _to_unmanaged_positions(watchdog.unwatchable_positions()))
        ),
        residual_source=(
            None if recheck is None
            else (lambda: _to_residual_rows(recheck.residual_rows()))
        ),
    )
    try:
        snap = await svc.assemble()
        return snap, snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()


async def _scanned(scan: OpenSpreadScan, *, advance: float = 0.0):
    """A real `PositionWatchdog` that has seen `scan`, with its episode state set.

    One `check_once` is what makes this real rather than a fixture: it runs
    `_refresh_if_due`, which sets `self._scan` AND runs `_report_unwatchable`, which is
    where `_unwatchable_since` comes from. Reading the accessor off a watchdog that had
    never scanned would be asserting on a constructor.
    """
    clock = FakeClock()
    _market, _dir, quotes = build_quotes(clock)
    watchdog, _audit, _halts, _alerts = build_watchdog(
        clock=clock, quotes=quotes, scan=scan
    )
    await watchdog.check_once()
    if advance:
        clock.advance(advance)
    return watchdog, clock


def _cell(html_out: str, label: str) -> str:
    return html_out[html_out.index(label):].split("</tr>")[0]


# -- unwatchable positions ----------------------------------------------------


async def test_a_positions_row_no_order_explains_is_named(db):
    """The headline. A held position Argus's own two records cannot reconcile, which
    until now showed only as a consumed concurrency slot with no explanation."""
    watchdog, _clock = await _scanned(
        OpenSpreadScan(spreads=(spread(),), unattributed=(GHOST,)), advance=812.0
    )
    _snap, js, html_out = await _render(db, watchdog=watchdog)

    (row,) = js["unmanaged"]["positions"]
    assert row["kind"] == "unattributed"
    assert row["symbols"] == [GHOST]
    assert row["unwatchable_for_s"] == 812.0
    assert row["settling"] is False
    assert row["candidate_id"] is None

    cell = _cell(html_out, "Unwatched positions")
    assert GHOST in cell
    assert "NOT WATCHED" in cell and "class='bad'" in cell
    assert "13m 32s" in cell


async def test_the_row_says_what_such_a_position_does_not_get(db):
    """The words are the part that is not obvious. A reader who knows the watchdog is
    running will assume a stop, a mark and the 15:30 flatten, because for every other
    position on this page all three hold. This is the only row that says otherwise."""
    watchdog, _clock = await _scanned(OpenSpreadScan(unattributed=(GHOST,)))
    _snap, _js, html_out = await _render(db, watchdog=watchdog)
    cell = _cell(html_out, "Unwatched positions")
    assert "no stop, no mark, NO 15:30 flatten" in cell
    assert "needs a human" in cell


async def test_a_malformed_order_carries_its_problem_verbatim(db):
    """`store.py`'s sentence IS the operator's diagnosis, so it is never truncated —
    reintroducing the audit tail's 240-char cut here would defeat the reason a live
    row exists at all."""
    watchdog, _clock = await _scanned(OpenSpreadScan(malformed=(LOPSIDED,)), advance=60.0)
    _snap, js, html_out = await _render(db, watchdog=watchdog)

    (row,) = js["unmanaged"]["positions"]
    assert row["kind"] == "malformed"
    assert row["problem"] == "outstanding legs are not one short and one long"
    assert row["candidate_id"] == "20260902-put-5000-4995"
    assert row["broker_order_id"] == 4242
    assert row["detail"] == [["SPXW  260902P05000000", "2 still held"]]

    cell = _cell(html_out, "Unwatched positions")
    assert "outstanding legs are not one short and one long" in cell
    assert "2 still held" in cell


async def test_the_divergent_shape_renders_both_numbers(db):
    """"the ledger says 2 and `positions` says 1" is the diagnosis, not a detail."""
    watchdog, _clock = await _scanned(OpenSpreadScan(malformed=(DIVERGENT,)), advance=60.0)
    _snap, js, html_out = await _render(db, watchdog=watchdog)
    assert js["unmanaged"]["positions"][0]["detail"] == [
        ["SPXW  260902C05100000", "ledger 2 / positions 1"]
    ]
    assert "ledger 2 / positions 1" in _cell(html_out, "Unwatched positions")


async def test_a_shape_with_no_per_symbol_numbers_yields_no_detail(db):
    """Four of the six `malformed` shapes carry none, and `()` is the right answer for
    them rather than an invented placeholder. Tolerant by construction: a `/status`
    read must not be able to fail on a record that is already malformed."""
    watchdog, _clock = await _scanned(
        OpenSpreadScan(malformed=({"candidate_id": "x",
                                   "problem": "closing fills with no opening order on record"},))
    )
    _snap, js, _html = await _render(db, watchdog=watchdog)
    row = js["unmanaged"]["positions"][0]
    assert (row["detail"], row["symbols"]) == ([], [])
    assert row["broker_order_id"] is None and row["external_identifier"] is None


async def test_a_malformed_only_condition_inside_the_settle_allowance_is_amber(db):
    """The mid-fill window: the two legs of one multi-leg order book ~0.25s apart, so
    the watchdog aims straight at a moment where a spread legitimately has one applied
    leg. `_report_unwatchable` suppresses its own page for exactly this, and colouring
    it red would be the false positive the link row was written to avoid."""
    watchdog, _clock = await _scanned(OpenSpreadScan(malformed=(LOPSIDED,)))
    _snap, js, html_out = await _render(db, watchdog=watchdog)
    assert js["unmanaged"]["positions"][0]["settling"] is True

    cell = _cell(html_out, "Unwatched positions")
    assert "settling" in cell and "class='warn'" in cell
    assert "class='bad'" not in cell
    # And the alarming sentence is withheld, because nothing is wrong yet.
    assert "NO 15:30 flatten" not in cell


async def test_an_unattributed_symbol_is_never_settling_at_any_age(db):
    """`_report_unwatchable`'s asymmetry, verbatim: there is no pending second leg
    that would resolve an unattributed row, so an allowance would only delay the
    answer. Asserted at zero age, which is where a shared allowance would show."""
    watchdog, _clock = await _scanned(
        OpenSpreadScan(unattributed=(GHOST,), malformed=(LOPSIDED,))
    )
    _snap, js, html_out = await _render(db, watchdog=watchdog)
    rows = {r["kind"]: r for r in js["unmanaged"]["positions"]}
    assert rows["unattributed"]["settling"] is False
    # And it suppresses the allowance for the malformed entry beside it, exactly as
    # the page's own condition does — one episode, one verdict.
    assert rows["malformed"]["settling"] is False
    assert "class='bad'" in _cell(html_out, "Unwatched positions")


async def test_the_age_is_the_episode_s_and_survives_a_refresh(db):
    """`_unwatchable_since` restarts only when the SIGNATURE changes, so "unexplained
    for 20 minutes and still not clearing" is true across every intervening scan. The
    same property `impaired_for_s` is load-bearing for one row over."""
    watchdog, clock = await _scanned(OpenSpreadScan(unattributed=(GHOST,)))
    clock.advance(1200.0)
    watchdog.request_refresh()
    await watchdog.check_once()

    _snap, js, _html = await _render(db, watchdog=watchdog)
    assert js["unmanaged"]["positions"][0]["unwatchable_for_s"] == 1200.0


async def test_the_row_clears_when_the_scan_explains_everything_again(db):
    """Pairs `watchdog.watchable_again`. A row that outlived its cause would be the
    process-lifetime-ledger failure this page has already been fixed for twice."""
    watchdog, clock = await _scanned(OpenSpreadScan(unattributed=(GHOST,)))
    _snap, js, _html = await _render(db, watchdog=watchdog)
    assert len(js["unmanaged"]["positions"]) == 1

    watchdog._spreads.scan = OpenSpreadScan(spreads=(spread(),))
    clock.advance(1.0)
    watchdog.request_refresh()
    await watchdog.check_once()

    _snap, js, html_out = await _render(db, watchdog=watchdog)
    assert js["unmanaged"]["positions"] == []
    assert "all explained" in _cell(html_out, "Unwatched positions")


# -- residual orders ----------------------------------------------------------


async def test_a_residual_order_is_named_with_what_an_operator_needs(db):
    recheck, _audit, clock, _cancel, _alerts = make_recheck(cancel_enabled=False)
    register(recheck, broker_order_id=1234, walk_outcome="walked_away",
             cancel_requested=True)
    clock.now += 240.0

    _snap, js, html_out = await _render(db, recheck=recheck)
    (row,) = js["unmanaged"]["orders"]
    assert row["broker_order_id"] == 1234
    assert row["external_identifier"] == OPEN_ID
    assert row["candidate_id"] == "20260828-put-5000-4995"
    assert row["minted_direction"] == "open"
    assert row["walk_outcome"] == "walked_away"
    assert row["cancel_requested"] is True
    assert row["working_for_s"] == 240.0

    cell = _cell(html_out, "Residual orders")
    assert "order 1234 (open)" in cell
    assert "working 4m 0s" in cell
    assert "class='warn'" in cell and "class='bad'" not in cell


@pytest.mark.parametrize(
    ("enabled", "mode", "phrase"),
    [
        (False, "would_cancel", "OBSERVER MODE"),
        (True, "cancel", "auto-cancel ON"),
    ],
)
async def test_the_row_says_which_mode_the_recheck_is_in(db, enabled, mode, phrase):
    """The flip is a CONFIG change with no deploy, so this is the only place an
    operator can confirm which mode is actually live — and while `would_cancel` is the
    shipped mode, this row and `order.residual_would_cancel` telemetry are the only
    surfaces showing what the recheck would have done."""
    recheck, _audit, _clock, _cancel, _alerts = make_recheck(cancel_enabled=enabled)
    register(recheck)

    _snap, js, html_out = await _render(db, recheck=recheck)
    assert js["unmanaged"]["cancel_mode"] == mode
    assert phrase in _cell(html_out, "Residual orders")


async def test_a_read_gap_escalates_the_row_to_red(db):
    """P5's rule: absence is not a verdict in either direction. The broker will neither
    confirm the order dead nor licence a cancel, so nothing is progressing — which is a
    different and less tractable problem than a cancel that failed."""
    recheck, _audit, _clock, _cancel, _alerts = make_recheck()
    register(recheck)
    recheck.outstanding()[0].last_sighting = str(ResidualSighting.AMBIGUOUS)

    _snap, js, html_out = await _render(db, recheck=recheck)
    assert js["unmanaged"]["orders"][0]["last_sighting"] == "ambiguous"
    cell = _cell(html_out, "Residual orders")
    assert "class='bad'" in cell
    assert "neither confirm it dead nor licence a cancel" in cell


async def test_a_closing_residual_is_shown_and_never_promised_a_cancel(db):
    """`HaltCheckFn`'s asymmetry, verbatim: cancelling a close strands a position, so a
    closing residual is watched and paged and never auto-cancelled — in EITHER mode,
    which the summary line alone does not say."""
    recheck, _audit, _clock, _cancel, _alerts = make_recheck(cancel_enabled=True)
    register(recheck, identifier=OPEN_ID.replace("-open-", "-close-"),
             direction=OrderDirection.CLOSE)

    _snap, js, html_out = await _render(db, recheck=recheck)
    assert js["unmanaged"]["orders"][0]["minted_direction"] == "close"
    cell = _cell(html_out, "Residual orders")
    assert "closing order: never auto-cancelled" in cell
    assert "auto-cancel ON" in cell  # the mode is still reported honestly


async def test_the_direction_is_the_minted_one_never_the_brokers(db):
    """The broker relabels leg actions, so `Order.direction` is the wrong source here.
    An AST test in `tests/test_residual_orders.py` already pins that the module reads
    no `.direction`; this pins that the value reaching the PAGE is the minted one."""
    recheck, _audit, _clock, _cancel, _alerts = make_recheck()
    register(recheck, direction=OrderDirection.OPEN)
    (row,) = recheck.residual_rows()
    assert row.minted_direction == "open"


async def test_the_row_clears_when_the_residual_resolves(db):
    recheck, _audit, _clock, _cancel, _alerts = make_recheck()
    register(recheck)
    _snap, js, _html = await _render(db, recheck=recheck)
    assert len(js["unmanaged"]["orders"]) == 1

    recheck._residuals.clear()          # what `_resolve` does on a DEAD/FILLED sighting
    _snap, js, html_out = await _render(db, recheck=recheck)
    assert js["unmanaged"]["orders"] == []
    assert js["unmanaged"]["cancel_mode"] is None
    assert "none working" in _cell(html_out, "Residual orders")


# -- the three-state contract -------------------------------------------------


async def test_no_session_renders_null_not_empty(db):
    """Shadow, or a `HaltGrade.ALL` parked process. `null` and `[]` must not look
    alike: one says nothing is being watched, the other says everything held is
    explained. The HALTED row above is what says which."""
    snap, js, html_out = await _render(db)
    assert (snap.unmanaged_positions, snap.residual_orders) == (None, None)
    assert js["unmanaged"] == {"positions": None, "orders": None, "cancel_mode": None}
    cell = _cell(html_out, "Unwatched positions")
    assert "no watchdog" in cell
    assert "all explained" not in cell


async def test_a_live_watchdog_with_nothing_unmanaged_says_so(db):
    watchdog, _clock = await _scanned(OpenSpreadScan(spreads=(spread(),)))
    recheck, _audit, _clock2, _cancel, _alerts = make_recheck()

    _snap, js, html_out = await _render(db, watchdog=watchdog, recheck=recheck)
    assert js["unmanaged"]["positions"] == []
    assert js["unmanaged"]["orders"] == []
    assert "all explained" in _cell(html_out, "Unwatched positions")
    assert "none working" in _cell(html_out, "Residual orders")


async def test_session_end_clears_both_rather_than_tallying_them(db):
    """The watchdog and the registry are BOTH rebuilt per trading day, and the registry
    is explicitly never rehydrated (P5) — so a source kept past teardown would show
    orders belonging to a process state that no longer exists."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, ss, _ = _make_service(audit)
    watchdog, _clock = await _scanned(OpenSpreadScan(unattributed=(GHOST,)))
    recheck, _audit2, _clock2, _cancel, _alerts = make_recheck()
    register(recheck)
    ss.session_starting(NOW.date())
    ss.session_running(
        lambda: _risk(md_age=1.0, acct_age=1.0, positions=2),
        unmanaged_source=lambda: _to_unmanaged_positions(watchdog.unwatchable_positions()),
        residual_source=lambda: _to_residual_rows(recheck.residual_rows()),
    )
    try:
        snap = await svc.assemble()
        assert len(snap.unmanaged_positions) == 1 and len(snap.residual_orders) == 1

        ss.session_finished(ended=True)
        snap = await svc.assemble()
        assert (snap.unmanaged_positions, snap.residual_orders) == (None, None)
    finally:
        await audit.aclose()


# -- ordering and severity ----------------------------------------------------


async def test_the_unwatched_row_sits_before_the_two_evaluability_rows(db):
    """This block is one escalating story about the same held spreads — what is held,
    is it watched at all, can its triggers be evaluated, can it be exited — and "on the
    watch list?" is the step before "evaluable?". Severity agrees: an unmanaged
    position is strictly worse than a BLIND one, which is at least still scanned,
    marked and flattened."""
    watchdog, _clock = await _scanned(OpenSpreadScan(unattributed=(GHOST,)))
    _snap, _js, html_out = await _render(db, watchdog=watchdog)
    order = [
        html_out.index(label)
        for label in ("Open positions", "Unwatched positions", "Exit triggers",
                      "Exit attempts", "Residual orders")
    ]
    assert order == sorted(order), "the positions block's order changed"


# -- the invariants that cannot be tested behaviourally -----------------------


def test_the_read_gap_set_matches_the_enum_it_mirrors():
    """`status.py` imports nothing from `execution/`, so the three read-gap values
    cross the seam as strings. A rename there would silently turn the red state off."""
    assert _RESIDUAL_READ_GAPS == {
        str(s) for s in ResidualSighting if not s.resolves
        and s is not ResidualSighting.WORKING
    }


def test_the_unwatchable_accessor_cannot_become_an_io_path():
    """The reason none of the five live-read accessors is wrapped in a `try`.

    `read_rss_bytes` swallows because it does FILE I/O against a path that may not
    exist. This does dict, tuple and clock reads of in-process state — like
    `impaired_positions()` and `spent_exit_attempts()`, neither of which is wrapped.
    A swallow here would render a genuine bug as "nothing unmanaged", which is the
    reassuring-when-wrong failure this whole section exists to remove. So the
    invariant is pinned instead of defended: it must not be able to GROW an I/O path.
    """
    tree = ast.parse(inspect.getsource(PositionWatchdog.unwatchable_positions).lstrip())
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Await)], (
        "unwatchable_positions must never await"
    )
    forbidden = {"_audit", "_spreads", "_executor", "_notify", "_residuals"}
    reached = {
        n.attr for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name) and n.value.id == "self"
    }
    assert not (reached & forbidden), (
        f"unwatchable_positions reached {reached & forbidden} — it must stay a pure read"
    )


async def test_the_accessor_answers_on_a_watchdog_that_has_never_scanned():
    """The other half of the same claim. A watchdog built and not yet run has an empty
    scan and no episode state at all, and the page must render rather than raise."""
    clock = FakeClock()
    _market, _dir, quotes = build_quotes(clock)
    watchdog, _audit, _halts, _alerts = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan()
    )
    assert watchdog.unwatchable_positions() == ()


def test_the_status_module_still_imports_nothing_from_execution():
    """The one-way dependency this section's types are defined in `status.py` to keep.
    Adding two more live rows is exactly the change that would break it."""
    tree = ast.parse((_SRC / "status.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("argus.execution"), (
                f"status.py must not import from execution/: {node.module}"
            )


# -- the count bounds ---------------------------------------------------------


async def test_many_rows_collapse_by_COUNT_and_point_at_the_trail(db):
    """Bounded so a pathological ledger cannot produce a page that has to be scrolled
    past — but by COUNT, never by characters. The audit tail's 240-char cut is exactly
    what a live row exists to sidestep, so nothing here may reintroduce it: every row
    that IS shown shows its `problem` in full."""
    scan = OpenSpreadScan(unattributed=tuple(f"SPXW  260902P0{4900 + i}000" for i in range(12)))
    watchdog, _clock = await _scanned(scan)
    _snap, js, html_out = await _render(db, watchdog=watchdog)

    # The JSON is NOT truncated — a script asking "what is unmanaged?" gets all of it.
    assert len(js["unmanaged"]["positions"]) == 12
    cell = _cell(html_out, "Unwatched positions")
    assert cell.count("NOT WATCHED") == MAX_UNMANAGED_ROWS
    assert f"+{12 - MAX_UNMANAGED_ROWS} more" in cell
    assert "watchdog.unwatchable" in cell


async def test_a_long_symbol_list_inside_one_row_collapses_too(db):
    """A lopsided remainder has one to three symbols in practice; the cap is for the
    shape that does not."""
    entry = {
        **LOPSIDED,
        "outstanding": {f"SPXW  260902P0{5000 + i}000": 1 for i in range(7)},
    }
    watchdog, _clock = await _scanned(OpenSpreadScan(malformed=(entry,)), advance=60.0)
    _snap, js, html_out = await _render(db, watchdog=watchdog)

    assert len(js["unmanaged"]["positions"][0]["detail"]) == 7
    cell = _cell(html_out, "Unwatched positions")
    assert cell.count("still held") == MAX_UNMANAGED_SYMBOLS
    assert f"(+{7 - MAX_UNMANAGED_SYMBOLS} more)" in cell
