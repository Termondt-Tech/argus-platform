"""`tools.replay_selection` — the mid-delta selector's PRE-DEPLOY GATE.

Two halves:

1. **The tool**, on a synthetic day written through the REAL audit log with the
   REAL strategy's payloads (never hand-built dicts — a reader that names its
   source's keys cannot fail loudly when the source changes, so it is pinned
   against the source).
2. **The gate**, on the committed fixture of 2026-09-21's recorded live quotes:
   the four criteria of the decision record. It must pass before the image ships.

→ docs/decisions/2026-09-21-short-strike-selection-on-mid-implied-delta.md (Rollout)
"""

from __future__ import annotations

import json
import math
import pathlib
from datetime import date, time

import pytest

from argus.config import SignalConfig
from argus.signal import implied, regime
from argus.signal.models import CALL, PUT, ChainSnapshot, OptionQuote, RegimeSnapshot
from argus.signal.strategy import CreditSpreadStrategy
from argus.tools import replay_selection as tool
from argus.tools.replay_selection import (
    CANDIDATE,
    NONE,
    ReplayLeg,
    from_fixture,
    gate_failures,
    load_fixture,
    read_legs,
    replay,
    to_fixture,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "selection_replay_2026-09-21.json"
#: The five live entries of 2026-09-21, true (mid-implied) short deltas, in order
#: (decision record, "The defect").
ENTRIES_2026_09_21 = [0.182, 0.197, 0.182, 0.151, 0.193]

DAY = date(2026, 9, 22)
F = 6000.0
MINUTES = 240.0
T = MINUTES / regime.MINUTES_PER_YEAR
REGIME = RegimeSnapshot(atm_iv=0.15, skew=0.0, today=DAY, minutes_to_settlement=MINUTES)


def _ts(minute: int) -> str:
    """UTC; 16:00Z is 12:00 ET."""
    return f"2026-09-22T16:{minute:02d}:00.000000Z"


def _leg(strike: float, right: str, *, feed_bias: float = 0.0, sigma: float = 0.15) -> OptionQuote:
    price, _, _ = implied._price_and_vega(
        sigma, F, strike, math.sqrt(T), math.log(F / strike), right == CALL
    )
    v = sigma * math.sqrt(T)
    n = implied._ncdf(math.log(F / strike) / v + v / 2)
    delta = (n if right == CALL else n - 1.0)
    delta += feed_bias if right == CALL else -feed_bias
    return OptionQuote(
        f"{right}{strike:g}", strike, right, delta,
        max(round(price, 2) - 0.05, 0.0), round(price, 2) + 0.05, greeks_age_s=3.0,
    )


def _chain(**kw) -> ChainSnapshot:
    options = tuple(
        _leg(F + k, right, **kw) for right in (PUT, CALL) for k in range(-80, 85, 5)
    )
    return ChainSnapshot("SPXW", F, options, captured_at=0.0)


def _cfg(side: str, **over) -> SignalConfig:
    base = dict(
        sides=(side,), target_short_delta=0.15, width=5.0, max_width=10.0,
        min_credit=0.05, entry_earliest=time(10, 0), entry_latest=time(15, 0),
        cadence_s=5.0, short_delta_source="feed",
    )
    base.update(over)
    return SignalConfig(**base)


@pytest.fixture
async def running(audit):
    audit.start()
    yield audit
    await audit.aclose()


async def _candidate(audit, minute, side, *, allowed, walked, feed_bias=0.0):
    """One tick shaped exactly as `SignalLoop.evaluate_once` shapes it."""
    result = CreditSpreadStrategy(_cfg(side)).evaluate(
        _chain(feed_bias=feed_bias), time(12, 0), REGIME
    )
    assert result.signal is not None
    audit._clock = lambda: _ts(minute)
    await audit.record(CANDIDATE, {
        **result.signal.to_payload(), "contracts": 1,
        "regime": {**REGIME.to_payload(), "regime_bucket": None},
    })
    await audit.record("gate.decision", {"allowed": allowed, "reasons": []})
    if walked:
        await audit.record("walk.started", {"direction": "open"})
    return result.signal


async def _near_miss(audit, minute, side):
    result = CreditSpreadStrategy(_cfg(side, min_credit=50.0)).evaluate(
        _chain(), time(12, 0), REGIME
    )
    assert result.reason == "credit_below_min"
    audit._clock = lambda: _ts(minute)
    audit.telemetry(NONE, {
        "underlying_price": F, "reason": result.reason,
        "regime": {**REGIME.to_payload(), "regime_bucket": None}, **result.detail,
    })
    await audit._queue.join()


# -- reading ------------------------------------------------------------------


async def test_reads_both_categories_and_flags_only_real_entries(db, running):
    await _candidate(running, 0, PUT, allowed=True, walked=True)      # entered
    await _candidate(running, 1, CALL, allowed=False, walked=False)   # gate denied
    await _candidate(running, 2, CALL, allowed=True, walked=False)    # guard blocked
    await _near_miss(running, 3, PUT)
    await _candidate(running, 4, CALL, allowed=True, walked=True)     # entered

    legs = read_legs(db, DAY)
    assert [(leg.kind, leg.side, leg.role) for leg in legs] == [
        (CANDIDATE, PUT, "short"), (CANDIDATE, PUT, "long"),
        (CANDIDATE, CALL, "short"), (CANDIDATE, CALL, "long"),
        (CANDIDATE, CALL, "short"), (CANDIDATE, CALL, "long"),
        (NONE, PUT, "short"), (NONE, PUT, "long"),
        (CANDIDATE, CALL, "short"), (CANDIDATE, CALL, "long"),
    ]
    assert [leg.entered for leg in legs if leg.role == "short"] == [
        True, False, False, False, True,
    ]
    short = legs[0]
    assert short.greeks_age_s == 3.0 and short.spot == F and short.minutes == MINUTES
    assert legs[1].greeks_age_s is None           # only the short leg carries one
    assert all(leg.bid > 0 and leg.ask > leg.bid for leg in legs)


async def test_rows_recorded_before_the_quoted_spread_are_skipped(db, running):
    """Rows from before 2026-09-15 carry a `mid` and no `bid`/`ask`: nothing to
    replay, and nothing invented from the floored mid."""
    running._clock = lambda: _ts(0)
    await running.record(CANDIDATE, {
        "side": PUT, "underlying_price": F, "regime": {"minutes_to_settlement": 240.0},
        "short": {"strike": 5975.0, "delta": -0.15, "mid": 1.5},
        "long": {"strike": 5970.0, "delta": -0.12, "mid": 1.1},
    })
    assert read_legs(db, DAY) == []


async def test_only_the_asked_day_is_read(db, running):
    await _candidate(running, 0, PUT, allowed=True, walked=True)
    assert read_legs(db, date(2026, 9, 21)) == []
    assert len(read_legs(db, DAY)) == 2


# -- the fixture ----------------------------------------------------------------


async def test_the_fixture_round_trips_and_holds_market_data_only(db, running):
    await _candidate(running, 0, PUT, allowed=True, walked=True)
    await _near_miss(running, 1, CALL)
    legs = read_legs(db, DAY)
    data = json.loads(json.dumps(to_fixture(DAY, legs)))
    assert from_fixture(data) == (DAY, legs)
    assert set(data) == {"format", "trading_date", "source", "columns", "legs"}
    # Nothing about the account, the order or the position is in a column.
    assert not {c for c in data["columns"]} & {
        "contracts", "account_nlv", "broker_order_id", "external_identifier", "symbol",
    }


def test_an_unrecognised_fixture_refuses_rather_than_misreads():
    with pytest.raises(ValueError, match="re-extract"):
        from_fixture({"format": 99, "columns": [], "legs": []})


# -- the replay -----------------------------------------------------------------


def _synthetic_day(n: int = 40, *, feed_bias: float = 0.0) -> list[ReplayLeg]:
    """Legs that behave like a recorded day, straight from the synthetic surface."""
    legs = []
    for i in range(n):
        side = PUT if i % 2 else CALL
        for role, off in (("short", 25.0), ("long", 30.0)):
            strike = F - off if side == PUT else F + off
            q = _leg(strike, side, feed_bias=feed_bias)
            legs.append(ReplayLeg(
                i, NONE, False, side, role, strike, q.bid, q.ask, q.delta,
                3.0 if role == "short" else None, F, MINUTES,
            ))
    return legs


def test_a_consistent_day_passes_the_gate():
    report = replay(DAY, _synthetic_day())
    assert report.solve_rate == 1.0
    assert report.put_fresh_median_gap is not None and abs(report.put_fresh_median_gap) < 0.005
    assert report.unencodable == [] and report.payload_rows == 2 * 40
    assert gate_failures(report) == []


def test_the_replay_asks_what_the_runtime_asks():
    """Same arithmetic, same inputs: each solved leg's delta IS `solve_leg`'s, off
    spot (the runtime's fallback, since a row cannot form a parity forward)."""
    report = replay(DAY, _synthetic_day(4))
    for r in report.results:
        assert r.outcome == implied.solve_leg(r.leg.quote(), F, T)


def test_a_feed_that_disagrees_fails_criterion_2():
    report = replay(DAY, _synthetic_day(feed_bias=0.02))
    assert any(f.startswith("criterion 2") for f in gate_failures(report))


@pytest.mark.parametrize("bias", [0.02, -0.02])
def test_criterion_2_catches_a_bias_in_either_direction(bias):
    report = replay(DAY, _synthetic_day(feed_bias=bias))
    assert report.put_fresh_median_gap == pytest.approx(-bias, abs=0.003)
    assert any(f.startswith("criterion 2") for f in gate_failures(report))


def test_criterion_2_does_not_gate_on_dispersion():
    """Scatter with no offset — alternating +/-0.008, wider than 2026-09-21's
    measured ~0.005 — is quote noise, not a convention error, and passes."""
    legs = [
        ReplayLeg(*[
            (leg.feed_delta + (0.008 if leg.seq % 4 < 2 else -0.008))
            if name == "feed_delta" and leg.role == "short" else getattr(leg, name)
            for name in tool.FIXTURE_COLUMNS
        ])
        for leg in _synthetic_day()
    ]
    report = replay(DAY, legs)
    assert report.put_fresh_median_abs_gap > 0.005
    assert not any(f.startswith("criterion 2") for f in gate_failures(report))


def test_an_unsolvable_day_fails_criterion_1():
    legs = [
        ReplayLeg(i, NONE, False, CALL, "short", F + 25.0, F + 10.0, F + 11.0, 0.15, 3.0, F, MINUTES)
        for i in range(10)
    ] + _synthetic_day(10)
    failures = gate_failures(replay(DAY, legs))
    assert any(f.startswith("criterion 1") for f in failures)


def test_one_sided_and_in_the_money_legs_are_not_solver_failures():
    """One-sided legs are refused by design and ITM legs are never asked, so
    neither is in criterion 1's denominator."""
    legs = _synthetic_day(10) + [
        ReplayLeg(99, NONE, False, CALL, "long", F + 80.0, 0.0, 0.05, 0.01, None, F, MINUTES),
        ReplayLeg(99, NONE, False, CALL, "short", F - 20.0, 25.0, 25.5, 0.8, 3.0, F, MINUTES),
    ]
    assert replay(DAY, legs).solve_rate == 1.0


def test_criterion_3_compares_the_entries_in_order():
    legs = _synthetic_day(4)
    legs[0] = ReplayLeg(*[
        True if name == "entered" else getattr(legs[0], name)
        for name in tool.FIXTURE_COLUMNS
    ])
    report = replay(DAY, legs)
    [entry] = report.entries
    got = abs(entry.outcome.delta)
    assert gate_failures(report, [round(got, 4)]) == []
    assert any(f.startswith("criterion 3") for f in gate_failures(report, [got + 0.01]))
    assert any(f.startswith("criterion 3") for f in gate_failures(report, [got, got]))


def test_the_cli_runs_a_fixture_and_exits_nonzero_on_failure(tmp_path, capsys):
    path = tmp_path / "day.json"
    path.write_text(json.dumps(to_fixture(DAY, _synthetic_day())))
    assert tool.main(["--fixture", str(path)]) == 0
    assert "GATE: PASS" in capsys.readouterr().out
    path.write_text(json.dumps(to_fixture(DAY, _synthetic_day(feed_bias=0.02))))
    assert tool.main(["--fixture", str(path)]) == 1
    assert "GATE: FAIL" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--date", "2026-09-21"],                                    # no source
        ["--config", "x.toml", "--db", "x.db", "--date", "2026-09-21"],  # two
        ["--db", "x.db"],                                            # no date
    ],
)
def test_the_cli_requires_exactly_one_database_source(argv):
    with pytest.raises(SystemExit):
        tool.main(argv)


