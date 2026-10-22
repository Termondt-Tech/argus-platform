"""The per-hour RSS and gen-2 rates are a TRAILING slope, never `cumulative / uptime`.

2026-09-01: at uptime 447 s `/status` reported 43 MB/h and 16.1 gen2/h on a process
whose RSS and gen-2 count had not moved since 81 s — every byte and both sweeps were
startup cost, divided by a small uptime. The sustained figures were 1.5 MB/h and
0.039/h. → docs/decisions/2026-09-01-status-per-hour-rates-startup-contaminated.md
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import timedelta

import pytest

import argus.status as status_mod
from argus.audit.log import AuditLog
from argus.status import RATE_WINDOW_S, TrailingRates
from test_status import NOW, _make_service

MB = 1024 * 1024
#: An arbitrary monotonic origin — the rates take DURATION-clock seconds, never a
#: wall-clock datetime, so an NTP step cannot skew a window.
T0 = 5_000.0


def _at(seconds: float) -> float:
    return T0 + seconds


def _run(rates: TrailingRates, until_s: float, rss_at, gen2_at, step_s: float = 60.0):
    """Sample every `step_s` from the first tick (never at t=0, as the service)."""
    t = step_s
    while t <= until_s:
        rates.add(_at(t), rss_at(t), gen2_at(t))
        t += step_s


# The 2026-09-01 process: a 5.3 MB / 2-sweep bump inside the first 81 s, flat after.
def _incident_rss(t: float) -> int:
    return 100 * MB + (5_345_280 if t >= 81 else int(5_345_280 * t / 81))


def _incident_gen2(t: float) -> int:
    return 2 if t >= 81 else 0


def test_the_incident_reads_no_rate_where_it_read_43_mb_per_hour():
    rates = TrailingRates()
    _run(rates, 447, _incident_rss, _incident_gen2)
    # The old formula at this instant: 5,345,280 / (447/3600) = 43 MB/h, 16.1 gen2/h.
    assert rates.rate(_at(447), _incident_rss(447), _incident_gen2(447)) is None


def test_once_startup_has_left_the_window_a_flat_process_reads_zero():
    rates = TrailingRates()
    _run(rates, 2 * 3600, _incident_rss, _incident_gen2)
    r = rates.rate(_at(2 * 3600), _incident_rss(2 * 3600), _incident_gen2(2 * 3600))
    assert r is not None
    # Old formula at 2 h: 2.6 MB/h and 1.0 gen2/h — of a process that is not growing.
    assert r.rss_bytes_per_hour == 0.0
    assert r.gen2_per_hour == 0.0
    assert RATE_WINDOW_S <= r.window_s < RATE_WINDOW_S + 60.0


def test_a_real_leak_is_measured_at_its_true_rate_under_the_startup_bump():
    leak = 1.5 * MB  # per hour, the 51.7 h figure from finding 7

    def rss(t: float) -> int:
        return _incident_rss(t) + int(leak * t / 3600)

    def gen2(t: float) -> int:
        return _incident_gen2(t) + int(t // 1800)  # two sweeps an hour, sustained

    rates = TrailingRates()
    _run(rates, 3 * 3600, rss, gen2)
    r = rates.rate(_at(3 * 3600), rss(3 * 3600), gen2(3 * 3600))
    assert r is not None
    assert r.rss_bytes_per_hour == pytest.approx(leak, rel=1e-3)
    assert r.gen2_per_hour == pytest.approx(2.0, abs=0.05)


def test_the_history_stays_bounded_however_long_the_process_lives():
    rates = TrailingRates()
    _run(rates, 72 * 3600, _incident_rss, _incident_gen2)
    assert len(rates._samples) <= RATE_WINDOW_S / 60.0 + 2


def test_a_repeated_or_earlier_instant_cannot_make_a_zero_or_negative_span():
    rates = TrailingRates()
    rates.add(_at(3600), 100 * MB, 0)
    rates.add(_at(60), 50 * MB, 0)  # dropped
    rates.add(_at(3600), 50 * MB, 0)  # same instant, dropped
    assert [s.at for s in rates._samples] == [_at(3600)]
    assert rates.rate(_at(3600), 100 * MB, 0) is None


def test_missing_rss_nulls_the_rss_rate_and_keeps_the_gen2_rate():
    """Dev Macs have no /proc; gen-2 is platform-independent and still measurable."""
    rates = TrailingRates()
    rates.add(_at(0), None, 3)
    r = rates.rate(_at(3600), None, 5)
    assert r is not None
    assert r.rss_bytes_per_hour is None
    assert r.gen2_per_hour == 2.0


def test_nothing_in_status_divides_by_uptime():
    """The defect was `x / (uptime_s / 3600)`. Pinned structurally, so a new rate
    field cannot reintroduce the startup bias by the obvious route."""
    tree = ast.parse(inspect.getsource(status_mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            names = {
                n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", None)
                for n in ast.walk(node.right)
            }
            assert "uptime_s" not in names, ast.unparse(node)


async def test_the_service_reports_the_trailing_rate_on_both_renderings(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    mono = [1_000.0]
    rss = [200 * MB]
    svc, _, _ = _make_service(audit, read_rss=lambda: rss[0], monotonic=lambda: mono[0])
    try:
        svc.sample()
        mono[0], rss[0] = 1_000.0 + 3600.0, 203 * MB
        snap = await svc.assemble()
    finally:
        await audit.aclose()

    js = snap.to_json_dict()["process"]
    assert js["rss_growth_bytes_per_hour"] == pytest.approx(3 * MB)
    assert js["rate_window_s"] == 3600.0
    assert js["gc_gen2_per_hour"] is not None
    assert "3.0 MB/h over last 60 min" in snap.to_html()


async def test_the_sampler_runs_on_its_own_cadence_and_stops_cleanly(db):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    ticks = iter(float(i) for i in range(10_000))
    svc, _, _ = _make_service(audit, monotonic=lambda: next(ticks))
    try:
        svc.start_sampling(interval_s=0.001)
        for _ in range(100):
            await asyncio.sleep(0.001)
            if len(svc._rates._samples) >= 3:
                break
        await svc.stop_sampling()
        assert len(svc._rates._samples) >= 3
        assert svc._sampler is None
        await svc.stop_sampling()  # idempotent
    finally:
        await audit.aclose()


async def test_the_rates_run_on_the_monotonic_clock_not_the_wall_clock(db):
    """A wall clock stepped by an hour (NTP, a VM resume) must not move the rate: the
    span is a DURATION. With the wall clock jumping and the monotonic one steady, the
    window is still exactly the monotonic hour."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    wall = [NOW]
    mono = [0.0]
    rss = [200 * MB]
    svc, _, _ = _make_service(
        audit, read_rss=lambda: rss[0], clock=lambda: wall[0], monotonic=lambda: mono[0]
    )
    try:
        svc.sample()
        mono[0], rss[0] = 3600.0, 203 * MB
        wall[0] = NOW + timedelta(hours=3)          # the wall clock stepped forward
        snap = await svc.assemble()
    finally:
        await audit.aclose()
    js = snap.to_json_dict()["process"]
    assert js["rate_window_s"] == 3600.0
    assert js["rss_growth_bytes_per_hour"] == pytest.approx(3 * MB)


async def test_a_failed_sample_does_not_kill_the_sampler(db, caplog):
    """The sampler used to die on its first exception, silently: the rates then aged
    against a frozen anchor with nothing to say so."""
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    calls = [0]

    def flaky_rss():
        calls[0] += 1
        if calls[0] == 2:  # call 1 is the service's start-of-process baseline
            raise OSError("statm vanished")
        return 100 * MB

    ticks = iter(float(i) for i in range(10_000))
    svc, _, _ = _make_service(audit, read_rss=flaky_rss, monotonic=lambda: next(ticks))
    try:
        svc.start_sampling(interval_s=0.001)
        for _ in range(200):
            await asyncio.sleep(0.001)
            if len(svc._rates._samples) >= 3:
                break
        assert svc._sampler is not None and not svc._sampler.done()
        assert len(svc._rates._samples) >= 3          # it kept sampling after the failure
        assert "sample failed; continuing" in caplog.text
        await svc.stop_sampling()
    finally:
        await audit.aclose()
