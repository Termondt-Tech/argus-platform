"""Application supervisor — the always-on process (spec §4).

This is the single persistent asyncio process the whole system runs inside. It
owns the one event loop for the life of the deployment and composes two layers:

    PROCESS lifetime (built once, survives across trading days):
        config -> DB lock -> SQLite + migrations -> audit log (Tier 1/2)
               -> kill switch -> loop heartbeat -> session scheduler

    SESSION-DAY lifetime (rebuilt fresh for each trading day):
        daily-loss breaker -> day-P&L anchor -> pre-trade gate -> broker/market
        data -> signal loop

The split is the day-rollover contract (CLAUDE.md): the kill switch is
process-lifetime, so a trip survives into the next day and a rollover can never
un-halt the system — and since 2026-09-01 it survives the PROCESS too, read back
from the audit log at startup so a restart cannot un-halt it either
(`risk/kill_state.py`); the breaker and day-P&L anchor are keyed to the TRADING DATE,
so neither can carry yesterday's latch or yesterday's P&L into today — and
neither is lost to a restart WITHIN a day, which is a separate guarantee and was
missing until 2026-08-20 (`risk/rehydrate.py`).

`startup()`/`shutdown()` bring the process-lifetime layer up and down; `run()`
adds signal handlers and drives the `SessionScheduler`, which starts a session at
each ET open and ends it at the close, idling in between.

**Credential-free boot is now SHADOW-ONLY** (chunk 5). Shadow still needs no broker
credentials outside a session, because login only happens inside one. Sandbox and
live do NOT get that: spec §6 requires reconciling broker state before resuming on
every restart, so `_reconcile` opens a short-lived trading session at startup
(`session.startup_reconcile`), runs the full position+order check, and closes it
again. Nothing broker-related is held for the process's lifetime either way — but a
sandbox/live process with no credentials refuses to start rather than starting blind.

The one secret every environment needs from the first moment is the kill-switch
bearer token (`ARGUS_KILL_TOKEN`): the always-on `POST /kill` control surface
(`control.py`, spec §6) must be authenticated the instant the process is up, so a
missing token fails startup loud rather than exposing an unauthenticated halt.

The control server is a PROCESS-lifetime resource — bound at startup on the
private Tailscale interface and serving continuously, including nights, weekends,
and idle-between-days, since "reachable at any time" is exactly the requirement.
A bind failure (Tailscale down / wrong IP) refuses startup; it never falls back
to a wider bind.

`tools.shadow` reuses the same machinery for a single time-boxed dev run via
`run_single_session`, so the pipeline never lives in two places.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import gc
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from argus._version import build_version
from argus.alerts import AlertFn, log_alert
from argus.audit.log import AuditLog, AuditWriteError
from argus.broker import credential_slot_labels
from argus.clock import et_now, et_today
from argus.config import Config, ConfigError, EventConfig, GcConfig, load_blackout_dates
from argus.control import ControlServer, load_kill_token
from argus.db import apply_migrations, connect
from argus.db.lock import DatabaseLock
from argus.heartbeat import LoopHeartbeat
from argus.marketcalendar import MarketCalendar
from argus.memprofile import MemoryProfiler
from argus.notify import SlackNotifier, build_alert_fn
from argus.reconcile import (
    BrokerUnreachable,
    HaltGrade,
    ReconciliationError,
    ReconciliationResult,
    Reconciler,
    halt_grade,
    mismatch_halts_process,
    reconcile_with_retry,
)
from argus.risk.breaker import DailyLossBreaker
from argus.risk.gate import PreTradeGate
from argus.risk.kill_state import (
    RESTORE_FAILED_CATEGORY,
    KillSwitchState,
    load_kill_switch_state,
)
from argus.risk.kill_state import RESTORED_CATEGORY as KILL_RESTORED_CATEGORY
from argus.risk.killswitch import KillSwitch
from argus.risk.rehydrate import (
    RESTORED_CATEGORY,
    DayRiskRehydrationError,
    DayRiskState,
    load_day_risk_state,
)
from argus.risk.snapshot import DailyPnl
from argus.scheduler import SessionScheduler
from argus.session import (
    SessionAborted,
    SessionState,
    run_session,
    session_settled,
    startup_reconcile,
)
from argus.status import BlackoutRunway, SessionStatus, StatusService

logger = logging.getLogger("argus.app")

# session_body(config, audit, state, stop, trading_date, alert, account_number)
SessionBody = Callable[..., Awaitable[None]]

#: How often a parked process repeats its "still halted" line to the journal. Log
#: only — the Slack alert fires ONCE on entering the state. A parked process is
#: indistinguishable from a healthy one in `systemctl status` (the unit is
#: legitimately active), so the journal has to keep saying so; 15 minutes is often
#: enough that a tail shows it promptly and rare enough that a multi-day halt does
#: not bury the surrounding lines.
HALTED_REMINDER_INTERVAL_S = 900.0


@dataclass(frozen=True)
class HaltRecord:
    """Why this process will not trade again without a human, and how wide it reaches.

    Distinct from the kill switch, which it always accompanies. The switch means
    "no new entries"; this means "not trading again until a human resolves the
    cause" — the two differ for `/kill`, which engages the switch and deliberately
    leaves the process running and able to resume the next day.

    **`grade` is what a halt STOPS, and it is not what a halt MEANS** (2026-08-29).
    Both grades are permanent for the process and both engage the kill switch, so
    neither ever enters again. They differ only in whether the process is also
    asked to stop:

    - `ALL` — no further sessions, the current one ends, the process parks. The
      default, and what every halt source that is not a reconciliation verdict
      gets without being edited.
    - `ENTRIES` — the scheduler keeps running and the day's session comes up with
      its entry hook unwired, so the WATCHDOG runs: virtual stops are evaluated,
      held positions are marked, and the time-based flatten fires. Earned only by
      a reconciliation verdict `reconcile.halt_grade` classifies as carrying no
      position implication.

    **The two fields have OPPOSITE update rules and that is deliberate.** `reason`
    is first-writer-wins (a cascade's first cause is the useful one); `grade` is
    strictest-wins (`HaltGrade.tighten`). See `_mark_halted`.
    """

    reason: str
    source: str
    at: datetime
    grade: HaltGrade = HaltGrade.ALL


#: Set to enable asyncio debug mode + slow-callback logging. See `enable_loop_debug`.
LOOP_DEBUG_ENV_VAR = "ARGUS_LOOP_DEBUG"
#: Seconds a single callback may occupy the loop before it is logged, with its
#: identity. Deliberately well above the heartbeat's 0.05s breach threshold: the
#: target is the ~0.7-1.0s stall, and logging every 50ms callback would bury it.
SLOW_CALLBACK_ENV_VAR = "ARGUS_SLOW_CALLBACK_S"
DEFAULT_SLOW_CALLBACK_S = 0.25


def enable_loop_debug(env: dict[str, str] | None = None) -> float | None:
    """Ask asyncio to NAME the callback that blocks the loop. Off unless asked.

    The intra-session lag ramp is, as of 2026-08-06, a ~0.7-1.0s block arriving
    once per ~11s and growing linearly through a session. Everything measured so
    far says what it is NOT: not gen-2 GC (2 collections in 22h against 11,522
    breaches), not a Python object leak (tracemalloc flat at ~3,060 KiB across
    three hours while lag tripled), not per-call accumulation in the account REST
    path (flat over 4,000 refreshes). The period pins it to the 10s account
    liveness refresh, but nothing on that path is anywhere near a second.

    Rather than keep narrowing by elimination, this asks the runtime directly.
    `loop.set_debug(True)` makes `Handle._run` time every callback and log any
    that exceeds `slow_callback_duration`, WITH THE CALLBACK'S IDENTITY —
    `Executing <Handle ...> took 1.234 seconds`. That is the one diagnostic that
    returns a name instead of another exclusion.

    Env-gated and inert by default, exactly like `ARGUS_TRACEMALLOC`, so it ships
    dark and costs a deploy nothing. It is NOT free when on: debug mode also
    enables coroutine origin tracking and non-threadsafe-call checks, so leave it
    on for a session or two, read the names, and turn it off. Returns the
    threshold in effect, or None when disabled.
    """
    raw = (env if env is not None else os.environ).get(LOOP_DEBUG_ENV_VAR, "").strip()
    if raw.lower() not in {"1", "true", "yes", "on"}:
        return None
    threshold = DEFAULT_SLOW_CALLBACK_S
    override = (env if env is not None else os.environ).get(SLOW_CALLBACK_ENV_VAR, "").strip()
    if override:
        try:
            parsed = float(override)
        except ValueError:
            logger.warning(
                "%s=%r is not a number; using %.2fs", SLOW_CALLBACK_ENV_VAR, override,
                DEFAULT_SLOW_CALLBACK_S,
            )
        else:
            # A non-positive threshold logs EVERY callback, which on a 2,420
            # event/sec feed is a journal flood that hides the thing being hunted.
            if parsed > 0:
                threshold = parsed
            else:
                logger.warning("%s must be > 0; using %.2fs", SLOW_CALLBACK_ENV_VAR,
                               DEFAULT_SLOW_CALLBACK_S)
    loop = asyncio.get_running_loop()
    loop.set_debug(True)
    loop.slow_callback_duration = threshold
    logger.warning(
        "asyncio DEBUG MODE on: callbacks over %.2fs will be logged by name. "
        "This adds real overhead — diagnostic only, not a steady-state setting.",
        threshold,
    )
    return threshold


def tune_gc(cfg: GcConfig | None = None) -> None:
    """Cut the cost and frequency of gen-2 GC. Called once, at the end of startup.

    NOT the cause of the intra-session lag ramp — that claim was made here and is
    now FALSIFIED. On 2026-08-06 this ran in production for a full session and the
    ramp was unchanged to three decimals at every hour (09h 0.086 / 11h 0.374 /
    14h 1.027, all inside the pre-tuning baseline band) while gen-2 collections
    fell from ~500/hour to 2 in 22 hours. Keep it — removing ~500 pauses an hour
    is real work saved — but it is a reduction, not a fix, and the ramp is still
    open. The reasoning below stands as measurement of GC; only its conclusion
    about causation was wrong:

      - The DXLink subscription delivers up to 2,420 events/second (121 symbols x
        Quote+Greeks at `refresh_interval=0.1`), each allocating several tracked
        container objects. At CPython's default `(700, 10, 10)` thresholds a gen-2
        pass runs every ~70,000 net container allocations, i.e. **every ~7
        seconds** at that rate — which is the observed ~9-10s breach period.
      - A gen-2 pause is LINEAR in the number of live objects: measured 4ms at 50k
        objects, 99ms at 1M, 198ms at 2M (x~4 on the droplet's vCPU). That is the
        observed ramp from 0.065s at the open to 1.3s mean by 15:00.

    Two independent levers, both applied here:

    **`gc.freeze()`** moves everything alive right now — the interpreter, every
    imported module, the config, all the code objects — into a permanent generation
    that gen-2 never traverses again. Startup objects are long-lived by definition
    and were being rescanned every seven seconds forever. Measured: a 197ms gen-2
    pause over a 2M-object heap dropped to 0.0ms. It must run AFTER startup has
    built everything and BEFORE the first session, which is exactly here.

    **A 10x higher gen-2 threshold** makes the pass run every ~700,000 allocations
    instead of ~70,000 — roughly once a minute rather than once every seven
    seconds. It trades a little peak memory (garbage lives longer between passes)
    for an order of magnitude fewer pauses, which is the right trade on a 4GB box
    holding ~114MB. gen-0 and gen-1 are UNTOUCHED: they are cheap, they collect the
    short-lived quote churn that actually needs collecting, and slowing them would
    grow the heap for no benefit.

    Neither changes program semantics. Reference counting still frees everything
    acyclic immediately; this only affects when CYCLES are reclaimed. It is not a
    substitute for finding what accumulates — that is still open — but it reduces
    the impact by roughly an order of magnitude while that work happens.
    """
    cfg = cfg or GcConfig()
    gc.collect()
    if cfg.freeze_on_startup:
        gc.freeze()
    gen0, gen1, _ = gc.get_threshold()
    gc.set_threshold(gen0, gen1, cfg.gen2_threshold)
    # NOT `len(gc.get_objects())` — materializing the object graph to log a number
    # would be a multi-hundred-ms stall in the function whose job is removing them.
    logger.info(
        "gc tuned: startup heap %s, thresholds now %s",
        "frozen" if cfg.freeze_on_startup else "NOT frozen (disabled in [gc])",
        gc.get_threshold(),
    )


def read_blackout_runway(events: EventConfig | None) -> BlackoutRunway:
    """Read the macro-event blackout list and report how much of it is left.

    The caller-side half of `status.BlackoutRunway`, here rather than in `status.py`
    for the reason every other derived status type is built by its owner: `status.py`
    takes primitives and holds no config loader, and giving it one would make the
    monitoring surface a second reader of the config file.

    **`fomc_dates.txt` is deliberately not consulted.** It is an INPUT to
    `argus.tools.blackout_dates`, not to the runtime — `CreditSpreadStrategy` sees one
    frozenset, loaded from `[events] blackout_dates_path` and nothing else — so the
    horizon that matters is that file's, and reading the generator's inputs here would
    report a runway the running strategy does not have.

    **Never raises.** A `ConfigError` is captured as state, not propagated: this is a
    monitoring read, and an unreadable file is already fatal where it counts (session
    start), so turning it fatal here as well would only cost the operator the page
    that explains why.
    """
    if events is None or not events.enabled:
        return BlackoutRunway(enabled=False)
    try:
        dates = load_blackout_dates(events.blackout_dates_path)
    except ConfigError as exc:
        return BlackoutRunway(enabled=True, error=str(exc)[:300])
    except OSError as exc:  # unreadable for a reason the loader does not model
        return BlackoutRunway(
            enabled=True, error=f"{type(exc).__name__}: {str(exc)[:280]}"
        )
    return BlackoutRunway(
        enabled=True,
        last_date=max(dates) if dates else None,
        date_count=len(dates),
        dates=dates,
    )


def _log_blackout(runway: BlackoutRunway, today: date) -> None:
    """One line at startup, at the severity the state deserves.

    Matches what the observability convention here actually is — a `/status` field
    AND a log line, the shape `heartbeat`/`staleness` already use — rather than
    picking one. The journal is where an operator is already looking on the morning
    of a deploy; `/status` is where they look from a phone three weeks later.

    INFO on a healthy runway, and that is not the same as silent: the HTML row stays
    colourless while `ok`, but a startup line naming the horizon is what lets a deploy
    be checked against the file that was supposed to ship with it.
    """
    state = runway.state(today)
    if state == "disabled":
        logger.info("event blackout: OFF ([events] disabled or absent)")
        return
    if state == "unreadable":
        logger.warning("event blackout: UNREADABLE (%s) — sessions will abort at start",
                       runway.error)
        return
    if state == "exhausted":
        logger.warning(
            "event blackout: EXHAUSTED (last date %s) — FOMC/CPI/NFP/PCE days are "
            "now being traded as ordinary days; regenerate with "
            "`python -m argus.tools.blackout_dates`",
            runway.last_date,
        )
        return
    days = runway.days_remaining(today)
    if state == "expiring":
        logger.warning(
            "event blackout: %d date(s), EXPIRING in %d day(s) (last %s) — "
            "regenerate with `python -m argus.tools.blackout_dates`",
            runway.date_count, days, runway.last_date,
        )
        return
    logger.info(
        "event blackout: %d date(s), through %s (%d days of runway)",
        runway.date_count, runway.last_date, days,
    )


class Application:
    def __init__(
        self,
        config: Config,
        *,
        alert: AlertFn | None = None,
        reconciler: Reconciler | None = None,
        session_body: SessionBody | None = None,
        account_number: str | None = None,
        now_et: Callable[[], datetime] | None = None,
        wait_fn: Callable[[float], Awaitable[None]] | None = None,
        kill_token: str | None = None,
    ) -> None:
        self._config = config
        self._alert = alert or log_alert
        # An explicitly injected alert sink wins and is left alone (tests, tools).
        # Otherwise startup() resolves the real phone-reachable path — the Slack
        # notifier — so that every existing AlertFn consumer (audit queue depth and
        # write failure, heartbeat stalls, reconcile mismatches, session errors,
        # foreign fills, walk failures) reaches a human without each having to know
        # how. That seam has existed since `alerts.py` was written; until now nothing
        # was plugged into it.
        self._alert_injected = alert is not None
        self._notifier: SlackNotifier | None = None
        # Reads ARGUS_TRACEMALLOC here; `start()` at the end of startup() is what
        # actually begins tracing. Constructed unconditionally so `shutdown()` has
        # something to call and the disabled path stays a single no-op branch.
        self._memprofile = MemoryProfiler()
        # The kill-switch endpoint's bearer token. Injected in tests; None means
        # "read it from ARGUS_KILL_TOKEN at startup" (fail loud if absent).
        self._kill_token = kill_token
        # Injected because building a real reconciler needs a broker session +
        # network. None no longer means "no process-start reconcile": sandbox/live
        # build one themselves (`session.startup_reconcile`), and only shadow skips.
        # The seam remains so tests and tools can supply a fake.
        self._reconciler = reconciler
        # The per-day session body. Injected in tests (a fake that needs no
        # credentials or live market); defaults to the real pipeline. The
        # injection seam is what lets the scheduling/rollover tests run offline.
        self._session_body = session_body
        self._account_number = account_number
        # Clock/wait seams for the scheduler — overridden in tests to drive many
        # trading days deterministically without real sleeping.
        self._now_et = now_et or et_now
        self._wait_fn = wait_fn

        # TWO events, and the difference is the whole of the halted-but-alive
        # design (2026-08-17). `_stop` means "stop TRADING" — end the session, do
        # not start another. `_exit` means "leave the PROCESS", and only an
        # operator signal sets it. An unrecoverable halt sets `_stop` alone, so the
        # process parks with the control surface still bound instead of exiting
        # into a restart loop that cannot fix the condition.
        self._stop = asyncio.Event()
        self._exit = asyncio.Event()
        #: Set when a rail has refused to let this process trade again without a
        #: human. Read by `run()` (park rather than exit) and by /status. Carries
        #: the GRADE — how wide the refusal reaches — since 2026-08-29; ask through
        #: `halt_grade()` rather than testing `is not None`, which conflates the two.
        self._halted: HaltRecord | None = None
        #: The periodic "still halted" journal line, started on the halt edge and
        #: cancelled in teardown. Runs under BOTH grades: a monitor-only process
        #: looks even more like a healthy one than a parked process does.
        self._halted_reminder: asyncio.Task | None = None
        #: The daily-loss breaker `_run_session_day` built LAST, paired with the
        #: trading date it was built for. Exists so /status can report the rail the
        #: gate is actually holding instead of inferring it from a
        #: `daily_loss.breach` row that outlives the process. The date is not
        #: decoration — see `_breaker_for_today`.
        self._day_breaker: tuple[date, DailyLossBreaker] | None = None
        self._started = False

        # Wired up in startup().
        self._lock: DatabaseLock | None = None
        self._conn = None
        self._audit: AuditLog | None = None
        self._killswitch: KillSwitch | None = None
        self._heartbeat: LoopHeartbeat | None = None
        self._scheduler: SessionScheduler | None = None
        self._control: ControlServer | None = None
        # Process-lifetime session-state publisher for the monitoring surface; the
        # per-day session body writes its phase and live risk source here.
        self._status: SessionStatus | None = None
        self._status_service: StatusService | None = None
        self._started_at: datetime | None = None

    # -- exposed collaborators (for the monitoring surface / tests) --------

    @property
    def audit(self) -> AuditLog:
        assert self._audit is not None, "not started"
        return self._audit

    @property
    def killswitch(self) -> KillSwitch:
        assert self._killswitch is not None, "not started"
        return self._killswitch

    @property
    def heartbeat(self) -> LoopHeartbeat:
        assert self._heartbeat is not None, "not started"
        return self._heartbeat

    @property
    def control(self) -> ControlServer:
        assert self._control is not None, "not started"
        return self._control

    @property
    def status(self) -> SessionStatus:
        assert self._status is not None, "not started"
        return self._status

    @property
    def status_service(self) -> StatusService:
        assert self._status_service is not None, "not started"
        return self._status_service

    # -- lifecycle ---------------------------------------------------------

    async def startup(self) -> None:
        if self._started:
            raise RuntimeError("already started")
        cfg = self._config

        # The very first line of the journal, deliberately: it acquires nothing, can
        # refuse nothing, and "what is actually deployed?" is the question a FAILED
        # startup makes most urgent — a version that only printed on success would be
        # missing exactly when it is wanted. `unknown` is a real answer here (an
        # unstamped build), never an error. → argus/_version.py
        version = build_version()
        # ONE line carrying BOTH facts, not two. The deploy script greps this
        # anchor and reads the version and the config fingerprint back from the
        # same match — and the config half is the only post-restart assertion a
        # config-only release has, since on one of those the version legitimately
        # still names the previous tag. → config.ConfigSource
        src = cfg.source
        logger.info(
            "argus build %s config %s/%s @%s",
            version,
            src.profile if src and src.profile else "-",
            src.filename if src else "-",
            src.sha if src else "-",
        )

        # Resolve the kill-switch token FIRST — before acquiring any resource — so
        # a missing token fails loud with nothing to tear down. An unauthenticated
        # halt control is a broken safety rail (spec §6), so the process refuses to
        # start without it rather than run with the endpoint exposed or disabled.
        kill_token = self._kill_token if self._kill_token is not None else load_kill_token()

        # Resolve the alert sink before anything that could need to alert. Unlike the
        # kill token this NEVER refuses startup: a missing Slack webhook is a missing
        # notification channel, not a broken safety rail — every alert still reaches
        # the log and every rail still functions (see `notify.load_webhook_url`).
        if not self._alert_injected:
            self._alert, self._notifier = build_alert_fn(environment=cfg.environment)
            if self._notifier is not None:
                self._notifier.start()

        # BEFORE opening the database. The audit chain assigns `seq` from
        # in-memory state, so a second writer process silently collides and
        # wedges both logs. Refuse to start rather than corrupt the chain.
        self._lock = DatabaseLock(cfg.audit.db_path)
        self._lock.acquire()

        self._conn = connect(cfg.audit.db_path, synchronous=cfg.audit.synchronous)
        apply_migrations(self._conn)

        self._audit = AuditLog(
            self._conn,
            tier2_queue_maxsize=cfg.audit.tier2_queue_maxsize,
            tier2_queue_alert_depth=cfg.audit.tier2_queue_alert_depth,
            alert=self._alert,
        )
        self._audit.start()

        # **The halt is state of the world, not state of this process**
        # (2026-09-01). Built from what the audit log says rather than from
        # `False`, so a `/kill` an operator pressed at 10:00 is still in force
        # after the 10:05 OOM/deploy/reboot they never saw. Never raises and fails
        # CLOSED to engaged — "I cannot say whether a kill is in force" must not
        # resolve to "it isn't". → risk/kill_state.py
        # Deliberately a LOCAL and not an attribute: the switch itself carries
        # engaged/reason/source/restored, and a second copy on the Application
        # would be state that only cannot drift because nothing writes it.
        kill_state = await load_kill_switch_state(self._audit)
        # The alert sink is resolved above, so every trip — from the phone endpoint,
        # the heartbeat, the audit-failure hook, or a reconcile mismatch — reaches
        # Slack from the single funnel inside `engage` rather than from each caller.
        self._killswitch = KillSwitch(
            self._audit,
            alert=self._alert,
            initially_engaged=kill_state.engaged,
            initial_reason=kill_state.reason,
            initial_source=kill_state.source,
        )
        # Close the loop: an unwritable audit log halts new entries. The
        # recorder is the precondition for every other action, so losing it must
        # stop trading, not be survived silently.
        self._audit.on_failure = self._halt_on_audit_failure

        on_sustained = self._trip_on_stall if cfg.heartbeat.trip_on_sustained_stall else None
        self._heartbeat = LoopHeartbeat(
            interval_s=cfg.heartbeat.interval_s,
            lag_threshold_s=cfg.heartbeat.lag_alert_threshold_s,
            sustained_breach_count=cfg.heartbeat.sustained_breach_count,
            alert=self._alert,
            on_sustained=on_sustained,
        )

        self._scheduler = SessionScheduler(
            calendar=MarketCalendar.from_config(cfg.market),
            run_session=self._run_session_day,
            stop=self._stop,
            now_et=self._now_et,
            wait_fn=self._wait_fn,
        )

        # The macro-event blackout list's remaining runway, read HERE — where it is
        # consumed — and never again. It is a FINITE generated file (`blackout_dates.txt`
        # ends 2026-12-23), and the day it runs out Argus starts trading
        # FOMC/CPI/NFP/PCE days as ordinary days with nothing inside the process able
        # to tell that apart from a quiet calendar: the blackout's only observable is
        # entries not happening. Never refuses startup — this is visibility, not a
        # rail, and an unreadable file already stops every session at its own loader.
        # → status.BlackoutRunway
        blackout = read_blackout_runway(cfg.events)
        _log_blackout(blackout, self._now_et().date())

        # The monitoring surface's data seam (spec §3/§5). The publisher is
        # process-lifetime — the per-day session body writes its phase and live
        # risk source into it; the service assembles the read-only StatusSnapshot
        # the control server serves at GET /status and GET /.
        self._started_at = self._now_et()
        self._status = SessionStatus(now=self._now_et)
        cred_slot_trading, cred_slot_marketdata = credential_slot_labels(cfg)
        self._status_service = StatusService(
            environment=cfg.environment,
            started_at=self._started_at,
            session_status=self._status,
            killswitch=self._killswitch,
            heartbeat=self._heartbeat,
            audit=self._audit,
            version=version,
            config_profile=src.profile if src else None,
            config_filename=src.filename if src else None,
            config_sha=src.sha if src else None,
            # Which tastytrade REGISTRATION each session uses — names only, and a
            # pure function of `environment` / `marketdata.source`, so it is lifted
            # here beside the other config-derived scalars rather than reaching into
            # the environment. See `broker.credential_slot_labels`.
            credential_slot_trading=cred_slot_trading,
            credential_slot_marketdata=cred_slot_marketdata,
            # The account those credentials ADDRESS. Config-derived scalar, lifted
            # here with the other four for the same reason: `status.py` imports no
            # config loader. None = unpinned (accounts[0]), which live cannot reach
            # — the loader refuses (2026-09-10, C9).
            broker_account_number=cfg.broker.account_number,
            blackout=blackout,
            market_data_max_age_s=cfg.staleness.market_data_max_age_s,
            account_stream_max_age_s=cfg.staleness.account_stream_max_age_s,
            audit_queue_alert_depth=cfg.audit.tier2_queue_alert_depth,
            audit_queue_maxsize=cfg.audit.tier2_queue_maxsize,
            next_open=lambda: self._scheduler.next_session_open(self._now_et()),
            now=self._now_et,
            # Late-bound on purpose: the halt happens long after this is built, and
            # a parked process's whole reason to still be alive is that this page
            # can report why.
            halt_state=lambda: self._halted,
            # Late-bound for the same reason, and load-bearing for a different one:
            # the breaker is rebuilt every trading day, and reading its state off a
            # `daily_loss.breach` row instead of the object let the page report a
            # halt that was not in force. → `StatusSnapshot.breaker_tripped`.
            breaker=self._breaker_for_today,
        )

        # The always-on phone-reachable kill surface (spec §6), which also hosts
        # the read-only status routes (same listener, same auth). Bound in the
        # guarded block below so a bind failure (Tailscale down) tears down cleanly.
        self._control = ControlServer(
            host=cfg.server.bind_host,
            port=cfg.server.bind_port,
            token=kill_token,
            killswitch=self._killswitch,
            audit=self._audit,
            alert=self._alert,
            status_service=self._status_service,
            # Read live, for `/resume` alone: a process holding a HaltRecord will
            # not trade again whatever the route does, and the response says so
            # rather than the route behaving differently.
            halt_state=lambda: self._halted,
        )

        try:
            # The control surface binds FIRST, BEFORE the reconciliation rail
            # (2026-08-17, reordered). It is specified as the always-on
            # phone-reachable halt control, and binding it after a rail that can
            # refuse startup made it unreachable during exactly the failure an
            # operator most needs to look at: on 2026-08-14 a reconcile mismatch
            # raised here 34 times and the listener never came up once.
            #
            # A bad bind (Tailscale down, missing interface) still tears down and
            # re-raises rather than falling back to a wider address — that property
            # is why this is inside the guarded block, and it is unchanged.
            self._heartbeat.start()
            self._status_service.start_sampling()
            await self._control.start()
        except BaseException:
            await self._teardown()
            raise

        # Tier 1: the process starting is a crash-relevant boundary in the trail.
        # Written BEFORE reconciliation, because by here the process genuinely HAS
        # started — it is serving /status and /kill. A halt that follows is its own
        # record (`process.halted`), which reads as a state transition rather than
        # as a process that never existed.
        host, port = self._control.bound_address or (cfg.server.bind_host, cfg.server.bind_port)
        await self._audit.record(
            "process.start",
            {
                "environment": cfg.environment,
                # Which BUILD produced everything that follows in the chain. The
                # journal rotates and `/status` only ever describes the process
                # answering right now; this is the durable answer to "which code
                # wrote this day's records?".
                "version": version,
                "trip_on_sustained_stall": cfg.heartbeat.trip_on_sustained_stall,
                "kill_endpoint": f"{host}:{port}",
            },
        )

        # AFTER `process.start` and BEFORE the reconciliation rail. The order is
        # the trail an operator reads: the process started, AND it started halted.
        # Before `process.start` it would read as a halt on a process that never
        # existed; after the reconcile it could be buried under a park.
        if kill_state.engaged:
            await self._announce_restored_kill(kill_state)

        try:
            await self._reconcile()
        except ReconciliationError as exc:
            # The rail did its job: this process must not trade. What it must NOT do
            # is die — dying drops the surface the operator needs and hands the
            # condition to a supervisor that cannot fix it. Park instead, fully
            # halted and fully observable. `run()` waits for a signal from here.
            #
            # **How wide it parks is the verdict's own property** (2026-08-29), not
            # this call site's: `halt_grade` reads the discrepancy set and answers
            # ENTRIES only for an order-level discrepancy carrying no position
            # implication. Computed from `exc.result` rather than passed down from
            # `_apply_reconcile_verdict`, so the raise stays exactly what it was and
            # the two refusal sites keep deriving the grade from one function.
            grade = halt_grade(exc.result)
            await self._halt_process(
                f"broker reconciliation mismatch ({exc.result.total_discrepancies} "
                "discrepancy(ies))",
                source="reconciliation_mismatch",
                grade=grade,
                detail={**exc.result.as_payload(), "halt_grade": str(grade)},
            )
        except BrokerUnreachable as exc:
            # Could not ASK — a different verdict from "asked and disagreed", and
            # until 2026-08-26 it was not a verdict at all: it fell to the handler
            # below, tore the process down and exited into Docker's blind restart.
            # That ran five times in 33 seconds with no /status, no /kill and no
            # halt reason recorded anywhere. The bounded retry inside
            # `startup_reconcile` has already spent its whole budget by the time
            # this is reached, so what is left is terminal — and terminal means
            # PARK, on the same seam a mismatch has used since 2026-08-17.
            await self._halt_process(
                f"broker unreachable at process start ({exc})",
                source="broker_unreachable",
                detail=exc.as_payload(),
            )
        except SessionAborted as exc:
            # Asked, and the answer is a CONFIGURATION error (no matching account).
            # Permanent, so it is never retried — but it is still a rail refusing on
            # a known condition rather than an unknown-state failure, so it parks
            # rather than exiting, for the same reason.
            await self._halt_process(
                f"broker reconciliation could not start ({exc})",
                source="broker_unreachable",
                detail={"error_type": type(exc).__name__, "error": str(exc)[:500]},
            )
        except BaseException:
            # Anything else during reconciliation is an unknown-state failure, not a
            # rail refusing on a known one. Tear down and re-raise as before — a
            # process that dies from a genuine BUG must still exit non-zero so the
            # supervisor restarts it (2026-08-17). Only deliberate halts stop looping,
            # and the three handlers above are the deliberate ones.
            await self._teardown()
            raise
        # LAST, and deliberately so: everything process-lifetime is now built, so
        # `gc.freeze()` moves the whole startup heap out of gen-2's scan permanently.
        # Doing it earlier would leave later allocations behind to be rescanned every
        # pass for the life of the process. See `tune_gc`.
        tune_gc(cfg.gc)
        # Same placement, same reason: the profiler's baseline must exclude every
        # import, the config, the DB and the control server, so its report is about
        # what a RUNNING process accumulates. Inert unless ARGUS_TRACEMALLOC is set.
        self._memprofile.start()
        # Inert unless ARGUS_LOOP_DEBUG is set. Last, so debug mode's per-callback
        # overhead never applies to startup itself — the 09:40 chain load is
        # already the largest stall of the day and does not need inflating.
        enable_loop_debug()
        self._started = True
        logger.info("argus started in %s mode", cfg.environment)

    async def shutdown(self) -> None:
        if not self._started:
            return
        logger.info("argus shutting down")
        # FIRST, before anything is torn down: the profiler's final report is most
        # informative at the end of a session, when accumulation has had all day to
        # show itself, and it must be taken while the live objects are still alive.
        await self._memprofile.stop()
        if self._audit is not None and self._audit.failed is None:
            # Record the stop first, then drain/close in teardown. Skip if the log
            # has latched failed — every record() would raise, and shutdown must
            # still tear down cleanly.
            with contextlib.suppress(AuditWriteError):
                await self._audit.record("process.stop", {"reason": "shutdown"})
        await self._teardown()
        self._started = False

    async def _teardown(self) -> None:
        """Release resources. Safe to call on a partial startup (idempotent-ish)."""
        # FIRST: it is a bare log loop with no resources of its own, and leaving it
        # running would keep writing "still halted" through the shutdown it is
        # describing.
        if self._halted_reminder is not None:
            self._halted_reminder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._halted_reminder
            self._halted_reminder = None
        if self._heartbeat is not None:
            await self._heartbeat.stop()
        if self._status_service is not None:
            await self._status_service.stop_sampling()
        # Stop the control surface before closing the audit log, so an in-flight
        # /kill can't try to write a trip into a DB that is being torn down.
        if self._control is not None:
            await self._control.stop()
            self._control = None
        if self._audit is not None:
            await self._audit.aclose()
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        # Released last, and after the connection is closed, so the lock's
        # lifetime strictly contains every write.
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        # The notifier goes down LAST so alerts raised during teardown (an audit
        # close failure, a stuck stream) still get delivered. Its drain is
        # time-boxed — an unreachable Slack must not stop the process exiting.
        if self._notifier is not None:
            await self._notifier.aclose()
            self._notifier = None

    async def run(self) -> None:
        """Start, install signal handlers, drive the scheduler, then shut down.

        The scheduler loops over trading days until the process stop event is set
        (SIGINT/SIGTERM). It idles outside market hours and runs one shadow
        session per trading day.

        Three ways out, and only one of them exits on its own:

        - **Signalled** (SIGINT/SIGTERM): shut down and exit. Unchanged.
        - **Halted `ALL`** (a rail refused, widely): park. Serve /status and
          /kill, run no session, and wait for a signal. Exiting here would drop the
          surface and hand an unfixable condition to a supervisor that will only
          retry it.
        - **Startup halted `ALL`**: `startup()` already parked us before the
          scheduler ever ran; skip it entirely and go straight to the same wait.

        **A halt graded `ENTRIES` is none of those** (2026-08-29): the scheduler
        runs on and the process leaves only by signal, or by escalating to `ALL`
        and falling into the park above. It is still halted, still engaged, still
        HALTED on the page and still unable to enter — what it keeps is the
        watchdog, which is the only thing that can close or flatten a held
        position.
        """
        await self.startup()
        self._install_signal_handlers()
        assert self._scheduler is not None
        try:
            # **The gate is the GRADE, not `is not None`** (2026-08-29). A
            # `HaltGrade.ENTRIES` halt still runs the scheduler: it has engaged the
            # kill switch, so no session can enter anything, but held positions keep
            # a watchdog — which since P2 is also the only thing that can fire the
            # time-based flatten. A halt that parks before any session exists takes
            # the day's last-resort control with it, which is the whole of finding 7.
            if self.halt_grade() is not HaltGrade.ALL:
                await self._scheduler.run()
            # An ENTRIES halt leaves the scheduler only when it escalates to ALL
            # (whose `request_stop` ends the loop) or on a signal — so reaching here
            # still means park or exit, and this condition is unchanged.
            if self._halted is not None and not self._exit.is_set():
                await self._await_exit_while_halted()
        finally:
            await self.shutdown()

    async def _announce_halt(self) -> None:
        """Record the halt ONCE, page ONCE, and start the periodic journal line.

        Called from `_halt_process` on the EDGE — the single point every halting
        path funnels into, guarded by the same latch that makes `HaltRecord.reason`
        first-writer-wins. Startup reconciliation, the per-session mismatch, the
        audit-failure hook, `entry.py` and the watchdog all arrive here and get
        exactly one `process.halted` record and one alert between them. Exactly one
        is the requirement: the 2026-08-14 storm was ~68 Slack messages because 34
        restarts each re-announced one unchanged condition.

        **This used to live in `_await_exit_while_halted`** (2026-08-17), which was
        the funnel while every halt parked. A `HaltGrade.ENTRIES` halt never reaches
        that function — the process goes into the scheduler instead — so leaving the
        announcement there would have made the narrow grade the SILENT one, which is
        precisely backwards.
        """
        assert self._audit is not None
        halted = self._halted
        assert halted is not None
        payload = {
            "reason": halted.reason,
            "source": halted.source,
            "at": halted.at.isoformat(),
            # The grade is the operator's "what is still running?" answer and it
            # belongs in the durable record, not only on a page that may be missed.
            "grade": str(halted.grade),
        }
        # Best-effort: a dead audit log is itself one of the ways to get here. The
        # halt stands regardless — the state is latched in memory, and /status reads
        # it from there rather than from the log.
        with contextlib.suppress(AuditWriteError):
            await self._audit.record("process.halted", payload)
        self._alert("process_halted", payload)
        if halted.grade is HaltGrade.ALL:
            logger.critical(
                "HALTED: %s (source=%s). The process stays up and /status stays "
                "reachable; it will not trade again until an operator restarts it.",
                halted.reason,
                halted.source,
            )
        else:
            logger.critical(
                "HALTED (MONITOR-ONLY): %s (source=%s). NO ENTRIES will be taken "
                "again without a restart. Sessions still run and the watchdog still "
                "watches, closes and flattens held positions.",
                halted.reason,
                halted.source,
            )
        self._halted_reminder = asyncio.create_task(
            self._repeat_halted_line(halted), name="halted-reminder"
        )

    async def _repeat_halted_line(self, halted: HaltRecord) -> None:
        """Say "still halted" to the journal every 15 minutes until the process exits.

        A parked process is `active (running)` in `systemctl status`,
        indistinguishable at a glance from a working one (2026-08-17), and a
        MONITOR-ONLY one is worse on that axis: it genuinely is running sessions and
        printing the 15-second status line. So this runs in BOTH grades rather than
        only inside the parking loop it used to be part of. Log only — the Slack
        alert fires once, in `_announce_halt`.

        Reads `self._halted` for the grade rather than closing over the captured
        record's, so an escalation is reflected on the next line.
        """
        while not self._exit.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._exit.wait(), timeout=HALTED_REMINDER_INTERVAL_S
                )
                return
            current = self._halted or halted
            logger.error(
                "still HALTED since %s: %s (source=%s, grade=%s) — %s; "
                "restart the process once the cause is resolved",
                halted.at.isoformat(),
                halted.reason,
                halted.source,
                current.grade,
                (
                    "no session will run"
                    if current.grade is HaltGrade.ALL
                    else "sessions run WITHOUT entries; the watchdog still watches"
                ),
            )

    async def _await_exit_while_halted(self) -> None:
        """Stay up until an operator signals. Nothing else — see `_announce_halt`.

        Nothing is scheduled and nothing can trade — the kill switch is engaged and
        the scheduler is not running — but the process is alive, which is the whole
        point: `/status` answers, `/kill` answers, and Docker's `unless-stopped`
        sees a healthy container instead of restarting into the same wall.

        Reached only under `HaltGrade.ALL`. The announcement and the periodic
        journal line moved out on 2026-08-29 precisely because a narrower halt does
        not come here.
        """
        await self._exit.wait()

    async def run_single_session(self, *, stop_after_s: float | None = None) -> None:
        """Run ONE session now, for `stop_after_s` seconds (or until stop).

        The single-session path for `tools.shadow`: same per-day state build and
        same pipeline as the scheduler, without the calendar. `startup()` must
        already have run; the caller owns `shutdown()`.
        """
        assert self._started, "startup() must run before run_single_session()"
        session_stop = asyncio.Event()
        close = (
            self._now_et() + timedelta(seconds=stop_after_s)
            if stop_after_s is not None
            else self._now_et() + timedelta(days=1)
        )

        async def timer() -> None:
            # Ends the session at the deadline OR when the process is asked to
            # stop (SIGTERM), whichever comes first.
            if stop_after_s is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=stop_after_s)
            else:
                await self._stop.wait()
            session_stop.set()

        timer_task = asyncio.create_task(timer(), name="single-session-timer")
        try:
            await self._run_session_day(et_today(), close, session_stop)
        finally:
            session_stop.set()
            timer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer_task

    def request_stop(self) -> None:
        """Stop TRADING: end the session, start no more. Does not exit the process.

        A halted process sets only this, which is what lets `run()` park.
        """
        self._stop.set()

    def request_exit(self) -> None:
        """Leave the PROCESS. Only an operator signal (SIGINT/SIGTERM) calls this.

        Sets the trading stop too, so a signal mid-session still ends the day
        cleanly through the existing path before the process unwinds — and so a
        signal delivered to a PARKED process (`systemctl stop` on a halted Argus)
        releases the wait and shuts down normally.
        """
        self._exit.set()
        self._stop.set()

    # -- per-session-day -------------------------------------------------------

    async def _run_session_day(
        self, trading_date: date, session_close: datetime, session_stop: asyncio.Event
    ) -> None:
        """Build per-day risk state — REHYDRATED, not blindly fresh — and run the body.

        `session_close` is enforced by the scheduler (it owns the close timer);
        the body just runs until `session_stop`. Re-instantiating the breaker and
        day-P&L anchor here — rather than resetting flags — is what makes rollover
        correct: neither can inherit YESTERDAY's state. The kill switch is the
        deliberate exception, shared across every day so a trip persists.

        But "fresh" was wrong WITHIN a day. Until 2026-08-20 this constructed both
        bare, so a same-day restart (crash, deploy, `systemctl restart`) came up
        untripped AND re-anchored day P&L to the post-loss NLV — silently handing
        back a loss budget that was already spent, with no record that it had
        happened. `load_day_risk_state` reads today's trip and today's original
        anchor back out of the audit log first, so the latch survives a restart
        the same way a day rollover clears it: from the trading date, deliberately.
        → docs/decisions/2026-08-20-daily-loss-breaker-restart-rehydration.md

        **Refuses outright once the process is halted with `HaltGrade.ALL`.**
        `run()` already skips the scheduler in that state, but this is the choke
        point EVERY session path goes through — including `run_single_session`
        (`tools.shadow`), which calls this directly and would otherwise trade a
        halted process, because `startup()` now completes rather than raising when a
        rail refuses.

        Under `HaltGrade.ENTRIES` the day RUNS, monitor-only: the body is handed the
        same `halt_grade` seam and unwires its entry hook, so the watchdog watches
        and nothing enters. The refusal is graded here rather than at `run()` alone
        because this is the choke point, and `tools.shadow` must inherit whichever
        answer applies.
        """
        assert self._audit is not None and self._killswitch is not None
        if self.halt_grade() is HaltGrade.ALL:
            assert self._halted is not None
            logger.critical(
                "refusing to run the session for %s: process HALTED (%s)",
                trading_date,
                self._halted.reason,
            )
            return
        if self._halted is not None:
            logger.warning(
                "session for %s runs MONITOR-ONLY: process halted (%s) — the "
                "watchdog watches, marks, closes and flattens held positions; "
                "no entry will be taken",
                trading_date,
                self._halted.reason,
            )
        try:
            day_risk = await load_day_risk_state(self._audit, trading_date=trading_date)
        except DayRiskRehydrationError as exc:
            # Fail closed, and hard. Not being able to read the day's loss state is
            # indistinguishable from a spent budget, so the session must not start
            # — and since the read goes through the audit log's own connection, a
            # failure here means the recorder is compromised too, which is already
            # a halting condition on every other path.
            logger.critical("day-risk rehydration failed for %s: %s", trading_date, exc)
            self._alert(
                "day_risk_rehydrate_failed",
                {"trading_date": trading_date.isoformat(), "error": str(exc)},
            )
            await self._halt_process(
                f"could not rehydrate the daily-loss state for {trading_date.isoformat()}",
                source="day_risk_rehydrate",
                detail={"error": str(exc)},
            )
            return
        breaker = DailyLossBreaker(
            self._audit,
            daily_max_loss=self._config.risk.daily_max_loss,
            initially_tripped=day_risk.breaker_tripped,
        )
        # Publish the rail for /status the instant it exists and BEFORE the body
        # runs, so the page reads the same object the gate reads for the whole of
        # the day — including a session that comes up already tripped, which is
        # precisely the case the page used to get backwards.
        self._day_breaker = (trading_date, breaker)
        state = SessionState(
            breaker=breaker,
            # Already anchored when a same-day anchor was found, which is what makes
            # the session body's `anchor_day_pnl_if_possible` correctly no-op: the
            # rehydration seam is this constructor, never a special case inside the
            # anchoring call.
            day_pnl=DailyPnl(anchor_nlv=day_risk.anchor_nlv),
            # Today's fills, read back in the same round trip: a same-day restart
            # resumes the day's realized cash and open legs rather than an empty
            # ledger that would hand the spent loss budget back (2026-09-21).
            day_ledger=day_risk.ledger(),
            gate=PreTradeGate(
                config=self._config,
                audit=self._audit,
                killswitch=self._killswitch,
                breaker=breaker,
                alert=self._alert,
            ),
            # The same PROCESS-lifetime switch the gate holds, carried explicitly so
            # the entry path can reach it directly (refuse a candidate after a trip,
            # abandon a walk in flight) without reaching into the gate's internals.
            # Rebuilt into each day's state, but never re-instantiated: a trip has to
            # survive rollover, which is why it is the deliberate exception above.
            killswitch=self._killswitch,
        )
        if day_risk.restored:
            await self._record_restored_day_risk(trading_date, day_risk)
        # Bind the process-lifetime status publisher AND the halt seam into the
        # REAL body via partial, so the body's call signature (and every injected
        # test fake) stays unchanged while the monitoring surface still sees session
        # phases and a sandbox/live reconcile mismatch can halt the process. `halt`
        # is the SAME engage+request_stop pairing the audit-failure path uses.
        body = self._session_body or functools.partial(
            run_session,
            status=self._status,
            halt=self._halt_process,
            # The monitor-only seam. Read LIVE by the body (not captured), because
            # the halt it has to react to may be the one its OWN reconcile is about
            # to raise — and because a process that starts halted-ENTRIES and then
            # reconciles CLEAN must still refuse to wire an entry hook.
            halted_grade=self.halt_grade,
        )
        try:
            await body(
                config=self._config,
                audit=self._audit,
                state=state,
                stop=session_stop,
                trading_date=trading_date,
                alert=self._alert,
                account_number=self._account_number,
            )
        except AuditWriteError:
            # The recorder is dead process-wide; there is no recovering to the
            # next day. End the supervisor so a clean process is restarted (the
            # single-writer lock releases with it). README §9.
            logger.critical(
                "audit log unwritable — stopping the supervisor for restart"
            )
            self.request_stop()
            return
        except Exception as exc:
            # A single day's unexpected failure must NOT take down the always-on
            # process — the timer model isolated days, and this preserves that:
            # alert loudly, record it, and idle to the next trading day. (Login,
            # reconcile, and account failures are handled inside the body; what
            # reaches here is whatever they did NOT catch.)
            #
            # The payload carries the exception, and that is not cosmetic: on
            # 2026-08-26 a `ReadTimeout` out of the per-day reconcile landed here
            # and the record said only `{"trading_date": ...}` — diagnosing it
            # needed a journal dig on the droplet, because the one place that
            # writes the failure down wrote nothing about it. Same two fields, same
            # 500-char bound, as `account.refresh_failed` (`account/stream.py`).
            logger.exception("session for %s failed unexpectedly", trading_date)
            failure = {
                "trading_date": trading_date.isoformat(),
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
            self._alert("session_error", failure)
            with contextlib.suppress(AuditWriteError):
                await self._audit.record("session.error", failure)
        if self._audit.failed is not None:
            # The signal loop (or the record above) may have latched the audit log
            # failed without an AuditWriteError reaching here. Same conclusion:
            # stop for a restart.
            self.request_stop()

    def _breaker_for_today(self) -> DailyLossBreaker | None:
        """The daily-loss rail governing entries RIGHT NOW, or None if none is armed.

        The seam `/status` reads through, so the page reports the object the
        pre-trade gate holds rather than inferring the rail's state from a
        `daily_loss.breach` row. A row outlives the process that wrote it and a
        latch does not, which is how the page came to render TRIPPED in red after a
        same-day restart while the gate was still authorizing entries.

        **Trading-date-scoped, and that is not a nicety.** `_run_session_day` builds
        one breaker per day and `_day_breaker` keeps the last one built, so an
        unscoped read would report Tuesday's spent budget all through Wednesday's
        pre-open — the same misreport in mirror image. The latch is per trading date
        (`risk.rehydrate`); so is the report of it.

        None is a THIRD state, distinct from "not tripped": no session has built
        today's breaker yet, or the process is halted and never will. Nothing is
        entering in that state either, so there is no rail to speak for.
        """
        if self._day_breaker is None:
            return None
        built_for, breaker = self._day_breaker
        return breaker if built_for == self._now_et().date() else None

    async def _record_restored_day_risk(
        self, trading_date: date, day_risk: DayRiskState
    ) -> None:
        """Say, out loud and in the trail, that this session did not start the day.

        A restart into a day already under way is otherwise invisible: the breaker
        writes no second `daily_loss.breach` (the latch is idempotent by design)
        and the anchor is not re-recorded, so without this record the audit trail
        of an 11:40 restart looks identical to a process that ran since the open.

        The Slack alert fires only for a RESTORED TRIP, because that one changes
        what the desk should expect for the rest of the day — the process is up,
        healthy, and will enter nothing. A restored anchor alone is normal restart
        bookkeeping and gets the record and a log line, not a page.
        """
        assert self._audit is not None
        payload = {"trading_date": trading_date.isoformat(), **day_risk.to_payload()}
        # Best-effort: a dead recorder has already halted the process through
        # `on_failure`, and the restored state stands either way — it was
        # reconstructed from what is already on disk.
        with contextlib.suppress(AuditWriteError):
            await self._audit.record(RESTORED_CATEGORY, payload)
        if day_risk.breaker_tripped:
            logger.warning(
                "same-day restart into a TRIPPED daily-loss breaker (%s): the day's "
                "loss budget is spent and this session will enter nothing",
                trading_date,
            )
            self._alert("daily_loss_breaker_restored", payload)
        else:
            logger.info(
                "same-day restart: day-P&L anchor restored to NLV %s for %s",
                day_risk.anchor_nlv,
                trading_date,
            )

    async def _announce_restored_kill(self, state: KillSwitchState) -> None:
        """Say, out loud and in the trail, that this process came up halted.

        The `_record_restored_day_risk` precedent, for the same reason: nothing
        else marks it. The restoration deliberately calls no `engage`, so there is
        no second `kill_switch.trip` — which is what keeps `/status`'s `since`
        reporting the moment the operator actually pressed it rather than the
        moment the box rebooted — and without this record an 03:00 reboot into a
        Friday halt would be indistinguishable in the log from a process that had
        been halted all along.

        **Exactly one page per restored start, unconditionally.** The degraded
        case (the state could not be read, so the switch engaged fail-closed) gets
        its own Tier 1 record for the detail, but rides the SAME alert — two pages
        for one event is noise, and the payload already carries `restore_error`.
        """
        assert self._audit is not None
        payload = state.to_payload()
        # Best-effort, exactly like `_record_restored_day_risk` and
        # `_announce_halt`: a dead recorder has its own halt path, and the
        # restored state stands either way — it was reconstructed from disk.
        #
        # Two suppressions rather than one block around both: the restored record
        # is the primary fact and the degradation is the footnote, so a failure
        # writing either must not cost the other.
        with contextlib.suppress(AuditWriteError):
            await self._audit.record(KILL_RESTORED_CATEGORY, payload)
        if state.restore_error is not None:
            with contextlib.suppress(AuditWriteError):
                await self._audit.record(RESTORE_FAILED_CATEGORY, payload)
        logger.critical(
            "started HALTED: a kill switch trip from %s (%s: %s) was never resumed. "
            "No entry will be taken until POST /resume clears it — a restart will "
            "not.",
            state.trip_ts or "an unreadable record",
            state.source,
            state.reason,
        )
        self._alert("kill_switch_restored", payload)

    # -- internals ---------------------------------------------------------

    async def _reconcile(self) -> None:
        """Broker reconciliation at PROCESS START (spec §6 non-negotiable).

        With a reconciler wired, fetch broker positions, compare to expected
        state, record the verdict (Tier 1), and refuse to resume on a mismatch —
        but the refusal is ENVIRONMENT-GATED (`mismatch_halts_process`) identically
        to the per-session reconcile (`session._reconcile_or_abort`), so the two
        call sites stay consistent:

          - sandbox/live: a mismatch is fatal. Raise `ReconciliationError`, which
            propagates out of `startup()` and tears the half-built process down
            BEFORE it records `process.start` — the strongest possible "refuse to
            resume", available here precisely because nothing is running yet to
            halt (contrast the per-session path, which engages the kill switch and
            stops a process that is already up).
          - shadow: a mismatch is not fatal — nothing executes. Record the verdict
            and proceed, matching the per-session lenient path. In production shadow
            wires NO process-start reconciler (the authoritative reconcile runs
            inside each session body); this branch only bites if one is injected,
            and even then must not hard-halt where the per-session path wouldn't.

        With an INJECTED reconciler (tests, tools) that one is used as-is. Without
        one, sandbox/live now BUILD a real one — `session.startup_reconcile` opens a
        short-lived trading session, runs the full position+order check, and closes it
        again, so the process-lifetime layer still holds no broker credentials. That
        is chunk 5's replacement for the `reconcile.skipped` record sandbox used to
        get; shadow still skips, because it has no broker session at process start and
        nothing of its own to find at the broker.
        """
        assert self._audit is not None

        def _inputs() -> tuple[date, bool]:
            # ONE reading of the clock for both inputs, and the SHARED settlement
            # helper the two session-side call sites use — this one just feeds it
            # the app's own injectable ET clock instead of `clock.et_now`. Two
            # readings could straddle midnight and judge one day's expiry against
            # another day's close. Called PER outer attempt, because the retry
            # budget is long enough for the ET date to roll over inside it.
            now_et = self._now_et()
            return now_et.date(), session_settled(self._config, now_et.date(), now_et)

        # The unreachable-broker refusal is ENVIRONMENT-GATED through the SAME
        # function the mismatch refusal uses. Not a second boundary — the same one,
        # fed a second failure class, and the docstring's own rationale carries over
        # unchanged: the split turns on "shadow submits nothing, so there is no order
        # state to protect", which is a statement about the consequence of resuming
        # BLIND. Resuming blind is resuming blind whether the answer disagreed or
        # never came.
        #
        # In production shadow never reaches either branch below (it wires no
        # process-start reconciler at all); this only bites for an injected one, and
        # even then it must not hard-halt where the per-session path would not.
        def _refuse_unreachable(exc: BrokerUnreachable) -> None:
            if mismatch_halts_process(self._config.environment):
                raise exc
            logger.warning(
                "shadow: could not reach the broker at process start (%s) — "
                "proceeding (shadow executes nothing; the record and the alert are "
                "already written)", exc,
            )

        # The page must not read `idle` while the process is blocked on the broker.
        # Cheap before 2026-08-26 and load-bearing after it: the reconcile can now
        # occupy `[reconcile] retry_budget_s` — half an hour — and a parked-looking
        # `idle` for that long is the same class of misreport as a `daily_loss.breach`
        # row outliving its process.
        self._status.set_reconciling()
        try:
            if self._reconciler is not None:
                # Injected: the caller owns the comparison. Still goes through the
                # shared retry-record-alert sequence so it cannot drift from the
                # other call sites.
                try:
                    result = await reconcile_with_retry(
                        self._reconciler,
                        config=self._config,
                        audit=self._audit,
                        alert=self._alert,
                        inputs=_inputs,
                        order_store=self._order_store_for_reset(),
                        stop=self._stop,
                    )
                except BrokerUnreachable as exc:
                    _refuse_unreachable(exc)
                    return
                self._apply_reconcile_verdict(result)
                return

            if mismatch_halts_process(self._config.environment):
                # Build one. Sandbox/live must not start without having ASKED the
                # broker, and "could not ask" is not "nothing to report" — that
                # premise is unchanged from when this comment was first written.
                #
                # What changed on 2026-08-26 is the CONSEQUENCE. This used to say the
                # failure "tears the half-built process down", which was true when
                # written and stopped being true on 2026-08-17, when the control bind
                # and `process.start` moved AHEAD of this call: by here the process
                # genuinely HAS started and is serving /status and /kill, so tearing
                # down throws away the surface an operator needs to see why. It is now
                # retried on the `[reconcile]` budget and then PARKED — the same seam
                # a mismatch has used since that same 2026-08-17 change.
                # → docs/decisions/2026-08-26-reconcile-broker-unreachable.md
                result = await startup_reconcile(
                    config=self._config,
                    audit=self._audit,
                    alert=self._alert,
                    account_number=self._account_number,
                    stop=self._stop,
                )
                self._apply_reconcile_verdict(result)
                return

            await self._audit.record("reconcile.skipped", {"reason": "shadow_no_broker"})
        finally:
            # Back to IDLE on every path, including the halting ones: the phase says
            # what the SESSION layer is doing, and no session is running either way.
            # A halt is reported by `StatusSnapshot.halted`, which sits above the
            # phase and changes what it means — leaving `reconciling` latched would
            # have the page claim work is in progress on a parked process.
            self._status.set_idle()

    def _apply_reconcile_verdict(self, result: ReconciliationResult) -> None:
        """Refuse to resume, or note that it was clean. The REFUSAL half only.

        The record and the alert already happened in `reconcile_and_record`; all that
        is left here is this call site's own way of refusing, which at process start is
        the strongest one available — raise, so `startup()` unwinds and nothing is
        running at all. (The per-session site instead halts a process already up.)
        """
        if result.ok:
            logger.info("broker reconciliation clean")
            return
        if mismatch_halts_process(self._config.environment):
            raise ReconciliationError(result)
        logger.warning(
            "shadow: startup reconcile mismatch — proceeding (shadow executes "
            "nothing; the per-session reconcile applies the same lenient path)"
        )

    def _order_store_for_reset(self) -> Any:
        """The order ledger the sandbox-reset check clears, for an INJECTED reconciler.

        Read off the reconciler rather than rebuilt, so an injected one that reconciles
        orders gets reset handling and one that does not gets none — the ledger that is
        cleared is always the same ledger that was compared.
        """
        return getattr(self._reconciler, "order_store", None)

    async def _halt_process(
        self,
        reason: str,
        *,
        source: str,
        grade: HaltGrade = HaltGrade.ALL,
        detail: dict | None = None,
    ) -> None:
        """Engage the kill switch AND request process stop — the ONE halt pairing.

        **`grade` defaults to `ALL`, which is today's behaviour**, so every caller
        that does not pass one — the audit-failure hook, `entry.py`,
        `BrokerUnreachable`, `SessionAborted`, day-risk rehydration, and the
        watchdog's `AuditWriteError` exclusion (`_halt_on_exception`) — keeps
        stopping everything without being edited, and a future halt source nobody
        graded gets the answer that stops everything too. Two kinds of caller pass
        `ENTRIES`: the two reconciliation sites, only when `reconcile.halt_grade`
        says the verdict carries no position implication; and, since 2026-09-10,
        the watchdog's exception path (`PositionWatchdog._halt_entries`), because a
        watchdog exception must not take the watchdog.
        → docs/decisions/2026-09-10-a-watchdog-exception-must-not-take-the-watchdog.md
        A `HaltGrade.ENTRIES` halt skips step 2 alone: the switch is still engaged,
        the halt is still latched and permanent, `/status` still reports HALTED,
        and the process still will not trade again without a human — but the
        scheduler keeps running so held positions keep their watchdog.
        → docs/decisions/2026-08-29-halt-scope-split.md

        Two actions, both required, and paired here in one helper every terminal
        path calls so no caller can get only half of it right:

        1. Engage the kill switch, blocking new entries immediately. (`engage`
           flips state first and tolerates its own trip-record failing, so the
           halt stands with or without a record. `source`/`detail` attribute the
           trip in the audit trail and thus in /status, for free.)
        2. Request process stop, so the supervisor winds down for a clean restart
           that re-runs the rail from a good state. Engaging alone halts new
           entries but leaves the process up; a terminal condition — a
           latched-failed recorder (no auto-recovery, CLAUDE.md) or a sandbox/live
           reconcile mismatch — also needs the process to actually STOP.

        This is what keeps the halt UNIFORM across every path. The signal loop
        already stops itself on `AuditWriteError` and `_run_session_day` translates
        that into a stop, but a halt raised OUTSIDE a session loop (the always-on
        `/kill` endpoint's failed trip write; the per-session reconcile mismatch)
        has no loop to carry the signal — this pairing does. Setting the stop event
        cannot fail and is idempotent, so calling this on top of a path that
        already stops is safe.

        NB `request_stop` stops TRADING, not the process: since 2026-08-17 `run()`
        parks on a halt instead of exiting, so the control surface survives it.
        """
        assert self._killswitch is not None
        await self._killswitch.engage(reason, source=source, detail=detail)
        first = self._mark_halted(reason, source=source, grade=grade)
        # The announcement is on the EDGE, not in the parking loop (2026-08-29).
        # It used to live in `_await_exit_while_halted`, which an ENTRIES halt
        # never reaches — the process goes into the scheduler instead — so the
        # `process.halted` record and the `process_halted` page would simply have
        # gone missing for the one grade that most needs explaining. Guarded on
        # the same latch that makes `reason` first-writer-wins, so "exactly one
        # record and one alert per halt" is preserved whatever cascades after it.
        if first:
            await self._announce_halt()
        assert self._halted is not None
        if self._halted.grade is HaltGrade.ALL:
            self.request_stop()

    def _mark_halted(self, reason: str, *, source: str, grade: HaltGrade) -> bool:
        """Latch the halted state. Returns True if this call is what latched it.

        **Two fields, two opposite update rules, and neither is an accident.**

        `reason`/`source`/`at` are FIRST-writer-wins. A halt often cascades — a
        reconcile mismatch stops the day, and the audit log may then latch failed
        on the way down. The first reason is the useful one; overwriting it with a
        downstream symptom is how a post-mortem loses the cause (2026-08-17).

        `grade` is STRICTEST-wins (`HaltGrade.tighten`). An `ENTRIES` halt followed
        by an `ALL` halt is `ALL` — the second condition genuinely does stop
        everything, and inheriting the first one's narrower scope would leave the
        watchdog running through a state nobody cleared it for. The reverse never
        happens: an `ALL` halt is never relaxed by a later narrow verdict, which is
        what makes the per-session re-derivation safe to run on an already-halted
        process. Unifying these two rules is the tidy-up this docstring exists to
        stop.
        """
        if self._halted is None:
            self._halted = HaltRecord(
                reason=reason, source=source, at=self._now_et(), grade=grade
            )
            return True
        tightened = self._halted.grade.tighten(grade)
        if tightened is not self._halted.grade:
            # Reason, source and instant are the FIRST halt's, untouched. Only the
            # scope escalates, and it escalates in place rather than through a
            # second record — a post-mortem should read one halt that widened, not
            # two halts.
            logger.critical(
                "halt scope escalated to %s by %s (%s); the original halt at %s "
                "stands as the cause",
                tightened, source, reason, self._halted.at.isoformat(),
            )
            self._halted = dataclasses.replace(self._halted, grade=tightened)
        return False

    def halt_grade(self) -> HaltGrade | None:
        """How wide the halt in force is, or None if this process is not halted.

        THE seam every "is this process halted, and what does that stop?" question
        goes through — `run()`, `_run_session_day`, and the session body's
        monitor-only decision. Read live rather than captured, for the same reason
        `StatusService`'s `halt_state` is: the halt happens long after everything
        that needs to ask about it was built.
        """
        return self._halted.grade if self._halted is not None else None


    async def _halt_on_audit_failure(self, detail: str) -> None:
        """Halt the process when the audit log can no longer persist.

        Delegates the engage+stop pairing to `_halt_process` (the shared helper) so
        the audit-failure halt and the reconcile-mismatch halt cannot drift. The
        kill switch's own trip write may itself fail here (a dead recorder is the
        trigger); `engage` swallows that so the halt still stands, and the paired
        `request_stop` guarantees the process winds down for a restart even when
        the trip went unrecorded — including when hit while idle between sessions.
        """
        self._alert("audit_log_down", {"detail": detail})
        await self._halt_process(f"audit log unwritable ({detail})", source="audit_log")

    async def _trip_on_stall(self, lag_s: float) -> None:
        assert self._killswitch is not None
        await self._killswitch.engage(
            f"sustained event-loop stall (lag {lag_s:.3f}s)", source="heartbeat"
        )

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_exit)
            except NotImplementedError:  # e.g. non-Unix; falls back to KeyboardInterrupt
                logger.warning("signal handler for %s not available on this platform", sig)
