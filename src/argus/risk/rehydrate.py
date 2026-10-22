"""Same-day rehydration of the daily-loss rail.

The daily max-loss breaker is a LATCH — "once tripped it stays tripped until an
explicit reset at the next trading day's session start" (`breaker.py`). That
promise held only for the life of one process. `Application._run_session_day`
builds the per-day risk state fresh, which is exactly right at a day rollover and
exactly wrong at 11:40 when the 11:20 trip is still supposed to be in force: the
new process came up untripped, and — worse — `anchor_day_pnl_if_possible`
re-anchored day P&L to the POST-loss NLV, so the loss already taken read as ~0 and
the breaker could not even re-trip on it. The day's risk budget silently reset.

Two independent facts have to survive the restart, and this module reconstructs
both from the audit log:

  - **the trip** — today's `daily_loss.breach` row. Latched state, restored
    directly into `DailyLossBreaker(initially_tripped=...)`.
  - **the anchor** — today's FIRST `day_pnl.anchored` row. The reference NLV the
    day's P&L is measured against, restored into `DailyPnl(anchor_nlv=...)`.

A THIRD fact joined them on 2026-09-21: **today's own fills** — every `order.fill`
row of the trading date — from which `DayLedger` rebuilds the day's premium cash and
open legs for the loss budget (`risk.loss_budget`). Same round trip, same failure
path: a DB that cannot be read halts like the other two. A single ROW that cannot be
parsed is NOT a halt — it makes the ledger say it cannot bound what is open, which
zeroes the budget for the day: fail closed at the rail, not at the process.

Both matter, and neither substitutes for the other. Restoring only the trip would
leave a breaker that is tripped now but measuring against a wrong baseline for
every later decision (and for /status); restoring only the anchor would leave a
breaker that re-trips on the next proposal but not before one — and not at all if
NLV happens to be unreadable, which is precisely when a rail should be loudest.

**Why the audit log and not a new table.** It already IS the same-day state
store: it is the single-writer, hash-chained, crash-durable record that migration
0003 indexed `(category, ts)` specifically so day-bounded lookups stay flat in
total history — and that migration's own comment names "today's
daily_loss.breach" as one of the lookups it serves. A new table would be a second
place for the same fact to live and disagree.

**Why the anchor needs its own record.** Nothing durably recorded the day's
reference NLV before this; it existed only inside a `DailyPnl` instance. That
record (`day_pnl.anchored`, Tier 1) is written by the session body the once, at
the moment the anchor is set, and never rewritten — the FIRST one of the day is
the day's true baseline, so repeated restarts all rehydrate to the same number
rather than ratcheting it toward whatever NLV happened to be current.

**Cert cannot exercise any of this.** Day P&L is derived from NLV and cert never
marks NLV, so `daily_pnl` is identically 0.0 there and the breaker cannot trip at
all (CLAUDE.md). "Sandbox looked fine after a restart" is not evidence — the only
proof available is the restart-simulation test, `tests/test_day_risk_rehydrate.py`.
→ docs/decisions/2026-08-20-daily-loss-breaker-restart-rehydration.md
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from argus.audit.log import AuditLog
from argus.clock import et_day_bounds_utc_iso
from argus.risk.breaker import BREACH_CATEGORY
from argus.risk.loss_budget import DayLedger, LedgerFill

#: The Tier 1 record `FillConsumer` writes once per leg movement — the loss budget's
#: rehydration source. Named here, not imported from `execution.fills`, so the risk
#: package keeps no dependency on the execution runtime.
FILL_CATEGORY = "order.fill"

#: Tier 1, written once per session by the body that sets the anchor. Read back
#: as the day's reference NLV on a same-day restart.
ANCHOR_CATEGORY = "day_pnl.anchored"

#: Tier 1, written by the supervisor when a session starts on state that was
#: RESTORED rather than fresh. Not the same event as a breach — it is the record
#: that says "this process did not start the day", which is otherwise invisible
#: in the trail and is the first thing anyone reading a restart wants to know.
RESTORED_CATEGORY = "day_risk.restored"


class DayRiskRehydrationError(RuntimeError):
    """The day's loss state could not be read back. Terminal, deliberately.

    "I cannot say whether the day's loss budget is spent" must never resolve to
    "it isn't" — that is the failure this whole module exists to close. The
    supervisor halts on it rather than starting a session on an assumption.
    """


@dataclass(frozen=True)
class DayRiskState:
    """What the audit log says about this trading date, so far."""

    breaker_tripped: bool
    anchor_nlv: float | None
    #: Today's `order.fill` rows in `seq` order, each decoded — or None for a row
    #: that exists but cannot be read, which the ledger counts as unreadable.
    fills: tuple[LedgerFill | None, ...] = ()

    @property
    def restored(self) -> bool:
        """True when this is a RESTART into a day already under way.

        False on the day's first session — nothing was found, nothing to restore,
        and the fresh-state path is correct.
        """
        return self.breaker_tripped or self.anchor_nlv is not None or bool(self.fills)

    def ledger(self) -> DayLedger:
        """A fresh `DayLedger` holding exactly today's recorded fills."""
        return DayLedger(self.fills)

    def to_payload(self) -> dict[str, Any]:
        ledger = self.ledger()
        return {
            "breaker_tripped": self.breaker_tripped,
            "anchor_nlv": self.anchor_nlv,
            "fills": len(self.fills),
            "cash_today": ledger.cash_today,
            "ledger_complete": ledger.cash_complete,
        }


