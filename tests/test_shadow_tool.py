"""Session-body failure-path behaviour (the helpers `tools.shadow` reuses).

Regression cover for the droplet's first run (2026-07-23), where a broker
read-timeout was REPLACED in the output by `sqlite3.OperationalError: unable to
open database file` — a crash in the shutdown path, on a fresh volume where the
database did not exist yet. The real error was buried and the investigation went
looking at the network.

These helpers moved from `argus.tools.shadow` into `argus.session` when the loop
became always-on; the tool is now a thin wrapper over the same session body.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argus.db import apply_migrations, connect
from argus.session import SessionAborted, _counts, _fetch_accounts, _format_counts


# -- the status line's `(+N)` deltas ---------------------------------------
#
# The `{...}` block is an ALL-TIME ledger. On 2026-08-05 that was read as a rate
# three times in one day — once stopping a running session mid-market — because a
# counter frozen for an hour and a counter climbing every tick render identically.
# The deltas make movement a visual property of the line.


def test_a_frozen_counter_renders_bare_and_a_moving_one_carries_its_delta():
    baseline = {"account.refresh_failed": 285, "gate.decision": 25375}
    now = {"account.refresh_failed": 285, "gate.decision": 25435}
    out = _format_counts(now, baseline)
    # THE distinction the whole change exists for.
    assert "account.refresh_failed=285," in out
    assert "account.refresh_failed=285(" not in out
    assert "gate.decision=25435(+60)" in out


def test_a_category_first_seen_this_session_shows_its_whole_count_as_the_delta():
    """Honest rather than tidy: it really did all happen this session."""
    assert _format_counts({"entry.finished": 3}, {}) == "{entry.finished=3(+3)}"


def test_output_is_sorted_so_the_line_does_not_reshuffle_between_prints():
    """The underlying dict mixes the seed query's order with first-write order, so
    a new category would otherwise appear in the middle of the line one print and
    at the end the next."""
    out = _format_counts({"z.last": 1, "a.first": 1, "m.middle": 1}, {})
    assert out.index("a.first") < out.index("m.middle") < out.index("z.last")


def test_an_empty_tally_renders_without_crashing():
    assert _format_counts({}, {}) == "{}"


# -- _counts() must never raise from the shutdown path --------------------


def test_counts_on_missing_database_returns_empty(tmp_path: Path):
    """THE bug: startup failed before the DB existed, so cleanup crashed."""
    assert _counts(str(tmp_path / "does-not-exist.db")) == {}


def test_counts_does_not_create_the_database(tmp_path: Path):
    missing = tmp_path / "nope.db"
    _counts(str(missing))
    assert not missing.exists()


def test_counts_on_a_database_without_the_table(tmp_path: Path):
    import sqlite3

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()          # a file, but no audit_log table
    assert _counts(str(empty)) == {}


def test_counts_on_an_unreadable_path_returns_empty(tmp_path: Path):
    # A directory is not a database — must degrade, not raise.
    assert _counts(str(tmp_path)) == {}


def test_counts_reads_real_categories(tmp_path: Path):
    db = tmp_path / "argus.db"
    conn = connect(db)
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO audit_log (seq, ts, tier, category, payload, prev_hash, hash) "
        "VALUES (1,'t',1,'process.start','{}','x','y')"
    )
    conn.close()
    assert _counts(str(db)) == {"process.start": 1}


# -- startup account fetch: bounded retry, clear failure ------------------


class _Boom(Exception):
    pass


async def test_fetch_accounts_returns_on_first_success(monkeypatch):
    async def ok(_session):
        return ["acct"]

    monkeypatch.setattr("argus.session.Account.get", ok)
    assert await _fetch_accounts(object(), timeout_s=30.0) == ["acct"]


async def test_fetch_accounts_retries_a_transient_failure(monkeypatch):
    calls = {"n": 0}

    async def flaky(_session):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Boom("cert hiccup")
        return ["acct"]

    monkeypatch.setattr("argus.session.Account.get", flaky)
    monkeypatch.setattr("argus.session.asyncio.sleep", lambda _s: _noop())
    assert await _fetch_accounts(object(), timeout_s=30.0) == ["acct"]
    assert calls["n"] == 3


async def test_fetch_accounts_gives_up_with_an_actionable_message(monkeypatch):
    async def always_fails(_session):
        raise _Boom("read timeout")

    monkeypatch.setattr("argus.session.Account.get", always_fails)
    monkeypatch.setattr("argus.session.asyncio.sleep", lambda _s: _noop())
    # In the always-on process a slow cert must end the DAY, not kill the
    # supervisor — so this raises the domain SessionAborted, not SystemExit.
    with pytest.raises(SessionAborted) as exc:
        await _fetch_accounts(object(), timeout_s=30.0, attempts=2)
    msg = str(exc.value)
    # Must name the real error and point somewhere useful — NOT a bare traceback.
    assert "_Boom" in msg and "read timeout" in msg
    assert "broker.http_timeout_s" in msg
    assert "OAuth login already succeeded" in msg


async def _noop():
    return None
