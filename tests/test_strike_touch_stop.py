"""The strike-touch stop (v0.5.2, 2026-09-26): `watchdog.credit_trigger_enabled = false`.

The switch gates the credit trigger's FIRING and nothing else: the mark and
`credit_stop` are still computed and recorded, the profit target (which shares the
mark's branch) is still evaluated, and `unknown` still says when the mark's input is
missing. With `underlying_buffer_points = 0` the emergency exit is spot reaching the
short strike. → docs/decisions/2026-09-26-strike-touch-stop.md
"""

from __future__ import annotations

import ast
import json
import logging
import pathlib

import pytest

from argus.config import Config, ConfigError
from argus.execution.store import OpenSpreadScan
from argus.execution.watchdog import ExitOutcome, TriggerReason, evaluate_triggers, page_view
from argus.session import _to_unclosable
from argus.status import _fmt_spent_mark
from support import config_dict, make_config
from test_watchdog import (
    LONG_STREAM,
    SHORT_STREAM,
    _LONG_BEFORE_FLATTEN,
    FakeClock,
    RecordingExecutor,
    build_quotes,
    build_watchdog,
    spread,
    thresholds,
)


def _past_the_credit_stop(clock):
    market, _, quotes = build_quotes(clock)
    market.set("SPX", 5100.0, 5100.0)          # far from the 5000 short put
    market.set(SHORT_STREAM, 2.00, 2.25)       # mid 2.125
    market.set(LONG_STREAM, 0.30, 0.50)        # mid 0.40 -> cost to close 1.725 > 1.70
    return market, quotes


