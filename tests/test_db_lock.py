"""Single-writer lock — the guard that makes the 2026-07-21 incident impossible.

Two processes writing one audit database both seed `seq` from the same head and
then collide, wedging the loser's recorder permanently. These tests pin the
enforcement, not the symptom.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from argus.db.lock import DatabaseLock, DatabaseLockedError


def test_acquire_and_release(tmp_path: Path):
    lock = DatabaseLock(tmp_path / "argus.db")
    assert not lock.held
    lock.acquire()
    assert lock.held
    lock.release()
    assert not lock.held


def test_release_is_idempotent(tmp_path: Path):
    lock = DatabaseLock(tmp_path / "argus.db")
    lock.acquire()
    lock.release()
    lock.release()  # must not raise


def test_context_manager(tmp_path: Path):
    db = tmp_path / "argus.db"
    with DatabaseLock(db) as lock:
        assert lock.held
    assert not DatabaseLock(db).held


def test_double_acquire_in_same_process_raises(tmp_path: Path):
    lock = DatabaseLock(tmp_path / "argus.db")
    lock.acquire()
    with pytest.raises(RuntimeError):
        lock.acquire()
    lock.release()


def test_second_process_is_refused(tmp_path: Path):
    """THE test: a second OS process cannot take the lock while we hold it.

    flock is per-process, so this genuinely needs a subprocess — two DatabaseLock
    objects in one process would not exercise the real contention.
    """
    db = tmp_path / "argus.db"
    lock = DatabaseLock(db)
    lock.acquire()
    try:
        script = textwrap.dedent(f"""
            import sys
            from argus.db.lock import DatabaseLock, DatabaseLockedError
            try:
                DatabaseLock({str(db)!r}).acquire()
            except DatabaseLockedError as exc:
                print(str(exc)); sys.exit(17)
            sys.exit(0)
        """)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
        )
        assert proc.returncode == 17, f"second process was NOT refused: {proc.stdout}{proc.stderr}"
        # The error must name the holder, or it is useless at 09:30.
        assert f"pid={os.getpid()}" in proc.stdout
        assert str(db) in proc.stdout
    finally:
        lock.release()


def test_lock_is_released_when_holder_process_dies(tmp_path: Path):
    """The kernel drops the flock on process death — no stale-lock recovery."""
    db = tmp_path / "argus.db"
    script = textwrap.dedent(f"""
        from argus.db.lock import DatabaseLock
        DatabaseLock({str(db)!r}).acquire()   # never released; process just exits
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
    )
    assert proc.returncode == 0
    DatabaseLock(db).acquire()  # must succeed; would raise if the lock were stale


def test_readers_are_not_blocked(tmp_path: Path):
    """Analysis tools open read-only and never take the lock (WAL concurrency)."""
    import sqlite3

    from argus.db import apply_migrations, connect

    db = tmp_path / "argus.db"
    conn = connect(db)
    apply_migrations(conn)
    conn.close()

    lock = DatabaseLock(db)
    lock.acquire()
    try:
        ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        assert ro.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
        ro.close()
    finally:
        lock.release()
