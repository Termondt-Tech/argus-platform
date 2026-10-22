"""One trading day's session — the pipeline, factored out of the tool.

This is the body the always-on supervisor runs once per trading day and that
`argus.tools.shadow` runs once, time-boxed, for ad-hoc dev runs. It is the SINGLE
implementation of the pipeline — login -> reconcile -> chain -> signal loop -> the
terminal action — so the loop never lives in two places (CLAUDE.md).

**The terminal action is the ONE thing `environment` changes** (chunk 5). Everything
above it is byte-for-byte the same code in every environment:

  - shadow:       `submit=None`, so `SignalLoop` records `shadow.would_submit` and
                  routes NOTHING. Unchanged from before chunk 5, deliberately —
                  weeks of accumulated candidate data must stay comparable.
  - sandbox/live: `submit=EntryExecutor.submit`, plus the three things that only
                  make sense once orders exist: the fill consumer on the account
                  stream's existing `on_order` hook, ORDER-level reconciliation
                  alongside positions, and the position watchdog as its own task.

Those four are wired together or not at all (`_build_trading_side`). A watchdog
without the fill consumer would watch a `positions` table nobody writes and report
nothing wrong — the one failure mode it cannot detect about itself — and order
reconciliation without the fill consumer would hard-halt the morning after the first
fill. They are one switch for that reason.

**One other thing reaches that same `submit` seam, and only that seam** (2026-08-29):
a process halted with `HaltGrade.ENTRIES` runs a MONITOR-ONLY day. The trading side
is built whole — fills book, the watchdog watches, marks, closes and flattens — and
`submit` stays `None`, so the signal loop routes nothing. It is not a fifth mode and
not a new branch through the pipeline; it is `environment`'s own seam, taken for a
different reason. → docs/decisions/2026-08-29-halt-scope-split.md

It owns only per-session-day state; the process-lifetime collaborators (audit,
kill switch) are passed in. The per-day risk state (`SessionState`: breaker, day
P&L anchor, gate) is built fresh by the caller for each session, which is what
makes day-rollover correct in an always-on process — a fresh breaker can't carry
yesterday's latch, a fresh anchor can't carry yesterday's P&L. The kill switch is
deliberately NOT per-session: a trip must survive rollover.

Order of operations mirrors the safety rails:
  1. Log in (bounded, ban-aware retry — see `_login_with_retry`).
  2. Fetch the account, then RECONCILE broker vs. expected positions. The response
     to a mismatch is environment-gated (see `_reconcile_or_abort`): shadow refuses
     to run just today (records the verdict, alerts) but keeps the PROCESS alive to
     try again tomorrow — a hard halt would let a stale sandbox position, wiped
     nightly, brick every following day, and shadow holds no order state to
     protect. Sandbox/live instead treat a mismatch as the crash-recovery scenario
     §6 exists for and halt the whole process (kill switch + stop).
  3. Subscribe the underlying, wait for spot; load + subscribe the 0DTE chain.
  4. Seed account state, anchor day P&L, start the watchdog, run the loop until
     `stop`.

The loop stops when `stop` is set — the scheduler sets it at the ET close, on process
shutdown, or when the entry path ends the day (the soft side of chunk 5's exception
boundary; see `execution/entry.py`). Shadow submits NOTHING (spec §10 step 1).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime

from tastytrade import Account

from argus.account.refresher import ThreadedAccountRefresher
from argus.account.rest import BrokerAccount
from argus.account.stream import AlertStreamerAccount
from argus.alerts import AlertFn
from argus.audit.log import AuditLog
from argus.broker import (
    MissingCredentialsError,
    SessionRole,
    TastytradeSession,
    market_data_credentials,
    trading_credentials,
)
from argus.clock import et_now, et_today
from argus.config import Config, load_blackout_dates
from argus.db import connect
from argus.execution.entry import EntriesEnded, EntryExecutor
from argus.execution.fills import FillConsumer
# The watchdog's own health record crosses this seam twice-named: the source type
# is `execution.watchdog.WatchdogHealth` and the status-layer twin (what `/status`
# renders, imported bare below beside its siblings) is the same name in `status`.
# The module alias keeps the source side of the converter's annotation honest
# without shadowing the target the seam sweep reads by bare name.
from argus.execution import watchdog as watchdog_types
from argus.execution.pipeline import ExecutionStack, build_execution_stack, trades_for_real
from argus.execution.residual_orders import OutstandingResidual, ResidualOrderRecheck
from argus.execution.watchdog import (
    ChainImpliedVol,
    ExitExecutor,
    ImpairedPosition,
    ImpliedVolSource,
    LegQuotes,
    PositionWatchdog,
    SpentExitAttempt,
    UnwatchablePosition,
)
from argus.marketcalendar import MarketCalendar
from argus.marketdata.dxlink import DXLinkMarketData
from argus.orchestrator import SignalLoop, SubmitFn
from argus.reconcile import (
    BrokerUnreachable,
    HaltGrade,
    ReconciliationResult,
    Reconciler,
    SqlitePositionStore,
    halt_grade,
    mismatch_halts_process,
    reconcile_with_retry,
)
from argus.risk.gate import PreTradeGate
from argus.risk.breaker import DailyLossBreaker
from argus.risk.killswitch import KillSwitch
from argus.risk.loss_budget import DayLedger, Exposure, LossBudgetSource
from argus.risk.rehydrate import record_day_pnl_anchor
from argus.risk.snapshot import DailyPnl, RiskSnapshotBuilder
from argus.signal.live_chain import DXLinkChainSource
from argus.signal.strategy import CreditSpreadStrategy
from argus.status import (
    EntriesEndedToday,
    FlattenDeferralRowStatus,
    FlattenDeferralStatus,
    LinkHealth,
    PositionEvaluability,
    ProfitTargetStatus,
    ResidualOrderRow,
    RetiredProfitTargetRow,
    SessionStatus,
    UnclosablePosition,
    UnmanagedPosition,
    WatchdogHealth,
)

logger = logging.getLogger("argus.session")

# halt(reason, *, source, grade=HaltGrade.ALL, detail) -> engage the kill switch AND
# (for HaltGrade.ALL) request process stop. Injected by the supervisor
# (`Application._halt_process`); the seam is what lets the session body — which holds
# neither the kill switch nor the stop event — trigger a process-wide halt on a
# sandbox/live reconcile mismatch.
HaltFn = Callable[..., Awaitable[None]]

#: halted_grade() -> how wide the halt in force is, or None if not halted. The
#: supervisor's `Application.halt_grade`, read LIVE rather than captured — the halt
#: this body reacts to is very often the one its OWN reconcile just raised.
#:
#: It answers two different questions in this module and they are not the same one:
#: `_reconcile_or_abort` asks it AFTER halting, to learn whether the halt it just
#: caused actually parks the process (its own verdict may be narrow while an earlier
#: halt already latched `ALL`); `run_session` asks it before wiring the entry hook,
#: where ANY halt means monitor-only. Deliberately the AUTHORITATIVE latched state in
#: both cases, never a locally-computed grade.
#: → docs/decisions/2026-08-29-halt-scope-split.md
HaltedGradeFn = Callable[[], HaltGrade | None]


@dataclass
class SessionState:
    """Per-session-day risk state, built once per trading DAY — not per process.

    Re-instantiating (not resetting flags) is the day-rollover contract, and the
    unit it is keyed to is the TRADING DATE: a new day gets an un-tripped
    `breaker` and an un-anchored `day_pnl`, while a second session on the SAME day
    (a restart) gets both rehydrated from today's audit records by
    `Application._run_session_day`. The latch is per-day in both directions —
    cleared at rollover, preserved across a restart — which is the whole point of
    it being a DAILY loss budget.

    `gate` composes that per-day breaker with the PROCESS-lifetime kill switch, so
    a manual/latched halt still survives into the new day while the daily-loss
    latch does not.
    """

    breaker: DailyLossBreaker
    day_pnl: DailyPnl
    gate: PreTradeGate
    #: Today's premium cash and open legs from Argus's own fills — the loss budget's
    #: ledger (2026-09-21). Rehydrated from today's `order.fill` rows on the same
    #: seam as the breaker and the anchor, and fed by this day's `FillConsumer`.
    #: REQUIRED: an empty ledger on a same-day restart would hand back the day's
    #: spent budget, the 2026-08-20 bug one rail over.
    day_ledger: DayLedger
    #: The PROCESS-lifetime kill switch, carried explicitly rather than reached for
    #: inside `gate`. The entry path needs it directly — to refuse a candidate the
    #: gate approved microseconds before a trip, and to abandon a walk already in
    #: flight — and reading it off the gate's private attribute would make a safety
    #: rail depend on another component's internals. Optional so the shadow-only
    #: construction in tests and tools stays a two-line `SessionState`.
    killswitch: KillSwitch | None = None


class SessionAborted(Exception):
    """Raised to end one day's session cleanly (not a process-level failure).

    The always-on supervisor treats this as "no session today" and idles to the
    next trading day; the process keeps running.
    """


# -- helpers (shared by the supervisor and the dev tool) ----------------------


async def _wait_for_quote(md: DXLinkMarketData, symbol: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if md.latest(symbol) is not None:
            return True
        await asyncio.sleep(0.5)
    return False


async def _fetch_accounts(session, *, timeout_s: float, attempts: int = 3):
    """Fetch accounts with a bounded retry, reporting elapsed time per attempt.

    The startup account fetch was the one broker call with no retry and no
    timing: a single slow response killed the whole session with a raw
    traceback and no indication of how long it had waited. tastytrade's cert
    API is documented (see `staleness.account_refresh_timeout_s`) as hanging and
    502-ing, and the first authenticated call from a new host is exactly when
    that bites — as it did on the droplet's first run.

    Retries are for TRANSIENT failures only. On exhaustion this raises
    `SessionAborted` (NOT `SystemExit`): in the always-on process a slow cert
    must end the day, not kill the supervisor.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            accounts = await Account.get(session)
            elapsed = time.monotonic() - started
            if attempt > 1:
                print(f"  account fetch succeeded on attempt {attempt} ({elapsed:.1f}s)")
            elif elapsed > 2.0:
                # Slow-but-OK is worth surfacing: it is the early warning for
                # the failure mode this retry exists to survive.
                print(f"  note: account fetch took {elapsed:.1f}s")
            return accounts
        except Exception as exc:  # noqa: BLE001 — reported, then re-raised below
            last = exc
            elapsed = time.monotonic() - started
            print(
                f"  account fetch attempt {attempt}/{attempts} failed after "
                f"{elapsed:.1f}s: {type(exc).__name__}: {exc}"
            )
            if attempt < attempts:
                await asyncio.sleep(min(2.0 * attempt, 5.0))
    raise SessionAborted(
        f"could not fetch accounts after {attempts} attempts "
        f"(per-request timeout {timeout_s:g}s): {type(last).__name__}: {last}\n"
        "  The OAuth login already succeeded, so credentials and connectivity to "
        "the broker are fine.\n"
        "  If these were timeouts, raise `broker.http_timeout_s` in the config; "
        "the cert API is slow on a first call from a new host."
    )


