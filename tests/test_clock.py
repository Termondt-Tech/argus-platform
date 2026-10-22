"""Exchange-time helpers: the trading day is ET, not the host's local date."""

from __future__ import annotations

from datetime import datetime

from argus.clock import ET, et_now, et_time_of_day, et_today


def test_et_now_is_eastern():
    assert et_now().tzinfo is ET


def test_et_today_matches_eastern_date():
    # Must track ET, which can differ from the host's local date near midnight.
    assert et_today() == datetime.now(ET).date()


def test_et_time_of_day_matches_eastern_clock():
    now = datetime.now(ET)
    tod = et_time_of_day()
    # Same hour unless we straddled an hour boundary between the two calls.
    assert tod.hour in (now.hour, (now.hour + 1) % 24)


def test_the_readers_have_one_day_window_and_one_ts_format():
    """`et_day_bounds_utc_iso` and `UTC_TS_FMT` are THE rule for "records belonging to
    this ET trading date". The day-bounded readers (`tools/*`, `shadow/`) had grown
    five private copies of both — behaviour-identical, which is exactly how one of
    them quietly drifts (`zerobid`'s precedent). Collapsed 2026-09-18; this holds it.

    Scoped to the readers. `execution/store.py` stamps its own write-time `ts` and is
    trading-path code, deliberately out of this sweep.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "argus"
    offenders = []
    for sub in ("tools", "shadow"):
        for path in sorted((src / sub).rglob("*.py")):
            text = path.read_text()
            if "%Y-%m-%dT%H:%M:%S.%fZ" in text:
                offenders.append(f"{path.relative_to(src)}: a ts-format literal")
            if "\ndef _utc_window(trading_date" in text:
                offenders.append(f"{path.relative_to(src)}: a private day window")
    assert not offenders, offenders
