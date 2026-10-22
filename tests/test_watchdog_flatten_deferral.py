"""The flatten deferral inside a real `PositionWatchdog`. (2026-09-25)

The pure arithmetic is `tests/test_flatten_deferral.py`. This file drives the watchdog
through a 15:30 flatten and asserts on what it DOES: whether an exit is handed off,
what is recorded and paged, and — the one rule everything here keeps — that a
deferral only ever removes the flatten, and that every failure means the flatten
runs exactly as it did before.

The fixture spread is the suite's put 5000/4995 (barrier 5005). With the implied vol
pinned at 0.15 and a flat 30-minute spot path, spot 5100 is z ≈ 16.6 (deep in the
deferral region) and spot 5030 is z ≈ 4.4 (in the tail but over budget at k = 10).

→ docs/designs/2026-09-25-deferring-a-far-otm-flatten.md §4
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest

from argus.audit.log import AuditWriteError
from argus.execution.models import LiveOrders
from argus.execution.store import OpenSpreadScan
from argus.execution.watchdog import (
    ChainImpliedVol,
    ExitOutcome,
    LegQuotes,
    TriggerReason,
)
from tests.support import make_config
from tests.test_watchdog import (
    CALL_LONG_STREAM,
    CALL_SHORT_STREAM,
    ET_HALF_DAY,
    ET_SESSION_DAY,
    LONG_STREAM,
    SHORT_STREAM,
    FakeClock,
    FakeDirectory,
    FakeETClock,
    FakeMarket,
    RecordingExecutor,
    build_quotes,
    build_watchdog,
    contracts,
    spread,
)

KEY = spread().external_identifier


class FixedIV:
    """An `ImpliedVolSource` returning one number, and counting its calls."""

    def __init__(self, value: float | None = 0.15, *, raises: Exception | None = None):
        self.value = value
        self.raises = raises
        self.calls = 0

    def atm_iv(self, *, now_et, today):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.value


def _config(**watchdog):
    """The suite's config with the deferral's own precondition: `on` requires the
    welded size invariant (condition 6), which the suite's base risk section (2 ×
    1000 against a 1000 day) breaks — so these tests hold one position. The
    two-position case is its own test below, at 2 × 475 against 950."""
    return make_config(watchdog=watchdog, risk={"max_concurrent_positions": 1})


async def _live_orders() -> LiveOrders:
    return LiveOrders()


def _book(market, spot: float) -> None:
    """Both legs priceable and quiet, no price trigger true, spot where asked."""
    market.set(SHORT_STREAM, 0.50, 0.60)
    market.set(LONG_STREAM, 0.20, 0.30)
    market.set("SPX", spot, spot)


def _seed_path(
    watchdog, et_clock: FakeETClock, spot: float, minutes: int = 31, *, stamp: bool = True
) -> None:
    """Thirty minutes of one-minute spot samples ending now — what the watchdog would
    have sampled itself had it been running since 15:00 — and (unless `stamp=False`,
    the restart tests) the watch start its own `_sample_spot` would have stamped."""
    now = et_clock.now
    first = now - timedelta(minutes=minutes)
    for i in range(minutes, -1, -1):
        wobble = 0.5 if i % 2 else -0.5
        watchdog._spot_path.append((now - timedelta(minutes=i), spot + wobble))
    if stamp and (watchdog._spot_watch_since is None or first < watchdog._spot_watch_since):
        watchdog._spot_watch_since = first


class Harness:
    """One watchdog on one fixture spread, with the book re-quoted every tick.

    Re-quoting matters: an unrefreshed quote goes stale past the 2-second bound, spot
    goes None, the underlying trigger goes BLIND — and a blind backstop correctly
    RELEASES a deferral (condition 3). Tests that are not about that keep the book
    fresh; tests that are about it stop refreshing.
    """

    def __init__(self, *, spot=5100.0, mode="on", iv=None, live_orders=_live_orders,
                 seed=True, executor=None, hour=15, minute=30, day=ET_SESSION_DAY,
                 config=None):
        self.clock = FakeClock()
        self.market, _, quotes = build_quotes(self.clock)
        self.spot = spot
        self.short = (0.50, 0.60)
        self.long = (0.20, 0.30)
        self.fresh = True
        self.et_clock = FakeETClock(day=day, hour=hour, minute=minute)
        self.executor = executor or RecordingExecutor()
        self.iv = iv if iv is not None else FixedIV()
        self.watchdog, self.audit, self.alerts, self.halts = build_watchdog(
            clock=self.clock,
            quotes=quotes,
            scan=OpenSpreadScan(spreads=(spread(),)),
            executor=self.executor,
            config=config or _config(flatten_deferral=mode),
            et_clock=self.et_clock,
            live_orders=live_orders,
            implied_vol=self.iv,
        )
        self._quote()
        if seed:
            _seed_path(self.watchdog, self.et_clock, spot)

    def _quote(self) -> None:
        self.market.set(SHORT_STREAM, *self.short)
        self.market.set(LONG_STREAM, *self.long)
        self.market.set("SPX", self.spot, self.spot)

    async def tick(self, seconds: float = 1.0) -> None:
        self.clock.advance(seconds)
        if self.fresh:
            self._quote()
        await self.watchdog.check_once()
        await self.watchdog._drain_exits()

    def kinds(self) -> list[str]:
        return [k for k, _ in self.alerts]


# -- the behaviour -------------------------------------------------------------


async def test_a_far_otm_flatten_is_deferred_and_nothing_is_sent():
    h = Harness()
    for _ in range(5):
        await h.tick()

    assert h.executor.calls == [], "a deferred flatten hands nothing to the exit path"
    assert h.audit.events.count("watchdog.flatten_deferred") == 1, "Tier 1 once, not per tick"
    assert h.kinds().count("watchdog_flatten_deferred") == 1
    assert "watchdog.triggered" not in h.audit.events, "no stop-loss page for a deferral"
    record = h.audit.payload("watchdog.flatten_deferred")
    assert record["defer"] is True and record["acted"] is True and record["mode"] == "on"
    assert record["z"] > 10 and record["lhs"] <= record["premium"]
    assert record["candidate_id"] == spread().candidate_id
    # Every no-action tick carries the decision on `watchdog.check`.
    check = [p for c, p in h.audit.payloads if c == "watchdog.check"][-1]
    assert check["flatten_deferral"]["defer"] is True


async def test_a_nearer_flatten_is_not_deferred_and_fires_as_today():
    h = Harness(spot=5030.0)
    await h.tick()

    assert len(h.executor.calls) == 1
    assert h.executor.calls[0][1] == frozenset({TriggerReason.FLATTEN_BEFORE_CLOSE})
    assert "watchdog.flatten_deferred" not in h.audit.events
    assert h.audit.payload("watchdog.triggered")["fired"] == ["flatten_before_close"]


async def test_spot_coming_closer_releases_the_flatten_on_the_same_tick():
    h = Harness()
    await h.tick()
    assert h.executor.calls == []

    h.spot = 5030.0
    await h.tick()

    assert len(h.executor.calls) == 1, "released AND handed off on the same tick"
    released = h.audit.payload("watchdog.flatten_undeferred")
    # Closer in points AND — a 70-point jump inside a minute — a realized-vol spike;
    # either way it is no longer far enough.
    assert released["reason"] in ("insurance_not_overpriced", "inside_the_body")
    assert h.kinds().count("watchdog_flatten_undeferred") == 1
    events = h.audit.events
    assert events.index("watchdog.flatten_undeferred") < events.index("watchdog.triggered")


async def test_a_blind_backstop_releases_a_deferral():
    """Condition 3: stop re-quoting and spot goes stale; the underlying trigger can
    no longer see, so the flatten must fire."""
    h = Harness()
    await h.tick()
    h.fresh = False
    await h.tick(5.0)
    assert h.audit.payload("watchdog.flatten_undeferred")["reason"] == "underlying_trigger_blind"
    # Released to today's path exactly: with the legs stale too, the flatten cannot be
    # built, so it is the pre-existing unpriceable escalation — never a price invented.
    assert "watchdog.exit_unpriceable" in h.audit.events
    assert h.executor.calls == []


async def test_record_mode_decides_and_records_and_flattens_exactly_as_off():
    """`record` is observation only: the SAME handoff as `off`, plus the records."""
    rec, off = Harness(mode="record"), Harness(mode="off")
    await rec.tick()
    await off.tick()

    assert [c[1] for c in rec.executor.calls] == [c[1] for c in off.executor.calls] != []
    assert rec.audit.payload("watchdog.flatten_deferred")["acted"] is False
    assert not [k for k in rec.kinds() if k.startswith("watchdog_flatten")], "no page in record"
    assert not any("flatten_deferr" in c for c in off.audit.events)


async def test_off_computes_nothing_at_all():
    h = Harness(mode="off", seed=False)
    await h.tick()
    assert h.iv.calls == 0 and len(h.watchdog._spot_path) == 0
    assert len(h.executor.calls) == 1


# -- every failure means "flatten as usual" -------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [{"iv": FixedIV(None)}, {"seed": False}, {"live_orders": None}],
    ids=["no-iv", "restart-empty-rv-buffer", "no-broker-poll"],
)
async def test_each_missing_input_flattens_as_today(kwargs):
    h = Harness(**kwargs)
    await h.tick()
    assert len(h.executor.calls) == 1
    assert h.audit.payload("watchdog.triggered")["fired"] == ["flatten_before_close"]
    # A refusal that was never a deferral writes no deferral edge.
    assert "watchdog.flatten_deferred" not in h.audit.events
    assert h.watchdog._deferrals == {}


async def test_a_stale_broker_poll_releases_a_deferral():
    """Condition 5: one missed poll is tolerated, two are not."""
    answered = {"ok": True}

    async def flaky() -> LiveOrders:
        if not answered["ok"]:
            raise ConnectionError("broker down")
        return LiveOrders()

    h = Harness(live_orders=flaky)
    await h.tick()
    assert h.executor.calls == []

    answered["ok"] = False
    refresh = h.watchdog._wd.refresh_interval_s
    await h.tick(refresh + 0.1)          # one failed poll: tolerated
    assert h.executor.calls == []
    await h.tick(refresh + 0.1)          # two: released
    assert len(h.executor.calls) == 1
    assert h.audit.payload("watchdog.flatten_undeferred")["reason"] == "broker_poll_stale"


async def test_an_exception_while_deciding_flattens_and_is_recorded():
    h = Harness(iv=FixedIV(raises=RuntimeError("boom")))
    await h.tick()
    assert len(h.executor.calls) == 1, "the fail-safe direction IS the flatten"
    assert h.audit.payload("watchdog.flatten_deferral_failed")["error_type"] == "RuntimeError"


async def test_an_emergency_trigger_is_never_deferred():
    """Condition 1: the credit stop fires beside the clock — that is an emergency exit."""
    h = Harness()
    h.short = (2.00, 2.20)               # cost to close well past the 1.70 stop
    for _ in range(8):                   # past sustained_s
        await h.tick()
    assert h.executor.calls
    fired = h.executor.calls[0][1]
    assert TriggerReason.CREDIT_MULTIPLE in fired and TriggerReason.FLATTEN_BEFORE_CLOSE in fired


async def test_a_spent_position_still_gets_its_flatten_override():
    """Hard constraint 1: never applied to a spent position, so the flatten's one-shot
    override past the mark is untouched."""
    h = Harness(executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED), hour=11, minute=0)
    h.short = (2.00, 2.20)
    for _ in range(8):
        await h.tick()
    assert KEY in h.watchdog._attempted
    before = len(h.executor.calls)

    h.short = (0.50, 0.60)               # far OTM again, the stop no longer true
    h.et_clock.at(15, 30)
    await h.tick()

    assert len(h.executor.calls) == before + 1, "the override fired, undeferred"
    assert "watchdog.flatten_deferred" not in h.audit.events


async def test_after_a_send_it_never_defers_again_but_after_nothing_sent_it_may():
    """No latch is needed (§4.6): a sent order sets `_attempted`, which bars deferral;
    a `NOT_ATTEMPTED` sent nothing, so deferring again is as safe as the first time."""
    h = Harness(executor=RecordingExecutor(ExitOutcome.NOT_ATTEMPTED), iv=FixedIV(None))
    await h.tick()                       # no IV: flattened, and it sent nothing
    assert len(h.executor.calls) == 1 and KEY not in h.watchdog._attempted

    h.iv.value = 0.15
    await h.tick()
    assert len(h.executor.calls) == 1, "deferred after a handoff that sent nothing"
    assert h.watchdog._deferrals[KEY].deferring

    # Released again — by an emergency trigger this time — and an order goes out.
    h.executor.outcome = ExitOutcome.CROSSED
    h.short = (2.00, 2.20)
    for _ in range(8):
        await h.tick()
    assert KEY in h.watchdog._attempted
    calls = len(h.executor.calls)
    h.short = (0.50, 0.60)
    await h.tick()
    assert not h.watchdog._deferrals[KEY].deferring, "an order was sent: never again"
    assert "watchdog.flatten_deferred_again" not in h.audit.events[-6:]
    assert len(h.executor.calls) >= calls


# -- the decision is on the record whichever way it went (2026-09-28, P11) ------------
#
# Until 2026-09-28 only `watchdog.check` (a tick where nothing fired) carried the
# decision, so a flatten the deferral DECLINED to defer — the one that fires, and most
# of them — left its z, P_touch and lhs on no record: the population k is priced on.


async def test_a_flatten_the_deferral_declines_carries_its_decision_on_the_triggered_row():
    h = Harness(spot=5030.0)
    await h.tick()

    triggered = h.audit.payload("watchdog.triggered")
    assert triggered["fired"] == ["flatten_before_close"]
    decision = triggered["flatten_deferral"]
    assert decision["defer"] is False
    assert 0 < decision["z"] < 10, "the z the deferral computed, not a placeholder"
    assert decision["p_touch"] > 0 and decision["lhs"] > decision["premium"]
    assert decision["k"] == 10.0 and decision["side"] == "put"


async def test_the_triggered_row_and_the_check_row_carry_the_same_field():
    """One builder: the same key and the same shape whichever record carries it."""
    declined, deferred = Harness(spot=5030.0), Harness()
    await declined.tick()
    await deferred.tick()

    on_triggered = declined.audit.payload("watchdog.triggered")["flatten_deferral"]
    on_check = [p for c, p in deferred.audit.payloads if c == "watchdog.check"][-1]
    assert set(on_triggered) == set(on_check["flatten_deferral"])


async def test_a_released_flatten_records_the_releasing_decision_on_both_rows():
    h = Harness()
    await h.tick()
    h.spot = 5030.0
    await h.tick()

    released = h.audit.payload("watchdog.flatten_undeferred")
    decision = h.audit.payload("watchdog.triggered")["flatten_deferral"]
    assert decision["defer"] is False
    assert (decision["reason"], decision["z"]) == (released["reason"], released["z"])


async def test_record_mode_puts_the_unacted_deferral_on_the_triggered_row_and_off_nothing():
    """In `record` the flatten fires even when the decision says defer, so the row that
    records the flatten is the only place that says it would have been deferred."""
    rec, off = Harness(mode="record"), Harness(mode="off")
    await rec.tick()
    await off.tick()

    assert rec.audit.payload("watchdog.triggered")["flatten_deferral"]["defer"] is True
    assert "flatten_deferral" not in off.audit.payload("watchdog.triggered"), (
        "off decides nothing, so the row keeps its pre-2026-09-25 shape"
    )


@pytest.mark.parametrize(
    "kwargs, reason",
    [({"iv": FixedIV(None)}, None), ({"seed": False}, None), ({"live_orders": None}, None),
     ({"iv": FixedIV(raises=RuntimeError("boom"))}, "deferral_error:RuntimeError")],
    ids=["no-iv", "restart-empty-rv-buffer", "no-broker-poll", "deciding-raised"],
)
async def test_a_refusal_is_on_the_triggered_row_with_its_reason(kwargs, reason):
    h = Harness(**kwargs)
    await h.tick()
    decision = h.audit.payload("watchdog.triggered")["flatten_deferral"]
    assert decision["defer"] is False and decision["reason"]
    if reason is not None:
        assert decision["reason"] == reason


async def test_an_emergency_exit_beside_the_clock_records_why_it_was_not_deferred():
    h = Harness()
    h.short = (2.00, 2.20)
    for _ in range(8):
        await h.tick()
    triggered = h.audit.payload("watchdog.triggered")
    assert "credit_multiple" in triggered["fired"]
    assert triggered["flatten_deferral"]["defer"] is False


async def test_a_spent_positions_override_carries_no_decision():
    """The deferral never applies to a spent position, so it decides nothing there and
    the override's row says nothing about it — never a stale decision from before."""
    h = Harness(executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED), hour=11, minute=0)
    h.short = (2.00, 2.20)
    for _ in range(8):
        await h.tick()
    assert KEY in h.watchdog._attempted
    h.short = (0.50, 0.60)
    h.et_clock.at(15, 30)
    await h.tick()

    overrides = [p for c, p in h.audit.payloads
                 if c in ("watchdog.triggered", "watchdog.triggered_still")
                 and "flatten_before_close" in p["fired"]]
    assert overrides and all("flatten_deferral" not in p for p in overrides)