async def test_the_cli_reads_a_snapshot_file(db, running, tmp_path, capsys):
    await _candidate(running, 0, PUT, allowed=True, walked=True)
    await _near_miss(running, 1, PUT)
    [(path,)] = [(row[2],) for row in db.execute("PRAGMA database_list") if row[1] == "main"]
    out = tmp_path / "fixture.json"
    code = tool.main(["--db", path, "--date", DAY.isoformat(), "--extract-fixture", str(out)])
    assert code == 0, capsys.readouterr().out
    assert load_fixture(out)[1] == read_legs(db, DAY)


def test_the_database_is_opened_read_only():
    import inspect

    src = inspect.getsource(tool._open_readonly)
    assert "mode=ro" in src


# -- THE PRE-DEPLOY GATE ----------------------------------------------------------


def test_pre_deploy_gate_on_the_recorded_2026_09_21_quotes():
    """The four criteria, over 2026-09-21's recorded live quotes. MUST PASS BEFORE
    THE IMAGE SHIPS — which is why a missing fixture FAILS rather than skips: a
    skipped gate is a green suite that checked nothing.

    To produce the fixture (read-only, from a pulled snapshot of `argus-live.db`):

        ./.venv/bin/python -m argus.tools.replay_selection --db dev/nas/<snapshot>.db \\
            --date 2026-09-21 --expect-entries 0.182,0.197,0.182,0.151,0.193 \\
            --extract-fixture tests/fixtures/selection_replay_2026-09-21.json
    """
    if not FIXTURE.exists():
        pytest.fail(
            f"PRE-DEPLOY GATE NOT RUN: {FIXTURE.relative_to(FIXTURE.parents[2])} is missing. "
            "Extract it from a snapshot of argus-live.db holding 2026-09-21 "
            "(see this test's docstring). Do not ship the mid selector until it passes."
        )
    trading_date, legs = load_fixture(FIXTURE)
    assert trading_date == date(2026, 9, 21)
    report = replay(trading_date, legs)
    failures = gate_failures(report, ENTRIES_2026_09_21)
    assert not failures, tool.render(report, failures)
