"""AuditLog — the two-tier hash-chained recorder (spec §6).

Design constraints this satisfies:

- **Single serialized writer of the chain.** Both tiers share one hash chain, so
  the `prev_hash`/`seq` linkage must be assigned by exactly one writer at a time.
  An `asyncio.Lock` (`_chain_lock`) guards the in-memory `_seq`/`_last_hash` and
  the DB write for both tiers, so the chain can never race even though Tier 1 and
  the Tier 2 consumer run on the same loop.

- **Tier 1 awaits durability, off the loop.** `record()` holds the lock, builds
  the record, then routes the blocking INSERT+commit through `asyncio.to_thread`.
  The caller's coroutine awaits the write (ordering + crash-survival guarantee),
  but the event loop stays free while the disk I/O is in flight — so the watchdog
  and kill switch stay responsive through a slow-write outlier (spec §4).

- **Tier 2 never blocks the loop and never OOMs.** `telemetry()` is a synchronous
  non-awaiting enqueue onto a *bounded* queue. If the queue is full it drops the
  line and counts the drop rather than blocking a producer or growing without
  limit — the fail-closed choice during exactly the volatility burst when Tier 2
  volume spikes. It alerts before the queue fills.

- **A write failure is TERMINAL and halts trading.** "Everything writes to the
  audit log before the next action proceeds" is only a guarantee if a broken log
  stops the next action. A failed write latches `failed`, alerts, and fires
  `on_failure` (wired to the kill switch by `Application`); every subsequent
  `record()` raises `AuditWriteError` rather than retrying.

  There is deliberately NO auto-recovery — no re-reading the head and retrying.
  With the single-writer lock (`db/lock.py`) in place, a `seq` collision means an
  invariant has genuinely been violated; silently re-seating the counter would
  let two writers interleave into one chain, producing a log that verifies
  perfectly and means nothing. Refusing is the only honest response.

  Learned the hard way on 2026-07-21: before this, a failed write left `_seq`
  un-advanced, so the writer retried the same `seq` every 5s forever while the
  signal loop kept evaluating and deciding — unrecorded, and looking healthy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from argus.audit.models import GENESIS_HASH, AuditRecord, Tier
from argus.clock import UTC_TS_FMT

logger = logging.getLogger("argus.audit")

T = TypeVar("T")

# alert(kind, detail) — pluggable so this can be wired to the phone-reachable
# alerting path later. Defaults to logging.
AlertFn = Callable[[str, dict[str, Any]], None]
ClockFn = Callable[[], str]
# on_failure(detail) — invoked once when the log latches failed. Wired to the
# kill switch by Application so a dead recorder halts new entries.
FailureFn = Callable[[str], Awaitable[None]]


class AuditWriteError(RuntimeError):
    """The audit log could not persist a record and has latched failed.

    Terminal by design: the black-box recorder is the precondition for every
    other action, so callers must stop rather than continue unrecorded.
    """


def _default_alert(kind: str, detail: dict[str, Any]) -> None:
    logger.warning("audit alert: %s %s", kind, detail)


def _utc_now_iso() -> str:
    # The format lives in `clock.UTC_TS_FMT` because the day-bounded readers
    # (`status`, `risk.rehydrate`) compare against it lexicographically; a second
    # copy here that drifted would make those queries quietly match nothing.
    return datetime.now(timezone.utc).strftime(UTC_TS_FMT)


class AuditLog:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        tier2_queue_maxsize: int,
        tier2_queue_alert_depth: int,
        alert: AlertFn | None = None,
        clock: ClockFn | None = None,
        on_failure: FailureFn | None = None,
    ) -> None:
        self._conn = conn
        self._chain_lock = asyncio.Lock()
        self._alert = alert or _default_alert
        self._clock = clock or _utc_now_iso
        # Settable after construction: Application builds the kill switch from
        # this AuditLog, so the wiring can only close the loop afterwards.
        self.on_failure = on_failure
        self._failed: str | None = None

        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
            maxsize=tier2_queue_maxsize
        )
        self._alert_depth = tier2_queue_alert_depth
        self._dropped = 0
        self._consumer: asyncio.Task[None] | None = None

        self._seq, self._last_hash = self._load_head()
        # Per-category totals, kept in memory. See `counts` and `_load_counts`.
        self._category_counts: dict[str, int] = self._load_counts()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the Tier 2 background writer. Tier 1 works without this."""
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.create_task(self._consume(), name="audit-tier2")

    async def aclose(self) -> None:
        """Drain outstanding Tier 2 items, then stop the consumer."""
        if self._consumer is not None:
            await self._queue.join()
            self._consumer.cancel()
            try:
                await self._consumer
            except asyncio.CancelledError:
                pass
            self._consumer = None

    # -- Tier 1: sequenced, awaited ---------------------------------------

    async def record(self, category: str, payload: dict[str, Any]) -> AuditRecord:
        """Append a Tier 1 (crash-critical) event and await its durability.

        Returns once the row is committed to disk. Raises if the payload is not
        JSON-serializable — a Tier 1 event that can't be hashed must fail loudly,
        not be silently dropped.
        """
        if self._failed is not None:
            raise AuditWriteError(
                f"audit log has failed and will not accept further records: {self._failed}"
            )
        _reject_unserializable(category, payload)
        async with self._chain_lock:
            rec = self._build(Tier.SEQUENCED, category, payload)
            try:
                await asyncio.to_thread(self._insert, rec)
            except Exception as exc:
                await self._latch_failure(exc, category, Tier.SEQUENCED)
                raise AuditWriteError(
                    f"failed to persist tier 1 record {category!r} at seq {rec.seq}: {exc}"
                ) from exc
            self._advance(rec)
            return rec

    # -- Tier 2: telemetry, non-blocking ----------------------------------

    def telemetry(self, category: str, payload: dict[str, Any]) -> bool:
        """Enqueue a Tier 2 (non-safety) event. Never blocks; never awaits.

        Returns False if the event was dropped because the bounded queue is full.
        Dropping telemetry under extreme burst is acceptable and deliberate — it
        is the fail-closed choice over unbounded growth and an OOM-kill.
        """
        try:
            self._queue.put_nowait((category, payload))
        except asyncio.QueueFull:
            self._dropped += 1
            self._alert("tier2_queue_full", {"dropped_total": self._dropped})
            return False
        depth = self._queue.qsize()
        if depth >= self._alert_depth:
            self._alert("tier2_queue_depth", {"depth": depth, "maxsize": self._queue.maxsize})
        return True

    async def _consume(self) -> None:
        while True:
            category, payload = await self._queue.get()
            try:
                if self._failed is not None:
                    continue  # drained, not written — the log is done
                if not _is_serializable(payload):
                    # Telemetry payloads shouldn't be unserializable, but if one
                    # is, drop it with an alert rather than killing the writer.
                    self._dropped += 1
                    self._alert("tier2_unserializable", {"category": category})
                    continue
                async with self._chain_lock:
                    rec = self._build(Tier.TELEMETRY, category, payload)
                    try:
                        await asyncio.to_thread(self._insert, rec)
                    except Exception as exc:
                        # NOT "one bad line" — a write failure means the chain
                        # itself is unwritable. Latch and stop; retrying would
                        # spin on the same seq forever (the 2026-07-21 failure).
                        await self._latch_failure(exc, category, Tier.TELEMETRY)
                        continue
                    self._advance(rec)
            except Exception:  # a bad line must not kill the background writer
                logger.exception("tier2 audit write failed for category=%s", category)
            finally:
                self._queue.task_done()

    # -- failure latch -----------------------------------------------------

    async def _latch_failure(self, exc: Exception, category: str, tier: Tier) -> None:
        """Mark the log permanently failed, alert, and halt trading.

        Idempotent — only the first failure alerts and fires `on_failure`.
        """
        if self._failed is not None:
            return
        self._failed = f"{type(exc).__name__}: {exc}"
        detail = {
            "error": self._failed,
            "category": category,
            "tier": int(tier),
            "seq_attempted": self._seq + 1,
        }
        logger.critical(
            "AUDIT LOG WRITE FAILED — the black-box recorder is down; halting new "
            "entries. category=%s tier=%s error=%s",
            category, int(tier), self._failed,
        )
        self._alert("audit_write_failed", detail)
        if self.on_failure is not None:
            try:
                await self.on_failure(self._failed)
            except Exception:
                # The halt path itself failing must not mask the original
                # failure — the alert above has already gone out.
                logger.exception("audit on_failure handler raised")

    # -- chain internals ---------------------------------------------------

    def _build(self, tier: Tier, category: str, payload: dict[str, Any]) -> AuditRecord:
        return AuditRecord.build(
            seq=self._seq + 1,
            ts=self._clock(),
            tier=tier,
            category=category,
            payload=payload,
            prev_hash=self._last_hash,
        )

    def _advance(self, rec: AuditRecord) -> None:
        self._seq = rec.seq
        self._last_hash = rec.hash
        # Only reached AFTER a successful insert, on both tiers, which is what
        # makes the in-memory tally exactly equal to the table's contents rather
        # than an approximation of it. A failed write `raise`s (Tier 1) or
        # `continue`s (Tier 2) before getting here, and a dropped Tier 2 line never
        # builds a record at all.
        self._category_counts[rec.category] = self._category_counts.get(rec.category, 0) + 1

    def _insert(self, rec: AuditRecord) -> None:
        # Runs in a worker thread. Serialized by _chain_lock, so single-threaded
        # access to the connection is guaranteed despite check_same_thread=False.
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                "INSERT INTO audit_log (seq, ts, tier, category, payload, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.seq,
                    rec.ts,
                    rec.tier,
                    rec.category,
                    json.dumps(rec.payload, sort_keys=True, ensure_ascii=False, allow_nan=False),
                    rec.prev_hash,
                    rec.hash,
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _load_head(self) -> tuple[int, str]:
        row = self._conn.execute(
            "SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0, GENESIS_HASH
        return int(row["seq"]), str(row["hash"])

    def _load_counts(self) -> dict[str, int]:
        """Seed the per-category tally from the table. ONCE, at construction.

        This is the one full scan, and it is deliberately here rather than on the
        read path. `session.py`'s status line used to run this same `GROUP BY`
        against a freshly-opened connection **on the event loop, every 15 seconds**,
        so the stall it caused grew with the table — ~88k rows on the droplet at
        2026-08-05 and accumulating ~14k a day. Paying it once during `startup()`
        (which already migrates, takes the writer flock, and in sandbox/live makes a
        broker round trip) instead of 1,500 times a session turns an unbounded
        recurring cost into a bounded one-off.

        Routing it through `read()` was considered and REJECTED: that method's own
        contract forbids exactly this ("Do NOT add an unbounded or full-scan query
        to a caller of this method"), because it holds `_chain_lock` — so a scan
        there would delay the crash-critical Tier 1 writes spec §6/§9 protect, in
        direct proportion to table size. That is strictly worse than the bug.

        No error handling, deliberately, matching `_load_head`: by the time this
        runs the table has already been read, so a failure here is a genuinely
        broken database and the process must not start. (`session._counts` is the
        one that must survive a missing file — it runs from the shutdown path.)
        """
        rows = self._conn.execute(
            "SELECT category, COUNT(*) FROM audit_log GROUP BY category"
        ).fetchall()
        return {str(category): int(n) for category, n in rows}

    # -- introspection / verification -------------------------------------

    @property
    def failed(self) -> str | None:
        """The failure reason if the log has latched failed, else None."""
        return self._failed

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def counts(self) -> dict[str, int]:
        """All-time records per category — from MEMORY, never a query.

        Same numbers the old `SELECT category, COUNT(*) ... GROUP BY category`
        produced, at O(number of categories) instead of O(rows): seeded once at
        construction and incremented on every successful write. The equality is
        exact rather than approximate, and it holds for a specific reason — the
        process-level `flock` (`db/lock.py`) guarantees nothing else can write to
        this table while it is open, so "what was there at startup plus what I have
        written since" IS the table.

        Intended for the session status line, which polls every 15s. A copy, so a
        caller cannot mutate the recorder's state.

        NOTE this is a CUMULATIVE ledger across the database's whole life, not a
        rate. Differencing two readings is the only way to see what is happening
        now — on 2026-08-05 a frozen `marketdata.disconnected: 20` in this block was
        read as ongoing disconnects when it meant the opposite (see CLAUDE.md,
        "The Slack alert storm").
        """
        return dict(self._category_counts)

    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run a READ-ONLY query on the log's own connection, off the event loop.

        The monitoring surface (`/status`) reads back recent records and a couple
        of small counts. It must NOT open a second connection (the `mode=ro` URI
        pattern is for outside dev/analysis tools that aren't the single writer
        process); it is running INSIDE the writer process, so it shares this
        connection — the one the writer lock protects.

        Two disciplines make that safe:

        - **Serialized with the writer.** `_chain_lock` guards every write's
          `to_thread(self._insert)`. Acquiring it here means a status read can
          never touch `self._conn` from a worker thread while a write is touching
          it from another — the exact hazard `check_same_thread=False` opens up.
        - **Off the loop.** The query itself runs in `asyncio.to_thread`, so the
          single event loop the watchdog and kill switch share stays free while
          SQLite works (spec §4), exactly as Tier 1 writes do.

        The cost of sharing the lock is real and deliberate, not the path of least
        resistance: this read contends for the SAME lock a crash-critical Tier 1
        write takes, and Tier 1 write latency is precisely what spec §6/§9 protect
        during a volatility spike (the watchdog needing to log a stop-loss exit
        *now*). Reusing the connection was a deliberate design choice — `/status`
        runs inside the single writer process and must not open a competing handle.
        It is acceptable only under two conditions, and BOTH must hold for every
        caller:

          1. Reads are RARE — a manual phone reload, never an automated hot path.
          2. Every query is index-backed and BOUNDED. The status queries are: the
             recent-N tail (reverse `seq`/rowid scan, O(N)), the latest
             kill/breach lookups (tiny categories), and the would_submit COUNT,
             which uses migration 0003's `(category, ts)` index to touch only
             today's rows — flat in total history. Measured sub-millisecond even
             at ~500k rows.

        **Do NOT add an unbounded or full-scan query to a caller of this method.**
        A scan here lengthens the hold on the safety-critical writer's lock in
        direct proportion to table size. If a future status field genuinely needs
        a heavy query, move it OFF this lock — onto a dedicated read-only WAL
        connection — rather than delaying Tier 1 writes.

        `fn` receives the live connection and MUST only read — there is no second
        write path here, and a write would corrupt the chain's single-writer
        invariant.
        """
        async with self._chain_lock:
            return await asyncio.to_thread(fn, self._conn)

    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run a WRITE against the process's own connection, off the event loop.

        The twin of `read()`, and it exists for the same reason: this database
        holds more than the audit chain. The `positions` table reconciliation
        reads back (migration 0002) and the `orders`/`order_legs` ledger
        (migration 0004) live in the same file, and the process holds ONE writer
        connection under ONE exclusive `flock` (`db/lock.py`). Opening a second
        writer handle to reach those tables would sidestep the process-level lock
        from inside the very process it protects, and put two handles in a
        busy-timeout race for the same WAL — exactly the collision the
        single-writer rule exists to prevent.

        So order recording goes through here instead: same connection, same
        `_chain_lock`, same `asyncio.to_thread` hand-off. Holding the chain lock
        means a fill can never be booked from a worker thread while a Tier 1
        record is being written from another.

        `fn` OWNS ITS OWN TRANSACTION — it should `BEGIN`/`COMMIT` explicitly, so
        a multi-table update (a leg's applied quantity and the position it
        produced) is atomic. Two rules for callers, both load-bearing:

          1. **Do not touch the audit log from inside `fn`.** `record()` and
             `telemetry()`'s consumer take the same non-reentrant lock this
             method already holds — calling either would deadlock. Audit the
             result AFTER `write()` returns.
          2. **Keep the work BOUNDED**, same discipline as `read()`. This lock is
             the one a crash-critical Tier 1 write needs during a volatility
             spike; a long transaction here delays it directly. Fill application
             touches a handful of rows by primary key, which is the scale this is
             sized for.
        """
        async with self._chain_lock:
            return await asyncio.to_thread(fn, self._conn)

    def verify_chain(self) -> None:
        """Recompute the whole chain and raise ChainIntegrityError on any break.

        Checks: no gaps in `seq`, each `prev_hash` links to the prior `hash`, and
        each stored `hash` matches a recomputation of the row's contents.
        """
        rows = self._conn.execute(
            "SELECT seq, ts, tier, category, payload, prev_hash, hash "
            "FROM audit_log ORDER BY seq ASC"
        ).fetchall()
        prev_hash = GENESIS_HASH
        expected_seq = 1
        for row in rows:
            if row["seq"] != expected_seq:
                raise ChainIntegrityError(
                    f"seq gap: expected {expected_seq}, found {row['seq']}"
                )
            if row["prev_hash"] != prev_hash:
                raise ChainIntegrityError(
                    f"broken link at seq {row['seq']}: prev_hash does not match seq {row['seq'] - 1}"
                )
            rec = AuditRecord(
                seq=row["seq"],
                ts=row["ts"],
                tier=row["tier"],
                category=row["category"],
                payload=json.loads(row["payload"]),
                prev_hash=row["prev_hash"],
                hash=row["hash"],
            )
            if rec.recompute_hash() != row["hash"]:
                raise ChainIntegrityError(f"hash mismatch at seq {row['seq']} — row tampered")
            prev_hash = row["hash"]
            expected_seq += 1


class ChainIntegrityError(Exception):
    """Raised when the audit hash chain fails verification."""


def _is_serializable(payload: dict[str, Any]) -> bool:
    try:
        json.dumps(payload, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


def _reject_unserializable(category: str, payload: dict[str, Any]) -> None:
    if not _is_serializable(payload):
        raise ValueError(
            f"Tier 1 audit payload for category={category!r} is not JSON-serializable"
        )