# -- the outcome dataset -----------------------------------------------------------


async def test_the_outcome_row_is_written_once_at_the_close():
    h = Harness()
    await h.tick()
    h.et_clock.at(16, 0)
    # The watchdog samples spot every pass; a fake clock that jumps 30 minutes must
    # supply the path the real one would have recorded meanwhile.
    _seed_path(h.watchdog, h.et_clock, h.spot)
    await h.tick()
    await h.tick()

    assert h.audit.events.count("watchdog.flatten_deferral_outcome") == 1
    outcome = h.audit.payload("watchdog.flatten_deferral_outcome")
    assert outcome["ended"] == "close" and outcome["touched"] is False
    assert outcome["deferring_at_end"] is True
    assert outcome["min_z_after_first_deferral"] > 10
    assert outcome["seconds_to_close"] == 0.0
    # Review fix: the flatten is true on every tick of a deferral episode, so it is
    # not one of the triggers the outcome counts — a quiet close lists none.
    assert outcome["emergency_triggers_after_first_deferral"] == []


async def test_the_outcome_row_is_written_when_the_position_leaves_the_ledger():
    h = Harness()
    await h.tick()
    h.watchdog._spreads.scan = OpenSpreadScan()
    h.watchdog.request_refresh()
    await h.tick()
    assert h.audit.payload("watchdog.flatten_deferral_outcome")["ended"] == "left_the_ledger"


