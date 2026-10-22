"""`signal.ladder`: the strike ladder, recorded once a minute. (2026-09-29, P9)

The properties:
- a row's mid deltas are EXACTLY the selector's;
- a hostile quote can never refuse a row;
- one row per ET minute, whatever the tick decides (blackout days included);
- a failure never costs the tick its decision;
- nothing in `src/` reads the record back.
"""

from __future__ import annotations

import ast
import json
import math
from datetime import date, time
from pathlib import Path

import pytest

import argus.signal.ladder as ladder_mod
from argus.audit.log import AuditLog
from argus.signal.ladder import LADDER_CATEGORY, LADDER_MAX_STRIKES_PER_SIDE, build_ladder
from argus.signal.models import ChainSnapshot, OptionQuote
from argus.signal.regime import compute_regime
from argus.signal.strategy import _mid_pricing, _solve_side
from tests.test_orchestrator_regime import TODAY, _StubStrategy, _loop, _records, _strip

_SRC = Path(__file__).resolve().parents[1] / "src" / "argus"


def _regime(chain=None, at=time(12, 0)):
    return compute_regime(chain or _strip(), now_et=at, today=TODAY, close_et=time(16, 0))


def _encodes(payload) -> None:
    json.dumps(payload, allow_nan=False)  # the audit log's own rule


# -- the row --------------------------------------------------------------------


def test_the_row_is_the_selectors_view_of_both_wings():
    chain, regime = _strip(), _regime()
    row = build_ladder(chain, regime)
    _encodes(row)
    pricing = _mid_pricing(chain, regime)
    assert row["forward"] == pytest.approx(pricing.forward)
    for side in ("put", "call"):
        s = row[side]
        n = len(s["strike"])
        assert n > 0 and all(len(s[k]) == n for k in ("bid", "ask", "mid_delta", "mid_iv"))
        # out of the money, nearest first
        dist = [abs(k - pricing.forward) for k in s["strike"]]
        assert dist == sorted(dist)
        assert all((k > pricing.forward) if side == "call" else (k < pricing.forward)
                   for k in s["strike"])
        # EXACTLY the selector's mid deltas, strike by strike
        legs = [o for o in chain.by_right(side) if o.tradeable]
        solved = _solve_side(legs, side, pricing).solved
        for k, d, iv in zip(s["strike"], s["mid_delta"], s["mid_iv"]):
            assert d == (solved[k].delta if k in solved else None)
            assert iv == (solved[k].iv if k in solved else None)
        assert s["truncated"] == 0 and s["otm_strikes"] == n


def test_a_side_is_capped_and_says_by_how_much():
    options = tuple(
        OptionQuote(f"P{k}", float(k), "put", -0.1, 0.10, 0.20) for k in range(4500, 5000, 5)
    )
    row = build_ladder(ChainSnapshot("SPXW", 5000.0, options, 0.0), None)
    assert len(row["put"]["strike"]) == LADDER_MAX_STRIKES_PER_SIDE
    assert row["put"]["truncated"] == 100 - LADDER_MAX_STRIKES_PER_SIDE
    assert row["put"]["strike"][0] == 4995.0, "nearest first"


def test_with_no_regime_strikes_are_recorded_and_deltas_are_null():
    row = build_ladder(_strip(), None)
    _encodes(row)
    assert row["cause"] == "no_time_to_settlement"
    assert row["put"]["strike"] and set(row["put"]["mid_delta"]) == {None}


@pytest.mark.parametrize("field", ["bid", "ask"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_hostile_quote_never_refuses_the_row(field, value):
    chain = _strip()
    poisoned = tuple(
        OptionQuote(o.symbol, o.strike, o.right, o.delta,
                    value if field == "bid" else o.bid,
                    value if field == "ask" else o.ask)
        if o.strike == 4975.0 else o
        for o in chain.options
    )
    _encodes(build_ladder(ChainSnapshot("SPXW", 5000.0, poisoned, 0.0), _regime()))


def test_a_hostile_spot_never_refuses_the_row():
    chain = _strip()
    _encodes(build_ladder(ChainSnapshot("SPXW", math.nan, chain.options, math.inf), None))


# -- in the loop -------------------------------------------------------------------


async def test_one_row_per_et_minute_whatever_the_tick_decides(audit: AuditLog):
    """Blackout ticks included: those days are the release-day question's data."""
    audit.start()
    clock = {"at": time(12, 0, 5)}
    loop = _loop(audit, _StubStrategy(None, reason="event_blackout"))
    loop._clock_et = lambda: clock["at"]
    for seconds in (5, 10, 55):
        clock["at"] = time(12, 0, seconds)
        await loop.evaluate_once()
    clock["at"] = time(12, 1, 0)
    await loop.evaluate_once()
    await audit.aclose()
    assert len(_records(audit, LADDER_CATEGORY)) == 2
    assert len(_records(audit, "signal.none")) == 4, "every tick still decided"


async def test_a_failing_ladder_never_costs_the_tick(audit: AuditLog, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("ladder defect")

    monkeypatch.setattr(ladder_mod, "build_ladder", boom)
    audit.start()
    loop = _loop(audit, _StubStrategy(None, reason="credit_below_min"))
    await loop.evaluate_once()
    await loop.evaluate_once()          # same minute: not retried
    await audit.aclose()
    assert _records(audit, LADDER_CATEGORY) == []
    assert len(_records(audit, "signal.none")) == 2


# -- structural -------------------------------------------------------------------


def test_nothing_in_src_reads_the_ladder_back():
    """Written, never read: only the orchestrator imports the module, it calls only
    `maybe_record`, and discards what that returns. The category string lives in the
    ladder module alone. `tools/` is out of scope — reading it back is a tool's job."""
    importers, literal_sites = [], []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC)
        if "tools" in rel.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "argus.signal.ladder":
                importers.append(str(rel))
            if isinstance(node, ast.Constant) and node.value == LADDER_CATEGORY:
                literal_sites.append(str(rel))
    assert importers == ["orchestrator.py"]
    assert literal_sites == ["signal/ladder.py"]
    tree = ast.parse((_SRC / "orchestrator.py").read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "_ladder"]
    assert [c.func.attr for c in calls] == ["maybe_record"]
    statements = [n for n in ast.walk(tree) if isinstance(n, ast.Expr) and n.value in calls]
    assert len(statements) == len(calls), "its result must be discarded, never used"
