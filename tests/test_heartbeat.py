"""LoopHeartbeat: lag measurement, breach alerting, sustained-stall escalation."""

from __future__ import annotations

import asyncio
import time

from argus.heartbeat import LoopHeartbeat


def _make(alerts, *, on_sustained=None, threshold=0.02, sustained=3):
    return LoopHeartbeat(
        interval_s=0.01,
        lag_threshold_s=threshold,
        sustained_breach_count=sustained,
        alert=lambda kind, detail: alerts.append((kind, detail)),
        on_sustained=on_sustained,
    )


# -- deterministic unit tests of the observe/breach logic ------------------


async def test_healthy_samples_do_not_breach():
    alerts: list = []
    hb = _make(alerts)
    for _ in range(10):
        hb._observe(0.001)
    assert hb.breach_count == 0
    assert alerts == []


async def test_breach_alerts_once_on_entry():
    alerts: list = []
    hb = _make(alerts, threshold=0.02)
    hb._observe(0.05)  # breach -> alert
    hb._observe(0.05)  # still breaching -> no new "entry" alert
    kinds = [k for k, _ in alerts]
    assert kinds.count("loop_lag_breach") == 1
    assert hb.breach_count == 2


async def test_recovery_resets_consecutive_but_never_alerts():
    """A recovery is a log line, NOT an alert.

    It used to alert, which made a flapping stall cost two Slack messages per
    episode. The droplet's ~10s account-liveness refresh flaps this detector all
    day (13,406 breaches in eleven days), so the pair was ~700 messages an hour of
    something nobody can act on. The breach that opened the episode already
    alerted; `after_breaches` lives in the metrics.
    """
    alerts: list = []
    hb = _make(alerts, threshold=0.02)
    hb._observe(0.05)
    hb._observe(0.001)  # recovered
    kinds = [k for k, _ in alerts]
    assert "loop_lag_recovered" not in kinds
    assert kinds == ["loop_lag_breach"]
    # The state machine still resets, so a subsequent breach is a fresh entry.
    hb._observe(0.05)
    assert [k for k, _ in alerts].count("loop_lag_breach") == 2


async def test_a_flapping_stall_costs_one_alert_per_episode():
    """The shape the droplet actually produces: breach, recover, breach, recover...

    One alert per episode is the contract the notifier's per-kind throttle is then
    free to collapse further. Two would double a volume that is already the
    problem.
    """
    alerts: list = []
    hb = _make(alerts, threshold=0.02)
    for _ in range(20):
        hb._observe(0.05)   # stall
        hb._observe(0.001)  # catches back up
    assert [k for k, _ in alerts] == ["loop_lag_breach"] * 20


async def test_sustained_escalates_once():
    alerts: list = []
    tripped: list = []

    async def on_sustained(lag):
        tripped.append(lag)

    hb = _make(alerts, on_sustained=on_sustained, threshold=0.02, sustained=3)
    for _ in range(5):  # five consecutive breaches, sustained threshold is 3
        hb._observe(0.05)
    await asyncio.sleep(0)  # let the escalation task run
    assert hb.sustained_events == 1  # fires exactly once per episode
    assert tripped == [0.05]
    assert [k for k, _ in alerts].count("loop_lag_sustained") == 1


# -- integration: a real blocking call on the loop is detected -------------


async def test_detects_real_blocking_stall():
    alerts: list = []
    hb = _make(alerts, threshold=0.03)
    hb.start()
    await asyncio.sleep(0.03)  # a few healthy cycles
    time.sleep(0.1)  # block the event loop synchronously
    await asyncio.sleep(0.05)  # let the heartbeat wake late and measure
    await hb.stop()

    assert hb.breach_count >= 1
    assert hb.max_lag >= 0.05
    assert any(k == "loop_lag_breach" for k, _ in alerts)