async def test_the_session_stop_at_the_close_writes_the_outcome_row():
    """Review fix (2026-09-25). The scheduler sets the session's stop AT the close, so
    in production the last pass runs at ~15:59:59 and the in-pass "close" branch is
    not reached; a held-to-expiry position leaves the ledger after settlement, when
    the watchdog is gone. `stop()` is therefore the NORMAL writer — driven here
    through `run()`, the way the session drives it."""
    h = Harness()
    h.watchdog.start()
    for _ in range(200):
        await asyncio.sleep(0)
        if "watchdog.flatten_deferred" in h.audit.events:
            break
    assert "watchdog.flatten_deferred" in h.audit.events, "run() made one pass"

    await h.watchdog.stop()

    assert h.audit.events.count("watchdog.flatten_deferral_outcome") == 1
    outcome = h.audit.payload("watchdog.flatten_deferral_outcome")
    assert outcome["ended"] == "session_end" and outcome["deferring_at_end"] is True
    assert outcome["seconds_to_close"] == 30 * 60.0
    await h.watchdog.stop()
    assert h.audit.events.count("watchdog.flatten_deferral_outcome") == 1, "once only"


async def test_stop_never_raises_when_the_outcome_cannot_be_written():
    """The session's teardown still has streams to stop behind the watchdog; an audit
    failure has already halted through `on_failure` by then."""
    h = Harness()
    await h.tick()

    async def dead(category, payload):
        raise AuditWriteError("recorder gone")

    h.audit.record = dead
    await h.watchdog.stop()
    assert "watchdog.flatten_deferral_outcome" not in h.audit.events


