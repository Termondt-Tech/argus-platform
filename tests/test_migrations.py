"""Migration runner: applies in order, idempotent, creates the audit schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from argus.db import apply_migrations, applied_migrations, connect
from argus.db.migrations import _discover


def test_apply_creates_audit_log(tmp_path: Path):
    conn = connect(tmp_path / "m.db")
    applied = apply_migrations(conn)
    assert 1 in applied
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "audit_log" in tables
    assert "schema_migrations" in tables


def test_apply_is_idempotent(tmp_path: Path):
    conn = connect(tmp_path / "m.db")
    first = apply_migrations(conn)
    second = apply_migrations(conn)
    # 0001_initial + 0002_positions + 0003_audit_status_index + 0004_orders
    # + 0005_order_leg_fill_prices
    assert first == [1, 2, 3, 4, 5]
    assert second == []  # nothing new applied the second time
    assert applied_migrations(conn) == {1, 2, 3, 4, 5}


def test_discover_rejects_bad_filename(tmp_path: Path):
    (tmp_path / "nope.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="NNNN_name.sql"):
        _discover(tmp_path)


def test_discover_orders_numerically(tmp_path: Path):
    (tmp_path / "0002_b.sql").write_text("SELECT 1;")
    (tmp_path / "0010_c.sql").write_text("SELECT 1;")
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    versions = [v for v, _ in _discover(tmp_path)]
    assert versions == [1, 2, 10]


def test_failed_migration_rolls_back(tmp_path: Path):
    conn = connect(tmp_path / "m.db")
    bad_dir = tmp_path / "migrations"
    bad_dir.mkdir()
    (bad_dir / "0001_broken.sql").write_text("CREATE TABLE ok (id INTEGER); INVALID SQL;")
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(conn, bad_dir)
    # The half-applied migration must not have left its table or a registry row.
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "ok" not in tables
    assert applied_migrations(conn) == set()


# -- durability posture reaches the connection ---------------------------


@pytest.mark.parametrize(
    "mode, expected",
    [("OFF", 0), ("NORMAL", 1), ("FULL", 2), ("EXTRA", 3)],  # SQLite's numeric codes
)
def test_connect_applies_the_configured_synchronous(tmp_path, mode, expected):
    conn = connect(tmp_path / "s.db", synchronous=mode)
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == expected
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_connect_defaults_to_normal(tmp_path):
    conn = connect(tmp_path / "d.db")
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()


def test_connect_rejects_an_unknown_synchronous(tmp_path):
    with pytest.raises(ValueError, match="synchronous"):
        connect(tmp_path / "bad.db", synchronous="'; DROP TABLE audit_log; --")
