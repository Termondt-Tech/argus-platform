"""The read-only evaluability measurement (Fix D step 1).

`_Escalation` is a pure rising/falling edge with no hysteresis, so a flapping
condition defeats it — and on 2026-08-13 every flap was a Tier 1 write. Whether
Fix C removed that is a question about episode COUNT and episode DURATION, and
this is what answers it. The tool decides nothing; it reconstructs the episodes
and prints the decision rule.
"""

from __future__ import annotations

from datetime import date

import pytest

from argus.clock import et_day_bounds_utc_iso
from argus.tools.evaluability import (
    BLIND_CLOSED,
    BLIND_OPENED,
    CHECK,
    DEGRADED,
    _pct,
    read_counts,
    read_episodes,
    read_rate,
    read_span,
)

TRADING_DATE = date(2026, 8, 13)
WINDOW = et_day_bounds_utc_iso(TRADING_DATE)

POSITION = "argus-open-20260813-call-7835-7840"
OTHER = "argus-open-20260813-put-7700-7695"


def _ts(hh: int, mm: int, ss: float = 0.0) -> str:
    """UTC. 14:00 UTC is 10:00 ET, inside the trading day either way."""
    return f"2026-08-13T{hh:02d}:{mm:02d}:{ss:06.3f}000Z"


@pytest.fixture
async def running(audit):
    """The Tier 2 consumer has to be alive: half the categories here are telemetry,
    and a queued record that never lands is a record this tool cannot count."""
    audit.start()
    yield audit
    await audit.aclose()


async def _record(audit, category, ts, key=POSITION, reasons=()):
    """One record at a controlled timestamp.

    Telemetry is drained immediately rather than at the end, because `_build`
    stamps `ts` inside the background consumer — batching them would give every
    Tier 2 record the last clock value set, and every duration here would be a lie.
    """
    audit._clock = lambda: ts
    payload = {"external_identifier": key, "candidate_id": None,
               "unknown_reasons": list(reasons)}
    if category in (BLIND_OPENED, BLIND_CLOSED):
        await audit.record(category, payload)
    else:
        audit.telemetry(category, payload)
        await audit._queue.join()


async def test_a_clean_open_and_close_is_one_episode(db, running):
    await _record(running, BLIND_OPENED, _ts(14, 0, 0), reasons=["credit_multiple"])
    await _record(running, BLIND_CLOSED, _ts(14, 0, 3.5))

    [position] = read_episodes(db, *WINDOW)
    assert position.key == POSITION
    [episode] = position.episodes
    assert episode.duration_s(episode.opened_at) == 3.5
    assert episode.reasons == ("credit_multiple",)
    assert not episode.open_ended


async def test_two_positions_blind_at_once_are_two_episodes(db, running):
    """`_Escalation` keys on `external_identifier`, so "distinct episodes" has to
    mean the same thing here — otherwise a second position going blind while the
    first already is would be counted as no new page, when it fires one."""
    await _record(running, BLIND_OPENED, _ts(14, 0, 0), key=POSITION)
    await _record(running, BLIND_OPENED, _ts(14, 0, 1), key=OTHER)
    await _record(running, BLIND_CLOSED, _ts(14, 0, 2), key=POSITION)
    await _record(running, BLIND_CLOSED, _ts(14, 0, 5), key=OTHER)

    positions = read_episodes(db, *WINDOW)
    assert {p.key for p in positions} == {POSITION, OTHER}
    assert all(len(p.episodes) == 1 for p in positions)


async def test_an_episode_that_never_recovers_is_flagged_not_dropped(db, running):
    """The worst case is a position that goes blind and stays blind. Measuring it
    as zero-length — or leaving it out — would hide exactly that."""
    await _record(running, BLIND_OPENED, _ts(14, 0, 0))
    await _record(running, CHECK, _ts(15, 30, 0))

    [position] = read_episodes(db, *WINDOW)
    [episode] = position.episodes
    assert episode.open_ended
    _, last = read_span(db, *WINDOW)
    assert episode.duration_s(last) == 5400.0


async def test_a_flap_is_many_short_episodes_on_ONE_position(db, running):
    """The 2026-08-13 signature, and the thing a settle allowance would address:
    the same position entering and leaving the state over and over, each stretch
    short enough to have cleared on its own."""
    for i in range(6):
        await _record(running, BLIND_OPENED, _ts(14, i, 0.0))
        await _record(running, BLIND_CLOSED, _ts(14, i, 1.0))

    [position] = read_episodes(db, *WINDOW)
    assert len(position.episodes) == 6
    durations = [e.duration_s(e.opened_at) for e in position.episodes]
    assert durations == [1.0] * 6
    # Recovered, then blind again 59s later, five times over.
    assert position.gaps_s() == [59.0] * 5