# -- review fixes (2026-09-25) -----------------------------------------------------


async def test_a_position_hovering_at_the_boundary_writes_bounded_records():
    """No hysteresis is added — the deferral is still re-checked every tick and a
    `NOT_ATTEMPTED` handoff still re-defers — but the RECORDS are bounded: a release
    reason already on record is Tier 2, and a withheld tick does not close the
    `_TRIGGERED` episode. Before the fix, 20 alternating ticks wrote 10 Tier 1
    `flatten_undeferred`, 10 Tier 1 `watchdog.triggered` and 20 pages."""
    h = Harness(executor=RecordingExecutor(ExitOutcome.NOT_ATTEMPTED))
    for i in range(20):
        h.spot = 5100.0 if i % 2 == 0 else 5030.0
        await h.tick()

    assert len(h.executor.calls) == 10, "still handed off on every crossing"
    reasons = {
        p["reason"] for c, p in h.audit.payloads if c == "watchdog.flatten_undeferred"
    }
    assert 1 <= len(reasons) <= 2
    assert h.audit.events.count("watchdog.flatten_undeferred") == len(reasons)
    assert h.kinds().count("watchdog_flatten_undeferred") == len(reasons)
    assert "tier2:watchdog.flatten_undeferred_again" in h.audit.events
    assert h.audit.events.count("watchdog.triggered") == 1
    assert h.kinds().count("watchdog_triggered") == 1
    assert h.audit.events.count("watchdog.flatten_deferred") == 1


