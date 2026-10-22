"""PreTradeGate: fail-closed allow/deny across every rail, all audited Tier 1."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from support import fresh_loss_budget
from argus.audit.log import AuditLog
from argus.config import Config
from support import make_config
from argus.risk.breaker import DailyLossBreaker
from argus.risk.gate import PreTradeGate
from argus.risk.killswitch import KillSwitch
from argus.risk.models import EntryProposal, RiskSnapshot



def _config(**risk_overrides) -> Config:
    return make_config(risk=risk_overrides)


# A proposal and snapshot that pass every check; individual tests break one thing.
_GOOD_PROPOSAL = EntryProposal(symbol="SPXW", contracts=1, max_loss=425.0)
_GOOD_SNAPSHOT = RiskSnapshot(
    open_position_count=0,
    daily_pnl=-100.0,
    account_nlv=100_000.0,   # size cap = 0.02 * 100k = 2000 > 425
    market_data_age_s=0.5,
    account_stream_age_s=1.0, loss_budget=fresh_loss_budget(),
)


def _gate(audit: AuditLog, killswitch: KillSwitch, breaker: DailyLossBreaker, cfg: Config) -> PreTradeGate:
    return PreTradeGate(config=cfg, audit=audit, killswitch=killswitch, breaker=breaker)


@pytest.fixture
def wiring(audit: AuditLog):
    cfg = _config()
    ks = KillSwitch(audit)
    breaker = DailyLossBreaker(audit, daily_max_loss=cfg.risk.daily_max_loss)
    gate = _gate(audit, ks, breaker, cfg)
    return audit, ks, breaker, gate


async def test_allows_clean_proposal(wiring):
    audit, _ks, _b, gate = wiring
    decision = await gate.evaluate(_GOOD_PROPOSAL, _GOOD_SNAPSHOT)
    assert decision.allowed is True
    assert decision.reasons == ()
    # The decision is recorded Tier 1 before returning.
    row = audit._conn.execute(
        "SELECT tier, category, payload FROM audit_log ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert row["tier"] == 1 and row["category"] == "gate.decision"
    assert json.loads(row["payload"])["allowed"] is True


async def test_denies_when_kill_switch_engaged(wiring):
    audit, ks, _b, gate = wiring
    await ks.engage("manual", source="phone")
    decision = await gate.evaluate(_GOOD_PROPOSAL, _GOOD_SNAPSHOT)
    assert decision.allowed is False
    assert "kill_switch_engaged" in decision.reasons


async def test_denies_and_trips_breaker_on_daily_loss(wiring):
    audit, _ks, breaker, gate = wiring
    snap = replace(_GOOD_SNAPSHOT, daily_pnl=-1500.0)
    decision = await gate.evaluate(_GOOD_PROPOSAL, snap)
    assert decision.allowed is False
    assert "daily_loss_breaker_tripped" in decision.reasons
    assert breaker.tripped is True  # evaluating the gate tripped the breaker


# -- the concurrent limit, in SPREADS (fixed 2026-08-13) ----------------------
#
# `max_concurrent_positions` is a SPREAD count; the broker reports LEGS. Comparing
# them directly made a cap of 2 permit exactly ONE vertical and denied 402 of 404
# gate decisions on the first live-fire day. Test config cap: 2 spreads = 4 legs.


@pytest.mark.parametrize(
    "legs, spreads, allowed",
    [
        (0, 0, True),    # flat
        (1, 1, True),    # half a vertical filled — one spread, one slot left
        (2, 1, True),    # one full vertical open, one slot left
        (3, 2, False),   # a vertical plus a stray leg — at the cap
        (4, 2, False),   # two full verticals — at the cap
        (5, 3, False),   # over
    ],
)
async def test_the_concurrent_limit_counts_spreads_not_legs(wiring, legs, spreads, allowed):
    """THE regression test for 2026-08-13. Before the fix, 2 legs (one vertical)
    denied against a cap of 2; now it allows, and 4 legs is what saturates it.

    Odd counts round UP, and that direction is deliberate: a transient odd leg count
    is real (`broker_legs: 1` was observed during fill propagation that same day),
    and half a vertical is a position, not half a licence to open another."""
    _audit, _ks, _b, gate = wiring
    snapshot = replace(_GOOD_SNAPSHOT, open_position_count=legs)
    assert snapshot.open_spread_equivalent == spreads
    decision = await gate.evaluate(_GOOD_PROPOSAL, snapshot)
    assert decision.allowed is allowed
    assert ("concurrent_limit" in decision.reasons) is not allowed


async def test_denies_on_concurrent_limit(wiring):
    _audit, _ks, _b, gate = wiring
    snap = replace(_GOOD_SNAPSHOT, open_position_count=4)  # cap is 2 SPREADS
    decision = await gate.evaluate(_GOOD_PROPOSAL, snap)
    assert decision.allowed is False
    assert "concurrent_limit" in decision.reasons


async def test_a_cap_of_one_permits_exactly_one_vertical(audit: AuditLog):
    """The value deployed to sandbox with this fix. Both legs of one spread must not
    consume the slot twice — that double-count IS the defect — but a second spread
    must still be refused."""
    cfg = _config(max_concurrent_positions=1)
    gate = _gate(
        audit,
        KillSwitch(audit),
        DailyLossBreaker(audit, daily_max_loss=cfg.risk.daily_max_loss),
        cfg,
    )
    one_open = await gate.evaluate(
        _GOOD_PROPOSAL, replace(_GOOD_SNAPSHOT, open_position_count=0)
    )
    assert one_open.allowed is True

    for legs in (1, 2, 3):
        decision = await gate.evaluate(
            _GOOD_PROPOSAL, replace(_GOOD_SNAPSHOT, open_position_count=legs)
        )
        assert decision.allowed is False, f"{legs} legs must saturate a cap of 1 spread"
        assert "concurrent_limit" in decision.reasons


async def test_the_audit_payload_carries_both_units(wiring):
    """A `concurrent_limit` reason is unreadable without knowing which unit the
    limit was in — so the record carries the raw broker leg count AND the spread
    equivalent it was actually compared against."""
    audit, _ks, _b, gate = wiring
    await gate.evaluate(_GOOD_PROPOSAL, replace(_GOOD_SNAPSHOT, open_position_count=3))
    row = audit._conn.execute(
        "SELECT payload FROM audit_log ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    snap = json.loads(row["payload"])["snapshot"]
    assert snap["open_position_count"] == 3      # broker legs, original key kept
    assert snap["open_spread_equivalent"] == 2


async def test_denies_when_size_cap_exceeded(wiring):
    _audit, _ks, _b, gate = wiring
    # max_loss 3000 > 0.02 * 100k = 2000
    proposal = replace(_GOOD_PROPOSAL, max_loss=3000.0)
    decision = await gate.evaluate(proposal, _GOOD_SNAPSHOT)
    assert decision.allowed is False
    assert "size_cap_exceeded" in decision.reasons


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("market_data_age_s", 5.0, "market_data_stale"),
        ("account_stream_age_s", 10.0, "account_stream_stale"),
        ("market_data_age_s", float("inf"), "market_data_stale"),
        ("market_data_age_s", -1.0, "market_data_stale"),  # clock skew fails closed
        ("account_nlv", 0.0, "invalid_account_nlv"),
    ],
)
async def test_denies_on_bad_snapshot_field(wiring, field, value, reason):
    audit, _ks, _b, gate = wiring
    snap = replace(_GOOD_SNAPSHOT, **{field: value})
    decision = await gate.evaluate(_GOOD_PROPOSAL, snap)
    assert decision.allowed is False
    assert reason in decision.reasons
    # A non-finite field must not break the audit write.
    audit.verify_chain()


async def test_denies_invalid_proposal(wiring):
    _audit, _ks, _b, gate = wiring
    decision = await gate.evaluate(replace(_GOOD_PROPOSAL, contracts=0), _GOOD_SNAPSHOT)
    assert "invalid_proposal" in decision.reasons


async def test_collects_multiple_reasons(wiring):
    audit, ks, _b, gate = wiring
    await ks.engage("manual", source="phone")
    snap = RiskSnapshot(
        open_position_count=5,
        daily_pnl=-2000.0,
        account_nlv=100_000.0,
        market_data_age_s=float("inf"),
        account_stream_age_s=1.0, loss_budget=fresh_loss_budget(),
    )
    decision = await gate.evaluate(_GOOD_PROPOSAL, snap)
    assert decision.allowed is False
    assert {
        "kill_switch_engaged",
        "daily_loss_breaker_tripped",
        "concurrent_limit",
        "market_data_stale",
    } <= set(decision.reasons)


async def test_every_evaluation_is_audited_and_chain_intact(wiring):
    audit, _ks, _b, gate = wiring
    for _ in range(3):
        await gate.evaluate(_GOOD_PROPOSAL, _GOOD_SNAPSHOT)
    n = audit._conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE category = 'gate.decision'"
    ).fetchone()[0]
    assert n == 3
    audit.verify_chain()


# -- the two ABSOLUTE size rails (chunk 5) ------------------------------------
#
# `position_size_pct` was the only size rail until chunk 5, and being relative it made
# the operator's decided per-trade cap a coincidence of account size: 2% of NLV is
# $1,250 only at an NLV of exactly $62,500. These two are absolute, independent of NLV
# and of each other, and re-checked here even though `size_contracts` already applies
# them — the sizer chooses, the gate REFUSES.


async def test_denies_a_position_over_the_absolute_dollar_cap(audit: AuditLog):
    cfg = _config(max_loss_per_position=400.0)
    gate = _gate(audit, KillSwitch(audit), DailyLossBreaker(audit, daily_max_loss=1000.0), cfg)
    # 425 fits comfortably inside 2% of a 100k NLV (2000); only the absolute cap denies.
    decision = await gate.evaluate(_GOOD_PROPOSAL, _GOOD_SNAPSHOT)
    assert decision.allowed is False
    assert "position_max_loss_exceeded" in decision.reasons
    assert "size_cap_exceeded" not in decision.reasons


async def test_denies_a_position_over_the_absolute_contract_cap(audit: AuditLog):
    cfg = _config(max_contracts_per_position=1)
    gate = _gate(audit, KillSwitch(audit), DailyLossBreaker(audit, daily_max_loss=1000.0), cfg)
    decision = await gate.evaluate(replace(_GOOD_PROPOSAL, contracts=2), _GOOD_SNAPSHOT)
    assert decision.allowed is False
    assert "contract_cap_exceeded" in decision.reasons


async def test_an_unreadable_nlv_does_not_excuse_an_oversized_position(audit: AuditLog):
    """The absolute rails are deliberately NOT chained onto the NLV branch.

    A percentage of an unreadable NLV cannot be computed, and before chunk 5 that meant
    the ONLY size check went unevaluated. The whole point of an absolute cap is that it
    still applies when the account snapshot does not.
    """
    cfg = _config(max_loss_per_position=100.0, max_contracts_per_position=1)
    gate = _gate(audit, KillSwitch(audit), DailyLossBreaker(audit, daily_max_loss=1000.0), cfg)
    snap = replace(_GOOD_SNAPSHOT, account_nlv=float("nan"))
    decision = await gate.evaluate(replace(_GOOD_PROPOSAL, contracts=3), snap)
    assert decision.allowed is False
    assert {"invalid_account_nlv", "position_max_loss_exceeded", "contract_cap_exceeded"} <= set(
        decision.reasons
    )


async def test_the_rail_values_in_force_are_recorded_with_the_decision(audit: AuditLog):
    """A reason says a cap was breached; the limits block says by how much, without
    anyone reconstructing what the config held that day."""
    cfg = _config(max_loss_per_position=400.0, max_contracts_per_position=2)
    gate = _gate(audit, KillSwitch(audit), DailyLossBreaker(audit, daily_max_loss=1000.0), cfg)
    await gate.evaluate(_GOOD_PROPOSAL, _GOOD_SNAPSHOT)
    payload = json.loads(
        audit._conn.execute(
            "SELECT payload FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()["payload"]
    )
    assert payload["limits"] == {
        "max_concurrent_positions": cfg.risk.max_concurrent_positions,
        "position_size_pct": cfg.risk.position_size_pct,
        "max_loss_per_position": 400.0,
        "max_contracts_per_position": 2,
        # The loss budget's cap (2026-09-21) — the fourth size bound's limit.
        "daily_max_loss": cfg.risk.daily_max_loss,
        # The side-balance rule (2026-09-26): whether it was on, and its band.
        "side_balance": cfg.risk.side_balance,
        "max_side_imbalance": cfg.risk.max_side_imbalance,
    }


# -- overlapping verticals defeat a leg-derived count (the 2026-08-17 incident) --


@pytest.mark.parametrize(
    "legs, ledger, expected, allowed",
    [
        (4, 0, 2, False),   # nothing from the ledger: broker-derived, as before
        (2, 3, 3, False),   # THE BUG: 3 chained spreads present as 2 broker legs
        (2, 1, 1, True),    # ledger agrees with the broker; one slot left
        (6, 1, 3, False),   # broker sees MORE than the ledger booked — broker wins
        (0, 0, 0, True),    # flat
    ],
)
def test_the_concurrent_limit_takes_the_HIGHER_of_broker_and_ledger(
    legs, ledger, expected, allowed
):
    """THE regression test for 2026-08-17.

    Chained spreads (7755/7750, 7750/7745, 7745/7740) share strikes that net to
    ZERO at the broker, so k of them report as 2 legs however large k is. A
    leg-derived count alone reads "1 spread" forever and the concurrent limit stops
    bounding anything — three spreads opened against a cap of two, ~2850 of max loss
    against a 2500 daily cap.

    Each count catches what the other cannot, so the higher governs: the broker sees
    a position the ledger never booked, the ledger sees overlap the broker cannot
    represent. Neither alone is sufficient and neither is trusted to lower the other.
    """
    snapshot = replace(
        _GOOD_SNAPSHOT, open_position_count=legs, ledger_spread_count=ledger
    )
    assert snapshot.open_spread_equivalent == expected
    assert (expected < 2) is allowed


# -- working opening orders count toward the cap (Finding 6, 2026-08-28) --------


@pytest.mark.parametrize(
    "legs, ledger, working, expected, allowed",
    [
        (0, 0, 0, 0, True),    # flat
        (0, 0, 1, 1, True),    # one order working, nothing filled — one slot left
        (2, 0, 1, 2, False),   # one filled spread + one working order — at the cap
        (0, 0, 2, 2, False),   # two working orders, nothing filled — at the cap
        (2, 3, 1, 4, False),   # working ADDS on top of the higher of broker/ledger
    ],
)
def test_working_orders_add_to_the_spread_equivalent(
    legs, ledger, working, expected, allowed
):
    """A pending order is exposure DISTINCT from a filled position, so it ADDS —
    it does not fold into the max() of the two position counts."""
    snap = replace(
        _GOOD_SNAPSHOT,
        open_position_count=legs,
        ledger_spread_count=ledger,
        working_open_spread_count=working,
    )
    assert snap.open_spread_equivalent == expected
    assert (expected < 2) is allowed


async def test_an_abandoned_working_order_refuses_a_new_entry_over_the_cap(wiring):
    """THE Finding 6 case: one filled spread, one order a walk abandoned and could
    not confirm dead. Together they are at the cap of 2 — a third must be refused."""
    _audit, _ks, _b, gate = wiring
    snap = replace(_GOOD_SNAPSHOT, open_position_count=2, working_open_spread_count=1)
    decision = await gate.evaluate(_GOOD_PROPOSAL, snap)
    assert decision.allowed is False
    assert "concurrent_limit" in decision.reasons


async def test_the_audit_payload_carries_the_working_order_count(wiring):
    audit, _ks, _b, gate = wiring
    await gate.evaluate(
        _GOOD_PROPOSAL, replace(_GOOD_SNAPSHOT, working_open_spread_count=1)
    )
    row = audit._conn.execute(
        "SELECT payload FROM audit_log ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    snap = json.loads(row["payload"])["snapshot"]
    assert snap["working_open_spread_count"] == 1
    assert snap["open_spread_equivalent"] == 1


async def test_a_ledger_count_can_only_ever_TIGHTEN_the_limit(wiring):
    """The safety direction. The ledger may add slots-consumed that the broker
    cannot see; it must never hand one back that the broker says is taken."""
    _audit, _ks, _b, gate = wiring
    broker_only = replace(_GOOD_SNAPSHOT, open_position_count=4, ledger_spread_count=0)
    ledger_lower = replace(_GOOD_SNAPSHOT, open_position_count=4, ledger_spread_count=1)

    assert broker_only.open_spread_equivalent == 2
    assert ledger_lower.open_spread_equivalent == 2, "the ledger cannot free a slot"
    assert (await gate.evaluate(_GOOD_PROPOSAL, ledger_lower)).allowed is False


async def test_the_audit_payload_says_whether_day_pnl_is_anchored(wiring):
    """`tools.eod`'s G1 read excludes an unanchored placeholder 0.0 on this flag.
    Recorded, never decided on — the decision below is the same either way."""
    audit, _ks, _b, gate = wiring
    for anchored in (False, True):
        decision = await gate.evaluate(
            _GOOD_PROPOSAL, replace(_GOOD_SNAPSHOT, daily_pnl_anchored=anchored)
        )
        assert decision.allowed is True
        row = audit._conn.execute(
            "SELECT payload FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        assert json.loads(row["payload"])["snapshot"]["daily_pnl_anchored"] is anchored