async def load_day_risk_state(audit: AuditLog, *, trading_date: date) -> DayRiskState:
    """Read back the trip and the anchor for `trading_date`. One round trip.

    Goes through `AuditLog.read`, so it shares the single writer connection and
    runs off the event loop. Both queries obey that method's contract — bounded
    and index-backed by migration 0003's `(category, ts)` — and this runs ONCE per
    session start, never in a hot path.

    Raises `DayRiskRehydrationError` if the state cannot be read or cannot be
    understood. There is no "assume nothing happened" fallback.
    """
    day_start, day_end = et_day_bounds_utc_iso(trading_date)
    try:
        return await audit.read(
            lambda conn: _read_day_risk_state(conn, day_start=day_start, day_end=day_end)
        )
    except DayRiskRehydrationError:
        raise
    except Exception as exc:  # a broken/unreadable audit DB
        raise DayRiskRehydrationError(
            f"could not read the daily-loss state for {trading_date.isoformat()}: {exc}"
        ) from exc


def _read_day_risk_state(
    conn: sqlite3.Connection, *, day_start: str, day_end: str
) -> DayRiskState:
    """The read itself. READ-ONLY, both queries bounded to one ET day.

    Runs inside `AuditLog.read`, under the chain lock a crash-critical Tier 1
    write takes — so it must stay microseconds. Both queries are equality on
    `category` plus a range on `ts`, served end-to-end by migration 0003's
    composite index and flat in total history. Do NOT add an unbounded query here.

    The upper bound (`ts < day_end`) is not decoration: it is the episode
    boundary. Without it, a query keyed to a specific trading date could pick up a
    later day's rows, which is how a per-day latch leaks across days.
    """
    breach = conn.execute(
        "SELECT 1 FROM audit_log WHERE category = ? AND ts >= ? AND ts < ? LIMIT 1",
        (BREACH_CATEGORY, day_start, day_end),
    ).fetchone()
    # ORDER BY seq ASC = the FIRST anchor of the day, which is the day's true
    # start-of-day reference. A later one (from a restart that predates this fix,
    # or any future code path that writes a second) must never win — re-anchoring
    # mid-day to a post-loss NLV is the exact bug being closed here. The sort is
    # over a single ET day's rows in a category that gets at most a handful per
    # day, so it stays bounded regardless of total history.
    anchor_row = conn.execute(
        "SELECT payload FROM audit_log WHERE category = ? AND ts >= ? AND ts < ? "
        "ORDER BY seq ASC LIMIT 1",
        (ANCHOR_CATEGORY, day_start, day_end),
    ).fetchone()
    # Every fill of the day, in chain order. Bounded by the day: one row per leg
    # movement, a few dozen on a busy day, served by the same (category, ts) index.
    fill_rows = conn.execute(
        "SELECT payload FROM audit_log WHERE category = ? AND ts >= ? AND ts < ? "
        "ORDER BY seq ASC",
        (FILL_CATEGORY, day_start, day_end),
    ).fetchall()
    return DayRiskState(
        breaker_tripped=breach is not None,
        anchor_nlv=_anchor_from(anchor_row["payload"]) if anchor_row is not None else None,
        fills=tuple(_fill_from(row["payload"]) for row in fill_rows),
    )


