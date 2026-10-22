"""SQLite persistence layer.

The broker is the only source of truth (spec §4). This database is a cache and
an audit log — never the thing trusted when it and the broker disagree.
"""

from argus.db.connection import connect
from argus.db.migrations import apply_migrations, applied_migrations

__all__ = ["connect", "apply_migrations", "applied_migrations"]
