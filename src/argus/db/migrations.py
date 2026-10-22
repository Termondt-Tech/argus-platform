"""Numbered, immutable migration runner (spec §5 — direct reuse from TOMIC).

Migrations are `NNNN_name.sql` files in `migrations/`, applied in ascending
numeric order inside a transaction, and recorded in `schema_migrations`. Applied
files are immutable — never edit one after it has shipped; add a new file. On
startup the runner applies only the unapplied migrations, so it is safe to call
unconditionally.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def _discover(migrations_dir: Path) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        m = _FILENAME_RE.match(path.name)
        if not m:
            raise ValueError(
                f"migration filename must match NNNN_name.sql, got {path.name!r}"
            )
        found.append((int(m.group(1)), path))
    numbers = [n for n, _ in found]
    if len(set(numbers)) != len(numbers):
        raise ValueError(f"duplicate migration numbers in {migrations_dir}")
    if numbers != sorted(numbers):
        raise ValueError("migrations discovered out of order")  # pragma: no cover
    return found


def _ensure_registry(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )


def applied_migrations(conn: sqlite3.Connection) -> set[int]:
    _ensure_registry(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def apply_migrations(
    conn: sqlite3.Connection, migrations_dir: Path | None = None
) -> list[int]:
    """Apply all unapplied migrations in order. Returns versions applied this call."""
    migrations_dir = migrations_dir or _MIGRATIONS_DIR
    _ensure_registry(conn)
    already = applied_migrations(conn)
    newly_applied: list[int] = []
    for version, path in _discover(migrations_dir):
        if version in already:
            continue
        sql = path.read_text()
        # `executescript` implicitly commits any pending transaction, so we cannot
        # wrap it in an outer BEGIN/COMMIT via `conn.execute`. Instead embed the
        # transaction control (and the registry insert) inside the script itself,
        # so the whole migration is one atomic unit that rolls back on failure.
        # `version` is an int and `path.stem` is regex-constrained to
        # [0-9]/[a-z0-9_], so literal interpolation here is injection-safe.
        script = (
            "BEGIN;\n"
            f"{sql}\n"
            "INSERT INTO schema_migrations (version, name) "
            f"VALUES ({version}, '{path.stem}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            # A failure mid-script leaves the transaction open; unwind it so the
            # partially-applied DDL does not persist.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        newly_applied.append(version)
    return newly_applied