def _counts(db_path: str) -> dict[str, int]:
    """Audit category counts, or `{}` when there is nothing to count.

    **SHUTDOWN PATH ONLY — never call this on a cadence.** It opens a connection
    and full-scans `audit_log`, so its cost grows with the table forever. The one
    legitimate caller is `tools.shadow`, which prints a summary AFTER
    `app.shutdown()` has closed the `AuditLog` and released the writer lock; at
    that point there is no in-memory tally left to read and no loop left to stall.
    Anything holding a live `AuditLog` must use `AuditLog.counts` instead, which
    is the same numbers from memory at O(1).

    That distinction is the whole of the 2026-08-05 fix: this function used to be
    called from the session's 15-second status print, on the event loop, ~1,500
    times a session against a table growing ~14k rows a day.

    MUST NOT RAISE. The shutdown path also executes when startup failed *before*
    the database was created — a fresh volume, a broker timeout, a refused writer
    lock. Previously `sqlite3.connect()` sat outside the `try`, so on a missing
    file it raised `unable to open database file` from inside `finally` and
    REPLACED the real error with a meaningless one. Seen on the droplet's first
    run: a broker read-timeout surfaced as a SQLite error, sending the
    investigation in the wrong direction entirely.
    """
    try:
        # mode=ro fails loudly on a missing file rather than creating one.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT category, COUNT(*) FROM audit_log GROUP BY category"
        ).fetchall()
        return {c: n for c, n in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _format_counts(counts: dict[str, int], baseline: dict[str, int]) -> str:
    """The audit tally as `category=total(+N)`, where N is the change THIS SESSION.

    The `{...}` block is a CUMULATIVE ledger over the database's whole life, and
    reading it as a rate produced three false "the system is failing" diagnoses in
    a single day on 2026-08-05 — one of which stopped a running session mid-market.
    The numbers were all correct; the instrument was ambiguous. `account.refresh_failed:
    285` frozen for an hour and `285` climbing every tick render identically.

    So a category that has not moved since the session started renders bare, and one
    that has carries `(+N)`. Movement becomes a visual property of the line rather
    than something the reader has to reconstruct by diffing two timestamps by eye.

    Sorted, because the underlying dict's order is a mix of the seed query's order
    and first-write order; a line that reorders itself between prints is its own
    small readability tax.
    """
    parts = []
    for category in sorted(counts):
        total = counts[category]
        delta = total - baseline.get(category, 0)
        parts.append(f"{category}={total}(+{delta})" if delta else f"{category}={total}")
    return "{" + ", ".join(parts) + "}"


async def _interruptible_sleep(seconds: float, stop: asyncio.Event) -> None:
    """Sleep up to `seconds`, returning early the moment `stop` is set."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _login_with_retry(
    tt: TastytradeSession,
    md_tt: TastytradeSession,
    *,
    config: Config,
    audit: AuditLog,
    alert: AlertFn,
    stop: asyncio.Event,
) -> bool:
    """Open both sessions, retrying transient failures across the open.

    Returns True once both are open, False if the process was asked to stop or the
    attempts were exhausted. Every failed attempt closes whatever opened before
    sleeping — a partially-open pair would leak the httpx client and, worse, count
    a stale connection as "logged in".

    BAN RISK is the binding constraint (CLAUDE.md): each failed OAuth refresh
    accrues toward tastytrade's undocumented IP-block threshold, so the cadence is
    the retired systemd tuning's — a handful of attempts, a flat multi-minute gap
    — not a tight loop. The window is bounded so a cert that never recovers costs
    at most one missed day, not an 8-hour block that also costs tomorrow.
    """
    attempts = config.market.login_attempts
    interval = config.market.login_retry_interval_s
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        if stop.is_set():
            return False
        try:
            await tt.open()
            await md_tt.open()
            if attempt > 1:
                print(f"login succeeded on attempt {attempt}/{attempts}")
                audit.telemetry("session.login_recovered", {"attempt": attempt})
            return True
        except Exception as exc:  # noqa: BLE001 — reported below, then retried
            last = exc
            await _safe_aclose(tt)
            await _safe_aclose(md_tt)
            detail = f"{type(exc).__name__}: {exc}"
            print(f"login attempt {attempt}/{attempts} failed: {detail}")
            audit.telemetry(
                "session.login_attempt_failed",
                {"attempt": attempt, "attempts": attempts, "error": detail},
            )
            if attempt < attempts:
                await _interruptible_sleep(interval, stop)
    # Exhausted. Tier 1 — why a session did not run is exactly the kind of thing a
    # crash must not lose.
    reason = f"{type(last).__name__}: {last}" if last is not None else "unknown"
    alert("login_gave_up", {"attempts": attempts, "error": reason})
    await audit.record(
        "session.login_gave_up", {"attempts": attempts, "error": reason}
    )
    print(f"login gave up after {attempts} attempts; no session today ({reason})")
    return False


async def _safe_aclose(session: TastytradeSession) -> None:
    with contextlib.suppress(Exception):
        await session.aclose()


def session_settled(config: Config, today: date, now_et: datetime) -> bool:
    """Has `today`'s session closed and settled, as of `now_et`, in exchange time?

    The ONE place the same-day expiry carve-out's input is built (2026-08-25).
    All three reconcile call sites go through it — both of this module's, and
    `app.Application._reconcile` — so they ask the same calendar the same
    question, which is the property `reconcile_and_record` exists to protect for
    everything else it does.

    `MarketCalendar` and never a literal 16:15, because the calendar is the thing
    that knows which days close early. The CLOCK is the caller's, not read here:
    the app has its own injectable ET clock and this module uses `clock.et_now`,
    and a caller that takes ONE reading for both `today` and this cannot end up
    judging one day's expiry against another day's close.
    """
    return MarketCalendar.from_config(config.market).is_settled(today, now_et)


#: The Tier 1 category written at session start on a blackout day, and only then.
EVENT_BLACKOUT_DAY = "session.event_blackout"


async def _announce_blackout_day(
    audit: AuditLog, *, trading_date: date, blackout_dates: frozenset[date]
) -> bool:
    """Say at session start that today takes no entries, if it is a blackout day.

    Returns whether it is. **Announces; decides nothing** — the rail is the
    strategy's own check, which is untouched, and nothing reads this record.

    Why it exists (2026-09-18): `CreditSpreadStrategy.evaluate` checks the entry
    window BEFORE the blackout, so from session start until 10:00 ET every
    `signal.none` says `outside_entry_window`. On 2026-09-11 (CPI) and 2026-09-16
    (FOMC, live day 1) nothing in the journal or the audit trail said the day would
    take no entries until the window opened. That check order is right and stays;
    this fills the silence ahead of it.

    It is called with the SAME frozenset the strategy is built from, so it is the
    session's verdict, not `/status`'s startup read of the file
    (`status.BlackoutRunway.covers`) — the two are independent reads, and a
    disagreement between them means the file changed under a running process.

    Tier 1, once, only on a blackout day: a durable fact about the day whose
    presence is the answer. A failed write halts like any other Tier 1 write.
    """
    if trading_date not in blackout_dates:
        return False
    print(
        f"event blackout: {trading_date.isoformat()} IS A BLACKOUT DAY — no entries"
        " will be taken; the watchdog, exits and the pre-close flatten still run"
    )
    await audit.record(EVENT_BLACKOUT_DAY, {"trading_date": trading_date.isoformat()})
    return True


async def _reconcile_or_abort(
    broker: BrokerAccount,
    config: Config,
    audit: AuditLog,
    alert: AlertFn,
    halt: HaltFn | None = None,
    stack: ExecutionStack | None = None,
    trading_date: date | None = None,
    stop: asyncio.Event | None = None,
    halted_grade: HaltedGradeFn | None = None,
) -> bool:
    """Reconcile broker vs. expected positions AND orders at session start.

    Re-runs the startup rail per trading day (process start and trading-day start
    are no longer the same event). A read-only connection to the positions table
    keeps this independent of the supervisor's writer connection (WAL permits
    concurrent readers); no `apply_migrations` here — the supervisor already
    migrated under the writer lock.

    `stack` (sandbox/live) extends the check from positions to ORDERS — an unexplained
    WORKING order can become a position at any moment, so it is the same class of
    "Argus has lost track of reality" and rides in the same `ReconciliationResult`
    (chunk 2). It also enables the sandbox 24h-reset handling, which needs the order
    ledger to know what to clear. Shadow passes neither and gets the position-only
    check it always had.

    The response to a mismatch is ENVIRONMENT-GATED (`mismatch_halts_process`),
    identically to the process-start reconcile (`app.Application._reconcile`):

      - shadow: abort THIS day only — record the verdict, alert, raise
        `SessionAborted` — and let the process keep running to try again tomorrow.
        Shadow submits nothing, so there is no order state to protect; a hard
        process halt here would let a stale sandbox position (wiped nightly) brick
        every following day.
      - sandbox/live: a broker/expected mismatch is exactly the crash-recovery
        scenario continuous reconciliation exists to catch (spec §6). Refuse to
        resume PROCESS-WIDE via `halt` — the SAME engage-kill-switch +
        request-stop pairing the audit-write-failure fix uses — attributing the
        trip `source="reconciliation_mismatch"` with the discrepancy detail so
        /status (and a direct audit query) tell the whole story. Then still raise
        `SessionAborted` to unwind this session cleanly; the process is already
        winding down.

    **Returns True when the caller should keep going MONITOR-ONLY** (2026-08-29).
    A mismatch that `reconcile.halt_grade` classifies as carrying no position
    implication — no position discrepancy of any kind, every order discrepancy one
    of Argus's own working OPENING orders — still halts the process permanently and
    still engages the kill switch, but does not ask it to stop. The session then
    continues with its entry hook unwired, so the WATCHDOG runs: held positions keep
    their virtual stops, their marks, and the time-based flatten. Every other
    mismatch raises `SessionAborted` exactly as before.

    **The return value comes from the LATCHED grade, never from this call's own
    verdict.** `halted_grade()` is asked after `halt()` returns, so an earlier `ALL`
    halt — which `HaltGrade.tighten` refuses to relax — wins over a narrow verdict
    computed here. Without a `halted_grade` seam the answer is always "abort", which
    is the fail-closed direction and what every existing caller (tests, shadow, the
    tools) keeps getting unchanged.
    → docs/decisions/2026-08-29-halt-scope-split.md
    """
    store_conn = connect(config.audit.db_path, synchronous=config.audit.synchronous)
    today = trading_date or et_today()

    def _inputs() -> tuple[date, bool]:
        # `today` is the SESSION's date, handed down by the scheduler — never
        # re-derived from the clock, even across a retry that spans the rollover.
        # Settlement IS re-read: a 30-minute outer retry can cross the close, and
        # the expiry carve-out's answer changes when it does.
        return today, session_settled(config, today, et_now())

    try:
        reconciler = Reconciler(
            broker,
            SqlitePositionStore(store_conn),
            execution=stack.execution if stack is not None else None,
            order_store=stack.store if stack is not None else None,
        )
        # ONE shared sequence with the process-start reconcile: sandbox-reset handling,
        # the compare, the `trading_date`-stamped Tier 1 record, and the mismatch
        # alert — now wrapped in the ONE shared outer retry, so an unreachable
        # broker at 09:40 does not cost the 10:00-15:00 entry window over a blip
        # that clears in seconds. This call site's own job is only the REFUSAL below.
        result = await reconcile_with_retry(
            reconciler,
            config=config,
            audit=audit,
            alert=alert,
            inputs=_inputs,
            order_store=stack.store if stack is not None else None,
            stop=stop,
        )
    except BrokerUnreachable as exc:
        # NOT a halt, and deliberately not — unlike a mismatch. A mismatch means
        # Argus's model of reality is KNOWN-WRONG, which is permanent: tomorrow
        # inherits the same wrong state and only a human can adjudicate it. This
        # means UNKNOWN, and tomorrow's session asks again from scratch. Halting on
        # unknown would convert a one-day outage into stay-down-until-a-human, for
        # a condition that is transient by nature.
        #
        # Neither response starts a watchdog (`trading` is None either way), so an
        # open position is unmanaged under both and the only difference that matters
        # is whether the operator is TOLD — which `reconcile_with_retry` has already
        # done, Tier 1 plus an alert. `session.aborted` alone fires no alert at all.
        raise SessionAborted(
            f"could not reach the broker to reconcile ({exc}); no session today"
        ) from exc
    finally:
        store_conn.close()
    if result.ok:
        return False

    if mismatch_halts_process(config.environment):
        # Fail loud if the halt seam is missing: a sandbox/live session with no way
        # to halt on a mismatch is a broken safety rail, not something to run past.
        assert halt is not None, "sandbox/live session body requires a halt seam"
        grade = halt_grade(result)
        await halt(
            f"broker reconciliation mismatch ({result.total_discrepancies} discrepancy(ies))",
            source="reconciliation_mismatch",
            grade=grade,
            detail={**result.as_payload(), "halt_grade": str(grade)},
        )
        # Re-read the AUTHORITATIVE state rather than trusting `grade`. The two
        # agree in every ordinary case; they diverge when a wider halt was already
        # latched, and there the latched one governs. Deriving the decision from the
        # local computation would let a narrow verdict resume a session on a process
        # that had already refused everything.
        if halted_grade is not None and halted_grade() is HaltGrade.ENTRIES:
            logger.warning(
                "reconciliation mismatch is order-level with no position "
                "implication (%d discrepancy(ies)): entries are HALTED for the "
                "life of this process, and today's session continues MONITOR-ONLY "
                "so held positions keep their watchdog",
                result.total_discrepancies,
            )
            await audit.record(
                "session.monitor_only",
                {
                    "trading_date": today.isoformat(),
                    "reason": "reconciliation_mismatch",
                    **result.as_payload(),
                },
            )
            alert(
                "session_monitor_only",
                {
                    # No scope field, deliberately. B1's throttle keys on
                    # `(kind, alert_scope(detail))` and `SCOPE_FIELDS` is
                    # `candidate_id`/`external_identifier` — this alert is about the
                    # PROCESS, not a position, so it correctly takes the kind's one
                    # unscoped window. It fires once per session start, which is far
                    # inside the coalescing window anyway.
                    "trading_date": today.isoformat(),
                    "reason": "reconciliation_mismatch",
                    "discrepancies": result.total_discrepancies,
                },
            )
            return True
    raise SessionAborted(
        f"broker reconciliation failed with {result.total_discrepancies} "
        "discrepancy(ies); refusing to run today's session"
    )


async def startup_reconcile(
    *,
    config: Config,
    audit: AuditLog,
    alert: AlertFn,
    account_number: str | None = None,
    stop: asyncio.Event | None = None,
) -> "ReconciliationResult":
    """Reconcile at PROCESS START for sandbox/live, opening its own broker session.

    Spec §6 is "on every restart, reconcile broker state before resuming", and
    `Application._reconcile` has always REFUSED to start sandbox/live without a
    reconciler. Until chunk 5 nothing could build one, because the always-on process
    boots credential-free and logs in only inside a session — so the only environment
    that could actually start was shadow, which records `reconcile.skipped`.

    This closes that. The session it opens is SHORT-LIVED by design: it exists for
    this one comparison and is closed before `startup()` returns, so the
    process-lifetime layer still holds no broker credentials and no token that has to
    be kept refreshed across a weekend. The authoritative per-trading-day reconcile is
    still the one inside the session body.

    It is the FULL check — positions and orders — and it is reset-aware, through the
    same `reconcile_and_record` both call sites share.

    **Two exception types, and the split is what the caller refuses on** (2026-08-26):

      - `BrokerUnreachable` — could not ASK. The reconcile fetches are retried on the
        `[reconcile]` budget; the LOGIN and the account fetch are not retried on that
        cadence (see below) but do raise this type, so all three legs reach the same
        park. Until 2026-08-26 the fetch raised a raw `TastytradeError` instead and
        the docstring's promise here was simply false: on that day a cert 502
        propagated out of `startup()` and exited the process five times in 33 seconds.
      - `SessionAborted` — asked, and the CONFIGURATION is wrong (no matching
        account). Permanent, so it is never retried; the caller parks on it at once
        rather than spending the budget re-proving it.

    **The login is deliberately outside the outer retry.** `tt.open()` forces an OAuth
    refresh, and each failed refresh accrues toward tastytrade's undocumented IP-block
    threshold — which is why `_login_with_retry` uses 6 attempts a flat 10 MINUTES
    apart rather than a tight loop. Retrying a login on the reconcile's 60s cadence
    would be five times more aggressive than that deliberate number. So the session is
    opened ONCE, and a login failure becomes `BrokerUnreachable` immediately; the
    self-heal covers the fetch legs, which is where 2026-08-26 actually failed.
    """
    trading_creds = trading_credentials(config)
    tt = TastytradeSession(config, trading_creds, role=SessionRole.TRADING)
    try:
        await tt.open()
    except Exception as exc:  # noqa: BLE001 — re-typed, not swallowed
        # No retry here, by design (see the docstring's ban-risk note). The type
        # change is what matters: it routes the login leg into the same park as the
        # fetch legs instead of tearing the process down as an unknown failure.
        raise BrokerUnreachable("login", 1, exc) from exc
    store_conn = None
    try:
        try:
            accounts = await _fetch_accounts(
                tt.trading_session, timeout_s=config.broker.http_timeout_s
            )
        except SessionAborted as exc:
            # `_fetch_accounts` has its own 3-attempt retry and raises SessionAborted
            # on exhaustion — which means "could not ask", not "the config is wrong".
            # Re-typed so the two meanings SessionAborted used to carry here stop
            # sharing one exception: an outer loop must retry this one and must not
            # retry the "no matching account" below it.
            raise BrokerUnreachable("accounts", 1, exc) from exc
        if account_number:
            accounts = [a for a in accounts if a.account_number == account_number]
        if not accounts:
            # PERMANENT, and stays `SessionAborted` for exactly that reason: a wrong
            # account number in the config is not going to become right in 30 minutes.
            raise SessionAborted(
                "no matching account at process start (see `python -m argus.tools.account`)"
            )
        account = accounts[0]
        stack = build_execution_stack(
            session=tt.trading_session, account=account, audit=audit, config=config
        )
        store_conn = connect(config.audit.db_path, synchronous=config.audit.synchronous)
        reconciler = Reconciler(
            BrokerAccount(tt.trading_session, account),
            SqlitePositionStore(store_conn),
            execution=stack.execution,
            order_store=stack.store,
        )

        def _inputs() -> tuple[date, bool]:
            # RE-READ per outer attempt, both from ONE clock reading. Over a
            # 30-minute budget the ET date can roll over and a session can settle —
            # and an evening restart is precisely when a process-start reconcile
            # runs (2026-08-25), so a date captured before the loop is a live hazard.
            now = et_now()
            today = now.date()
            return today, session_settled(config, today, now)

        return await reconcile_with_retry(
            reconciler,
            config=config,
            audit=audit,
            alert=alert,
            inputs=_inputs,
            order_store=stack.store,
            stop=stop,
        )
    finally:
        if store_conn is not None:
            store_conn.close()
        await _safe_aclose(tt)


# -- the trading side (sandbox/live only) -------------------------------------


@dataclass
class TradingSide:
    """Everything that only exists once orders are real. Built together or not at all.

    The four members are one unit because of how they depend on each other, and the
    dependency runs in one direction:

        FillConsumer  writes `positions`  (chunk 2)
             |
             v
        PositionWatchdog  reads it back via `store.open_spreads()`  (chunk 4)

    So a watchdog wired WITHOUT the fill consumer watches a table nobody writes, finds
    nothing to watch, and reports nothing wrong — the one failure it cannot detect
    about itself. And order reconciliation wired without the fill consumer hard-halts
    the morning after the first fill, because that fill was never recorded as expected.
    Hence one builder, one switch, no partial states.
    """

    stack: ExecutionStack
    entry: EntryExecutor
    watchdog: PositionWatchdog
    fills: FillConsumer
    #: P5's residual-order registry (2026-08-30). Already shared by the entry walk,
    #: the exit walk and the watchdog; carried here so `/status` can read the orders a
    #: finished walk left working. Part of the same both-or-neither unit: it is
    #: constructed in this builder and belongs to exactly one trading day, like
    #: everything else on this dataclass, and it is deliberately never rehydrated.
    residuals: ResidualOrderRecheck


def _build_trading_side(
    *,
    config: Config,
    audit: AuditLog,
    stack: ExecutionStack,
    quotes: LegQuotes,
    stop: asyncio.Event,
    alert: AlertFn,
    halt: HaltFn,
    killswitch: KillSwitch,
    trading_date: date,
    fills: FillConsumer,
    broker_position_count: Callable[[], int | None],
    confirm_broker_position_count: Callable[[], Awaitable[int | None]] | None = None,
    implied_vol: ImpliedVolSource | None = None,
) -> TradingSide:
    """Assemble the entry path, the watchdog, and the fill consumer.

    `halt` is REQUIRED here (not `HaltFn | None`) because both the entry executor and
    the watchdog refuse to be built without it: the hard side of their exception
    policies IS that seam, and an executor with nowhere to halt is a safety rail that
    silently does nothing.

    `killswitch` goes to the ENTRY executor and to NOTHING else, and that asymmetry
    is the whole of the kill switch's blast radius (spec §6). A trip must stop the
    system taking on new risk — so the entry path refuses new candidates and abandons
    a walk already in flight — while exits run on: `ExitExecutor` is built here with
    no kill-switch reference at all, so a halt can never cancel the order closing a
    position or stop the watchdog from opening one. Passing it to `ExitExecutor`
    below would silently invert that, which is why it is stated here rather than left
    to be inferred from an omitted argument.
    """
    # ONE recheck for the whole trading side, and it has to be one: the entry walk
    # and the exit walk both register into it and the watchdog steps it, so a second
    # instance would be a registry nobody polls. It is built with
    # `stack.execution.cancel` — the SAME audited, throttled cancel every other
    # cancel in the process goes through, so `order.cancel_attempt` /
    # `order.cancel_failed` / `order.cancel_requested` are written by the existing
    # layer at the existing tier and this adds no cancel record types of its own.
    #
    # Deliberately NOT given the kill switch: cancelling an abandoned OPENING order
    # while halted is precisely what the operator asked for, and a recheck disabled
    # by a halt would be disabled exactly when it matters most. It never cancels a
    # CLOSING order, so it cannot strand a position a halt left open either.
    # → docs/decisions/2026-08-29-mid-session-cancel-recheck.md
    residuals = ResidualOrderRecheck(
        audit=audit,
        config=config,
        cancel=stack.execution.cancel,
        alert=alert,
    )
    watchdog_exits = ExitExecutor(
        execution=stack.execution,
        audit=audit,
        config=config,
        guard=stack.guard,
        quotes=quotes,
        alert=alert,
        stop=stop,
        # An exit walk's residual is a CLOSING order: paged, never cancelled.
        residuals=residuals,
        # The session's OWN trading date, not `et_today()`. Both agree in practice, but
        # the session already knows the date it was started for, and an exit order's
        # identity should not be able to disagree with the entry order's just because a
        # session outlives an ET midnight it should never see.
        trading_date=lambda: trading_date,
    )
    watchdog = PositionWatchdog(
        spreads=stack.store,
        quotes=quotes,
        executor=watchdog_exits,
        audit=audit,
        config=config,
        halt=halt,
        alert=alert,
        stop=stop,
        # The session's OWN trading date again, and for the same reason it is
        # passed to `ExitExecutor` above — here it decides which ledger legs have
        # expired and are therefore closed (`store._leg_is_expired`), so the two
        # must never be able to disagree about which day it is.
        #
        # Since 2026-08-28 it ALSO decides which day's close the time-based flatten
        # is measured from (`MarketCalendar.flatten_mark`, half-day aware:
        # `watchdog.flatten_before_close_s` before THIS day's close, so 15:30 ET
        # normally and 12:30 on a half-day). Nothing is passed for the ET clock
        # itself — the watchdog defaults to `clock.et_now`, which is what this
        # module uses everywhere else.
        # → docs/decisions/2026-08-28-time-based-flatten-trigger.md
        trading_date=lambda: trading_date,
        # The book-agreement check. `positions` is written ONLY from AlertStreamer
        # fill events, so a fill during a stream gap leaves a real position with no
        # ledger row and therefore no virtual stop — see
        # `PositionWatchdog._check_broker_agreement`. Both sides are passed here
        # because this is the only place that can see both: the broker-derived count
        # off the account cache, and the ledger's own row count off the store.
        broker_position_count=broker_position_count,
        ledger_state=stack.store.ledger_state,
        # The authoritative tiebreak, consulted only where a page would fire. The
        # count above is a stream-fed CACHE and can over-report a position that
        # closed (2026-08-19); without this, that drift pages on the one condition
        # deliberately built to have no settle allowance.
        confirm_broker_position_count=confirm_broker_position_count,
        # The pre-trade gate's working-order count (Finding 6, 2026-08-28). The
        # watchdog polls this on its refresh cadence and exposes it as
        # `working_open_spread_count`; `RiskSnapshotBuilder` reads that late-bound,
        # exactly as it reads `open_spread_count` for overlapping verticals. Routed
        # through the same audited+throttled stack every other order request uses.
        live_orders=stack.execution.get_live_orders,
        # Stepped from `_refresh_if_due` on the SAME `LiveOrders` answer the count
        # above is derived from — one fetch, two readers, no new task (P5).
        residuals=residuals,
        # The flatten deferral's ATM IV (2026-09-25): `ChainImpliedVol` over the SAME
        # chain source `quotes` resolves contracts from — the shared market-data
        # feed, never `SignalLoop` state. None refuses every deferral.
        implied_vol=implied_vol,
    )
    entry = EntryExecutor(
        execution=stack.execution,
        audit=audit,
        config=config,
        guard=stack.guard,
        killswitch=killswitch,
        halt=halt,
        stop=stop,
        alert=alert,
        trading_date=lambda: trading_date,
        # Arms the entry walk's underlying-drift breaker, and it is deliberately the
        # SAME `LegQuotes` the watchdog reads its underlying trigger from — one quote
        # path, one staleness rule, one place the OCC/streamer-symbol distinction is
        # handled. A second resolver here is the bug chunk 4 already paid for.
        spot=quotes.spot,
        # A filled entry makes the watchdog look NOW rather than up to
        # `[watchdog] refresh_interval_s` later. Belt and braces with the fill
        # consumer's own call below: the walk's report and the stream's event are two
        # independent observations of the same fill and either may arrive first.
        on_filled=watchdog.request_refresh,
        # An entry walk's residual is an OPENING order — the one kind that gets the
        # automatic re-cancel, because a cancel there is monotone toward less
        # exposure and is the second half of a guarantee the kill switch already
        # makes. Same object the watchdog steps.
        residuals=residuals,
    )
    return TradingSide(
        stack=stack, entry=entry, watchdog=watchdog, fills=fills,
        residuals=residuals,
    )


# -- the /status live-read seams ----------------------------------------------
#
# Three converters from a trading-side type to the `status.py` type `/status`
# renders. Each is a FIELD-FOR-FIELD copy and nothing else, and each is called by a
# one-line closure inside `run_session` (below) that binds the live object — the
# closure is the publication, these are the translation.
#
# They are module-level rather than inline in `run_session` because of what went
# wrong when they were inline. `spent_exits` was written on 2026-08-21 with the five
# fields `SpentExitAttempt` had then; three more (`cost_to_close`, `credit_stop`,
# `still_triggered`) were added to it on 2026-08-28 — by the pass whose entire point
# was that a held, triggered spread had gone 76 minutes with no surface able to say
# what it was worth — and the closure was never updated. `UnclosablePosition`
# declares all three with `None` defaults, so nothing raised, nothing warned, and
# `/status` rendered `mark: unpriced` for every spent exit for five days. Every test
# that covered the row hand-built an `UnclosablePosition` with the fields populated,
# which is exactly the construction that misses this.
#
# So: an UNDER-COPY BETWEEN TWO DATACLASSES WITH `None` DEFAULTS IS SILENT AT EVERY
# LAYER. Being callable from a test is what lets the copy be asserted against a real
# watchdog rather than against a hand-built row, and `tests/test_session_status_seams.py`
# additionally pins each one structurally: the target construction must name every
# field of its source type.


def _to_unclosable(
    spent: Iterable[SpentExitAttempt],
) -> tuple[UnclosablePosition, ...]:
    """`PositionWatchdog.spent_exit_attempts()` -> `/status`'s exit-attempts row.

    All TEN fields. The three mark fields are the half an operator actually acts
    on: this row says a position cannot be closed automatically, and what it is
    WORTH is what decides whether to leg out of it by hand. `None` is passed
    through as `None` — "that tick could not price it", rendered as `unpriced` —
    and never defaulted to a number. `stub_close` (2026-09-24) passes through
    the same way: None is "not a stub", a real state, not a missing value.
    """
    return tuple(
        UnclosablePosition(
            external_identifier=p.external_identifier,
            candidate_id=p.candidate_id,
            contracts=p.contracts,
            outcome=p.outcome,
            spent_for_s=p.spent_for_s,
            cost_to_close=p.cost_to_close,
            credit_stop=p.credit_stop,
            still_triggered=p.still_triggered,
            crash_error=p.crash_error,
            stub_close=p.stub_close,
            working_close_order_id=p.working_close_order_id,
            working_close_limit=p.working_close_limit,
            working_close_for_s=p.working_close_for_s,
            credit_trigger_enabled=p.credit_trigger_enabled,
        )
        for p in spent
    )


def _to_evaluability(
    impaired: Iterable[ImpairedPosition],
) -> tuple[PositionEvaluability, ...]:
    """`PositionWatchdog.impaired_positions()` -> `/status`'s exit-triggers row.

    All five fields, which is every field `ImpairedPosition` has.
    """
    return tuple(
        PositionEvaluability(
            external_identifier=p.external_identifier,
            candidate_id=p.candidate_id,
            unknown_reasons=p.unknown_reasons,
            blind=p.blind,
            impaired_for_s=p.impaired_for_s,
        )
        for p in impaired
    )


def _to_unmanaged_positions(
    unwatchable: Iterable[UnwatchablePosition],
) -> tuple[UnmanagedPosition, ...]:
    """`PositionWatchdog.unwatchable_positions()` -> `/status`'s unwatched row.

    All nine fields. The watchdog has already flattened `malformed`'s per-symbol dicts
    and resolved the episode age and the settle verdict, so this — like every converter
    in this block — copies and interprets nothing.
    """
    return tuple(
        UnmanagedPosition(
            kind=p.kind,
            problem=p.problem,
            symbols=p.symbols,
            detail=p.detail,
            candidate_id=p.candidate_id,
            external_identifier=p.external_identifier,
            broker_order_id=p.broker_order_id,
            unwatchable_for_s=p.unwatchable_for_s,
            settling=p.settling,
        )
        for p in unwatchable
    )


def _to_residual_rows(
    residuals: Iterable[OutstandingResidual],
) -> tuple[ResidualOrderRow, ...]:
    """`ResidualOrderRecheck.residual_rows()` -> `/status`'s residual-orders row.

    All eleven fields, including `cancel_enabled` — the registry stamps its own mode on
    every row, so the page cannot report a mode for a session that has no recheck.
    `working_for_s` likewise arrives resolved: `registered_at` is on the registry's
    monotonic clock and differencing it here against any other would produce a number
    that looks right and is not.
    """
    return tuple(
        ResidualOrderRow(
            broker_order_id=r.broker_order_id,
            external_identifier=r.external_identifier,
            candidate_id=r.candidate_id,
            minted_direction=r.minted_direction,
            walk_outcome=r.walk_outcome,
            cancel_requested=r.cancel_requested,
            attempts=r.attempts,
            working_for_s=r.working_for_s,
            last_status=r.last_status,
            last_sighting=r.last_sighting,
            cancel_enabled=r.cancel_enabled,
            limit_price=r.limit_price,
            working_expected=r.working_expected,
        )
        for r in residuals
    )


def _to_entries_ended(ended: EntriesEnded | None) -> EntriesEndedToday | None:
    """`EntryExecutor.entries_ended` -> `/status`'s entries-stood-down row.

    Two-state rather than a sequence, and the only one of the three whose source and
    target are different SHAPES: None means entries are still live, and the row does
    not render at all.
    """
    if ended is None:
        return None
    return EntriesEndedToday(
        kind=ended.kind,
        candidate_id=ended.candidate_id,
        error_type=ended.error_type,
        since=ended.since,
    )


def _to_watchdog_health(health: watchdog_types.WatchdogHealth) -> WatchdogHealth:
    """`PositionWatchdog.watchdog_health()` -> `/status`'s Watchdog row.

    All ten fields. The source resolves every duration against the watchdog's own
    monotonic clock, so this — like every converter in this block — copies and
    interprets nothing.
    """
    return WatchdogHealth(
        degraded=health.degraded,
        watching=health.watching,
        failures=health.failures,
        where=health.where,
        error_type=health.error_type,
        error=health.error,
        degraded_for_s=health.degraded_for_s,
        last_failure_for_s=health.last_failure_for_s,
        last_clean_pass_s=health.last_clean_pass_s,
        affected=health.affected,
    )


def _to_retired_profit_target(
    row: watchdog_types.RetiredProfitTarget,
) -> RetiredProfitTargetRow:
    """One `ProfitTargetState.retired` entry -> its `/status` row. (2026-09-23)

    All six fields; the duration is already resolved against the watchdog's own
    monotonic clock, so this copies and interprets nothing.
    """
    return RetiredProfitTargetRow(
        external_identifier=row.external_identifier,
        candidate_id=row.candidate_id,
        outcome=row.outcome,
        reason=row.reason,
        order_may_be_working=row.order_may_be_working,
        retired_for_s=row.retired_for_s,
    )


def _to_profit_target(state: watchdog_types.ProfitTargetState) -> ProfitTargetStatus:
    """`PositionWatchdog.profit_target_state()` -> `/status`'s Profit target row.

    All three fields, the nested rows through their own swept converter rather than
    copied inline — an inline copy is a second, unswept seam, which is the shape the
    2026-09-02 under-copy came from.
    """
    return ProfitTargetStatus(
        enabled=state.enabled,
        taken=state.taken,
        retired=tuple(_to_retired_profit_target(r) for r in state.retired),
    )


def _to_flatten_deferral_row(
    row: watchdog_types.FlattenDeferralRow,
) -> FlattenDeferralRowStatus:
    """One `FlattenDeferralState.rows` entry -> its `/status` row. (2026-09-25)

    All ten fields; the duration is already resolved against the watchdog's own
    monotonic clock, so this copies and interprets nothing.
    """
    return FlattenDeferralRowStatus(
        external_identifier=row.external_identifier,
        candidate_id=row.candidate_id,
        deferring=row.deferring,
        reason=row.reason,
        z=row.z,
        p_touch=row.p_touch,
        lhs=row.lhs,
        premium=row.premium,
        distance_points=row.distance_points,
        deferred_for_s=row.deferred_for_s,
    )


def _to_flatten_deferral(
    state: watchdog_types.FlattenDeferralState,
) -> FlattenDeferralStatus:
    """`PositionWatchdog.flatten_deferral_state()` -> `/status`'s deferral row.

    All five fields, the nested rows through their own swept converter.
    """
    return FlattenDeferralStatus(
        mode=state.mode,
        k=state.k,
        tail_z99=state.tail_z99,
        tail_alpha=state.tail_alpha,
        rows=tuple(_to_flatten_deferral_row(r) for r in state.rows),
    )


# -- the session body ---------------------------------------------------------


async def run_session(
    *,
    config: Config,
    audit: AuditLog,
    state: SessionState,
    stop: asyncio.Event,
    trading_date: date,
    alert: AlertFn,
    account_number: str | None = None,
    status: SessionStatus | None = None,
    halt: HaltFn | None = None,
    halted_grade: HaltedGradeFn | None = None,
) -> None:
    """Run one trading day's pipeline. Shadow submits nothing; sandbox/live trades.

    Returns when `stop` is set (the close, process shutdown, or the entry path ending
    the day) or when the day is aborted (login gave up, reconciliation refused,
    account unreachable). Raising is reserved for genuine faults the caller should
    see; a clean "no session today" returns normally.

    `status` (when provided by the supervisor) is the process-lifetime publisher
    the monitoring surface reads: this body walks it through logging_in ->
    reconciling -> running and lands on a terminal ended/aborted, and hands over
    the live risk snapshot so `/status` can report data-freshness ages for a
    session it does not own.

    `halt` (when provided by the supervisor) is the engage-kill-switch +
    request-stop pairing every process-wide refusal uses: a sandbox/live reconcile
    mismatch (`_reconcile_or_abort`), an unhandled watchdog exception, and an entry
    failure past the submission boundary (`execution/entry.py`). In shadow none of
    them can fire, so it is never invoked — but sandbox/live REQUIRE it, and this
    body refuses to build its trading side without one rather than running a session
    whose safety rails have nowhere to escalate.

    `halted_grade` (also the supervisor's) is what makes a MONITOR-ONLY day possible
    (2026-08-29): when it reports any halt in force, the trading side is still built
    — fill consumer, watchdog, exit executor — but `submit` stays `None`, so the
    signal loop routes nothing while held positions keep their virtual stops and
    their 15:30 flatten. Absent (tests, tools, shadow), it reads as "not halted",
    which is exactly what those callers were doing before it existed.
    """
    if status is not None:
        status.session_starting(trading_date)
    try:
        trading_creds = trading_credentials(config)
        md_creds = market_data_credentials(config)
    except MissingCredentialsError as exc:
        alert("login_missing_credentials", {"error": str(exc)})
        await audit.record(
            "session.login_gave_up",
            {"reason": "missing_credentials", "error": str(exc)},
        )
        print(f"cannot start session: {exc}")
        if status is not None:
            status.session_finished(ended=False)
        return

    tt = TastytradeSession(config, trading_creds, role=SessionRole.TRADING)
    md_tt = TastytradeSession(config, md_creds, role=SessionRole.MARKET_DATA)
    # The credential SLOT is on this line beside the endpoint, because the two are
    # keyed on the same authority and a mismatch between them was invisible until
    # 2026-09-09. Variable names only — never a value.
    print(
        f"trading session: env={config.environment} (is_test={tt.is_test}) "
        f"creds={trading_creds.slot}\n"
        f"market data:     source={config.marketdata.source} (is_test={md_tt.is_test}, "
        f"READ-ONLY) creds={md_creds.slot}"
    )

    md: DXLinkMarketData | None = None
    acct_stream: AlertStreamerAccount | None = None
    refresher: ThreadedAccountRefresher | None = None
    loop: SignalLoop | None = None
    trading: TradingSide | None = None
    # Distinguishes a normal end (reached the loop, ended at the close/stop) from
    # an early abort, for the terminal phase set in `finally`.
    reached_running = False
    try:
        if not await _login_with_retry(
            tt, md_tt, config=config, audit=audit, alert=alert, stop=stop
        ):
            return
        if stop.is_set():
            return

        accounts = await _fetch_accounts(
            tt.trading_session, timeout_s=config.broker.http_timeout_s
        )
        if account_number:
            accounts = [a for a in accounts if a.account_number == account_number]
        if not accounts:
            raise SessionAborted("no matching account (see `python -m argus.tools.account`)")
        account = accounts[0]
        broker = BrokerAccount(tt.trading_session, account)
        print(f"account: {account.account_number}")

        # -- the execution stack, BEFORE reconciling ------------------------
        # Built here rather than later because reconciliation needs it: the ORDER
        # half of the check reads `get_live_orders()` through this stack, and the
        # sandbox-reset handling needs the order ledger to know what to clear.
        # Shadow gets None all the way down and keeps the position-only check.
        stack: ExecutionStack | None = None
        if trades_for_real(config):
            # A missing halt seam here is a broken safety rail, not a degraded mode:
            # the entry path and the watchdog both escalate through it. Caught before
            # anything is built so the failure is at wiring time, not at 10:42.
            if halt is None:
                raise SessionAborted(
                    f"environment={config.environment} submits orders but no halt seam "
                    "was provided; refusing to trade with nowhere to escalate"
                )
            # Same posture, same reason, for the kill switch: without it the entry
            # path cannot refuse a candidate after a trip or abandon a walk in
            # flight, so `POST /kill` would reach the gate and stop nothing already
            # in motion. Caught at wiring time rather than discovered by pressing it.
            if state.killswitch is None:
                raise SessionAborted(
                    f"environment={config.environment} submits orders but no kill switch "
                    "was provided; refusing to trade with an unreachable halt control"
                )
            stack = build_execution_stack(
                session=tt.trading_session,
                account=account,
                audit=audit,
                config=config,
                alert=alert,
            )

        # -- reconcile (refuses to run today's session on mismatch) --------
        if status is not None:
            status.set_reconciling()
        monitor_only = await _reconcile_or_abort(
            broker, config, audit, alert, halt, stack=stack, trading_date=trading_date,
            # So the outer unreachable-retry ends at the close (or on `/kill`)
            # rather than running on past the point a session could still be useful.
            stop=stop,
            halted_grade=halted_grade,
        )
        # A process that came up already halted-ENTRIES and then reconciled CLEAN
        # takes the same path: `monitor_only` is about THIS verdict, the seam is
        # about the process, and either one alone is enough to withhold the entry
        # hook. Read once, here, so the decision below cannot drift from the print.
        monitor_only = monitor_only or (
            halted_grade is not None and halted_grade() is not None
        )
        print("reconciliation clean" if not monitor_only else "MONITOR-ONLY: entries halted")
        await audit.record(
            "session.configured",
            {
                "trading_environment": config.environment,
                "trading_is_test": tt.is_test,
                "marketdata_source": config.marketdata.source,
                "marketdata_is_test": md_tt.is_test,
                "account": account.account_number,
                "trading_date": trading_date.isoformat(),
            },
        )

        # -- market data ---------------------------------------------------
        underlying = config.chain.underlying_symbol
        md = DXLinkMarketData(
            md_tt.market_data_session, clock=time.monotonic, telemetry=audit.telemetry
        )
        await md.start()
        await md.subscribe([underlying])
        if not await _wait_for_quote(md, underlying, timeout=30.0):
            raise SessionAborted(f"no {underlying} quote within 30s")
        spot = md.latest(underlying).mid
        print(f"{underlying} spot: {spot}")

        # -- fill consumption ----------------------------------------------
        # Built BEFORE the account stream, and deliberately not inside
        # `_build_trading_side`: the stream's `on_order` hook starts delivering the
        # moment `start()` returns, while the watchdog cannot exist until the chain is
        # loaded. Booking a fill is the part that must not be missed in that window
        # (an unrecorded fill is a position reconciliation will refuse tomorrow), so it
        # is wired first and the watchdog's refresh HINT is the only late-bound part —
        # harmless, because the watchdog reads the whole ledger on its first cycle.
        fills: FillConsumer | None = (
            None if stack is None
            else FillConsumer(
                store=stack.store, audit=audit, alert=alert, ledger=state.day_ledger
            )
        )

        # -- account stream, seeded from REST ------------------------------
        async def on_order(order):
            if fills is None:
                # SHADOW: submits nothing, so ANY order event here is unexpected and
                # worth a Tier 1 record. Unchanged from before chunk 5.
                await audit.record(
                    "order.event",
                    {
                        "id": getattr(order, "id", None),
                        "status": str(getattr(order, "status", "")),
                    },
                )
                return
            # Sandbox/live: this is the hook chunk 2 was written against and never
            # wired. `consume` never raises out (an exception here would tear down the
            # stream's `_pump` and take positions and balances with it), so there is
            # nothing to guard.
            applied = await fills.consume(order)
            if trading is not None:
                # `note_fills` BEFORE `request_refresh`: the refresh is what runs the
                # book-agreement check, and a closing fill is the evidence that check
                # needs to tell "the broker's book is trailing our own exit" from "a
                # position is held with nothing watching it" (2026-08-20). Recording
                # it after would race the very comparison it exists to inform.
                trading.watchdog.note_fills(applied)
                trading.watchdog.request_refresh()

        # The liveness refresh runs OFF the event loop (2026-08-26): its two GETs
        # pay a full TCP+TLS handshake whenever the pool is cold — the permanent
        # state during a failure storm — and that handshake's CPU cost lands at
        # the same order as the 50ms heartbeat breach threshold. Deliberately NOT
        # `broker`, which has no bound of its own and so must not satisfy a port
        # whose contract is "bound yourself".
        # → docs/designs/2026-08-25-account-refresh-off-event-loop.md
        refresher = ThreadedAccountRefresher(
            tt.trading_session,
            account.account_number,
            budget_s=config.staleness.account_refresh_timeout_s,
            http_timeout_s=config.broker.http_timeout_s,
            refresh_interval_s=config.staleness.account_refresh_s,
        )
        acct_stream = AlertStreamerAccount(
            tt.trading_session,
            account,
            clock=time.monotonic,
            on_order=on_order,
            refresher=refresher,
            refresh_interval_s=config.staleness.account_refresh_s,
            position_evict_after_s=config.staleness.account_position_evict_after_s,
            stale_after_s=config.staleness.account_stream_max_age_s,
            reauth=functools.partial(tt.refresh, force=True),
            telemetry=audit.telemetry,
        )
        await acct_stream.start()
        acct_stream.seed(await broker.fetch_positions(), await broker.fetch_balances())

        # -- chain ---------------------------------------------------------
        chain_source = DXLinkChainSource(
            md,
            underlying_symbol=underlying,
            chain_symbol=config.chain.chain_symbol,
            strike_window_points=config.chain.strike_window_points,
            clock=time.monotonic,
            # The SAME bound the pre-trade gate applies to the underlying and the
            # watchdog's `LegQuotes` applies per leg on the exit path. Entry legs
            # were the one priced input nothing bounded.
            max_age_s=config.staleness.market_data_max_age_s,
            # NOT the same bound (2026-08-27). Quotes are prices and greeks are a
            # selector; the greeks feed bursts ~137s apart, so holding delta to the
            # price bound left a short strike selectable on 2.8% of ticks.
            greeks_max_age_s=config.staleness.greeks_max_age_s,
        )
        n = await chain_source.load(md_tt.market_data_session, today=trading_date)
        print(
            f"subscribed {n} 0DTE {config.chain.chain_symbol} contracts "
            f"(+/-{config.chain.strike_window_points:g} pts around {underlying} spot)"
        )
        if n == 0:
            print("  (no 0DTE expiry today — the loop will find no chain)")
        else:
            print("waiting for chain data ...")
            deadline_cov = time.monotonic() + 20.0
            while time.monotonic() < deadline_cov and not stop.is_set():
                await _interruptible_sleep(2.0, stop)
                cov = chain_source.coverage()
                print(
                    f"   quotes {cov.with_quote}/{cov.total}   "
                    f"greeks {cov.with_greeks}/{cov.total}   "
                    f"admissible {cov.admissible}/{cov.total} "
                    f"(short-selectable {cov.short_admissible})"
                )
                # Waits for an ADMISSIBLE leg, not merely a present one: with_both
                # can be satisfied by readings already too old to price against,
                # which would end this wait on a chain the strategy sees as empty.
                #
                # Deliberately NOT `short_admissible`, though that is the count a
                # signal actually needs: the greeks feed runs behind the quote feed
                # by more than the bound most of the time (2026-08-24), so waiting
                # on it would burn the full deadline every morning to learn nothing
                # the loop will not report every 15s anyway. The warning below says
                # so instead.
                if cov.admissible:
                    break
            cov = chain_source.coverage()
            if cov.with_greeks == 0:
                if config.signal.short_delta_source == "feed":
                    print("   WARNING: NO GREEKS. Short strikes are selected by the feed")
                    print("   delta (short_delta_source = feed), so no signal can fire.")
                else:
                    print("   WARNING: NO GREEKS. Selection uses mid-implied delta and is")
                    print("   unaffected, but the recorded feed delta will be null.")
                print("   Quotes arriving without greeks points at an options (OPRA)")
                print("   entitlement gap rather than a feed problem.")
            elif cov.with_quote == 0:
                print("   WARNING: no quotes for any option contract.")
            elif cov.admissible == 0:
                # Distinct from both cases above: data IS arriving, it is just too
                # old to price against. That is a lagging feed, not an entitlement
                # gap, and it reads as "no signal" everywhere downstream.
                print(
                    f"   WARNING: {cov.with_quote} contract(s) have quotes but NONE are "
                    f"fresh enough to price against "
                    f"(stale quote {cov.stale_quote}, stale greeks {cov.stale_greeks})."
                )
            elif cov.short_admissible == 0 and config.signal.short_delta_source == "feed":
                # "feed" only: under "mid" the short strike is chosen by the delta
                # implied from its own quote, so a lagging greeks feed no longer
                # stops selection and this warning would name a cause that is not.
                # The ladder is priceable and no SHORT strike can be chosen from it.
                # Its own case: nothing above is wrong, entries still cannot fire,
                # and the cause is the greeks feed alone. This is the state that
                # ran unnamed for a whole session on 2026-08-24.
                print(
                    f"   WARNING: {cov.admissible} contract(s) are priceable but NONE "
                    f"carry greeks fresh enough to select a short strike by "
                    f"(stale greeks {cov.stale_greeks}/{cov.with_greeks}, freshest "
                    f"{cov.min_greeks_age_s:.1f}s vs bound "
                    f"{config.staleness.greeks_max_age_s:g}s). Long legs are "
                    f"unaffected; entries will report no_short_leg_with_fresh_delta."
                )

        # -- risk snapshot + loop -----------------------------------------
        builder = RiskSnapshotBuilder(
            md,
            acct_stream,
            state.day_pnl,
            reference_symbols=(underlying,),
            # LATE-BOUND on purpose: the watchdog that owns the scan is built below,
            # and `trading` is rebound there. Without this the gate counts only
            # broker legs, which cannot see overlapping verticals — chained spreads
            # share a strike that nets to zero at the broker, so any number of them
            # reads as one and the concurrent limit stops bounding anything
            # (measured 2026-08-17). Zero while there is no trading side, which is
            # shadow — where nothing is submitted and the count is moot.
            ledger_spread_count=lambda: (
                trading.watchdog.open_spread_count if trading is not None else 0
            ),
            # OPENING orders still working at the broker, counted toward the
            # concurrency cap (Finding 6, 2026-08-28). Same late-bound shape and the
            # same `trading is None` (shadow) guard: shadow submits nothing, so
            # there is never a working order and the count is moot.
            working_open_spread_count=lambda: (
                trading.watchdog.working_open_spread_count if trading is not None else 0
            ),
            # The same two counts SPLIT BY SIDE, for the side-balance rule
            # (2026-09-26). Same late binding, same shadow guard; the broker's half
            # the builder reads off the account snapshot itself.
            ledger_spreads_by_side=lambda: (
                trading.watchdog.open_spreads_by_side if trading is not None else {}
            ),
            working_open_spreads_by_side=lambda: (
                trading.watchdog.working_open_spreads_by_side if trading is not None else {}
            ),
            # The day's loss budget (2026-09-21). The ledger is the day's own; the
            # two exposure reads are late-bound off the watchdog for the reason the
            # counts above are, and answer "nothing open" in shadow, which holds and
            # submits nothing. The broker estimate counts only in live: cert's day
            # P&L is structurally zero.
            loss_budget=LossBudgetSource(
                daily_max_loss=config.risk.daily_max_loss,
                ledger=state.day_ledger,
                broker_pnl_is_real=config.environment == "live",
                scan_exposure=lambda: (
                    trading.watchdog.open_exposure()
                    if trading is not None else Exposure.none_open()
                ),
                working_exposure=lambda: (
                    trading.watchdog.working_open_exposure()
                    if trading is not None else Exposure.none_open()
                ),
            ),
        )
        if builder.anchor_day_pnl_if_possible():
            anchor = state.day_pnl.anchor
            assert anchor is not None  # anchor_day_pnl_if_possible returned True
            # Tier 1, and the ONLY durable record of the day's reference NLV. A
            # same-day restart rehydrates the anchor from this row; without it the
            # restarted process re-anchors to whatever NLV is current, which after
            # a loss reads the day's damage as ~0 and disarms the daily-loss
            # breaker. Nothing is recorded when the anchor was RESTORED (this
            # branch is False then) — the first record of the day stays the day's
            # baseline.
            # → docs/decisions/2026-08-20-daily-loss-breaker-restart-rehydration.md
            await record_day_pnl_anchor(
                audit, trading_date=trading_date, anchor_nlv=anchor
            )
            print(f"day P&L anchored at NLV {anchor}")
        elif state.day_pnl.anchored:
            print(f"day P&L anchor RESTORED from today's record: NLV {state.day_pnl.anchor}")
        else:
            # NEITHER anchored NOR restored: the account had no NLV at the one moment
            # the session asks. `DailyPnl.value` then answers 0.0 all day and the
            # daily-loss breaker cannot trip — which used to be SILENT here. The
            # account is REST-seeded just above, so this needs the broker to return a
            # null NLV; rare, and exactly the state nothing else would announce.
            # SAID, not changed: re-trying the anchor later in the day would move the
            # rail's baseline and is its own decision. `/status` shows the same fact
            # as `UNANCHORED` (`RiskSnapshot.daily_pnl_anchored`), and the EOD
            # journal sweep matches this line. (2026-09-18)
            print(
                "DAY P&L NOT ANCHORED: no account NLV at session start — the daily-loss"
                " breaker reads a placeholder 0.0 today and CANNOT TRIP"
            )

        # -- the trading side, and THE terminal action ----------------------
        # `submit=None` is shadow's would-submit path, untouched. Anything else is
        # the entry executor, and the watchdog starts alongside it — spec §4's
        # separate always-watching task, independent of whether entry logic is
        # running.
        submit: SubmitFn | None = None
        if stack is not None:
            assert halt is not None and fills is not None  # guaranteed above
            def _broker_position_count(
                acct: AlertStreamerAccount = acct_stream,
            ) -> int | None:
                """Non-flat LEG positions per the account CACHE, or None if unknown.

                Read off the cache, not REST, because it survives an AlertStreamer
                outage — the REST liveness refresh keeps re-seeding it — and that
                outage is exactly the case the watchdog's book-agreement check exists
                to catch (`positions` is written only from stream fill events). None
                when the snapshot is unusable; the check then SKIPS rather than
                guessing, and the gate is already failing closed on that same state.

                **This is a MIRROR of the broker, not the broker.** Until 2026-08-19
                this docstring said the re-seed made it authoritative; it did not,
                because `seed` merged and never subtracted, so a missed close frame
                left a phantom leg here for the life of the session. `evict_absent`
                now repairs it within `account_position_evict_after_s`, and
                `confirm_broker_position_count` below is what the watchdog asks
                before paging on any disagreement this number produces.
                → docs/decisions/2026-08-19-account-cache-ghost-positions.md
                """
                snap = acct.snapshot()
                return snap.open_position_count if snap is not None else None

            async def _confirm_broker_position_count(
                acct_broker: BrokerAccount = broker,
            ) -> int | None:
                """Non-flat LEG positions per REST — the authority, freshly fetched.

                Used ONLY on the paging edge of the book-agreement check, never in
                the hot path: it is a round trip, where `_broker_position_count` is a
                dict read. Returning None (or raising) makes the watchdog page on the
                cache count, which is the fail-closed direction.
                """
                positions = await acct_broker.fetch_positions()
                return sum(1 for p in positions if p.signed_quantity != 0)

            trading = _build_trading_side(
                config=config,
                audit=audit,
                stack=stack,
                quotes=LegQuotes(
                    md,
                    chain_source,
                    underlying_symbol=underlying,
                    # The same fail-closed staleness bound the pre-trade gate uses.
                    # An exit priced off a stale quote is the failure this whole
                    # resolver exists to prevent.
                    max_age_s=config.staleness.market_data_max_age_s,
                ),
                stop=stop,
                alert=alert,
                halt=halt,
                broker_position_count=_broker_position_count,
                confirm_broker_position_count=_confirm_broker_position_count,
                killswitch=state.killswitch,
                trading_date=trading_date,
                fills=fills,
                # The SAME `chain_source` `LegQuotes` resolves contracts from, so the
                # flatten deferral's IV and the watchdog's legs are one feed.
                implied_vol=ChainImpliedVol(
                    chain_source, MarketCalendar.from_config(config.market)
                ),
            )
            # **The one line MONITOR-ONLY changes** (2026-08-29). The trading side is
            # built whole — the fill consumer books, the watchdog watches, marks,
            # closes and flattens — and only the ENTRY hook is withheld, which is the
            # `submit=None` seam shadow has always used.
            #
            # Belt and braces, deliberately: the kill switch is engaged on every halt,
            # so `PreTradeGate` already refuses every candidate and
            # `EntryExecutor.submit`'s pre-build re-check already refuses anything that
            # slipped through. This adds a STRUCTURAL refusal on top of two behavioural
            # ones, because "nothing submits an order outside `trades_for_real`" is
            # asserted rather than trusted and this deserves the same posture.
            #
            # `EntryExecutor` is still constructed: it is cheap, it owns the
            # `on_filled` hook the watchdog wants, and not building it would fork
            # `_build_trading_side` into a second shape whose watchdog-without-entry
            # wiring nobody has reasoned about.
            submit = None if monitor_only else trading.entry.submit
            # The stub-close allowance's durable spend, read back BEFORE the
            # watchdog starts ticking (2026-09-24): a stub held across a restart
            # gets no fresh allowance-backed send on top of the ordinary attempt
            # the restart legitimately re-arms. Inside the `trading is not None`
            # guard like everything else that touches the watchdog; unreadable
            # fails toward spent with one page, never a halt.
            await trading.watchdog.rehydrate_stub_close()
            trading.watchdog.start()
            mode = (
                "DRY RUN (validates only, routes nothing)"
                if config.execution.dry_run_submissions
                else "LIVE SUBMISSION"
            )
            if monitor_only:
                mode = "MONITOR-ONLY (HALTED: no entries; exits and flatten still run)"
            print(f"\n*** {config.environment.upper()}: {mode} — watchdog running ***")

        # THE EVENT BLACKOUT ARMS ITSELF HERE, and the coupling is worth stating
        # rather than leaving as tribal knowledge. `CreditSpreadStrategy` suppresses
        # entries when `regime.today` is in `blackout_dates` — a check that consults
        # neither `regime_config` nor any "armed" flag. So the blackout is live
        # exactly when BOTH of these hold:
        #
        #   1. the loop passes a non-None `regime` every tick (it does — the regime
        #      is computed unconditionally for telemetry), and
        #   2. `blackout_dates` is non-empty (it is, whenever `[events]` is present
        #      and enabled, which is the shipped state).
        #
        # There is no third "turn it on" step. The corollary matters more: if the
        # regime telemetry is ever kept while `[events]` is dropped, this must fall
        # back to the empty frozenset — which it does — or the claim that the
        # telemetry is a pure no-op stops being true.
        #
        # `regime_config` is deliberately NOT passed. `config.regime` is None while
        # `[regime]` stays out of the shipped configs, so the skew side-gate and the size
        # scaler remain no-ops; passing it through would be the single edit that
        # turns uncalibrated research thresholds into live behaviour.
        blackout_dates: frozenset[date] = frozenset()
        if config.events is not None and config.events.enabled:
            blackout_dates = load_blackout_dates(config.events.blackout_dates_path)
            # The HORIZON, not just the count. This line is read every morning, and a
            # bare count cannot distinguish a list with a year left from one that
            # expires next week — the whole point being that an exhausted list and a
            # quiet calendar are indistinguishable from inside the process.
            # `/status` carries the same fact with a threshold on it
            # (`status.BlackoutRunway`); this is the journal's half.
            through = max(blackout_dates).isoformat() if blackout_dates else "NO DATES"
            print(
                f"event blackout: {len(blackout_dates)} dates loaded, through {through}"
            )
            # AND whether TODAY is one of them — off this very set, the one the
            # strategy is built from two lines down. See `_announce_blackout_day`.
            await _announce_blackout_day(
                audit, trading_date=trading_date, blackout_dates=blackout_dates
            )

        loop = SignalLoop(
            strategy=CreditSpreadStrategy(config.signal, blackout_dates=blackout_dates),
            chain_source=chain_source,
            snapshot_builder=builder,
            gate=state.gate,
            audit=audit,
            config=config,
            submit=submit,  # None in SHADOW: never routes an order
            # The entry pipeline's own dedup, READ-ONLY, for the side-balance
            # selector (2026-09-26): None wherever nothing is submitted.
            attempted=trading.entry.was_attempted if submit is not None else None,
        )
        loop.start()

        # Baseline for the `(+N)` deltas below. Taken here, once the loop is up, so
        # "this session" means the trading day rather than the process — a mid-day
        # restart should not make every counter look like it just started moving.
        #
        # ONE baseline, used for the journal line AND published to `/status`, so the
        # phone-reachable surface and the journal can never disagree about what
        # "this session" means.
        counts_at_start = audit.counts

        # Publish the live risk source so /status can report freshness ages for a
        # session it does not own; mark that we reached the loop (a normal end).
        if status is not None:
            # Link health is published at the SAME instant as the risk source, so
            # `/status` can never report a live feed age next to a stale idea of
            # whether the link is up. Read live off the adapters on each call —
            # nothing is cached, so it cannot drift from what they believe.
            def _link_health(
                md: DXLinkMarketData = md, acct: AlertStreamerAccount = acct_stream
            ) -> LinkHealth:
                return LinkHealth(
                    market_data_connected=md.connected,
                    market_data_reconnects=md.reconnects,
                    account_connected=acct.connected,
                    account_reconnects=acct.reconnects,
                    account_refresh_consecutive_failures=acct.consecutive_refresh_failures,
                    # The lag-ramp instruments. Both SDK streamers buffer into
                    # UNBOUNDED memory object streams, so a climbing depth here is
                    # the accumulation; near-zero exonerates the streaming path.
                    market_data_queue_depths=md.queue_depths(),
                    account_queue_depths=acct.queue_depths(),
                )

            # The watchdog's live evaluability, published at the same instant as the
            # other two. `trading` is None in SHADOW — no watchdog, nothing watched —
            # and the seam stays None there so `/status` renders "not applicable"
            # rather than the "all evaluable" that an empty tuple would mean.
            evaluability = None
            spent_exits = None
            unmanaged = None
            residuals_working = None
            entries_ended = None
            watchdog_health = None
            profit_target = None
            flatten_deferral = None
            if trading is not None:
                def evaluability(wd: PositionWatchdog = trading.watchdog):
                    return _to_evaluability(wd.impaired_positions())

                # The successor state to the one above, and published from the same
                # watchdog inside the same guard. A position whose exit attempt is
                # spent has already been popped out of `impaired_positions()`, so
                # without this seam `/status` renders the worst state the exit path
                # can produce as an ordinary open position (2026-08-21, A2b).
                def spent_exits(wd: PositionWatchdog = trading.watchdog):
                    return _to_unclosable(wd.spent_exit_attempts())

                # The two halves of HELD BUT NOT MANAGED, published from the same
                # trading side inside the same guard (2026-09-02). Both were Tier 1
                # pages that appeared on NO surface between pages — the third and
                # fourth instances of the pattern the two rows above were each fixed
                # for. An unwatchable position is the most serious thing this page can
                # report: `check_once` iterates `scan.spreads`, so it has no trigger
                # evaluated, no mark, and no 15:30 flatten, while still consuming a
                # concurrency slot through the broker's leg count.
                # → docs/decisions/2026-09-02-status-names-what-is-unmanaged.md
                def unmanaged(wd: PositionWatchdog = trading.watchdog):
                    return _to_unmanaged_positions(wd.unwatchable_positions())

                # Off the REGISTRY, not the watchdog — which holds one and could
                # delegate. `residual_orders.py` states the boundary it is keeping:
                # the policy lives there rather than in a position-monitoring module,
                # and a read-only passthrough is the kind of small violation that makes
                # the next one easy.
                def residuals_working(rr: ResidualOrderRecheck = trading.residuals):
                    return _to_residual_rows(rr.residual_rows())

                # The third live watchdog-era read, published at the same instant and
                # from the same trading side. Answers a question no other field can:
                # the session is RUNNING and the entry path has stood down for the
                # day (`EntryExecutor._end_entries_today`, 2026-09-01) — entries off,
                # watchdog and exits and the 15:30 flatten still going. Deliberately
                # NOT folded into P3's `monitor_only`: that one is permanent, engages
                # the kill switch and needs a human, this one clears at the rollover,
                # and "what clears it" is the only question the operator has.
                # → docs/decisions/2026-09-01-end-day-keeps-the-watchdog.md
                def entries_ended(ex: EntryExecutor = trading.entry):
                    return _to_entries_ended(ex.entries_ended)

                # The watchdog's OWN health, published from the same trading side
                # inside the same guard (2026-09-10). Since a watchdog exception
                # degrades rather than parks the process, `phase: running` is no
                # longer evidence that anything is being watched, and this row is
                # the one that says which way it fell. None in shadow, like the rest.
                def watchdog_health(wd: PositionWatchdog = trading.watchdog):
                    return _to_watchdog_health(wd.watchdog_health())

                # The profit target's switch and what it has left, off the same
                # watchdog inside the same guard (2026-09-23). The switch is a config
                # flip, so this row is where an operator confirms which one is live.
                def profit_target(wd: PositionWatchdog = trading.watchdog):
                    return _to_profit_target(wd.profit_target_state())

                # The flatten deferral's loaded mode and parameters, and what it has
                # decided (2026-09-25). A change is a config-only release, so this is
                # where it is confirmed live.
                def flatten_deferral(wd: PositionWatchdog = trading.watchdog):
                    return _to_flatten_deferral(wd.flatten_deferral_state())

            status.session_running(
                builder.build, _link_health, counts_at_start,
                evaluability_source=evaluability,
                spent_exit_source=spent_exits,
                unmanaged_source=unmanaged,
                residual_source=residuals_working,
                entries_ended_source=entries_ended,
                watchdog_health_source=watchdog_health,
                profit_target_source=profit_target,
                flatten_deferral_source=flatten_deferral,
            )
        reached_running = True
        print(
            "\nsignal loop running (%s); ends at the close or on stop.\n"
            % ("submits nothing" if submit is None else "SUBMITS ORDERS")
        )

        while not stop.is_set():
            risk = builder.build()
            cov = chain_source.coverage()
            current_spot = chain_source.spot()
            links = ""
            if md.reconnects or acct_stream.reconnects:
                links = f" reconnects=md:{md.reconnects}/acct:{acct_stream.reconnects}"
            if not md.connected:
                links += " [MD LINK DOWN]"
            print(
                f"  spot={current_spot} "
                f"chain q={cov.with_quote}/{cov.total} g={cov.with_greeks}/{cov.total} "
                # `usable` is present-only; `admissible` also applies the freshness
                # gate and is what the strategy actually sees. `short` is the subset
                # a SHORT strike can be chosen from — the two are separate because
                # a full ladder with `short=0` is a lagging greeks feed and reads
                # nothing like an empty chain, which is precisely how a 95.7%
                # skip rate hid for a session (2026-08-24). Gaps against `usable`
                # are what the gate costs; stale_q/stale_g attribute them.
                f"usable={cov.with_both} admissible={cov.admissible} "
                f"short={cov.short_admissible} "
                f"stale_q={cov.stale_quote} stale_g={cov.stale_greeks} "
                # The FEED, not the gate. `short`/`stale_g` both collapse once the
                # bound is wide enough to admit everything, so on their own they
                # cannot say whether it is wide by 10s or by 100s. This one keeps
                # reporting, and it is what the next bound gets sized from.
                f"g_age={cov.min_greeks_age_s:.1f}s "
                f"md_age={risk.market_data_age_s:.1f}s "
                f"acct_age={risk.account_stream_age_s:.1f}s nlv={risk.account_nlv:g}"
                f"{links} "
                # `audit.counts`, NOT `_counts(db_path)`. Same numbers, from memory.
                # This line polls every 15s for the length of a session (~1,500
                # times), and the query version was a full `GROUP BY` on a
                # freshly-opened connection, ON THE EVENT LOOP — a stall that grew
                # with the audit log (~88k rows and +14k/day as of 2026-08-05).
                f"| {_format_counts(audit.counts, counts_at_start)}"
            )
            await _interruptible_sleep(15.0, stop)
    except SessionAborted as exc:
        # A clean "no session today" — record why, then return without raising.
        print(f"session aborted: {exc}")
        await audit.record("session.aborted", {"reason": str(exc)})
    finally:
        if loop is not None:
            await loop.stop()
        if trading is not None:
            # AFTER the signal loop, BEFORE the streams. After the loop so no new
            # entry can land while positions are going unwatched; before the market
            # data and account streams because `watchdog.stop()` waits for any exit
            # already in flight to finish reporting, and that exit is still walking an
            # order — it needs quotes and the order feed until it returns.
            await trading.watchdog.stop()
        if acct_stream is not None:
            await acct_stream.stop()
        if refresher is not None:
            # AFTER `acct_stream.stop()`, which cancels the refresh loop, so no
            # new dispatch can race the close. BEFORE `_safe_aclose(tt)`, so the
            # Session the bearer token is read from outlives its last reader. A
            # session is built per TRADING DAY, so skipping this leaks a socket a
            # day in a process designed to run for weeks.
            await refresher.aclose()
        if md is not None:
            await md.stop()
        await _safe_aclose(md_tt)
        await _safe_aclose(tt)
        # Terminal phase + clears the live risk source: reached_running True means
        # the loop ran and ended (close/stop); False means an early abort.
        if status is not None:
            status.session_finished(ended=reached_running)
        print("session ended.")


#: The pre-chunk-5 name. Kept as an alias rather than left as the real name: this
#: body now submits real orders in sandbox/live, and a function called
#: `run_shadow_session` doing that is exactly the drift CLAUDE.md exists to prevent.
#: Callers may use either; new code should say `run_session`.
run_shadow_session = run_session