def _fill_from(raw: str) -> LedgerFill | None:
    """One `order.fill` payload, or None if it cannot be read (see the module doc)."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return LedgerFill.from_payload(payload)


def _anchor_from(raw: str) -> float:
    """Parse the anchor NLV out of a `day_pnl.anchored` payload, or refuse.

    A row that EXISTS but cannot be read is not the same as no row: it means the
    day has an anchor whose value is lost, and adopting the fresh-start path there
    would re-anchor to a post-loss NLV — the bug. So this raises, and the
    supervisor halts, rather than silently degrading to the unsafe default.
    """
    try:
        payload = json.loads(raw)
        value = float(payload["anchor_nlv"])
    except Exception as exc:
        raise DayRiskRehydrationError(
            f"today's {ANCHOR_CATEGORY} record is unreadable ({exc}); refusing to "
            "re-anchor day P&L to the current NLV"
        ) from exc
    if not math.isfinite(value):
        raise DayRiskRehydrationError(
            f"today's {ANCHOR_CATEGORY} record holds a non-finite anchor ({value!r})"
        )
    return value


#: Tier 1, written by the watchdog BEFORE a stub continuation's order can go out
#: (2026-09-24) — the DURABLE spend of a partial close's ONE further close, on the
#: flatten-override precedent. Named here, not imported from
#: `execution.watchdog`, so the risk package keeps no dependency on the execution
#: runtime (the FILL_CATEGORY rule); a test pins the two literals equal.
STUB_CLOSE_CATEGORY = "watchdog.stub_close"


class StubCloseRehydrationError(RuntimeError):
    """Today's stub-close spends could not be read back. (2026-09-24)

    Deliberately NOT terminal, unlike `DayRiskRehydrationError`: an unreadable
    loss budget must halt (spending it twice is the unsafe direction), while an
    unreadable extra-close allowance must merely REFUSE to grant one — the
    kill-state posture. The watchdog catches this, disables the continuation for
    the session, and pages once.
    """


async def load_stub_close_spent(
    audit: AuditLog, *, trading_date: date
) -> frozenset[str]:
    """Today's spent stub allowances, as the POSITION keys the rows name.

    One round trip through `AuditLog.read`, bounded to one ET day by the same
    `(category, ts)` index the reads above ride — a stub is same-day state by
    construction (0DTE), and the candidate key embeds the trading date, so a
    yesterday identifier can never collide with today's even inside the broker's
    rolling 24h order window.

    Returns each row's `external_identifier` — the POSITION (open) identifier,
    the key every in-process watchdog allowance (`_attempted`,
    `_flatten_attempted`, `_stub_spent`) is keyed by — exactly as written. The
    identifier GRAMMAR is deliberately not applied here: it lives in
    `execution.models`, and this package keeps no dependency on the execution
    package (the FILL_CATEGORY rule). The watchdog, which owns the grammar,
    validates every key it is handed and fails toward spent on one it cannot
    parse.

    Raises `StubCloseRehydrationError` if the day's rows cannot be read or a row
    cannot be understood. A row that exists but cannot be parsed is NOT skipped:
    it names a position whose allowance is spent, and dropping it would hand a
    crash loop a fresh budget for exactly that position.
    """
    day_start, day_end = et_day_bounds_utc_iso(trading_date)
    try:
        return await audit.read(
            lambda conn: _read_stub_close_spent(
                conn, day_start=day_start, day_end=day_end
            )
        )
    except StubCloseRehydrationError:
        raise
    except Exception as exc:  # a broken/unreadable audit DB
        raise StubCloseRehydrationError(
            f"could not read the stub-close spends for {trading_date.isoformat()}: {exc}"
        ) from exc


def _read_stub_close_spent(
    conn: sqlite3.Connection, *, day_start: str, day_end: str
) -> frozenset[str]:
    """The read itself. READ-ONLY, bounded to one ET day, microseconds.

    Runs inside `AuditLog.read` under the chain lock, so it obeys the same rule
    `_read_day_risk_state` states: equality on `category` plus a range on `ts`,
    served end-to-end by migration 0003's composite index. A handful of rows a
    day at most — one per partial close that left a stub.
    """
    rows = conn.execute(
        "SELECT payload FROM audit_log WHERE category = ? AND ts >= ? AND ts < ?",
        (STUB_CLOSE_CATEGORY, day_start, day_end),
    ).fetchall()
    spent: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
            key = payload["external_identifier"]
            if not isinstance(key, str) or not key:
                raise ValueError(f"not a non-empty string: {key!r}")
        except Exception as exc:
            raise StubCloseRehydrationError(
                f"today's {STUB_CLOSE_CATEGORY} record is unreadable ({exc}); "
                "refusing to grant a stub-close allowance it may have spent"
            ) from exc
        spent.add(key)
    return frozenset(spent)


async def record_day_pnl_anchor(
    audit: AuditLog, *, trading_date: date, anchor_nlv: float
) -> None:
    """Persist the day's reference NLV. Tier 1 — this IS the rehydration source.

    Awaited durability, not telemetry: a Tier 2 line can be dropped under burst,
    and a dropped anchor is a restart that re-anchors to the wrong number. Called
    once per session, immediately after `anchor_day_pnl_if_possible` succeeds.

    `trading_date` rides in the payload for the human reading the trail; the
    lookup itself is by `ts` range, which is the indexed path.
    """
    await audit.record(
        ANCHOR_CATEGORY,
        {"trading_date": trading_date.isoformat(), "anchor_nlv": anchor_nlv},
    )