async def test_a_new_release_reason_is_always_loud():
    """A reason not yet on record — here an emergency trigger beside the clock, after a
    release and a re-deferral — is Tier 1 and paged, and names the trigger."""
    h = Harness(executor=RecordingExecutor(ExitOutcome.NOT_ATTEMPTED))
    await h.tick()
    h.spot = 5030.0
    await h.tick()                       # released, handed off, nothing sent
    h.spot = 5100.0
    await h.tick()                       # re-deferred
    assert h.watchdog._deferrals[KEY].deferring

    h.executor.outcome = ExitOutcome.CROSSED
    h.short = (2.00, 2.20)
    for _ in range(8):                   # past sustained_s
        await h.tick()

    released = [p for c, p in h.audit.payloads if c == "watchdog.flatten_undeferred"]
    assert len(released) == 2
    assert released[-1]["reason"] == "another_trigger_fired:credit_multiple"
    assert h.kinds().count("watchdog_flatten_undeferred") == 2
    assert TriggerReason.CREDIT_MULTIPLE in h.executor.calls[-1][1]


async def test_a_restart_inside_the_window_disables_the_deferral_for_the_day():
    """Review fix: "a restart after 15:00 disables it for the day" is now TRUE.
    `realized_vol` accepts 80% of its window, so a full-looking path alone re-armed the
    deferral ~24 minutes after any restart; the watch start is what decides it."""
    h = Harness(seed=False, hour=15, minute=5)
    await h.tick()                       # restarted 15:05: before the mark, no flatten
    assert h.watchdog._spot_watch_since == h.et_clock.now

    h.et_clock.at(15, 45)
    _seed_path(h.watchdog, h.et_clock, h.spot, stamp=False)   # RV itself is usable
    await h.tick()

    assert len(h.executor.calls) == 1, "flattened as before the deferral existed"
    assert h.watchdog._deferrals == {}
    ev = SimpleNamespace(unknown_reasons=frozenset())
    fresh = Harness(seed=False)
    fresh.watchdog._spot_watch_since = fresh.et_clock.now.replace(minute=0, second=1)
    assert fresh.watchdog._deferral_ineligible(
        spread(), ev, frozenset({TriggerReason.FLATTEN_BEFORE_CLOSE})
    ) == "not_watching_since_window_start"


