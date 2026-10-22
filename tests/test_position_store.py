"""Position stores: in-memory and SQLite-backed expected positions."""

from __future__ import annotations

import sqlite3

from argus.reconcile import InMemoryPositionStore, SqlitePositionStore


def test_in_memory_excludes_flats():
    store = InMemoryPositionStore({"A": 1, "B": 0})
    assert store.open_positions() == {"A": 1}  # B=0 excluded
    store.set("C", -2)
    store.set("A", 0)  # closing A removes it
    assert store.open_positions() == {"C": -2}


def test_sqlite_reads_positions_table(db: sqlite3.Connection):
    db.execute(
        "INSERT INTO positions (symbol, underlying, signed_quantity, average_open_price, opened_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("SPXW260718P05000000", "SPX", -2, 1.20, "2026-07-18T09:31:00Z"),
    )
    db.commit()
    store = SqlitePositionStore(db)
    assert store.open_positions() == {"SPXW260718P05000000": -2}


def test_sqlite_empty_is_valid(db: sqlite3.Connection):
    # A fresh Argus has no recorded positions — an empty expected map, not an error.
    assert SqlitePositionStore(db).open_positions() == {}
