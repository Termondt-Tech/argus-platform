"""Single-writer advisory lock on the audit database.

The hash chain requires EXACTLY ONE writer process. `AuditLog` assigns `seq` and
`prev_hash` from in-memory state seeded once at startup by `_load_head()`, so a
second process opening the same file loads the same head and both then claim the
same `seq`. SQLite does nothing to stop this — file-level concurrent writers are
allowed, and the chain's single-writer requirement was, until now, an assumption
with nothing enforcing it.

The consequence is not a clean error. The loser of the race takes a
`UNIQUE constraint failed: audit_log.seq`, and because `_advance()` only runs on
a successful write its `_seq` never moves — so it retries the same `seq` forever
and the black-box recorder is dead for the life of the process. This happened for
real on 2026-07-21: a stray `python -m argus` run from the repo root (where
`audit.db_path` resolves to the LIVE argus.db) stole three seq numbers from a
running shadow session and cost ~80 minutes of unrecorded signal data.

Design notes:

- **`flock` on a sidecar `<db>.lock`, not on the DB file itself.** Locking the
  database would fight SQLite's own locking. A sidecar is inert.
- **Released by the KERNEL on process death**, including SIGKILL and a hard
  crash. There is no stale-lock problem and nothing to clean up manually — which
  is exactly why this is an flock rather than a PID file.
- **Readers are never blocked.** Only the writer takes this lock. Analysis tools
  and the reconciler's read connection open the DB read-only and are unaffected,
  so WAL read concurrency is preserved.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from pathlib import Path

logger = logging.getLogger("argus.db.lock")


class DatabaseLockedError(RuntimeError):
    """Another process already holds the write lock on this database."""


class DatabaseLock:
    """Exclusive, non-blocking advisory lock guarding one database file."""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self._db_path = Path(db_path)
        self._lock_path = Path(f"{db_path}.lock")
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._lock_path

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Take the lock, or raise `DatabaseLockedError` naming the holder.

        Non-blocking on purpose: waiting would just delay the process behind
        another writer that intends to hold the lock for the whole session. The
        correct response is to refuse to start and say who has it.
        """
        if self._fd is not None:
            raise RuntimeError("lock already held by this process")

        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = self._read_holder(fd)
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise DatabaseLockedError(
                    f"another Argus process is already writing to {self._db_path} "
                    f"({holder}). Refusing to start: two writers corrupt the audit "
                    f"chain's seq allocation. Stop the other process, or point this "
                    f"one at a different database with ARGUS_CONFIG."
                ) from exc
            raise

        # Record who holds it, for the error message the next process will print.
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"pid={os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:  # advisory metadata only — never fail the lock over it
            logger.debug("could not write holder pid into %s", self._lock_path)

        self._fd = fd
        logger.info("acquired write lock on %s", self._db_path)

    def release(self) -> None:
        """Release the lock. Idempotent.

        The lock FILE is deliberately left behind: unlinking it races with
        another process that has already opened it and is about to flock, which
        would let two writers both believe they hold the lock. An empty sidecar
        file is a trivial price for closing that hole.
        """
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def _read_holder(self, fd: int) -> str:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            text = os.read(fd, 64).decode(errors="replace").strip()
            return text or "holder pid unknown"
        except OSError:
            return "holder pid unknown"

    def __enter__(self) -> "DatabaseLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