async def test_counts_report_the_tier_each_category_was_written_at(db, running):
    """Which tier is the whole point: a Tier 1 write is awaited, holds the chain
    lock and fsyncs under FULL, and a Tier 2 one does none of that. A regression
    that moved `degraded` back to Tier 1 must show up here."""
    await _record(running, BLIND_OPENED, _ts(14, 0, 0))
    await _record(running, BLIND_CLOSED, _ts(14, 0, 1))
    await _record(running, DEGRADED, _ts(14, 0, 2))
    await _record(running, CHECK, _ts(14, 0, 3))

    counts = read_counts(db, *WINDOW)
    assert counts[BLIND_OPENED] == (1, 1)      # (tier, count)
    assert counts[BLIND_CLOSED] == (1, 1)
    assert counts[DEGRADED] == (2, 1)
    assert counts[CHECK] == (2, 1)


async def test_the_write_rate_splits_by_tier(db, running):
    """`synchronous = FULL` costs an fsync per Tier 1 COMMIT, so the Tier 1 rate is
    the number that justifies the setting — not the total, which Tier 2 dominates."""
    await _record(running, BLIND_OPENED, _ts(14, 0, 0))
    await _record(running, BLIND_CLOSED, _ts(14, 0, 1))
    for i in range(8):
        await _record(running, CHECK, _ts(14, 0, 2 + i))

    assert read_rate(db, *WINDOW) == {1: 2, 2: 8}


async def test_the_ET_day_window_is_the_same_one_marking_uses(db, running):
    """00:30 UTC on the 14th is still the 13th in ET. Bucketing on the UTC date
    would file the tail of a session under the next trading day."""
    running._clock = lambda: "2026-08-14T00:30:00.000000Z"
    await running.record(BLIND_OPENED, {"external_identifier": POSITION})

    assert read_episodes(db, *et_day_bounds_utc_iso(date(2026, 8, 14))) == []
    assert len(read_episodes(db, *WINDOW)) == 1


def test_percentiles_do_not_walk_off_the_end():
    assert _pct([], 0.9) == 0.0
    assert _pct([1.0], 0.99) == 1.0
    assert _pct([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert _pct([1.0, 2.0, 3.0, 4.0], 0.99) == 4.0


def test_percentiles_never_interpolate_or_round_up_to_the_larger_value():
    """Every number printed has to be an episode that actually happened — a
    settle allowance gets sized from these, and Python's round-half-to-even would
    put a two-element p50 on the LARGER value."""
    assert _pct([45.0, 90.0], 0.5) == 45.0
    assert _pct([1.0, 2.0, 3.0], 0.5) == 2.0
    assert all(_pct([1.0, 2.0, 3.0], q) in (1.0, 2.0, 3.0) for q in (0.1, 0.5, 0.9, 1.0))


async def test_a_day_that_held_no_position_is_INCONCLUSIVE_not_DONE(db, running, capsys):
    """THE false negative. The watchdog only evaluates OPEN positions, so a day
    that never filled writes no `watchdog.check` at all — and "zero blind episodes"
    there is not evidence the flap is gone, it is evidence nothing was ever
    watched. Reading it as DONE would close the finding on a day that never
    tested it."""
    from argus.tools.evaluability import _report, read_counts, read_rate, read_span

    running._clock = lambda: _ts(14, 0)
    await running.record("gate.decision", {"allowed": False})

    _report(TRADING_DATE, "x.db", read_counts(db, *WINDOW), [],
            read_rate(db, *WINDOW), read_span(db, *WINDOW), 1.0)
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out
    assert "DONE" not in out


async def test_a_watched_day_with_no_blind_episodes_IS_done(db, running, capsys):
    """The other side of the same guard: ticks happened, none of them were blind."""
    from argus.tools.evaluability import _report, read_counts, read_rate, read_span

    for i in range(20):
        await _record(running, CHECK, _ts(14, 0, i))

    _report(TRADING_DATE, "x.db", read_counts(db, *WINDOW), [],
            read_rate(db, *WINDOW), read_span(db, *WINDOW), 1.0)
    out = capsys.readouterr().out
    assert "No blind episodes across 20 watched tick(s)" in out
    assert "DONE" in out and "INCONCLUSIVE" not in out