async def test_the_watch_start_is_the_first_sample_and_never_moves():
    h = Harness(seed=False, hour=14, minute=0)
    await h.tick()
    first = h.watchdog._spot_watch_since
    assert first == h.et_clock.now
    h.et_clock.at(14, 30)
    await h.tick()
    assert h.watchdog._spot_watch_since == first


async def test_a_missing_iv_mid_window_is_carried_only_while_as_fresh_as_a_quote():
    """Review fix: an estimator failing mid-window is failing on crossed or stale
    quotes — a fast market — so a stale, lower IV there would inflate z. One missed
    estimate is tolerated (the reading is as fresh as a quote may be); a second is not."""
    h = Harness(executor=RecordingExecutor(ExitOutcome.NOT_ATTEMPTED))
    max_age = h.watchdog._config.staleness.market_data_max_age_s
    await h.tick()
    assert h.watchdog._deferrals[KEY].deferring

    h.iv.value = None
    await h.tick(max_age / 2)
    assert h.watchdog._deferrals[KEY].deferring, "carried: younger than a quote may be"
    await h.tick(max_age)
    assert h.audit.payload("watchdog.flatten_undeferred")["reason"] == "no_implied_vol_since_mark"
    assert len(h.executor.calls) == 1


async def test_inside_the_estimators_nan_regime_the_last_iv_is_carried_at_any_age():
    h = Harness(hour=15, minute=49)
    _seed_path(h.watchdog, h.et_clock, h.spot, minutes=50)
    await h.tick()                       # 11 minutes left: a live reading
    assert h.watchdog._deferrals[KEY].deferring

    h.iv.value = None                    # `compute_regime` is NaN below 10 minutes
    h.et_clock.at(15, 51)
    await h.tick(60.0)

    assert h.watchdog._deferrals[KEY].deferring
    assert h.executor.calls == []


