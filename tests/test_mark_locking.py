"""`tools.mark` is a WRITER and must respect the single-writer lock.

Marking appends `shadow.outcome` to the same hash chain a live session is
writing, seeding `seq` from the head at open. Without the lock, marking
mid-session steals sequence numbers and wedges that session's audit writer —
the 2026-07-21 failure, reached by a different door.

`--dry-run` is the deliberate exception: it opens read-only and writes nothing,
so it stays usable while a session runs.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from argus.config import Config
from argus.db import apply_migrations, connect
from argus.db.lock import DatabaseLock
from argus.tools import mark as mark_tool
from support import config_dict


def _args(**overrides):
    base = dict(
        config="unused", date="2026-07-22", from_feed=False, settlement=7500.0,
        timeout=30.0, dry_run=False, remark=False, force=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _seed_would_submit(conn) -> None:
    """One valid shadow.would_submit on 2026-07-22, so there is an idea to mark."""
    import json

    payload = {
        "proposal": {"contracts": 4, "max_loss": 1780.0, "symbol": "SPXW call 7530/7535"},
        "signal": {
            "side": "call", "underlying": "SPXW", "width": 5.0, "credit": 0.55,
            "short": {"strike": 7530.0, "delta": 0.15, "mid": 1.08, "symbol": "s"},
            "long": {"strike": 7535.0, "delta": 0.08, "mid": 0.53, "symbol": "l"},
        },
    }
    conn.execute(
        "INSERT INTO audit_log (seq, ts, tier, category, payload, prev_hash, hash) "
        "VALUES (1, '2026-07-22T17:40:00.000000Z', 1, 'shadow.would_submit', ?, 'x', 'y')",
        (json.dumps(payload),),
    )


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "argus.db"
    conn = connect(path)
    apply_migrations(conn)
    _seed_would_submit(conn)
    conn.close()
    cfg = Config.from_dict(config_dict(audit={"db_path": str(path)}))
    monkeypatch.setattr(mark_tool.Config, "load", staticmethod(lambda _p: cfg))
    return path


async def test_write_path_is_refused_while_another_writer_holds_the_lock(db: Path):
    lock = DatabaseLock(db)
    lock.acquire()
    try:
        with pytest.raises(SystemExit) as exc:
            await mark_tool._run(_args())
        message = str(exc.value)
        assert "already writing" in message
        # The message must point at the way out, not just the failure.
        assert "--dry-run" in message
    finally:
        lock.release()


async def test_dry_run_works_while_the_lock_is_held(db: Path, capsys):
    """A live session must not block previewing the scoring."""
    lock = DatabaseLock(db)
    lock.acquire()
    try:
        await mark_tool._run(_args(dry_run=True))
    finally:
        lock.release()
    assert "nothing written" in capsys.readouterr().out


async def test_write_path_succeeds_and_releases_the_lock(db: Path):
    await mark_tool._run(_args())
    # Lock must be free afterwards, or the next session cannot start.
    probe = DatabaseLock(db)
    probe.acquire()
    probe.release()


async def test_lock_is_released_even_when_marking_fails(db: Path):
    await mark_tool._run(_args())                    # marks 2026-07-22
    with pytest.raises(SystemExit, match="already marked"):
        await mark_tool._run(_args())                # refuses to double-mark
    probe = DatabaseLock(db)                         # ...but must not leak the lock
    probe.acquire()
    probe.release()


async def test_dry_run_never_creates_a_database(tmp_path: Path, monkeypatch):
    """A typo'd path must not silently report zero ideas."""
    missing = tmp_path / "nope.db"
    cfg = Config.from_dict(config_dict(audit={"db_path": str(missing)}))
    monkeypatch.setattr(mark_tool.Config, "load", staticmethod(lambda _p: cfg))
    with pytest.raises(SystemExit, match="database not found"):
        await mark_tool._run(_args(dry_run=True))
    assert not missing.exists()
