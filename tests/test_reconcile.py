"""Reconciliation: pure comparison across every discrepancy class + orchestration."""

from __future__ import annotations

from argus.account.fake import FakeBrokerAccount
from argus.account.models import Position
import pytest

from argus.reconcile import (
    MISSING_AT_BROKER,
    QUANTITY_MISMATCH,
    UNEXPECTED_AT_BROKER,
    InMemoryPositionStore,
    Reconciler,
    mismatch_halts_process,
    reconcile_positions,
)


def test_clean_when_maps_match():
    r = reconcile_positions({"A": 1, "B": -2}, {"A": 1, "B": -2})
    assert r.ok is True
    assert r.discrepancies == ()


def test_unexpected_broker_position():
    r = reconcile_positions({}, {"A": 1})
    assert r.ok is False
    assert r.discrepancies[0].kind == UNEXPECTED_AT_BROKER
    assert r.discrepancies[0].expected_qty == 0
    assert r.discrepancies[0].broker_qty == 1


def test_missing_broker_position():
    r = reconcile_positions({"A": 1}, {})
    assert r.discrepancies[0].kind == MISSING_AT_BROKER


def test_quantity_mismatch():
    r = reconcile_positions({"A": 2}, {"A": 1})
    assert r.discrepancies[0].kind == QUANTITY_MISMATCH
    assert (r.discrepancies[0].expected_qty, r.discrepancies[0].broker_qty) == (2, 1)


def test_multiple_discrepancies_sorted():
    r = reconcile_positions({"B": 1, "C": 3}, {"A": 1, "C": 5})
    kinds = {d.symbol: d.kind for d in r.discrepancies}
    assert kinds == {
        "A": UNEXPECTED_AT_BROKER,
        "B": MISSING_AT_BROKER,
        "C": QUANTITY_MISMATCH,
    }


def test_result_payload_is_serializable():
    import json

    r = reconcile_positions({}, {"A": 1})
    json.dumps(r.as_payload())  # must not raise


async def test_reconciler_clean_against_flat_broker():
    reconciler = Reconciler(FakeBrokerAccount([]), InMemoryPositionStore())
    result = await reconciler.reconcile()
    assert result.ok is True


async def test_reconciler_flags_unexpected_broker_position():
    broker = FakeBrokerAccount(
        [Position("SPXW260718P05000000", "SPX", "Equity Option", -1, 1.2, 0.9)]
    )
    reconciler = Reconciler(broker, InMemoryPositionStore())
    result = await reconciler.reconcile()
    assert result.ok is False
    assert result.discrepancies[0].kind == UNEXPECTED_AT_BROKER


async def test_reconciler_matches_expected_and_broker():
    pos = Position("SPXW260718P05000000", "SPX", "Equity Option", -1, 1.2, 0.9)
    store = InMemoryPositionStore({pos.symbol: -1})
    reconciler = Reconciler(FakeBrokerAccount([pos]), store)
    result = await reconciler.reconcile()
    assert result.ok is True


async def test_reconciler_ignores_flat_positions():
    # A broker position reported flat (signed 0) is not a discrepancy.
    flat = Position("SPXW260718P05000000", "SPX", "Equity Option", 0, 1.2, 0.9)
    reconciler = Reconciler(FakeBrokerAccount([flat]), InMemoryPositionStore())
    result = await reconciler.reconcile()
    assert result.ok is True


# -- environment boundary: which envs hard-halt on a mismatch -----------------


@pytest.mark.parametrize(
    "environment,halts",
    [("shadow", False), ("sandbox", True), ("live", True)],
)
def test_mismatch_halts_process_boundary_is_shadow_vs_everything_else(environment, halts):
    # The single source of truth both reconcile call sites gate on. The boundary is
    # shadow vs. the rest — NOT live vs. the rest — so sandbox proves out the
    # hard-halt path before it matters on a live account (spec §10 step 2).
    assert mismatch_halts_process(environment) is halts