def test_the_nan_regime_boundary_is_the_estimators_own():
    """The watchdog's "inside the NaN regime?" must never disagree with the estimator's
    own refusal, or a release on that one tick hands a deferred flatten to the exit."""
    from argus.clock import ET
    from argus.execution.watchdog import _minutes_to_close
    from argus.signal.regime import _minutes_to_settlement

    for close in (time(16, 0), time(13, 0)):
        for now in (time(15, 50), time(15, 49, 59, 999999), time(15, 50, 0, 1),
                    time(12, 50), time(12, 49, 59, 999999), time(9, 45)):
            now_et = datetime.combine(ET_SESSION_DAY, now, tzinfo=ET)
            assert _minutes_to_close(now_et, close) == _minutes_to_settlement(now, close)


async def test_two_positions_are_each_decided_on_their_own_barrier():
    """The cap moved to 2 spreads × 1 contract on 2026-09-25, at unchanged total risk
    (2 × 475 against a 950 day). Each position is decided against ITS OWN barrier on
    the same tick: at spot 5025 the put (barrier 5005, z ≈ 3.5) is over budget and
    flattens, while the call (barrier 5095, z ≈ 12) is deferred. Both get their own
    records, and the stop writes the outcome row for the deferred one only."""
    clock = FakeClock()
    market = FakeMarket(clock)
    quotes = LegQuotes(
        market, FakeDirectory(contracts() + contracts(call=True)),
        underlying_symbol="SPX", max_age_s=2.0,
    )
    et_clock = FakeETClock(hour=15, minute=30)
    put, call = spread(contracts_held=1), spread(call=True, contracts_held=1)
    executor = RecordingExecutor(ExitOutcome.NOT_ATTEMPTED)
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(put, call)),
        executor=executor, et_clock=et_clock, live_orders=_live_orders,
        implied_vol=FixedIV(),
        config=make_config(
            watchdog={"flatten_deferral": "on"},
            risk={"daily_max_loss": 950.0, "max_concurrent_positions": 2,
                  "max_loss_per_position": 475.0, "max_contracts_per_position": 1},
        ),
    )
    spot = 5025.0
    _seed_path(watchdog, et_clock, spot)
    clock.advance(1.0)
    for stream in (SHORT_STREAM, CALL_SHORT_STREAM):
        market.set(stream, 0.05, 0.10)
    for stream in (LONG_STREAM, CALL_LONG_STREAM):
        market.set(stream, 0.00, 0.05)
    market.set("SPX", spot, spot)
    await watchdog.check_once()
    await watchdog._drain_exits()

    assert [(s.candidate_id, r) for s, r in executor.calls] == [
        (put.candidate_id, frozenset({TriggerReason.FLATTEN_BEFORE_CLOSE}))
    ], "the put flattens; the call does not"
    deferred = [p for c, p in audit.payloads if c == "watchdog.flatten_deferred"]
    assert [p["candidate_id"] for p in deferred] == [call.candidate_id]
    assert deferred[0]["z"] > 10
    assert set(watchdog._deferrals) == {call.external_identifier}

    await watchdog.stop()
    outcomes = [p for c, p in audit.payloads if c == "watchdog.flatten_deferral_outcome"]
    assert [p["candidate_id"] for p in outcomes] == [call.candidate_id]