def test_disabled_the_credit_trigger_does_not_fire_past_its_stop():
    _, quotes = _past_the_credit_stop(FakeClock())
    ev = evaluate_triggers(spread(credit="0.85"), quotes,
                           thresholds(credit_trigger_enabled=False),
                           seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert TriggerReason.CREDIT_MULTIPLE not in ev.true_reasons
    # The mark and the level are still computed and recorded.
    assert ev.cost_to_close == pytest.approx(1.725)
    assert ev.credit_stop == pytest.approx(1.70)
    assert ev.fully_evaluable


def test_enabled_the_same_quotes_fire_it():
    _, quotes = _past_the_credit_stop(FakeClock())
    ev = evaluate_triggers(spread(credit="0.85"), quotes,
                           thresholds(credit_trigger_enabled=True),
                           seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert TriggerReason.CREDIT_MULTIPLE in ev.true_reasons


def test_the_profit_target_is_still_evaluated_with_the_credit_trigger_off():
    market, _, quotes = build_quotes(FakeClock())
    market.set("SPX", 5100.0, 5100.0)
    market.set(SHORT_STREAM, 0.10, 0.10)
    market.set(LONG_STREAM, 0.05, 0.05)        # cost to close 0.05: one tick
    ev = evaluate_triggers(spread(credit="0.85"), quotes,
                           thresholds(credit_trigger_enabled=False),
                           seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert TriggerReason.PROFIT_TARGET in ev.true_reasons


def test_a_missing_mark_is_still_unknown_with_the_credit_trigger_off():
    """`unknown` is a fact about the INPUT the profit target shares; switching the
    credit trigger off must not make a blind mark look evaluable."""
    market, _, quotes = build_quotes(FakeClock())
    market.set("SPX", 5100.0, 5100.0)          # no leg quotes at all
    ev = evaluate_triggers(spread(credit="0.85"), quotes,
                           thresholds(credit_trigger_enabled=False),
                           seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert TriggerReason.CREDIT_MULTIPLE in ev.unknown_reasons
    assert TriggerReason.PROFIT_TARGET in ev.unknown_reasons


@pytest.mark.parametrize("spot, fires", [(5000.0, True), (4999.0, True), (5000.5, False), (5004.0, False)])
def test_at_buffer_zero_the_underlying_trigger_is_the_strike_touch(spot, fires):
    market, _, quotes = build_quotes(FakeClock())
    market.set("SPX", spot, spot)
    market.set(SHORT_STREAM, 2.00, 2.25)
    market.set(LONG_STREAM, 0.30, 0.50)
    ev = evaluate_triggers(spread(credit="0.85"), quotes,
                           thresholds(credit_trigger_enabled=False, underlying_buffer_points=0.0),
                           seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert (TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE in ev.true_reasons) is fires
    # And never the credit trigger, whatever the mark says.
    assert TriggerReason.CREDIT_MULTIPLE not in ev.true_reasons


@pytest.mark.parametrize("bad", [0, 1, "false", "true", None])
def test_the_switch_must_be_a_real_boolean(bad):
    raw = config_dict()
    raw["watchdog"]["credit_trigger_enabled"] = bad
    with pytest.raises(ConfigError, match="credit_trigger_enabled"):
        Config.from_dict(raw)


def test_the_switch_is_required():
    raw = config_dict()
    del raw["watchdog"]["credit_trigger_enabled"]
    with pytest.raises(ConfigError, match="credit_trigger_enabled"):
        Config.from_dict(raw)


@pytest.mark.parametrize("profile", ["argus.live.toml", "argus.sandbox.toml", "argus.dev.toml"])
def test_every_profile_ships_the_strike_touch_stop(profile):
    wd = Config.load(profile).watchdog
    assert wd.credit_trigger_enabled is False
    assert wd.underlying_buffer_points == 0.0
    assert wd.profit_target_enabled is False


# -- blind means "no stop that can fire" (2026-09-27 review, finding 1) ---------------
#
# `blind` was measured against every market trigger, so a switched-off credit trigger
# (and the profit target, which is not protection) counted as "watching". Under the
# strike-touch stop a dead SPX feed with fresh legs then read DEGRADED — Tier 2, no
# page — while no trigger that could close the position was evaluable.


def _legs_fresh_no_spot(clock):
    market, _, quotes = build_quotes(clock)
    market.set(SHORT_STREAM, 2.00, 2.25)       # legs price (cost to close 1.725) ...
    market.set(LONG_STREAM, 0.30, 0.50)        # ... and there is no SPX quote at all
    return market, quotes


def test_the_acting_stops_follow_the_switch():
    assert thresholds(credit_trigger_enabled=True).acting_stops == {
        TriggerReason.CREDIT_MULTIPLE, TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE}
    assert thresholds(credit_trigger_enabled=False).acting_stops == {
        TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE}
    # Never the profit target (not protection) and never the clock (never unknown).
    for enabled in (True, False):
        acting = thresholds(credit_trigger_enabled=enabled).acting_stops
        assert TriggerReason.PROFIT_TARGET not in acting
        assert TriggerReason.FLATTEN_BEFORE_CLOSE not in acting


def test_with_the_credit_trigger_off_a_dead_spot_feed_alone_is_blind():
    _, quotes = _legs_fresh_no_spot(FakeClock())
    ev = evaluate_triggers(spread(credit="0.85"), quotes,
                           thresholds(credit_trigger_enabled=False, underlying_buffer_points=0.0),
                           seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert ev.unknown_reasons == {TriggerReason.UNDERLYING_NEAR_SHORT_STRIKE}
    assert ev.cost_to_close == pytest.approx(1.725)   # the mark is fine; it cannot act
    assert ev.blind is True


def test_with_the_credit_trigger_on_the_same_state_stays_degraded():
    """The pre-2026-09-26 meaning, unchanged: the credit trigger is a live stop."""
    _, quotes = _legs_fresh_no_spot(FakeClock())
    ev = evaluate_triggers(spread(credit="0.85"), quotes,
                           thresholds(credit_trigger_enabled=True),
                           seconds_to_flatten=_LONG_BEFORE_FLATTEN)
    assert ev.blind is False


async def test_the_watchdog_pages_it_and_status_calls_it_blind():
    """End to end through `check_once`: the Tier 1 record, the page, and the
    `/status` row (`impaired_positions`, a separate copy of the superset test)."""
    clock = FakeClock()
    market, quotes = _legs_fresh_no_spot(clock)
    config = make_config(watchdog={"credit_trigger_enabled": False,
                                   "underlying_buffer_points": 0.0})
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)), config=config)
    for _ in range(2):
        market.set(SHORT_STREAM, 2.00, 2.25)
        market.set(LONG_STREAM, 0.30, 0.50)
        await watchdog.check_once()
    assert audit.events.count("watchdog.unevaluable") == 1
    assert [k for k, _ in alerts].count("watchdog_position_unevaluable") == 1
    (impaired,) = watchdog.impaired_positions()
    assert impaired.unknown_reasons == ("underlying_near_short_strike",)
    assert impaired.blind is True


# -- no page names a credit stop while the credit trigger is off ----------------------
#
# Richard, 2026-09-27 (the v0.5.2 review's Q4). Under the strike-touch stop,
# `credit_stop` is the level a switched-off trigger would use, and a page printing the
# mark beside it reads as "the mark against the stop". Records keep it; pages,
# journal lines and /status do not show it.

_WATCHDOG_SRC = pathlib.Path("src/argus/execution/watchdog.py")


def test_page_view_is_the_identity_with_the_credit_trigger_on():
    detail = {"credit_stop": 1.70, "evaluation": {"credit_stop": 1.70}}
    assert page_view(detail, credit_trigger_enabled=True) is detail


def test_page_view_drops_every_credit_stop_and_says_why():
    record = {
        "candidate_id": "20260928-put-7600-7595",
        "cost_to_close": 1.75,
        "credit_stop": 1.70,
        "evaluation": {"credit_stop": 1.70, "spot": 4990.0},
        "rows": [{"credit_stop": 1.0}],
        "position": {"contracts": 1},
    }
    page = page_view(record, credit_trigger_enabled=False)
    assert "credit_stop" not in json.dumps(page)
    assert page["credit_trigger"] == "off"
    assert page["evaluation"] == {"spot": 4990.0, "credit_trigger": "off"}
    assert page["rows"] == [{"credit_trigger": "off"}]
    # Nothing claimed where there was no credit stop; the scope key and the mark stay.
    assert page["position"] == {"contracts": 1}
    assert page["candidate_id"] == record["candidate_id"]
    assert page["cost_to_close"] == 1.75
    # The caller's dict is the RECORD, and is untouched.
    assert record["credit_stop"] == 1.70 and record["evaluation"]["credit_stop"] == 1.70


def _touched_and_priceable(market):
    market.set(SHORT_STREAM, 2.05, 2.25)       # legs price: cost to close 1.75
    market.set(LONG_STREAM, 0.30, 0.50)
    market.set("SPX", 4990.0, 4990.0)          # through the 5000 short put: the stop


async def test_with_the_credit_trigger_off_no_page_names_a_credit_stop(caplog):
    """End to end: the trigger page, the spent page, its journal line and /status.
    The records keep `credit_stop`."""
    clock = FakeClock()
    market, _, quotes = build_quotes(clock)
    _touched_and_priceable(market)
    config = make_config(watchdog={"credit_trigger_enabled": False,
                                   "underlying_buffer_points": 0.0})
    watchdog, audit, alerts, _ = build_watchdog(
        clock=clock, quotes=quotes, scan=OpenSpreadScan(spreads=(spread(),)),
        config=config, executor=RecordingExecutor(ExitOutcome.NOT_RESOLVED))
    for _ in range(3):                         # the strike touch fires; the exit spends
        clock.advance(1.5)
        _touched_and_priceable(market)
        await watchdog.check_once()
        await watchdog._drain_exits()
    assert spread().external_identifier in watchdog._attempted
    clock.advance(25.0)                        # past the spent page's bound
    _touched_and_priceable(market)
    with caplog.at_level(logging.WARNING, logger="argus.execution.watchdog"):
        await watchdog.check_once()

    kinds = [k for k, _ in alerts]
    assert "watchdog_triggered" in kinds and "watchdog_exit_spent_still_held" in kinds
    for kind, detail in alerts:
        assert "credit_stop" not in json.dumps(detail, default=str), kind
    spent_page = next(d for k, d in alerts if k == "watchdog_exit_spent_still_held")
    assert spent_page["credit_trigger"] == "off"
    assert spent_page["candidate_id"] == spread().candidate_id     # B1 scope intact

    # The journal says what the page says.
    spent_line = next(r.getMessage() for r in caplog.records
                      if r.getMessage().startswith("EXIT ATTEMPT SPENT"))
    assert "credit trigger off" in spent_line and "vs stop" not in spent_line
    page_line = next(r.getMessage() for r in caplog.records
                     if r.getMessage().startswith("watchdog_exit_spent_still_held:"))
    assert "credit_stop" not in page_line

    # The records keep the level: the decision record promises it stays recorded.
    assert "credit_stop" in audit.payload("watchdog.triggered")
    assert "credit_stop" in audit.payload("watchdog.exit_spent_still_held")

    # /status: no credit stop, and the row says why.
    (attempt,) = watchdog.spent_exit_attempts()
    assert attempt.credit_stop is None and attempt.credit_trigger_enabled is False
    (row,) = _to_unclosable([attempt])
    assert _fmt_spent_mark(row).startswith("mark: 1.75 (credit trigger off)")


def _class_node(name):
    tree = ast.parse(_WATCHDOG_SRC.read_text())
    (node,) = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]
    return node


#: Pages sent with `self._alert` directly, each for its own reason, and each pinned
#: below to never name `credit_stop`. `_check_broker_agreement`'s page is leg COUNTS
#: (`broker_legs` / `ledger_legs` / `direction`) with its own error-level journal line.
_DIRECT_PAGERS = {"_check_broker_agreement"}


def _functions(cls):
    return {
        fn.name: fn for fn in ast.walk(cls)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_every_watchdog_page_passes_through_the_one_sink():
    """`page_view` is applied in `PositionWatchdog._notify`. A page sent by calling
    `self._alert` anywhere else skips it, so every such site is named and pinned."""
    functions = _functions(_class_node("PositionWatchdog"))
    callers = {
        name for name, fn in functions.items()
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "_alert"
    }
    assert callers == {"_notify"} | _DIRECT_PAGERS
    for name in _DIRECT_PAGERS:
        constants = {
            n.value for n in ast.walk(functions[name])
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        assert not any("credit_stop" in c for c in constants), name


def test_the_exit_executor_pages_no_credit_stop():
    """`ExitExecutor` has its own `_notify` and no config to apply `page_view` with;
    it is safe because it never names the key. Pinned, so that stays true."""
    names = {
        n.value for n in ast.walk(_class_node("ExitExecutor"))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert not any("credit_stop" in v for v in names)