async def test_a_position_whose_exit_send_already_failed_is_never_deferred():
    """Hard constraint 1, read literally: never skip a position whose exit is spent OR
    FAILING. A retryable exit charges `_sends` and leaves the position out of
    `_attempted`."""
    h = Harness()
    h.watchdog._sends[KEY] = 1
    await h.tick()
    assert len(h.executor.calls) == 1
    assert h.watchdog._deferrals == {}


# -- /status ---------------------------------------------------------------------


async def test_the_status_row_carries_the_loaded_parameters_and_the_decision():
    from argus.session import _to_flatten_deferral
    from argus.status import _flatten_deferral_cell

    h = Harness()
    await h.tick()
    state = _to_flatten_deferral(h.watchdog.flatten_deferral_state())

    assert (state.mode, state.k, state.tail_z99, state.tail_alpha) == ("on", 10.0, 2.33, 2.93)
    (row,) = state.rows
    assert row.deferring and row.reason == "deferred" and row.z > 10
    html = _flatten_deferral_cell(state)
    assert "DEFERRED" in html and "its stop stays live to the close" in html and "k 10" in html
    assert "no watchdog" in _flatten_deferral_cell(None)


# -- the IV source ------------------------------------------------------------------


class _Chain:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self):
        return self._snapshot


@pytest.mark.parametrize(
    "day,now",
    [(ET_SESSION_DAY, time(15, 30)), (ET_HALF_DAY, time(12, 30))],
    ids=["normal-close", "half-day"],
)
def test_the_watchdog_iv_is_exactly_the_calibrated_iv(day, now):
    """Guard 1 of the rollout (§4.9): the number the watchdog standardizes by is
    `compute_regime` on the same snapshot with the day's OWN close — the estimator the
    calibration used — so a wiring slip (wrong close, wrong clock) fails here, before
    deploy, rather than on a live 15:30."""
    from argus.clock import ET
    from argus.marketcalendar import MarketCalendar
    from argus.signal.regime import compute_regime
    from tests.test_regime import _chain

    calendar = MarketCalendar.from_config(make_config().market)
    snapshot = _chain(sigma=0.20, minutes=30.0)
    source = ChainImpliedVol(_Chain(snapshot), calendar)
    now_et = datetime.combine(day, now, tzinfo=ET)

    direct = compute_regime(
        snapshot, now_et=now, today=day, close_et=calendar.close_time(day)
    ).atm_iv
    assert source.atm_iv(now_et=now_et, today=day) == direct
    assert direct > 0.0     # (the 0DTE strip reads high this late — regime.py says so)
    assert ChainImpliedVol(_Chain(None), calendar).atm_iv(now_et=now_et, today=day) is None


def test_production_wires_the_chain_iv_into_the_watchdog():
    """The session hands the watchdog `ChainImpliedVol` over the SAME chain source
    `LegQuotes` resolves contracts from — one feed for both."""
    import ast
    import inspect

    import argus.session as session_mod

    source = inspect.getsource(session_mod.run_session)
    assert "implied_vol=ChainImpliedVol(" in source
    call = next(
        n for n in ast.walk(ast.parse(inspect.getsource(session_mod).lstrip()))
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "ChainImpliedVol"
    )
    assert ast.unparse(call.args[0]) == "chain_source"


def test_the_deferral_reads_its_parameters_only_from_config():
    watchdog = Harness(
        config=_config(
            flatten_deferral="on",
            flatten_defer_insurance_multiple=17.0,
            flatten_defer_tail_z99=2.5,
            flatten_defer_tail_alpha=2.5,
        )
    ).watchdog
    p = watchdog._deferral_params
    assert (p.insurance_multiple, p.tail_z99, p.tail_alpha) == (17.0, 2.5, 2.5)
    assert p.buffer_points == watchdog._wd.underlying_buffer_points


def test_changing_k_moves_the_boundary_as_the_formula_says():
    from argus.execution.flatten_deferral import deferral_boundary

    for k, expected in ((5.0, 4.035), (20.0, 6.47)):
        assert deferral_boundary(k=k, width=5.0, tail_z99=2.33, tail_alpha=2.93) == (
            pytest.approx(expected, abs=0.02)
        )

